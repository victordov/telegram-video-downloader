"""
Telegram Video Downloader Bot
================================

A robust Telegram bot that downloads videos from multiple social media platforms:
- YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK

Features:
- Automatic platform detection
- Live download progress in the status message
- Video size estimation and validation
- Smart file sending (video vs document based on size)
- Retry logic for failed uploads
- Comprehensive error handling
- Support for privileged chats to bypass size limits

Limitations:
- api.telegram.org caps bot uploads at 50MB. Point TELEGRAM_API_BASE_URL at a
  self-hosted Bot API server to raise that to 2GB.
- Videos above 50MB are sent as documents rather than playable videos.
"""

import asyncio
import html
import logging
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx  # a hard dependency of python-telegram-bot
from telegram import BotCommand, Message, MessageEntity, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

import config
from video_downloader import ERROR_MESSAGES, MB, VideoDownloader

config.configure_logging()
logger = logging.getLogger(__name__)

# Constants kept at module level for backwards compatibility with existing tests
# and scripts that import them directly.
TELEGRAM_VIDEO_SIZE_LIMIT_MB = config.TELEGRAM_VIDEO_SIZE_LIMIT_MB
MAX_UPLOAD_SIZE_MB = config.MAX_UPLOAD_SIZE_MB
UPLOAD_TIMEOUT_SECONDS = config.UPLOAD_TIMEOUT_SECONDS
MAX_RETRIES = config.MAX_UPLOAD_RETRIES
PRIVATE_CHAT_IDS = config.PRIVATE_CHAT_IDS
DOWNLOAD_DIR = config.DOWNLOAD_DIR

logger.info("Loaded %d privileged chat IDs from environment", len(PRIVATE_CHAT_IDS))
logger.info("Configuration: %s", config.describe())

SUPPORTED_PLATFORMS_TEXT = (
    "YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK"
)

#: Greedy enough to catch the whole URL, then trimmed of trailing punctuation.
URL_REGEX = re.compile(r'https?://[^\s<>"\'`\\^{}|\[\]]+', re.IGNORECASE)

#: Punctuation that is almost always sentence punctuation rather than URL content.
_TRAILING_PUNCTUATION = '.,;:!?\'")]}>'

#: Errors that will never succeed on a retry.
_FATAL_SEND_ERRORS = (BadRequest, Forbidden)

#: Timeouts sent to Telegram alongside a file. Uploading a video is one long
#: write followed by a wait while Telegram ingests it, so both halves need
#: headroom. Crucially, python-telegram-bot applies `media_write_timeout`
#: (default: 20 seconds!) instead of `write_timeout` to any request carrying a
#: file, so passing write_timeout explicitly here is what actually takes effect.
MEDIA_TIMEOUTS = {
    'write_timeout': config.UPLOAD_TIMEOUT_SECONDS,
    'read_timeout': config.UPLOAD_TIMEOUT_SECONDS,
    'connect_timeout': 60,
}


def upload_retry_is_safe(error: BaseException) -> bool:
    """Whether re-sending after this error could post the same media twice.

    A connect/write/pool timeout means the body never fully reached Telegram, so
    a retry is safe. A read timeout means we sent everything and gave up waiting
    for the reply — Telegram may already have posted the message, and retrying
    would duplicate it.
    """
    return not isinstance(error.__cause__, httpx.ReadTimeout)


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------

def esc(value: Any) -> str:
    """HTML-escape a value for Telegram's HTML parse mode.

    Captions are built from user-controlled video titles. Markdown would break on
    a stray ``*`` or ``_``; HTML only needs ``&``, ``<`` and ``>`` escaped.
    """
    return html.escape(str(value), quote=False)


def esc_attr(value: Any) -> str:
    """HTML-escape a value destined for an attribute such as ``href``."""
    return html.escape(str(value), quote=True)


def format_size(num_bytes: Optional[float]) -> str:
    """Render a byte count as a compact human readable string."""
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if num_bytes < 1024 or unit == 'TB':
            precision = 0 if unit in ('B', 'KB') else 1
            return f"{num_bytes:.{precision}f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def format_duration(seconds: Optional[float]) -> Optional[str]:
    """Render seconds as m:ss, or h:mm:ss for anything over an hour."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_bar(percent: Optional[float], width: int = 12) -> str:
    """Render a text progress bar; returns an indeterminate bar when unknown."""
    if percent is None:
        return '▪' * width
    filled = max(0, min(width, int(round(percent / 100 * width))))
    return '█' * filled + '░' * (width - filled)


def render_progress(platform: str, progress: Dict[str, Any]) -> str:
    """Build the body of the "downloading" status message."""
    percent = progress.get('percent')
    total = progress.get('total_bytes')
    downloaded = progress.get('downloaded_bytes') or 0
    speed = progress.get('speed')
    eta = progress.get('eta')

    lines = [f"⬇️ Downloading from <b>{esc(platform.title())}</b>"]

    if percent is not None:
        lines.append(f"<code>{progress_bar(percent)}</code> {percent:.0f}%")
    else:
        lines.append(f"<code>{progress_bar(None)}</code>")

    details = [format_size(downloaded) + (f" / {format_size(total)}" if total else "")]
    if speed:
        details.append(f"{format_size(speed)}/s")
    eta_text = format_duration(eta)
    if eta_text:
        details.append(f"ETA {eta_text}")
    lines.append("<i>" + esc(" · ".join(details)) + "</i>")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Progress bridge
# ----------------------------------------------------------------------------

class ProgressReporter:
    """Throttled bridge from yt-dlp's download thread to a Telegram message.

    yt-dlp calls the progress hook from the worker thread many times a second;
    Telegram rate limits message edits. Calls are dropped unless at least
    ``interval`` seconds have passed and the rendered text actually changed.
    """

    def __init__(self, bot: 'TelegramVideoBot', message: Message,
                 loop: asyncio.AbstractEventLoop, platform: str,
                 interval: float = config.PROGRESS_UPDATE_INTERVAL):
        self._bot = bot
        self._message = message
        self._loop = loop
        self._platform = platform
        self._interval = interval
        self._lock = threading.Lock()
        self._last_edit = 0.0
        self._last_text = ''

    def __call__(self, progress: Dict[str, Any]) -> None:
        if progress.get('status') != 'downloading':
            return

        now = time.monotonic()
        with self._lock:
            # Cheap check first: this runs several times a second per download.
            if now - self._last_edit < self._interval:
                return
            text = render_progress(self._platform, progress)
            if text == self._last_text:
                return
            self._last_edit = now
            self._last_text = text

        if self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._bot.safe_edit(self._message, text), self._loop
            )
        except RuntimeError:
            # Loop shut down mid-download; nothing useful left to report.
            pass


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

@dataclass
class BotStats:
    """Counters backing the /status command."""
    started_at: float = field(default_factory=time.time)
    succeeded: int = 0
    failed: int = 0
    bytes_sent: int = 0
    active: int = 0
    per_platform: Dict[str, int] = field(default_factory=dict)

    def record_success(self, platform: str, size: int) -> None:
        self.succeeded += 1
        self.bytes_sent += max(0, size)
        self.per_platform[platform] = self.per_platform.get(platform, 0) + 1

    def record_failure(self) -> None:
        self.failed += 1

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at


# ----------------------------------------------------------------------------
# Bot
# ----------------------------------------------------------------------------

class TelegramVideoBot:
    """Telegram bot for downloading and sharing videos from social media platforms"""

    def __init__(self, token: str):
        """Initialize the bot with token and downloader"""
        self.token = token
        self.downloader = VideoDownloader(
            download_dir=config.DOWNLOAD_DIR,
            max_filesize_mb=config.MAX_DOWNLOAD_SIZE_MB,
            max_video_height=config.MAX_VIDEO_HEIGHT,
            cookie_file=config.COOKIE_FILE,
            cookies_from_browser=config.COOKIES_FROM_BROWSER,
            ffmpeg_location=config.FFMPEG_LOCATION,
            concurrent_fragments=config.CONCURRENT_FRAGMENTS,
            socket_timeout=config.SOCKET_TIMEOUT_SECONDS,
            retries=config.DOWNLOAD_RETRIES,
        )
        self.application: Optional[Application] = None
        self.stats = BotStats()

        # (chat_id, message_id) -> None, ordered so the oldest entries can be
        # evicted. Message IDs are only unique per chat, so keying on the ID
        # alone silently dropped messages that collided across chats.
        self.processed_messages: 'OrderedDict[Tuple[int, int], None]' = OrderedDict()

        self._semaphore: Optional[asyncio.Semaphore] = None
        logger.info("Bot instance created successfully")

    # -- infrastructure -------------------------------------------------

    def download_semaphore(self) -> asyncio.Semaphore:
        """Lazily create the download semaphore bound to the running loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
        return self._semaphore

    def remember_message(self, chat_id: int, message_id: int) -> bool:
        """Record a message as processed. Returns False if it was already seen."""
        key = (chat_id, message_id)
        if key in self.processed_messages:
            return False
        self.processed_messages[key] = None
        while len(self.processed_messages) > config.PROCESSED_MESSAGE_CACHE_SIZE:
            self.processed_messages.popitem(last=False)
        return True

    @staticmethod
    async def safe_edit(message: Message, text: str) -> bool:
        """Edit a message, swallowing the errors that are not worth surfacing."""
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            return True
        except BadRequest as exc:
            if 'not modified' not in str(exc).lower():
                logger.debug("Could not edit status message: %s", exc)
        except RetryAfter as exc:
            logger.debug("Edit rate limited, skipping update (retry after %ss)", exc.retry_after)
        except TelegramError as exc:
            logger.debug("Could not edit status message: %s", exc)
        return False

    @staticmethod
    async def safe_delete(message: Optional[Message]) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except TelegramError as exc:
            logger.debug("Could not delete status message: %s", exc)

    def is_privileged(self, message: Message) -> bool:
        """Privileged private chats may exceed the normal download size limit."""
        return (
            message.chat.type == 'private'
            and str(message.chat.id) in PRIVATE_CHAT_IDS
        )

    # -- commands -------------------------------------------------------

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command"""
        message = update.effective_message
        if not message:
            return

        tag = esc(config.DOWNLOAD_TAG)
        auto = ', '.join(sorted(p.title() for p in config.GROUP_AUTO_PLATFORMS)) or 'none'
        welcome_message = (
            "🎥 <b>Video Downloader Bot</b>\n\n"
            "Send me a link and I'll fetch the video.\n\n"
            "<b>Supported platforms</b>\n"
            "• YouTube\n• Instagram\n• Facebook\n• TikTok\n• Twitter/X\n"
            "• Threads <i>(screenshot for non-video posts)</i>\n• 9GAG\n• VK\n\n"
            "<b>How it works</b>\n"
            f"• <b>Private chats:</b> every supported link is downloaded automatically\n"
            f"• <b>Groups:</b> {esc(auto)} downloads automatically, everything else "
            f"needs <code>{tag}</code> in the message\n"
            f"• Or use <code>/dl &lt;url&gt;</code> anywhere to force a download\n\n"
            f"Maximum file size: <b>{config.MAX_UPLOAD_SIZE_MB}MB</b>\n\n"
            "<i>Only messages sent after I joined are processed.</i>"
        )
        await message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command"""
        message = update.effective_message
        if not message:
            return

        tag = esc(config.DOWNLOAD_TAG)
        help_message = (
            "🔧 <b>How to use</b>\n\n"
            "<b>1. Send a link from</b>\n"
            "• YouTube — <code>youtube.com/watch</code>, <code>youtu.be</code>, <code>/shorts</code>\n"
            "• Instagram — <code>/p/</code>, <code>/reel/</code>, <code>/tv/</code>, <code>/stories/</code>\n"
            "• Facebook — <code>/watch</code>, <code>/videos/</code>, <code>/reel/</code>, <code>fb.watch</code>\n"
            "• TikTok — <code>/@user/video/</code>, <code>vm.tiktok.com</code>, <code>vt.tiktok.com</code>\n"
            "• Twitter/X — <code>/status/</code>, <code>t.co</code>\n"
            "• Threads — <code>/@user/post/</code>\n"
            "• 9GAG — <code>9gag.com/gag/</code>\n"
            "• VK — <code>vk.com/video</code>, <code>/clip</code>, <code>vkvideo.ru</code>\n\n"
            "<b>2. When I download</b>\n"
            "• Private chats: always\n"
            f"• Groups: automatically for "
            f"{esc(', '.join(sorted(p.title() for p in config.GROUP_AUTO_PLATFORMS)) or 'nothing')}, "
            f"otherwise add <code>{tag}</code>\n"
            f"• <code>/dl &lt;url&gt;</code> forces a download anywhere\n\n"
            "<b>3. What you get back</b>\n"
            f"• Under {config.TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB: a playable video\n"
            f"• Up to {config.MAX_UPLOAD_SIZE_MB}MB: a file attachment\n"
            "• Threads posts without video: a screenshot\n\n"
            "<b>Commands</b>\n"
            "/start — welcome message\n"
            "/help — this message\n"
            "/status — bot status and statistics\n"
            "/dl — download a specific link"
        )
        await message.reply_text(help_message, parse_mode=ParseMode.HTML)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command"""
        message = update.effective_message
        if not message:
            return

        uptime = format_duration(self.stats.uptime_seconds) or '0:00'
        total = self.stats.succeeded + self.stats.failed
        success_rate = f"{self.stats.succeeded / total * 100:.0f}%" if total else "n/a"

        top_platforms = sorted(
            self.stats.per_platform.items(), key=lambda item: item[1], reverse=True
        )[:5]
        platform_line = (
            ", ".join(f"{name.title()} ({count})" for name, count in top_platforms)
            or "nothing yet"
        )

        status_message = (
            "📊 <b>Bot Status</b>\n\n"
            f"✅ Online · uptime <b>{uptime}</b>\n"
            f"⬇️ Downloads: <b>{self.stats.succeeded}</b> ok / "
            f"<b>{self.stats.failed}</b> failed ({success_rate})\n"
            f"📦 Transferred: <b>{format_size(self.stats.bytes_sent)}</b>\n"
            f"🔄 Active now: <b>{self.stats.active}</b> / {config.MAX_CONCURRENT_DOWNLOADS}\n"
            f"🏆 Most used: {esc(platform_line)}\n\n"
            f"📁 Max upload: <b>{config.MAX_UPLOAD_SIZE_MB}MB</b>"
            f"{' (local Bot API)' if config.USE_LOCAL_BOT_API else ''}\n"
            f"🎬 Merging/thumbnails: <b>{'on' if self.downloader.has_ffmpeg else 'off (no ffmpeg)'}</b>\n"
            f"🔧 Platforms: {SUPPORTED_PLATFORMS_TEXT}"
        )
        await message.reply_text(status_message, parse_mode=ParseMode.HTML)

    async def download_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /dl <url> - force a download regardless of chat type or tag."""
        message = update.effective_message
        if not message:
            return

        candidate_text = ' '.join(context.args or []) if context else ''
        if not candidate_text and message.reply_to_message:
            replied = message.reply_to_message
            candidate_text = replied.text or replied.caption or ''

        urls = [url for url in self.extract_urls(candidate_text)
                if self.downloader.detect_platform(url)]

        if not urls:
            await message.reply_text(
                "Send me a link to download, e.g. <code>/dl https://youtu.be/dQw4w9WgXcQ</code>\n"
                "You can also reply to a message that contains a link with /dl.",
                parse_mode=ParseMode.HTML,
            )
            return

        self.remember_message(message.chat.id, message.message_id)
        await asyncio.gather(*(self.process_video_url(message, url) for url in urls))

    # -- URL extraction --------------------------------------------------

    @staticmethod
    def _trim_url(url: str) -> str:
        """Strip sentence punctuation that got swept into the match."""
        while url and url[-1] in _TRAILING_PUNCTUATION:
            # A closing paren that balances one inside the URL belongs to the URL.
            if url[-1] == ')' and url.count(')') <= url.count('('):
                break
            url = url[:-1]
        return url

    def extract_urls(self, text: str) -> List[str]:
        """Extract URLs from message text

        Args:
            text: Message text to search

        Returns:
            De-duplicated list of URLs found in text, in order of appearance
        """
        urls: List[str] = []
        for raw in URL_REGEX.findall(text or ''):
            cleaned = self._trim_url(raw)
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
        return urls

    def urls_from_message(self, message: Message) -> List[str]:
        """Collect URLs from the message body plus any hyperlinked text."""
        text = (getattr(message, 'text', None) or getattr(message, 'caption', None) or '')
        urls = self.extract_urls(text)

        entities = (
            getattr(message, 'entities', None)
            or getattr(message, 'caption_entities', None)
            or []
        )
        for entity in entities:
            if getattr(entity, 'type', None) == MessageEntity.TEXT_LINK and entity.url:
                cleaned = self._trim_url(entity.url)
                if cleaned and cleaned not in urls:
                    urls.append(cleaned)
        return urls

    @staticmethod
    def should_download(chat_type: str, platform: str, has_tag: bool) -> bool:
        """Decide whether a link should be downloaded without an explicit command."""
        if chat_type == 'private':
            return True
        if platform in config.GROUP_AUTO_PLATFORMS:
            return True
        return has_tag

    # -- message handling ------------------------------------------------

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages and process video URLs"""
        message = update.effective_message if update else None
        if not message:
            return

        text = (getattr(message, 'text', None) or getattr(message, 'caption', None) or '')
        if not text:
            return

        chat = message.chat
        chat_type = chat.type
        chat_title = chat.title or "Private Chat"

        logger.debug("Message from %s (%s, %s)", chat_title, chat.id, chat_type)

        urls = self.urls_from_message(message)
        if not urls:
            return

        # Only link-bearing messages are worth a slot in the de-duplication cache.
        if not self.remember_message(chat.id, message.message_id):
            return

        has_download_tag = config.DOWNLOAD_TAG in text.lower()

        supported: List[Tuple[str, str]] = []
        for url in urls:
            platform = self.downloader.detect_platform(url)
            if platform:
                supported.append((url, platform))
                logger.info("Found %s URL in message: %s", platform, url)

        if not supported:
            if chat_type == 'private' and config.REPLY_ON_UNSUPPORTED_LINKS:
                await message.reply_text(
                    "🤔 I don't recognise that link. Send /help to see what I support.",
                    parse_mode=ParseMode.HTML,
                )
            return

        to_process = [
            (url, platform) for url, platform in supported
            if self.should_download(chat_type, platform, has_download_tag)
        ]

        if not to_process:
            logger.info("Skipping %d link(s): no %s tag in group chat",
                        len(supported), config.DOWNLOAD_TAG)
            if config.HINT_ON_UNTAGGED_LINKS:
                platforms = ', '.join(sorted({p.title() for _, p in supported}))
                await message.reply_text(
                    f"Found a {esc(platforms)} link. Add <code>{esc(config.DOWNLOAD_TAG)}</code> "
                    f"to download it.",
                    parse_mode=ParseMode.HTML,
                )
            return

        # Run links concurrently; the semaphore keeps actual downloads bounded.
        await asyncio.gather(
            *(self.process_video_url(message, url) for url, _ in to_process),
            return_exceptions=True,
        )

    async def process_video_url(self, message: Message, url: str) -> None:
        """Process a URL and send the downloaded video or screenshot

        Args:
            message: Original message containing the URL
            url: URL to process (video URL or Threads post URL)
        """
        platform = self.downloader.detect_platform(url) or 'unknown'
        chat_id = message.chat.id
        chat_title = message.chat.title or "Private Chat"
        logger.info("Processing %s URL %s for chat %s (%s)", platform, url, chat_title, chat_id)

        processing_msg = await message.reply_text(
            f"🔎 Found a <b>{esc(platform.title())}</b> link…",
            parse_mode=ParseMode.HTML,
        )

        semaphore = self.download_semaphore()
        if semaphore.locked():
            await self.safe_edit(
                processing_msg,
                f"⏳ Queued — {self.stats.active} download(s) in progress…",
            )

        result: Optional[Dict[str, Any]] = None
        try:
            async with semaphore:
                self.stats.active += 1
                try:
                    result = await self._download(message, processing_msg, url, platform)
                finally:
                    self.stats.active -= 1

            if result is None:
                return

            await self._deliver(message, processing_msg, result, platform)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error processing %s: %s", url, exc, exc_info=True)
            self.stats.record_failure()
            await self.safe_edit(
                processing_msg,
                "❌ An unexpected error occurred while processing that link.",
            )
            if result and result.get('filepath'):
                self.downloader.cleanup_file(result['filepath'])

    async def _download(self, message: Message, processing_msg: Message, url: str,
                        platform: str) -> Optional[Dict[str, Any]]:
        """Run the blocking download off the event loop and normalize the outcome.

        Returns the successful result dict, or None when the failure has already
        been reported to the user.
        """
        privileged = self.is_privileged(message)
        if privileged:
            logger.info("Chat %s is privileged: bypassing the download size limit", message.chat.id)

        await self.safe_edit(processing_msg, f"⬇️ Downloading from <b>{esc(platform.title())}</b>…")

        reporter = ProgressReporter(
            self, processing_msg, asyncio.get_running_loop(), platform
        )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.downloader.download_video, url,
                    ignore_size_limit=privileged, progress_callback=reporter,
                ),
                timeout=config.DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Download timed out after %ss: %s", config.DOWNLOAD_TIMEOUT_SECONDS, url)
            self.stats.record_failure()
            await self.safe_edit(
                processing_msg,
                f"⏱️ Download timed out after "
                f"{format_duration(config.DOWNLOAD_TIMEOUT_SECONDS)}. Try a shorter video.",
            )
            return None

        if not result:
            logger.warning("Download returned nothing for %s", url)
            self.stats.record_failure()
            await self.safe_edit(processing_msg, f"❌ {esc(ERROR_MESSAGES['download_failed'])}")
            return None

        if 'error' in result:
            await self._report_error(processing_msg, result, url)
            return None

        # A privileged download can still be too big for the Bot API to accept.
        filesize = result.get('filesize') or 0
        if filesize > config.MAX_UPLOAD_SIZE_MB * MB:
            logger.warning("Downloaded %.1fMB exceeds the %dMB upload limit",
                           filesize / MB, config.MAX_UPLOAD_SIZE_MB)
            self.stats.record_failure()
            self.downloader.cleanup_file(result['filepath'])
            hint = ("" if config.USE_LOCAL_BOT_API else
                    "\n\n<i>Uploads above 50MB need a self-hosted Bot API server "
                    "(TELEGRAM_API_BASE_URL).</i>")
            await self.safe_edit(
                processing_msg,
                f"❌ The video is {format_size(filesize)}, above the "
                f"{config.MAX_UPLOAD_SIZE_MB}MB upload limit.{hint}",
            )
            return None

        logger.info("Downloaded %s (%s) -> %s",
                    result.get('title'), format_size(filesize), result.get('filepath'))
        return result

    async def _report_error(self, processing_msg: Message, result: Dict[str, Any],
                            url: str) -> None:
        """Turn a downloader error dict into a single user-facing message."""
        code = result.get('error', 'download_failed')
        text = result.get('message') or ERROR_MESSAGES.get(code, ERROR_MESSAGES['download_failed'])
        logger.warning("Download failed (%s) for %s: %s", code, url, text)
        self.stats.record_failure()

        if code == 'file_too_large' and result.get('filesize'):
            text = f"{text}\nVideo size: {format_size(result['filesize'])}"

        await self.safe_edit(processing_msg, f"❌ {esc(text)}")

    async def _deliver(self, message: Message, processing_msg: Message,
                       result: Dict[str, Any], platform: str) -> None:
        """Send the downloaded artefact and clean up afterwards, success or not."""
        filepath = result.get('filepath')
        try:
            if result.get('is_screenshot'):
                sent = await self._send_screenshot(message, result, processing_msg)
            else:
                sent = await self._send_video_with_retry(message, result, processing_msg)

            if sent:
                self.stats.record_success(platform, result.get('filesize') or 0)
                await self.safe_delete(processing_msg)
                logger.info("Successfully delivered %s from %s", result.get('title'), platform)
            else:
                self.stats.record_failure()
        finally:
            # The file is ours either way; never leave it behind.
            self.downloader.cleanup_file(filepath)

    # -- sending ---------------------------------------------------------

    async def _send_screenshot(self, message: Message, result: Dict[str, Any],
                               processing_msg: Message) -> bool:
        """Send a Threads screenshot as a photo."""
        caption = self._create_caption(result, (result.get('filesize') or 0) / MB,
                                       icon="📸")
        error = await self._attempt_send(
            lambda: message.reply_photo(
                photo=result['filepath'],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **MEDIA_TIMEOUTS,
            )
        )
        if error is None:
            return True

        logger.error("Failed to send screenshot: %s", error)
        await self.safe_edit(processing_msg, f"❌ Failed to send the screenshot: {esc(error)}")
        return False

    async def _send_video_with_retry(self, message: Message, result: Dict[str, Any],
                                     processing_msg: Message) -> bool:
        """Send video to chat with retry logic and proper error handling"""
        file_size_mb = (result.get('filesize') or 0) / MB

        if file_size_mb > config.MAX_UPLOAD_SIZE_MB:
            await self.safe_edit(
                processing_msg,
                f"❌ File is {file_size_mb:.1f}MB, above the "
                f"{config.MAX_UPLOAD_SIZE_MB}MB upload limit.",
            )
            return False

        if file_size_mb > config.TELEGRAM_VIDEO_SIZE_LIMIT_MB:
            return await self._send_as_document(message, result, processing_msg, file_size_mb)

        error = await self._send_as_video(message, result, processing_msg, file_size_mb)
        if error is None:
            return True

        if not upload_retry_is_safe(error):
            await self.safe_edit(
                processing_msg,
                "⚠️ Telegram stopped responding while accepting the upload. "
                "The video may still have been posted — check the chat before retrying.",
            )
            return False

        # Telegram rejects some containers as playable video but accepts them as
        # a file, so a document is a genuinely useful fallback rather than noise.
        logger.info("Video send failed, retrying as a document")
        await self.safe_edit(processing_msg, "📁 Sending as a file instead…")
        return await self._send_as_document(message, result, processing_msg, file_size_mb)

    async def _send_as_document(self, message: Message, result: Dict[str, Any],
                                processing_msg: Message, file_size_mb: float) -> bool:
        """Send file as document with proper error handling"""
        await self.safe_edit(
            processing_msg, f"📤 Uploading {format_size(result.get('filesize'))}…"
        )
        caption = self._create_caption(result, file_size_mb, is_document=True)

        error = await self._attempt_send(
            lambda: message.reply_document(
                document=result['filepath'],
                caption=caption,
                parse_mode=ParseMode.HTML,
                **MEDIA_TIMEOUTS,
            )
        )
        if error is None:
            logger.info("Document sent successfully (%.1fMB)", file_size_mb)
            return True

        logger.error("Failed to send document: %s", error)
        await self.safe_edit(processing_msg, f"❌ Failed to send the file: {esc(error)}")
        return False

    async def _send_as_video(self, message: Message, result: Dict[str, Any],
                             processing_msg: Message,
                             file_size_mb: float) -> Optional[BaseException]:
        """Send file as video. Returns None on success, else the last error."""
        await self.safe_edit(
            processing_msg, f"📤 Uploading {format_size(result.get('filesize'))}…"
        )

        send_kwargs: Dict[str, Any] = {
            'video': result['filepath'],
            'caption': self._create_caption(result, file_size_mb),
            'parse_mode': ParseMode.HTML,
            'supports_streaming': True,
            **MEDIA_TIMEOUTS,
        }

        duration = result.get('duration')
        if isinstance(duration, (int, float)) and duration > 0:
            send_kwargs['duration'] = int(duration)

        width, height = result.get('width'), result.get('height')
        if width and height:
            try:
                send_kwargs['width'] = int(width)
                send_kwargs['height'] = int(height)
            except (TypeError, ValueError):
                logger.warning("Invalid video dimensions %r x %r, skipping", width, height)

        thumbnail = None
        if config.GENERATE_THUMBNAILS:
            thumbnail = await asyncio.to_thread(
                self.downloader.generate_thumbnail, result['filepath']
            )
            if thumbnail:
                send_kwargs['thumbnail'] = thumbnail

        try:
            error = await self._attempt_send(lambda: message.reply_video(**send_kwargs))
        finally:
            self.downloader.cleanup_file(thumbnail)

        if error is None:
            logger.info("Video sent successfully (%.1fMB)", file_size_mb)
        else:
            logger.error("Failed to send video: %s", error)
        return error

    async def _attempt_send(self, send) -> Optional[BaseException]:
        """Run an upload with retries. Returns None on success, else the last error."""
        last_error: Optional[BaseException] = None

        for attempt in range(1, config.MAX_UPLOAD_RETRIES + 1):
            try:
                await send()
                return None
            except RetryAfter as exc:
                last_error = exc
                logger.warning("Rate limited, waiting %ss (attempt %d)", exc.retry_after, attempt)
                await asyncio.sleep(float(exc.retry_after) + 1)
            except _FATAL_SEND_ERRORS as exc:
                logger.error("Non-retryable upload error: %s", exc)
                return exc
            except (TimedOut, NetworkError) as exc:
                last_error = exc
                logger.warning("Upload attempt %d failed: %s", attempt, exc)
                if not upload_retry_is_safe(exc):
                    # The whole body reached Telegram and only the reply was lost,
                    # so a retry would post the same media a second time.
                    logger.error("Not retrying: Telegram may already have accepted the upload")
                    return exc
                if attempt < config.MAX_UPLOAD_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 15))
            except TelegramError as exc:
                last_error = exc
                logger.warning("Telegram error on attempt %d: %s", attempt, exc)
                if attempt < config.MAX_UPLOAD_RETRIES:
                    await asyncio.sleep(2)
            except OSError as exc:
                logger.error("Could not read the file to upload: %s", exc)
                return exc

        return last_error or TimedOut()

    def _create_caption(self, result: Dict[str, Any], file_size_mb: float,
                        is_document: bool = False, icon: Optional[str] = None) -> str:
        """Create formatted caption for video/document, trimmed to Telegram's limit"""
        if icon is None:
            icon = "📁" if is_document else "🎥"

        details = [f"📱 {esc(str(result.get('platform') or 'unknown').title())}"]

        duration_text = format_duration(result.get('duration'))
        if duration_text:
            details.append(f"⏱️ {duration_text}")
        details.append(f"📊 {file_size_mb:.1f}MB")

        suffix = "\n\n" + " · ".join(details)
        if is_document:
            suffix += "\n\n⚠️ Sent as a file because it exceeds " \
                      f"{config.TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB"

        source_url = result.get('url')
        if source_url:
            suffix += f'\n<a href="{esc_attr(source_url)}">source</a>'
            # Only the anchor text counts toward Telegram's caption budget.
            visible_suffix_len = len(suffix) - len(f'<a href="{esc_attr(source_url)}"></a>')
        else:
            visible_suffix_len = len(suffix)

        # Titles come from the platform and can be arbitrarily long; Telegram
        # rejects the whole message once the caption passes 1024 characters.
        # Escaping does not change the parsed length, so budget against the raw title.
        budget = config.MAX_CAPTION_LENGTH - visible_suffix_len - len(icon) - 1
        title = str(result.get('title') or 'Video').strip()
        if len(title) > budget:
            title = title[:max(0, budget - 1)].rstrip() + "…"

        return f"{icon} <b>{esc(title)}</b>{suffix}"

    # -- lifecycle -------------------------------------------------------

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors with detailed logging"""
        error = context.error
        update_id = getattr(update, 'update_id', 'N/A')
        logger.error("Error in update %s: %s", update_id, error, exc_info=error)

        # Nothing useful to tell the user about network blips; they'd just get spam.
        if isinstance(error, (NetworkError, TimedOut, RetryAfter)):
            return

        message = getattr(update, 'effective_message', None)
        if message is not None:
            try:
                await message.reply_text("❌ An unexpected error occurred. Please try again later.")
            except TelegramError:
                logger.debug("Could not deliver the error message to the user")

    async def _post_init(self, application: Application) -> None:
        """Register the command menu and sweep stale downloads."""
        removed = await asyncio.to_thread(
            self.downloader.cleanup_old_files, config.CLEANUP_MAX_AGE_HOURS
        )
        if removed:
            logger.info("Startup cleanup removed %d stale file(s)", removed)

        try:
            await application.bot.set_my_commands([
                BotCommand('start', 'Welcome message'),
                BotCommand('help', 'How to use the bot'),
                BotCommand('status', 'Bot status and statistics'),
                BotCommand('dl', 'Download a specific link'),
            ])
        except TelegramError as exc:
            logger.warning("Could not register the command menu: %s", exc)

    async def _post_shutdown(self, application: Application) -> None:
        await asyncio.to_thread(self.downloader.close)

    def build_application(self) -> Application:
        """Construct the Application with timeouts that suit large file transfers."""
        builder = Application.builder().token(self.token)

        if config.TELEGRAM_API_BASE_URL:
            base_url = config.TELEGRAM_API_BASE_URL.rstrip('/')
            if not base_url.endswith('/bot'):
                base_url += '/bot'
            logger.info("Using local Bot API server at %s", base_url)
            builder = builder.base_url(base_url).local_mode(True)

        return (
            builder
            # Without this PTB handles updates strictly one at a time, so a
            # single download would block every other chat.
            .concurrent_updates(True)
            .connect_timeout(60)
            .read_timeout(config.UPLOAD_TIMEOUT_SECONDS)
            .write_timeout(config.UPLOAD_TIMEOUT_SECONDS)
            # PTB ignores write_timeout for requests carrying a file and uses
            # this instead. Its 20 second default fails any sizeable upload.
            .media_write_timeout(config.UPLOAD_TIMEOUT_SECONDS)
            .pool_timeout(60)
            .get_updates_read_timeout(60)
            .connection_pool_size(max(8, config.MAX_CONCURRENT_DOWNLOADS * 4))
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )

    def run(self) -> None:
        """Start the bot with proper configuration"""
        logger.info("Initializing Telegram Video Bot...")

        try:
            self.application = self.build_application()
            logger.info(
                "Application created (media upload timeout: %ss, PTB's default is 20s)",
                self.application.bot.request._media_write_timeout,
            )

            self._register_handlers()

            logger.info("Starting bot polling...")
            self.application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )

        except Exception as exc:
            logger.error("Failed to start bot: %s", exc, exc_info=True)
            raise
        finally:
            logger.info("Bot shutdown complete")

    def _register_handlers(self) -> None:
        """Register all command and message handlers"""
        logger.info("Registering handlers...")

        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler(["dl", "download"], self.download_command))

        # Captions carry links too (e.g. a forwarded photo with a URL under it).
        self.application.add_handler(
            MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                           self.handle_message)
        )

        self.application.add_error_handler(self.error_handler)
        logger.info("All handlers registered successfully")


def main():
    """Main entry point for the bot"""
    try:
        token = config.TELEGRAM_BOT_TOKEN
        if not token:
            logger.error("TELEGRAM_BOT_TOKEN environment variable not set")
            sys.exit(1)

        logger.info("Creating bot instance...")
        bot = TelegramVideoBot(token)

        logger.info("Starting bot...")
        bot.run()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as exc:
        logger.error("Fatal error in main: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
