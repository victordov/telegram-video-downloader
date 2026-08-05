#!/usr/bin/env python3
"""
Run the Telegram bot with a friendly pre-flight check and setup instructions.
"""

import os
import shutil
import sys

import config


SETUP_INSTRUCTIONS = """
💡 Setup Instructions:
1. Create a bot with @BotFather on Telegram
2. Copy the bot token
3. Create the .env file:  cp .env.example .env
4. Edit .env and add your token
5. Run this script again
"""


def check_requirements() -> bool:
    """Verify everything the bot needs before we try to start it."""
    print("🔍 Checking requirements...")
    ok = True

    if not os.path.exists('.env'):
        print("⚠️  No .env file found - relying on the ambient environment.")

    token = config.TELEGRAM_BOT_TOKEN
    if not token or token == 'your_bot_token_here':
        print("❌ TELEGRAM_BOT_TOKEN is not set properly!")
        ok = False
    elif ':' not in token:
        print("❌ TELEGRAM_BOT_TOKEN does not look like a bot token (expected '<id>:<secret>').")
        ok = False
    else:
        print(f"✅ Bot token loaded (…{token[-4:]})")

    print(f"✅ Download directory: {config.DOWNLOAD_DIR}")

    if shutil.which('ffmpeg'):
        print("✅ ffmpeg found - high quality merging and thumbnails enabled")
    else:
        print("⚠️  ffmpeg not found - falling back to single-stream downloads, no thumbnails")

    if config.USE_LOCAL_BOT_API:
        print(f"✅ Local Bot API server: {config.TELEGRAM_API_BASE_URL}")
    print(f"ℹ️  Max upload size: {config.MAX_UPLOAD_SIZE_MB}MB, "
          f"max concurrent downloads: {config.MAX_CONCURRENT_DOWNLOADS}")

    if ok:
        print("✅ Requirements check passed!")
    return ok


def main() -> None:
    """Main function with setup checks"""
    print("🤖 Telegram Video Downloader Bot")
    print("=" * 40)

    if not check_requirements():
        print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    print("\n🚀 Starting bot...")

    # Imported late so the requirements check runs before any heavy setup.
    from telegram_bot import TelegramVideoBot

    try:
        TelegramVideoBot(config.TELEGRAM_BOT_TOKEN).run()
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as exc:
        print(f"\n❌ Error starting bot: {exc}")
        print("💡 Make sure your bot token is valid and you have an internet connection")
        sys.exit(1)


if __name__ == "__main__":
    main()
