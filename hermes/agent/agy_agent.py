"""Antigravity CLI (`agy`) native AIAgent implementation for the customized Hermes fork.

Implements the exact interface expected by Hermes's `GatewayRunner` & `TurnRunner`:
- `run_conversation(user_message, conversation_history=..., task_id=..., **kwargs)`
- `interrupt(message=None)` (terminates the active `agy` subprocess immediately)
- `stream_delta_callback`, `tool_progress_callback`, `status_callback`, etc.
- Multi-account round-robin / failover / manual rotation across `default`, `akun2`, `akun3`
- Cross-account context continuity combining Hermes SQLite `conversation_history` +
  `SharedContextStore` so memory & conversation context never drop across account switches.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from agent.agy_state import (
    AGY_BIN,
    BOT_BASE_DIR,
    DEFAULT_WORKDIR,
    IDLE_TIMEOUT,
    PRINT_TIMEOUT,
    VALID_EFFORTS,
    VALID_MODES,
    build_agy_env,
    classify_agy_error,
    ensure_account_home,
    get_account_manager,
    get_chat_state,
    get_shared_context,
)

logger = logging.getLogger("agent.agy_agent")

_AGY_TO_HERMES_TOOL: dict[str, str] = {
    "run_command": "terminal",
    "read_file": "read_file",
    "view_file": "read_file",
    "write_file": "write_file",
    "write_to_file": "write_file",
    "edit_file": "patch",
    "replace_file_content": "patch",
    "multi_replace_file_content": "patch",
    "sed_file": "patch",
    "grep_search": "search_files",
    "find_by_name": "search_files",
    "list_dir": "search_files",
    "search_web": "web_search",
    "read_url_content": "web_extract",
    "open_browser_url": "browser_navigate",
    "read_browser_page": "browser_snapshot",
    "invoke_subagent": "delegate_task",
    "run_workflow": "delegate_task",
    "generate_image": "image_generate",
    "call_mcp_tool": "mcp",
}


def _shorten(text: str, limit: int = 64) -> str:
    single = " ".join(str(text or "").splitlines()).strip()
    if len(single) <= limit:
        return single
    return single[: limit - 1] + "…"


def _short_path(path_str: str) -> str:
    s = str(path_str or "").strip()
    if not s:
        return ""
    home = str(Path.home())
    if home and s.startswith(home + "/"):
        s = "~/" + s[len(home) + 1 :]
    if len(s) <= 44:
        return s
    return "…/" + "/".join(Path(s).parts[-2:])


def _tool_preview(tool_name: str, params: dict[str, Any] | None) -> str:
    p = params if isinstance(params, dict) else {}
    if tool_name == "run_command":
        return _shorten(p.get("CommandLine") or p.get("command") or p.get("toolSummary") or "", 60)
    if tool_name in ("read_file", "view_file", "write_file", "write_to_file", "edit_file", "replace_file_content", "multi_replace_file_content", "sed_file"):
        return _short_path(p.get("Path") or p.get("TargetFile") or p.get("AbsolutePath") or "")
    if tool_name == "grep_search":
        return _shorten(p.get("Query") or "", 52)
    if tool_name in ("find_by_name", "list_dir"):
        target = _short_path(p.get("SearchDirectory") or p.get("DirectoryPath") or "")
        pat = p.get("Pattern") or ""
        return _shorten(f"{pat} {target}".strip(), 52)
    if tool_name == "search_web":
        return _shorten(p.get("query") or "", 54)
    if tool_name in ("read_url_content", "open_browser_url", "read_browser_page"):
        return _shorten(p.get("Url") or p.get("url") or "", 54)
    if tool_name in ("invoke_subagent", "run_workflow"):
        return _shorten(p.get("Role") or p.get("Description") or p.get("TypeName") or "", 54)
    if tool_name == "call_mcp_tool":
        return f"{p.get('ServerName') or ''}.{p.get('ToolName') or ''}".strip(".")
    return ""


def _materialize_image_data_url(data_url: str) -> Optional[str]:
    """Decode a data:image/...;base64,... URL into hermes_home/cache/images so agy can read it."""
    if not data_url.startswith("data:image/") or ";base64," not in data_url:
        return None
    header, b64 = data_url.split(";base64,", 1)
    mime = header[5:].strip().lower()
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".jpg")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    digest = hashlib.sha256(raw).hexdigest()[:12]
    hermes_home = Path(os.environ.get("HERMES_HOME") or BOT_BASE_DIR).expanduser().resolve()
    cache_dir = hermes_home / "cache" / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"img_{digest}{ext}"
    if not out_path.exists():
        out_path.write_bytes(raw)
    return str(out_path)


def _extract_user_prompt(user_message: Any) -> str:
    """Convert plain string or OpenAI-style multimodal content list into an agy-readable prompt."""
    if isinstance(user_message, str):
        return user_message.strip()
    if isinstance(user_message, list):
        parts: list[str] = []
        for item in user_message:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                txt = str(item.get("text") or "").strip()
                if txt:
                    parts.append(txt)
            elif itype == "image_url":
                url = str((item.get("image_url") or {}).get("url") or "").strip()
                if url.startswith("data:image/"):
                    saved = _materialize_image_data_url(url)
                    if saved:
                        parts.append(f"[Lampiran gambar tersimpan di: {saved}]")
                elif url:
                    parts.append(f"[Lampiran gambar: {url}]")
        return "\n\n".join(parts).strip()
    return str(user_message or "").strip()


def _history_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = [
            str(p.get("text") or "").strip()
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and str(p.get("text") or "").strip()
        ]
        return "\n".join(chunks).strip()
    return str(content or "").strip()


_AGYMES_IDENTITY_PREAMBLE = (
    "[IDENTITAS]\n"
    "Namamu adalah Agymes, asisten AI pribadi di Telegram. "
    "Jika ditanya nama atau siapa kamu, perkenalkan dirimu sebagai Agymes. "
    "Jangan menyebut nama model atau engine internal kecuali user bertanya langsung.\n\n"
)


def _with_agymes_identity(prompt: str) -> str:
    return f"{_AGYMES_IDENTITY_PREAMBLE}{prompt}"


def _build_prompt_from_hermes_history(
    conversation_history: list[dict[str, Any]] | None,
    current_prompt: str,
    *,
    max_context_chars: int = 22000,
) -> str:
    """Format Hermes SQLite conversation history for cross-account agy continuity."""
    if not conversation_history:
        return current_prompt

    turns: list[tuple[str, str]] = []
    cur_user = ""
    for msg in conversation_history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        txt = _history_text(msg.get("content"))
        if not txt:
            continue
        if role == "user":
            if cur_user:
                turns.append((cur_user, ""))
            cur_user = txt
        elif role == "assistant":
            turns.append((cur_user, txt))
            cur_user = ""
    if cur_user:
        turns.append((cur_user, ""))

    if not turns:
        return current_prompt

    header = (
        "[KONTEKS PERCAKAPAN SEBELUMNYA DI CHAT TELEGRAM INI]\n"
        "Kamu adalah asisten yang sama yang melanjutkan percakapan ini secara langsung dan konsisten. "
        "Jawab pesan terbaru user berdasarkan konteks di bawah ini. "
        "Jangan menyebut blok konteks ini atau perpindahan akun kecuali user bertanya langsung.\n"
    )
    footer = f"\n[PESAN TERBARU USER]\n{current_prompt}"
    budget = max(2000, max_context_chars - len(header) - len(footer))

    rendered: list[str] = []
    used = 0
    for idx in range(len(turns) - 1, -1, -1):
        u_txt, a_txt = turns[idx]
        recent = idx >= len(turns) - 4
        u_lim = 2400 if recent else 900
        a_lim = 3600 if recent else 1400
        if len(u_txt) > u_lim:
            u_txt = u_txt[: int(u_lim * 0.65)] + "\n…[dipotong]…\n" + u_txt[-(u_lim - int(u_lim * 0.65) - 20) :]
        if len(a_txt) > a_lim:
            a_txt = a_txt[: int(a_lim * 0.65)] + "\n…[dipotong]…\n" + a_txt[-(a_lim - int(a_lim * 0.65) - 20) :]
        block = f"--- Giliran {idx + 1} ---\nUser: {u_txt}\nAsisten: {a_txt}\n"
        if used + len(block) <= budget or not rendered:
            rendered.append(block)
            used += len(block)
        else:
            break

    rendered.reverse()
    return "\n".join([header, "[Riwayat percakapan terbaru]\n" + "\n".join(rendered), footer])


@dataclass
class _AgyContextStub:
    """Minimal context_compressor shape read by Hermes's /status, /context, & TurnRunner."""
    last_prompt_tokens: int = 0
    context_length: int = 1_000_000
    compression_count: int = 0
    context_source: str = "provider"


class AgyAIAgent:
    """Drop-in Hermes AIAgent backed by Antigravity CLI (`agy`) + multi-account rotation."""

    def __init__(
        self,
        *,
        model: str = "gemini-3.8-flash-medium",
        provider: str = "antigravity",
        requested_provider: str | None = "antigravity",
        base_url: str | None = "process://antigravity",
        api_mode: str | None = "chat_completions",
        max_iterations: int = 150,
        session_id: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        chat_name: str | None = None,
        chat_type: str | None = None,
        thread_id: str | None = None,
        gateway_session_key: str | None = None,
        session_db: Any = None,
        reasoning_config: Any = None,
        service_tier: Any = None,
        request_overrides: Any = None,
        **kwargs: Any,
    ) -> None:
        self.model = (model or "gemini-3.8-flash-medium").strip()
        self.provider = "antigravity"
        self.requested_provider = requested_provider or "antigravity"
        self.base_url = base_url or "process://antigravity"
        self.api_mode = api_mode or "chat_completions"
        self.max_iterations = max_iterations
        self.session_id = session_id or ""
        self.platform = platform or "telegram"
        self.user_id = user_id
        self.chat_id = str(chat_id) if chat_id is not None else ""
        self.chat_name = chat_name
        self.chat_type = chat_type
        self.thread_id = thread_id
        self.gateway_session_key = gateway_session_key or ""
        self._session_db = session_db
        self.reasoning_config = reasoning_config
        self.service_tier = service_tier
        self.request_overrides = dict(request_overrides or {}) if isinstance(request_overrides, dict) else {}

        # Standard Hermes AIAgent telemetry & lifecycle fields
        self.session_prompt_tokens: int = 0
        self.session_completion_tokens: int = 0
        self.session_output_tokens: int = 0
        self._session_messages: list[dict[str, Any]] = []
        self._primary_runtime: dict[str, Any] = {
            "provider": "antigravity",
            "requested_provider": "antigravity",
            "base_url": self.base_url,
            "api_mode": self.api_mode,
        }
        self._fallback_activated: bool = False
        self._fallback_model: Any = None
        self._pending_fallback_notice: Optional[str] = None
        self._last_compaction_in_place: bool = False
        self._silence_marker_needs_reply: bool = False
        self._memory_manager: Any = None
        self._db_flush_scan_prefix: Any = None
        self.context_compressor = _AgyContextStub(
            context_length=1_000_000 if "gemini" in self.model.lower() else 200_000,
        )

        # Callbacks wired per-turn by TurnRunner._wire_turn_agent_callbacks
        self.stream_delta_callback: Optional[Callable[[str], None]] = None
        self.tool_progress_callback: Optional[Callable[..., None]] = None
        self.tool_start_callback: Optional[Callable[..., None]] = None
        self.tool_complete_callback: Optional[Callable[..., None]] = None
        self.interim_assistant_callback: Optional[Callable[[str], None]] = None
        self.status_callback: Optional[Callable[[str], None]] = None
        self.notice_callback: Optional[Callable[..., None]] = None
        self.notice_clear_callback: Optional[Callable[..., None]] = None
        self.step_callback: Optional[Callable[..., None]] = None
        self.event_callback: Optional[Callable[..., None]] = None
        self.reasoning_callback: Optional[Callable[..., None]] = None
        self.background_review_callback: Optional[Callable[..., None]] = None

        # Active subprocess & interrupt synchronization
        self._proc_lock = threading.Lock()
        self._active_proc: Optional[subprocess.Popen[str]] = None
        self.is_interrupted: bool = False
        self.interrupt_message: Optional[str] = None
        self._last_used_account: str = ""

    def interrupt(self, message: Optional[str] = None) -> None:
        self.is_interrupted = True
        self.interrupt_message = message
        with self._proc_lock:
            proc = self._active_proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()

    def clear_interrupt(self) -> None:
        self.is_interrupted = False
        self.interrupt_message = None

    def release_clients(self) -> None:
        with self._proc_lock:
            proc = self._active_proc
            self._active_proc = None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
                proc.wait(timeout=2)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()

    def close(self) -> None:
        self.release_clients()

    def _emit_status(self, text: str) -> None:
        cb = self.status_callback
        if callable(cb) and text:
            with contextlib.suppress(Exception):
                cb(text)

    def _emit_delta(self, delta: str) -> None:
        cb = self.stream_delta_callback
        if callable(cb) and delta:
            with contextlib.suppress(Exception):
                cb(delta)

    def _emit_tool_progress(self, event_type: str, tool_name: str, preview: str, params: dict[str, Any]) -> None:
        cb = self.tool_progress_callback
        if callable(cb):
            hermes_name = _AGY_TO_HERMES_TOOL.get(tool_name, tool_name)
            with contextlib.suppress(Exception):
                cb(event_type, hermes_name, preview, params)

    def _resolve_effective_effort(self, chat_cfg: dict[str, Any]) -> str:
        eff = str(chat_cfg.get("effort") or "").strip().lower()
        if eff in VALID_EFFORTS and eff:
            return eff
        if isinstance(self.reasoning_config, dict):
            rc_eff = str(self.reasoning_config.get("effort") or "").strip().lower()
            if rc_eff in VALID_EFFORTS and rc_eff:
                return rc_eff
        return ""

    def run_conversation(
        self,
        user_message: Any,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        task_id: str | None = None,
        persist_user_message: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.clear_interrupt()
        prompt_text = _extract_user_prompt(user_message)
        clean_user_text = _extract_user_prompt(persist_user_message) if persist_user_message is not None else prompt_text
        if not prompt_text:
            return {
                "final_response": "",
                "messages": list(conversation_history or []),
                "api_calls": 0,
                "completed": True,
                "failed": False,
                "interrupted": False,
                "provider": "antigravity",
                "model": self.model,
                "agent_persisted": False,
            }

        chat_key = self.chat_id or self.gateway_session_key or "default"
        accounts = get_account_manager()
        chat_state = get_chat_state()
        shared_history = get_shared_context()

        chat_cfg = chat_state.get_chat(chat_key)
        workdir = str(chat_cfg.get("workdir") or DEFAULT_WORKDIR)
        if not Path(workdir).is_dir():
            workdir = DEFAULT_WORKDIR
        mode = str(chat_cfg.get("mode") or "auto")
        if mode not in VALID_MODES:
            mode = "auto"
        effort = self._resolve_effective_effort(chat_cfg)
        rotation_mode = str(chat_cfg.get("rotation_mode") or "rotate")
        pinned_account = str(chat_cfg.get("pinned_account") or "default")
        last_account = str(chat_cfg.get("last_account") or "")
        convs_by_account: dict[str, str] = dict(chat_cfg.get("conversations_by_account") or {})

        candidates = accounts.plan_candidates(rotation_mode, pinned_account)
        if not candidates:
            candidates = [{"name": "default", "home": str(Path.home())}]

        hermes_hist = list(conversation_history or [])
        has_shared_turns = shared_history.turn_count(chat_key) > 0

        final_result: Optional[dict[str, Any]] = None
        stream_buf = ""
        stderr_lines: list[str] = []
        raw_non_json: list[str] = []
        tools_used: list[str] = []
        rc = -1
        timed_out = False
        used_account = candidates[0]["name"]
        attempt_succeeded = False
        failover_notes: list[str] = []
        streamed_any = False

        for attempt_idx, acc in enumerate(candidates):
            if self.is_interrupted:
                break

            acc_name = acc["name"]
            acc_home = acc["home"]
            ensure_account_home(Path(acc_home))
            used_account = acc_name
            self._last_used_account = acc_name

            final_result = None
            stream_buf = ""
            stderr_lines = []
            raw_non_json = []
            tools_used = []
            seen_tools: dict[Any, str] = {}
            rc = -1
            timed_out = False
            streamed_any = False

            native_cid = convs_by_account.get(acc_name)
            use_native = bool(native_cid) and (last_account == acc_name)

            if use_native:
                effective_prompt = _with_agymes_identity(prompt_text)
            elif hermes_hist:
                effective_prompt = _with_agymes_identity(_build_prompt_from_hermes_history(hermes_hist, prompt_text))
            elif has_shared_turns:
                effective_prompt = _with_agymes_identity(shared_history.build_contextual_prompt(chat_key, prompt_text))
            else:
                effective_prompt = _with_agymes_identity(prompt_text)

            cmd = [AGY_BIN, "--output-format", "stream-json"]
            if mode == "auto":
                cmd.append("--dangerously-skip-permissions")
            else:
                cmd.extend(["--mode", mode])
            if self.model and self.model != "default":
                cmd.extend(["--model", self.model])
            if effort:
                cmd.extend(["--effort", effort])
            if use_native and native_cid:
                cmd.extend(["--conversation", native_cid])
            if PRINT_TIMEOUT > 0:
                cmd.extend(["--print-timeout", f"{PRINT_TIMEOUT}s"])
            cmd.append(f"-p={effective_prompt}")

            logger.info(
                "AgyAIAgent chat=%s -> account=%s (attempt %d/%d, mode=%s, model=%s, native=%s, hist=%d)",
                chat_key,
                acc_name,
                attempt_idx + 1,
                len(candidates),
                rotation_mode,
                self.model,
                use_native,
                len(hermes_hist),
            )

            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=build_agy_env(acc_home),
            )
            with self._proc_lock:
                self._active_proc = proc

            last_activity = [time.monotonic()]
            idle_timed_out = False

            def _drain_stderr(p: subprocess.Popen[str], sink: list[str]) -> None:
                if p.stderr is None:
                    return
                for line in p.stderr:
                    last_activity[0] = time.monotonic()
                    if len(sink) < 200:
                        sink.append(line.rstrip("\n"))

            stderr_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_lines), daemon=True)
            stderr_thread.start()

            wall_deadline = (time.monotonic() + PRINT_TIMEOUT + 30) if PRINT_TIMEOUT > 0 else None

            def _timeout_watcher(p: subprocess.Popen[str]) -> None:
                nonlocal timed_out, idle_timed_out
                while p.poll() is None:
                    now_m = time.monotonic()
                    if wall_deadline is not None and now_m > wall_deadline:
                        timed_out = True
                        with contextlib.suppress(OSError):
                            p.terminate()
                        break
                    if IDLE_TIMEOUT > 0 and (now_m - last_activity[0]) > IDLE_TIMEOUT:
                        timed_out = True
                        idle_timed_out = True
                        with contextlib.suppress(OSError):
                            p.terminate()
                        break
                    time.sleep(1.0)

            watchdog = threading.Thread(target=_timeout_watcher, args=(proc,), daemon=True)
            watchdog.start()

            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    last_activity[0] = time.monotonic()
                    if self.is_interrupted:
                        break
                    line = raw_line.strip()
                    if not line:
                        continue
                    if not line.startswith("{"):
                        raw_non_json.append(line)
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        raw_non_json.append(line)
                        continue

                    etype = evt.get("event")
                    if etype == "init":
                        cid = evt.get("conversation_id")
                        if cid:
                            chat_state.set_account_conversation(chat_key, acc_name, cid)
                        continue

                    if etype == "step_update":
                        su = evt.get("step_update") or {}
                        cid = su.get("conversation_id")
                        if cid:
                            chat_state.set_account_conversation(chat_key, acc_name, cid)
                        stype = su.get("step_type")
                        state = str(su.get("state") or "")
                        step_idx = su.get("step_index")

                        if stype == "tool":
                            tinfo = su.get("tool_info") or {}
                            tname = str(su.get("tool_name") or tinfo.get("name") or "tool")
                            tparams = tinfo.get("parameters") or {}
                            preview = _tool_preview(tname, tparams)
                            prev_state = seen_tools.get(step_idx)
                            if prev_state is None:
                                seen_tools[step_idx] = state or "ACTIVE"
                                label = f"{tname}: {preview}" if preview else tname
                                if len(tools_used) < 24:
                                    tools_used.append(label[:100])
                                self._emit_tool_progress("tool.started", tname, preview, tparams)
                            elif state not in ("ACTIVE", "RUNNING", "PENDING", "") and prev_state != "DONE":
                                seen_tools[step_idx] = "DONE"
                                self._emit_tool_progress("tool.completed", tname, preview, tparams)
                            continue

                        if stype in ("agent_response", "planner_response"):
                            delta = su.get("text_delta") or ""
                            if not delta:
                                resp_chunk = su.get("response") or ""
                                if resp_chunk:
                                    delta = resp_chunk[len(stream_buf) :] if resp_chunk.startswith(stream_buf) else resp_chunk
                            if delta:
                                stream_buf += delta
                                streamed_any = True
                                self._emit_delta(delta)
                            continue

                    if etype == "result":
                        res_obj = evt.get("result")
                        final_result = res_obj if isinstance(res_obj, dict) else evt
                        cid = final_result.get("conversation_id") or evt.get("conversation_id")
                        if cid:
                            chat_state.set_account_conversation(chat_key, acc_name, cid)
                        continue
            finally:
                with contextlib.suppress(Exception):
                    rc = proc.wait(timeout=5)
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
                        rc = proc.wait(timeout=2)
                stderr_thread.join(timeout=1.5)
                with self._proc_lock:
                    if self._active_proc is proc:
                        self._active_proc = None

            if self.is_interrupted or timed_out:
                break

            stderr_joined = "\n".join(stderr_lines).strip()
            non_json_joined = "\n".join(raw_non_json).strip()
            result_status = str((final_result or {}).get("status") or "")
            result_resp = str((final_result or {}).get("response") or "").strip()
            had_text = bool(result_resp or stream_buf.strip())

            if rc != 0 or result_status.lower() in ("error", "failed") or not had_text:
                err_kind, cd_sec = classify_agy_error(
                    stderr_joined,
                    f"{non_json_joined}\n{result_resp}",
                    rc,
                )
                if err_kind != "none":
                    snippet = (stderr_joined or non_json_joined or result_resp or f"rc={rc}")[:220]
                    accounts.mark_error(acc_name, err_kind, snippet, cd_sec)
                    logger.warning(
                        "Agy account '%s' hit %s error (cooldown %ds): %s",
                        acc_name, err_kind, cd_sec, snippet,
                    )
                    if (attempt_idx + 1) < len(candidates) and not streamed_any:
                        next_acc = candidates[attempt_idx + 1]["name"]
                        reason_lbl = "belum login" if err_kind == "auth" else "limit/kuota"
                        failover_notes.append(f"{acc_name} ({reason_lbl})")
                        self._emit_status(f"🔄 Akun `{acc_name}` {reason_lbl}, beralih ke `{next_acc}`…")
                        continue
                attempt_succeeded = had_text and rc == 0
                break

            attempt_succeeded = True
            break

        if self.is_interrupted:
            return {
                "final_response": stream_buf.strip() or "🛑 Proses dihentikan.",
                "messages": hermes_hist,
                "api_calls": 1,
                "completed": False,
                "failed": False,
                "interrupted": True,
                "interrupt_message": self.interrupt_message,
                "provider": "antigravity",
                "model": self.model,
                "agent_persisted": False,
            }

        if timed_out:
            limit_desc = f"diam >{IDLE_TIMEOUT} detik tanpa aktivitas" if idle_timed_out else f"melewati batas {PRINT_TIMEOUT} detik"
            chat_state.update_chat(chat_key, last_account=used_account, model=self.model)
            partial_note = f"\n\n{stream_buf.strip()}" if stream_buf.strip() else ""
            return {
                "final_response": f"⏱️ Tugas dihentikan karena {limit_desc} pada akun `{used_account}`. Kirim **lanjutkan** untuk meneruskan dari progres terakhir.{partial_note}",
                "messages": hermes_hist,
                "api_calls": 1,
                "completed": False,
                "failed": True,
                "interrupted": False,
                "error": f"Timeout ({limit_desc}) on account {used_account}",
                "provider": "antigravity",
                "model": self.model,
                "agent_persisted": False,
            }

        reply_text = ""
        if final_result is not None:
            reply_text = str(final_result.get("response") or "").strip()
        if not reply_text:
            reply_text = stream_buf.strip()

        if not reply_text:
            err_tail = "\n".join(stderr_lines[-8:]).strip() or "\n".join(raw_non_json[-8:]).strip()
            msg = f"❌ Akun `{used_account}` tidak menghasilkan jawaban (rc={rc})."
            if err_tail:
                msg += f"\n```\n{err_tail[:1200]}\n```"
            return {
                "final_response": msg,
                "messages": hermes_hist,
                "api_calls": 1,
                "completed": False,
                "failed": True,
                "interrupted": False,
                "error": err_tail[:400] or f"exit_code={rc}",
                "provider": "antigravity",
                "model": self.model,
                "agent_persisted": False,
            }

        # Success! Update account stats, last_account, SharedContextStore, and Hermes messages
        if attempt_succeeded or rc == 0:
            accounts.mark_success(used_account)
            chat_state.update_chat(chat_key, last_account=used_account, model=self.model)
            with contextlib.suppress(Exception):
                shared_history.append_turn(
                    chat_key,
                    account=used_account,
                    model=self.model,
                    workdir=workdir,
                    user_text=clean_user_text,
                    assistant_text=reply_text,
                    tools=tools_used,
                )

        if failover_notes:
            reply_text = f"{reply_text}\n\n_🔄 Auto-failover: {' → '.join(failover_notes)} → {used_account}_"

        # Approximate token counters so Hermes /status & footer stay meaningful
        est_in = max(1, len(prompt_text) // 4)
        est_out = max(1, len(reply_text) // 4)
        self.session_prompt_tokens += est_in
        self.session_completion_tokens += est_out
        self.session_output_tokens += est_out
        self.context_compressor.last_prompt_tokens = est_in + sum(
            len(_history_text(m.get("content"))) // 4 for m in hermes_hist[-20:]
        )

        updated_messages = [
            *hermes_hist,
            {"role": "user", "content": clean_user_text},
            {"role": "assistant", "content": reply_text},
        ]
        self._session_messages = updated_messages

        return {
            "final_response": reply_text,
            "messages": updated_messages,
            "api_calls": 1,
            "completed": True,
            "failed": False,
            "interrupted": False,
            "provider": f"antigravity:{used_account}",
            "model": self.model,
            "total_tokens": est_in + est_out,
            "agent_persisted": False,
        }
