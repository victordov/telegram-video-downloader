"""
Video downloader module using yt-dlp for multiple platforms.

Supports YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG and VK.
Also supports taking screenshots of Threads posts when they're not videos.

Selenium is only imported when a screenshot is actually needed, so the bot runs
fine on machines without a browser as long as no Threads screenshots are taken.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yt_dlp

MB = 1024 * 1024

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
}

#: (needle in the yt-dlp error, error code). Checked in order, first match wins.
_ERROR_SIGNATURES: List[Tuple[str, str]] = [
    ('this content is only available for registered users', 'registered_users_only'),
    ('login required', 'login_required'),
    ('log in', 'login_required'),
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
            r'(?:https?://)?(?:www\.|m\.)?tiktok\.com/@[^/]+/video/([0-9]+)',
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
        ]
    }

    #: Patterns are matched on every incoming message, so compile them once.
    COMPILED_URL_PATTERNS = {
        platform: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
        for platform, patterns in URL_PATTERNS.items()
    }

    #: Platforms whose links always point at a video.
    ALWAYS_VIDEO_PLATFORMS = frozenset(
        {'youtube', 'tiktok', 'facebook', 'twitter', '9gag', 'vk', 'instagram'}
    )

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
                 retries: int = 3):
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
        """
        self.download_dir = download_dir or tempfile.gettempdir()
        self.max_filesize_mb = max_filesize_mb
        self.max_video_height = max_video_height
        self.cookie_file = cookie_file
        self.cookies_from_browser = cookies_from_browser
        self.ffmpeg_location = ffmpeg_location
        self.concurrent_fragments = concurrent_fragments
        self.socket_timeout = socket_timeout
        self.retries = retries
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

    def is_video_url(self, url: str) -> bool:
        """Check if URL is from a supported video platform

        Args:
            url: URL to check

        Returns:
            True if URL is from supported platform, False otherwise
        """
        return self.detect_platform(url) is not None

    def check_for_video(self, url: str) -> bool:
        """Check if a URL contains a video

        Only Threads posts need an actual lookup; every other supported platform
        links straight to a video. The Threads check is a plain HTTP fetch, which
        is orders of magnitude cheaper than spinning up a headless browser.

        Args:
            url: URL to check

        Returns:
            True if URL contains a video, False otherwise
        """
        platform = self.detect_platform(url)

        if not platform:
            self.logger.info("URL is not from a supported platform: %s", url)
            return False

        if platform != 'threads':
            return True

        return self._threads_has_video(url)

    def _threads_has_video(self, url: str) -> bool:
        """Cheap HTTP probe for a video on a Threads post."""
        try:
            response = self._session.get(
                url,
                headers={'User-Agent': MOBILE_USER_AGENT},
                timeout=15,
                allow_redirects=True,
            )
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            # Inconclusive: let yt-dlp try, it falls back to a screenshot anyway.
            self.logger.warning("Could not probe Threads post %s (%s), assuming video", url, exc)
            return True

        video_markers = (
            'og:video',
            'video_versions',
            '"is_video":true',
            'twitter:player:stream',
            '<video',
        )
        for marker in video_markers:
            if marker in html:
                self.logger.info("Threads post %s looks like a video (marker: %s)", url, marker)
                return True

        self.logger.info("Threads post %s has no video markers, will screenshot", url)
        return False

    # ------------------------------------------------------------------
    # yt-dlp configuration
    # ------------------------------------------------------------------

    def _format_selector(self, platform: str) -> str:
        """Build a format string that prefers the largest video under the limit."""
        height = min(self.PLATFORM_MAX_HEIGHT.get(platform, self.max_video_height),
                     self.max_video_height)
        size = self.max_filesize_mb

        if self.has_ffmpeg:
            # Separate video+audio streams give much better quality per byte, but
            # they need ffmpeg to be merged back together.
            return (
                f'bv*[height<=?{height}][filesize<{size}M]+ba/'
                f'b[height<=?{height}][filesize<{size}M]/'
                f'bv*[height<=?{height}][filesize_approx<{size}M]+ba/'
                f'b[height<=?{height}][filesize_approx<{size}M]/'
                f'bv*[height<=?{height}]+ba/b[height<=?{height}]/bv*+ba/b'
            )

        return (
            f'b[height<=?{height}][filesize<{size}M]/'
            f'b[height<=?{height}][filesize_approx<{size}M]/'
            f'b[filesize<{size}M]/b[height<=?{height}]/b'
        )

    def get_ydl_opts(self, platform: str) -> Dict[str, Any]:
        """Get yt-dlp options for specific platform

        Args:
            platform: Platform name

        Returns:
            Dictionary of yt-dlp options
        """
        opts: Dict[str, Any] = {
            # Include the id so two downloads of different videos with the same
            # title cannot clobber each other, and cap the title length so long
            # titles do not blow past the filesystem's name limit.
            'outtmpl': os.path.join(self.download_dir, '%(title).80B [%(id)s].%(ext)s'),
            'format': self._format_selector(platform),
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
        if self.cookie_file and os.path.exists(self.cookie_file):
            opts['cookiefile'] = self.cookie_file
        elif self.cookie_file:
            self.logger.warning("COOKIE_FILE %s does not exist, ignoring", self.cookie_file)
        if self.cookies_from_browser:
            opts['cookiesfrombrowser'] = tuple(self.cookies_from_browser.split(':'))

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
            opts.update(overrides)

        self.logger.debug("yt-dlp options for %s: format=%s", platform, opts['format'])
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

    def _build_hooks(self, progress_callback: Optional[ProgressCallback]) -> List[Callable]:
        hooks: List[Callable] = [self.progress_hook]
        if progress_callback is None:
            return hooks

        def forward(d: Dict[str, Any]) -> None:
            try:
                progress_callback(self.normalize_progress(d))
            except Exception as exc:  # never let UI errors abort a download
                self.logger.debug("Progress callback failed: %s", exc)

        hooks.append(forward)
        return hooks

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

        # yt-dlp may have remuxed to a different container.
        for candidate in candidates:
            if not candidate:
                continue
            base_name = os.path.splitext(candidate)[0]
            for ext in ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v'):
                test_path = base_name + ext
                if os.path.exists(test_path):
                    return test_path
            # Multi-video posts get a " #1" suffix appended by yt-dlp.
            for index in range(1, 10):
                for ext in ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v'):
                    test_path = f"{base_name} #{index}{ext}"
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
                       progress_callback: Optional[ProgressCallback] = None
                       ) -> Optional[Dict[str, Any]]:
        """Download video from URL or take screenshot of Threads post

        Args:
            url: URL to process (video URL or Threads post URL)
            ignore_size_limit: If True, download even when the estimate exceeds the limit
            progress_callback: Optional callable invoked with normalized progress dicts

        Returns:
            Dictionary with download info if successful, None otherwise
            For videos: Contains 'filepath', 'title', 'platform', 'duration', 'filesize'
            For screenshots: Contains 'filepath', 'title', 'platform', 'filesize', 'is_screenshot'
        """
        self.logger.info("Processing download request for URL: %s", url)

        platform = self.detect_platform(url)
        if not platform:
            self.logger.error("Unsupported URL format: %s", url)
            return None

        size_limit = self.max_filesize_bytes
        ydl_opts = self.get_ydl_opts(platform)
        ydl_opts['progress_hooks'] = self._build_hooks(progress_callback)

        download_info: Dict[str, Any] = {}
        try:
            self.logger.info("Extracting video information from %s", url)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError('Extractor returned no information')

                download_info = self._select_download_info(info)
                if platform == 'twitter':
                    download_info = self._optimize_twitter_format(download_info, size_limit)

                title = download_info.get('title') or info.get('title') or 'Unknown'
                duration = self._normalize_duration(
                    download_info.get('duration', info.get('duration'))
                )
                self.logger.info("Video info - Title: %s, Duration: %ss", title, duration)

                if download_info.get('is_live'):
                    return self._error('live_stream', title=title, platform=platform)

                filesize = self._estimate_filesize(platform, download_info)
                if filesize <= 0:
                    self.logger.warning("File size information not available for %s", url)
                else:
                    self.logger.info("Estimated file size: %.1fMB", filesize / MB)

                if filesize > size_limit and not ignore_size_limit:
                    self.logger.warning(
                        "File too large: %.1fMB (limit: %dMB)", filesize / MB, self.max_filesize_mb
                    )
                    return self._error(
                        'file_too_large',
                        message=f'Video file is too large to download (limit: {self.max_filesize_mb}MB)',
                        filesize=filesize, title=title, platform=platform, duration=duration,
                    )
                if filesize > size_limit:
                    self.logger.warning(
                        "Ignoring pre-download size limit for %s: %.1fMB", platform, filesize / MB
                    )

                # Reuse the already-fetched info instead of re-requesting the page,
                # which is what causes 403s on TikTok's JS challenge.
                self.logger.info("Starting download of %s from %s", title, platform)
                ydl.process_info(download_info)

                filename = self._resolve_downloaded_path(ydl, download_info)
                if not filename:
                    self.logger.error("Downloaded file not found for %s", url)
                    self._purge_partials(download_info)
                    return self._error('download_failed', title=title, platform=platform)

                actual_filesize = os.path.getsize(filename)
                self.logger.info(
                    "Download complete - File: %s, Size: %.1fMB", filename, actual_filesize / MB
                )

                # The pre-download estimate is frequently missing or wrong, so
                # re-check against the real file before handing it to Telegram.
                if actual_filesize > size_limit and not ignore_size_limit:
                    self.logger.warning(
                        "Downloaded file is %.1fMB, above the %dMB limit; discarding",
                        actual_filesize / MB, self.max_filesize_mb,
                    )
                    self.cleanup_file(filename)
                    return self._error(
                        'file_too_large',
                        message=f'Video file is too large to send (limit: {self.max_filesize_mb}MB)',
                        filesize=actual_filesize, title=title, platform=platform, duration=duration,
                    )

                width, height = self._extract_dimensions(download_info)

                return {
                    'filepath': filename,
                    'title': title,
                    'platform': platform,
                    'duration': duration,
                    'filesize': actual_filesize,
                    'url': url,
                    'width': width,
                    'height': height,
                }

        except Exception as exc:
            error_message = str(exc).strip() or f"{type(exc).__name__}: {exc!r}"
            self.logger.error("Error downloading %s: %s", url, error_message)
            self._purge_partials(download_info)

            if platform == 'tiktok' and '/photo/' in error_message:
                self.logger.warning("Detected TikTok photo URL (not a video): %s", url)
                return self._error('tiktok_photo', platform=platform)

            if platform == 'threads':
                self.logger.info("Video download failed for Threads URL, taking a screenshot instead")
                screenshot_result = self.take_screenshot(url)
                if screenshot_result:
                    return screenshot_result
                return self._error('threads_screenshot_failed', platform=platform)

            if platform == '9gag':
                self.logger.info("Attempting 9GAG direct MP4 fallback")
                try:
                    mp4_url = self._extract_9gag_mp4_url(url)
                    if mp4_url:
                        return self._download_direct_media(mp4_url, source_url=url, platform='9gag')
                    return self._error('no_video_found', platform=platform)
                except Exception as fallback_error:
                    self.logger.error("9GAG fallback failed for %s: %s", url, fallback_error)
                    return self._error('download_failed', platform=platform)

            return self._error(self.classify_error(error_message), platform=platform)

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
                response = requests.get(page_url, headers=headers, timeout=20)
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
                probe = requests.head(candidate, headers=headers, timeout=10, allow_redirects=True)
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

        parsed = urlparse(media_url)
        basename = os.path.basename(parsed.path) or f"{platform}_video.mp4"
        if not basename.lower().endswith('.mp4'):
            basename = f"{basename}.mp4"

        timestamp = int(time.time())
        filename = os.path.join(self.download_dir, f"{timestamp}_{basename}")

        response = requests.get(media_url, headers=headers, timeout=30, stream=True)
        response.raise_for_status()

        content_length = int(response.headers.get('Content-Length', 0) or 0)
        if content_length > size_limit:
            return self._error(
                'file_too_large',
                message=f'Video file is too large to download (limit: {self.max_filesize_mb}MB)',
                filesize=content_length, title=basename, platform=platform, duration=None,
            )

        written = 0
        oversized = False
        with open(filename, 'wb') as output_file:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                output_file.write(chunk)
                written += len(chunk)
                if written > size_limit:
                    oversized = True
                    break

        if oversized:
            self.cleanup_file(filename)
            return self._error(
                'file_too_large',
                message=f'Video file is too large to download (limit: {self.max_filesize_mb}MB)',
                filesize=written, title=basename, platform=platform, duration=None,
            )

        # 9GAG direct files can use codec/container combinations that fail on iPhone.
        # Normalize to a more broadly compatible MP4 when tools are available.
        filename = self._ensure_ios_compatible_video(filename, platform)
        written = os.path.getsize(filename)

        if written > size_limit:
            self.cleanup_file(filename)
            return self._error(
                'file_too_large',
                message=f'Video file is too large to download (limit: {self.max_filesize_mb}MB)',
                filesize=written, title=basename, platform=platform, duration=None,
            )

        width, height = self._probe_video_dimensions(filename)
        return {
            'filepath': filename,
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

    def _probe_video_dimensions(self, filepath: str) -> Tuple[Optional[int], Optional[int]]:
        """Return (width, height) of the video file using ffprobe, or (None, None) on failure."""
        ffprobe_path = self._ffprobe_binary()
        if not ffprobe_path or not os.path.exists(filepath):
            return None, None
        try:
            cmd = [
                ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'default=noprint_wrappers=1:nokey=0', filepath,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            width = height = None
            for line in result.stdout.splitlines():
                if line.startswith('width='):
                    width = int(line.split('=', 1)[1].strip())
                elif line.startswith('height='):
                    height = int(line.split('=', 1)[1].strip())
            return width, height
        except Exception:
            return None, None

    def probe_duration(self, filepath: str) -> Optional[float]:
        """Return the media duration in seconds via ffprobe, or None."""
        ffprobe_path = self._ffprobe_binary()
        if not ffprobe_path or not os.path.exists(filepath):
            return None
        try:
            cmd = [
                ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', filepath,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            return self._normalize_duration(result.stdout.strip())
        except Exception:
            return None

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

    def _ensure_ios_compatible_video(self, filepath: str, platform: str) -> str:
        """Repackage/transcode fallback videos to improve iPhone playback compatibility."""
        if platform != '9gag' or not os.path.exists(filepath):
            return filepath

        ffmpeg_path = self._ffmpeg_binary()
        ffprobe_path = self._ffprobe_binary()
        if not ffmpeg_path or not ffprobe_path:
            self.logger.warning("ffmpeg/ffprobe not found, skipping iOS compatibility normalization")
            return filepath

        video_codec = None
        audio_codec = None
        pix_fmt = None
        try:
            probe_cmd = [
                ffprobe_path, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,pix_fmt',
                '-of', 'default=noprint_wrappers=1:nokey=0', filepath,
            ]
            video_probe = subprocess.run(probe_cmd, capture_output=True, text=True,
                                         check=False, timeout=60)
            for line in video_probe.stdout.splitlines():
                if line.startswith('codec_name='):
                    video_codec = line.split('=', 1)[1].strip().lower()
                elif line.startswith('pix_fmt='):
                    pix_fmt = line.split('=', 1)[1].strip().lower()

            audio_cmd = [
                ffprobe_path, '-v', 'error', '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name',
                '-of', 'default=noprint_wrappers=1:nokey=0', filepath,
            ]
            audio_probe = subprocess.run(audio_cmd, capture_output=True, text=True,
                                         check=False, timeout=60)
            for line in audio_probe.stdout.splitlines():
                if line.startswith('codec_name='):
                    audio_codec = line.split('=', 1)[1].strip().lower()
                    break
        except Exception as probe_error:
            self.logger.warning("ffprobe failed, skipping compatibility check: %s", probe_error)
            return filepath

        needs_transcode = (
            video_codec != 'h264' or
            (audio_codec not in (None, 'aac', 'mp4a')) or
            (pix_fmt not in (None, 'yuv420p'))
        )

        base, ext = os.path.splitext(filepath)
        output_path = f"{base}_ios{ext or '.mp4'}"

        if needs_transcode:
            cmd = [
                ffmpeg_path, '-y', '-loglevel', 'error', '-i', filepath,
                '-c:v', 'libx264', '-profile:v', 'main', '-level', '4.0',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100',
                output_path,
            ]
            self.logger.info("Transcoding 9GAG video for iPhone compatibility")
        else:
            cmd = [
                ffmpeg_path, '-y', '-loglevel', 'error', '-i', filepath,
                '-c', 'copy', '-movflags', '+faststart', output_path,
            ]
            self.logger.info("Remuxing 9GAG video with faststart for better compatibility")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=600)
            if proc.returncode != 0 or not os.path.exists(output_path):
                self.logger.warning("Compatibility normalization failed, using original fallback file")
                self.cleanup_file(output_path)
                return filepath
            self.cleanup_file(filepath)
            return output_path
        except Exception as ffmpeg_error:
            self.logger.warning("Compatibility normalization error: %s", ffmpeg_error)
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


# Test function
def test_downloader():
    """Print platform detection results for a handful of sample URLs"""
    downloader = VideoDownloader()

    test_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.instagram.com/p/example/",
        "https://www.instagram.com/reels/DJJ4ngeoYbx/",
        "https://www.tiktok.com/@user/video/123456789",
        "https://vt.tiktok.com/ZSkUakqex/",
        "https://www.facebook.com/watch/?v=123456789",
        "https://twitter.com/username/status/1234567890",
        "https://x.com/username/status/1234567890",
        "https://t.co/abcdef123",
        "https://www.9gag.com/gag/avyKyAd",
        "https://vk.com/video-12345_67890",
        "https://example.com/not-a-video",
    ]

    for url in test_urls:
        print(f"{url} -> {downloader.detect_platform(url)}")


if __name__ == "__main__":
    test_downloader()
