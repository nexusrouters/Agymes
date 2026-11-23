#!/usr/bin/env python3
"""
Multi-account profile isolation, round-robin / failover rotation, and shared
cross-account conversation context for Agymes (`agy`).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

VALID_ROTATION_MODES = ("rotate", "failover", "manual")
ACCOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

REAL_USER_HOME = Path(os.environ.get("AGYMES_USER_HOME") or Path.home()).expanduser().resolve()
SHARED_SYMLINKS: tuple[tuple[str, ...], ...] = (
    (".gemini", "config"),
    (".ssh",),
    (".hermes",),
    (".agymes",),
    (".local",),
    (".bun",),
    (".npm",),
    (".npmrc",),
    (".gitconfig",),
    (".config", "uv"),
)

_AUTH_ERROR_PATTERNS = (
    "please sign in",
    "not signed in",
    "sign in to",
    "unauthenticated",
    "authentication required",
    "login required",
    "invalid_grant",
    "token expired",
    "token has been expired",
    "credentials not found",
    "oauth",
)

_QUOTA_ERROR_PATTERNS = (
    "quota",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "resource_exhausted",
    "resource exhausted",
    "exhausted",
    "capacity",
    "overloaded",
    "limit exceeded",
    "usage limit",
    "try again later",
    "http 429",
    "status 429",
    "code 429",
    "error 429",
)


def sanitize_account_name(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not ACCOUNT_NAME_RE.match(name):
        raise ValueError(
            "Nama akun harus 1-32 karakter huruf kecil/angka/`-`/`_` (diawali huruf/angka)."
        )
    return name


def ensure_account_home(account_home: Path) -> Path:
    """Create isolated HOME for an agy account while symlinking shared host resources."""
    resolved = account_home.expanduser().resolve()
    if resolved == REAL_USER_HOME:
        return resolved

    resolved.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(resolved, 0o700)

    (resolved / ".gemini").mkdir(parents=True, exist_ok=True)
    (resolved / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    (resolved / ".config").mkdir(parents=True, exist_ok=True)

    for rel_parts in SHARED_SYMLINKS:
        src = REAL_USER_HOME.joinpath(*rel_parts)
        dst = resolved.joinpath(*rel_parts)
        if not src.exists():
            continue
        # Never symlink a directory into itself (e.g. ~/.agymes inside ~/.agymes/accounts/<name>)
        with contextlib.suppress(OSError):
            if resolved == src.resolve() or src.resolve() in resolved.parents:
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            if dst.resolve(strict=False) == src.resolve(strict=False):
                continue
            with contextlib.suppress(OSError):
                dst.unlink()
        elif dst.exists():
            continue
        with contextlib.suppress(OSError):
            dst.symlink_to(src)

    return resolved


def build_agy_env(account_home: str, base_env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Build subprocess env for agy pinned to a specific account HOME."""
    env = dict(os.environ if base_env is None else base_env)
    home_path = str(Path(account_home).expanduser().resolve())
    env["HOME"] = home_path
    if not env.get("HERMES_HOME"):
        env["HERMES_HOME"] = str(REAL_USER_HOME / ".agymes")
    for key in (
        "ANTIGRAVITY_APP_DATA_DIR",
        "ANTIGRAVITY_SOURCE_METADATA",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        env.pop(key, None)

    local_bin = str(REAL_USER_HOME / ".local" / "bin")
    path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    if local_bin not in path_parts:
        env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}".strip(os.pathsep)
    return env


def classify_agy_error(stderr_text: str, output_text: str, rc: int) -> tuple[str, int]:
    """Classify whether an agy failure is an account-level rotatable error ('auth'|'quota'|'none')."""
    combined = f"{stderr_text}\n{output_text}".lower()
    if not combined.strip() and rc == 0:
        return "none", 0

    for pat in _AUTH_ERROR_PATTERNS:
        if pat in combined:
            return "auth", 1800

    for pat in _QUOTA_ERROR_PATTERNS:
        if pat in combined:
            return "quota", 600

    return "none", 0


class AccountManager:
    """Thread/process-safe, hot-reloading registry of Agymes accounts and rotation cursor."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.accounts_dir = base_dir / "accounts"
        self.file_path = base_dir / "accounts.json"
        self.lock_path = base_dir / ".accounts.lock"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {"rotation_index": 0, "accounts": {}}
        self._mtime: float = -1.0
        self._ensure_initialized()

    @contextlib.contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _default_account_entry() -> dict[str, Any]:
        return {
            "name": "default",
            "home": str(REAL_USER_HOME),
            "label": "Agymes Utama",
            "enabled": True,
            "needs_login": False,
            "added_at": int(time.time()),
            "last_used_at": 0,
            "last_error": "",
            "cooldown_until": 0,
            "request_count": 0,
        }

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            accounts = {}
        if "default" not in accounts:
            accounts = {"default": self._default_account_entry(), **accounts}
        else:
            merged_def = self._default_account_entry()
            merged_def.update(accounts["default"])
            merged_def["name"] = "default"
            merged_def["home"] = str(REAL_USER_HOME)
            accounts["default"] = merged_def

        clean_accounts: dict[str, dict[str, Any]] = {}
        for name, raw in accounts.items():
            if not isinstance(raw, dict):
                continue
            entry = {
                "name": name,
                "home": str(raw.get("home") or (REAL_USER_HOME if name == "default" else self.accounts_dir / name)),
                "label": str(raw.get("label") or name),
                "enabled": bool(raw.get("enabled", True)),
                "needs_login": bool(raw.get("needs_login", False)),
                "added_at": int(raw.get("added_at") or int(time.time())),
                "last_used_at": int(raw.get("last_used_at") or 0),
                "last_error": str(raw.get("last_error") or ""),
                "cooldown_until": int(raw.get("cooldown_until") or 0),
                "request_count": int(raw.get("request_count") or 0),
            }
            clean_accounts[name] = entry

        rot_idx = max(0, int(data.get("rotation_index") or 0))
        return {"rotation_index": rot_idx, "accounts": clean_accounts}

    def _read_unlocked(self) -> dict[str, Any]:
        if self.file_path.exists():
            try:
                loaded = json.loads(self.file_path.read_text(encoding="utf-8"))
                return self._normalize(loaded)
            except Exception:
                pass
        return self._normalize({})

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.file_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.file_path)
        self._data = data
        with contextlib.suppress(OSError):
            self._mtime = self.file_path.stat().st_mtime

    def _ensure_initialized(self) -> None:
        with self._file_lock():
            data = self._read_unlocked()
            if not self.file_path.exists():
                self._write_unlocked(data)
            else:
                self._data = data
                with contextlib.suppress(OSError):
                    self._mtime = self.file_path.stat().st_mtime

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.file_path.stat().st_mtime
        except OSError:
            return
        if mtime != self._mtime:
            with self._file_lock():
                self._data = self._read_unlocked()
                with contextlib.suppress(OSError):
                    self._mtime = self.file_path.stat().st_mtime

    def list_accounts(self) -> list[dict[str, Any]]:
        self._reload_if_changed()
        return [dict(v) for v in self._data["accounts"].values()]

    def get_account(self, name: str) -> Optional[dict[str, Any]]:
        self._reload_if_changed()
        acc = self._data["accounts"].get(name)
        return dict(acc) if acc else None

    def enabled_accounts(self) -> list[dict[str, Any]]:
        return [a for a in self.list_accounts() if a.get("enabled")]

    def next_auto_account_name(self) -> str:
        existing = {a["name"] for a in self.list_accounts()}
        idx = 2
        while f"akun{idx}" in existing:
            idx += 1
        return f"akun{idx}"

    def add_account(self, raw_name: str, *, label: str = "", enabled: bool = False) -> dict[str, Any]:
        name = sanitize_account_name(raw_name)
        if name == "default":
            return self.get_account("default") or self._default_account_entry()

        home = ensure_account_home(self.accounts_dir / name)
        with self._file_lock():
            data = self._read_unlocked()
            existing = data["accounts"].get(name)
            if existing:
                if label:
                    existing["label"] = label
                existing["home"] = str(home)
                if enabled:
                    existing["enabled"] = True
                    existing["needs_login"] = False
                entry = existing
            else:
                entry = {
                    "name": name,
                    "home": str(home),
                    "label": label or f"Agymes ({name})",
                    "enabled": bool(enabled),
                    "needs_login": not bool(enabled),
                    "added_at": int(time.time()),
                    "last_used_at": 0,
                    "last_error": "",
                    "cooldown_until": 0,
                    "request_count": 0,
                }
                data["accounts"][name] = entry
            self._write_unlocked(data)
            return dict(entry)

    def remove_account(self, name: str, *, purge: bool = False) -> bool:
        if name == "default":
            raise ValueError("Akun 'default' tidak dapat dihapus (bisa dinonaktifkan jika perlu).")
        with self._file_lock():
            data = self._read_unlocked()
            entry = data["accounts"].pop(name, None)
            if entry is None:
                return False
            self._write_unlocked(data)

        if purge:
            home = Path(entry.get("home") or "")
            with contextlib.suppress(OSError):
                if home.is_dir() and home.resolve().parent == self.accounts_dir.resolve():
                    shutil.rmtree(home, ignore_errors=True)
        return True

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        with self._file_lock():
            data = self._read_unlocked()
            entry = data["accounts"].get(name)
            if not entry:
                raise KeyError(f"Akun tidak ditemukan: {name}")
            entry["enabled"] = bool(enabled)
            if enabled:
                entry["cooldown_until"] = 0
                entry["last_error"] = ""
            self._write_unlocked(data)
            return dict(entry)

    def mark_authenticated(self, name: str, *, enable: bool = True) -> dict[str, Any]:
        with self._file_lock():
            data = self._read_unlocked()
            entry = data["accounts"].get(name)
            if not entry:
                raise KeyError(f"Akun tidak ditemukan: {name}")
            entry["needs_login"] = False
            entry["cooldown_until"] = 0
            entry["last_error"] = ""
            if enable:
                entry["enabled"] = True
            self._write_unlocked(data)
            return dict(entry)

    def mark_success(self, name: str) -> None:
        with self._file_lock():
            data = self._read_unlocked()
            entry = data["accounts"].get(name)
            if not entry:
                return
            entry["last_used_at"] = int(time.time())
            entry["request_count"] = int(entry.get("request_count") or 0) + 1
            entry["cooldown_until"] = 0
            entry["needs_login"] = False
            entry["last_error"] = ""
            self._write_unlocked(data)

    def mark_error(self, name: str, err_kind: str, err_msg: str, cooldown_sec: int) -> None:
        with self._file_lock():
            data = self._read_unlocked()
            entry = data["accounts"].get(name)
            if not entry:
                return
            now = int(time.time())
            entry["last_error"] = (err_msg or err_kind)[:240]
            if cooldown_sec > 0:
                entry["cooldown_until"] = now + int(cooldown_sec)
            if err_kind == "auth":
                entry["needs_login"] = True
            self._write_unlocked(data)

    def clear_cooldowns(self) -> int:
        cleared = 0
        with self._file_lock():
            data = self._read_unlocked()
            for entry in data["accounts"].values():
                if entry.get("cooldown_until") or entry.get("last_error") or entry.get("needs_login"):
                    entry["cooldown_until"] = 0
                    entry["last_error"] = ""
                    if entry.get("enabled"):
                        entry["needs_login"] = False
                    cleared += 1
            self._write_unlocked(data)
        return cleared

    def _ordered_enabled(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        now = int(time.time())
        enabled = [dict(a) for a in data["accounts"].values() if a.get("enabled")]
        if not enabled:
            default_acc = data["accounts"].get("default")
            return [dict(default_acc)] if default_acc else []
        ready = [a for a in enabled if int(a.get("cooldown_until") or 0) <= now]
        if ready:
            return ready
        return sorted(enabled, key=lambda a: int(a.get("cooldown_until") or 0))

    def plan_candidates(self, rotation_mode: str, pinned_account: str = "default") -> list[dict[str, Any]]:
        mode = rotation_mode if rotation_mode in VALID_ROTATION_MODES else "rotate"
        with self._file_lock():
            data = self._read_unlocked()
            all_accounts = data["accounts"]

            if mode == "manual":
                acc = all_accounts.get(pinned_account) or all_accounts.get("default")
                return [dict(acc)] if acc else []

            pool = self._ordered_enabled(data)
            if not pool:
                return []

            if mode == "failover":
                pinned = next((a for a in pool if a["name"] == pinned_account), None)
                if pinned:
                    rest = [a for a in pool if a["name"] != pinned_account]
                    return [pinned, *rest]
                return pool

            idx = int(data.get("rotation_index") or 0)
            start = idx % len(pool)
            ordered = pool[start:] + pool[:start]
            data["rotation_index"] = idx + 1
            self._write_unlocked(data)
            return ordered

    def peek_next_account(self, rotation_mode: str, pinned_account: str = "default") -> Optional[dict[str, Any]]:
        self._reload_if_changed()
        mode = rotation_mode if rotation_mode in VALID_ROTATION_MODES else "rotate"
        if mode == "manual":
            acc = self._data["accounts"].get(pinned_account) or self._data["accounts"].get("default")
            return dict(acc) if acc else None

        pool = self._ordered_enabled(self._data)
        if not pool:
            return None
        if mode == "failover":
            pinned = next((a for a in pool if a["name"] == pinned_account), None)
            return pinned or pool[0]
        idx = int(self._data.get("rotation_index") or 0)
        return pool[idx % len(pool)]


class SharedContextStore:
    """Stores per-chat turns outside any single agy profile for seamless account rotation."""

    MAX_STORED_TURNS = 30
    MAX_SUMMARY_NOTES = 20
    MAX_STORED_USER_CHARS = 4000
    MAX_STORED_ASSISTANT_CHARS = 6000

    def __init__(self, base_dir: Path) -> None:
        self.history_dir = base_dir / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.history_dir, 0o700)

    def _chat_file(self, chat_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", str(chat_id))
        return self.history_dir / f"chat_{safe}.json"

    def _load(self, chat_id: str) -> dict[str, Any]:
        path = self._chat_file(chat_id)
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    loaded.setdefault("turns", [])
                    loaded.setdefault("summary_notes", [])
                    return loaded
            except Exception:
                pass
        return {"chat_id": str(chat_id), "updated_at": 0, "summary_notes": [], "turns": []}

    def _save(self, chat_id: str, data: dict[str, Any]) -> None:
        path = self._chat_file(chat_id)
        data["updated_at"] = int(time.time())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(path)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        s = (text or "").strip()
        if len(s) <= limit:
            return s
        head = int(limit * 0.65)
        tail = limit - head - 24
        return f"{s[:head]}\n…[dipotong]…\n{s[-tail:]}"

    @staticmethod
    def _digest_turn(turn: dict[str, Any]) -> str:
        u = " ".join(str(turn.get("user") or "").split())[:110]
        a = " ".join(str(turn.get("assistant") or "").split())[:130]
        tools = turn.get("tools") or []
        tool_part = f" | tools: {', '.join(tools[:3])}" if tools else ""
        return f"User: {u} -> Asisten: {a}{tool_part}"

    def append_turn(
        self,
        chat_id: str,
        *,
        account: str,
        model: str,
        workdir: str,
        user_text: str,
        assistant_text: str,
        tools: Optional[list[str]] = None,
    ) -> int:
        data = self._load(chat_id)
        turns: list[dict[str, Any]] = data.get("turns", [])
        notes: list[str] = data.get("summary_notes", [])

        clean_tools = [str(t).strip()[:100] for t in (tools or []) if str(t).strip()][:12]
        turns.append(
            {
                "ts": int(time.time()),
                "account": account,
                "model": model or "default",
                "workdir": workdir,
                "user": self._clip(user_text, self.MAX_STORED_USER_CHARS),
                "tools": clean_tools,
                "assistant": self._clip(assistant_text, self.MAX_STORED_ASSISTANT_CHARS),
            }
        )

        while len(turns) > self.MAX_STORED_TURNS:
            evicted = turns.pop(0)
            notes.append(self._digest_turn(evicted))
        if len(notes) > self.MAX_SUMMARY_NOTES:
            notes = notes[-self.MAX_SUMMARY_NOTES :]

        data["turns"] = turns
        data["summary_notes"] = notes
        self._save(chat_id, data)
        return len(turns)

    def clear(self, chat_id: str) -> None:
        with contextlib.suppress(OSError):
            self._chat_file(chat_id).unlink()

    def turn_count(self, chat_id: str) -> int:
        return len(self._load(chat_id).get("turns", []))

    def build_contextual_prompt(
        self,
        chat_id: str,
        current_prompt: str,
        *,
        max_context_chars: int = 22000,
    ) -> str:
        data = self._load(chat_id)
        turns: list[dict[str, Any]] = data.get("turns", [])
        notes: list[str] = data.get("summary_notes", [])
        if not turns and not notes:
            return current_prompt

        header = (
            "[KONTEKS PERCAKAPAN SEBELUMNYA DI CHAT TELEGRAM INI]\n"
            "Kamu adalah Agymes, asisten yang sama yang melanjutkan percakapan ini secara langsung dan konsisten. "
            "Jawab pesan terbaru user berdasarkan konteks di bawah ini. "
            "Jangan menyebut blok konteks ini atau perpindahan akun kecuali user bertanya langsung.\n"
        )
        footer = f"\n[PESAN TERBARU USER]\n{current_prompt}"
        budget = max(2000, max_context_chars - len(header) - len(footer))

        rendered_turns: list[str] = []
        used_chars = 0
        older_overflow: list[str] = []

        for idx in range(len(turns) - 1, -1, -1):
            t = turns[idx]
            is_very_recent = idx >= len(turns) - 4
            u_lim = 2400 if is_very_recent else 900
            a_lim = 3600 if is_very_recent else 1400
            u_txt = self._clip(str(t.get("user") or ""), u_lim)
            a_txt = self._clip(str(t.get("assistant") or ""), a_lim)
            tools = t.get("tools") or []
            wd = t.get("workdir") or ""

            lines = [f"--- Giliran {idx + 1}" + (f" (workdir: {wd})" if wd else "") + " ---"]
            lines.append(f"User: {u_txt}")
            if tools:
                lines.append(f"Aksi tool: {'; '.join(tools[:8])}")
            lines.append(f"Asisten: {a_txt}")
            block = "\n".join(lines) + "\n"

            if used_chars + len(block) <= budget or not rendered_turns:
                rendered_turns.append(block)
                used_chars += len(block)
            else:
                older_overflow.append(self._digest_turn(t))

        rendered_turns.reverse()
        older_overflow.reverse()

        all_notes = [*notes, *older_overflow]
        parts = [header]
        if all_notes:
            remaining = max(0, budget - used_chars)
            if remaining > 200:
                note_lines: list[str] = ["[Ringkasan topik lebih awal]"]
                n_chars = len(note_lines[0]) + 1
                for note in all_notes[-12:]:
                    line = f"- {note}"
                    if n_chars + len(line) + 1 > remaining:
                        break
                    note_lines.append(line)
                    n_chars += len(line) + 1
                if len(note_lines) > 1:
                    parts.append("\n".join(note_lines) + "\n")

        parts.append("[Riwayat percakapan terbaru]\n" + "\n".join(rendered_turns))
        parts.append(footer)
        return "\n".join(parts)
