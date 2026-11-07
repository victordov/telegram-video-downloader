"""
Central configuration for the Telegram Video Downloader Bot.

Every tunable lives here so the rest of the code never touches ``os.getenv``
directly. Values are read once at import time from the environment (``.env`` is
loaded first), validated, and exposed as module level constants.
"""

import logging
import os
import tempfile
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MB = 1024 * 1024


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
        f"local_api={'yes' if USE_LOCAL_BOT_API else 'no'}"
    )
