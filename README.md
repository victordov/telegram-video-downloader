# Telegram Video Downloader Bot

A powerful Telegram bot that automatically detects video links from popular social media platforms (YouTube, Instagram, Facebook, and TikTok) in chat messages, downloads the videos, and shares them back to the chat.

## Features

- **Multi-Platform Support**: Downloads videos from YouTube, Instagram, Facebook, and TikTok
- **Automatic Detection**: Automatically detects video URLs in messages
- **Works Everywhere**: Functions in both private chats and group chats
- **Smart Processing**: Only processes new messages after the bot is added to prevent spam
- **File Size Management**: Respects Telegram's 50MB file size limit
- **Error Handling**: Robust error handling with user-friendly messages
- **Easy Setup**: Simple configuration with environment variables

## Supported Platforms

| Platform | URL Formats Supported |
|----------|----------------------|
| **YouTube** | `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/` |
| **Instagram** | `instagram.com/p/`, `instagram.com/reel/`, `instagram.com/tv/` |
| **TikTok** | `tiktok.com/@user/video/`, `vm.tiktok.com/`, `tiktok.com/t/` |
| **Facebook** | `facebook.com/watch/`, `facebook.com/videos/`, `fb.watch/` |

## Requirements

- Python 3.9 or higher
- Internet connection
- Telegram Bot Token (from @BotFather)

## Installation

### 1. Clone or Download the Project

```bash
# If you have git installed
git clone <repository-url>
cd telegram_video_bot

# Or download and extract the files to a folder
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Start a chat with BotFather and send `/newbot`
3. Follow the instructions to create your bot:
   - Choose a name for your bot (e.g., "My Video Downloader")
   - Choose a username for your bot (must end with "bot", e.g., "myvideo_downloader_bot")
4. BotFather will give you a token that looks like: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
5. **Keep this token secure and private!**

### 4. Configure the Bot

1. Copy the example environment file:
   ```bash
   cp .env.example .env
   ```

2. Edit the `.env` file and add your bot token:
   ```
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```

3. **Important for Group Chats**: Disable Privacy Mode
   - Chat with @BotFather again
   - Send the command `/mybots`
   - Select your bot
   - Go to `Bot Settings` > `Group Privacy`
   - Select `Turn off`

   This step is **required** for the bot to see and process all messages in group chats.

### 5. Run the Bot

```bash
python run_bot.py
```

You should see:
```
🤖 Telegram Video Downloader Bot
========================================
🔍 Checking requirements...
✅ Requirements check passed!

🚀 Starting bot...
Starting Telegram Video Bot...
```

## Usage

### Adding the Bot to a Chat

1. **For Private Chats**: Search for your bot's username and start a chat
2. **For Group Chats**: Add the bot to the group as a member

### Bot Commands

- `/start` - Show welcome message and bot information
- `/help` - Display help and usage instructions  
- `/status` - Check bot status and statistics

### How It Works

1. **Send a message** containing a video URL from any supported platform
2. **The bot detects** the video link automatically
3. **Downloads the video** using yt-dlp
4. **Shares the video** back to the chat with metadata

### Example Usage

```
User: "Check out this amazing video! https://www.youtube.com/watch?v=dQw4w9WgXcQ"

Bot: "🔄 Downloading video from YouTube..."

Bot: [Sends video file with caption]
🎥 Never Gonna Give You Up

📱 Platform: Youtube
⏱️ Duration: 212s
📊 Size: 8.5MB
```

## Configuration Options

### Environment Variables

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather | Yes | None |
| `DOWNLOAD_DIR` | Directory to store temporary downloads | No | System temp directory |

### Advanced Configuration

You can modify the video downloader settings in `video_downloader.py`:

- **File size limits**: Change the `50M` limit in format strings
- **Video quality**: Modify format selection preferences
- **Platform-specific options**: Add custom yt-dlp options per platform

## Troubleshooting

### Common Issues

**Bot doesn't respond to commands:**
- Check that the bot token is correct in `.env`
- Ensure the bot is added to the chat
- Verify internet connection

**Bot doesn't work in group chats:**
- Make sure Privacy Mode is disabled (see "Configure the Bot" section)
- The bot can only process messages sent after it was added to the group
- Try mentioning the bot (@your_bot_username) in your message
- Ensure the bot has permission to send messages in the group

**Video download fails:**
- Some videos may be private or geo-restricted
- File size might exceed 50MB limit
- Platform may have changed their API (yt-dlp updates regularly)

**"Sign in to confirm you're not a bot" error:**
- This is a YouTube anti-bot measure
- The bot includes workarounds, but some videos may still fail
- Try with a different video URL

### Getting Help

1. Check the bot logs for error messages
2. Verify the video URL works in a web browser
3. Try with different video URLs to isolate the issue
4. Update yt-dlp: `pip install --upgrade yt-dlp`

## Development

### Project Structure

```
telegram_video_bot/
├── telegram_bot.py          # Main bot implementation
├── video_downloader.py      # Video downloading logic
├── run_bot.py              # Bot runner with setup checks
├── requirements.txt        # Python dependencies
├── .env.example           # Environment variables template
├── test_bot.py            # Bot functionality tests
├── test_integration.py    # Integration tests
└── README.md              # This file
```

### Running Tests

```bash
# Test URL detection and bot logic
python test_bot.py

# Test integration between components
python test_integration.py

# Test video downloader (requires internet)
python test_downloader.py
```

### Adding New Platforms

To add support for a new video platform:

1. Add URL patterns to `URL_PATTERNS` in `video_downloader.py`
2. Add platform-specific yt-dlp options if needed
3. Test with sample URLs
4. Update documentation

## Deployment

### Local Deployment

The bot runs locally on your machine. Keep the terminal/command prompt open while the bot is running.

### Server Deployment

For 24/7 operation, deploy to a server:

1. **VPS/Cloud Server**: Upload files and run with a process manager like `systemd` or `supervisor`
2. **Docker**: Create a Dockerfile for containerized deployment
3. **Heroku/Railway**: Use git-based deployment platforms

### Process Management

For production deployment, use a process manager:

```bash
# Using systemd (Linux)
sudo nano /etc/systemd/system/telegram-video-bot.service

# Using supervisor
sudo nano /etc/supervisor/conf.d/telegram-video-bot.conf

# Using PM2 (Node.js ecosystem)
pm2 start run_bot.py --interpreter python3
```

## Security Considerations

- **Keep your bot token private** - never commit it to version control
- **Use environment variables** for sensitive configuration
- **Monitor bot usage** to prevent abuse
- **Set up rate limiting** if deploying publicly
- **Regular updates** - keep yt-dlp and dependencies updated

## Limitations

- **File Size**: Maximum 50MB per video (Telegram limitation)
- **Platform Changes**: Video platforms may change APIs, requiring yt-dlp updates
- **Rate Limiting**: Some platforms may rate-limit requests
- **Geographic Restrictions**: Some videos may not be available in all regions
- **Private Content**: Cannot download private or restricted videos

## License

This project is provided as-is for educational and personal use. Respect the terms of service of video platforms and copyright laws when using this bot.

## Contributing

Contributions are welcome! Please:

1. Test your changes thoroughly
2. Update documentation as needed
3. Follow the existing code style
4. Add tests for new features

## Changelog

### Version 1.0.0
- Initial release
- Support for YouTube, Instagram, Facebook, TikTok
- Automatic URL detection
- Telegram bot integration
- Comprehensive error handling
- Full test suite

---

**Created by Manus AI** - A powerful video downloading bot for Telegram
