# Quick Start Guide

## 🚀 Get Your Bot Running in 5 Minutes

### Step 1: Get the Files
Download all the bot files to a folder on your computer.

### Step 2: Install Python Requirements
Open terminal/command prompt in the bot folder and run:
```bash
pip install -r requirements.txt
```

Install `ffmpeg` too — without it the bot can only grab single-stream (lower
quality) videos and cannot generate thumbnails:
```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian/Ubuntu
```

### Step 3: Create Your Bot
1. Open Telegram
2. Search for `@BotFather`
3. Send `/newbot`
4. Choose a name: "My Video Bot"
5. Choose username: "myvideo_bot" (must end with "bot")
6. Copy the token you receive

### Step 4: Configure
1. Copy `.env.example` to `.env`
2. Edit `.env` and paste your token:
   ```
   TELEGRAM_BOT_TOKEN=your_token_here
   ```

### Step 5: Run
```bash
python run_bot.py
```

### Step 6: Test
1. Find your bot in Telegram
2. Send: `/start`
3. Send a YouTube link
4. Watch the magic happen! 🎥

## ⚡ One-Line Setup (Advanced Users)

```bash
pip install -r requirements.txt && cp .env.example .env && echo "Edit .env with your token, then run: python run_bot.py"
```

## 🆘 Need Help?

- **Bot not responding?** Check your token in `.env`
- **Nothing happens in a group?** Turn off Group Privacy in @BotFather, and add
  `#download` to the message (or use `/dl <url>`)
- **Download fails?** Try a different video URL; `/status` shows whether ffmpeg is
  available
- **"Too large to upload"?** Telegram caps bot uploads at 50MB — see the local Bot
  API server section in README.md
- **Still stuck?** Check the full README.md

---

That's it! Your video downloader bot is ready to use. 🎉

