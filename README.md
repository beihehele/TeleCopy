# 💬 TeleCopy - Telegram Message Copier & Archiver

![Python](https://img.shields.io/badge/Python-3.7%2B-blue.svg)
![Status](https://img.shields.io/badge/status-active-brightgreen.svg)


```
 _________  _______   ___       _______   ________  ________  ________  ___    ___ 
|\___   ___\\  ___ \ |\  \     |\  ___ \ |\   ____\|\   __  \|\   __  \|\  \  /  /|
\|___ \  \_\ \   __/|\ \  \    \ \   __/|\ \  \___|\ \  \|\  \ \  \|\  \ \  \/  / /
     \ \  \ \ \  \_|/_\ \  \    \ \  \_|/_\ \  \    \ \  \\\  \ \   ____\ \    / / 
      \ \  \ \ \  \_|\ \ \  \____\ \  \_|\ \ \  \____\ \  \\\  \ \  \___|\/  /  /  
       \ \__\ \ \_______\ \_______\ \_______\ \_______\ \_______\ \__\ __/  / /    
        \|__|  \|_______|\|_______|\|_______|\|_______|\|_______|\|__||\___/ /     
                                                                      \|___|/      
                                                                                   By HanuTyagi
```



## 🔧 Features

- 📤 Copy **past messages** from one Telegram chat to another
- 📅 **Custom date-range** filtering for selective cloning
- 🔄 **Live forwarding** of messages as they arrive
- 🤖 Optional **management Bot** for `/watch`, `/unwatch`, `/lswatch`, and `/copy`
- 🐳 **Daemon mode** for Docker: auto-connect and monitor on startup
- 📁 Supports all media types and polls
- 💾 SQLite-backed task and copy-progress persistence
- 🧼 Resets TDLib session when API credentials change
- 📝 Structured logging to console
- 🔁 Automatic retry with FloodWait handling and exponential back-off


---

## 🚀 Getting Started

### 1. Clone the Repository
```bash
git clone https://github.com/HanuTyagi/TeleCopy.git
cd TeleCopy
```

### 2. Create Virtual Environment
```bash
python -m venv .venv
```

### 3. Activate Virtual Environment
```bash
source .venv/bin/activate
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```
Or simply run the script — it will prompt you for any missing values on first launch.

### 6. Start TeleCopy

**Local development** — run the daemon directly:

```bash
python main.py
```

On first launch, TeleCopy connects to Telegram and may prompt for login in the terminal. After authorization, it keeps monitoring configured routes until you stop it with `Ctrl+C`.

**Docker** — use the two-step flow below (interactive login once, then background daemon).

---

## 🐳 Docker Deployment

Tagged releases publish a Linux `amd64` Docker image to GitHub Container Registry:

```bash
docker pull ghcr.io/beihehele/telecopy:latest
```

### 1. Configure `.env`

Copy `.env.example` to `.env` and fill in credentials. `SOURCE` and `DESTINATION` are optional; when both are set, TeleCopy monitors that built-in route automatically. Set `BOT_TOKEN` and `BOT_ADMIN_IDS` to enable private-chat management commands.

### 2. Log in once (interactive)

Host directory `./data` is bind-mounted to `/app/data` so sessions, SQLite state, and copy progress survive container restarts:

```bash
docker compose run --rm telecopy
```

Complete Telegram authorization in the terminal. When login succeeds, exit the container.

### 3. Run the daemon

```bash
docker compose up -d
```

TeleCopy connects on startup, restores watches from SQLite, and forwards new messages from all effective routes. The compose file sets `restart: unless-stopped`.

### Management Bot commands

When `BOT_TOKEN` is configured, authorized admins (`BOT_ADMIN_IDS`, comma-separated Telegram user IDs) can manage watches from a **private chat** with the Bot:

| Command | Description |
|---|---|
| `/watch <source_id> [destination_id]` | Add a dynamic watch. Uses env `DESTINATION` when destination is omitted. |
| `/unwatch <source_id>` | Remove a dynamic watch |
| `/lswatch` | List dynamic watches (built-in `SOURCE→DESTINATION` route is hidden) |
| `/copy <source_id> [destination_id]` | Queue a one-shot history copy job |

Live forwarding continues even when the Bot is not configured.

---

## 📦 Release Docker Image

Create and push a tag that starts with `v` to publish a Docker image:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The release workflow publishes:

- `ghcr.io/beihehele/telecopy:v1.0.0`
- `ghcr.io/beihehele/telecopy:latest`

The workflow only builds `linux/amd64`. If anonymous pulls are required, make the GHCR package public in the repository's package settings.

---

### Note
For `TeleCopy` to work, you need an `API_ID` and `API_HASH`.

You can get your own `API_ID` and `API_HASH` at [my.telegram.org/apps](https://my.telegram.org/auth?to=apps).

Log in with your Telegram number, choose an app name, and copy the credentials into `.env`.

Also, when setting `PHONE`, include your country code (e.g. `+12025551234`).

---
#### 🔎 Restarting Instructions

**Local:** activate the virtual environment and run the daemon:

```bash
source .venv/bin/activate
python main.py
```

**Docker:** after the one-time login, restart with `docker compose up -d`.
---

### ⚙️ Configuration Reference (`.env`)

| Variable | Required | Description |
|---|---|---|
| `PHONE` | ✅ | Your Telegram phone number with country code |
| `API_ID` | ✅ | From my.telegram.org/apps |
| `API_HASH` | ✅ | From my.telegram.org/apps |
| `DB_PASSWORD` | ✅ | Encryption key for the local TDLib database |
| `SOURCE` | ❌ | Built-in source chat ID (monitored when `DESTINATION` is also set) |
| `DESTINATION` | ❌ | Built-in destination chat ID, or default for `/watch` without destination |
| `BOT_TOKEN` | ❌ | Telegram Bot token for management commands |
| `BOT_ADMIN_IDS` | ❌ | Comma-separated Telegram user IDs allowed to use Bot commands |
| `FILES_DIRECTORY` | ❌ | Where TDLib stores downloaded media (default: `data/tdlib_files`) |
| `SEND_COPY` | ❌ | `true` strips "Forwarded from" header; `false` preserves it (default: `true`) |
| `PROXY_URL` | ❌ | SOCKS5 proxy for TDLib and the management Bot, e.g. `socks5://user:pass@host:port` |

---
### 🚧 Limitation
TeleCopy currently only runs on Linux-based operating systems because of:

```
module 'signal' has no attribute 'SIGQUIT'
```

This is a constraint of the underlying `python-telegram` / TDLib library on Windows.

### 🛠️ Workaround
Use **WSL** (Windows Subsystem for Linux) to run TeleCopy on Windows.

Once WSL is set up, follow the setup steps above inside the WSL terminal.

### ⛔ OpenSSL Error
```
ImportError: libssl.so.1.1: cannot open shared object file: No such file or directory
```

If you encounter this error, run:
```bash
wget http://nz2.archive.ubuntu.com/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2_amd64.deb
sudo dpkg -i libssl1.1_1.1.1f-1ubuntu2_amd64.deb
```
---

### 📦 Dependencies
`python-telegram`, `python-dotenv`, `tqdm`, `setuptools`

### 🤝 Contributions
Contributions, issues and feature requests are welcome!
Feel free to submit a PR or open an issue.

# Enjoy using TeleCopy! 🚀

