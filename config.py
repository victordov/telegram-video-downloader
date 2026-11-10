"""
Central configuration for the Telegram Video Downloader Bot.

Every tunable lives here so the rest of the code never touches ``os.getenv``
directly. Values are read once at import time from the environment (``.env`` is
loaded first), validated, and exposed as module level constants.
"""

import hashlib
import logging
import os
import tempfile
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MB = 1024 * 1024

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_str(name: str, default: str = '') -> str:
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> int:
    raw = _get_str(name)
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("Invalid integer for %s=%r, falling back to %s", name, raw, default)
            value = default
    if minimum is not None and value < minimum:
        logger.warning("%s=%s is below the minimum of %s, clamping", name, value, minimum)
        value = minimum
    if maximum is not None and value > maximum:
        logger.warning("%s=%s is above the maximum of %s, clamping", name, value, maximum)
        value = maximum
    return value


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_str(name).lower()
    if not raw:
        return default
    return raw in ('1', 'true', 'yes', 'y', 'on')


def _get_list(name: str) -> List[str]:
    return [item.strip() for item in _get_str(name).split(',') if item.strip()]


# --- Telegram -----------------------------------------------------------------

TELEGRAM_BOT_TOKEN = _get_str('TELEGRAM_BOT_TOKEN')

#: Base URL of a self-hosted Bot API server. Leave empty to use api.telegram.org.
#: A local server is the only way to upload files larger than 50MB.
TELEGRAM_API_BASE_URL = _get_str('TELEGRAM_API_BASE_URL')
USE_LOCAL_BOT_API = bool(TELEGRAM_API_BASE_URL)

#: Chats allowed to download files above ``MAX_DOWNLOAD_SIZE_MB``.
PRIVATE_CHAT_IDS = _get_list('PRIVATE_CHAT_IDS')

#: Tag that opts a group message into downloading.
DOWNLOAD_TAG = _get_str('DOWNLOAD_TAG', '#download').lower()

#: Platforms downloaded in groups without the tag.
GROUP_AUTO_PLATFORMS = set(
    p.lower() for p in (_get_list('GROUP_AUTO_PLATFORMS') or ['tiktok'])
)

#: Reply with a short hint when a group message has a supported link but no tag.
HINT_ON_UNTAGGED_LINKS = _get_bool('HINT_ON_UNTAGGED_LINKS', False)

#: Reply with an explanation when a private-chat link is not supported.
REPLY_ON_UNSUPPORTED_LINKS = _get_bool('REPLY_ON_UNSUPPORTED_LINKS', True)

#: In private chats (and with /dl), also try links from any site yt-dlp knows,
#: not just the platforms listed in the help text.
TRY_ANY_LINK = _get_bool('TRY_ANY_LINK', True)


# --- Access -------------------------------------------------------------------

#: User or chat IDs allowed to use the bot. Empty means everyone. A group's ID
#: lets everyone in that group use it (in the group, privately and inline); a
#: user's ID lets them use it anywhere. Privileged and admin IDs are always
#: allowed, and admins can approve more chats from Telegram (see /access).
ALLOWED_CHAT_IDS = _get_list('ALLOWED_CHAT_IDS')

#: Who receives breakage alerts and may run /update. Defaults to the
#: privileged chats, which are the owner's own.
ADMIN_CHAT_IDS = _get_list('ADMIN_CHAT_IDS') or list(PRIVATE_CHAT_IDS)

#: Downloads each user may start per hour. 0 disables the limit. Admins and
#: privileged chats are exempt, and links answered from the cache are free.
RATE_LIMIT_PER_HOUR = _get_int('RATE_LIMIT_PER_HOUR', 20, minimum=0)

#: Tell strangers in a private chat that the bot is private (and their ID).
REPLY_TO_UNAUTHORIZED = _get_bool('REPLY_TO_UNAUTHORIZED', True)

#: Send admins an Allow / Ignore prompt when a chat outside the allow list
#: tries the bot (at most once a day per chat).
NOTIFY_ACCESS_REQUESTS = _get_bool('NOTIFY_ACCESS_REQUESTS', True)


# --- Sizes and limits ---------------------------------------------------------

#: Telegram refuses to send anything above 50MB as a playable video.
TELEGRAM_VIDEO_SIZE_LIMIT_MB = 50

#: Hard ceiling for any upload. 50MB on api.telegram.org, 2000MB on a local server.
MAX_UPLOAD_SIZE_MB = _get_int(
    'MAX_UPLOAD_SIZE_MB', 2000 if USE_LOCAL_BOT_API else 50, minimum=1, maximum=2000
)

#: Videos estimated above this are rejected before downloading (privileged chats bypass it).
MAX_DOWNLOAD_SIZE_MB = _get_int('MAX_DOWNLOAD_SIZE_MB', MAX_UPLOAD_SIZE_MB, minimum=1)

#: Upper bound on video height requested from yt-dlp.
MAX_VIDEO_HEIGHT = _get_int('MAX_VIDEO_HEIGHT', 1080, minimum=144, maximum=4320)

#: Telegram caption limit.
MAX_CAPTION_LENGTH = 1024


# --- Concurrency and timeouts -------------------------------------------------

#: Downloads running at the same time across all chats.
MAX_CONCURRENT_DOWNLOADS = _get_int('MAX_CONCURRENT_DOWNLOADS', 3, minimum=1, maximum=32)

#: Give up on a single download after this long.
DOWNLOAD_TIMEOUT_SECONDS = _get_int('DOWNLOAD_TIMEOUT_SECONDS', 900, minimum=30)

#: Give up on a single upload attempt after this long. This drives python-telegram-bot's
#: media_write_timeout, whose 20 second default is far too short: a 20MB video on a
#: 1MB/s uplink needs ~20s of pure write time, so anything under a minute is a footgun.
UPLOAD_TIMEOUT_SECONDS = _get_int('UPLOAD_TIMEOUT_SECONDS', 300, minimum=60)

#: Attempts per upload before reporting failure.
MAX_UPLOAD_RETRIES = _get_int('MAX_UPLOAD_RETRIES', 3, minimum=1, maximum=10)

#: Minimum seconds between progress message edits (Telegram rate limits edits).
PROGRESS_UPDATE_INTERVAL = _get_int('PROGRESS_UPDATE_INTERVAL', 4, minimum=2, maximum=60)

#: Remember at most this many processed messages for de-duplication.
PROCESSED_MESSAGE_CACHE_SIZE = _get_int('PROCESSED_MESSAGE_CACHE_SIZE', 10000, minimum=100)


# --- Downloader ---------------------------------------------------------------

def _resolve_download_dir() -> str:
    configured = _get_str('DOWNLOAD_DIR')
    fallback = os.path.join(tempfile.gettempdir(), 'telegram-video-bot')
    target = configured or fallback
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, '.write-test')
        with open(probe, 'w') as handle:
            handle.write('ok')
        os.remove(probe)
        return target
    except OSError as exc:
        if target == fallback:
            raise
        logger.warning("DOWNLOAD_DIR %s is not writable (%s), using %s", target, exc, fallback)
        os.makedirs(fallback, exist_ok=True)
        return fallback


DOWNLOAD_DIR = _resolve_download_dir()

#: Delete leftover files in DOWNLOAD_DIR older than this at startup. 0 disables.
CLEANUP_MAX_AGE_HOURS = _get_int('CLEANUP_MAX_AGE_HOURS', 6, minimum=0)

#: Netscape cookie file handed to yt-dlp (helps with Instagram/Facebook logins).
COOKIE_FILE = _get_str('COOKIE_FILE') or None

#: e.g. "chrome" or "firefox:profile" - yt-dlp reads cookies straight from the browser.
COOKIES_FROM_BROWSER = _get_str('COOKIES_FROM_BROWSER') or None

#: Only send the cookies to sites that refuse to work without a login (Reddit,
#: Vimeo, private posts). Everything else stays anonymous, so your accounts are
#: not tied to every download anyone asks for.
COOKIES_ONLY_WHEN_NEEDED = _get_bool('COOKIES_ONLY_WHEN_NEEDED', True)

#: DNS-over-HTTPS server yt-dlp switches to when plain DNS is blocked (some VPNs
#: such as Tailscale, corporate networks). TikTok and other sites that need a
#: browser-like connection resolve names with their own DNS client, which those
#: networks block. Use an IP address so the server itself needs no lookup.
#: Empty turns the fallback off.
DNS_FALLBACK_DOH_URL = os.getenv('DNS_FALLBACK_DOH_URL', 'https://1.1.1.1/dns-query').strip() or None

#: Explicit ffmpeg directory when it is not on PATH.
FFMPEG_LOCATION = _get_str('FFMPEG_LOCATION') or None

#: Normalize downloads to H.264/AAC in MP4 (needs ffmpeg). WebM's VP8/VP9/AV1
#: video and Opus/Vorbis audio are not decodable by Apple's players, so iPhone
#: and macOS show a black frame or refuse the file outright.
FORCE_MP4 = _get_bool('FORCE_MP4', True)

#: Attach a generated poster frame to videos (needs ffmpeg).
GENERATE_THUMBNAILS = _get_bool('GENERATE_THUMBNAILS', True)

#: Parallel fragment downloads for HLS/DASH sources.
CONCURRENT_FRAGMENTS = _get_int('CONCURRENT_FRAGMENTS', 4, minimum=1, maximum=16)

#: Socket timeout and retry budget handed to yt-dlp.
SOCKET_TIMEOUT_SECONDS = _get_int('SOCKET_TIMEOUT_SECONDS', 30, minimum=5)
DOWNLOAD_RETRIES = _get_int('DOWNLOAD_RETRIES', 3, minimum=0, maximum=20)

#: Re-encode videos that come out above the upload limit so they still fit
#: (needs ffmpeg). Videos too long to fit at a watchable bitrate are refused.
COMPRESS_OVERSIZED = _get_bool('COMPRESS_OVERSIZED', True)

#: Do not download more than this just to shrink it afterwards.
COMPRESS_MAX_SOURCE_MB = _get_int('COMPRESS_MAX_SOURCE_MB', 400, minimum=1)

#: Most items sent from one multi-item post (Telegram albums hold up to 10).
MAX_ALBUM_ITEMS = _get_int('MAX_ALBUM_ITEMS', 10, minimum=1, maximum=10)

#: Height used by the "Higher quality" button. Above 1080p most sites only
#: serve VP9/AV1, which would need a slow re-encode to play on Apple devices.
HD_MAX_HEIGHT = _get_int('HD_MAX_HEIGHT', 1080, minimum=144, maximum=4320)


# --- Captions -----------------------------------------------------------------

#: Add the author, linked to their profile, under the title.
CAPTION_AUTHOR = _get_bool('CAPTION_AUTHOR', True)

#: Add the post's description as a collapsed "show more" quote.
CAPTION_DESCRIPTION = _get_bool('CAPTION_DESCRIPTION', True)

#: Blur media the site marks as adult (age limit 18+) behind a spoiler.
SPOILER_ADULT_CONTENT = _get_bool('SPOILER_ADULT_CONTENT', True)


# --- State --------------------------------------------------------------------

#: SQLite file for the media cache, statistics and button links. Keep it out of
#: git; in Docker, mount a volume over its folder so it survives rebuilds.
DATABASE_PATH = _get_str('DATABASE_PATH') or os.path.join(PROJECT_DIR, 'data', 'bot.sqlite3')

#: Resend a link that was already uploaded instead of downloading it again,
#: for this many days. 0 disables the cache.
CACHE_TTL_DAYS = _get_int('CACHE_TTL_DAYS', 30, minimum=0)

#: Chat the bot uploads to when it prepares a video for inline mode
#: (@bot <link>) before anyone has sent that link. A private channel with the
#: bot as admin works best. Empty: inline users get a "send in private" button.
CACHE_CHAT_ID = _get_str('CACHE_CHAT_ID') or None


# --- Maintenance --------------------------------------------------------------

#: Update yt-dlp in place on a schedule and restart. Sites change often and
#: yt-dlp is what breaks first. Skipped when the environment is read-only
#: (e.g. a Docker image); rebuild the image there instead.
YTDLP_AUTO_UPDATE = _get_bool('YTDLP_AUTO_UPDATE', True)
YTDLP_UPDATE_INTERVAL_HOURS = _get_int('YTDLP_UPDATE_INTERVAL_HOURS', 168, minimum=1)

#: /status warns when the installed yt-dlp is older than this.
YTDLP_STALE_DAYS = _get_int('YTDLP_STALE_DAYS', 30, minimum=1)

#: Alert the admins after this many failures in a row from one site.
ALERT_FAILURE_THRESHOLD = _get_int('ALERT_FAILURE_THRESHOLD', 3, minimum=1)

#: Do not repeat an alert about the same site more often than this.
ALERT_COOLDOWN_MINUTES = _get_int('ALERT_COOLDOWN_MINUTES', 60, minimum=1)


# --- Webhook ------------------------------------------------------------------

#: Public HTTPS address Telegram should push updates to, e.g.
#: https://bot.example.com. Empty: poll for updates (fine on a laptop).
WEBHOOK_URL = _get_str('WEBHOOK_URL').rstrip('/')
WEBHOOK_LISTEN = _get_str('WEBHOOK_LISTEN', '0.0.0.0')
WEBHOOK_PORT = _get_int('WEBHOOK_PORT', 8443, minimum=1, maximum=65535)
WEBHOOK_PATH = _get_str('WEBHOOK_PATH', 'telegram').strip('/')

#: Telegram sends this back in a header so the bot can reject forged updates.
#: Defaults to a value derived from the token, so it is stable and secret.
WEBHOOK_SECRET = _get_str('WEBHOOK_SECRET') or (
    hashlib.sha256(f"webhook:{TELEGRAM_BOT_TOKEN}".encode()).hexdigest()[:48]
    if TELEGRAM_BOT_TOKEN else ''
)


# --- Logging ------------------------------------------------------------------

LOG_LEVEL = _get_str('LOG_LEVEL', 'INFO').upper()
LOG_FILE = _get_str('LOG_FILE') or None


def configure_logging() -> None:
    """Apply the configured logging setup to the root logger."""
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if LOG_FILE:
        try:
            handlers.append(logging.FileHandler(LOG_FILE, encoding='utf-8'))
        except OSError as exc:
            logger.warning("Could not open LOG_FILE %s: %s", LOG_FILE, exc)

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers,
        force=True,
    )

    # These are chatty at INFO and drown out anything useful.
    for noisy in ('httpx', 'httpcore', 'telegram.ext', 'urllib3', 'selenium', 'WDM'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def describe() -> str:
    """Human readable summary of the active configuration (no secrets)."""
    return (
        f"download_dir={DOWNLOAD_DIR} "
        f"max_download={MAX_DOWNLOAD_SIZE_MB}MB max_upload={MAX_UPLOAD_SIZE_MB}MB "
        f"concurrency={MAX_CONCURRENT_DOWNLOADS} "
        f"upload_timeout={UPLOAD_TIMEOUT_SECONDS}s "
        f"privileged_chats={len(PRIVATE_CHAT_IDS)} "
        f"allowed={'everyone' if not ALLOWED_CHAT_IDS else len(ALLOWED_CHAT_IDS)} "
        f"rate_limit={RATE_LIMIT_PER_HOUR or 'off'}/h "
        f"local_api={'yes' if USE_LOCAL_BOT_API else 'no'} "
        f"mode={'webhook' if WEBHOOK_URL else 'polling'}"
    )
