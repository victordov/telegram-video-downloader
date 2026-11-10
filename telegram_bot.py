"""
Telegram Video Downloader Bot
================================

A robust Telegram bot that downloads videos from multiple social media platforms:
- YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK, Reddit,
  Pinterest, Vimeo, Twitch clips, Bluesky, Dailymotion
- in private chats (and with /dl), any other site yt-dlp has an extractor for

Features:
- Automatic platform detection and live download progress with a Cancel button
- Multi-item posts sent as albums (tweets with several videos, Instagram
  carousels, TikTok photo posts)
- Videos above the upload limit are shrunk to fit instead of refused
- Audio-only downloads (/audio or the 🎵 button) and an HD button for privileged chats
- Already-uploaded links are resent instantly from a Telegram file-ID cache,
  which also powers inline mode (@bot <link> in any chat)
- Captions with the author, a collapsed description and spoilers for adult content
- Access control (allow list) and a per-user hourly rate limit
- Lifetime statistics, breakage alerts for the admins, and yt-dlp self-updates
- Polling (laptop) or webhooks (server)

Limitations:
- api.telegram.org caps bot uploads at 50MB. Point TELEGRAM_API_BASE_URL at a
  self-hosted Bot API server to raise that to 2GB.
- Videos above 50MB are sent as documents rather than playable videos.
"""

import asyncio
import hashlib
import html
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

import httpx  # a hard dependency of python-telegram-bot
from telegram import (
    BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultCachedAudio, InlineQueryResultCachedDocument,
    InlineQueryResultCachedMpeg4Gif, InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo, InlineQueryResultsButton, InputMediaDocument,
    InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message, MessageEntity, Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, InlineQueryHandler,
    MessageHandler, filters,
)

import config
import dns_fallback
import updater
from access import AccessPolicy, RateLimiter
from monitoring import FailureMonitor
from storage import Storage
from video_downloader import (
    ERROR_MESSAGES, MB, MODE_AUDIO, MODE_HD, MODE_VIDEO, TELEGRAM_PHOTO_LIMIT_BYTES,
    VideoDownloader, platform_label,
)

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
    "YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK, "
    "Reddit, Pinterest, Vimeo, Twitch clips, Bluesky, Dailymotion"
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

#: Telegram allows at most this many items in one album.
MAX_MEDIA_GROUP = 10

#: Don't tell the same stranger the bot is private more often than this.
DENIED_NOTICE_INTERVAL = 6 * 3600

#: Ask the admins about the same chat at most this often.
ACCESS_REQUEST_INTERVAL = 24 * 3600

#: How long a group membership check is trusted.
MEMBER_CACHE_SECONDS = 6 * 3600
NON_MEMBER_CACHE_SECONDS = 10 * 60

#: Callback data prefixes for the inline buttons.
CB_CANCEL, CB_AUDIO, CB_HD = 'x', 'a', 'h'
#: Admin access buttons: allow a chat, ignore it, remove its entry.
CB_GRANT, CB_IGNORE, CB_FORGET = 'ga', 'gi', 'gr'

#: Deep-link prefix used by inline mode's "download in a private chat" button.
DEEP_LINK_PREFIX = 'dl_'


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


def tg_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units (emoji count as two)."""
    return len(text.encode('utf-16-le')) // 2


def tg_truncate(text: str, limit: int, ellipsis: str = '…') -> str:
    """Cut text to at most ``limit`` UTF-16 units, ending in an ellipsis."""
    if tg_len(text) <= limit:
        return text
    budget = max(0, limit - tg_len(ellipsis))
    out, used = [], 0
    for char in text:
        width = tg_len(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return ''.join(out).rstrip() + ellipsis


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


def format_wait(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60)))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def progress_bar(percent: Optional[float], width: int = 12) -> str:
    """Render a text progress bar; returns an indeterminate bar when unknown."""
    if percent is None:
        return '▪' * width
    filled = max(0, min(width, int(round(percent / 100 * width))))
    return '█' * filled + '░' * (width - filled)


def render_progress(platform: str, progress: Dict[str, Any]) -> str:
    """Build the body of the "downloading" status message."""
    percent = progress.get('percent')
    item_count = progress.get('item_count') or 0
    item = f" · item {progress.get('item_index')}/{item_count}" if item_count > 1 else ""

    if progress.get('status') == 'compressing':
        target = progress.get('target_bytes')
        lines = [f"🗜️ Shrinking to fit {esc(format_size(target)) if target else 'the limit'}{item}"]
        lines.append(f"<code>{progress_bar(percent)}</code>"
                     + (f" {percent:.0f}%" if percent is not None else ""))
        return "\n".join(lines)

    total = progress.get('total_bytes')
    downloaded = progress.get('downloaded_bytes') or 0
    speed = progress.get('speed')
    eta = progress.get('eta')

    lines = [f"⬇️ Downloading from <b>{esc(platform_label(platform))}</b>{item}"]

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

    _SHOWN_STATUSES = ('downloading', 'compressing')

    def __init__(self, bot: 'TelegramVideoBot', message: Optional[Message],
                 loop: asyncio.AbstractEventLoop, platform: str,
                 interval: float = config.PROGRESS_UPDATE_INTERVAL,
                 reply_markup: Optional[InlineKeyboardMarkup] = None):
        self._bot = bot
        self._message = message
        self._loop = loop
        self._platform = platform
        self._interval = interval
        self._markup = reply_markup
        self._lock = threading.Lock()
        self._last_edit = 0.0
        self._last_text = ''

    def __call__(self, progress: Dict[str, Any]) -> None:
        if progress.get('status') not in self._SHOWN_STATUSES or self._message is None:
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
                self._bot.safe_edit(self._message, text, reply_markup=self._markup), self._loop
            )
        except RuntimeError:
            # Loop shut down mid-download; nothing useful left to report.
            pass


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

@dataclass
class BotStats:
    """Counters behind /status: this session in memory, lifetime in SQLite."""
    started_at: float = field(default_factory=time.time)
    succeeded: int = 0
    failed: int = 0
    bytes_sent: int = 0
    active: int = 0
    cache_hits: int = 0
    per_platform: Dict[str, int] = field(default_factory=dict)
    storage: Optional[Storage] = field(default=None, repr=False)

    def record_success(self, platform: str, size: int) -> None:
        self.succeeded += 1
        self.bytes_sent += max(0, size)
        self.per_platform[platform] = self.per_platform.get(platform, 0) + 1
        if self.storage is not None:
            self.storage.record_success(platform, size)

    def record_failure(self, platform: str = 'unknown', error: Optional[str] = None) -> None:
        self.failed += 1
        if self.storage is not None:
            self.storage.record_failure(platform, error)

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at


@dataclass
class ActiveJob:
    """A running (or queued) download the Cancel button can stop."""
    job_id: str
    user_id: Optional[int]
    chat_id: int
    url: str
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class AlbumEntry:
    """One item of an album about to be sent: a local file or a cached file ID."""
    kind: str  # 'photo', 'video' or 'document'
    source: str
    size: int = 0
    is_file: bool = True
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None


class ChatTarget:
    """Quacks like a Message for the reply_* calls the send helpers make.

    Lets the same sending code post into an arbitrary chat (the inline-mode
    cache chat) instead of replying to a user's message.
    """

    def __init__(self, bot, chat_id):
        self._bot = bot
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type='channel', title='cache')
        self.message_id = 0
        self.from_user = None

    def _send(self, method: str) -> Callable[..., Awaitable[Any]]:
        async def send(**kwargs):
            kwargs.setdefault('disable_notification', True)
            return await getattr(self._bot, method)(chat_id=self.chat_id, **kwargs)
        return send

    def __getattr__(self, name: str):
        if name.startswith('reply_'):
            return self._send('send_' + name[len('reply_'):])
        raise AttributeError(name)


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
            force_mp4=config.FORCE_MP4,
            upload_limit_mb=config.MAX_UPLOAD_SIZE_MB,
            compress_oversized=config.COMPRESS_OVERSIZED,
            compress_max_source_mb=config.COMPRESS_MAX_SOURCE_MB,
            max_album_items=config.MAX_ALBUM_ITEMS,
            hd_max_height=config.HD_MAX_HEIGHT,
            cookies_only_when_needed=config.COOKIES_ONLY_WHEN_NEEDED,
        )
        dns_fallback.install(config.DNS_FALLBACK_DOH_URL)
        self.application: Optional[Application] = None
        self.storage = Storage(config.DATABASE_PATH)
        self.stats = BotStats(storage=self.storage)
        self.access = AccessPolicy(config.ALLOWED_CHAT_IDS, PRIVATE_CHAT_IDS, config.ADMIN_CHAT_IDS,
                                   granted=self.storage.access_entries(Storage.ACCESS_ALLOWED))
        self.rate_limiter = RateLimiter(config.RATE_LIMIT_PER_HOUR)
        self.monitor = FailureMonitor(config.ALERT_FAILURE_THRESHOLD,
                                      config.ALERT_COOLDOWN_MINUTES * 60)
        # Unexpected exceptions alert on the first occurrence, then cool down.
        self.error_monitor = FailureMonitor(1, config.ALERT_COOLDOWN_MINUTES * 60)

        # (chat_id, message_id) -> None, ordered so the oldest entries can be
        # evicted. Message IDs are only unique per chat, so keying on the ID
        # alone silently dropped messages that collided across chats.
        self.processed_messages: 'OrderedDict[Tuple[int, int], None]' = OrderedDict()

        self.jobs: Dict[str, ActiveJob] = {}
        self._denied_notified: Dict[int, float] = {}
        self._access_requested: Dict[str, float] = {}
        self._chat_titles: Dict[str, str] = {}
        # user_id -> (member of an allowed group, valid until)
        self._membership: Dict[int, Tuple[bool, float]] = {}
        self._prefetching: Dict[str, asyncio.Task] = {}
        self._background: set = set()
        self._latest_ytdlp: Optional[str] = None
        self._restart_requested = False
        self._restart_task: Optional[asyncio.Task] = None

        self._semaphore: Optional[asyncio.Semaphore] = None
        if self.access.is_open:
            logger.warning("ALLOWED_CHAT_IDS is empty: anyone who finds this bot can use it")
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

    def _spawn(self, coroutine: Awaitable[Any]) -> asyncio.Task:
        """Fire-and-forget a coroutine, keeping a reference so it is not GC'd."""
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    @staticmethod
    async def safe_edit(message: Optional[Message], text: str,
                        reply_markup: Optional[InlineKeyboardMarkup] = None) -> bool:
        """Edit a message, swallowing the errors that are not worth surfacing."""
        if message is None:
            return False
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
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

    # -- access ------------------------------------------------------------

    @staticmethod
    def _user_id(message: Optional[Message]) -> Optional[int]:
        user = getattr(message, 'from_user', None)
        return getattr(user, 'id', None)

    def is_authorized(self, message: Message, user_id: Optional[int] = None) -> bool:
        user_id = user_id if user_id is not None else self._user_id(message)
        return self.access.is_allowed(message.chat.id, user_id)

    async def authorized(self, message: Message, user_id: Optional[int] = None) -> bool:
        """is_authorized(), plus members of an allowed group in their private chat."""
        user_id = user_id if user_id is not None else self._user_id(message)
        if self.is_authorized(message, user_id):
            return True
        return message.chat.type == 'private' and await self.is_group_member(user_id)

    async def is_group_member(self, user_id: Optional[int]) -> bool:
        """Whether the user is in one of the allowed groups (cached).

        Listing a group in ALLOWED_CHAT_IDS is meant to let its people use the
        bot, which includes their private chat with it and inline mode.
        """
        bot = getattr(self.application, 'bot', None)
        if user_id is None or bot is None or self.access.is_open or not self.access.group_ids:
            return False
        now = time.time()
        cached = self._membership.get(user_id)
        if cached and now < cached[1]:
            return cached[0]

        member = False
        for group in self.access.group_ids:
            try:
                info = await bot.get_chat_member(int(group), user_id)
            except (TelegramError, ValueError) as exc:
                logger.debug("Membership check of %s in %s failed: %s", user_id, group, exc)
                continue
            status = getattr(info, 'status', None)
            if status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR,
                          ChatMemberStatus.MEMBER) or (
                    status == ChatMemberStatus.RESTRICTED and getattr(info, 'is_member', False)):
                member = True
                break

        if len(self._membership) > 5000:
            self._membership = {k: v for k, v in self._membership.items() if v[1] > now}
        ttl = MEMBER_CACHE_SECONDS if member else NON_MEMBER_CACHE_SECONDS
        self._membership[user_id] = (member, now + ttl)
        return member

    def _is_exempt(self, message: Message, user_id: Optional[int]) -> bool:
        return (self.is_privileged(message) or self.access.is_admin(user_id)
                or self.access.is_exempt_from_limits(None, user_id))

    async def _deny(self, message: Message) -> None:
        """Turn a chat outside the allow list away, and ask the admins about it."""
        logger.info("Refused unauthorized chat %s", message.chat.id)
        asked = await self._request_access(message)
        if message.chat.type != 'private' or not config.REPLY_TO_UNAUTHORIZED:
            return
        now = time.time()
        if now - self._denied_notified.get(message.chat.id, 0) < DENIED_NOTICE_INTERVAL:
            return
        self._denied_notified[message.chat.id] = now
        user_id = self._user_id(message) or message.chat.id
        if asked:
            text = ("🔒 This bot is private.\n\n"
                    "I've asked its owner to let you in, and I'll tell you here if they do.")
        else:
            text = ("🔒 This bot is private.\n\n"
                    f"If you know its owner, send them your ID: <code>{esc(user_id)}</code>")
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    @staticmethod
    def _describe_user(user: Any) -> str:
        name = getattr(user, 'full_name', None) or getattr(user, 'first_name', None) or ''
        username = getattr(user, 'username', None)
        if isinstance(username, str) and username:
            name = f"{name} (@{username})" if name else f"@{username}"
        return name if isinstance(name, str) else ''

    async def _request_access(self, message: Message) -> bool:
        """Send the admins an Allow / Ignore prompt for this chat.

        Returns True when the admins have been asked (now or recently).
        """
        if (not config.NOTIFY_ACCESS_REQUESTS or self.access.is_open
                or not config.ADMIN_CHAT_IDS):
            return False
        chat = message.chat
        key = str(chat.id)
        if self.storage.access_status(key) == Storage.ACCESS_IGNORED:
            return False
        now = time.time()
        if now - self._access_requested.get(key, 0) < ACCESS_REQUEST_INTERVAL:
            return True
        self._access_requested[key] = now

        who = self._describe_user(getattr(message, 'from_user', None))
        if chat.type == 'private':
            title = who or key
            text = f"🔑 <b>{esc(title)}</b> wants to use the bot.\nID <code>{esc(key)}</code>"
        else:
            title = getattr(chat, 'title', None) or key
            text = (f"🔑 Someone sent a link in <b>{esc(title)}</b>, which isn't on the allow list."
                    + (f"\nSent by {esc(who)}" if who else "")
                    + f"\nChat ID <code>{esc(key)}</code>")
        self._chat_titles[key] = title
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Allow", callback_data=f"{CB_GRANT}:{key}"),
            InlineKeyboardButton("🙈 Ignore", callback_data=f"{CB_IGNORE}:{key}"),
        ]])
        await self.notify_admins(text, reply_markup=markup)
        return True

    async def _chat_title(self, chat_id: str) -> str:
        """A readable name for a chat ID, asking Telegram when it is not known yet."""
        if chat_id in self._chat_titles:
            return self._chat_titles[chat_id]
        title = chat_id
        bot = getattr(self.application, 'bot', None)
        if bot is not None:
            try:
                chat = await bot.get_chat(int(chat_id))
                title = (chat.title or getattr(chat, 'full_name', None)
                         or (f"@{chat.username}" if chat.username else chat_id))
            except (TelegramError, ValueError) as exc:
                logger.debug("Could not look up chat %s: %s", chat_id, exc)
        self._chat_titles[chat_id] = title
        return title

    async def _access_overview(self) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        """Text and remove buttons for /access."""
        lines = ["🔑 <b>Who can use the bot</b>", ""]
        if self.access.is_open:
            lines.append("Everyone: ALLOWED_CHAT_IDS in .env is empty.")
        else:
            lines.append("<b>From .env</b>")
            for chat_id in sorted(self.access.allowed):
                lines.append(f"• {esc(await self._chat_title(chat_id))} <code>{esc(chat_id)}</code>")
            if self.access.group_ids:
                lines.append("<i>Members of allowed groups can also use it privately and inline.</i>")

        entries = self.storage.access_entries()
        for status, heading in ((Storage.ACCESS_ALLOWED, "Allowed from Telegram"),
                                (Storage.ACCESS_IGNORED, "Ignored")):
            chosen = [(cid, e['title'] or cid) for cid, e in entries.items() if e['status'] == status]
            if chosen:
                lines += ["", f"<b>{heading}</b>"]
                lines += [f"• {esc(title)} <code>{esc(cid)}</code>" for cid, title in chosen]

        buttons = [
            [InlineKeyboardButton(f"❌ {(entry['title'] or cid)[:40]}",
                                  callback_data=f"{CB_FORGET}:{cid}")]
            for cid, entry in list(entries.items())[:30]
        ]
        if buttons:
            lines += ["", "Tap a chat below to forget the decision."]
        return "\n".join(lines), (InlineKeyboardMarkup(buttons) if buttons else None)

    async def _access_button(self, query: Any, action: str, chat_id: str,
                             user_id: Optional[int]) -> None:
        if not self.access.is_admin(user_id):
            await query.answer("Only the bot's admins can do that.", show_alert=True)
            return
        message = query.message if isinstance(query.message, Message) else None
        title = await self._chat_title(chat_id)

        if action == CB_GRANT:
            self.access.grant(chat_id)
            self.storage.access_set(chat_id, Storage.ACCESS_ALLOWED, title)
            self._membership.clear()
            await query.answer("Allowed")
            await self.safe_edit(message, f"✅ <b>{esc(title)}</b> can use the bot now.")
            welcome = ("✅ The owner let you in. Send me a link." if not chat_id.startswith('-')
                       else "✅ I can download links in this chat now.")
            self._spawn(self._send_quietly(chat_id, welcome))
        elif action == CB_IGNORE:
            self.access.revoke(chat_id)
            self.storage.access_set(chat_id, Storage.ACCESS_IGNORED, title)
            self._membership.clear()
            await query.answer("Ignored")
            await self.safe_edit(message, f"🙈 Ignoring <b>{esc(title)}</b>. "
                                          "Send /access to change that.")
        else:
            self.access.revoke(chat_id)
            self.storage.access_remove(chat_id)
            self._membership.clear()
            self._access_requested.pop(chat_id, None)
            await query.answer("Forgotten")
            text, markup = await self._access_overview()
            await self.safe_edit(message, text, reply_markup=markup)

    async def _send_quietly(self, chat_id: str, text: str) -> None:
        bot = getattr(self.application, 'bot', None)
        if bot is None:
            return
        try:
            await bot.send_message(chat_id=int(chat_id), text=text, disable_notification=True)
        except (TelegramError, ValueError) as exc:
            logger.debug("Could not message chat %s: %s", chat_id, exc)

    def _rate_limit_notice(self, message: Message, user_id: Optional[int]) -> Optional[str]:
        """Take a download slot. Returns a notice when the user is over the limit."""
        if not self.rate_limiter.enabled or self._is_exempt(message, user_id):
            return None
        allowed, wait = self.rate_limiter.try_acquire(user_id or message.chat.id)
        if allowed:
            return None
        return (f"⏳ That's {config.RATE_LIMIT_PER_HOUR} downloads in the last hour. "
                f"Try again in {format_wait(wait)}.")

    # -- notifications -----------------------------------------------------

    def _ytdlp_summary(self) -> str:
        version = updater.installed_version()
        age = updater.version_age_days(version)
        text = f"yt-dlp {version}" + (f", {age} days old" if age is not None else "")
        if updater.is_newer(self._latest_ytdlp, version):
            text += f" ({self._latest_ytdlp} is available)"
        return text

    async def notify_admins(self, text: str,
                            reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
        bot = getattr(self.application, 'bot', None)
        if bot is None or not config.ADMIN_CHAT_IDS:
            logger.info("Admin notice: %s", text)
            return
        for admin in config.ADMIN_CHAT_IDS:
            try:
                await bot.send_message(
                    chat_id=int(admin), text=text, parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    reply_markup=reply_markup,
                )
            except (TelegramError, ValueError) as exc:
                logger.warning("Could not notify admin %s: %s", admin, exc)

    def _note_failure(self, platform: str, code: str, detail: str = '') -> None:
        if not VideoDownloader.is_named_platform(platform):
            return  # random sites fail for random reasons; only alert on the named ones
        alert = self.monitor.record_failure(platform_label(platform), code, detail)
        if alert:
            can_update, _ = updater.can_self_update()
            hint = "\nSend /update to update yt-dlp now." if can_update else ""
            self._spawn(self.notify_admins(
                f"⚠️ {esc(alert)}\n<i>{esc(self._ytdlp_summary())}</i>{hint}"
            ))

    def _note_success(self, platform: str) -> None:
        recovered = self.monitor.record_success(platform_label(platform))
        if recovered:
            self._spawn(self.notify_admins(f"✅ {esc(recovered)}"))

    # -- commands -------------------------------------------------------

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command (and inline mode's deep links)"""
        message = update.effective_message
        if not message:
            return

        args = list(getattr(context, 'args', None) or []) if context else []
        if args and args[0].startswith(DEEP_LINK_PREFIX):
            if not await self.authorized(message):
                await self._deny(message)
                return
            url = self.storage.url_for(args[0][len(DEEP_LINK_PREFIX):])
            if url:
                await self.process_video_url(message, url, any_site=True)
                return

        if not await self.authorized(message):
            await self._deny(message)
            return

        tag = esc(config.DOWNLOAD_TAG)
        auto = ', '.join(sorted(platform_label(p) for p in config.GROUP_AUTO_PLATFORMS)) or 'none'
        welcome_message = (
            "🎥 <b>Video Downloader Bot</b>\n\n"
            "Send me a link and I'll fetch the video.\n\n"
            "<b>Supported platforms</b>\n"
            "• YouTube\n• Instagram <i>(carousels as albums)</i>\n• Facebook\n"
            "• TikTok <i>(photo posts too)</i>\n• Twitter/X\n"
            "• Threads <i>(screenshot for non-video posts)</i>\n• 9GAG\n• VK\n"
            "• Reddit, Pinterest, Vimeo, Twitch clips, Bluesky, Dailymotion\n"
            + ("• <i>In this private chat: most other video sites too</i>\n"
               if config.TRY_ANY_LINK else "")
            + "\n<b>How it works</b>\n"
            f"• <b>Private chats:</b> every supported link is downloaded automatically\n"
            f"• <b>Groups:</b> {esc(auto)} downloads automatically, everything else "
            f"needs <code>{tag}</code> in the message\n"
            f"• Or use <code>/dl &lt;url&gt;</code> anywhere to force a download\n"
            f"• <code>/audio &lt;url&gt;</code> sends just the sound\n\n"
            f"Maximum file size: <b>{config.MAX_UPLOAD_SIZE_MB}MB</b>"
            + (" <i>(bigger videos are shrunk to fit)</i>" if config.COMPRESS_OVERSIZED else "")
            + "\n\n<i>Only messages sent after I joined are processed.</i>"
        )
        await message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command"""
        message = update.effective_message
        if not message:
            return

        tag = esc(config.DOWNLOAD_TAG)
        bot_name = getattr(getattr(self.application, 'bot', None), 'username', None)
        inline_line = (f"• In any chat, type <code>@{esc(bot_name)} &lt;link&gt;</code> "
                       "to share a video I already have\n" if bot_name else "")
        help_message = (
            "🔧 <b>How to use</b>\n\n"
            "<b>1. Send a link from</b>\n"
            "• YouTube — <code>youtube.com/watch</code>, <code>youtu.be</code>, <code>/shorts</code>\n"
            "• Instagram — <code>/p/</code>, <code>/reel/</code>, <code>/tv/</code>, <code>/stories/</code>\n"
            "• Facebook — <code>/watch</code>, <code>/videos/</code>, <code>/reel/</code>, <code>fb.watch</code>\n"
            "• TikTok — <code>/@user/video/</code>, <code>/photo/</code>, <code>vm.tiktok.com</code>\n"
            "• Twitter/X — <code>/status/</code>, <code>t.co</code>\n"
            "• Threads — <code>/@user/post/</code>\n"
            "• 9GAG — <code>9gag.com/gag/</code>\n"
            "• VK — <code>vk.com/video</code>, <code>/clip</code>, <code>vkvideo.ru</code>\n"
            "• Reddit, Pinterest, Vimeo, Twitch clips, Bluesky, Dailymotion\n"
            + ("• Private chats: most other video sites as well\n" if config.TRY_ANY_LINK else "")
            + "\n<b>2. When I download</b>\n"
            "• Private chats: always\n"
            f"• Groups: automatically for "
            f"{esc(', '.join(sorted(platform_label(p) for p in config.GROUP_AUTO_PLATFORMS)) or 'nothing')}, "
            f"otherwise add <code>{tag}</code>\n"
            f"• <code>/dl &lt;url&gt;</code> forces a download anywhere\n"
            f"{inline_line}\n"
            "<b>3. What you get back</b>\n"
            f"• Under {config.TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB: a playable video"
            + (", bigger ones are shrunk to fit" if config.COMPRESS_OVERSIZED else "") + "\n"
            f"• Up to {config.MAX_UPLOAD_SIZE_MB}MB: a file attachment\n"
            "• Posts with several videos or photos: an album\n"
            "• Threads posts without video: a screenshot\n"
            "• Buttons under a video: 🎵 audio only, 🔁 higher quality (owner only)\n\n"
            "<b>Commands</b>\n"
            "/start — welcome message\n"
            "/help — this message\n"
            "/status — bot status and statistics\n"
            "/dl — download a specific link\n"
            "/audio — download just the audio of a link"
        )
        await message.reply_text(help_message, parse_mode=ParseMode.HTML)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command"""
        message = update.effective_message
        if not message:
            return
        if not await self.authorized(message):
            await self._deny(message)
            return

        uptime = format_duration(self.stats.uptime_seconds) or '0:00'
        total = self.stats.succeeded + self.stats.failed
        success_rate = f"{self.stats.succeeded / total * 100:.0f}%" if total else "n/a"

        lifetime_ok, lifetime_failed, lifetime_bytes = self.storage.totals()
        since = time.strftime('%Y-%m-%d', time.localtime(self.storage.first_started_at))
        per_platform = self.storage.platform_stats()
        top_platforms = sorted(per_platform.items(),
                               key=lambda item: item[1]['succeeded'], reverse=True)[:6]
        platform_line = (
            ", ".join(
                f"{platform_label(name)} ({row['succeeded']}"
                + (f", {row['failed']} failed" if row['failed'] else "") + ")"
                for name, row in top_platforms if row['succeeded'] or row['failed']
            ) or "nothing yet"
        )

        version = updater.installed_version()
        age = updater.version_age_days(version)
        stale = age is not None and age >= config.YTDLP_STALE_DAYS
        ytdlp_line = f"🧩 {esc(self._ytdlp_summary())}"
        if stale:
            ytdlp_line = "⚠️ " + ytdlp_line[2:] + (
                " — send /update" if self.access.is_admin(self._user_id(message)) else ""
            )

        if self.access.is_open:
            access_line = "open to everyone"
        else:
            access_line = f"{len(self.access.allowed | self.access.granted)} allowed chat(s)"
            if self.access.group_ids:
                access_line += " and their members"
        if self.rate_limiter.enabled:
            access_line += f" · {config.RATE_LIMIT_PER_HOUR}/hour per user"

        status_message = (
            "📊 <b>Bot Status</b>\n\n"
            f"✅ Online · uptime <b>{uptime}</b>\n"
            f"⬇️ Downloads: <b>{self.stats.succeeded}</b> ok / "
            f"<b>{self.stats.failed}</b> failed ({success_rate})"
            + (f" · {self.stats.cache_hits} from cache" if self.stats.cache_hits else "") + "\n"
            f"📈 Since {since}: <b>{lifetime_ok}</b> ok / <b>{lifetime_failed}</b> failed · "
            f"{format_size(lifetime_bytes)} sent\n"
            f"📦 Transferred: <b>{format_size(self.stats.bytes_sent)}</b>\n"
            f"🔄 Active now: <b>{self.stats.active}</b> / {config.MAX_CONCURRENT_DOWNLOADS}\n"
            f"🏆 By site: {esc(platform_line)}\n"
            f"💾 Cached links: <b>{self.storage.cache_count()}</b>\n\n"
            f"📁 Max upload: <b>{config.MAX_UPLOAD_SIZE_MB}MB</b>"
            f"{' (local Bot API)' if config.USE_LOCAL_BOT_API else ''}\n"
            f"🎬 Merging/thumbnails: <b>{'on' if self.downloader.has_ffmpeg else 'off (no ffmpeg)'}</b>\n"
            f"{ytdlp_line}\n"
            f"🔒 Access: {esc(access_line)}\n"
            f"🔧 Platforms: {SUPPORTED_PLATFORMS_TEXT}"
        )
        await message.reply_text(status_message, parse_mode=ParseMode.HTML)

    async def download_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /dl <url> - force a download regardless of chat type or tag."""
        await self._link_command(update, context, MODE_VIDEO)

    async def audio_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /audio <url> - send just the audio track."""
        await self._link_command(update, context, MODE_AUDIO)

    async def _link_command(self, update: Update, context, mode: str) -> None:
        message = update.effective_message
        if not message:
            return

        candidate_text = ' '.join(context.args or []) if context else ''
        if not candidate_text and message.reply_to_message:
            candidate_text = self._message_text(message.reply_to_message)

        urls = [url for url in self.extract_urls(candidate_text)
                if await self._resolve_platform(url, any_site=config.TRY_ANY_LINK)]

        if not urls:
            command = '/audio' if mode == MODE_AUDIO else '/dl'
            await message.reply_text(
                f"Send me a link to download, e.g. <code>{command} https://youtu.be/dQw4w9WgXcQ</code>\n"
                f"You can also reply to a message that contains a link with {command}.",
                parse_mode=ParseMode.HTML,
            )
            return

        if not await self.authorized(message):
            await self._deny(message)
            return

        self.remember_message(message.chat.id, message.message_id)
        await asyncio.gather(*(
            self.process_video_url(message, url, mode=mode, any_site=config.TRY_ANY_LINK)
            for url in urls
        ))

    async def access_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /access - admins only: who may use the bot, and undo decisions."""
        message = update.effective_message
        if not message:
            return
        if not self.access.is_admin(self._user_id(message)):
            await message.reply_text("Only the bot's admins can do that.")
            return
        text, markup = await self._access_overview()
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def update_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /update - admins only: update yt-dlp now and restart."""
        message = update.effective_message
        if not message:
            return
        if not self.access.is_admin(self._user_id(message)):
            await message.reply_text("Only the bot's admins can do that.")
            return
        status = await message.reply_text("🔄 Checking for a newer yt-dlp…")
        text = await self.check_for_update(manual=True)
        await self.safe_edit(status, esc(text))

    # -- URL extraction --------------------------------------------------

    @staticmethod
    def _message_text(message: Message) -> str:
        """Text or caption of a message, tolerant of partially-populated objects."""
        return getattr(message, 'text', None) or getattr(message, 'caption', None) or ''

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
        urls = self.extract_urls(self._message_text(message))

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

    async def _resolve_platform(self, url: str, any_site: bool) -> Optional[str]:
        """Named platform, or (when allowed) any site yt-dlp has an extractor for."""
        platform = self.downloader.detect_platform(url)
        if platform or not any_site:
            return platform
        # Scanning ~1800 extractors is quick but not free; keep it off the loop.
        return await asyncio.to_thread(self.downloader.generic_site, url)

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

        text = self._message_text(message)
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

        if not await self.authorized(message):
            await self._deny(message)
            return

        has_download_tag = config.DOWNLOAD_TAG in text.lower()
        any_site = config.TRY_ANY_LINK and chat_type == 'private'

        supported: List[Tuple[str, str]] = []
        for url in urls:
            platform = await self._resolve_platform(url, any_site=any_site)
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
                platforms = ', '.join(sorted({platform_label(p) for _, p in supported}))
                await message.reply_text(
                    f"Found a {esc(platforms)} link. Add <code>{esc(config.DOWNLOAD_TAG)}</code> "
                    f"to download it.",
                    parse_mode=ParseMode.HTML,
                )
            return

        # Run links concurrently; the semaphore keeps actual downloads bounded.
        await asyncio.gather(
            *(self.process_video_url(message, url, any_site=any_site) for url, _ in to_process),
            return_exceptions=True,
        )

    def _cancel_markup(self, job: ActiveJob) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB_CANCEL}:{job.job_id}")
        ]])

    async def process_video_url(self, message: Message, url: str, *, mode: str = MODE_VIDEO,
                                user_id: Optional[int] = None,
                                any_site: Optional[bool] = None) -> None:
        """Process a URL and send the downloaded video, album, audio or screenshot

        Args:
            message: Message to reply to (the user's, or the bot's own when a
                button under a video was pressed)
            url: URL to process (video URL or Threads post URL)
            mode: MODE_VIDEO, MODE_AUDIO or MODE_HD
            user_id: Who asked, when it is not ``message``'s sender
            any_site: Accept links outside the named platforms
        """
        if any_site is None:
            any_site = config.TRY_ANY_LINK and message.chat.type == 'private'
        user_id = user_id if user_id is not None else self._user_id(message)
        platform = await self._resolve_platform(url, any_site=any_site) or 'unknown'
        chat_id = message.chat.id
        chat_title = message.chat.title or "Private Chat"
        logger.info("Processing %s %s URL %s for chat %s (%s)",
                    mode, platform, url, chat_title, chat_id)

        cache_key = self._cache_key(url, mode)
        if await self._send_from_cache(message, cache_key, mode):
            return

        if self._restart_task is not None:
            # yt-dlp imports extractors lazily: a download started now could
            # load modules of the freshly installed version into the old one.
            await message.reply_text(
                "🔄 I'm restarting to load an updated downloader. "
                "Send the link again in a minute."
            )
            return

        notice = self._rate_limit_notice(message, user_id)
        if notice:
            await message.reply_text(notice)
            return

        job = ActiveJob(job_id=uuid.uuid4().hex[:12], user_id=user_id, chat_id=chat_id, url=url)
        self.jobs[job.job_id] = job
        markup = self._cancel_markup(job)
        try:
            processing_msg = await message.reply_text(
                f"🔎 Found a <b>{esc(platform_label(platform))}</b> link…",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )

            semaphore = self.download_semaphore()
            if semaphore.locked():
                await self.safe_edit(
                    processing_msg,
                    f"⏳ Queued — {self.stats.active} download(s) in progress…",
                    reply_markup=markup,
                )

            result: Optional[Dict[str, Any]] = None
            try:
                async with semaphore:
                    if job.cancel_event.is_set():
                        await self.safe_edit(processing_msg, "✖️ Cancelled.")
                        return
                    self.stats.active += 1
                    try:
                        result = await self._download(message, processing_msg, url, platform,
                                                      mode=mode, job=job, any_site=any_site)
                    finally:
                        self.stats.active -= 1

                if result is None:
                    return

                await self._deliver(message, processing_msg, result, platform,
                                    mode=mode, cache_key=cache_key)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Error processing %s: %s", url, exc, exc_info=True)
                self.stats.record_failure(platform, str(exc))
                self._note_failure(platform, 'unexpected', str(exc))
                await self.safe_edit(
                    processing_msg,
                    "❌ An unexpected error occurred while processing that link.",
                )
                self._cleanup_result(result)
        finally:
            self.jobs.pop(job.job_id, None)

    async def _download(self, message: Message, processing_msg: Message, url: str,
                        platform: str, mode: str = MODE_VIDEO,
                        job: Optional[ActiveJob] = None,
                        any_site: bool = False) -> Optional[Dict[str, Any]]:
        """Run the blocking download off the event loop and normalize the outcome.

        Returns the successful result dict, or None when the failure has already
        been reported to the user.
        """
        privileged = self.is_privileged(message)
        if privileged:
            logger.info("Chat %s is privileged: bypassing the download size limit", message.chat.id)

        markup = self._cancel_markup(job) if job else None
        verb = "Fetching the audio from" if mode == MODE_AUDIO else "Downloading from"
        await self.safe_edit(processing_msg, f"⬇️ {verb} <b>{esc(platform_label(platform))}</b>…",
                             reply_markup=markup)

        reporter = ProgressReporter(
            self, processing_msg, asyncio.get_running_loop(), platform, reply_markup=markup,
        )
        cancel_event = job.cancel_event if job else None

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.downloader.download_video, url,
                    ignore_size_limit=privileged, progress_callback=reporter,
                    mode=mode, cancel_event=cancel_event, any_site=any_site,
                ),
                timeout=config.DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # wait_for cannot stop the worker thread; the event can.
            if cancel_event is not None:
                cancel_event.set()
            logger.error("Download timed out after %ss: %s", config.DOWNLOAD_TIMEOUT_SECONDS, url)
            self.stats.record_failure(platform, 'timeout')
            await self.safe_edit(
                processing_msg,
                f"⏱️ Download timed out after "
                f"{format_duration(config.DOWNLOAD_TIMEOUT_SECONDS)}. Try a shorter video.",
            )
            return None

        if not result:
            logger.warning("Download returned nothing for %s", url)
            self.stats.record_failure(platform, 'no result')
            await self.safe_edit(processing_msg, f"❌ {esc(ERROR_MESSAGES['download_failed'])}")
            return None

        if 'error' in result:
            await self._report_error(processing_msg, result, url)
            return None

        # A privileged download can still be too big for the Bot API to accept.
        # Album items are each checked against the limit by the downloader.
        filesize = result.get('filesize') or 0
        if not result.get('is_album') and filesize > config.MAX_UPLOAD_SIZE_MB * MB:
            logger.warning("Downloaded %.1fMB exceeds the %dMB upload limit",
                           filesize / MB, config.MAX_UPLOAD_SIZE_MB)
            self.stats.record_failure(platform, 'above upload limit')
            self._cleanup_result(result)
            hint = ("" if config.USE_LOCAL_BOT_API else
                    "\n\n<i>Uploads above 50MB need a self-hosted Bot API server "
                    "(TELEGRAM_API_BASE_URL).</i>")
            await self.safe_edit(
                processing_msg,
                f"❌ The video is {format_size(filesize)}, above the "
                f"{config.MAX_UPLOAD_SIZE_MB}MB upload limit.{hint}",
            )
            return None

        logger.info("Downloaded %s (%s) -> %s", result.get('title'), format_size(filesize),
                    result.get('filepath') or f"{len(result.get('items') or [])} items")
        return result

    async def _report_error(self, processing_msg: Message, result: Dict[str, Any],
                            url: str) -> None:
        """Turn a downloader error dict into a single user-facing message."""
        code = result.get('error', 'download_failed')
        if code == 'cancelled':
            logger.info("Download cancelled by the user: %s", url)
            await self.safe_edit(processing_msg, "✖️ Cancelled.")
            return

        text = result.get('message') or ERROR_MESSAGES.get(code, ERROR_MESSAGES['download_failed'])
        platform = result.get('platform') or 'unknown'
        logger.warning("Download failed (%s) for %s: %s", code, url, text)
        self.stats.record_failure(platform, result.get('detail') or code)
        self._note_failure(platform, code, result.get('detail') or text)

        if code == 'file_too_large' and result.get('filesize'):
            text = f"{text}\nVideo size: {format_size(result['filesize'])}"

        await self.safe_edit(processing_msg, f"❌ {esc(text)}")

    def _cleanup_result(self, result: Optional[Dict[str, Any]]) -> None:
        if not result:
            return
        self.downloader.cleanup_file(result.get('filepath'))
        for item in result.get('items') or []:
            self.downloader.cleanup_file(item.get('filepath'))

    async def _deliver(self, message: Message, processing_msg: Optional[Message],
                       result: Dict[str, Any], platform: str, *, mode: str = MODE_VIDEO,
                       cache_key: Optional[str] = None, buttons: bool = True) -> None:
        """Send the downloaded artefact and clean up afterwards, success or not."""
        try:
            if buttons:
                result['reply_markup'] = self._media_buttons(message, result, mode)

            if result.get('is_screenshot'):
                sent = await self._send_screenshot(message, result, processing_msg)
            elif result.get('is_album'):
                sent = await self._send_album(message, result, processing_msg)
            elif result.get('kind') == 'photo':
                sent = await self._send_photo(message, result, processing_msg)
            elif result.get('kind') == 'audio':
                sent = await self._send_audio(message, result, processing_msg)
            else:
                sent = await self._send_video_with_retry(message, result, processing_msg)

            if sent:
                self.stats.record_success(platform, result.get('filesize') or 0)
                self._note_success(platform)
                self._cache_delivery(cache_key, result)
                await self.safe_delete(processing_msg)
                logger.info("Successfully delivered %s from %s", result.get('title'), platform)
            else:
                self.stats.record_failure(platform, 'upload failed')
        finally:
            # The files are ours either way; never leave them behind.
            self._cleanup_result(result)

    # -- buttons under media ---------------------------------------------

    def _media_buttons(self, message: Message, result: Dict[str, Any],
                       mode: str) -> Optional[InlineKeyboardMarkup]:
        """🎵 Audio (and 🔁 HD for privileged chats) under a delivered video."""
        if result.get('is_screenshot') or result.get('is_album') or mode == MODE_AUDIO:
            return None
        if result.get('kind', 'video') not in ('video', 'animation', 'document'):
            return None
        url = result.get('url')
        if not url:
            return None
        token = self.storage.token_for(url)
        row = [InlineKeyboardButton("🎵 Audio", callback_data=f"{CB_AUDIO}:{token}")]
        if mode == MODE_VIDEO and result.get('hd_available') and self.is_privileged(message):
            row.append(InlineKeyboardButton("🔁 HD", callback_data=f"{CB_HD}:{token}"))
        return InlineKeyboardMarkup([row])

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle presses on Cancel, 🎵 Audio and 🔁 HD."""
        query = update.callback_query
        if query is None:
            return
        action, _, arg = (query.data or '').partition(':')
        user_id = getattr(query.from_user, 'id', None)

        if action in (CB_GRANT, CB_IGNORE, CB_FORGET):
            await self._access_button(query, action, arg, user_id)
            return

        if action == CB_CANCEL:
            job = self.jobs.get(arg)
            if job is None:
                await query.answer("That download already finished.")
                return
            if user_id != job.user_id and job.user_id is not None and not self.access.is_admin(user_id):
                await query.answer("Only the person who sent the link can cancel it.")
                return
            job.cancel_event.set()
            await query.answer("Cancelling…")
            return

        message = query.message
        if not isinstance(message, Message):
            await query.answer("Open the chat with the bot to use this button.")
            return
        if not (self.access.is_allowed(message.chat.id, user_id)
                or (message.chat.type == 'private' and await self.is_group_member(user_id))):
            await query.answer("This bot is private.", show_alert=True)
            return

        url = self.storage.url_for(arg)
        if not url:
            await query.answer("This button has expired. Send the link again.")
            return

        if action == CB_AUDIO:
            await query.answer("Getting the audio…")
            await self.process_video_url(message, url, mode=MODE_AUDIO, user_id=user_id,
                                         any_site=True)
        elif action == CB_HD:
            if not self.is_privileged(message):
                await query.answer("Higher quality is only available in the owner's chat.",
                                   show_alert=True)
                return
            await query.answer("Getting a higher quality version…")
            await self.process_video_url(message, url, mode=MODE_HD, user_id=user_id,
                                         any_site=True)
        else:
            await query.answer()

    # -- cache -------------------------------------------------------------

    def _cache_key(self, url: str, mode: str) -> str:
        return f"{mode}:{self.downloader.canonical_id(url)}"

    def _cache_delivery(self, key: Optional[str], result: Dict[str, Any]) -> None:
        sent = result.get('telegram_file')
        if not key or not sent or config.CACHE_TTL_DAYS <= 0 or result.get('is_screenshot'):
            return
        payload = dict(
            sent,
            url=result.get('url'),
            title=result.get('title'),
            platform=result.get('platform'),
            filesize=result.get('filesize') or 0,
            compressed=bool(result.get('compressed')),
            hd_available=bool(result.get('hd_available')),
        )
        try:
            self.storage.cache_put(key, payload)
        except Exception as exc:  # the cache is an optimisation, never a failure
            logger.warning("Could not cache %s: %s", key, exc)

    def _cached(self, key: str) -> Optional[Dict[str, Any]]:
        if config.CACHE_TTL_DAYS <= 0:
            return None
        return self.storage.cache_get(key, max_age_seconds=config.CACHE_TTL_DAYS * 86400)

    async def _send_from_cache(self, message: Message, key: str, mode: str) -> bool:
        """Resend an already-uploaded link by file ID. Returns True when it worked."""
        payload = self._cached(key)
        if not payload:
            return False
        # A privileged chat on a local Bot API could get the full-size original.
        if payload.get('compressed') and self.is_privileged(message) and config.USE_LOCAL_BOT_API:
            return False

        markup = self._media_buttons(message, payload, mode)
        try:
            await self._resend(message, payload, markup)
        except BadRequest as exc:
            logger.info("Cached file for %s is no longer valid (%s); downloading again", key, exc)
            self.storage.cache_delete(key)
            return False
        except TelegramError as exc:
            logger.warning("Could not resend %s from the cache: %s", key, exc)
            return False

        logger.info("Sent %s from the cache", key)
        self.stats.cache_hits += 1
        self.stats.record_success(payload.get('platform') or 'unknown', 0)
        return True

    async def _resend(self, message: Message, payload: Dict[str, Any],
                      markup: Optional[InlineKeyboardMarkup]) -> None:
        kind = payload.get('kind')
        file_id = payload.get('file_id')
        caption = payload.get('caption')
        spoiler = bool(payload.get('has_spoiler'))
        common = {'caption': caption, 'parse_mode': ParseMode.HTML}

        if kind == 'video':
            await message.reply_video(video=file_id, supports_streaming=True,
                                      has_spoiler=spoiler, reply_markup=markup, **common)
        elif kind == 'animation':
            await message.reply_animation(animation=file_id, has_spoiler=spoiler,
                                          reply_markup=markup, **common)
        elif kind == 'document':
            await message.reply_document(document=file_id, reply_markup=markup, **common)
        elif kind == 'photo':
            await message.reply_photo(photo=file_id, has_spoiler=spoiler, **common)
        elif kind == 'audio':
            await message.reply_audio(audio=file_id, **common)
        elif kind == 'album':
            entries = [AlbumEntry(kind=item['kind'], source=item['file_id'], is_file=False)
                       for item in payload.get('items') or []]
            sent = await self._send_media_groups(message, entries, caption, spoiler)
            if sent is None:
                raise TelegramError("album resend failed")
        else:
            raise BadRequest(f"unknown cached kind {kind!r}")

    @staticmethod
    def _file_id(sent: Any, kind: str) -> Optional[str]:
        if sent is None:
            return None
        if kind == 'photo':
            photos = getattr(sent, 'photo', None)
            media = photos[-1] if isinstance(photos, (list, tuple)) and photos else None
        else:
            media = getattr(sent, kind, None)
        file_id = getattr(media, 'file_id', None)
        return file_id if isinstance(file_id, str) else None

    def _record_sent(self, result: Dict[str, Any], sent: Any, kinds: Iterable[str],
                     caption: Optional[str], has_spoiler: bool = False) -> None:
        """Remember the file ID Telegram assigned, for the cache and inline mode."""
        for kind in kinds:
            file_id = self._file_id(sent, kind)
            if file_id:
                result['telegram_file'] = {'kind': kind, 'file_id': file_id,
                                           'caption': caption, 'has_spoiler': has_spoiler}
                return

    # -- sending ---------------------------------------------------------

    @staticmethod
    def _spoiler(result: Dict[str, Any]) -> bool:
        return config.SPOILER_ADULT_CONTENT and (result.get('age_limit') or 0) >= 18

    async def _send_screenshot(self, message: Message, result: Dict[str, Any],
                               processing_msg: Optional[Message]) -> bool:
        """Send a Threads screenshot as a photo."""
        return await self._send_photo(message, result, processing_msg, icon="📸",
                                      force_photo=True)

    async def _send_photo(self, message: Message, result: Dict[str, Any],
                          processing_msg: Optional[Message], icon: str = "🖼️",
                          force_photo: bool = False) -> bool:
        """Send one image; files Telegram will not take as a photo go as documents."""
        path = result['filepath']
        size = result.get('filesize') or 0
        caption = self._create_caption(result, size / MB, icon=icon)
        spoiler = self._spoiler(result)
        as_photo = force_photo or (size <= TELEGRAM_PHOTO_LIMIT_BYTES
                                   and path.lower().endswith(('.jpg', '.jpeg', '.png')))

        holder: List[Any] = []
        if as_photo:
            async def send():
                holder.append(await message.reply_photo(
                    photo=path, caption=caption, parse_mode=ParseMode.HTML,
                    has_spoiler=spoiler, **MEDIA_TIMEOUTS,
                ))
        else:
            async def send():
                holder.append(await message.reply_document(
                    document=path, caption=caption, parse_mode=ParseMode.HTML, **MEDIA_TIMEOUTS,
                ))

        error = await self._attempt_send(send)
        if error is None:
            self._record_sent(result, holder[-1] if holder else None,
                              ('photo',) if as_photo else ('document',), caption, spoiler)
            return True

        logger.error("Failed to send image: %s", error)
        what = "screenshot" if result.get('is_screenshot') else "image"
        await self.safe_edit(processing_msg, f"❌ Failed to send the {what}: {esc(error)}")
        return False

    async def _send_audio(self, message: Message, result: Dict[str, Any],
                          processing_msg: Optional[Message]) -> bool:
        path = result['filepath']
        size = result.get('filesize') or 0
        caption = self._create_caption(result, size / MB, icon="🎵")
        await self.safe_edit(processing_msg, f"📤 Uploading {format_size(size)}…")

        if path.lower().endswith(('.m4a', '.mp3')):
            kwargs: Dict[str, Any] = {
                'audio': path, 'caption': caption, 'parse_mode': ParseMode.HTML,
                'title': tg_truncate(str(result.get('title') or 'Audio'), 64),
                **MEDIA_TIMEOUTS,
            }
            if result.get('uploader'):
                kwargs['performer'] = tg_truncate(str(result['uploader']), 64)
            duration = result.get('duration')
            if isinstance(duration, (int, float)) and duration > 0:
                kwargs['duration'] = int(duration)
            method, kinds = message.reply_audio, ('audio', 'document')
        else:
            # Opus/WebM audio (no ffmpeg to convert it) does not play as music.
            kwargs = {'document': path, 'caption': caption, 'parse_mode': ParseMode.HTML,
                      **MEDIA_TIMEOUTS}
            method, kinds = message.reply_document, ('document',)

        holder: List[Any] = []

        async def send():
            holder.append(await method(**kwargs))

        error = await self._attempt_send(send)
        if error is None:
            self._record_sent(result, holder[-1] if holder else None, kinds, caption)
            return True
        logger.error("Failed to send audio: %s", error)
        await self.safe_edit(processing_msg, f"❌ Failed to send the audio: {esc(error)}")
        return False

    async def _send_video_with_retry(self, message: Message, result: Dict[str, Any],
                                     processing_msg: Optional[Message]) -> bool:
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
                                processing_msg: Optional[Message], file_size_mb: float) -> bool:
        """Send file as document with proper error handling"""
        await self.safe_edit(
            processing_msg, f"📤 Uploading {format_size(result.get('filesize'))}…"
        )
        caption = self._create_caption(result, file_size_mb, is_document=True)
        send_kwargs: Dict[str, Any] = {
            'document': result['filepath'],
            'caption': caption,
            'parse_mode': ParseMode.HTML,
            **MEDIA_TIMEOUTS,
        }
        if result.get('reply_markup') is not None:
            send_kwargs['reply_markup'] = result['reply_markup']

        holder: List[Any] = []

        async def send():
            holder.append(await message.reply_document(**send_kwargs))

        error = await self._attempt_send(send)
        if error is None:
            logger.info("Document sent successfully (%.1fMB)", file_size_mb)
            self._record_sent(result, holder[-1] if holder else None, ('document',), caption)
            return True

        logger.error("Failed to send document: %s", error)
        await self.safe_edit(processing_msg, f"❌ Failed to send the file: {esc(error)}")
        return False

    async def _send_as_video(self, message: Message, result: Dict[str, Any],
                             processing_msg: Optional[Message],
                             file_size_mb: float) -> Optional[BaseException]:
        """Send file as video. Returns None on success, else the last error."""
        await self.safe_edit(
            processing_msg, f"📤 Uploading {format_size(result.get('filesize'))}…"
        )

        spoiler = self._spoiler(result)
        send_kwargs: Dict[str, Any] = {
            'video': result['filepath'],
            'caption': self._create_caption(result, file_size_mb),
            'parse_mode': ParseMode.HTML,
            'supports_streaming': True,
            **MEDIA_TIMEOUTS,
        }
        if spoiler:
            send_kwargs['has_spoiler'] = True
        if result.get('reply_markup') is not None:
            send_kwargs['reply_markup'] = result['reply_markup']

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

        holder: List[Any] = []

        async def send():
            holder.append(await message.reply_video(**send_kwargs))

        try:
            error = await self._attempt_send(send)
        finally:
            self.downloader.cleanup_file(thumbnail)

        if error is None:
            logger.info("Video sent successfully (%.1fMB)", file_size_mb)
            # Telegram files short silent clips as animations (GIFs).
            self._record_sent(result, holder[-1] if holder else None,
                              ('video', 'animation', 'document'), send_kwargs['caption'], spoiler)
        else:
            logger.error("Failed to send video: %s", error)
        return error

    # -- albums ------------------------------------------------------------

    async def _send_album(self, message: Message, result: Dict[str, Any],
                          processing_msg: Optional[Message]) -> bool:
        """Send a multi-item post as one or more Telegram albums."""
        items = result.get('items') or []
        total = result.get('filesize') or sum(item.get('filesize') or 0 for item in items)
        caption = self._create_caption(result, total / MB, icon="🖼️")
        spoiler = self._spoiler(result)
        await self.safe_edit(processing_msg,
                             f"📤 Uploading {len(items)} items ({format_size(total)})…")

        entries: List[AlbumEntry] = []
        for item in items:
            path = item['filepath']
            size = item.get('filesize') or 0
            kind = item.get('kind', 'video')
            if kind == 'photo' and (size > TELEGRAM_PHOTO_LIMIT_BYTES
                                    or not path.lower().endswith(('.jpg', '.jpeg', '.png'))):
                kind = 'document'
            elif kind == 'video' and size > config.TELEGRAM_VIDEO_SIZE_LIMIT_MB * MB:
                kind = 'document'
            entries.append(AlbumEntry(kind=kind, source=path, size=size,
                                      width=item.get('width'), height=item.get('height'),
                                      duration=item.get('duration')))

        sent = await self._send_media_groups(message, entries, caption, spoiler)
        if sent is None:
            await self.safe_edit(processing_msg, "❌ Failed to send the album.")
            return False
        result['telegram_file'] = {'kind': 'album', 'items': sent,
                                   'caption': caption, 'has_spoiler': spoiler}
        return True

    @staticmethod
    def _album_groups(entries: List[AlbumEntry]) -> List[List[AlbumEntry]]:
        """Split into sendable groups: photos/videos together (≤10, ≤ upload
        limit per request), documents on their own."""
        limit = config.MAX_UPLOAD_SIZE_MB * MB
        groups: List[List[AlbumEntry]] = []
        current: List[AlbumEntry] = []
        current_size = 0
        for entry in entries:
            if entry.kind == 'document':
                if current:
                    groups.append(current)
                    current, current_size = [], 0
                groups.append([entry])
                continue
            if current and (len(current) >= MAX_MEDIA_GROUP or current_size + entry.size > limit):
                groups.append(current)
                current, current_size = [], 0
            current.append(entry)
            current_size += entry.size
        if current:
            groups.append(current)
        return groups

    def _input_media(self, entry: AlbumEntry, caption: Optional[str], spoiler: bool):
        """InputMedia for an album item.

        python-telegram-bot turns a path inside InputMedia into a file:// URI,
        which only a local Bot API server can read, so files are passed as bytes.
        """
        filename = None
        media: Any = entry.source
        if entry.is_file and not config.USE_LOCAL_BOT_API:
            media = Path(entry.source).read_bytes()
            filename = os.path.basename(entry.source)
        common: Dict[str, Any] = {'caption': caption, 'parse_mode': ParseMode.HTML}
        if filename:
            common['filename'] = filename

        if entry.kind == 'photo':
            return InputMediaPhoto(media=media, has_spoiler=spoiler, **common)
        if entry.kind == 'video':
            extra: Dict[str, Any] = {}
            if entry.width and entry.height:
                extra.update(width=int(entry.width), height=int(entry.height))
            if entry.duration:
                extra['duration'] = int(entry.duration)
            return InputMediaVideo(media=media, supports_streaming=True, has_spoiler=spoiler,
                                   **extra, **common)
        return InputMediaDocument(media=media, **common)

    async def _send_single(self, message: Message, entry: AlbumEntry, caption: Optional[str],
                           spoiler: bool) -> Tuple[Optional[Dict[str, str]], Optional[BaseException]]:
        common: Dict[str, Any] = {'caption': caption, 'parse_mode': ParseMode.HTML, **MEDIA_TIMEOUTS}
        if entry.kind == 'photo':
            method, kwargs, kinds = message.reply_photo, {'photo': entry.source, 'has_spoiler': spoiler}, ('photo',)
        elif entry.kind == 'video':
            kwargs = {'video': entry.source, 'supports_streaming': True, 'has_spoiler': spoiler}
            if entry.width and entry.height:
                kwargs.update(width=int(entry.width), height=int(entry.height))
            if entry.duration:
                kwargs['duration'] = int(entry.duration)
            method, kinds = message.reply_video, ('video', 'animation', 'document')
        else:
            method, kwargs, kinds = message.reply_document, {'document': entry.source}, ('document',)

        holder: List[Any] = []

        async def send():
            holder.append(await method(**kwargs, **common))

        error = await self._attempt_send(send)
        if error is not None:
            return None, error
        sent = holder[-1] if holder else None
        for kind in kinds:
            file_id = self._file_id(sent, kind)
            if file_id:
                return {'kind': kind, 'file_id': file_id}, None
        return {'kind': entry.kind, 'file_id': ''}, None

    async def _send_media_groups(self, message: Message, entries: List[AlbumEntry],
                                 caption: Optional[str], spoiler: bool) -> Optional[List[Dict[str, str]]]:
        """Send entries as albums. Returns the sent file IDs, or None if nothing went out."""
        sent_files: List[Dict[str, str]] = []
        for group_index, group in enumerate(self._album_groups(entries)):
            group_caption = caption if group_index == 0 else None
            if len(group) == 1:
                sent, error = await self._send_single(message, group[0], group_caption, spoiler)
                if error is not None:
                    logger.error("Failed to send album item: %s", error)
                    if not sent_files:
                        return None
                    continue
                sent_files.append(sent)
                continue

            media = [self._input_media(entry, group_caption if i == 0 else None, spoiler)
                     for i, entry in enumerate(group)]
            holder: List[Any] = []

            async def send(media=media):
                holder.append(await message.reply_media_group(media=media, **MEDIA_TIMEOUTS))

            error = await self._attempt_send(send)
            if error is not None:
                logger.error("Failed to send album: %s", error)
                if not sent_files:
                    return None
                continue
            messages = holder[-1] if holder and isinstance(holder[-1], (list, tuple)) else ()
            for entry, sent in zip(group, messages):
                file_id = None
                for kind in ((entry.kind,) if entry.kind != 'video' else ('video', 'animation')):
                    file_id = self._file_id(sent, kind)
                    if file_id:
                        sent_files.append({'kind': 'video' if kind == 'animation' else kind,
                                           'file_id': file_id})
                        break
        return sent_files

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

    # -- captions ----------------------------------------------------------

    @staticmethod
    def _clean_description(description: Optional[str], title: str) -> Optional[str]:
        """The post text worth showing under the title, or None if it adds nothing."""
        if not description:
            return None
        text = re.sub(r'\n{3,}', '\n\n', str(description)).strip()
        if not text:
            return None
        bare_title = title.rstrip('…').strip()
        # TikTok and Twitter titles are the post text itself, sometimes cut
        # short. Only show the description when it says more than the title.
        if text == bare_title or text in bare_title:
            return None
        if text[:60] in bare_title and len(text) <= len(bare_title) + 10:
            return None
        return text

    def _create_caption(self, result: Dict[str, Any], file_size_mb: float,
                        is_document: bool = False, icon: Optional[str] = None) -> str:
        """Create formatted caption for video/document, trimmed to Telegram's limit

        Layout:  icon + title / 👤 author / collapsed description / details /
        notes / source link. Telegram counts only visible text (after parsing
        the HTML) towards its 1024 character limit, so the budget is computed
        on the raw strings and the title then the description get what is left.
        """
        if icon is None:
            icon = "📁" if is_document else "🎥"

        details = [f"📱 {platform_label(result.get('platform') or 'unknown')}"]
        duration_text = format_duration(result.get('duration'))
        if duration_text:
            details.append(f"⏱️ {duration_text}")
        details.append(f"📊 {file_size_mb:.1f}MB")
        if result.get('is_album'):
            details.append(f"🖼️ {len(result.get('items') or [])} items")

        notes: List[str] = []
        if is_document:
            notes.append(f"⚠️ Sent as a file because it exceeds {config.TELEGRAM_VIDEO_SIZE_LIMIT_MB}MB")
        if result.get('compressed'):
            notes.append(f"🗜️ Compressed to fit {config.MAX_UPLOAD_SIZE_MB}MB")
        if result.get('skipped'):
            notes.append(f"⚠️ {result['skipped']} item(s) could not be downloaded")

        footer_text = "\n\n" + " · ".join(details) + "".join("\n" + note for note in notes)
        footer_html = "\n\n" + esc(" · ".join(details)) + "".join("\n" + esc(note) for note in notes)

        source_url = result.get('url')
        source_html = f'\n<a href="{esc_attr(source_url)}">source</a>' if source_url else ''
        source_visible = tg_len("\nsource") if source_url else 0

        author_html, author_visible = '', 0
        uploader = result.get('uploader')
        if config.CAPTION_AUTHOR and uploader:
            name = tg_truncate(str(uploader).strip(), 64)
            uploader_url = result.get('uploader_url')
            author_html = (f'\n👤 <a href="{esc_attr(uploader_url)}">{esc(name)}</a>'
                           if uploader_url else f"\n👤 {esc(name)}")
            author_visible = tg_len(f"\n👤 {name}")

        budget = (config.MAX_CAPTION_LENGTH - tg_len(icon) - 1 - tg_len(footer_text)
                  - source_visible - author_visible)

        # Titles come from the platform and can be arbitrarily long; Telegram
        # rejects the whole message once the caption passes 1024 characters.
        title = str(result.get('title') or 'Video').strip()
        title = tg_truncate(title, max(1, budget))
        remaining = budget - tg_len(title)

        description_html = ''
        description = (self._clean_description(result.get('description'), title)
                       if config.CAPTION_DESCRIPTION else None)
        # "\n" before the quote; not worth it for a handful of characters.
        if description and remaining >= 40:
            description = tg_truncate(description, remaining - 1)
            description_html = f"\n<blockquote expandable>{esc(description)}</blockquote>"

        return (f"{icon} <b>{esc(title)}</b>{author_html}{description_html}"
                f"{footer_html}{source_html}")

    # -- inline mode -------------------------------------------------------

    def _inline_results(self, payload: Dict[str, Any], key: str) -> List[Any]:
        """Turn a cache entry into inline results (one per album item)."""
        base_id = hashlib.sha1(key.encode()).hexdigest()[:24]
        title = tg_truncate(str(payload.get('title') or 'Video'), 100)
        description = platform_label(payload.get('platform'))
        caption = payload.get('caption')
        items = payload.get('items') if payload.get('kind') == 'album' else [payload]

        results = []
        for index, item in enumerate(items or []):
            file_id, kind = item.get('file_id'), item.get('kind')
            if not file_id:
                continue
            common = {'id': f"{base_id}_{index}", 'parse_mode': ParseMode.HTML,
                      'caption': caption if index == 0 else None}
            item_title = title if len(items) == 1 else f"{title} ({index + 1}/{len(items)})"
            if kind == 'video':
                results.append(InlineQueryResultCachedVideo(
                    video_file_id=file_id, title=item_title, description=description, **common))
            elif kind == 'animation':
                results.append(InlineQueryResultCachedMpeg4Gif(
                    mpeg4_file_id=file_id, title=item_title, **common))
            elif kind == 'photo':
                results.append(InlineQueryResultCachedPhoto(
                    photo_file_id=file_id, title=item_title, description=description, **common))
            elif kind == 'audio':
                results.append(InlineQueryResultCachedAudio(audio_file_id=file_id, **common))
            elif kind == 'document':
                results.append(InlineQueryResultCachedDocument(
                    document_file_id=file_id, title=item_title, description=description, **common))
        return results

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """@bot <link>: share an already-downloaded video from any chat.

        Inline answers must come back within seconds and can only point at
        files Telegram already has, so links not in the cache get a button that
        opens a private chat to download them (and, with CACHE_CHAT_ID set, a
        background download so the next attempt is instant).
        """
        query = update.inline_query
        if query is None:
            return
        user_id = getattr(query.from_user, 'id', None)

        if not (self.access.is_allowed(None, user_id) or await self.is_group_member(user_id)):
            await query.answer([], cache_time=300, is_personal=True,
                               button=InlineQueryResultsButton(text="🔒 This bot is private",
                                                               start_parameter="private"))
            return

        urls = self.extract_urls(query.query or '')
        if not urls:
            await query.answer([], cache_time=5, is_personal=True,
                               button=InlineQueryResultsButton(text="Paste a video link",
                                                               start_parameter="help"))
            return

        url = urls[0]
        platform = await self._resolve_platform(url, any_site=config.TRY_ANY_LINK)
        if not platform:
            await query.answer([], cache_time=60, is_personal=True,
                               button=InlineQueryResultsButton(text="🤔 Unsupported link",
                                                               start_parameter="help"))
            return

        key = self._cache_key(url, MODE_VIDEO)
        payload = self._cached(key)
        if payload:
            results = self._inline_results(payload, key)
            if results:
                await query.answer(results, cache_time=300, is_personal=False)
                return

        token = self.storage.token_for(url)
        text = "⬇️ Download it in a private chat first"
        if config.CACHE_CHAT_ID and self._start_inline_prefetch(url, key, platform, user_id):
            text = "⏳ Preparing… type the link again in a moment"
        await query.answer([], cache_time=0, is_personal=True,
                           button=InlineQueryResultsButton(text=text,
                                                           start_parameter=DEEP_LINK_PREFIX + token))

    def _start_inline_prefetch(self, url: str, key: str, platform: str,
                               user_id: Optional[int]) -> bool:
        if key in self._prefetching:
            return True
        if self.application is None:
            return False
        if self.rate_limiter.enabled and not self.access.is_admin(user_id) \
                and not self.access.is_exempt_from_limits(None, user_id):
            allowed, _ = self.rate_limiter.try_acquire(user_id)
            if not allowed:
                return False
        task = self._spawn(self._inline_prefetch(url, key, platform))
        self._prefetching[key] = task
        task.add_done_callback(lambda _task: self._prefetching.pop(key, None))
        return True

    async def _inline_prefetch(self, url: str, key: str, platform: str) -> None:
        """Download a link and upload it to CACHE_CHAT_ID so inline mode can share it."""
        logger.info("Preparing %s for inline mode", url)
        result = None
        try:
            async with self.download_semaphore():
                result = await asyncio.wait_for(
                    asyncio.to_thread(self.downloader.download_video, url,
                                      any_site=config.TRY_ANY_LINK),
                    timeout=config.DOWNLOAD_TIMEOUT_SECONDS,
                )
            if not result or 'error' in result:
                logger.info("Inline prefetch of %s failed: %s", url,
                            (result or {}).get('message'))
                return
            target = ChatTarget(self.application.bot, config.CACHE_CHAT_ID)
            await self._deliver(target, None, result, platform, cache_key=key, buttons=False)
        except Exception as exc:
            logger.warning("Inline prefetch of %s failed: %s", url, exc)
            self._cleanup_result(result)

    # -- maintenance -------------------------------------------------------

    async def check_for_update(self, manual: bool = False) -> str:
        """Update yt-dlp if a newer release exists. Returns a human summary."""
        current = updater.installed_version()
        latest = await asyncio.to_thread(updater.latest_version)
        self.storage.meta_set('ytdlp_last_check', str(time.time()))
        if latest:
            self._latest_ytdlp = latest
        if latest is None:
            return f"Could not reach PyPI. Installed: yt-dlp {current}."
        if not updater.is_newer(latest, current):
            return f"yt-dlp {current} is the latest release."

        allowed, reason = updater.can_self_update()
        if not allowed:
            age = updater.version_age_days(current) or 0
            last_notice = float(self.storage.meta_get('ytdlp_stale_notice_at') or 0)
            if not manual and age >= config.YTDLP_STALE_DAYS and time.time() - last_notice > 7 * 86400:
                self.storage.meta_set('ytdlp_stale_notice_at', str(time.time()))
                await self.notify_admins(
                    f"⚠️ yt-dlp {esc(current)} is {age} days old and {esc(latest)} is out, "
                    f"but it can't be updated in place: {esc(reason)}"
                )
            return f"yt-dlp {latest} is available, but it can't be updated here: {reason}."

        result = await asyncio.to_thread(updater.upgrade, current)
        if result.changed:
            self.storage.meta_set('pending_admin_notice',
                                  f"✅ {result.message} The bot restarted to load it.")
            self.request_restart()
            return f"{result.message} Restarting once the current downloads finish."
        if not result.ok:
            await self.notify_admins(f"⚠️ yt-dlp update failed: {esc(result.message)}")
        return result.message

    def request_restart(self) -> None:
        if self._restart_task is None:
            self._restart_task = self._spawn(self._restart_when_idle())

    async def _restart_when_idle(self) -> None:
        deadline = time.monotonic() + 1800
        while (self.stats.active or self.jobs) and time.monotonic() < deadline:
            await asyncio.sleep(5)
        logger.info("Restarting to load the updated yt-dlp")
        self._restart_requested = True
        if self.application is not None:
            self.application.stop_running()

    async def _update_loop(self) -> None:
        """Check for a new yt-dlp every YTDLP_UPDATE_INTERVAL_HOURS."""
        interval = config.YTDLP_UPDATE_INTERVAL_HOURS * 3600
        while True:
            try:
                last = float(self.storage.meta_get('ytdlp_last_check') or 0)
            except ValueError:
                last = 0
            # Restarts must not turn into an update check each time.
            await asyncio.sleep(max(60, last + interval - time.time()))
            try:
                summary = await self.check_for_update()
                logger.info("Scheduled yt-dlp check: %s", summary)
            except Exception as exc:
                logger.warning("Scheduled yt-dlp check failed: %s", exc)
                self.storage.meta_set('ytdlp_last_check', str(time.time()))

    # -- lifecycle -------------------------------------------------------

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors with detailed logging"""
        error = context.error
        update_id = getattr(update, 'update_id', 'N/A')
        logger.error("Error in update %s: %s", update_id, error, exc_info=error)

        # Nothing useful to tell the user about network blips; they'd just get spam.
        if isinstance(error, (NetworkError, TimedOut, RetryAfter)):
            return

        alert = self.error_monitor.record_failure('the bot', 'unexpected', repr(error))
        if alert:
            self._spawn(self.notify_admins(f"⚠️ Unexpected error: <code>{esc(repr(error)[:500])}</code>"))

        message = getattr(update, 'effective_message', None)
        if message is not None:
            try:
                await message.reply_text("❌ An unexpected error occurred. Please try again later.")
            except TelegramError:
                logger.debug("Could not deliver the error message to the user")

    async def _post_init(self, application: Application) -> None:
        """Register the command menu, sweep stale downloads and start maintenance."""
        removed = await asyncio.to_thread(
            self.downloader.cleanup_old_files, config.CLEANUP_MAX_AGE_HOURS
        )
        if removed:
            logger.info("Startup cleanup removed %d stale file(s)", removed)

        pruned = self.storage.prune(config.CACHE_TTL_DAYS * 86400 if config.CACHE_TTL_DAYS else None)
        if pruned:
            logger.info("Pruned %d expired cache entries and button links", pruned)

        commands = [
            BotCommand('start', 'Welcome message'),
            BotCommand('help', 'How to use the bot'),
            BotCommand('status', 'Bot status and statistics'),
            BotCommand('dl', 'Download a specific link'),
            BotCommand('audio', 'Download just the audio of a link'),
        ]
        try:
            await application.bot.set_my_commands(commands)
            for admin in config.ADMIN_CHAT_IDS:
                try:
                    await application.bot.set_my_commands(
                        commands + [BotCommand('access', 'Who can use the bot'),
                                    BotCommand('update', 'Update yt-dlp and restart')],
                        scope=BotCommandScopeChat(chat_id=int(admin)),
                    )
                except (TelegramError, ValueError) as exc:
                    logger.debug("Could not set admin commands for %s: %s", admin, exc)
        except TelegramError as exc:
            logger.warning("Could not register the command menu: %s", exc)

        if config.TRY_ANY_LINK:
            # Load yt-dlp's extractor list now rather than on the first odd link.
            self._spawn(asyncio.to_thread(self.downloader.generic_site, 'https://example.com/'))

        if config.DNS_FALLBACK_DOH_URL:
            # Find out now whether plain DNS is blocked, not on the first download.
            self._spawn(asyncio.to_thread(dns_fallback.probe))

        if config.COOKIES_FROM_BROWSER:
            # Read the browser's cookies now, while someone is likely at the
            # computer to answer a Keychain prompt, rather than mid-download.
            self._spawn(asyncio.to_thread(self.downloader.browser_cookies))

        notice = self.storage.meta_pop('pending_admin_notice')
        if notice:
            self._spawn(self.notify_admins(esc(notice)))

        if config.YTDLP_AUTO_UPDATE:
            self._spawn(self._update_loop())

    async def _post_shutdown(self, application: Application) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.to_thread(self.downloader.close)
        self.storage.close()

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
                getattr(self.application.bot.request, '_media_write_timeout', 'unknown'),
            )

            self._register_handlers()

            if config.WEBHOOK_URL:
                webhook_url = f"{config.WEBHOOK_URL}/{config.WEBHOOK_PATH}"
                logger.info("Starting webhook on %s:%s for %s",
                            config.WEBHOOK_LISTEN, config.WEBHOOK_PORT, webhook_url)
                self.application.run_webhook(
                    listen=config.WEBHOOK_LISTEN,
                    port=config.WEBHOOK_PORT,
                    url_path=config.WEBHOOK_PATH,
                    webhook_url=webhook_url,
                    secret_token=config.WEBHOOK_SECRET or None,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
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

        if self._restart_requested:
            self._exec_restart()

    @staticmethod
    def _exec_restart() -> None:
        """Replace this process with a fresh one so the new yt-dlp is imported."""
        logger.info("Restarting: %s %s", sys.executable, ' '.join(sys.argv))
        logging.shutdown()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _register_handlers(self) -> None:
        """Register all command and message handlers"""
        logger.info("Registering handlers...")

        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler(["dl", "download"], self.download_command))
        self.application.add_handler(CommandHandler(["audio", "mp3"], self.audio_command))
        self.application.add_handler(CommandHandler("update", self.update_command))
        self.application.add_handler(CommandHandler("access", self.access_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(InlineQueryHandler(self.inline_query))

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
