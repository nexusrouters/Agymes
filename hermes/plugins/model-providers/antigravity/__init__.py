"""Antigravity CLI (agy) model provider plugin for customized Hermes Agent."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

AGY_DEFAULT_BIN = os.environ.get("AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CACHE_TTL = 600.0
_models_cache: list[tuple[str, str]] = []
_models_cache_ts: float = 0.0

FALLBACK_AGY_MODELS: tuple[str, ...] = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)


def _bot_base_dir() -> Path:
    return Path(os.environ.get("AGYMES_HOME") or os.environ.get("AGY_BOT_DIR") or (Path.home() / ".agymes")).expanduser().resolve()


def _parse_models_output(raw: str) -> list[tuple[str, str]]:
    models: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_line in (raw or "").splitlines():
        line = _ANSI_RE.sub("", raw_line.split("\r")[-1]).strip()
        if not line or "Fetching available models" in line:
            continue
        parts = line.split("\t", 1) if "\t" in line else re.split(r"\s{2,}", line, maxsplit=1)
        if len(parts) != 2:
            continue
        slug, label = parts[0].strip(), parts[1].strip()
        if not slug or " " in slug or slug in seen:
            continue
        seen.add(slug)
        models.append((slug, label))
    return models


def _probe_agy_models(force_refresh: bool = False) -> list[tuple[str, str]]:
    global _models_cache, _models_cache_ts
    now = time.monotonic()
    if not force_refresh and _models_cache and (now - _models_cache_ts) < _CACHE_TTL:
        return list(_models_cache)

    try:
        from agent.agy_state import build_agy_env, ensure_account_home, get_account_manager

        mgr = get_account_manager()
        candidates = mgr.enabled_accounts() or mgr.list_accounts()
    except Exception:
        candidates = [{"name": "default", "home": str(Path.home())}]
        build_agy_env = None
        ensure_account_home = None

    agy_bin = os.environ.get("AGY_BIN") or shutil.which("agy") or AGY_DEFAULT_BIN
    for acc in candidates[:3]:
        home = acc.get("home") or str(Path.home())
        try:
            if ensure_account_home:
                ensure_account_home(Path(home))
            env = build_agy_env(home) if build_agy_env else dict(os.environ, HOME=home)
            res = subprocess.run(
                [agy_bin, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
            parsed = _parse_models_output(f"{res.stdout}\n{res.stderr}")
            if parsed:
                _models_cache = parsed
                _models_cache_ts = time.monotonic()
                return list(parsed)
        except Exception as exc:
            logger.debug("agy models probe failed on %s: %s", acc.get("name"), exc)

    if _models_cache:
        return list(_models_cache)
    return [(m, m) for m in FALLBACK_AGY_MODELS]


class AntigravityProfile(ProviderProfile):
    """Antigravity CLI (`agy`) provider with multi-account rotation."""

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0) -> list[str] | None:
        pairs = _probe_agy_models(force_refresh=False)
        return [slug for slug, _ in pairs] if pairs else list(FALLBACK_AGY_MODELS)

    def discover_models(self, **kwargs: Any) -> list[dict[str, str]] | None:
        pairs = _probe_agy_models(force_refresh=bool(kwargs.get("force_refresh")))
        return [{"id": slug, "label": label, "note": "agy"} for slug, label in pairs]

    def invalidate_catalog_cache(self) -> None:
        global _models_cache_ts
        _models_cache_ts = 0.0

    def external_auth_evidence(self, resolved_command: str | None = None) -> tuple[bool, str | None]:
        try:
            from agent.agy_state import get_account_manager

            enabled = get_account_manager().enabled_accounts()
            if enabled:
                names = ",".join(a["name"] for a in enabled)
                return True, f"agy accounts ({names})"
        except Exception:
            pass
        if resolved_command:
            return True, resolved_command
        return False, None

    def get_model_context_length(self, model: str) -> int | None:
        low = (model or "").lower()
        if "gemini" in low:
            return 1_000_000
        if "claude" in low:
            return 200_000
        return 128_000


antigravity = AntigravityProfile(
    name="antigravity",
    aliases=("agy", "agymes", "antigravity-cli"),
    display_name="Agymes",
    description="Agymes multi-account engine",
    api_mode="chat_completions",
    auth_type="external_process",
    base_url="process://antigravity",
    process_command=AGY_DEFAULT_BIN,
    process_args=(),
    process_command_env_vars=("AGY_BIN",),
    supports_streaming=True,
    supports_vision=True,
    supports_health_check=False,
    supports_model_listing=True,
    fallback_models=FALLBACK_AGY_MODELS,
    static_models=FALLBACK_AGY_MODELS,
    default_aux_model="gemini-3.8-flash-low",
    model_capabilities={
        slug: {
            "supports_reasoning": True,
            "supports_vision": True,
            "supports_tools": True,
            "context_window": 1_000_000 if "gemini" in slug else 200_000,
            "model_family": "antigravity",
        }
        for slug in FALLBACK_AGY_MODELS
    },
)

register_provider(antigravity)
