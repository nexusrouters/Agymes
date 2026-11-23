# Agymes

**Agymes** adalah asisten AI & Telegram Gateway berbasis fork mandiri Hermes Agent yang terintegrasi penuh dengan **Antigravity CLI (`agy`)**, dilengkapi **rotasi multi-akun otomatis**, **auto-failover kuota/auth**, **konteks bersama lintas akun**, dan **CLI `agymes`** (`agymes doctor`, `agymes account`, `agymes setup`, `agymes gateway`, dll.).

---

## Instalasi Otomatis (1 Perintah)

Installer otomatis akan memasang **Antigravity CLI (`agy`)** + **Agymes** + Python environment + perintah global `agymes`:

```bash
curl -fsSL https://raw.githubusercontent.com/nexusrouters/Agymes/main/install.sh | bash
```

---

## Quick Start

### 1. Setup awal (Telegram Bot Token + login akun pertama)
```bash
agymes setup
```

### 2. Tambah multi-akun `agy` otomatis (rotasi round-robin + failover)
```bash
# Tambah akun baru otomatis (akun2, akun3, dst.), langsung login Google OAuth & aktif di rotasi
agymes account add

# Atau beri nama & label khusus
agymes account add akun3 "Akun Kerja"

# Lihat daftar & status semua akun
agymes account list

# Uji autentikasi semua akun
agymes account test
```

### 3. Cek kesehatan sistem
```bash
agymes doctor
agymes doctor --deep
```

### 4. Jalankan Telegram Gateway
```bash
agymes gateway start     # Jalankan di background (tmux)
agymes gateway status    # Cek status
agymes gateway logs      # Lihat log live
agymes gateway restart   # Restart gateway
agymes gateway stop      # Hentikan gateway
```

---

## Perintah CLI `agymes`

| Perintah | Fungsi |
| :--- | :--- |
| `agymes setup` | Wizard konfigurasi `.env`, Telegram token, dan login awal |
| `agymes doctor` | Cek kesehatan `agy`, Python, dependensi, config, Telegram, dan multi-akun |
| `agymes account list` | Lihat daftar akun & jumlah request per akun |
| `agymes account add [nama]` | Buat profil terisolasi, login Google OAuth, verifikasi, & aktifkan ke rotasi |
| `agymes account login <nama>` | Re-login akun yang sudah terdaftar |
| `agymes account test [nama]` | Uji autentikasi 1 akun atau semua akun |
| `agymes account enable\|disable <nama>` | Aktif / nonaktifkan akun dari rotasi |
| `agymes account remove <nama> [--purge]` | Hapus akun dari daftar rotasi |
| `agymes account reset` | Reset cooldown & status error semua akun |
| `agymes gateway start\|stop\|restart\|logs` | Kelola bot Telegram Agymes |
| `agymes model` | Pilih model default |
| `agymes chat` | Chat interaktif langsung di terminal |
| `agymes version` | Tampilkan versi & ringkasan akun aktif |

Semua subcommand bawaan Hermes (`skills`, `cron`, `sessions`, `config`, `mcp`, `plugins`, `memory`, `tools`, dll.) juga bisa langsung dijalankan lewat `agymes <subcommand>`.

---

## Perintah di Telegram

- `/account` (`/akun`) — Kelola multi-akun & tombol interaktif
- `/rotation` (`/rotasi`) — Pilih mode rotasi (`rotate` / `failover` / `manual`)
- `/model` — Pilih model AI (picker interaktif)
- `/mode` — Mode eksekusi (`auto` / `accept-edits` / `plan`)
- `/effort` — Atur reasoning effort (`default` / `low` / `medium` / `high` / `xhigh` / `max`)
- `/cd <folder>` — Ubah folder kerja
- `/status` — Lihat status gateway, sesi, & multi-akun
- `/profile` — Lihat profil aktif Agymes & semua akun
- `/new` — Mulai sesi baru & reset konteks bersama
- `/stop` — Hentikan proses yang sedang berjalan
- `/help` — Daftar semua perintah
