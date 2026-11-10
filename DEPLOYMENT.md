# Deployment Guide

## Local Development

### Prerequisites
- Python 3.9+
- pip package manager
- Internet connection
- Telegram account
- `ffmpeg` (strongly recommended: enables merged video+audio downloads and thumbnails)
- Chrome + chromedriver (optional: only used for Threads screenshots)

### Setup
1. Install dependencies: `pip install -r requirements.txt`
2. Install ffmpeg: `brew install ffmpeg` / `sudo apt install ffmpeg`
3. Configure bot token in `.env` (see `.env.example` for every setting)
4. Run: `python run_bot.py` — the pre-flight check reports what is missing
5. Run the tests: `pip install -r requirements-dev.txt && pytest`

## Production Deployment

### Option 1: VPS/Cloud Server

#### Ubuntu/Debian Server
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and pip
sudo apt install python3 python3-pip python3-venv -y

# Create user for bot
sudo useradd -m -s /bin/bash telegrambot
sudo su - telegrambot

# Setup bot
git clone <your-repo> telegram_video_bot
cd telegram_video_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env  # Add your bot token

# Test run
python run_bot.py
```

#### Systemd Service (Linux)
Create `/etc/systemd/system/telegram-video-bot.service`:

```ini
[Unit]
Description=Telegram Video Downloader Bot
After=network.target

[Service]
Type=simple
User=telegrambot
WorkingDirectory=/home/telegrambot/telegram_video_bot
Environment=PATH=/home/telegrambot/telegram_video_bot/venv/bin
ExecStart=/home/telegrambot/telegram_video_bot/venv/bin/python run_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-video-bot
sudo systemctl start telegram-video-bot
sudo systemctl status telegram-video-bot
```

### Option 2: Docker Deployment

A ready-to-use `Dockerfile` and `docker-compose.yml` ship with the repository.

```bash
cp .env.example .env   # add your token
docker compose up -d
docker compose logs -f
```

The compose file mounts `./downloads` as the staging directory and `./data`
for the SQLite database (file-ID cache, lifetime statistics, button links), so
nothing accumulates inside the container layer and the cache survives rebuilds.
ffmpeg is installed in the image, so merged downloads, thumbnails, shrinking
and M4A audio work.

The image's site-packages are read-only for the bot user, so yt-dlp cannot
update itself there (`YTDLP_AUTO_UPDATE` is off in the compose file). Rebuild
to pick up a new release: `docker compose build --pull && docker compose up -d`.
The bot warns the admins when its yt-dlp is older than `YTDLP_STALE_DAYS`.

### Option 3: Heroku Deployment

#### Procfile
```
worker: python run_bot.py
```

#### Deploy Steps
```bash
# Install Heroku CLI
# Login to Heroku
heroku login

# Create app
heroku create your-video-bot

# Set environment variables
heroku config:set TELEGRAM_BOT_TOKEN=your_token_here

# Deploy
git add .
git commit -m "Deploy bot"
git push heroku main

# Scale worker
heroku ps:scale worker=1
```

### Option 4: Railway Deployment

1. Connect your GitHub repository to Railway
2. Set environment variable: `TELEGRAM_BOT_TOKEN`
3. Railway will automatically deploy

### Option 5: DigitalOcean App Platform

#### app.yaml
```yaml
name: telegram-video-bot
services:
- name: worker
  source_dir: /
  github:
    repo: your-username/telegram-video-bot
    branch: main
  run_command: python run_bot.py
  environment_slug: python
  instance_count: 1
  instance_size_slug: basic-xxs
  envs:
  - key: TELEGRAM_BOT_TOKEN
    value: your_token_here
    type: SECRET
```

## Monitoring and Maintenance

### Logging
Logging is configured from the environment; no code changes are needed:

```env
LOG_LEVEL=INFO      # DEBUG, INFO, WARNING, ERROR
LOG_FILE=bot.log    # also write to a file (stdout is always used)
```

Under systemd or Docker, leave `LOG_FILE` unset and let journald/`docker logs`
handle rotation.

### Polling or webhooks
On a laptop, the default polling mode needs nothing. On a server with a public
HTTPS address, webhooks let Telegram push updates instead of the bot asking
for them every few seconds:

```env
WEBHOOK_URL=https://bot.example.com   # what Telegram calls
WEBHOOK_PORT=8443                     # where the bot listens
WEBHOOK_PATH=telegram                 # -> https://bot.example.com/telegram
```

Put a reverse proxy (Caddy, nginx, Traefik) with TLS in front of the port;
in Docker, uncomment the `ports` section of `docker-compose.yml`. Telegram
sends `WEBHOOK_SECRET` (derived from the token unless you set one) with every
update and the bot drops requests without it. Switching back to polling is just
removing `WEBHOOK_URL`: the bot deletes the webhook on startup.

### Access and abuse
Set `ALLOWED_CHAT_IDS` to the user and group IDs that may use the bot.
Members of an allowed group may also use it in their private chat and inline.
When anyone else tries it, the admins get an Allow / Ignore prompt; approved
chats are kept in the database, and `/access` lists them and undoes decisions.
`RATE_LIMIT_PER_HOUR` (default 20) caps how many downloads each user can
start; links resent from the cache are free.

To find a group's ID, add the bot to it and post a link: the prompt shows it.

### Logins (Reddit, Vimeo)
Set `COOKIES_FROM_BROWSER=chrome` (or `firefox`, `chrome:Profile 1`, ...) on a
machine where you are logged in to those sites. The bot reads the cookies once
every few hours and keeps them in memory. With Chrome on macOS the first read
asks for Keychain access; choose "Always Allow", or the prompt comes back.
`COOKIES_ONLY_WHEN_NEEDED` (default on) keeps the cookies away from sites that
work without a login. Docker containers cannot read a host browser: export a
`cookies.txt` and use `COOKIE_FILE` there.

### Alerts
Admins (`ADMIN_CHAT_IDS`, defaulting to `PRIVATE_CHAT_IDS`) get a message when
`ALERT_FAILURE_THRESHOLD` downloads in a row from one site fail for reasons
that point at the site (not private or deleted posts), and another when it
recovers. Unexpected errors are reported too, at most once per
`ALERT_COOLDOWN_MINUTES`.

### Uploading files larger than 50MB
`api.telegram.org` rejects bot uploads above 50MB. To lift that, run a
[local Bot API server](https://github.com/tdlib/telegram-bot-api) alongside the
bot and set:

```env
TELEGRAM_API_BASE_URL=http://localhost:8081
MAX_UPLOAD_SIZE_MB=2000
PRIVATE_CHAT_IDS=123456789
```

Without it, videos above 50MB are shrunk to fit (`COMPRESS_OVERSIZED`). Only
videos too long to stay watchable at 50MB are refused, with a clear message
rather than an opaque "Request Entity Too Large".

### Disk usage
Downloads are staged in `DOWNLOAD_DIR` and deleted after each send, success or
failure. On startup, files older than `CLEANUP_MAX_AGE_HOURS` (default 6) are
swept, which covers anything left behind by a crash or a hard restart.

### Health Checks
Add health check endpoint:

```python
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/health')
def health():
    return {'status': 'healthy', 'bot_running': True}

def run_health_server():
    app.run(host='0.0.0.0', port=8080)

# Start health server in background
threading.Thread(target=run_health_server, daemon=True).start()
```

### Monitoring Tools
- **Uptime monitoring**: UptimeRobot, Pingdom
- **Log aggregation**: ELK Stack, Grafana Loki
- **Metrics**: Prometheus + Grafana
- **Error tracking**: Sentry

### Backup and Recovery
- Back up `.env` and, if you care about the cache and lifetime statistics,
  `data/bot.sqlite3` (losing it only means links are downloaded again)
- Monitor disk space for downloads
- Set up automated restarts on failure

### Security Best Practices
- Use environment variables for secrets
- Run bot as non-root user
- Keep dependencies updated
- Set `ALLOWED_CHAT_IDS` and keep `RATE_LIMIT_PER_HOUR` on
- Never commit `data/` (it records which chats used the bot) or `.env`

### Performance Optimization
- Clean up downloaded files regularly
- Monitor memory usage
- Use SSD storage for better I/O
- Consider multiple bot instances for high load

### Updates and Maintenance
yt-dlp updates itself every `YTDLP_UPDATE_INTERVAL_HOURS` (weekly) when the
environment is writable, verifies the new copy imports (rolling back if not),
and restarts once active downloads finish. Admins can force it with `/update`.

```bash
# Update yt-dlp by hand
pip install --upgrade "yt-dlp[default]"

# Update all dependencies
pip install --upgrade -r requirements.txt

# Restart bot service
sudo systemctl restart telegram-video-bot
```

## Troubleshooting Production Issues

### Common Problems
1. **Bot stops responding**: Check logs, restart service
2. **High memory usage**: Clean download directory, restart bot
3. **Download failures**: Update yt-dlp, check platform changes
4. **Rate limiting**: Implement delays, use multiple IPs

### Log Analysis
```bash
# View recent logs
sudo journalctl -u telegram-video-bot -f

# Check error patterns
grep ERROR /var/log/telegram-video-bot.log

# Monitor resource usage
htop
df -h
```

### Emergency Procedures
1. Stop bot: `sudo systemctl stop telegram-video-bot`
2. Check disk space: `df -h`
3. Clean downloads: `rm -rf /tmp/downloads/*`
4. Restart: `sudo systemctl start telegram-video-bot`

---

Choose the deployment option that best fits your needs and technical expertise. For beginners, start with local development or a simple VPS deployment.

