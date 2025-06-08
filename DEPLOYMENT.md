# Deployment Guide

## Local Development

### Prerequisites
- Python 3.9+
- pip package manager
- Internet connection
- Telegram account

### Setup
1. Install dependencies: `pip install -r requirements.txt`
2. Configure bot token in `.env`
3. Run: `python run_bot.py`

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

#### Dockerfile
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Run the bot
CMD ["python", "run_bot.py"]
```

#### Docker Compose
```yaml
version: '3.8'

services:
  telegram-video-bot:
    build: .
    environment:
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
    volumes:
      - ./downloads:/app/downloads
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

Build and run:
```bash
docker-compose up -d
```

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
The bot includes built-in logging. For production, consider:

```python
# Enhanced logging configuration
import logging
from logging.handlers import RotatingFileHandler

# Setup rotating file handler
handler = RotatingFileHandler('bot.log', maxBytes=10*1024*1024, backupCount=5)
handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logging.getLogger().addHandler(handler)
```

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
- Backup bot configuration and logs
- Monitor disk space for downloads
- Set up automated restarts on failure

### Security Best Practices
- Use environment variables for secrets
- Run bot as non-root user
- Keep dependencies updated
- Monitor for unusual activity
- Implement rate limiting if needed

### Performance Optimization
- Clean up downloaded files regularly
- Monitor memory usage
- Use SSD storage for better I/O
- Consider multiple bot instances for high load

### Updates and Maintenance
```bash
# Update yt-dlp regularly
pip install --upgrade yt-dlp

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

