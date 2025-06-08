"""
Telegram Bot for downloading and sharing videos from social media platforms
Supports YouTube, Instagram, Facebook, and TikTok
"""

import os
import logging
import re
from typing import Optional
from telegram import Update, Message
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters
from telegram.constants import ParseMode
from dotenv import load_dotenv
from video_downloader import VideoDownloader

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class TelegramVideoBot:
    """Telegram bot for downloading and sharing videos"""

    def __init__(self, token: str):
        """Initialize the bot

        Args:
            token: Telegram bot token
        """
        self.token = token
        self.downloader = VideoDownloader()
        self.application = None
        self.processed_messages = set()  # Track processed message IDs

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command"""
        welcome_message = """
🎥 *Video Downloader Bot*

I can download videos from:
• YouTube
• Instagram  
• Facebook
• TikTok

Just send me a message with a video link and I'll download and share it!
I work in both private chats and group chats!

*Note:* I only process new messages sent after I was added to the chat.
        """
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command"""
        help_message = """
🔧 *How to use:*

1. Send a message containing a video URL from:
   • YouTube (youtube.com, youtu.be)
   • Instagram (instagram.com)
   • Facebook (facebook.com, fb.watch)
   • TikTok (tiktok.com, vm.tiktok.com)

2. I'll automatically detect the video link and download it
3. The video will be shared back to the chat

*Works in:*
• Private chats
• Group chats

*Limitations:*
• Maximum file size: 50MB
• Only processes new messages (after bot was added)
• Supports video content only

*Commands:*
/start - Show welcome message
/help - Show this help message
/status - Check bot status
        """
        await update.message.reply_text(help_message, parse_mode=ParseMode.MARKDOWN)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command"""
        status_message = f"""
📊 *Bot Status*

✅ Bot is running
📁 Downloads processed: {len(self.processed_messages)}
🔧 Supported platforms: YouTube, Instagram, Facebook, TikTok
        """
        await update.message.reply_text(status_message, parse_mode=ParseMode.MARKDOWN)

    def extract_urls(self, text: str) -> list:
        """Extract URLs from message text

        Args:
            text: Message text to search

        Returns:
            List of URLs found in text
        """
        url_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        return re.findall(url_pattern, text)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages and process video URLs"""
        message = update.message

        # Get chat information
        chat_type = message.chat.type
        chat_id = message.chat.id
        chat_title = message.chat.title if message.chat.title else "Private Chat"

        # Log chat information
        logger.info(f"Received message from chat: {chat_title} (ID: {chat_id}, Type: {chat_type})")

        # Skip if no text content
        if not message.text:
            return

        # Skip if message was already processed
        if message.message_id in self.processed_messages:
            return

        # Mark message as processed
        self.processed_messages.add(message.message_id)

        # Log the message content based on chat type and content
        if chat_type == 'private':
            # In one-to-one chats, log every message
            logger.info(f"Message in private chat: {message.text}")

        # Extract URLs from message
        urls = self.extract_urls(message.text)

        if urls:
            # Check if any URL is from a supported platform
            supported_platform_urls = []
            for url in urls:
                platform = self.downloader.detect_platform(url)
                if platform:
                    supported_platform_urls.append((url, platform))
                    logger.info(f"Found {platform} URL in message: {url}")

            # Process supported platform URLs
            for url, platform in supported_platform_urls:
                await self.process_video_url(message, url)
        else:
            # If no URLs found, log the full message (except for private chats which are already logged)
            if chat_type != 'private':
                logger.info(f"Message without URLs: {message.text}")

    async def process_video_url(self, message: Message, url: str) -> None:
        """Process a video URL and send the downloaded video

        Args:
            message: Original message containing the URL
            url: Video URL to process
        """
        platform = self.downloader.detect_platform(url)

        # Log the URL being accessed
        chat_id = message.chat.id
        chat_title = message.chat.title if message.chat.title else "Private Chat"
        logger.info(f"Attempting to download video from URL: {url}")
        logger.info(f"Platform detected: {platform}")
        logger.info(f"Request from chat: {chat_title} (ID: {chat_id})")

        # Send processing message
        processing_msg = await message.reply_text(
            f"🔄 Downloading video from {platform.title()}...",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            # Download the video
            logger.info(f"Starting download process for URL: {url}")
            result = self.downloader.download_video(url)

            if isinstance(result, dict) and 'error' in result:
                # Handle specific error cases
                if result['error'] == 'tiktok_photo':
                    logger.warning(f"TikTok photo URL detected: {url}")
                    await processing_msg.edit_text(
                        f"❌ {result['message']} The bot can only download videos."
                    )
                else:
                    logger.warning(f"Failed to download video from URL: {url}")
                    await processing_msg.edit_text(
                        f"❌ {result.get('message', 'Failed to download video. The video might be private, too large (>50MB), or not supported.')}"
                    )
                return
            elif not result:
                logger.warning(f"Failed to download video from URL: {url}")
                await processing_msg.edit_text(
                    "❌ Failed to download video. The video might be private, too large (>50MB), or not supported."
                )
                return

            # Log successful download
            logger.info(f"Successfully downloaded video: {result['title']}")
            logger.info(f"File size: {result['filesize'] / (1024*1024):.1f}MB")
            logger.info(f"Duration: {result['duration']}s")
            logger.info(f"File path: {result['filepath']}")

            # Send the video file
            with open(result['filepath'], 'rb') as video_file:
                caption = f"🎥 *{result['title']}*\n\n📱 Platform: {result['platform'].title()}"
                if result['duration']:
                    caption += f"\n⏱️ Duration: {result['duration']}s"
                caption += f"\n📊 Size: {result['filesize'] / (1024*1024):.1f}MB"

                logger.info(f"Sending video to chat: {chat_title} (ID: {chat_id})")
                await message.reply_video(
                    video=video_file,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN
                )

            # Delete processing message
            await processing_msg.delete()

            # Clean up downloaded file
            logger.info(f"Cleaning up file: {result['filepath']}")
            self.downloader.cleanup_file(result['filepath'])

            logger.info(f"Successfully processed video: {result['title']} from {platform}")

        except Exception as e:
            logger.error(f"Error processing video {url}: {str(e)}")
            await processing_msg.edit_text(
                "❌ An error occurred while processing the video. Please try again later."
            )

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors"""
        logger.error(f"Update {update} caused error {context.error}")

    def run(self) -> None:
        """Start the bot"""
        # Create application
        self.application = Application.builder().token(self.token).build()

        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        # Handle text messages in private chats
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            self.handle_message
        ))

        # Handle text messages in group chats
        self.application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            self.handle_message
        ))

        # Add error handler
        self.application.add_error_handler(self.error_handler)

        # Start the bot
        logger.info("Starting Telegram Video Bot...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)


def main():
    """Main function to run the bot"""
    # Get bot token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")
        logger.error("Please create a .env file with your bot token:")
        logger.error("TELEGRAM_BOT_TOKEN=your_bot_token_here")
        return

    # Create and run bot
    bot = TelegramVideoBot(token)
    bot.run()


if __name__ == "__main__":
    main()
