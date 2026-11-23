"""Native Hermes Gateway slash commands for Antigravity (`agy`) multi-account control.

Mixed directly into `GatewayRunner` so `/account`, `/rotation`, `/mode`, `/effort`,
and `/cd` work natively alongside all built-in Hermes commands (`/new`, `/model`,
`/status`, `/stop`, `/queue`, `/usage`, `/memory`, `/skills`, `/cron`, `/help`, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

from agent.agy_state import (
    AGY_BIN,
    BOT_BASE_DIR,
    VALID_EFFORTS,
    VALID_MODES,
    VALID_ROTATION_MODES,
    build_agy_env,
    classify_agy_error,
    ensure_account_home,
    get_account_manager,
    get_chat_state,
    get_shared_context,
)
from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.slash_commands_agy")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}d"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m{secs:02d}d"


def _command_arg(event: MessageEvent) -> str:
    text = (event.text or "").strip()
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


class GatewayAgyCommandsMixin:
    """Antigravity (`agy`) multi-account & runtime control slash commands for GatewayRunner."""

    def _agy_chat_id(self, event: MessageEvent) -> str:
        src = event.source
        return str(src.chat_id) if (src and src.chat_id) else "default"

    def _agy_meta(self, event: MessageEvent) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if event.source and event.source.thread_id:
            meta["thread_id"] = str(event.source.thread_id)
        if event.message_id:
            meta["reply_to_message_id"] = str(event.message_id)
        return meta

    def _reset_agy_chat_context(self, chat_id: str) -> None:
        if not chat_id:
            return
        try:
            get_chat_state().reset_chat_session(str(chat_id))
            get_shared_context().clear(str(chat_id))
        except Exception:
            logger.debug("Failed resetting agy chat context for %s", chat_id, exc_info=True)

    def _format_agy_status_block(self, chat_id: str) -> str:
        try:
            accounts = get_account_manager()
            cfg = get_chat_state().get_chat(chat_id)
            rot_mode = cfg.get("rotation_mode") or "rotate"
            pinned = cfg.get("pinned_account") or "default"
            enabled = accounts.enabled_accounts()
            nxt = accounts.peek_next_account(rot_mode, pinned)
            turns = get_shared_context().turn_count(chat_id)
            lines = [
                "",
                "🧩 **Agymes Multi-Akun:**",
                f"• **Rotasi:** `{rot_mode}` (`{len(enabled)}` akun siap · berikut: `{nxt['name'] if nxt else '-'}` · terakhir: `{cfg.get('last_account') or '-'}`)",
                f"• **Folder kerja:** `{cfg.get('workdir')}`",
                f"• **Mode / Effort:** `{cfg.get('mode')}` / `{cfg.get('effort') or 'default'}`",
                f"• **Shared Context:** `{turns}` giliran tersimpan",
            ]
            return "\n".join(lines)
        except Exception:
            return ""

    def _format_accounts_summary(self, chat_id: str) -> str:
        accounts = get_account_manager()
        cfg = get_chat_state().get_chat(chat_id)
        rot_mode = cfg.get("rotation_mode") or "rotate"
        pinned = cfg.get("pinned_account") or "default"
        last_acc = cfg.get("last_account") or "-"
        nxt = accounts.peek_next_account(rot_mode, pinned)
        nxt_name = nxt["name"] if nxt else "-"
        now = int(time.time())

        mode_labels = {
            "rotate": "🔄 Rotasi per Request (Shared Context)",
            "failover": "🛡️ Auto-Failover (pindah saat limit)",
            "manual": "📌 Manual (1 akun tetap)",
        }

        lines = [
            "👥 **Multi-Akun Agymes**",
            f"• **Mode rotasi:** {mode_labels.get(rot_mode, rot_mode)}",
            f"• **Akun berikutnya:** `{nxt_name}` (terakhir: `{last_acc}`)",
            f"• **Konteks bersama:** `{get_shared_context().turn_count(chat_id)}` giliran tersimpan",
            "",
            "**Daftar Akun:**",
        ]
        for acc in accounts.list_accounts():
            name = acc["name"]
            enabled = bool(acc.get("enabled"))
            needs_login = bool(acc.get("needs_login"))
            cd = int(acc.get("cooldown_until") or 0)
            reqs = int(acc.get("request_count") or 0)
            if not enabled:
                badge = "⏸️ nonaktif"
            elif needs_login:
                badge = "⚠️ perlu login"
            elif cd > now:
                badge = f"⏳ cooldown {_fmt_dur(cd - now)}"
            else:
                badge = "🟢 siap"
            pin_tag = " 📌" if (rot_mode != "rotate" and name == pinned) else ""
            lines.append(f"• `{name}`{pin_tag} — {badge} · `{reqs}` req")

        lines.append(
            "\n**Perintah cepat:**\n"
            "• `/rotation` — ubah mode rotasi\n"
            "• `/account add <nama>` — buat profil akun baru\n"
            "• `/account test <nama>` — cek login & aktifkan otomatis\n"
            "• `/account on|off <nama>` — aktif/nonaktifkan akun\n"
            "• `/account use <nama>` — pin akun (mode failover/manual)\n"
            "• `/account reset` — bersihkan semua cooldown"
        )
        return "\n".join(lines)

    async def _test_agy_account_auth(self, acc: dict[str, Any]) -> tuple[bool, str]:
        try:
            ensure_account_home(Path(acc["home"]))
            proc = await asyncio.create_subprocess_exec(
                AGY_BIN,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_agy_env(acc["home"]),
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            out_s = stdout_b.decode("utf-8", errors="replace").strip()
            err_s = stderr_b.decode("utf-8", errors="replace").strip()
            combined = f"{out_s}\n{err_s}".strip()
            err_kind, _ = classify_agy_error(err_s, out_s, proc.returncode or 0)
            if err_kind == "auth":
                return False, "Belum login (Please sign in)"
            model_lines = [ln for ln in out_s.splitlines() if "\t" in ln]
            if model_lines:
                return True, f"{len(model_lines)} model tersedia"
            if proc.returncode == 0:
                return True, "Terautentikasi"
            return False, (combined[:220] or f"Exit code {proc.returncode}")
        except asyncio.TimeoutError:
            return False, "Timeout (30 detik)"
        except Exception as exc:
            return False, str(exc)

    async def _handle_account_command(self, event: MessageEvent) -> Optional[str]:
        chat_id = self._agy_chat_id(event)
        arg = _command_arg(event)
        meta = self._agy_meta(event)
        adapter = self._delivery_adapter_for(event.source) if event.source else None
        accounts = get_account_manager()
        chat_state = get_chat_state()

        if not arg:
            summary = self._format_accounts_summary(chat_id)
            if adapter and hasattr(adapter, "send_choice_picker"):
                cfg = chat_state.get_chat(chat_id)
                cur_rot = cfg.get("rotation_mode") or "rotate"
                choices = [
                    {"value": "mode:rotate", "label": "🔄 Rotasi per Req", "is_current": cur_rot == "rotate"},
                    {"value": "mode:failover", "label": "🛡️ Auto-Failover", "is_current": cur_rot == "failover"},
                    {"value": "mode:manual", "label": "📌 Manual 1 Akun", "is_current": cur_rot == "manual"},
                    {"value": "action:reset", "label": "♻️ Reset Cooldown", "is_current": False},
                ]
                for acc in accounts.list_accounts()[:8]:
                    st_icon = "🟢" if acc.get("enabled") else "⏸️"
                    choices.append({"value": f"toggle:{acc['name']}", "label": f"{st_icon} Toggle {acc['name']}", "is_current": False})

                async def on_acc_action(cid: str, val: str) -> str:
                    if val.startswith("mode:"):
                        m = val.split(":", 1)[1]
                        chat_state.update_chat(cid, rotation_mode=m)
                        return f"✅ Mode rotasi diubah ke `{m}`.\n\n{self._format_accounts_summary(cid)}"
                    if val == "action:reset":
                        n = accounts.clear_cooldowns()
                        return f"♻️ Cooldown direset untuk `{n}` akun.\n\n{self._format_accounts_summary(cid)}"
                    if val.startswith("toggle:"):
                        aname = val.split(":", 1)[1]
                        acc_obj = accounts.get_account(aname)
                        if not acc_obj:
                            return f"❌ Akun `{aname}` tidak ditemukan."
                        new_en = not bool(acc_obj.get("enabled"))
                        accounts.set_enabled(aname, new_en)
                        return f"✅ Akun `{aname}` sekarang `{'AKTIF' if new_en else 'NONAKTIF'}`.\n\n{self._format_accounts_summary(cid)}"
                    return self._format_accounts_summary(cid)

                await adapter.send_choice_picker(
                    chat_id=chat_id,
                    title=summary,
                    choices=choices,
                    session_key=self._session_key_for_source(event.source),
                    on_choice_selected=on_acc_action,
                    metadata=meta,
                )
                return None
            return summary

        subparts = arg.split(maxsplit=2)
        sub = subparts[0].lower()
        target_name = subparts[1].strip().lower() if len(subparts) > 1 else ""
        extra_arg = subparts[2].strip() if len(subparts) > 2 else ""

        if sub in VALID_ROTATION_MODES:
            chat_state.update_chat(chat_id, rotation_mode=sub)
            return f"🔄 Mode rotasi diubah ke: `{sub}`"

        if sub == "reset":
            n = accounts.clear_cooldowns()
            return f"♻️ Cooldown & status error dibersihkan untuk `{n}` akun."

        if sub == "add":
            if not target_name:
                return "Gunakan: `/account add <nama>` (contoh: `/account add akun4`)"
            try:
                entry = accounts.add_account(target_name, label=extra_arg or target_name, enabled=False)
            except ValueError as exc:
                return f"❌ {exc}"
            return (
                f"✅ Profil akun `{entry['name']}` siap di `{entry['home']}`.\n\n"
                f"Jalankan di terminal server untuk login Google OAuth:\n"
                f"```bash\nagymes account login {entry['name']}\n```"
            )

        if sub in ("on", "enable", "off", "disable"):
            if not target_name:
                return f"Gunakan: `/account {sub} <nama>`"
            try:
                entry = accounts.set_enabled(target_name, sub in ("on", "enable"))
            except KeyError as exc:
                return f"❌ {exc}"
            return f"✅ Akun `{entry['name']}` -> `{'AKTIF' if entry['enabled'] else 'NONAKTIF'}`."

        if sub in ("use", "pin"):
            if not target_name:
                return "Gunakan: `/account use <nama>`"
            if not accounts.get_account(target_name):
                return f"❌ Akun `{target_name}` tidak ditemukan."
            cfg = chat_state.update_chat(chat_id, pinned_account=target_name)
            note = " (catatan: mode saat ini `rotate`, ubah ke `/rotation failover` atau `/rotation manual` jika ingin dipin)." if cfg.get("rotation_mode") == "rotate" else ""
            return f"📌 Akun utama chat di-pin ke `{target_name}`{note}"

        if sub == "test":
            if not target_name:
                return "Gunakan: `/account test <nama>`"
            acc_obj = accounts.get_account(target_name)
            if not acc_obj:
                return f"❌ Akun `{target_name}` tidak ditemukan."
            ok, detail = await self._test_agy_account_auth(acc_obj)
            if ok:
                accounts.mark_authenticated(target_name, enable=True)
                return f"✅ Akun `{target_name}` terautentikasi & **AKTIF** di rotasi! ({detail})"
            accounts.mark_error(target_name, "auth", detail, 1800)
            return f"❌ Akun `{target_name}` gagal verifikasi: `{detail}`\nJalankan: `agymes account login {target_name}`"

        if sub in ("del", "remove"):
            if not target_name:
                return "Gunakan: `/account del <nama>`"
            try:
                ok = accounts.remove_account(target_name, purge=False)
            except ValueError as exc:
                return f"❌ {exc}"
            return f"🗑️ Akun `{target_name}` dihapus dari daftar rotasi." if ok else f"❌ Akun `{target_name}` tidak ditemukan."

        return (
            "Subperintah `/account` tidak dikenal.\n"
            "Gunakan: `/account`, `/account add <nama>`, `/account test <nama>`, "
            "`/account on|off <nama>`, `/account use <nama>`, `/account reset`, `/account del <nama>`."
        )

    async def _handle_rotation_command(self, event: MessageEvent) -> Optional[str]:
        chat_id = self._agy_chat_id(event)
        arg = _command_arg(event).lower()
        meta = self._agy_meta(event)
        adapter = self._delivery_adapter_for(event.source) if event.source else None
        chat_state = get_chat_state()

        if arg:
            if arg not in VALID_ROTATION_MODES:
                return f"Pilihan mode rotasi: `{', '.join(VALID_ROTATION_MODES)}` (atau kirim `/rotation` untuk tombol)."
            chat_state.update_chat(chat_id, rotation_mode=arg)
            return f"🔄 Mode rotasi akun diubah ke: `{arg}`"

        cur = chat_state.get_chat(chat_id).get("rotation_mode") or "rotate"
        if adapter and hasattr(adapter, "send_choice_picker"):
            choices = [
                {"value": "rotate", "label": "🔄 Rotasi per Request", "is_current": cur == "rotate"},
                {"value": "failover", "label": "🛡️ Auto-Failover saja", "is_current": cur == "failover"},
                {"value": "manual", "label": "📌 Manual (1 Akun)", "is_current": cur == "manual"},
            ]

            async def on_selected(cid: str, val: str) -> str:
                chat_state.update_chat(cid, rotation_mode=val)
                return f"✅ Mode rotasi akun diubah ke `{val}`"

            await adapter.send_choice_picker(
                chat_id=chat_id,
                title=f"🔄 **Pilih Mode Rotasi Akun**\nSaat ini: `{cur}` (semua mode menjaga konteks chat yang sama)",
                choices=choices,
                session_key=self._session_key_for_source(event.source),
                on_choice_selected=on_selected,
                metadata=meta,
            )
            return None
        return f"🔄 Mode rotasi saat ini: `{cur}` (`rotate` / `failover` / `manual`)"

    async def _handle_mode_command(self, event: MessageEvent) -> Optional[str]:
        chat_id = self._agy_chat_id(event)
        arg = _command_arg(event).lower()
        meta = self._agy_meta(event)
        adapter = self._delivery_adapter_for(event.source) if event.source else None
        chat_state = get_chat_state()

        if arg:
            if arg not in VALID_MODES:
                return f"Pilihan mode: `{', '.join(VALID_MODES)}` (atau kirim `/mode` untuk tombol)."
            chat_state.update_chat(chat_id, mode=arg)
            return f"⚙️ Mode eksekusi Agymes diubah ke: `{arg}`"

        cur = chat_state.get_chat(chat_id).get("mode") or "auto"
        if adapter and hasattr(adapter, "send_choice_picker"):
            choices = [
                {"value": "auto", "label": "Auto (penuh)", "is_current": cur == "auto"},
                {"value": "accept-edits", "label": "Accept Edits", "is_current": cur == "accept-edits"},
                {"value": "plan", "label": "Plan (baca saja)", "is_current": cur == "plan"},
            ]

            async def on_selected(cid: str, val: str) -> str:
                chat_state.update_chat(cid, mode=val)
                return f"✅ Mode eksekusi Agymes diubah ke `{val}`"

            await adapter.send_choice_picker(
                chat_id=chat_id,
                title=f"⚙️ **Pilih Mode Eksekusi Agymes**\nSaat ini: `{cur}`",
                choices=choices,
                session_key=self._session_key_for_source(event.source),
                on_choice_selected=on_selected,
                metadata=meta,
            )
            return None
        return f"⚙️ Mode saat ini: `{cur}` (`auto` / `accept-edits` / `plan`)"

    async def _handle_effort_command(self, event: MessageEvent) -> Optional[str]:
        chat_id = self._agy_chat_id(event)
        arg = _command_arg(event).lower()
        meta = self._agy_meta(event)
        adapter = self._delivery_adapter_for(event.source) if event.source else None
        chat_state = get_chat_state()

        if arg:
            val = "" if arg == "default" else arg
            if val not in VALID_EFFORTS:
                return "Pilihan effort: `default`, `low`, `medium`, `high`, `xhigh`, `max`."
            chat_state.update_chat(chat_id, effort=val)
            return f"🧠 Reasoning effort Agymes diubah ke: `{val or 'default'}`"

        cur = chat_state.get_chat(chat_id).get("effort") or ""
        if adapter and hasattr(adapter, "send_choice_picker"):
            choices = [
                {"value": "default", "label": "Default", "is_current": cur == ""},
                {"value": "low", "label": "Low", "is_current": cur == "low"},
                {"value": "medium", "label": "Medium", "is_current": cur == "medium"},
                {"value": "high", "label": "High", "is_current": cur == "high"},
                {"value": "xhigh", "label": "XHigh", "is_current": cur == "xhigh"},
                {"value": "max", "label": "Max", "is_current": cur == "max"},
            ]

            async def on_selected(cid: str, val: str) -> str:
                chosen = "" if val == "default" else val
                chat_state.update_chat(cid, effort=chosen)
                return f"✅ Reasoning effort Agymes diubah ke `{chosen or 'default'}`"

            await adapter.send_choice_picker(
                chat_id=chat_id,
                title=f"🧠 **Pilih Reasoning Effort Agymes**\nSaat ini: `{cur or 'default'}`",
                choices=choices,
                session_key=self._session_key_for_source(event.source),
                on_choice_selected=on_selected,
                metadata=meta,
            )
            return None
        return f"🧠 Reasoning effort saat ini: `{cur or 'default'}`"

    async def _handle_cd_command(self, event: MessageEvent) -> str:
        chat_id = self._agy_chat_id(event)
        arg = _command_arg(event)
        chat_state = get_chat_state()
        if not arg:
            cur = chat_state.get_chat(chat_id).get("workdir") or "/root"
            return f"📁 Folder kerja Agymes saat ini: `{cur}`\nGunakan: `/cd /path/folder`"
        target = Path(arg).expanduser().resolve()
        if not target.is_dir():
            return f"❌ Folder tidak ditemukan: `{target}`"
        chat_state.update_chat(chat_id, workdir=str(target))
        return f"📁 Folder kerja Agymes diubah ke: `{target}`"
