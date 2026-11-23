#!/usr/bin/env python3
"""Portable entry point for the standalone Agymes Gateway."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
HERMES_FORK_DIR = REPO_ROOT / "hermes"


def _resolve_agymes_home() -> Path:
    for key in ("AGYMES_HOME", "AGY_BOT_DIR"):
        v = os.environ.get(key, "").strip()
        if v:
            return Path(v).expanduser().resolve()
    return (Path.home() / ".agymes").resolve()


def _resolve_hermes_home(agymes_home: Path) -> Path:
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    nested = agymes_home / "hermes_home"
    if nested.is_dir() and (nested / "config.yaml").exists():
        return nested
    return agymes_home


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    # Isolate Agymes Telegram Gateway from unrelated host platform tokens
    for unused_var in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "DISCORD_BOT_TOKEN",
        "WHATSAPP_ENABLED",
        "MATRIX_ACCESS_TOKEN",
        "MATTERMOST_TOKEN",
        "SIGNAL_HTTP_URL",
    ):
        os.environ.pop(unused_var, None)

    agymes_home = _resolve_agymes_home()
    hermes_home = _resolve_hermes_home(agymes_home)
    agymes_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)

    os.environ["AGYMES_HOME"] = str(agymes_home)
    os.environ["AGY_BOT_DIR"] = str(agymes_home)
    os.environ["HERMES_HOME"] = str(hermes_home)

    _load_env_file(hermes_home / ".env")
    _load_env_file(agymes_home / ".env")

    for p in (str(REPO_ROOT), str(HERMES_FORK_DIR)):
        if p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(HERMES_FORK_DIR))

    import gateway.run as agymes_gateway_run

    agymes_gateway_run.main()


if __name__ == "__main__":
    main()
