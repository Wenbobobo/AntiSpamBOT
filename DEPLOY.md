# JuryBot Deployment Guide

This document describes step-by-step commands for deploying JuryBot in two scenarios:
1. **Local demo on Windows (PowerShell + `uv`)**
2. **Server deployment on Debian (systemd service + optional webhook)**

Throughout the guide, replace placeholders such as `<BOT_TOKEN>` and `<CHAT_ID>` with real values.

---

## 1. Windows Demo (Long Polling)

### 1.1. Prerequisites
1. Install Python 3.12 (https://www.python.org/downloads/windows/). During setup, check **"Add python.exe to PATH"**.
2. Install [uv](https://github.com/astral-sh/uv):
   ```powershell
   powershell -ExecutionPolicy Bypass -Command "iwr https://astral.sh/install.ps1 -useb | iex"
   ```
3. Confirm versions:
   ```powershell
   python --version
   uv --version
   ```
### 1.2. Clone or open the project
If JuryBot is already on your machine, `cd` into it. Otherwise:
```powershell
git clone https://example.com/your/jurybot.git
cd jurybot
```

### 1.3. Install dependencies
```powershell
uv pip install -e .[test]
```
> uv automatically manages a virtual environment under `.venv/`.

If you need to inspect the environment:
```powershell
.\.venv\Scripts\python -m pip list
```

### 1.4. Configure secrets
Create `.env` (alongside `pyproject.toml`) with your bot token:
```powershell
@"
BOT_TOKEN=<BOT_TOKEN>
"@ | Out-File -Encoding utf8 .env
```

Copy the default config:
```powershell
Copy-Item config.example.toml config.toml
```
Edit `config.toml` to match your group needs (PowerShell `code .\config.toml` if using VS Code).
### 1.5. Run database migrations (automatic)
The first bot start creates `jurybot.db` automatically; no manual migration command is required.

### 1.6. Launch the bot (long polling)
```powershell
.\.venv\Scripts\python main.py --config config.toml
```

The bot keeps running until you stop the process (Ctrl+C). For unattended execution, wrap it in a PowerShell script:
```powershell
@"
Start-Transcript -Path logs\jurybot.log -Append
.\.venv\Scripts\python main.py --config config.toml
Stop-Transcript
"@ | Out-File -Encoding utf8 run.ps1

powershell -ExecutionPolicy Bypass -File .\run.ps1
```

### 1.7. Run tests
```powershell
.\.venv\Scripts\python -m pytest
```
---

## 2. Debian / Ubuntu Server Deployment

Assumptions:
- Target server runs Debian 12 or Ubuntu 22.04.
- Bot operates via long polling initially. (Webhook steps optional.)

### 2.1. Install system packages
```bash
sudo apt update
sudo apt install -y curl git python3.12 python3.12-venv pkg-config build-essential
```

### 2.2. Install uv (system-wide)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
uv --version
```

### 2.3. Create a service user (optional but recommended)
```bash
sudo useradd --system --create-home --shell /bin/bash jurybot
sudo su - jurybot
```

### 2.4. Fetch project files
```bash
git clone https://example.com/your/jurybot.git ~/jurybot
cd ~/jurybot
```
### 2.5. Install dependencies with uv
```bash
uv pip install -e .[test]
```

### 2.6. Configure environment
```bash
cat <<'EOF' > .env
BOT_TOKEN=<BOT_TOKEN>
EOF

cp config.example.toml config.toml
nano config.toml   # or vim config.toml
```

### 2.7. Test run manually
```bash
uv run python main.py --config config.toml
```
Verify the bot replies in Telegram, then stop with Ctrl+C.
### 2.8. Create a systemd service

1. Exit to root (if you switched users): `exit`
2. Create service file (as root):
   ```bash
   sudo tee /etc/systemd/system/jurybot.service >/dev/null <<'EOF'
   [Unit]
   Description=JuryBot Telegram anti-spam bot
   After=network-online.target

   [Service]
   Type=simple
   User=jurybot
   WorkingDirectory=/home/jurybot/jurybot
   ExecStart=/home/jurybot/.local/bin/uv run python main.py --config config.toml
   Restart=on-failure
   RestartSec=5
   Environment="PYTHONUNBUFFERED=1"

   [Install]
   WantedBy=multi-user.target
   EOF
   ```

3. Reload daemons and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now jurybot.service
   sudo systemctl status jurybot.service
   ```

4. View logs:
   ```bash
   journalctl -u jurybot.service -f
   ```
### 2.9. Optional: Webhook + Reverse Proxy

1. Obtain a public HTTPS endpoint (e.g., Cloudflare Tunnel, Nginx + Let’s Encrypt).
2. Configure Aiogram webhook by adding to `config.toml`:
   ```toml
   [bot]
   token_file = ".env"
   storage_url = "sqlite:///jurybot.db"
   log_level = "INFO"
   webhook_url = "https://example.com/jurybot/<token>"
   ```
   (Update application code to read `webhook_url` before switching from polling.)
3. Expose the bot via your proxy (instructions vary per provider).
---

## 3. Maintenance Commands

| Task | Command |
| --- | --- |
| Update dependencies | `uv pip install -e .[test]` |
| Run migrations (if schema changes) | The bot auto-migrates schema when starting; redeploy and restart. |
| Backup SQLite | `cp jurybot.db backups/jurybot-$(date +%Y%m%d).db` |
| Tail logs | Windows: `Get-Content logs\jurybot.log -Wait`; Linux: `journalctl -u jurybot -f` |
| Run tests | Windows: `.\.venv\Scripts\python -m pytest`; Linux: `uv run python -m pytest` |
---

## 4. Troubleshooting Tips

- **Bot starts but no responses**: Ensure the Telegram bot is added as admin in the group. Use `/start` in PM to check connectivity.
- **Rate limit errors**: Increase `vote_timeout_sec` or add `anti_flood_timeout` by slowing enforcement (see `jurybot/services/case.py`).
- **Permission errors on Linux**: Verify file ownership (`sudo chown -R jurybot:jurybot /home/jurybot/jurybot`).
- **Switch to Cloudflare D1/R2**: Update `storage_url` to the new adapter once implemented; keep `.env` secure and restart the service.

---

With these commands, you can reliably reproduce the deployment from local experiments to production servers. For future environments (e.g., Docker, Cloudflare Workers), derive provisioning scripts from the same steps above.
