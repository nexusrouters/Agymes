"""Portable Agymes multi-account & per-chat state helpers."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _resolve_agymes_data_dir() -> Path:
    for env_key in ("AGYMES_HOME", "AGY_BOT_DIR"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return Path(val).expanduser().resolve()
    return (Path.home() / ".agymes").resolve()


def _resolve_agy_bin() -> str:
    env_bin = os.environ.get("AGY_BIN", "").strip()
    if env_bin:
        return str(Path(env_bin).expanduser())
    found = shutil.which("agy")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "agy")


BOT_BASE_DIR = _resolve_agymes_data_dir()
if str(BOT_BASE_DIR) not in sys.path:
    sys.path.append(str(BOT_BASE_DIR))

from accounts import (  # noqa: E402
    AccountManager,
    SharedContextStore,
    VALID_ROTATION_MODES,
    build_agy_env,
    classify_agy_error,
    ensure_account_home,
    sanitize_account_name,
)

VALID_MODES = ("auto", "accept-edits", "plan")
VALID_EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
DEFAULT_WORKDIR = os.environ.get("AGY_WORKDIR", "").strip() or str(Path.home())
DEFAULT_MODE = os.environ.get("AGY_MODE", "auto").strip() or "auto"
AGY_BIN = _resolve_agy_bin()
PRINT_TIMEOUT = int(os.environ.get("AGY_TIMEOUT_SECONDS", "0"))
IDLE_TIMEOUT = int(os.environ.get("AGY_IDLE_TIMEOUT_SECONDS", "900"))

_SINGLETON_LOCK = threading.Lock()
_ACCOUNT_MANAGER: AccountManager | None = None
_SHARED_CONTEXT: SharedContextStore | None = None
_CHAT_STATE_STORE: "AgyChatStateStore | None" = None


class AgyChatStateStore:
    """Thread/process-safe wrapper around `<AGYMES_HOME>/state.json`."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or _resolve_agymes_data_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "state.json"
        self.lock_path = self.base_dir / ".state.lock"
        self._thread_lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _default_chat() -> dict[str, Any]:
        return {
            "conversation_id": None,
            "conversations_by_account": {},
            "last_account": "",
            "rotation_mode": "rotate",
            "pinned_account": "default",
            "workdir": DEFAULT_WORKDIR,
            "mode": DEFAULT_MODE if DEFAULT_MODE in VALID_MODES else "auto",
            "model": os.environ.get("AGY_MODEL", "").strip(),
            "effort": "",
            "progress_mode": "compact",
            "footer": False,
        }

    def _read_unlocked(self) -> dict[str, Any]:
        data: dict[str, Any] = {"allowed_user_ids": [], "pairing_code": "", "chats": {}}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                pass
        if not isinstance(data.get("chats"), dict):
            data["chats"] = {}
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        key = str(chat_id or "default")
        with self._locked():
            data = self._read_unlocked()
            raw = data["chats"].get(key) or {}
            merged = self._default_chat()
            merged.update(raw)
            if merged.get("rotation_mode") not in VALID_ROTATION_MODES:
                merged["rotation_mode"] = "rotate"
            if merged.get("mode") not in VALID_MODES:
                merged["mode"] = "auto"
            convs = merged.get("conversations_by_account")
            if not isinstance(convs, dict):
                convs = {}
            else:
                convs = dict(convs)
            if not convs and merged.get("conversation_id"):
                convs["default"] = merged["conversation_id"]
            merged["conversations_by_account"] = convs
            return merged

    def update_chat(self, chat_id: str, **updates: Any) -> dict[str, Any]:
        key = str(chat_id or "default")
        with self._locked():
            data = self._read_unlocked()
            chat = data["chats"].setdefault(key, self._default_chat())
            chat.update(updates)
            self._write_unlocked(data)
        return self.get_chat(key)

    def set_account_conversation(self, chat_id: str, account_name: str, conv_id: str) -> None:
        key = str(chat_id or "default")
        with self._locked():
            data = self._read_unlocked()
            chat = data["chats"].setdefault(key, self._default_chat())
            convs = chat.get("conversations_by_account")
            if not isinstance(convs, dict):
                convs = {}
            convs[account_name] = conv_id
            chat["conversations_by_account"] = convs
            chat["conversation_id"] = conv_id
            self._write_unlocked(data)

    def reset_chat_session(self, chat_id: str) -> None:
        self.update_chat(
            chat_id,
            conversation_id=None,
            conversations_by_account={},
            last_account="",
        )


def get_account_manager() -> AccountManager:
    global _ACCOUNT_MANAGER
    with _SINGLETON_LOCK:
        if _ACCOUNT_MANAGER is None:
            _ACCOUNT_MANAGER = AccountManager(_resolve_agymes_data_dir())
        return _ACCOUNT_MANAGER


def get_shared_context() -> SharedContextStore:
    global _SHARED_CONTEXT
    with _SINGLETON_LOCK:
        if _SHARED_CONTEXT is None:
            _SHARED_CONTEXT = SharedContextStore(_resolve_agymes_data_dir())
        return _SHARED_CONTEXT


def get_chat_state() -> AgyChatStateStore:
    global _CHAT_STATE_STORE
    with _SINGLETON_LOCK:
        if _CHAT_STATE_STORE is None:
            _CHAT_STATE_STORE = AgyChatStateStore(_resolve_agymes_data_dir())
        return _CHAT_STATE_STORE


__all__ = [
    "AGY_BIN",
    "BOT_BASE_DIR",
    "DEFAULT_MODE",
    "DEFAULT_WORKDIR",
    "IDLE_TIMEOUT",
    "PRINT_TIMEOUT",
    "VALID_EFFORTS",
    "VALID_MODES",
    "VALID_ROTATION_MODES",
    "AccountManager",
    "AgyChatStateStore",
    "SharedContextStore",
    "build_agy_env",
    "classify_agy_error",
    "ensure_account_home",
    "get_account_manager",
    "get_chat_state",
    "get_shared_context",
    "sanitize_account_name",
]
