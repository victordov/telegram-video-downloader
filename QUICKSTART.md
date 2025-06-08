# Quick Start Guide

## 🚀 Get Your Bot Running in 5 Minutes

### Step 1: Get the Files
Download all the bot files to a folder on your computer.

### Step 2: Install Python Requirements
Open terminal/command prompt in the bot folder and run:
```bash
pip install -r requirements.txt
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
- **Download fails?** Try a different video URL
- **Still stuck?** Check the full README.md

---

That's it! Your video downloader bot is ready to use. 🎉

