"""
Telegram Video Downloader Bot
================================

A robust Telegram bot that downloads videos from multiple social media platforms:
- YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK

Features:
- Automatic platform detection
- Video size estimation and validation
- Smart file sending (video vs document based on size)
- Retry logic for failed uploads
- Comprehensive error handling
- Support for privileged chats to bypass size limits

Limitations:
- Telegram has a 20-second upload timeout that effectively limits
  practical file uploads to ~500MB despite API allowing up to 2GB
- Videos >50MB are sent as documents
- Privileged chats can bypass initial size check but still subject to timeout
"""

import asyncio
import logging
import os
import re
import sys
import tempfile
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from telegram import Update, Message
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, TimedOut
from video_downloader import VideoDownloader

# Load environment variables
load_dotenv()

# Configure logging with better formatting
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configure debug logging for specific modules
logging.getLogger('httpx').setLevel(logging.WARNING)  # Reduce noise from httpx
logging.getLogger('telegram.ext').setLevel(logging.WARNING)  # Reduce noise from telegram

# Constants
TELEGRAM_VIDEO_SIZE_LIMIT_MB = 50
TELEGRAM_DOCUMENT_SIZE_LIMIT_MB = 2000  # 2GB
UPLOAD_TIMEOUT_SECONDS = 20  # Telegram's hard timeout
MAX_RETRIES = 2

# Get private chat IDs from environment variable
PRIVATE_CHAT_IDS = os.getenv('PRIVATE_CHAT_IDS', '').split(',')
# Clean up the list (remove empty strings and whitespace)
PRIVATE_CHAT_IDS = [chat_id.strip() for chat_id in PRIVATE_CHAT_IDS if chat_id.strip()]
logger.info(f"Loaded {len(PRIVATE_CHAT_IDS)} private chat IDs from environment variables")

# Get download directory from environment variable
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR')
# If not set, use system temp directory
if not DOWNLOAD_DIR:
    DOWNLOAD_DIR = tempfile.gettempdir()
# Create directory if it doesn't exist
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logger.info(f"Download directory configured: {DOWNLOAD_DIR}")


class TelegramVideoBot:
    """Telegram bot for downloading and sharing videos from social media platforms"""
    
    def __init__(self, token: str):
        """Initialize the bot with token and downloader"""
        self.token = token
        self.downloader = VideoDownloader(download_dir=DOWNLOAD_DIR)
        logger.info("Bot instance created successfully")
        self.application = None
        self.processed_messages = set()  # Track processed message IDs

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        logger.info(f"Received /start command from user {user.id} in chat {chat_id} (type: {chat_type})")

        welcome_message = """
🎥 *Video Downloader Bot*

I can download videos from:
• YouTube
• Instagram  
• Facebook
• TikTok
• Twitter/X
• Threads (screenshots for non-video posts)
• 9GAG
• VK (vk.com)

In private chats: All videos are downloaded automatically!
In group chats: 
• TikTok videos are downloaded automatically
• For other platforms, add #download tag to your message

I work in both private chats and group chats!

*Note:* I only process new messages sent after I was added to the chat.
        """
        logger.info(f"Sending welcome message to user {user.id} in chat {chat_id}")
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        logger.info(f"Received /help command from user {user.id} in chat {chat_id} (type: {chat_type})")

        help_message = """
🔧 *How to use:*

1. Send a message containing a video URL from:
   • YouTube (youtube.com, youtu.be)
   • Instagram (instagram.com)
   • Facebook (facebook.com, fb.watch)
   • TikTok (tiktok.com, vm.tiktok.com)
   • Twitter/X (twitter.com, x.com, t.co)
   • Threads (threads.com, threads.net)
   • 9GAG (9gag.com/gag/...)
   • VK (vk.com, m.vk.com)

2. Video download behavior:
   • In private chats: All videos are downloaded automatically
   • In group chats:
     - TikTok videos are downloaded automatically
     - For other platforms (YouTube, Instagram, Facebook, Twitter/X, Threads, 9GAG, VK), add #download tag to your message

3. The video will be shared back to the chat
   • For Threads posts that are not videos, a screenshot will be taken and shared as a photo

*Works in:*
• Private chats
• Group chats

*Limitations:*
• Maximum file size: 50MB
• Only processes new messages (after bot was added)
• Supports video content and screenshots for Threads posts

*Commands:*
/start - Show welcome message
/help - Show this help message
/status - Check bot status
        """
        logger.info(f"Sending help message to user {user.id} in chat {chat_id}")
        await update.message.reply_text(help_message, parse_mode=ParseMode.MARKDOWN)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command"""
        user = update.effective_user
        chat_id = update.effective_chat.id
        chat_type = update.effective_chat.type
        logger.info(f"Received /status command from user {user.id} in chat {chat_id} (type: {chat_type})")

        status_message = f"""
📊 *Bot Status*

✅ Bot is running
📁 Downloads processed: {len(self.processed_messages)}
🔧 Supported platforms: YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK
        """
        logger.info(f"Sending status message to user {user.id} in chat {chat_id} (downloads processed: {len(self.processed_messages)})")
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
        
        # Skip if no message (e.g., edited message, channel post, etc.)
        if not message:
            return

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

        # Check if message contains the download tag for non-TikTok videos
        has_download_tag = "#download" in message.text.lower()

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

            # Process supported platform URLs based on platform, tag, and chat type
            for url, platform in supported_platform_urls:
                # Always download videos in private chats, TikTok videos in any chat, or videos with the download tag
                if chat_type == 'private' or platform == 'tiktok' or has_download_tag:
                    await self.process_video_url(message, url)
                else:
                    logger.info(f"Skipping {platform} URL: {url} - No download tag found in group chat")
                    # await message.reply_text(
                    #     f"Found {platform.title()} video link. Add #download tag to download this video.",
                    #     parse_mode=ParseMode.MARKDOWN
                    # )
        else:
            # If no URLs found, log the full message (except for private chats which are already logged)
            if chat_type != 'private':
                logger.info(f"Message without URLs: {message.text}")

    async def process_video_url(self, message: Message, url: str) -> None:
        """Process a URL and send the downloaded video or screenshot

        Args:
            message: Original message containing the URL
            url: URL to process (video URL or Threads post URL)
        """
        platform = self.downloader.detect_platform(url)

        # Log the URL being accessed
        chat_id = message.chat.id
        chat_title = message.chat.title if message.chat.title else "Private Chat"
        logger.info(f"Processing content from URL: {url}")
        logger.info(f"Platform detected: {platform}")
        logger.info(f"Request from chat: {chat_title} (ID: {chat_id})")

        # Send processing message
        processing_text = f"🔄 Processing content from {platform.title()}..."

        processing_msg = await message.reply_text(
            processing_text,
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            # Check if the URL contains a video
            logger.info(f"Checking if URL contains a video: {url}")
            has_video = self.downloader.check_for_video(url)

            if has_video:
                # If it contains a video, download it
                logger.info(f"URL contains a video, downloading: {url}")
                await processing_msg.edit_text(
                    f"🔄 Downloading video from {platform.title()}...",
                    parse_mode=ParseMode.MARKDOWN
                )
                result = self.downloader.download_video(url)
            else:
                # If it doesn't contain a video, take a screenshot
                logger.info(f"URL does not contain a video, taking screenshot: {url}")
                await processing_msg.edit_text(
                    f"🔄 Taking screenshot of {platform.title()} post...",
                    parse_mode=ParseMode.MARKDOWN
                )
                result = self.downloader.take_screenshot(url)

            if isinstance(result, dict) and 'error' in result:
                # Handle specific error cases
                if result['error'] == 'tiktok_photo':
                    logger.warning(f"TikTok photo URL detected: {url}")
                    await processing_msg.edit_text(
                        f"❌ {result['message']} The bot can only download videos."
                    )
                elif result['error'] == 'file_too_large':
                    filesize_mb = result['filesize'] / (1024*1024)
                    logger.warning(f"Video too large to download: {filesize_mb:.1f}MB (limit: 50MB), URL: {url}")
                    logger.info(f"Large video details - Title: {result.get('title', 'Unknown')}, Platform: {result.get('platform', 'Unknown')}")
                    
                    # Special case for private chats with IDs in the PRIVATE_CHAT_IDS list
                    if str(chat_id) in PRIVATE_CHAT_IDS and message.chat.type == 'private':
                        # For Twitter, try to get a better file size estimate
                        if result.get('platform') == 'twitter' and filesize_mb > 100:
                            # Estimate based on duration (more accurate for Twitter)
                            duration = result.get('duration', 0)
                            if duration:
                                # Based on actual Twitter downloads: ~8 MB per minute
                                estimated_size_mb = duration * 8 / 60
                                filesize_mb = estimated_size_mb
                                logger.info(f"Corrected Twitter video size estimate: {filesize_mb:.1f}MB")
                        
                        logger.info(f"Special case: Downloading large video for privileged chat ID {chat_id} despite size: {filesize_mb:.1f}MB")
                        await processing_msg.edit_text(
                            f"🔄 Special download: Large video detected ({filesize_mb:.1f}MB). Downloading anyway...",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        
                        try:
                            result = self.downloader.download_video(url, ignore_size_limit=True)
                            if not result or (isinstance(result, dict) and 'error' in result):
                                error_text = result.get('message') if isinstance(result, dict) else None
                                await processing_msg.edit_text(
                                    "❌ Failed to download large video despite special permission."
                                    + (f"\n\n{error_text}" if error_text else "")
                                )
                                return

                            logger.info(
                                f"Special download complete - File: {result['filepath']}, "
                                f"Size: {result['filesize'] / (1024*1024):.1f}MB"
                            )
                        except Exception as e:
                            logger.error(f"Error in special download: {str(e)}")
                            await processing_msg.edit_text(
                                f"❌ Error downloading large video: {str(e)}"
                            )
                            return
                        
                        # Skip the rest of the error handling and continue to send the video
                        # This is crucial to ensure the video is sent to the chat
                    else:
                        # Normal case for other chats - show error message
                        await processing_msg.edit_text(
                            f"❌ {result['message']} Video size: {filesize_mb:.1f}MB (limit: 50MB)"
                        )
                        return
                elif result['error'] == 'registered_users_only':
                    logger.warning(f"Content only available for registered users: {url}")
                    logger.info(f"Platform: {result.get('platform', 'Unknown')}")
                    await processing_msg.edit_text(
                        f"❌ {result['message']}"
                    )
                    return
                elif result['error'] == 'threads_screenshot_failed':
                    logger.warning(f"Failed to take screenshot of Threads post: {url}")
                    logger.info(f"Platform: {result.get('platform', 'Unknown')}")
                    await processing_msg.edit_text(
                        f"❌ {result['message']}"
                    )
                    return
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
            logger.info(f"Successfully downloaded content: {result['title']}")
            logger.info(f"File size: {result['filesize'] / (1024*1024):.1f}MB")
            logger.info(f"File path: {result['filepath']}")

            # Check if this is a screenshot or a video
            is_screenshot = result.get('is_screenshot', False)

            if is_screenshot:
                # Send the screenshot as a photo
                with open(result['filepath'], 'rb') as photo_file:
                    caption = f"📸 *{result['title']}*\n\n📱 Platform: {result['platform'].title()}"
                    caption += f"\n📊 Size: {result['filesize'] / (1024*1024):.1f}MB"

                    logger.info(f"Sending screenshot to chat: {chat_title} (ID: {chat_id})")
                    await message.reply_photo(
                        photo=photo_file,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                # Log video duration
                logger.info(f"Duration: {result.get('duration', 'Unknown')}s")

                # Send the video file
                logger.info(f"Sending video to chat: {chat_title} (ID: {chat_id})")
                
                # Send video with retry logic
                success = await self._send_video_with_retry(message, result, processing_msg)
                
                if success:
                    # Delete processing message
                    await processing_msg.delete()
                    
                    # Clean up downloaded file
                    logger.info(f"Cleaning up file: {result['filepath']}")
                    self.downloader.cleanup_file(result['filepath'])
                    
                    logger.info(f"Successfully processed video: {result['title']} from {platform}")
                else:
                    # Still clean up on failure
                    logger.info(f"Cleaning up file after failed send: {result['filepath']}")
                    self.downloader.cleanup_file(result['filepath'])

        except Exception as e:
            logger.error(f"Error processing video {url}: {str(e)}")
            await processing_msg.edit_text(
                "❌ An error occurred while processing the video. Please try again later."
            )

    async def _send_video_with_retry(self, message: Message, result: Dict[str, Any], processing_msg) -> bool:
        """Send video to chat with retry logic and proper error handling"""
        file_size_mb = result['filesize'] / (1024 * 1024)
        
        # Determine send method based on file size
        if file_size_mb > TELEGRAM_VIDEO_SIZE_LIMIT_MB:
            return await self._send_as_document(message, result, processing_msg, file_size_mb)
        else:
            return await self._send_as_video(message, result, processing_msg, file_size_mb)
    
    async def _send_as_document(self, message: Message, result: Dict[str, Any], processing_msg, file_size_mb: float) -> bool:
        """Send file as document with proper error handling"""
        caption = self._create_caption(result, file_size_mb, is_document=True)
        
        logger.info(f"Attempting to send document: {file_size_mb:.1f}MB")
        
        for attempt in range(MAX_RETRIES):
            try:
                # Check if file is too large for practical upload
                if file_size_mb > 500:  # Files over 500MB likely won't upload within 20s timeout
                    logger.warning(f"File {file_size_mb:.1f}MB exceeds practical upload limit")
                    await processing_msg.edit_text(
                        f"⚠️ File too large ({file_size_mb:.1f}MB). Telegram's 20-second timeout prevents uploading files >500MB.\n\n"
                        f"Consider downloading a smaller video."
                    )
                    return False
                
                # Try to send with short timeout
                await asyncio.wait_for(
                    message.reply_document(
                        document=result['filepath'],
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN
                    ),
                    timeout=UPLOAD_TIMEOUT_SECONDS
                )
                logger.info(f"Document sent successfully on attempt {attempt + 1}")
                return True
                
            except asyncio.TimeoutError:
                logger.warning(f"Upload attempt {attempt + 1} timed out after {UPLOAD_TIMEOUT_SECONDS}s")
                if attempt == MAX_RETRIES - 1:
                    await processing_msg.edit_text(
                        f"❌ Upload failed after {MAX_RETRIES} attempts. File too large for Telegram's servers.\n\n"
                        f"Try a smaller video (<500MB)."
                    )
                    return False
                await asyncio.sleep(1)  # Brief delay before retry
                
            except TimedOut as e:
                logger.error(f"Telegram API timeout: {str(e)}")
                await processing_msg.edit_text(
                    f"❌ Telegram server timeout. The file ({file_size_mb:.1f}MB) is too large.\n\n"
                    f"Try a smaller video."
                )
                return False
                
            except Exception as e:
                logger.error(f"Unexpected error sending document: {str(e)}", exc_info=True)
                if attempt == MAX_RETRIES - 1:
                    await processing_msg.edit_text(f"❌ Failed to send file: {str(e)}")
                    return False
                await asyncio.sleep(1)
        
        return False
    
    async def _send_as_video(self, message: Message, result: Dict[str, Any], processing_msg, file_size_mb: float) -> bool:
        """Send file as video with proper parameters"""
        caption = self._create_caption(result, file_size_mb, is_document=False)
        
        # Prepare video parameters
        send_kwargs = {
            'video': result['filepath'],
            'caption': caption,
            'parse_mode': ParseMode.MARKDOWN,
            'supports_streaming': True
        }
        
        # Add duration if available
        if result.get('duration'):
            send_kwargs['duration'] = int(result['duration'])
        
        # Add dimensions if available
        if result.get('width') and result.get('height'):
            try:
                send_kwargs['width'] = int(result['width'])
                send_kwargs['height'] = int(result['height'])
            except (ValueError, TypeError):
                logger.warning("Invalid video dimensions, skipping")
        
        try:
            await message.reply_video(**send_kwargs)
            logger.info(f"Video sent successfully: {file_size_mb:.1f}MB")
            return True
        except Exception as e:
            logger.error(f"Error sending video: {str(e)}", exc_info=True)
            await processing_msg.edit_text(f"❌ Failed to send video: {str(e)}")
            return False
    
    def _create_caption(self, result: Dict[str, Any], file_size_mb: float, is_document: bool = False) -> str:
        """Create formatted caption for video/document"""
        icon = "📁" if is_document else "🎥"
        caption = f"{icon} *{result['title']}*\n\n📱 Platform: {result['platform'].title()}"
        
        if result.get('duration'):
            minutes = int(result['duration']) // 60
            seconds = int(result['duration']) % 60
            caption += f"\n⏱️ Duration: {minutes}:{seconds:02d}"
        
        caption += f"\n📊 Size: {file_size_mb:.1f}MB"
        
        if is_document:
            caption += "\n\n⚠️ Sent as document due to large size"
        
        return caption

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors with detailed logging"""
        update_id = update.update_id if update else "N/A"
        logger.error(f"Error in update {update_id}: {context.error}", exc_info=True)
        
        # Extract additional error details
        error = context.error
        if isinstance(error, TelegramError):
            logger.error(f"Telegram error details: {error.to_dict() if hasattr(error, 'to_dict') else str(error)}")
        
        # Try to notify user if possible
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ An unexpected error occurred. Please try again later."
                )
            except Exception:
                logger.warning("Could not send error message to user")

    def run(self) -> None:
        """Start the bot with proper configuration"""
        logger.info("Initializing Telegram Video Bot...")
        
        try:
            # Create application with optimized settings
            self.application = (
                Application.builder()
                .token(self.token)
                .connect_timeout(60)
                .read_timeout(600)  # 10 minutes for downloads
                .write_timeout(600)  # 10 minutes for uploads
                .pool_timeout(60)
                .get_updates_connect_timeout(60)
                .get_updates_read_timeout(60)
                .get_updates_write_timeout(60)
                .connection_pool_size(8)
                .build()
            )
            logger.info("Application created with optimized timeouts")
            
            # Register handlers
            self._register_handlers()
            
            # Start the bot
            logger.info("Starting bot polling...")
            self.application.run_polling(drop_pending_updates=True)
            
        except Exception as e:
            logger.error(f"Failed to start bot: {str(e)}", exc_info=True)
            raise
        finally:
            logger.info("Bot shutdown complete")
    
    def _register_handlers(self) -> None:
        """Register all command and message handlers"""
        logger.info("Registering handlers...")
        
        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        
        # Message handler for all text messages
        self.application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        
        # Error handler
        self.application.add_error_handler(self.error_handler)
        
        logger.info("All handlers registered successfully")

def main():
    """Main entry point for the bot"""
    try:
        # Get and validate bot token
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN environment variable not set")
            sys.exit(1)
        
        # Create and run bot
        logger.info("Creating bot instance...")
        bot = TelegramVideoBot(token)
        
        logger.info("Starting bot...")
        bot.run()
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error in main: {str(e)}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
