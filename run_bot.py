#!/usr/bin/env python3
"""
Run the Telegram bot with a friendly pre-flight check and setup instructions.
"""

import os
import shutil
import sys
import tempfile

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


def acquire_instance_lock(token: str):
    """Take an exclusive lock keyed on the bot ID, or None if already held.

    Telegram allows only one getUpdates poller per token; a second local
    instance would knock the first one offline with endless Conflict errors.
    The lock is released automatically when the process exits.
    """
    try:
        import fcntl
    except ImportError:  # non-POSIX platform; skip the guard
        return True

    bot_id = token.split(':', 1)[0]
    lock_path = os.path.join(tempfile.gettempdir(), f'telegram-video-bot-{bot_id}.lock')
    handle = open(lock_path, 'w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main() -> None:
    """Main function with setup checks"""
    print("🤖 Telegram Video Downloader Bot")
    print("=" * 40)

    if not check_requirements():
        print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    lock = acquire_instance_lock(config.TELEGRAM_BOT_TOKEN)
    if lock is None:
        print("❌ Another instance of this bot is already running on this machine.")
        print("   Two pollers with one token fight over getUpdates (Telegram 'Conflict' errors).")
        print("   Stop the other instance first (e.g. pkill -f run_bot.py).")
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
