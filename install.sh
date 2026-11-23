#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Agymes Automated Installer
# Installs Antigravity CLI (`agy`) + Agymes Gateway & CLI (`agymes`)
# Repo: https://github.com/nexusrouters/Agymes
# ==============================================================================

REPO_URL="${AGYMES_REPO_URL:-https://github.com/nexusrouters/Agymes.git}"
AGYMES_HOME="${AGYMES_HOME:-$HOME/.agymes}"
INSTALL_DIR="${AGYMES_INSTALL_DIR:-$AGYMES_HOME/src}"
VENV_DIR="$AGYMES_HOME/venv"
LOCAL_BIN="$HOME/.local/bin"

log()  { printf "\033[1;36m[Agymes]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[  OK  ]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[ WARN ]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ ERR  ]\033[0m %s\n" "$*" >&2; }

mkdir -p "$AGYMES_HOME" "$LOCAL_BIN"
export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"

log "Memulai instalasi Agymes + Antigravity CLI (agy)..."

# 1. Check basic prerequisites
for dep in curl git; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    err "Perintah '$dep' belum terpasang. Install dulu (mis. apt install -y $dep)."
    exit 1
  fi
done

if ! command -v tmux >/dev/null 2>&1; then
  warn "tmux belum terpasang (disarankan untuk background gateway: apt install -y tmux)."
fi

# 2. Auto-install Antigravity CLI (`agy`) if missing
if command -v agy >/dev/null 2>&1 || [ -x "$LOCAL_BIN/agy" ]; then
  ok "Antigravity CLI (agy) sudah terpasang: $(command -v agy || echo "$LOCAL_BIN/agy")"
else
  log "Menginstall Antigravity CLI (agy)..."
  curl -fsSL https://antigravity.google/cli/install.sh | bash
  export PATH="$LOCAL_BIN:$PATH"
  if command -v agy >/dev/null 2>&1 || [ -x "$LOCAL_BIN/agy" ]; then
    ok "Antigravity CLI (agy) berhasil terpasang!"
  else
    warn "Binary agy belum terdeteksi di PATH — pastikan ~/.local/bin ada di PATH."
  fi
fi

# 3. Clone or update Agymes repository
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Memperbarui kode Agymes di $INSTALL_DIR..."
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  log "Mengunduh kode Agymes dari $REPO_URL..."
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi
ok "Kode Agymes siap di $INSTALL_DIR"

# 4. Install uv if neither uv nor Python 3.11+ venv is ready
if ! command -v uv >/dev/null 2>&1; then
  log "Menginstall uv (Python package & runtime manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$LOCAL_BIN:$HOME/.cargo/bin:$PATH"
fi

# 5. Create isolated Python 3.12 virtualenv & install dependencies
log "Menyiapkan Python environment di $VENV_DIR..."
if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 "$VENV_DIR" --allow-existing
  uv pip install --python "$VENV_DIR/bin/python" -r "$INSTALL_DIR/requirements.txt"
else
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
fi
ok "Python environment & dependensi siap."

# 6. Seed default Agymes configuration (never overwrites existing user files)
for fname in config.yaml SOUL.md; do
  if [ ! -f "$AGYMES_HOME/$fname" ] && [ -f "$INSTALL_DIR/templates/$fname" ]; then
    cp "$INSTALL_DIR/templates/$fname" "$AGYMES_HOME/$fname"
  fi
done
if [ ! -f "$AGYMES_HOME/.env" ] && [ -f "$INSTALL_DIR/templates/.env.example" ]; then
  cp "$INSTALL_DIR/templates/.env.example" "$AGYMES_HOME/.env"
  chmod 600 "$AGYMES_HOME/.env"
fi

# 7. Install global `agymes` CLI binary
chmod +x "$INSTALL_DIR/bin/agymes" "$INSTALL_DIR/agymes_cli.py" "$INSTALL_DIR/run_agymes_gateway.py" "$INSTALL_DIR/account.sh"
ln -sf "$INSTALL_DIR/bin/agymes" "$LOCAL_BIN/agymes"
if [ -w "/usr/local/bin" ]; then
  ln -sf "$INSTALL_DIR/bin/agymes" "/usr/local/bin/agymes"
fi

# Ensure ~/.local/bin is on PATH in bashrc/zshrc
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$rc" ] && ! grep -q '\.local/bin' "$rc"; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
  fi
done

ok "Perintah global 'agymes' terpasang di $LOCAL_BIN/agymes"
echo ""

# 8. Run doctor diagnostics
"$LOCAL_BIN/agymes" doctor || true

echo ""
echo "================================================================"
echo " 🎉 Instalasi Agymes selesai!"
echo "================================================================"
echo "Langkah berikutnya:"
echo "  1. Konfigurasi Telegram & login akun utama:"
echo "       agymes setup"
echo "  2. Tambah akun tambahan otomatis untuk rotasi multi-akun:"
echo "       agymes account add"
echo "       agymes account add akun3"
echo "  3. Cek status semua akun & sistem:"
echo "       agymes account list"
echo "       agymes doctor"
echo "  4. Jalankan gateway Telegram di background:"
echo "       agymes gateway start"
echo "================================================================"
