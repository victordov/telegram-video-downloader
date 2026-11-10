"""
Video downloader module using yt-dlp for multiple platforms.

Supports YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, VK,
Reddit, Pinterest, Vimeo, Twitch clips, Bluesky and Dailymotion by name, and
optionally any other site yt-dlp has an extractor for.

On top of plain downloads it handles:
- multi-item posts (tweets with several videos, Instagram carousels, TikTok
  photo posts), returned as an album of videos and photos
- audio-only downloads (M4A) and a higher-quality "HD" mode
- shrinking videos that come out above the upload limit, by re-encoding them
  at a bitrate calculated to fit
- cancellation from another thread via a ``threading.Event``

Also supports taking screenshots of Threads posts when they're not videos.
Selenium is only imported when a screenshot is actually needed, so the bot runs
fine on machines without a browser as long as no Threads screenshots are taken.
"""

import functools
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests  # session only; all HTTP goes through VideoDownloader._session
import yt_dlp
from yt_dlp.utils import DownloadCancelled

MB = 1024 * 1024

#: What to fetch: the normal video, only the audio track, or a higher quality
#: video that ignores the per-platform height caps.
MODE_VIDEO = 'video'
MODE_AUDIO = 'audio'
MODE_HD = 'hd'

#: Telegram's sendPhoto limit; bigger images go out as files.
TELEGRAM_PHOTO_LIMIT_BYTES = 10 * MB

#: Below this video bitrate a shrunk video is not worth watching.
MIN_SHRINK_VIDEO_KBPS = 150

#: Share of the target size the encoder aims for; the rest covers container
#: overhead and the encoder overshooting its average bitrate.
SHRINK_SAFETY = 0.92

#: URL paths that are media files themselves, downloadable from any host.
DIRECT_MEDIA_EXTENSIONS = ('.mp4', '.m4v', '.mov', '.webm', '.mkv', '.m3u8', '.mp3', '.m4a')

TIKTOK_PHOTO_RE = re.compile(r'tiktok\.com/@[^/?#]*/photo/(\d+)', re.IGNORECASE)

#: Display names. Anything else is shown title-cased.
PLATFORM_LABELS = {
    'youtube': 'YouTube', 'instagram': 'Instagram', 'facebook': 'Facebook',
    'tiktok': 'TikTok', 'twitter': 'Twitter/X', 'threads': 'Threads', '9gag': '9GAG',
    'vk': 'VK', 'reddit': 'Reddit', 'pinterest': 'Pinterest', 'vimeo': 'Vimeo',
    'twitch': 'Twitch', 'bluesky': 'Bluesky', 'dailymotion': 'Dailymotion',
    'direct': 'Direct link',
}


def platform_label(platform: Optional[str]) -> str:
    """Human friendly name for a platform key ('youtube' -> 'YouTube')."""
    if not platform:
        return 'Unknown'
    return PLATFORM_LABELS.get(platform) or platform.replace('_', ' ').title()

#: Called from the download thread with a normalized progress dictionary.
ProgressCallback = Callable[[Dict[str, Any]], None]

BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)

MOBILE_USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
)

#: Error code -> message shown to the user. Keys are also used by the bot.
ERROR_MESSAGES = {
    'unsupported_url': 'That link is not from a platform I support.',
    'file_too_large': 'Video file is too large to download.',
    'tiktok_photo': 'This appears to be a TikTok photo post, not a video.',
    'registered_users_only': 'This video is only available to registered users who follow the account.',
    'login_required': 'This content is private or requires a login.',
    'unavailable': 'This content was removed or is unavailable.',
    'geo_restricted': 'This content is not available in the bot\'s region.',
    'rate_limited': 'The platform is rate limiting the bot. Please try again in a few minutes.',
    'live_stream': 'Live streams cannot be downloaded.',
    'no_video_found': 'No downloadable video was found at that link.',
    'unsupported_by_extractor': 'The platform changed and this link cannot be extracted right now.',
    'download_failed': 'Failed to download the video. It might be private, region locked, or unsupported.',
    'threads_screenshot_failed': 'Failed to take a screenshot of that Threads post.',
    'too_long_to_shrink': 'This video is too long to fit under the upload limit, even compressed.',
    'cancelled': 'Download cancelled.',
}

#: (needle in the yt-dlp error, error code). Checked in order, first match wins.
_ERROR_SIGNATURES: List[Tuple[str, str]] = [
    ('this content is only available for registered users', 'registered_users_only'),
    ('login required', 'login_required'),
    ('log in', 'login_required'),
    # Reddit and Vimeo refuse anonymous access outright; that needs cookies,
    # not a yt-dlp update, so it must not look like the site broke.
    ('authentication is required', 'login_required'),
    ('only works when logged-in', 'login_required'),
    ('use --cookies', 'login_required'),
    ('sign in to confirm', 'login_required'),
    ('private video', 'login_required'),
    ('this video is private', 'login_required'),
    ('account is private', 'login_required'),
    ('requested content is not available', 'unavailable'),
    ('video unavailable', 'unavailable'),
    ('has been removed', 'unavailable'),
    ('no longer available', 'unavailable'),
    ('page does not exist', 'unavailable'),
    ('available in your country', 'geo_restricted'),
    ('not available from your location', 'geo_restricted'),
    ('geo restricted', 'geo_restricted'),
    ('geo-restricted', 'geo_restricted'),
    ('http error 429', 'rate_limited'),
    ('too many requests', 'rate_limited'),
    ('rate-limit', 'rate_limited'),
    ('is live', 'live_stream'),
    ('no video formats found', 'no_video_found'),
    ('no video could be found', 'no_video_found'),
    ('unsupported url', 'unsupported_by_extractor'),
]


class _YdlLogger:
    """Routes yt-dlp's own output into the module logger instead of stdout."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def info(self, msg: str) -> None:
        self._logger.debug(msg)

    def warning(self, msg: str) -> None:
        self._logger.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        self._logger.error("yt-dlp: %s", msg)


class _TikTokPhotoRedirect(Exception):
    """A short TikTok link turned out to point at a photo post."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


@dataclass
class _Job:
    """Everything one download_video call needs to carry between helpers."""
    url: str
    platform: str
    mode: str
    ignore_size_limit: bool
    cancel_event: Optional[threading.Event]
    progress_callback: Optional['ProgressCallback']
    item_index: int = 0
    item_count: int = 0

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DownloadCancelled('Cancelled by user')


@functools.lru_cache(maxsize=1)
def _specific_extractors() -> Tuple[Any, ...]:
    """yt-dlp's extractors minus the catch-all ones (loaded once, ~0.2s)."""
    from yt_dlp.extractor import gen_extractor_classes

    skip = {'Generic', 'CommonMistakes', 'UnicodeBOM'}
    return tuple(ie for ie in gen_extractor_classes()
                 if ie.ie_key() not in skip and ie.working())


@functools.lru_cache(maxsize=1024)
def _extractor_for(url: str) -> Optional[str]:
    """Key of the first specific yt-dlp extractor that claims a URL."""
    for ie in _specific_extractors():
        try:
            if ie.suitable(url):
                return ie.ie_key()
        except Exception:  # a broken regex in one extractor must not stop the scan
            continue
    return None


class VideoDownloader:
    """Unified video downloader for multiple platforms"""

    # URL patterns for supported platforms
    URL_PATTERNS = {
        'youtube': [
            r'(?:https?://)?(?:www\.|m\.|music\.)?(?:youtube\.com/watch\?(?:[^ ]*&)?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|youtube\.com/live/)([a-zA-Z0-9_-]{11})',
            r'(?:https?://)?(?:www\.|m\.)?youtube\.com/shorts/([a-zA-Z0-9_-]+)'
        ],
        'instagram': [
            r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([a-zA-Z0-9_-]+)',
            r'(?:https?://)?(?:www\.)?instagram\.com/[^/]+/(?:p|reel|reels|tv)/([a-zA-Z0-9_-]+)',
            r'(?:https?://)?(?:www\.)?instagram\.com/stories/[^/]+/([0-9]+)',
            r'(?:https?://)?(?:www\.)?instagram\.com/share/(?:reel/|p/)?([a-zA-Z0-9_-]+)'
        ],
        'tiktok': [
            r'(?:https?://)?(?:www\.|m\.)?tiktok\.com/@[^/]+/(?:video|photo)/([0-9]+)',
            r'(?:https?://)?(?:vm|vt)\.tiktok\.com/([a-zA-Z0-9]+)',
            r'(?:https?://)?(?:www\.)?tiktok\.com/t/([a-zA-Z0-9]+)'
        ],
        'facebook': [
            r'(?:https?://)?(?:www\.|web\.|m\.)?facebook\.com/[^/]+/videos/(?:[^/]+/)?([0-9]+)',
            r'(?:https?://)?(?:www\.|web\.|m\.)?facebook\.com/watch/?\?v=([0-9]+)',
            r'(?:https?://)?(?:www\.|web\.|m\.)?facebook\.com/reel/([0-9]+)',
            r'(?:https?://)?(?:www\.)?fb\.watch/([a-zA-Z0-9_-]+)'
        ],
        'twitter': [
            r'(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/[^/]+/status/([0-9]+)',
            r'(?:https?://)?(?:www\.)?t\.co/([a-zA-Z0-9]+)'
        ],
        'threads': [
            r'(?:https?://)?(?:www\.)?threads\.(?:com|net)/@[^/]+/post/([a-zA-Z0-9_-]+)'
        ],
        '9gag': [
            r'(?:https?://)?(?:www\.)?9gag\.com/gag/([a-zA-Z0-9]+)'
        ],
        'vk': [
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/video-?\d+_\d+',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/videos-?\d+\?.*z=video',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/clip-?\d+_\d+',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/[^/]+\?.*z=video',
            r'(?:https?://)?(?:www\.|m\.)?vkvideo\.ru/(?:video|clip)-?\d+_\d+'
        ],
        'reddit': [
            r'(?:https?://)?(?:(?:www|old|new|m|sh)\.)?reddit\.com/r/[^/]+/(?:comments|s)/([a-zA-Z0-9_]+)',
            r'(?:https?://)?(?:(?:www|old|new|m)\.)?reddit\.com/(?:user|u)/[^/]+/comments/([a-zA-Z0-9]+)',
            r'(?:https?://)?(?:v\.)?redd\.it/([a-zA-Z0-9]+)',
        ],
        'pinterest': [
            r'(?:https?://)?(?:[a-z]{2,3}\.)?pinterest\.[a-z.]{2,6}/pin/(?:[^/?#]*--)?([0-9]+)',
            r'(?:https?://)?pin\.it/([a-zA-Z0-9]+)',
        ],
        'vimeo': [
            r'(?:https?://)?(?:www\.)?vimeo\.com/(?:channels/[^/]+/|groups/[^/]+/videos/|video/)?([0-9]+)',
            r'(?:https?://)?player\.vimeo\.com/video/([0-9]+)',
        ],
        'twitch': [
            r'(?:https?://)?clips\.twitch\.tv/([a-zA-Z0-9_-]+)',
            r'(?:https?://)?(?:www\.|m\.)?twitch\.tv/[^/]+/clip/([a-zA-Z0-9_-]+)',
        ],
        'bluesky': [
            r'(?:https?://)?(?:www\.)?bsky\.app/profile/[^/]+/post/([a-zA-Z0-9]+)',
        ],
        'dailymotion': [
            r'(?:https?://)?(?:www\.)?dailymotion\.com/video/([a-zA-Z0-9]+)',
            r'(?:https?://)?dai\.ly/([a-zA-Z0-9]+)',
        ],
    }

    #: Patterns are matched on every incoming message, so compile them once.
    COMPILED_URL_PATTERNS = {
        platform: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        for platform, patterns in URL_PATTERNS.items()
    }

    #: Per-platform cap on the requested video height.
    PLATFORM_MAX_HEIGHT = {'youtube': 720, 'vk': 720}

    # ChromeDriver resolution hits the network, so do it once per process.
    _chromedriver_path: Optional[str] = None
    _chromedriver_lock = threading.Lock()

    def __init__(self, download_dir: Optional[str] = None, max_filesize_mb: int = 50,
                 max_video_height: int = 1080, cookie_file: Optional[str] = None,
                 cookies_from_browser: Optional[str] = None,
                 ffmpeg_location: Optional[str] = None,
                 concurrent_fragments: int = 4, socket_timeout: int = 30,
                 retries: int = 3, force_mp4: bool = True,
                 upload_limit_mb: Optional[int] = None,
                 compress_oversized: bool = False, compress_max_source_mb: int = 400,
                 max_album_items: int = 1, hd_max_height: int = 1080,
                 cookies_only_when_needed: bool = False):
        """Initialize the video downloader

        Args:
            download_dir: Directory to save downloaded videos. If None, uses temp directory.
            max_filesize_mb: Refuse downloads estimated above this size.
            max_video_height: Upper bound on the requested video height.
            cookie_file: Optional Netscape cookie file passed to yt-dlp.
            cookies_from_browser: Optional yt-dlp browser cookie specification.
            ffmpeg_location: Optional explicit ffmpeg directory.
            concurrent_fragments: Parallel fragment downloads for HLS/DASH.
            socket_timeout: Per-socket timeout handed to yt-dlp.
            retries: Retry budget handed to yt-dlp.
            force_mp4: Normalize finished downloads to H.264/AAC MP4 for Apple
                playback. Needs ffmpeg; ignored when it is unavailable.
            upload_limit_mb: Hard ceiling of what can be delivered at all, even
                when ``ignore_size_limit`` is set. Defaults to max_filesize_mb.
            compress_oversized: Re-encode videos above the limit so they fit.
            compress_max_source_mb: Refuse to download more than this just to
                shrink it afterwards.
            max_album_items: Items kept from a multi-item post (1 = first only).
            hd_max_height: Height cap used by MODE_HD.
            cookies_only_when_needed: Try every site anonymously first and
                only send the cookies once a site asks for a login.
        """
        self.download_dir = download_dir or tempfile.gettempdir()
        self.max_filesize_mb = max_filesize_mb
        self.upload_limit_mb = upload_limit_mb or max_filesize_mb
        self.compress_oversized = compress_oversized
        self.compress_max_source_mb = compress_max_source_mb
        self.max_album_items = max(1, max_album_items)
        self.hd_max_height = hd_max_height
        self.max_video_height = max_video_height
        self.cookie_file = cookie_file
        self.cookies_from_browser = cookies_from_browser
        self.cookies_only_when_needed = cookies_only_when_needed
        # Sites that asked for a login during this run go straight to cookies.
        self._login_platforms: set = set()
        self._cookie_lock = threading.Lock()
        self._browser_cookies: Optional[str] = None
        self._browser_cookies_at = 0.0
        self._cookie_loader: Optional[threading.Thread] = None
        self.ffmpeg_location = ffmpeg_location
        self.concurrent_fragments = concurrent_fragments
        self.socket_timeout = socket_timeout
        self.retries = retries
        self.force_mp4 = force_mp4
        self.logger = logging.getLogger(__name__)

        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': BROWSER_USER_AGENT,
            'Accept-Language': 'en-US,en;q=0.9',
        })

        # Ensure download directory exists
        os.makedirs(self.download_dir, exist_ok=True)

    @property
    def max_filesize_bytes(self) -> int:
        return self.max_filesize_mb * MB

    @property
    def upload_limit_bytes(self) -> int:
        return self.upload_limit_mb * MB

    def _target_bytes(self, ignore_size_limit: bool) -> int:
        """The size a delivered file has to fit under for this request."""
        if ignore_size_limit:
            return self.upload_limit_bytes
        return min(self.max_filesize_bytes, self.upload_limit_bytes)

    @property
    def has_ffmpeg(self) -> bool:
        """True when ffmpeg is usable, which unlocks stream merging and thumbnails."""
        return self._ffmpeg_binary() is not None

    def _ffmpeg_binary(self) -> Optional[str]:
        if self.ffmpeg_location:
            candidate = os.path.join(self.ffmpeg_location, 'ffmpeg')
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            if os.path.isfile(self.ffmpeg_location):
                return self.ffmpeg_location
        return shutil.which('ffmpeg')

    def _ffprobe_binary(self) -> Optional[str]:
        if self.ffmpeg_location:
            candidate = os.path.join(self.ffmpeg_location, 'ffprobe')
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which('ffprobe')

    # ------------------------------------------------------------------
    # URL handling
    # ------------------------------------------------------------------

    def detect_platform(self, url: str) -> Optional[str]:
        """Detect which platform a URL belongs to

        Args:
            url: Video URL to check

        Returns:
            Platform name if detected, None otherwise
        """
        if not url:
            return None

        for platform, patterns in self.COMPILED_URL_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(url)
                if match:
                    self.logger.debug("Platform %s matched %s (%s)", platform, url, match.group(0))
                    return platform

        self.logger.debug("No platform matched URL: %s", url)
        return None

    def generic_site(self, url: str) -> Optional[str]:
        """Platform key for a link outside the named platforms, if yt-dlp can handle it.

        Only specific extractors count: the catch-all generic one would fetch
        any web page on the off chance it embeds a video. Links straight to a
        media file are the exception and come back as 'direct'.
        """
        if not url or not re.match(r'https?://', url, re.IGNORECASE):
            return None
        if urlparse(url).path.lower().endswith(DIRECT_MEDIA_EXTENSIONS):
            return 'direct'
        key = _extractor_for(url)
        return key.lower() if key else None

    def resolve_platform(self, url: str, any_site: bool = False) -> Optional[str]:
        """detect_platform, falling back to any yt-dlp site when allowed."""
        return self.detect_platform(url) or (self.generic_site(url) if any_site else None)

    @classmethod
    def is_named_platform(cls, platform: Optional[str]) -> bool:
        return platform in cls.URL_PATTERNS

    #: Query parameters that only track who shared a link.
    _TRACKING_PARAMS = re.compile(
        r'^(utm_\w+|fbclid|gclid|si|igsh|igshid|feature|ref|ref_src|ref_url|share|'
        r'share_id|mibextid|s|t)$', re.IGNORECASE,
    )

    def canonical_id(self, url: str) -> str:
        """A stable identity for "the same video", used as the cache key.

        Named platforms use the ID their URL pattern captures, so youtu.be/X,
        youtube.com/watch?v=X&si=... and m.youtube.com/watch?v=X all match.
        Other links fall back to the URL minus tracking parameters.
        """
        from urllib.parse import parse_qsl, urlencode

        platform = self.detect_platform(url)
        if platform:
            for pattern in self.COMPILED_URL_PATTERNS[platform]:
                match = pattern.search(url)
                if match:
                    if match.groups() and match.group(1):
                        return f"{platform}:{match.group(1)}"
                    break

        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        host = re.sub(r'^(www|m|mobile)\.', '', host)
        query = sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                       if not self._TRACKING_PARAMS.match(key))
        path = parsed.path.rstrip('/') or '/'
        return f"url:{host}{path}" + (f"?{urlencode(query)}" if query else '')

    # ------------------------------------------------------------------
    # yt-dlp configuration
    # ------------------------------------------------------------------

    def _format_selector(self, platform: str, mode: str = MODE_VIDEO) -> str:
        """Build a format string that prefers the largest video under the limit."""
        if mode == MODE_AUDIO:
            # AAC plays everywhere and usually needs no conversion at all.
            return 'ba[acodec^=mp4a]/ba[ext=m4a]/ba/b'

        if mode == MODE_HD:
            height = self.hd_max_height
            size = self.upload_limit_mb
        else:
            height = min(self.PLATFORM_MAX_HEIGHT.get(platform, self.max_video_height),
                         self.max_video_height)
            size = self.max_filesize_mb

        if self.has_ffmpeg:
            # Separate video+audio streams give much better quality per byte, but
            # they need ffmpeg to be merged back together.
            #
            # Apple's players decode H.264/HEVC + AAC only, so ask for those
            # first: a site that offers both VP9/Opus and H.264/AAC (YouTube
            # always does) then needs no transcode at all. The codec-agnostic
            # tiers below stay as a last resort and are repaired afterwards by
            # _ensure_ios_compatible_video.
            if self.force_mp4:
                preferred = (
                    f'bv*[vcodec^=avc1][height<=?{height}][filesize<{size}M]+ba[acodec^=mp4a]/'
                    f'bv*[vcodec^=avc1][height<=?{height}][filesize<{size}M]+ba/'
                    f'b[ext=mp4][height<=?{height}][filesize<{size}M]/'
                    f'bv*[vcodec^=avc1][height<=?{height}][filesize_approx<{size}M]+ba[acodec^=mp4a]/'
                    f'bv*[vcodec^=avc1][height<=?{height}][filesize_approx<{size}M]+ba/'
                    f'b[ext=mp4][height<=?{height}][filesize_approx<{size}M]/'
                    f'bv*[vcodec^=avc1][height<=?{height}]+ba/'
                    f'b[ext=mp4][height<=?{height}]/'
                )
            else:
                preferred = ''

            return preferred + (
                f'bv*[height<=?{height}][filesize<{size}M]+ba/'
                f'b[height<=?{height}][filesize<{size}M]/'
                f'bv*[height<=?{height}][filesize_approx<{size}M]+ba/'
                f'b[height<=?{height}][filesize_approx<{size}M]/'
                f'bv*[height<=?{height}]+ba/b[height<=?{height}]/bv*+ba/b'
            )

        # Without ffmpeg nothing can be merged or repaired later, so a
        # progressive MP4 is the only thing guaranteed to play on Apple devices.
        preferred = ''
        if self.force_mp4:
            preferred = (
                f'b[ext=mp4][height<=?{height}][filesize<{size}M]/'
                f'b[ext=mp4][height<=?{height}][filesize_approx<{size}M]/'
                f'b[ext=mp4][height<=?{height}]/b[ext=mp4]/'
            )

        return preferred + (
            f'b[height<=?{height}][filesize<{size}M]/'
            f'b[height<=?{height}][filesize_approx<{size}M]/'
            f'b[filesize<{size}M]/b[height<=?{height}]/b'
        )

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    #: Re-read the browser's cookies this often (sessions rotate slowly).
    BROWSER_COOKIE_TTL = 6 * 3600
    #: After a failed read, wait this long before asking the browser again.
    BROWSER_COOKIE_RETRY = 15 * 60
    #: How long a download waits for the browser (or a Keychain prompt).
    BROWSER_COOKIE_WAIT = 60

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies_from_browser
                    or (self.cookie_file and os.path.exists(self.cookie_file)))

    def wants_cookies(self, platform: str) -> bool:
        if not self.has_cookies:
            return False
        return not self.cookies_only_when_needed or platform in self._login_platforms

    def browser_cookies(self, force: bool = False) -> Optional[str]:
        """The browser's cookies as Netscape text, read once and kept in memory.

        Reading them can mean a Keychain prompt (Chrome on macOS) and a copy of
        the browser's cookie database, so it happens once per
        BROWSER_COOKIE_TTL instead of once per download, on a thread of its
        own: a prompt nobody answers must not hang the downloads. Nothing is
        written to disk. Returns None when the browser cannot be read (yet).
        """
        if not self.cookies_from_browser:
            return None
        with self._cookie_lock:
            have = self._browser_cookies is not None
            ttl = self.BROWSER_COOKIE_TTL if have else self.BROWSER_COOKIE_RETRY
            fresh = self._browser_cookies_at and time.time() - self._browser_cookies_at < ttl
            if fresh and not force:
                return self._browser_cookies
            loader = self._cookie_loader
            started = loader is None or not loader.is_alive()
            if started:
                loader = threading.Thread(target=self._load_browser_cookies,
                                          name='browser-cookies', daemon=True)
                self._cookie_loader = loader
                loader.start()
        # Only the caller that started the read waits for it; later callers
        # take what is there instead of piling up behind an open prompt.
        loader.join(self.BROWSER_COOKIE_WAIT if started else 0)
        if loader.is_alive() and started:
            self.logger.warning("Still waiting for %s cookies; is a Keychain prompt open?",
                                self.cookies_from_browser)
        return self._browser_cookies

    def _load_browser_cookies(self) -> None:
        spec = tuple(part or None for part in self.cookies_from_browser.split(':'))
        try:
            from yt_dlp.cookies import load_cookies
            jar = load_cookies(None, spec, None)
            buffer = io.StringIO()
            jar.save(buffer)
            text: Optional[str] = buffer.getvalue()
            self.logger.info("Loaded %d cookies from %s", len(jar), spec[0])
        except Exception as exc:
            text = None
            # yt-dlp wraps the real reason (no profile, locked keychain, ...).
            reason = exc.__cause__ or exc.__context__ or exc
            self.logger.warning("Could not read cookies from %s: %s: %s",
                                self.cookies_from_browser, type(reason).__name__, reason)
        with self._cookie_lock:
            now = time.time()
            if text is not None:
                self._browser_cookies, self._browser_cookies_at = text, now
            elif self._browser_cookies is not None:
                # Keep the last good cookies, but try again soon.
                self._browser_cookies_at = now - self.BROWSER_COOKIE_TTL + self.BROWSER_COOKIE_RETRY
            else:
                self._browser_cookies_at = now

    def cookies_available(self) -> bool:
        """Whether there are cookies to send right now (the browser may be unreadable)."""
        if self.cookie_file and os.path.exists(self.cookie_file):
            return True
        return self.browser_cookies() is not None

    def _apply_cookies(self, opts: Dict[str, Any]) -> bool:
        """Point yt-dlp at the cookies. Returns whether any were applied."""
        file_ok = bool(self.cookie_file and os.path.exists(self.cookie_file))
        if self.cookie_file and not file_ok:
            self.logger.warning("COOKIE_FILE %s does not exist, ignoring", self.cookie_file)
        browser = self.browser_cookies()
        if browser is not None:
            text = browser
            if file_ok:
                with open(self.cookie_file, encoding='utf-8') as handle:
                    text += '\n' + handle.read()
            # yt-dlp accepts a file object here, so the cookies stay in memory.
            opts['cookiefile'] = io.StringIO(text)
            return True
        if file_ok:
            opts['cookiefile'] = self.cookie_file
            return True
        return False

    def get_ydl_opts(self, platform: str, mode: str = MODE_VIDEO,
                     with_cookies: Optional[bool] = None) -> Dict[str, Any]:
        """Get yt-dlp options for specific platform

        Args:
            platform: Platform name
            mode: MODE_VIDEO, MODE_AUDIO or MODE_HD

        Returns:
            Dictionary of yt-dlp options
        """
        opts: Dict[str, Any] = {
            # Include the id so two downloads of different videos with the same
            # title cannot clobber each other, and cap the title length so long
            # titles do not blow past the filesystem's name limit.
            #
            # The folder goes in 'paths', not the template: yt-dlp applies
            # trim_file_name to the whole templated string, so an absolute
            # template had its directory (and the id) cut off on deep paths.
            'paths': {'home': self.download_dir},
            'outtmpl': '%(title).80B [%(id)s].%(ext)s',
            'format': self._format_selector(platform, mode),
            'noplaylist': True,
            'playlist_items': '1',
            'extractaudio': False,
            'embed_subs': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'writethumbnail': False,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'logger': _YdlLogger(self.logger),
            'socket_timeout': self.socket_timeout,
            'retries': self.retries,
            'fragment_retries': self.retries,
            'extractor_retries': self.retries,
            'concurrent_fragment_downloads': self.concurrent_fragments,
            'restrictfilenames': False,
            'windowsfilenames': True,
            'trim_file_name': 120,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                },
            },
            'http_headers': {
                'User-Agent': BROWSER_USER_AGENT,
                'Accept-Language': 'en-US,en;q=0.9',
            },
        }

        if self.has_ffmpeg:
            opts['merge_output_format'] = 'mp4'
        if self.ffmpeg_location:
            opts['ffmpeg_location'] = self.ffmpeg_location
        if with_cookies is None:
            with_cookies = self.wants_cookies(platform)
        if with_cookies:
            self._apply_cookies(opts)

        # Platform-specific overrides, merged rather than replacing shared keys.
        platform_opts: Dict[str, Dict[str, Any]] = {
            'tiktok': {
                # Reset http_headers so yt-dlp's native JS challenge solver uses its own UA
                'http_headers': {'User-Agent': None},
            },
            '9gag': {
                'format': f'b[ext=mp4][filesize<{self.max_filesize_mb}M]/'
                          f'b[filesize<{self.max_filesize_mb}M]/b',
            },
        }

        overrides = platform_opts.get(platform)
        if overrides:
            overrides = dict(overrides)
            header_overrides = overrides.pop('http_headers', None)
            if header_overrides is not None:
                for key, value in header_overrides.items():
                    if value is None:
                        opts['http_headers'].pop(key, None)
                    else:
                        opts['http_headers'][key] = value
            extractor_overrides = overrides.pop('extractor_args', None)
            if extractor_overrides:
                opts['extractor_args'].update(extractor_overrides)
            if mode != MODE_VIDEO:
                # Platform format overrides are tuned for the normal video mode.
                overrides.pop('format', None)
            opts.update(overrides)

        if mode == MODE_AUDIO:
            opts.pop('merge_output_format', None)
            if self.has_ffmpeg:
                # Opus/Vorbis do not play in Telegram's music player on iOS;
                # AAC sources are copied, everything else is converted.
                opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'm4a',
                    'preferredquality': '160',
                }]

        self.logger.debug("yt-dlp options for %s (%s): format=%s", platform, mode, opts['format'])
        return opts

    # ------------------------------------------------------------------
    # Progress reporting
    # ------------------------------------------------------------------

    def progress_hook(self, d: Dict[str, Any]) -> None:
        """Progress hook for yt-dlp to log download progress

        Args:
            d: Progress information dictionary from yt-dlp
        """
        status = d.get('status')
        if status == 'downloading':
            self.logger.debug(
                "Download progress: %s at %s, ETA: %s for %s",
                d.get('_percent_str', 'N/A'), d.get('_speed_str', 'N/A'),
                d.get('_eta_str', 'N/A'), d.get('filename', 'unknown'),
            )
        elif status == 'finished':
            self.logger.info("Download finished: %s", d.get('filename', 'unknown'))
        elif status == 'error':
            self.logger.error("Download error: %s", d.get('error', 'unknown error'))

    @staticmethod
    def normalize_progress(d: Dict[str, Any]) -> Dict[str, Any]:
        """Turn a raw yt-dlp progress dict into a stable, UI friendly shape."""
        downloaded = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        percent = (downloaded / total * 100) if total else None
        return {
            'status': d.get('status'),
            'downloaded_bytes': downloaded,
            'total_bytes': total or None,
            'speed': d.get('speed'),
            'eta': d.get('eta'),
            'percent': percent,
            'fragment_index': d.get('fragment_index'),
            'fragment_count': d.get('fragment_count'),
        }

    def _build_hooks(self, progress_callback: Optional[ProgressCallback],
                     cancel_event: Optional[threading.Event] = None,
                     job: Optional[_Job] = None) -> List[Callable]:
        hooks: List[Callable] = [self.progress_hook]

        if cancel_event is not None:
            def check_cancelled(d: Dict[str, Any]) -> None:
                # Raising from a progress hook is yt-dlp's supported way to
                # abort a download mid-transfer.
                if cancel_event.is_set():
                    raise DownloadCancelled('Cancelled by user')

            hooks.append(check_cancelled)

        if progress_callback is None:
            return hooks

        def forward(d: Dict[str, Any]) -> None:
            try:
                progress = self.normalize_progress(d)
                if job is not None and job.item_count > 1:
                    progress['item_index'] = job.item_index
                    progress['item_count'] = job.item_count
                progress_callback(progress)
            except Exception as exc:  # never let UI errors abort a download
                self.logger.debug("Progress callback failed: %s", exc)

        hooks.append(forward)
        return hooks

    def _emit(self, job: Optional[_Job], progress: Dict[str, Any]) -> None:
        """Send a non-yt-dlp progress event (e.g. compressing) to the callback."""
        if job is None or job.progress_callback is None:
            return
        if job.item_count > 1:
            progress = dict(progress, item_index=job.item_index, item_count=job.item_count)
        try:
            job.progress_callback(progress)
        except Exception as exc:
            self.logger.debug("Progress callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Info normalization helpers
    # ------------------------------------------------------------------

    def _select_download_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize extractor results to a concrete downloadable entry."""
        download_info = dict(info)
        if info.get('_type') == 'playlist':
            entries = [entry for entry in (info.get('entries') or []) if isinstance(entry, dict)]
            if not entries:
                raise ValueError('No downloadable entries found in playlist result')
            self.logger.info(
                "Extractor returned playlist result with %d entries; downloading first entry",
                len(entries),
            )
            download_info = dict(entries[0])
        return download_info

    @staticmethod
    def _normalize_duration(value: Any) -> Optional[float]:
        """Coerce yt-dlp's duration into a number or None (never the string 'Unknown')."""
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, str):
            try:
                parsed = float(value)
            except ValueError:
                return None
            return parsed if parsed > 0 else None
        return None

    def _estimate_filesize(self, platform: str, download_info: Dict[str, Any]) -> int:
        """Estimate download size, with Twitter-specific corrections for playlist/HLS metadata."""
        filesize = download_info.get('filesize') or download_info.get('filesize_approx') or 0
        if filesize is None:
            filesize = 0

        if platform != 'twitter':
            return int(filesize)

        candidate_sizes = []
        requested_formats_raw = download_info.get('requested_formats')
        requested_formats = (
            [fmt for fmt in requested_formats_raw if isinstance(fmt, dict)]
            if isinstance(requested_formats_raw, list) else []
        )
        if requested_formats:
            combined_requested_size = 0
            has_requested_size = False
            combined_tbr = 0
            for fmt in requested_formats:
                size = fmt.get('filesize') or fmt.get('filesize_approx')
                if size:
                    combined_requested_size += size
                    has_requested_size = True
                if fmt.get('tbr'):
                    combined_tbr += fmt.get('tbr')
            if has_requested_size:
                candidate_sizes.append(combined_requested_size)

            duration = download_info.get('duration') or 0
            if combined_tbr and duration:
                candidate_sizes.append(duration * combined_tbr * 1000 / 8)

        selected_format_id = download_info.get('format_id')
        formats = download_info.get('formats') or []
        for fmt in formats:
            if selected_format_id and fmt.get('format_id') == selected_format_id:
                size = fmt.get('filesize') or fmt.get('filesize_approx')
                if size:
                    candidate_sizes.append(size)
                if fmt.get('tbr') and download_info.get('duration'):
                    candidate_sizes.append(download_info['duration'] * fmt['tbr'] * 1000 / 8)
                break

        duration = download_info.get('duration') or 0
        tbr = download_info.get('tbr') or 0
        if duration and tbr:
            candidate_sizes.append(duration * tbr * 1000 / 8)

        positive_candidates = [int(size) for size in candidate_sizes if size and size > 0]
        if positive_candidates:
            better_estimate = min(positive_candidates)
            if filesize <= 0 or better_estimate < filesize:
                self.logger.info(
                    "Using corrected Twitter size estimate: %.1fMB instead of %.1fMB",
                    better_estimate / MB, filesize / MB,
                )
                filesize = better_estimate

        if filesize > 100 * MB and duration:
            estimated_bitrate = 2 * MB  # 2 Mbps in bits per second
            bitrate_estimate = duration * estimated_bitrate / 8
            if bitrate_estimate < filesize:
                self.logger.info(
                    "Calculated fallback Twitter size estimate from duration: %.1fMB",
                    bitrate_estimate / MB,
                )
                filesize = bitrate_estimate

        return int(filesize)

    def _optimize_twitter_format(self, download_info: Dict[str, Any],
                                 size_limit_bytes: int) -> Dict[str, Any]:
        """Pick the best Twitter MP4 variant that fits within the size limit, when available."""
        formats = download_info.get('formats') or []
        candidates = []
        for fmt in formats:
            if not isinstance(fmt, dict):
                continue
            if fmt.get('vcodec') == 'none':
                continue
            if fmt.get('protocol') != 'https':
                continue
            if fmt.get('ext') != 'mp4':
                continue

            candidate_size = fmt.get('filesize') or fmt.get('filesize_approx')
            if not candidate_size or candidate_size > size_limit_bytes:
                continue

            candidates.append(fmt)

        if not candidates:
            return download_info

        current_size = download_info.get('filesize') or download_info.get('filesize_approx') or 0
        current_format_id = download_info.get('format_id')
        best_candidate = max(
            candidates,
            key=lambda fmt: (
                fmt.get('height') or 0,
                fmt.get('tbr') or 0,
                fmt.get('filesize') or fmt.get('filesize_approx') or 0,
            )
        )

        if current_format_id == best_candidate.get('format_id'):
            return download_info

        best_size = best_candidate.get('filesize') or best_candidate.get('filesize_approx') or 0
        if current_size and best_size >= current_size:
            return download_info

        self.logger.info(
            "Switching Twitter format from %s to %s to stay within size limit (%.1fMB)",
            current_format_id or 'unknown', best_candidate.get('format_id'), best_size / MB,
        )

        optimized_info = dict(download_info)
        for field in (
            'format_id', 'format', 'filesize', 'filesize_approx', 'tbr', 'width', 'height',
            'protocol', 'ext', 'vcodec', 'acodec', 'url', 'manifest_url', 'http_headers'
        ):
            if field in best_candidate:
                optimized_info[field] = best_candidate.get(field)

        optimized_info.pop('requested_formats', None)
        optimized_info.pop('requested_downloads', None)
        return optimized_info

    @staticmethod
    def _extract_dimensions(download_info: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
        """Best effort width/height so Telegram renders the right aspect ratio."""
        width = download_info.get('width') or None
        height = download_info.get('height') or None
        if width and height:
            return width, height

        requested = download_info.get('requested_formats')
        if isinstance(requested, list):
            for fmt in requested:
                if not isinstance(fmt, dict):
                    continue
                width = width or fmt.get('width')
                height = height or fmt.get('height')

        formats = download_info.get('formats')
        if (not width or not height) and isinstance(formats, list):
            selected_id = download_info.get('format_id')
            for fmt in formats:
                if isinstance(fmt, dict) and selected_id and fmt.get('format_id') == selected_id:
                    width = width or fmt.get('width')
                    height = height or fmt.get('height')
                    break
            if not width or not height:
                for fmt in reversed(formats):
                    if not isinstance(fmt, dict):
                        continue
                    if fmt.get('vcodec', 'none') != 'none' and fmt.get('width') and fmt.get('height'):
                        width = width or fmt.get('width')
                        height = height or fmt.get('height')
                        break

        return width, height

    def _resolve_downloaded_path(self, ydl, download_info: Dict[str, Any]) -> Optional[str]:
        """Find the file yt-dlp actually wrote."""
        candidates: List[str] = []

        # Post-processors (e.g. audio extraction) record their output here.
        if download_info.get('filepath'):
            candidates.append(download_info['filepath'])

        requested_downloads = download_info.get('requested_downloads')
        if isinstance(requested_downloads, list):
            for entry in requested_downloads:
                if isinstance(entry, dict) and entry.get('filepath'):
                    candidates.append(entry['filepath'])

        if download_info.get('_filename'):
            candidates.append(download_info['_filename'])

        try:
            candidates.append(ydl.prepare_filename(download_info))
        except Exception:  # a fake/partial info dict can trip prepare_filename
            pass

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate

        # yt-dlp may have remuxed to a different container, and multi-video
        # posts get a " #N" suffix appended.
        extensions = ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v',
                      '.m4a', '.mp3', '.opus', '.ogg', '.aac')
        suffixes = ('',) + tuple(f' #{index}' for index in range(1, 10))
        for candidate in candidates:
            if not candidate:
                continue
            base_name = os.path.splitext(candidate)[0]
            for suffix in suffixes:
                for ext in extensions:
                    test_path = f"{base_name}{suffix}{ext}"
                    if os.path.exists(test_path):
                        return test_path

        return None

    def _purge_partials(self, download_info: Dict[str, Any]) -> None:
        """Remove .part/.ytdl leftovers so a failed download does not fill the disk."""
        try:
            title = str(download_info.get('title') or '')[:40]
            if not title:
                return
            for name in os.listdir(self.download_dir):
                if not name.endswith(('.part', '.ytdl', '.temp')):
                    continue
                if title[:20] and title[:20] not in name:
                    continue
                self.cleanup_file(os.path.join(self.download_dir, name))
        except OSError:
            pass

    @staticmethod
    def classify_error(message: str) -> str:
        """Map a yt-dlp error message onto one of the ERROR_MESSAGES codes."""
        lowered = (message or '').lower()
        for needle, code in _ERROR_SIGNATURES:
            if needle in lowered:
                return code
        return 'download_failed'

    def _error(self, code: str, **extra: Any) -> Dict[str, Any]:
        payload = {'error': code, 'message': ERROR_MESSAGES.get(code, ERROR_MESSAGES['download_failed'])}
        payload.update(extra)
        return payload

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_video(self, url: str, ignore_size_limit: bool = False,
                       progress_callback: Optional[ProgressCallback] = None, *,
                       mode: str = MODE_VIDEO,
                       cancel_event: Optional[threading.Event] = None,
                       any_site: bool = False) -> Optional[Dict[str, Any]]:
        """Download video from URL or take screenshot of Threads post

        Args:
            url: URL to process (video URL or Threads post URL)
            ignore_size_limit: Download even when the estimate exceeds the
                download limit. The upload ceiling still applies.
            progress_callback: Optional callable invoked with normalized progress dicts
            mode: MODE_VIDEO, MODE_AUDIO (audio track only) or MODE_HD
            cancel_event: Set it from another thread to abort the download
            any_site: Also accept links outside the named platforms

        Returns:
            None for an unsupported URL, a dict with 'error' on failure, or
            - one item: 'filepath', 'kind' ('video', 'audio' or 'photo'),
              'title', 'platform', 'duration', 'filesize', 'url', 'width',
              'height' plus caption metadata ('uploader', 'description', ...)
            - an album: 'is_album': True and 'items', a list of single items
            - a screenshot: one item plus 'is_screenshot': True
        """
        self.logger.info("Processing %s request for URL: %s", mode, url)

        platform = self.resolve_platform(url, any_site=any_site)
        if not platform:
            self.logger.error("Unsupported URL format: %s", url)
            return None

        if mode == MODE_HD:
            # HD exists to get past the normal limits; only the upload ceiling applies.
            ignore_size_limit = True

        job = _Job(url=url, platform=platform, mode=mode, ignore_size_limit=ignore_size_limit,
                   cancel_event=cancel_event, progress_callback=progress_callback)

        with_cookies = self.wants_cookies(platform)
        result = self._attempt(url, platform, job, with_cookies)
        if not (self.has_cookies and isinstance(result, dict)
                and result.get('error') == 'login_required'):
            return result

        # Remember it, so the next link from this site skips the anonymous try.
        self._login_platforms.add(platform)
        if not self.cookies_available():
            result['message'] = (f"{ERROR_MESSAGES['login_required']} {platform_label(platform)} "
                                 "needs a logged-in session, and the bot could not read its "
                                 "login cookies.")
        elif not with_cookies:
            self.logger.info("%s asked for a login, retrying with cookies", platform_label(platform))
            result = self._attempt(url, platform, job, True)
        return result

    def _attempt(self, url: str, platform: str, job: _Job,
                 with_cookies: bool) -> Optional[Dict[str, Any]]:
        """One download attempt; download_video() may retry it with cookies."""
        mode = job.mode
        cancel_event = job.cancel_event
        ydl_opts = self.get_ydl_opts(platform, mode, with_cookies=with_cookies)
        ydl_opts['progress_hooks'] = self._build_hooks(job.progress_callback, cancel_event, job)

        download_info: Dict[str, Any] = {}
        try:
            if platform == 'tiktok' and mode != MODE_AUDIO and TIKTOK_PHOTO_RE.search(url):
                return self._download_tiktok_photos(url, job)

            self.logger.info("Extracting video information from %s", url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                raw = self._extract(ydl, url)
                job.check_cancelled()

                entries = self._post_entries(raw) if mode != MODE_AUDIO else None
                if entries:
                    return self._download_album(ydl, raw, entries, job)

                info = ydl.process_ie_result(raw, download=False)
                if not info:
                    raise ValueError('Extractor returned no information')
                download_info = self._select_download_info(info)
                return self._download_entry(ydl, download_info, job, parent=info)

        except _TikTokPhotoRedirect as redirect:
            try:
                return self._download_tiktok_photos(redirect.url, job)
            except DownloadCancelled:
                return self._error('cancelled', platform=platform)

        except DownloadCancelled:
            self.logger.info("Download cancelled: %s", url)
            self._purge_partials(download_info)
            return self._error('cancelled', platform=platform)

        except Exception as exc:
            error_message = str(exc).strip() or f"{type(exc).__name__}: {exc!r}"
            self.logger.error("Error downloading %s: %s", url, error_message)
            self._purge_partials(download_info)

            if platform == 'tiktok' and '/photo/' in error_message:
                self.logger.warning("Detected TikTok photo URL (not a video): %s", url)
                return self._error('tiktok_photo', platform=platform)

            if platform == 'threads' and mode != MODE_AUDIO:
                self.logger.info("Video download failed for Threads URL, taking a screenshot instead")
                screenshot_result = self.take_screenshot(url)
                if screenshot_result:
                    return screenshot_result
                return self._error('threads_screenshot_failed', platform=platform)

            if platform == '9gag' and mode != MODE_AUDIO:
                self.logger.info("Attempting 9GAG direct MP4 fallback")
                try:
                    mp4_url = self._extract_9gag_mp4_url(url)
                    if mp4_url:
                        return self._download_direct_media(mp4_url, source_url=url, platform='9gag')
                    return self._error('no_video_found', platform=platform)
                except Exception as fallback_error:
                    self.logger.error("9GAG fallback failed for %s: %s", url, fallback_error)
                    return self._error('download_failed', platform=platform)

            code = self.classify_error(error_message)
            extra: Dict[str, Any] = {}
            if code == 'login_required' and not self.has_cookies:
                # Reddit and Vimeo now refuse every anonymous download; say why.
                extra['message'] = (f"{ERROR_MESSAGES['login_required']} {platform_label(platform)} "
                                    "needs a logged-in session, and the bot has no login cookies "
                                    "set up (COOKIES_FROM_BROWSER or COOKIE_FILE).")
            return self._error(code, platform=platform, detail=error_message[:300], **extra)

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def _extract(self, ydl, url: str) -> Dict[str, Any]:
        """Extract without resolving, following plain URL redirects ourselves.

        Resolving later (``process_ie_result``) lets a multi-item post be
        handled one item at a time, so a photo inside an Instagram carousel no
        longer fails the whole post the way yt-dlp's playlist processing does.
        """
        info = ydl.extract_info(url, download=False, process=False)
        for _ in range(5):
            if not isinstance(info, dict):
                raise ValueError('Extractor returned no information')
            if info.get('_type') != 'url':
                return info
            target = info.get('url') or ''
            # vm.tiktok.com short links resolve to photo posts yt-dlp cannot read.
            if TIKTOK_PHOTO_RE.search(target):
                raise _TikTokPhotoRedirect(target)
            info = ydl.extract_info(target, download=False, process=False,
                                    ie_key=info.get('ie_key'))
        raise ValueError('Too many redirects while extracting')

    @staticmethod
    def _post_entries(info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Items of a multi-item post, or None for a single video or a real playlist."""
        if info.get('_type') not in ('playlist', 'multi_video'):
            return None
        entries = info.get('entries')
        # Lazily paged entries mean a channel or playlist, not a single post.
        if not isinstance(entries, (list, tuple)):
            return None
        items = [entry for entry in entries if isinstance(entry, dict)]
        if not items or any(entry.get('_type') in ('url', 'url_transparent') for entry in items):
            return None
        return items

    #: Post-level fields an item of a multi-item post falls back to.
    _INHERITED_FIELDS = (
        'extractor', 'extractor_key', 'webpage_url', 'original_url',
        'webpage_url_basename', 'webpage_url_domain', 'http_headers',
        'uploader', 'uploader_id', 'uploader_url', 'channel', 'channel_url',
        'description', 'age_limit', 'timestamp',
    )

    def _entry_with_parent(self, entry: Dict[str, Any], parent: Dict[str, Any],
                           index: int) -> Dict[str, Any]:
        item = dict(entry)
        for field in self._INHERITED_FIELDS:
            if item.get(field) is None and parent.get(field) is not None:
                item[field] = parent[field]
        item.setdefault('id', f"{parent.get('id') or 'item'}_{index}")
        item.setdefault('playlist_index', index)
        if parent.get('id'):
            item.setdefault('playlist_id', parent['id'])
        if parent.get('title'):
            item.setdefault('playlist_title', parent['title'])
        return item

    # ------------------------------------------------------------------
    # Multi-item posts
    # ------------------------------------------------------------------

    def _download_album(self, ydl, parent: Dict[str, Any], entries: List[Dict[str, Any]],
                        job: _Job) -> Dict[str, Any]:
        """Download every item of a multi-item post (up to max_album_items)."""
        entries = entries[:self.max_album_items]
        job.item_count = len(entries)
        if len(entries) > 1:
            self.logger.info("Multi-item post with %d item(s) from %s", len(entries), job.platform)

        items: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries, start=1):
            job.check_cancelled()
            job.item_index = index
            entry = self._entry_with_parent(entry, parent, index)
            if self._is_image_entry(entry):
                result = self._download_image(entry, job)
            else:
                result = self._download_album_video(ydl, entry, job)
            if result and 'error' not in result:
                items.append(result)
            else:
                failures.append(result or self._error('download_failed', platform=job.platform))

        return self._album_result(items, failures, parent, job)

    def _album_result(self, items: List[Dict[str, Any]], failures: List[Dict[str, Any]],
                      parent: Dict[str, Any], job: _Job) -> Dict[str, Any]:
        if not items:
            return failures[0] if failures else self._error('no_video_found', platform=job.platform)
        if len(items) == 1:
            single = items[0]
            if failures:
                single['skipped'] = len(failures)
            return single

        meta = self._metadata(parent, platform=job.platform)
        return {
            'is_album': True,
            'items': items,
            'kind': 'album',
            'title': parent.get('title') or items[0].get('title') or 'Post',
            'platform': job.platform,
            'url': job.url,
            'filesize': sum(item.get('filesize') or 0 for item in items),
            'duration': None,
            'skipped': len(failures),
            **meta,
        }

    def _download_album_video(self, ydl, entry: Dict[str, Any], job: _Job) -> Dict[str, Any]:
        info: Optional[Dict[str, Any]] = None
        try:
            info = ydl.process_ie_result(entry, download=False)
            if not info:
                raise ValueError('Extractor returned no information')
            return self._download_entry(ydl, self._select_download_info(info), job, parent=entry)
        except DownloadCancelled:
            self._purge_partials(info or entry)
            raise
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            self._purge_partials(info or entry)
            if 'no video formats' in message.lower() and self._best_image_url(entry):
                return self._download_image(entry, job)
            self.logger.warning("Item %d of %s failed: %s", job.item_index, job.url, message)
            return self._error(self.classify_error(message), platform=job.platform)

    @staticmethod
    def _is_image_entry(entry: Dict[str, Any]) -> bool:
        if entry.get('formats') or entry.get('url'):
            return False
        return bool(entry.get('thumbnails') or entry.get('thumbnail'))

    @staticmethod
    def _best_image_url(entry: Dict[str, Any]) -> Optional[str]:
        thumbnails = [thumb for thumb in (entry.get('thumbnails') or [])
                      if isinstance(thumb, dict) and thumb.get('url')]
        if thumbnails:
            def area(thumb):
                try:
                    return int(thumb.get('width') or 0) * int(thumb.get('height') or 0)
                except (TypeError, ValueError):
                    return 0
            best = max(thumbnails, key=lambda thumb: (area(thumb), thumb.get('preference') or 0))
            # Without sizes, yt-dlp orders thumbnails worst to best.
            return (best if area(best) else thumbnails[-1])['url']
        thumbnail = entry.get('thumbnail')
        return thumbnail if isinstance(thumbnail, str) else None

    def _download_image(self, entry: Dict[str, Any], job: _Job) -> Dict[str, Any]:
        image_url = self._best_image_url(entry)
        if not image_url:
            return self._error('no_video_found', platform=job.platform)

        headers = {'User-Agent': BROWSER_USER_AGENT, 'Accept': 'image/*,*/*;q=0.8'}
        headers.update({k: v for k, v in (entry.get('http_headers') or {}).items()
                        if k.lower() in ('referer', 'cookie', 'user-agent')})
        base = f"{self._safe_name(entry.get('id') or 'image')}_{job.item_index or 1}"
        path = self._fetch_to_file(image_url, base, headers,
                                   self._target_bytes(job.ignore_size_limit), job)
        if not path:
            return self._error('download_failed', platform=job.platform)
        return self._photo_result(path, entry.get('title') or entry.get('playlist_title'),
                                  job, self._metadata(entry, platform=job.platform))

    def _photo_result(self, path: str, title: Optional[str], job: _Job,
                      meta: Dict[str, Any]) -> Dict[str, Any]:
        path = self._ensure_photo_format(path)
        width, height = self._probe_video_dimensions(path)  # ffprobe reads images too
        return {
            'filepath': path,
            'kind': 'photo',
            'title': title or 'Photo',
            'platform': job.platform,
            'duration': None,
            'filesize': os.path.getsize(path),
            'url': job.url,
            'width': width,
            'height': height,
            **meta,
        }

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r'[^A-Za-z0-9_-]+', '_', str(value))[:60] or 'media'

    def _fetch_to_file(self, media_url: str, base_name: str, headers: Dict[str, str],
                       limit_bytes: int, job: Optional[_Job] = None,
                       default_ext: str = '.jpg') -> Optional[str]:
        """Stream a URL to DOWNLOAD_DIR. Returns the path, or None on failure/too big."""
        try:
            response = self._session.get(media_url, headers=headers, timeout=30, stream=True)
            response.raise_for_status()
        except Exception as exc:
            self.logger.warning("Could not fetch %s: %s", media_url, exc)
            return None

        path = None
        try:
            ext = os.path.splitext(urlparse(media_url).path)[1].lower()
            content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip()
            guessed = mimetypes.guess_extension(content_type) if content_type else None
            if guessed:
                ext = '.jpg' if guessed in ('.jpe', '.jpeg') else guessed
            if not ext or len(ext) > 6:
                ext = default_ext
            path = os.path.join(self.download_dir, f"{base_name}{ext}")

            written = 0
            with open(path, 'wb') as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if job is not None:
                        job.check_cancelled()
                    if not chunk:
                        continue
                    output.write(chunk)
                    written += len(chunk)
                    if written > limit_bytes:
                        break

            if written == 0 or written > limit_bytes:
                self.logger.warning("Discarding %s (%d bytes, limit %d)", media_url, written, limit_bytes)
                self.cleanup_file(path)
                return None
            return path
        except DownloadCancelled:
            self.cleanup_file(path)
            raise
        except Exception as exc:
            self.logger.warning("Could not save %s: %s", media_url, exc)
            self.cleanup_file(path)
            return None
        finally:
            response.close()

    def _ensure_photo_format(self, path: str) -> str:
        """Telegram photos must be JPEG or PNG; convert WebP/HEIC/AVIF with ffmpeg."""
        if path.lower().endswith(('.jpg', '.jpeg', '.png')):
            return path
        ffmpeg_path = self._ffmpeg_binary()
        if not ffmpeg_path:
            return path
        output = f"{os.path.splitext(path)[0]}.jpg"
        try:
            proc = subprocess.run(
                [ffmpeg_path, '-y', '-nostdin', '-loglevel', 'error', '-i', path,
                 '-frames:v', '1', '-q:v', '2', output],
                capture_output=True, text=True, check=False, timeout=60,
            )
        except Exception as exc:
            self.logger.debug("Image conversion failed: %s", exc)
            return path
        if proc.returncode != 0 or not os.path.exists(output):
            self.cleanup_file(output)
            return path
        self.cleanup_file(path)
        return output

    # ------------------------------------------------------------------
    # One video (or audio track)
    # ------------------------------------------------------------------

    def _shrink_allowed(self, job: _Job) -> bool:
        return job.mode == MODE_VIDEO and self.compress_oversized and self.has_ffmpeg

    def _too_large(self, filesize: int, title: str, platform: str, duration: Optional[float],
                   target: int, stage: str) -> Dict[str, Any]:
        limit_mb = target // MB
        if (self.compress_oversized and self.has_ffmpeg and duration
                and self._shrink_plan(duration, target) is None):
            message = f'This video is too long to fit under {limit_mb}MB, even compressed.'
        else:
            message = f'Video file is too large to {stage} (limit: {limit_mb}MB)'
        return self._error('file_too_large', message=message, filesize=filesize,
                           title=title, platform=platform, duration=duration)

    def _download_entry(self, ydl, download_info: Dict[str, Any], job: _Job,
                        parent: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Size-check, download, normalize and (if needed) shrink one video."""
        platform = job.platform
        parent = parent or {}
        target = self._target_bytes(job.ignore_size_limit)

        if platform == 'twitter' and job.mode != MODE_AUDIO:
            download_info = self._optimize_twitter_format(download_info, target)

        title = download_info.get('title') or parent.get('title') or 'Unknown'
        duration = self._normalize_duration(download_info.get('duration', parent.get('duration')))
        self.logger.info("Video info - Title: %s, Duration: %ss", title, duration)

        if download_info.get('is_live'):
            return self._error('live_stream', title=title, platform=platform)

        filesize = self._estimate_filesize(platform, download_info)
        if filesize <= 0:
            self.logger.warning("File size information not available for %s", job.url)
        else:
            self.logger.info("Estimated file size: %.1fMB", filesize / MB)

        if filesize > target and not job.ignore_size_limit:
            if self._shrink_allowed(job) and self._can_shrink(filesize, duration, target):
                self.logger.info("Estimated %.1fMB is over the %dMB limit; will shrink it",
                                 filesize / MB, target // MB)
            else:
                self.logger.warning("File too large: %.1fMB (limit: %dMB)", filesize / MB, target // MB)
                return self._too_large(filesize, title, platform, duration, target, 'download')
        elif filesize > target:
            self.logger.warning(
                "Ignoring pre-download size limit for %s: %.1fMB", platform, filesize / MB
            )

        # Reuse the already-fetched info instead of re-requesting the page,
        # which is what causes 403s on TikTok's JS challenge.
        job.check_cancelled()
        self.logger.info("Starting download of %s from %s", title, platform)
        ydl.process_info(download_info)
        job.check_cancelled()

        filename = self._resolve_downloaded_path(ydl, download_info)
        if not filename:
            self.logger.error("Downloaded file not found for %s", job.url)
            self._purge_partials(download_info)
            return self._error('download_failed', title=title, platform=platform)

        actual_filesize = os.path.getsize(filename)
        compressed = False
        if job.mode != MODE_AUDIO:
            # Format selection asks for H.264/AAC first, but some sites only
            # serve WebM. A file that is about to be shrunk skips this step:
            # the shrink re-encodes to H.264/AAC anyway.
            if not (actual_filesize > target and self._shrink_allowed(job)):
                filename = self._ensure_ios_compatible_video(filename, platform)
                actual_filesize = os.path.getsize(filename)

            if actual_filesize > target and self._shrink_allowed(job):
                shrunk = self.shrink_to_fit(filename, target,
                                            duration or self.probe_duration(filename), job)
                if shrunk:
                    filename, compressed = shrunk, True
                    actual_filesize = os.path.getsize(filename)

        self.logger.info("Download complete - File: %s, Size: %.1fMB", filename, actual_filesize / MB)

        # The pre-download estimate is frequently missing or wrong, so
        # re-check against the real file before handing it to Telegram.
        if actual_filesize > target and not job.ignore_size_limit:
            self.logger.warning("Downloaded file is %.1fMB, above the %dMB limit; discarding",
                                actual_filesize / MB, target // MB)
            self.cleanup_file(filename)
            return self._too_large(actual_filesize, title, platform, duration, target, 'send')

        if job.mode == MODE_AUDIO:
            width = height = None
        elif compressed:
            width, height = self._probe_video_dimensions(filename)
        else:
            width, height = self._extract_dimensions(download_info)

        result = {
            'filepath': filename,
            'kind': 'audio' if job.mode == MODE_AUDIO else 'video',
            'title': title,
            'platform': platform,
            'duration': duration,
            'filesize': actual_filesize,
            'url': job.url,
            'width': width,
            'height': height,
            'compressed': compressed,
            **self._metadata(download_info, parent, platform=platform),
        }
        if job.mode == MODE_VIDEO:
            result['hd_available'] = self._hd_available(download_info, height)
        return result

    def _metadata(self, info: Dict[str, Any], parent: Optional[Dict[str, Any]] = None,
                  platform: Optional[str] = None) -> Dict[str, Any]:
        """Author, description and age rating for the caption."""
        sources = (info or {}, parent or {})

        def pick(*keys):
            for source in sources:
                for key in keys:
                    value = source.get(key)
                    if value not in (None, '', [], {}):
                        return value
            return None

        uploader = pick('uploader', 'channel', 'creator', 'uploader_id')
        uploader_url = pick('uploader_url', 'channel_url')
        if not uploader_url and platform == 'instagram' and pick('channel'):
            uploader_url = f"https://www.instagram.com/{pick('channel')}/"
        if not (isinstance(uploader_url, str) and re.match(r'https?://', uploader_url)):
            uploader_url = None

        try:
            age_limit = int(pick('age_limit') or 0)
        except (TypeError, ValueError):
            age_limit = 0

        description = pick('description')
        return {
            'uploader': str(uploader) if uploader else None,
            'uploader_url': uploader_url,
            'description': description if isinstance(description, str) else None,
            'age_limit': age_limit,
        }

    def _hd_available(self, info: Dict[str, Any], height: Optional[int]) -> bool:
        """Whether the site offers a taller video than the one delivered."""
        if not height:
            return False
        for fmt in info.get('formats') or []:
            if not isinstance(fmt, dict) or fmt.get('vcodec') == 'none':
                continue
            candidate = fmt.get('height') or 0
            if height < candidate <= self.hd_max_height:
                return True
        return False

    # ------------------------------------------------------------------
    # Shrinking oversized videos
    # ------------------------------------------------------------------

    #: (minimum video kbps, shorter side) - the resolution a bitrate can carry.
    _SHRINK_LADDER = ((3500, 1080), (1800, 720), (900, 540), (500, 480), (300, 360))

    def _shrink_plan(self, duration: Optional[float],
                     target_bytes: int) -> Optional[Tuple[int, int, int]]:
        """(video kbps, audio kbps, shorter side) that fit target_bytes, or None."""
        if not duration or duration <= 0:
            return None
        total_kbps = target_bytes * 8 * SHRINK_SAFETY / duration / 1000
        audio_kbps = 96 if total_kbps >= 1000 else 64 if total_kbps >= 400 else 48
        video_kbps = int(total_kbps - audio_kbps)
        if video_kbps < MIN_SHRINK_VIDEO_KBPS:
            return None
        short_side = 240
        for min_kbps, side in self._SHRINK_LADDER:
            if video_kbps >= min_kbps:
                short_side = side
                break
        return video_kbps, audio_kbps, short_side

    def _can_shrink(self, source_bytes: int, duration: Optional[float], target_bytes: int) -> bool:
        if not (self.compress_oversized and self.has_ffmpeg):
            return False
        if source_bytes > self.compress_max_source_mb * MB:
            self.logger.info("Not shrinking: %.0fMB source is above COMPRESS_MAX_SOURCE_MB",
                             source_bytes / MB)
            return False
        return self._shrink_plan(duration, target_bytes) is not None

    def shrink_to_fit(self, filepath: str, target_bytes: int, duration: Optional[float],
                      job: Optional[_Job] = None) -> Optional[str]:
        """Re-encode a video to H.264/AAC MP4 under target_bytes.

        Returns the new path (the original is removed), or None when the
        video cannot fit or ffmpeg fails, in which case the original is kept.
        """
        plan = self._shrink_plan(duration, target_bytes)
        ffmpeg_path = self._ffmpeg_binary()
        if plan is None or not ffmpeg_path or not os.path.exists(filepath):
            return None

        video_kbps, audio_kbps, short_side = plan
        original_size = os.path.getsize(filepath)
        output = f"{os.path.splitext(filepath)[0]}_small.mp4"
        self.logger.info("Shrinking %.1fMB video to fit %dMB (%dk video, %dk audio, %dp)",
                         original_size / MB, target_bytes // MB, video_kbps, audio_kbps, short_side)

        # The encoder's average bitrate can overshoot; one retry a notch lower
        # catches the rare file that lands just above the target.
        for factor in (1.0, 0.8):
            ok = self._run_shrink(ffmpeg_path, filepath, output, int(video_kbps * factor),
                                  audio_kbps, short_side, duration, target_bytes, job)
            if not ok:
                self.cleanup_file(output)
                return None
            size = os.path.getsize(output)
            if size <= target_bytes:
                self.logger.info("Shrunk to %.1fMB", size / MB)
                self.cleanup_file(filepath)
                return output
            self.logger.info("Shrunk file is %.1fMB, still above the limit; trying lower", size / MB)

        self.cleanup_file(output)
        return None

    def _run_shrink(self, ffmpeg_path: str, source: str, output: str, video_kbps: int,
                    audio_kbps: int, short_side: int, duration: float, target_bytes: int,
                    job: Optional[_Job]) -> bool:
        # Cap the shorter side (portrait videos too), never upscale, keep sizes even.
        scale = (
            f"scale='if(gt(iw,ih),-2,trunc(min(iw,{short_side})/2)*2)'"
            f":'if(gt(iw,ih),trunc(min(ih,{short_side})/2)*2,-2)'"
        )
        cmd = [
            ffmpeg_path, '-y', '-nostdin', '-loglevel', 'error', '-i', source,
            '-map', '0:v:0', '-map', '0:a:0?', '-vf', scale,
            '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-b:v', f'{video_kbps}k', '-maxrate', f'{int(video_kbps * 1.5)}k',
            '-bufsize', f'{video_kbps * 2}k',
            '-c:a', 'aac', '-b:a', f'{audio_kbps}k', '-ac', '2',
            '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', output,
        ]

        self._emit(job, {'status': 'compressing', 'percent': 0.0, 'target_bytes': target_bytes})
        deadline = time.monotonic() + 1800
        try:
            # stderr goes to a temp file: with -loglevel error it stays tiny,
            # and a pipe nobody reads could block ffmpeg if it ever filled up.
            with tempfile.TemporaryFile(mode='w+') as errors, \
                    subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True) as proc:
                try:
                    for line in proc.stdout:
                        if (job is not None and job.cancel_event is not None
                                and job.cancel_event.is_set()):
                            proc.kill()
                            proc.wait()
                            self.cleanup_file(output)
                            raise DownloadCancelled('Cancelled by user')
                        if time.monotonic() > deadline:
                            self.logger.warning("Shrinking took over 30 minutes, giving up")
                            proc.kill()
                            return False
                        key, _, value = line.strip().partition('=')
                        if key in ('out_time_us', 'out_time_ms') and value.isdigit() and duration:
                            percent = min(99.0, int(value) / 1_000_000 / duration * 100)
                            self._emit(job, {'status': 'compressing', 'percent': percent,
                                             'target_bytes': target_bytes})
                    proc.wait(timeout=60)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

                if proc.returncode != 0 or not os.path.exists(output):
                    errors.seek(0)
                    self.logger.warning("Shrinking failed: %s",
                                        errors.read().strip()[:300] or f"exit {proc.returncode}")
                    return False
                return True
        except OSError as exc:
            self.logger.warning("Could not run ffmpeg: %s", exc)
            return False

    # ------------------------------------------------------------------
    # TikTok photo posts
    # ------------------------------------------------------------------

    def _tiktok_item(self, url: str) -> Optional[Dict[str, Any]]:
        """The post JSON TikTok embeds in its page, for photo posts."""
        headers = {
            'User-Agent': BROWSER_USER_AGENT,
            'Referer': 'https://www.tiktok.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        candidates = [url]
        if '/photo/' in url:
            # The video page serves the same item JSON and is less often walled.
            candidates.append(url.replace('/photo/', '/video/'))

        for page_url in candidates:
            try:
                response = self._session.get(page_url, headers=headers, timeout=20)
                response.raise_for_status()
            except Exception as exc:
                self.logger.warning("Could not fetch TikTok page %s: %s", page_url, exc)
                continue
            match = re.search(
                r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                response.text, re.DOTALL,
            )
            if not match:
                continue
            try:
                scope = json.loads(match.group(1)).get('__DEFAULT_SCOPE__') or {}
            except ValueError:
                continue
            for key in ('webapp.video-detail', 'webapp.photo-detail', 'webapp.reflow.video.detail'):
                item = ((scope.get(key) or {}).get('itemInfo') or {}).get('itemStruct')
                if isinstance(item, dict) and (item.get('imagePost') or {}).get('images'):
                    return item
        return None

    def _download_tiktok_photos(self, url: str, job: _Job) -> Dict[str, Any]:
        """Download the images of a TikTok photo post; yt-dlp has no extractor for them."""
        self.logger.info("Fetching TikTok photo post %s", url)
        item = self._tiktok_item(url)
        if item is None:
            return self._error('tiktok_photo', platform='tiktok')

        image_urls: List[str] = []
        for image in (item.get('imagePost') or {}).get('images') or []:
            url_list = ((image or {}).get('imageURL') or {}).get('urlList') or []
            # Prefer a JPEG rendition; TikTok also offers HEIC/WebP ones.
            jpeg = [u for u in url_list if '.jpeg' in u or '.jpg' in u]
            if jpeg or url_list:
                image_urls.append((jpeg or url_list)[0])
        image_urls = image_urls[:self.max_album_items]
        if not image_urls:
            return self._error('tiktok_photo', platform='tiktok')

        author = item.get('author') or {}
        handle = author.get('uniqueId')
        meta = {
            'uploader': author.get('nickname') or handle,
            'uploader_url': f"https://www.tiktok.com/@{handle}" if handle else None,
            'description': item.get('desc') or None,
            'age_limit': 0,
        }
        title = (item.get('desc') or '').strip() or f"Photos by {meta['uploader'] or 'TikTok user'}"

        headers = {'User-Agent': BROWSER_USER_AGENT, 'Referer': 'https://www.tiktok.com/'}
        limit = self._target_bytes(job.ignore_size_limit)
        job.item_count = len(image_urls)
        items: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []
        for index, image_url in enumerate(image_urls, start=1):
            job.check_cancelled()
            job.item_index = index
            self._emit(job, {'status': 'downloading', 'percent': None,
                             'downloaded_bytes': 0, 'total_bytes': None})
            path = self._fetch_to_file(image_url, f"tiktok_{item.get('id') or 'photo'}_{index}",
                                       headers, limit, job)
            if path:
                items.append(self._photo_result(path, title, job, meta))
            else:
                failures.append(self._error('download_failed', platform='tiktok'))

        return self._album_result(items, failures, {'title': title, **meta}, job)

    # ------------------------------------------------------------------
    # 9GAG direct download fallback
    # ------------------------------------------------------------------

    def _extract_9gag_post_id(self, url: str) -> Optional[str]:
        """Extract 9GAG post ID from a /gag/{id} URL."""
        match = re.search(r'(?:https?://)?(?:www\.)?9gag\.com/gag/([a-zA-Z0-9]+)', url, re.IGNORECASE)
        return match.group(1) if match else None

    def _normalize_escaped_url(self, raw_url: str) -> str:
        """Normalize escaped URL fragments found in embedded JSON."""
        normalized = raw_url.strip().replace('\\/', '/')
        normalized = normalized.replace('\\u002F', '/')
        normalized = normalized.replace('&amp;', '&')
        return normalized

    def _extract_9gag_mp4_url(self, url: str) -> Optional[str]:
        """Extract a direct MP4 URL from a 9GAG post page using metadata and JSON fallbacks."""
        headers = {'Referer': 'https://9gag.com/',
                   'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
        post_id = self._extract_9gag_post_id(url)
        derived_candidates = []
        if post_id:
            derived_candidates = [
                f"https://img-9gag-fun.9cache.com/photo/{post_id}_460svav1.mp4",
                f"https://img-9gag-fun.9cache.com/photo/{post_id}_460sv.mp4",
                f"https://img-9gag-fun.9cache.com/photo/{post_id}_700b.mp4"
            ]

        html = None
        for page_url in [url, url.replace('://9gag.com', '://www.9gag.com')]:
            try:
                response = self._session.get(page_url, headers=headers, timeout=20)
                response.raise_for_status()
                html = response.text
                break
            except Exception as exc:
                self.logger.warning("Could not fetch 9GAG page HTML from %s: %s", page_url, exc)

        if html:
            meta_patterns = [
                r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+\.mp4[^"\']*)["\']',
                r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+\.mp4[^"\']*)["\']',
                r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']'
            ]

            for pattern in meta_patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    return self._normalize_escaped_url(match.group(1))

            json_url_match = re.search(r'https:\\/\\/[^"\']+\\.mp4[^"\']*', html)
            if json_url_match:
                return self._normalize_escaped_url(json_url_match.group(0))

        for candidate in derived_candidates:
            try:
                probe = self._session.head(candidate, headers=headers, timeout=10,
                                           allow_redirects=True)
                if probe.ok:
                    return candidate
            except Exception:
                continue

        # Final fallback: return first derived candidate and let direct download validate it.
        if derived_candidates:
            return derived_candidates[0]

        return None

    def _download_direct_media(self, media_url: str, source_url: str,
                               platform: str) -> Optional[Dict[str, Any]]:
        """Download a direct media URL and return a result object compatible with bot sending logic."""
        headers = {'Referer': 'https://9gag.com/', 'Accept': '*/*'}
        size_limit = self.max_filesize_bytes

        def too_large(filesize: int) -> Dict[str, Any]:
            return self._error(
                'file_too_large',
                message=f'Video file is too large to download (limit: {self.max_filesize_mb}MB)',
                filesize=filesize, title=basename, platform=platform, duration=None,
            )

        parsed = urlparse(media_url)
        basename = os.path.basename(parsed.path) or f"{platform}_video.mp4"
        if not basename.lower().endswith('.mp4'):
            basename = f"{basename}.mp4"

        timestamp = int(time.time())
        filename = os.path.join(self.download_dir, f"{timestamp}_{basename}")

        response = self._session.get(media_url, headers=headers, timeout=30, stream=True)
        response.raise_for_status()

        content_length = int(response.headers.get('Content-Length', 0) or 0)
        if content_length > size_limit:
            return too_large(content_length)

        written = 0
        with open(filename, 'wb') as output_file:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                output_file.write(chunk)
                written += len(chunk)
                if written > size_limit:
                    break

        if written > size_limit:
            self.cleanup_file(filename)
            return too_large(written)

        # Direct CDN files can use codec/container combinations that fail on
        # iPhone. Normalize to a compatible MP4 when the tools are available.
        filename = self._ensure_ios_compatible_video(filename, platform)
        written = os.path.getsize(filename)

        if written > size_limit:
            self.cleanup_file(filename)
            return too_large(written)

        width, height = self._probe_video_dimensions(filename)
        return {
            'filepath': filename,
            'kind': 'video',
            'title': os.path.splitext(basename)[0],
            'platform': platform,
            'duration': self.probe_duration(filename),
            'filesize': written,
            'url': source_url,
            'width': width,
            'height': height
        }

    # ------------------------------------------------------------------
    # ffmpeg helpers
    # ------------------------------------------------------------------

    def _ffprobe_entries(self, filepath: str, entries: str,
                         select_streams: Optional[str] = None) -> Optional[Dict[str, str]]:
        """Run ffprobe and return the requested entries as a key -> value dict.

        Returns None when ffprobe is unavailable or fails to run, an empty dict
        when it ran but found nothing (e.g. the stream does not exist).
        """
        ffprobe_path = self._ffprobe_binary()
        if not ffprobe_path or not os.path.exists(filepath):
            return None
        cmd = [ffprobe_path, '-v', 'error']
        if select_streams:
            cmd += ['-select_streams', select_streams]
        cmd += ['-show_entries', entries,
                '-of', 'default=noprint_wrappers=1:nokey=0', filepath]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
        except Exception as exc:
            self.logger.debug("ffprobe failed for %s: %s", filepath, exc)
            return None
        values: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition('=')
            if sep:
                values[key.strip()] = value.strip()
        return values

    def _probe_video_dimensions(self, filepath: str) -> Tuple[Optional[int], Optional[int]]:
        """Return (width, height) of the video file using ffprobe, or (None, None) on failure."""
        values = self._ffprobe_entries(filepath, 'stream=width,height', 'v:0') or {}

        def to_int(raw: Optional[str]) -> Optional[int]:
            try:
                return int(raw) if raw else None
            except ValueError:
                return None

        return to_int(values.get('width')), to_int(values.get('height'))

    def probe_duration(self, filepath: str) -> Optional[float]:
        """Return the media duration in seconds via ffprobe, or None."""
        values = self._ffprobe_entries(filepath, 'format=duration') or {}
        return self._normalize_duration(values.get('duration'))

    def generate_thumbnail(self, filepath: str) -> Optional[str]:
        """Extract a poster frame so Telegram shows something other than a black box.

        Returns the thumbnail path, or None when ffmpeg is unavailable or fails.
        """
        ffmpeg_path = self._ffmpeg_binary()
        if not ffmpeg_path or not os.path.exists(filepath):
            return None

        thumb_path = f"{os.path.splitext(filepath)[0]}.thumb.jpg"
        cmd = [
            ffmpeg_path, '-y', '-loglevel', 'error',
            '-ss', '1', '-i', filepath, '-frames:v', '1',
            # Telegram wants a JPEG of at most 320px on the long side, under 200kB.
            '-vf', "scale='min(320,iw)':'min(320,ih)':force_original_aspect_ratio=decrease",
            '-q:v', '6', thumb_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
            if proc.returncode != 0 or not os.path.exists(thumb_path):
                return None
            if os.path.getsize(thumb_path) > 200 * 1024:
                self.cleanup_file(thumb_path)
                return None
            return thumb_path
        except Exception as exc:
            self.logger.debug("Thumbnail generation failed: %s", exc)
            return None

    #: Video codecs Apple's decoders accept. Everything WebM carries (VP8, VP9,
    #: AV1) is absent, which is why those files fail on iPhone and macOS.
    APPLE_VIDEO_CODECS = frozenset({'h264', 'hevc'})

    #: Audio codecs Apple accepts in an MP4. Opus and Vorbis are not among them.
    APPLE_AUDIO_CODECS = frozenset({'aac', 'mp4a', 'alac'})

    def _ensure_ios_compatible_video(self, filepath: str, platform: str) -> str:
        """Normalize a download to H.264/AAC MP4 so it plays on iPhone and macOS.

        Chooses the cheapest repair that works: streams already in an Apple
        codec are copied rather than re-encoded, so a WebM holding H.264 is
        only rewrapped, and an MP4 whose audio alone is Opus keeps its video
        untouched. Returns the original path unchanged if anything goes wrong,
        so a normalization failure can never lose the download.
        """
        if not self.force_mp4 or not os.path.exists(filepath):
            return filepath

        ffmpeg_path = self._ffmpeg_binary()
        ffprobe_path = self._ffprobe_binary()
        if not ffmpeg_path or not ffprobe_path:
            self.logger.warning("ffmpeg/ffprobe not found, skipping MP4 normalization")
            return filepath

        video_info = self._ffprobe_entries(filepath, 'stream=codec_name,pix_fmt', 'v:0')
        audio_info = self._ffprobe_entries(filepath, 'stream=codec_name', 'a:0')
        format_info = self._ffprobe_entries(filepath, 'format=format_name')
        if video_info is None or audio_info is None or format_info is None:
            self.logger.warning("ffprobe failed, skipping compatibility check")
            return filepath

        video_codec = video_info.get('codec_name', '').lower() or None
        pix_fmt = video_info.get('pix_fmt', '').lower() or None
        audio_codec = audio_info.get('codec_name', '').lower() or None
        # ffprobe reports MP4 as the family "mov,mp4,m4a,3gp,3g2,mj2".
        container = format_info.get('format_name', '').lower()

        # A missing stream is fine; only a present-and-wrong one needs work.
        video_ok = video_codec is None or video_codec in self.APPLE_VIDEO_CODECS
        audio_ok = audio_codec is None or audio_codec in self.APPLE_AUDIO_CODECS
        # 10-bit/4:4:4 H.264 decodes nowhere on iOS, so treat it as incompatible.
        pixels_ok = pix_fmt is None or pix_fmt in ('yuv420p', 'yuvj420p')

        keep_video = video_ok and pixels_ok
        cmd = [ffmpeg_path, '-y', '-loglevel', 'error', '-i', filepath]

        if keep_video:
            cmd += ['-c:v', 'copy']
            # Apple rejects HEVC tagged hev1; hvc1 is the same bitstream.
            if video_codec == 'hevc':
                cmd += ['-tag:v', 'hvc1']
        else:
            cmd += ['-c:v', 'libx264', '-profile:v', 'main', '-level', '4.0',
                    '-pix_fmt', 'yuv420p', '-crf', '23', '-preset', 'veryfast']

        if audio_ok:
            cmd += ['-c:a', 'copy']
        else:
            cmd += ['-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100']

        # +faststart moves the moov atom to the front so playback can begin
        # before the whole file arrives - Telegram's inline player needs it.
        cmd += ['-movflags', '+faststart']

        base, ext = os.path.splitext(filepath)
        output_path = f"{base}_ios.mp4"
        if os.path.abspath(output_path) == os.path.abspath(filepath):
            output_path = f"{base}_ios_norm.mp4"
        cmd.append(output_path)

        if keep_video and audio_ok and 'mp4' in container:
            action = "Remuxing %s video with faststart"
        else:
            action = "Normalizing %s video to H.264/AAC MP4 for Apple playback"
        self.logger.info(
            action + " (container=%s, video=%s, audio=%s)",
            platform, ext.lstrip('.') or 'unknown', video_codec, audio_codec,
        )

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
            if proc.returncode != 0 or not os.path.exists(output_path):
                self.logger.warning(
                    "MP4 normalization failed (%s), using the original file",
                    (proc.stderr or '').strip()[:200] or f"exit {proc.returncode}",
                )
                self.cleanup_file(output_path)
                return filepath
            self.cleanup_file(filepath)
            return output_path
        except Exception as ffmpeg_error:
            self.logger.warning("MP4 normalization error: %s", ffmpeg_error)
            self.cleanup_file(output_path)
            return filepath

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    @classmethod
    def _resolve_chromedriver(cls) -> str:
        """Resolve (and cache) the chromedriver path; the lookup hits the network."""
        with cls._chromedriver_lock:
            if cls._chromedriver_path and os.path.exists(cls._chromedriver_path):
                return cls._chromedriver_path

            from webdriver_manager.chrome import ChromeDriverManager

            driver_path = ChromeDriverManager().install()
            # webdriver-manager occasionally returns a sibling notice file.
            if os.path.basename(driver_path) != 'chromedriver':
                driver_path = os.path.join(os.path.dirname(driver_path), 'chromedriver')

            try:
                import stat
                os.chmod(driver_path, os.stat(driver_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError as exc:
                logging.getLogger(__name__).warning("Could not chmod chromedriver: %s", exc)

            cls._chromedriver_path = driver_path
            return driver_path

    def take_screenshot(self, url: str) -> Optional[Dict[str, Any]]:
        """Take a screenshot of a web page

        Args:
            url: URL to take screenshot of

        Returns:
            Dictionary with screenshot info if successful, None otherwise
            Contains: 'filepath', 'title', 'platform', 'filesize'
        """
        self.logger.info("Taking screenshot of URL: %s", url)

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.common.by import By
            from selenium.common.exceptions import (
                NoSuchElementException, TimeoutException, WebDriverException,
            )
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            self.logger.error("Selenium is not installed, cannot take screenshots: %s", exc)
            return None

        platform = self.detect_platform(url)

        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--blink-settings=imagesEnabled=true')

        if platform == 'threads':
            self.logger.info("Using mobile emulation for Threads post")
            chrome_options.add_experimental_option('mobileEmulation', {
                'deviceMetrics': {'width': 375, 'height': 812, 'pixelRatio': 3.0},
                'userAgent': MOBILE_USER_AGENT,
            })
        else:
            chrome_options.add_argument('--window-size=1280,1024')

        timestamp = int(time.time() * 1000)
        filename = os.path.join(self.download_dir, f"screenshot_{timestamp}.png")

        driver = None
        try:
            driver_path = self._resolve_chromedriver()
            driver = webdriver.Chrome(service=Service(driver_path), options=chrome_options)
            driver.set_page_load_timeout(45)

            self.logger.info("Navigating to URL: %s", url)
            driver.get(url)

            if platform == 'threads':
                self._dismiss_threads_interstitials(driver, WebDriverWait, EC, By,
                                                    TimeoutException, NoSuchElementException)

            time.sleep(3)

            self.logger.info("Saving screenshot to: %s", filename)
            if not driver.save_screenshot(filename):
                self.logger.error("Selenium reported a failed screenshot for %s", url)
                return None

            title = driver.title or (platform.title() if platform else 'Screenshot')
            filesize = os.path.getsize(filename)

            self.logger.info("Screenshot taken: %s (%.2fMB)", filename, filesize / MB)

            return {
                'filepath': filename,
                'title': title,
                'platform': platform,
                'filesize': filesize,
                'url': url,
                'is_screenshot': True
            }

        except TimeoutException:
            self.logger.error("Timeout while loading URL: %s", url)
        except WebDriverException as exc:
            self.logger.error("WebDriver error while screenshotting %s: %s", url, exc)
        except Exception as exc:
            self.logger.error("Error taking screenshot of %s: %s", url, exc)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                self.logger.debug("Chrome driver closed")

        # Only reached on failure; make sure no half-written PNG is left behind.
        self.cleanup_file(filename)
        return None

    def _dismiss_threads_interstitials(self, driver, WebDriverWait, EC, By,
                                       TimeoutException, NoSuchElementException) -> None:
        """Click through the "continue in browser" / "open in Safari" prompts."""
        prompt_stages = (
            (
                "continue in browser",
                [
                    "//button[text()='Continue in browser']",
                    "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz'), 'continue in browser')]",
                    "//*[contains(text(), 'Continue in browser')]",
                    "//button[contains(text(), 'Continue')]",
                ],
                12,
            ),
            (
                "open in browser",
                [
                    "//button[text()='Continue']",
                    "//div[contains(text(), 'Safari')]/following::button",
                    "//button[contains(text(), 'Continue')]",
                ],
                6,
            ),
        )

        for label, selectors, timeout in prompt_stages:
            button = None
            wait = WebDriverWait(driver, timeout)
            for selector in selectors:
                try:
                    button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                    break
                except (TimeoutException, NoSuchElementException):
                    continue

            if not button:
                self.logger.debug("No '%s' prompt found, continuing", label)
                continue

            self.logger.info("Dismissing Threads '%s' prompt", label)
            try:
                driver.execute_script("arguments[0].scrollIntoView(true);", button)
                time.sleep(0.5)
                try:
                    button.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", button)
                time.sleep(3)
            except Exception as exc:
                self.logger.warning("Could not dismiss the '%s' prompt: %s", label, exc)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def cleanup_file(self, filepath: Optional[str]) -> bool:
        """Remove downloaded file

        Args:
            filepath: Path to file to remove

        Returns:
            True if file was removed successfully, False otherwise
        """
        if not filepath:
            return False
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                self.logger.debug("Removed file: %s", filepath)
                return True
        except OSError as exc:
            self.logger.error("Error removing file %s: %s", filepath, exc)
        return False

    def cleanup_old_files(self, max_age_hours: int = 6) -> int:
        """Delete stale downloads left behind by crashes or failed uploads.

        Args:
            max_age_hours: Files older than this are removed. 0 disables cleanup.

        Returns:
            Number of files removed.
        """
        if max_age_hours <= 0:
            return 0

        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        try:
            entries = os.listdir(self.download_dir)
        except OSError as exc:
            self.logger.warning("Could not list %s for cleanup: %s", self.download_dir, exc)
            return 0

        for name in entries:
            path = os.path.join(self.download_dir, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) > cutoff:
                    continue
            except OSError:
                continue
            if self.cleanup_file(path):
                removed += 1

        if removed:
            self.logger.info("Removed %d stale file(s) from %s", removed, self.download_dir)
        return removed

    def close(self) -> None:
        """Release network resources held by the downloader."""
        try:
            self._session.close()
        except Exception:
            pass
