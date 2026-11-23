#!/usr/bin/env python3
"""Agymes CLI — unified command line for Agymes AI & multi-account Antigravity (`agy`).

Subcommands handled directly by Agymes:
  agymes account [list|add [name] [label]|login <name>|test [name]|enable <name>|disable <name>|remove <name> [--purge]|reset]
  agymes doctor [--deep]
  agymes setup
  agymes gateway [start|stop|restart|run|status|logs]
  agymes version

Every other command (`chat`, `model`, `status`, `skills`, `cron`, `sessions`,
`config`, `mcp`, `plugins`, `memory`, `logs`, `tools`, `pairing`, etc.) runs on
the bundled customized Hermes Agent fork (`hermes/`) with `prog="agymes"`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

AGYMES_VERSION = "1.0.0"
REPO_ROOT = Path(__file__).resolve().parent
HERMES_FORK_DIR = REPO_ROOT / "hermes"


def resolve_agymes_home() -> Path:
    for key in ("AGYMES_HOME", "AGY_BOT_DIR"):
        v = os.environ.get(key, "").strip()
        if v:
            return Path(v).expanduser().resolve()
    return (Path.home() / ".agymes").resolve()


def resolve_hermes_home(agymes_home: Path) -> Path:
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    nested = agymes_home / "hermes_home"
    if nested.is_dir() and (nested / "config.yaml").exists():
        return nested
    return agymes_home


def resolve_agy_bin() -> str:
    env_bin = os.environ.get("AGY_BIN", "").strip()
    if env_bin:
        return str(Path(env_bin).expanduser())
    found = shutil.which("agy")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "agy")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ensure_initialized(agymes_home: Path, hermes_home: Path) -> None:
    agymes_home.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)
    templates = REPO_ROOT / "templates"
    for fname in ("config.yaml", "SOUL.md"):
        dst = hermes_home / fname
        src = templates / fname
        if not dst.exists() and src.exists():
            shutil.copy2(src, dst)
    env_dst = hermes_home / ".env"
    env_tpl = templates / ".env.example"
    if not env_dst.exists() and env_tpl.exists():
        shutil.copy2(env_tpl, env_dst)
        try:
            os.chmod(env_dst, 0o600)
        except OSError:
            pass


def _setup_sys_path(agymes_home: Path, hermes_home: Path) -> None:
    os.environ["AGYMES_HOME"] = str(agymes_home)
    os.environ["AGY_BOT_DIR"] = str(agymes_home)
    os.environ["HERMES_HOME"] = str(hermes_home)
    load_env_file(hermes_home / ".env")
    load_env_file(agymes_home / ".env")
    for p in (str(REPO_ROOT), str(HERMES_FORK_DIR)):
        if p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(HERMES_FORK_DIR))


def _check_agy_models(agy_bin: str, account_home: str) -> tuple[bool, list[str], str]:
    from accounts import build_agy_env, classify_agy_error, ensure_account_home

    ensure_account_home(Path(account_home))
    try:
        res = subprocess.run(
            [agy_bin, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=build_agy_env(account_home),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return False, [], f"Binary `agy` tidak ditemukan di {agy_bin}"
    except subprocess.TimeoutExpired:
        return False, [], "Timeout (30 detik)"

    combined = f"{res.stdout}\n{res.stderr}".strip()
    err_kind, _ = classify_agy_error(res.stderr, res.stdout, res.returncode)
    if err_kind == "auth":
        return False, [], "Belum login (Please sign in)"
    models = [ln.split("\t", 1)[0].strip() for ln in res.stdout.splitlines() if "\t" in ln]
    if models:
        return True, models, f"{len(models)} model tersedia"
    if res.returncode == 0:
        return True, [], "Terautentikasi"
    return False, [], combined[:200] or f"Exit code {res.returncode}"


def cmd_account(argv: list[str], agymes_home: Path) -> int:
    from accounts import AccountManager, build_agy_env, ensure_account_home

    mgr = AccountManager(agymes_home)
    agy_bin = resolve_agy_bin()
    sub = argv[0].lower() if argv else "list"
    rest = argv[1:]

    if sub in ("-h", "--help", "help"):
        print(
            "Penggunaan `agymes account`:\n"
            "  agymes account list                    — Tampilkan semua akun & status rotasi\n"
            "  agymes account add [nama] [label]      — Buat profil baru (auto akun2/akun3/...), login OAuth, & aktifkan otomatis\n"
            "  agymes account login <nama>            — Login / re-login akun yang sudah ada\n"
            "  agymes account test [nama]             — Uji autentikasi 1 akun atau semua akun\n"
            "  agymes account enable <nama>           — Aktifkan akun di rotasi\n"
            "  agymes account disable <nama>          — Nonaktifkan akun dari rotasi\n"
            "  agymes account remove <nama> [--purge] — Hapus akun (--purge untuk hapus foldernya)\n"
            "  agymes account reset                   — Bersihkan cooldown & error semua akun\n"
        )
        return 0

    if sub == "list":
        now = int(time.time())
        print(f"{'NAMA':14} {'LABEL':18} {'STATUS':16} {'REQ':>5}  {'HOME'}")
        print("-" * 86)
        for acc in mgr.list_accounts():
            cd = int(acc.get("cooldown_until") or 0)
            if not acc.get("enabled"):
                st = "NONAKTIF"
            elif acc.get("needs_login"):
                st = "PERLU LOGIN"
            elif cd > now:
                st = f"COOLDOWN {cd - now}s"
            else:
                st = "SIAP"
            print(f"{acc['name']:14} {str(acc.get('label') or '')[:18]:18} {st:16} {acc.get('request_count', 0):>5}  {acc['home']}")
        return 0

    if sub in ("add", "login"):
        if sub == "add":
            name = rest[0] if rest else mgr.next_auto_account_name()
            label = rest[1] if len(rest) > 1 else f"Agymes {name.replace('akun', '').strip() or name}"
            acc = mgr.add_account(name, label=label, enabled=False)
        else:
            if not rest:
                print("Error: masukkan nama akun. Contoh: agymes account login akun2", file=sys.stderr)
                return 1
            name = rest[0]
            acc = mgr.get_account(name) or mgr.add_account(name, enabled=False)

        prof_home = str(ensure_account_home(Path(acc["home"])))
        if not Path(agy_bin).exists() and not shutil.which(agy_bin):
            print(f"❌ Binary `agy` belum terpasang ({agy_bin}). Jalankan `curl -fsSL https://antigravity.google/cli/install.sh | bash`.", file=sys.stderr)
            return 1

        print("")
        print("================================================================")
        print(f" Login Agymes untuk profil akun: {acc['name']} ({acc.get('label')})")
        print(f" HOME profil: {prof_home}")
        print("================================================================")
        print("1. Ikuti instruksi login Google OAuth di bawah ini.")
        print("2. Setelah masuk ke prompt agy, ketik /exit atau tekan Ctrl+D 2x.")
        print("")

        env = build_agy_env(prof_home)
        subprocess.run([agy_bin], env=env, check=False)

        print(f"\nMemverifikasi login akun '{acc['name']}'...")
        ok, models, detail = _check_agy_models(agy_bin, prof_home)
        if not ok:
            mgr.mark_error(acc["name"], "auth", detail, 1800)
            print(f"❌ Akun '{acc['name']}' belum berhasil login ({detail}). Jalankan ulang: agymes account login {acc['name']}")
            return 1

        mgr.mark_authenticated(acc["name"], enable=True)
        print(f"✅ Akun '{acc['name']}' terverifikasi ({detail}) & AKTIF otomatis di rotasi multi-akun!")
        return 0

    if sub == "test":
        targets = [mgr.get_account(rest[0])] if rest else mgr.list_accounts()
        if not targets or targets[0] is None:
            print(f"❌ Akun '{rest[0]}' tidak ditemukan.", file=sys.stderr)
            return 1
        all_ok = True
        for acc in targets:
            ok, models, detail = _check_agy_models(agy_bin, acc["home"])
            if ok:
                mgr.mark_authenticated(acc["name"], enable=bool(acc.get("enabled", True)))
                print(f"✅ {acc['name']:14} OK — {detail}")
            else:
                all_ok = False
                mgr.mark_error(acc["name"], "auth", detail, 1800)
                print(f"❌ {acc['name']:14} GAGAL — {detail}")
        return 0 if all_ok else 1

    if sub in ("enable", "on", "disable", "off"):
        if not rest:
            print(f"Error: masukkan nama akun. Contoh: agymes account {sub} akun2", file=sys.stderr)
            return 1
        acc = mgr.set_enabled(rest[0], sub in ("enable", "on"))
        print(f"✅ Akun '{acc['name']}' -> {'AKTIF' if acc['enabled'] else 'NONAKTIF'}")
        return 0

    if sub in ("remove", "del", "rm"):
        if not rest:
            print("Error: masukkan nama akun.", file=sys.stderr)
            return 1
        purge = "--purge" in rest[1:]
        ok = mgr.remove_account(rest[0], purge=purge)
        print(f"✅ Akun '{rest[0]}' dihapus." if ok else f"❌ Akun '{rest[0]}' tidak ditemukan.")
        return 0 if ok else 1

    if sub == "reset":
        n = mgr.clear_cooldowns()
        print(f"✅ Cooldown & status error direset untuk {n} akun.")
        return 0

    print(f"Subperintah tidak dikenal: {sub}. Jalankan `agymes account --help`.", file=sys.stderr)
    return 1


def cmd_doctor(argv: list[str], agymes_home: Path, hermes_home: Path) -> int:
    from accounts import AccountManager

    agy_bin = resolve_agy_bin()
    print(f"🩺 Agymes Doctor v{AGYMES_VERSION}")
    print("=" * 60)
    ok_count = 0
    warn_count = 0

    def check(ok: bool, label: str, detail: str = "") -> None:
        nonlocal ok_count, warn_count
        icon = "✅" if ok else "❌"
        if ok:
            ok_count += 1
        else:
            warn_count += 1
        suffix = f" — {detail}" if detail else ""
        print(f" {icon} {label}{suffix}")

    agy_exists = Path(agy_bin).exists() or bool(shutil.which(agy_bin))
    check(agy_exists, "Antigravity CLI (agy)", agy_bin if agy_exists else "Belum terpasang! Jalankan install.sh")

    check(sys.version_info >= (3, 11), "Python Runtime", f"{sys.version.split()[0]} ({sys.executable})")

    try:
        import telegram  # noqa: F401
        import httpx  # noqa: F401
        import yaml  # noqa: F401
        check(True, "Dependensi Agymes", "python-telegram-bot, httpx, pyyaml siap")
    except ImportError as exc:
        check(False, "Dependensi Agymes", f"Kurang paket: {exc}")

    cfg_path = hermes_home / "config.yaml"
    check(cfg_path.exists(), "Konfigurasi Agymes", str(cfg_path))

    token_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    check(token_set, "Telegram Bot Token", "terpasang di .env" if token_set else "belum diisi (jalankan `agymes setup`)")

    mgr = AccountManager(agymes_home)
    accs = mgr.list_accounts()
    enabled = [a for a in accs if a.get("enabled") and not a.get("needs_login")]
    check(len(enabled) > 0, "Multi-Akun Agymes", f"{len(enabled)}/{len(accs)} akun siap ({', '.join(a['name'] for a in enabled) or 'belum ada'})")

    tmux_ok = bool(shutil.which("tmux"))
    check(tmux_ok, "tmux (background gateway)", shutil.which("tmux") or "tidak terpasang (opsional)")

    print("-" * 60)
    print(f"Ringkasan: {ok_count} OK, {warn_count} perlu perhatian | Home: {agymes_home}")

    if "--deep" in argv:
        print("\nMenjalankan pemeriksaan mendalam (Hermes Core Doctor)...")
        sys.argv = ["agymes", "doctor", *[a for a in argv if a != "--deep"]]
        from hermes_cli.main import main as hermes_main
        hermes_main()
    return 0 if warn_count == 0 else 1


def cmd_setup(agymes_home: Path, hermes_home: Path) -> int:
    print(f"⚙️  Agymes Interactive Setup (v{AGYMES_VERSION})")
    print(f"Home: {agymes_home}\n")

    env_path = hermes_home / ".env"
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                existing[k.strip()] = v.strip()

    cur_tok = existing.get("TELEGRAM_BOT_TOKEN", "")
    masked = f"{cur_tok[:8]}...{cur_tok[-4:]}" if len(cur_tok) > 14 else ("(terisi)" if cur_tok else "(kosong)")
    tok = input(f"1. Telegram Bot Token dari @BotFather [{masked}]: ").strip() or cur_tok

    cur_uid = existing.get("TELEGRAM_ALLOWED_USERS", "")
    uid = input(f"2. Telegram User ID yang diizinkan (pisahkan koma) [{cur_uid or 'kosong'}]: ").strip() or cur_uid

    cur_wd = existing.get("AGY_WORKDIR", str(Path.home()))
    wd = input(f"3. Folder kerja default [{cur_wd}]: ").strip() or cur_wd

    out = [
        f"TELEGRAM_BOT_TOKEN={tok}",
        f"TELEGRAM_ALLOWED_USERS={uid}",
        f"AGYMES_HOME={agymes_home}",
        f"AGY_WORKDIR={wd}",
        f"AGY_BIN={resolve_agy_bin()}",
        "AGY_MODE=auto",
        "AGY_TIMEOUT_SECONDS=0",
        "AGY_IDLE_TIMEOUT_SECONDS=900",
    ]
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    print(f"\n✅ Konfigurasi tersimpan di {env_path}")

    ans = input("\nIngin login akun utama `agy` sekarang? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        cmd_account(["login", "default"], agymes_home)

    ans2 = input("Ingin langsung tambah akun ke-2 untuk rotasi multi-akun? [y/N]: ").strip().lower()
    if ans2 in ("y", "yes"):
        cmd_account(["add"], agymes_home)

    print("\n🎉 Setup selesai! Jalankan gateway dengan: agymes gateway start")
    return 0


def cmd_gateway(argv: list[str], agymes_home: Path, hermes_home: Path) -> int:
    action = argv[0].lower() if argv else "run"
    session = os.environ.get("AGYMES_TMUX_SESSION", "agymes-gateway")
    log_file = agymes_home / "bot.log"
    launcher = REPO_ROOT / "run_agymes_gateway.py"

    if action == "run":
        import run_agymes_gateway
        sys.argv = ["agymes-gateway", *argv[1:]]
        run_agymes_gateway.main()
        return 0

    if action == "start":
        if not shutil.which("tmux"):
            print("❌ `tmux` tidak ditemukan. Jalankan di foreground dengan: agymes gateway run", file=sys.stderr)
            return 1
        has = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0
        if has:
            print(f"Agymes Gateway sudah berjalan di tmux session '{session}'.")
            return 0
        cmd_str = (
            f"AGYMES_HOME='{agymes_home}' HERMES_HOME='{hermes_home}' "
            f"PYTHONPATH='{HERMES_FORK_DIR}:{REPO_ROOT}' "
            f"'{sys.executable}' -u '{launcher}' >> '{log_file}' 2>&1"
        )
        subprocess.run(["tmux", "new-session", "-d", "-s", session, cmd_str], check=True)
        time.sleep(2)
        if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
            print(f"✅ Agymes Gateway berjalan di background (tmux: {session}). Log: {log_file}")
            return 0
        print(f"❌ Agymes Gateway gagal start. Cek log: {log_file}", file=sys.stderr)
        return 1

    if action == "stop":
        if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
            subprocess.run(["tmux", "kill-session", "-t", session], check=False)
            print("✅ Agymes Gateway dihentikan.")
        else:
            print("Agymes Gateway tidak sedang berjalan.")
        return 0

    if action == "restart":
        cmd_gateway(["stop"], agymes_home, hermes_home)
        return cmd_gateway(["start"], agymes_home, hermes_home)

    if action == "status":
        running = bool(shutil.which("tmux")) and (
            subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0
        )
        print(f"Agymes Gateway ({session}): {'🟢 RUNNING' if running else '⚪ STOPPED'}")
        state_file = hermes_home / "gateway_state.json"
        if state_file.exists():
            print(f"State: {state_file}")
        print(f"Log:   {log_file}")
        return 0

    if action == "logs":
        target = hermes_home / "logs" / "gateway.log"
        if not target.exists():
            target = log_file
        if not target.exists():
            print("Belum ada file log.")
            return 0
        os.execvp("tail", ["tail", "-n", "60", "-f", str(target)])

    # Delegate unknown gateway subcommands to forked Hermes gateway CLI
    sys.argv = ["agymes", "gateway", *argv]
    from hermes_cli.main import main as hermes_main
    hermes_main()
    return 0


def cmd_version(agymes_home: Path) -> int:
    from accounts import AccountManager

    accs = AccountManager(agymes_home).enabled_accounts()
    print(f"Agymes v{AGYMES_VERSION}")
    print(f"Home:   {agymes_home}")
    print(f"agy:    {resolve_agy_bin()}")
    print(f"Akun:   {len(accs)} aktif ({', '.join(a['name'] for a in accs) or '-'})")
    return 0


def main() -> None:
    agymes_home = resolve_agymes_home()
    hermes_home = resolve_hermes_home(agymes_home)
    ensure_initialized(agymes_home, hermes_home)
    _setup_sys_path(agymes_home, hermes_home)

    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(
            f"Agymes v{AGYMES_VERSION} — AI Assistant & Multi-Account Antigravity Gateway\n\n"
            "Perintah utama:\n"
            "  agymes setup                    Wizard konfigurasi awal (Telegram + akun pertama)\n"
            "  agymes doctor [--deep]          Cek kesehatan Agymes, agy, akun, & Telegram\n"
            "  agymes account [subcommand]     Kelola rotasi multi-akun (list|add|login|test|enable|disable|remove|reset)\n"
            "  agymes gateway <aksi>           Kelola gateway Telegram (start|stop|restart|run|status|logs)\n"
            "  agymes model                    Pilih model default\n"
            "  agymes chat                     Mulai chat interaktif di terminal\n"
            "  agymes status                   Status sesi & runtime\n"
            "  agymes version                  Versi Agymes & ringkasan akun\n\n"
            "Semua subcommand Hermes Agent lainnya (skills, cron, sessions, config, mcp, plugins, logs, dll.)\n"
            "juga langsung tersedia lewat `agymes <subcommand>`."
        )
        return

    cmd = argv[0].lower()
    rest = argv[1:]

    if cmd in ("account", "akun", "accounts"):
        sys.exit(cmd_account(rest, agymes_home))
    if cmd == "doctor":
        sys.exit(cmd_doctor(rest, agymes_home, hermes_home))
    if cmd == "setup":
        sys.exit(cmd_setup(agymes_home, hermes_home))
    if cmd == "gateway":
        sys.exit(cmd_gateway(rest, agymes_home, hermes_home))
    if cmd in ("version", "-v", "--version"):
        sys.exit(cmd_version(agymes_home))

    # Delegate all remaining commands to the bundled customized Hermes CLI
    sys.argv = ["agymes", *argv]
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == "__main__":
    main()
