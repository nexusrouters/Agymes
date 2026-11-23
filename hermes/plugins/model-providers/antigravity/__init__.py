"""Agymes (`antigravity` / `agy`) model provider plugin with full multi-account catalog discovery."""

from __future__ import annotations

import concurrent.futures
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
_account_models_cache: dict[str, set[str]] = {}
_models_cache_ts: float = 0.0

FALLBACK_AGY_MODELS: tuple[str, ...] = (
    "gemini-4-neon-high",
    "gemini-4-neon-xhigh",
    "gemini-4-neon-medium",
    "gemini-4-neon-low",
    "gemini-4-carbon-medium",
    "gemini-4-carbon-high",
    "gemini-4-carbon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-low",
    "gemini-pro-orchestrator-neon",
    "gemini-pro-orchestrator",
    "gemini-pro-orchestrator-argon",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
)


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


def _probe_single_account(agy_bin: str, acc: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    name = str(acc.get("name") or "default")
    home = str(acc.get("home") or Path.home())
    try:
        from agent.agy_state import build_agy_env, ensure_account_home

        ensure_account_home(Path(home))
        env = build_agy_env(home)
    except Exception:
        env = dict(os.environ, HOME=home)

    try:
        res = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return name, _parse_models_output(f"{res.stdout}\n{res.stderr}")
    except Exception as exc:
        logger.debug("agy models probe failed on %s: %s", name, exc)
        return name, []


def _probe_agy_models(force_refresh: bool = False) -> list[tuple[str, str]]:
    """Probe all enabled Agymes accounts in parallel and merge the full union of models."""
    global _models_cache, _account_models_cache, _models_cache_ts
    now = time.monotonic()
    if not force_refresh and _models_cache and (now - _models_cache_ts) < _CACHE_TTL:
        return list(_models_cache)

    try:
        from agent.agy_state import get_account_manager

        mgr = get_account_manager()
        candidates = mgr.enabled_accounts() or mgr.list_accounts()
    except Exception:
        candidates = [{"name": "default", "home": str(Path.home())}]

    agy_bin = os.environ.get("AGY_BIN") or shutil.which("agy") or AGY_DEFAULT_BIN

    # Probe all enabled accounts concurrently (capped at 6 workers) so latency stays ~2-3s
    per_account: dict[str, list[tuple[str, str]]] = {}
    if len(candidates) <= 1:
        for acc in candidates:
            n, pairs = _probe_single_account(agy_bin, acc)
            per_account[n] = pairs
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
            futs = [pool.submit(_probe_single_account, agy_bin, acc) for acc in candidates]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    n, pairs = fut.result()
                    per_account[n] = pairs
                except Exception:
                    pass

    merged: list[tuple[str, str]] = []
    seen: set[str] = set()
    acc_map: dict[str, set[str]] = {}
    # Preserve account order (`default`, `akun2`, `akun3`, ...) so primary account models appear first
    for acc in candidates:
        n = str(acc.get("name") or "default")
        pairs = per_account.get(n) or []
        if pairs:
            acc_map[n] = {slug for slug, _ in pairs}
        for slug, label in pairs:
            if slug not in seen:
                seen.add(slug)
                merged.append((slug, label))

    if merged:
        _models_cache = merged
        _account_models_cache = acc_map
        _models_cache_ts = time.monotonic()
        return list(merged)

    if _models_cache:
        return list(_models_cache)
    return [(m, m) for m in FALLBACK_AGY_MODELS]


def get_account_models_map(force_refresh: bool = False) -> dict[str, set[str]]:
    """Return cached `{account_name: {model_slugs}}` for smart model-aware account routing."""
    if force_refresh or not _account_models_cache:
        _probe_agy_models(force_refresh=force_refresh)
    return {k: set(v) for k, v in _account_models_cache.items()}


def _family_for_model(slug: str, label: str) -> tuple[str, str]:
    base_label = re.sub(r"\s*\([^)]*\)\s*$", "", label).strip() or slug
    fam_slug = re.sub(r"[^a-z0-9]+", "-", base_label.lower()).strip("-") or "other"
    return fam_slug, base_label


def get_agy_models_grouped(
    current_model: str,
    *,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    """Build grouped family list for Hermes TelegramAdapter.send_model_picker (exact original UX)."""
    pairs = _probe_agy_models(force_refresh=force_refresh)
    label_map = dict(pairs)
    providers: list[dict[str, Any]] = []
    buckets: dict[str, dict[str, Any]] = {}
    current_family = ""

    for slug, label in pairs:
        fam_slug, fam_name = _family_for_model(slug, label)
        entry = buckets.get(fam_slug)
        if entry is None:
            entry = {
                "slug": fam_slug,
                "name": fam_name,
                "models": [],
                "total_models": 0,
                "is_current": False,
            }
            buckets[fam_slug] = entry
            providers.append(entry)
        entry["models"].append(slug)
        entry["total_models"] += 1
        if slug == current_model:
            entry["is_current"] = True
            current_family = fam_slug

    if not current_family and providers:
        current_family = providers[0]["slug"]
    return providers, current_family, label_map


class AntigravityProfile(ProviderProfile):
    """Agymes (`agy`) provider with multi-account rotation and full live model discovery."""

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0) -> list[str] | None:
        pairs = _probe_agy_models(force_refresh=False)
        return [slug for slug, _ in pairs] if pairs else list(FALLBACK_AGY_MODELS)

    def discover_models(self, **kwargs: Any) -> list[dict[str, str]] | None:
        pairs = _probe_agy_models(force_refresh=bool(kwargs.get("force_refresh")))
        return [{"id": slug, "label": label, "note": "Agymes"} for slug, label in pairs]

    def invalidate_catalog_cache(self) -> None:
        global _models_cache_ts
        _models_cache_ts = 0.0

    def external_auth_evidence(self, resolved_command: str | None = None) -> tuple[bool, str | None]:
        try:
            from agent.agy_state import get_account_manager

            enabled = get_account_manager().enabled_accounts()
            if enabled:
                names = ",".join(a["name"] for a in enabled)
                return True, f"agymes accounts ({names})"
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
