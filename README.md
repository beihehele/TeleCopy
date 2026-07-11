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

- 📤 Copy **Past Messages** from one Telegram chat to another
- 📅 **Custom date-range** filtering for selective cloning
- 🔄 **Live Forwarding** of messages as they arrive
- ⚙️ Interactive **menu system** for configuration and actions
- 📁 Supports all media types and polls
- 💾 Automatically tracks copied messages to avoid duplicates
- 🧼 Resets session when API credentials change
- 📝 Structured logging to console and `telecopy.log`
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
```bash
python main.py
```

---

## 🐳 Docker Image

Tagged releases publish a Linux `amd64` Docker image to GitHub Container Registry:

```bash
docker pull ghcr.io/beihehele/telecopy:latest
```

Run TeleCopy with your `.env` file and persistent TDLib state.
Host directory `./data` is bind-mounted to `/app/data` so sessions and copy progress survive container restarts:

```bash
docker run --rm -it \
  --env-file .env \
  -v ./data:/app/data \
  ghcr.io/beihehele/telecopy:latest
```

Or use the included `docker-compose.yml`:

```yaml
services:
  telecopy:
    image: ghcr.io/beihehele/telecopy:latest
    env_file:
      - .env
    stdin_open: true
    tty: true
    volumes:
      - ./data:/app/data
```

Start an interactive session with:

```bash
docker compose run --rm telecopy
```

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
Each time you restart the terminal, activate the virtual environment first:
```bash
source .venv/bin/activate
python main.py
```
---
### 🔥 Main Menu
```
0. Connect to Telegram
1. Set source and destination
2. Copy full history
3. Live monitoring (auto-forward)
4. Copy by date range
5. Update API credentials
6. Advanced settings
7. Exit
```

---

### ⚙️ Configuration Reference (`.env`)

| Variable | Required | Description |
|---|---|---|
| `PHONE` | ✅ | Your Telegram phone number with country code |
| `API_ID` | ✅ | From my.telegram.org/apps |
| `API_HASH` | ✅ | From my.telegram.org/apps |
| `SOURCE` | ✅* | Source chat ID |
| `DESTINATION` | ✅* | Destination chat ID |
| `DB_PASSWORD` | ✅ | Encryption key for the local TDLib database |
| `FILES_DIRECTORY` | ❌ | Where TDLib stores downloaded media (default: `data/tdlib_files`) |
| `SEND_COPY` | ❌ | `true` strips "Forwarded from" header; `false` preserves it (default: `true`) |
| `PROXY_TYPE` | ❌ | `proxyTypeMtproto`, `proxyTypeHttp`, or `proxyTypeSocks5` |
| `PROXY_SERVER` | ❌ | Proxy hostname |
| `PROXY_PORT` | ❌ | Proxy port |

\* Set interactively via menu option 1 after connecting.

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

