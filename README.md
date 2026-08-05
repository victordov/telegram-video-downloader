# Telegram Video Downloader Bot

A Telegram bot that detects video links from popular social platforms in chat messages, downloads them, and shares them back to the chat.

## Features

- **Multi-Platform Support**: YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG and VK
- **Automatic Detection**: Finds video URLs in message text, captions and hyperlinked text
- **Live Progress**: The status message shows a progress bar, speed and ETA while downloading
- **Concurrent**: Several downloads run in parallel without blocking other chats
- **Works Everywhere**: Private chats and group chats, with per-chat download rules
- **Smart Sending**: Playable video under 50MB, file attachment above it, with a poster frame
- **Threads Screenshots**: Non-video Threads posts come back as a screenshot
- **Robust**: Retries, timeouts, size guards and specific error messages for private, geo-blocked, rate-limited or removed content
- **Configurable**: Every limit and timeout is an environment variable

## Supported Platforms

| Platform | URL Formats Supported |
|----------|----------------------|
| **YouTube** | `youtube.com/watch?v=`, `youtu.be/`, `youtube.com/shorts/`, `youtube.com/live/`, `m.`/`music.` |
| **Instagram** | `instagram.com/p/`, `/reel/`, `/reels/`, `/tv/`, `/stories/`, `/share/` |
| **TikTok** | `tiktok.com/@user/video/`, `vm.tiktok.com/`, `vt.tiktok.com/`, `tiktok.com/t/` |
| **Facebook** | `facebook.com/watch/`, `/videos/`, `/reel/`, `fb.watch/` |
| **Twitter/X.com** | `twitter.com/username/status/`, `x.com/username/status/`, `t.co/` |
| **Threads** | `threads.com/@.../post/`, `threads.net/@.../post/` |
| **9GAG** | `9gag.com/gag/<post_id>` |
| **VK** | `vk.com/video…`, `vk.com/clip…`, `vkvideo.ru/video…` |

## Requirements

- Python 3.9 or higher
- Internet connection
- Telegram Bot Token (from @BotFather)
- **ffmpeg** (recommended) — enables merging separate video/audio streams for much
  better quality per megabyte, plus video thumbnails. Without it the bot falls back
  to single-stream downloads.
- Chrome + chromedriver (optional) — only needed for Threads screenshots

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

   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ```

3. (Optional) Set a custom download directory:

   ```env
   # Videos will be downloaded to this folder
   # If not set, uses the system temp directory
   DOWNLOAD_DIR=/Users/yourname/downloads/telegram-videos
   ```

4. (Optional) Add private chat IDs for size limit bypass:

   ```env
   # These chat IDs can download videos larger than 50MB
   PRIVATE_CHAT_IDS=123456789,987654321
   ```

5. **Important for Group Chats**: Disable Privacy Mode
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
- `/status` - Bot status, uptime, download counters and configuration
- `/dl <url>` - Force a download of a specific link (also works as a reply to a
  message containing a link, which is handy in groups)

### Download Rules

| Where | Behaviour |
|-------|-----------|
| Private chat | Every supported link is downloaded automatically |
| Group chat | Platforms in `GROUP_AUTO_PLATFORMS` (TikTok by default) download automatically |
| Group chat | Everything else needs the `#download` tag in the message, or `/dl` |

### How It Works

1. **Send a message** containing a video URL from any supported platform
2. **The bot detects** the video link automatically
3. **Downloads the video** using yt-dlp, editing a status message with live progress
4. **Shares the video** back to the chat with metadata, then deletes the status message
   and removes the temporary file

### Example Usage

```
User: "Check out this amazing video! https://www.youtube.com/watch?v=dQw4w9WgXcQ"

Bot: 🔎 Found a Youtube link…
Bot: ⬇️ Downloading from Youtube
     ███████░░░░░ 62%
     12.0 MB / 20.0 MB · 3.1 MB/s · ETA 0:12
Bot: 📤 Uploading 20.0 MB…

Bot: [Sends video with caption]
🎥 Never Gonna Give You Up

📱 Youtube · ⏱️ 3:32 · 📊 8.5MB
source
```

## Configuration Options

All settings live in `config.py` and are read from the environment (or `.env`).
See `.env.example` for the annotated full list.

### Core

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather (**required**) | — |
| `DOWNLOAD_DIR` | Where downloads are staged | `<temp>/telegram-video-bot` |
| `PRIVATE_CHAT_IDS` | Private chat IDs allowed to exceed `MAX_DOWNLOAD_SIZE_MB` | empty |
| `LOG_LEVEL` / `LOG_FILE` | Logging verbosity and optional log file | `INFO` / none |

### Sizes and uploads

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_API_BASE_URL` | Self-hosted Bot API server; the only way to upload above 50MB | none |
| `MAX_UPLOAD_SIZE_MB` | Hard upload ceiling | `50` (`2000` with a local API server) |
| `MAX_DOWNLOAD_SIZE_MB` | Reject videos estimated above this before downloading | `MAX_UPLOAD_SIZE_MB` |
| `MAX_VIDEO_HEIGHT` | Upper bound on requested video height | `1080` |

### Behaviour

| Variable | Description | Default |
|----------|-------------|---------|
| `DOWNLOAD_TAG` | Tag that opts a group message in | `#download` |
| `GROUP_AUTO_PLATFORMS` | Platforms downloaded in groups without the tag | `tiktok` |
| `HINT_ON_UNTAGGED_LINKS` | Reply with a hint when a group link is skipped | `false` |
| `REPLY_ON_UNSUPPORTED_LINKS` | Explain unsupported links in private chats | `true` |
| `GENERATE_THUMBNAILS` | Attach a poster frame (needs ffmpeg) | `true` |

### Performance

| Variable | Description | Default |
|----------|-------------|---------|
| `MAX_CONCURRENT_DOWNLOADS` | Downloads running at once across all chats | `3` |
| `CONCURRENT_FRAGMENTS` | Parallel fragment downloads for HLS/DASH | `4` |
| `DOWNLOAD_TIMEOUT_SECONDS` | Abandon a download after this long | `900` |
| `UPLOAD_TIMEOUT_SECONDS` | Abandon an upload attempt after this long | `300` |
| `MAX_UPLOAD_RETRIES` | Upload attempts before giving up | `3` |
| `PROGRESS_UPDATE_INTERVAL` | Minimum seconds between progress edits | `4` |

### Authentication

| Variable | Description |
|----------|-------------|
| `COOKIE_FILE` | Netscape cookie file handed to yt-dlp |
| `COOKIES_FROM_BROWSER` | Read cookies from a browser profile, e.g. `chrome` |
| `FFMPEG_LOCATION` | Explicit ffmpeg directory when it is not on `PATH` |

### Uploading files larger than 50MB

`api.telegram.org` refuses bot uploads above 50MB regardless of what the bot does.
To send bigger files, run a [local Bot API server](https://github.com/tdlib/telegram-bot-api)
and point the bot at it:

```env
TELEGRAM_API_BASE_URL=http://localhost:8081
PRIVATE_CHAT_IDS=123456789
```

`MAX_UPLOAD_SIZE_MB` then defaults to 2000 and the chats listed in
`PRIVATE_CHAT_IDS` may download videos above the normal limit.

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
├── config.py                       # All environment-driven settings
├── telegram_bot.py                 # Bot handlers, progress UI and sending
├── video_downloader.py             # yt-dlp wrapper, fallbacks and screenshots
├── run_bot.py                      # Runner with a pre-flight check
├── requirements.txt                # Runtime dependencies
├── requirements-dev.txt            # Runtime + test dependencies
├── pytest.ini / conftest.py        # Test configuration and fixtures
├── test_bot.py                     # Handlers, captions, sending, dedup
├── test_downloader.py              # Downloader units (yt-dlp stubbed)
├── test_integration.py             # Message in -> video out
├── test_video_detection.py         # Threads probe and screenshot plumbing
├── test_9gag_fallback.py           # Direct MP4 fallback
├── test_twitter_playlist.py        # Playlist/format-selection regressions
├── test_privileged_twitter_large.py# Privileged large-download regressions
├── test_simple.py                  # Live smoke tests (opt in)
└── README.md                       # This file
```

### Running Tests

```bash
pip install -r requirements-dev.txt

# The whole offline suite (no network, no browser)
pytest

# A single file, verbose
pytest test_bot.py -v

# Live smoke tests against the real platforms
RUN_NETWORK_TESTS=1 pytest -m network
```

The offline suite stubs out yt-dlp and the Telegram transport, so it runs in
well under a second and is safe to run in CI.

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

- **File Size**: `api.telegram.org` caps bot uploads at 50MB. Run a local Bot API
  server (`TELEGRAM_API_BASE_URL`) to raise this to 2GB; see the configuration
  section above.
- **Playable video vs file**: Telegram only renders files up to 50MB as playable
  video. Anything larger is sent as a document.
- **Platform Changes**: Video platforms may change APIs, requiring yt-dlp updates
- **Rate Limiting**: Some platforms may rate-limit requests
- **Geographic Restrictions**: Some videos may not be available in all regions
- **Private Content**: Cannot download private or restricted videos without cookies
  (`COOKIE_FILE` / `COOKIES_FROM_BROWSER`)

## License

This project is provided as-is for educational and personal use. Respect the terms of service of video platforms and copyright laws when using this bot.

## Contributing

Contributions are welcome! Please:

1. Test your changes thoroughly
2. Update documentation as needed
3. Follow the existing code style
4. Add tests for new features

## Changelog

### Version 2.0.0
- Downloads and screenshots run off the event loop, so one download no longer
  blocks every other chat; concurrency is bounded by `MAX_CONCURRENT_DOWNLOADS`
- Live download progress (bar, speed, ETA) in the status message
- Captions switched to HTML with escaping and truncation, fixing crashes on
  titles containing Markdown characters or exceeding Telegram's 1024 limit
- Message de-duplication keyed on `(chat_id, message_id)` with a bounded cache;
  previously a message ID in one chat could silently suppress another chat's
- Screenshots are now cleaned up and their status message removed
- Post-download size check, upload-ceiling check and specific error messages for
  private, removed, geo-blocked, rate-limited and live content
- ffmpeg-aware format selection (merged video+audio), generated thumbnails,
  collision-free filenames and startup cleanup of stale files
- Threads video detection replaced a headless-browser launch with an HTTP probe
- New `config.py` for all settings, `/dl` command, richer `/status`,
  local Bot API server support, cookie support
- Test suite converted to asserting pytest tests (150+ offline tests)

### Version 1.3.0
- Added support for VK videos and clips

### Version 1.1.0
- Added support for Twitter/X.com videos
- Updated documentation to reflect new platform support

### Version 1.2.0
- Added support for 9GAG post video URLs
- Added fallback extraction for direct MP4 links on 9GAG pages

### Version 1.0.0
- Initial release
- Support for YouTube, Instagram, Facebook, TikTok
- Automatic URL detection
- Telegram bot integration
- Comprehensive error handling
- Full test suite

---

**Created by Manus AI** - A powerful video downloading bot for Telegram
