"""
Video downloader module using yt-dlp for multiple platforms
Supports YouTube, Instagram, Facebook, TikTok, Twitter/X, Threads, 9GAG, and VK
Also supports taking screenshots of Threads posts when they're not videos
"""

import os
import re
import tempfile
import logging
import time
import subprocess
import shutil
from typing import Optional, Dict, Any
from urllib.parse import urlparse
import requests
import yt_dlp
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException


class VideoDownloader:
    """Unified video downloader for multiple platforms"""

    # URL patterns for supported platforms
    URL_PATTERNS = {
        'youtube': [
            r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/)([a-zA-Z0-9_-]{11})',
            r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]+)'
        ],
        'instagram': [
            r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/([a-zA-Z0-9_-]+)',
            r'(?:https?://)?(?:www\.)?instagram\.com/stories/[^/]+/([0-9]+)',
            r'(?:https?://)?(?:www\.)?instagram\.com/reels/([a-zA-Z0-9_-]+)'
        ],
        'tiktok': [
            r'(?:https?://)?(?:www\.)?tiktok\.com/@[^/]+/video/([0-9]+)',
            r'(?:https?://)?vm\.tiktok\.com/([a-zA-Z0-9]+)',
            r'(?:https?://)?vt\.tiktok\.com/([a-zA-Z0-9]+)',
            r'(?:https?://)?(?:www\.)?tiktok\.com/t/([a-zA-Z0-9]+)'
        ],
        'facebook': [
            r'(?:https?://)?(?:www\.)?facebook\.com/[^/]+/videos/([0-9]+)',
            r'(?:https?://)?(?:www\.)?facebook\.com/watch/?\?v=([0-9]+)',
            r'(?:https?://)?(?:www\.)?fb\.watch/([a-zA-Z0-9_-]+)'
        ],
        'twitter': [
            r'(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[^/]+/status/([0-9]+)',
            r'(?:https?://)?(?:www\.)?t\.co/([a-zA-Z0-9]+)'
        ],
        'threads': [
            r'(?:https?://)?(?:www\.)?threads\.com/@[^/]+/post/([a-zA-Z0-9_-]+)',
            r'(?:https?://)?(?:www\.)?threads\.net/@[^/]+/post/([a-zA-Z0-9_-]+)'
        ],
        '9gag': [
            r'(?:https?://)?(?:www\.)?9gag\.com/gag/([a-zA-Z0-9]+)'
        ],
        'vk': [
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/video-?\d+_\d+',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/videos-?\d+\?.*z=video',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/clip-?\d+_\d+',
            r'(?:https?://)?(?:www\.|m\.)?vk\.com/[^/]+\?.*z=video',
            r'(?:https?://)?(?:www\.|m\.)?vkvideo\.ru/video-?\d+_\d+'
        ]
    }

    def __init__(self, download_dir: Optional[str] = None):
        """Initialize the video downloader

        Args:
            download_dir: Directory to save downloaded videos. If None, uses temp directory.
        """
        self.download_dir = download_dir or tempfile.gettempdir()
        self.logger = logging.getLogger(__name__)

        # Ensure download directory exists
        os.makedirs(self.download_dir, exist_ok=True)

    def detect_platform(self, url: str) -> Optional[str]:
        """Detect which platform a URL belongs to

        Args:
            url: Video URL to check

        Returns:
            Platform name if detected, None otherwise
        """
        self.logger.info(f"Detecting platform for URL: {url}")
        for platform, patterns in self.URL_PATTERNS.items():
            self.logger.info(f"Checking patterns for platform: {platform}")
            for i, pattern in enumerate(patterns):
                self.logger.info(f"Trying pattern {i+1}/{len(patterns)} for {platform}: {pattern}")
                match = re.search(pattern, url, re.IGNORECASE)
                if match:
                    self.logger.info(f"✅ Pattern matched! Platform detected: {platform} for URL: {url}")
                    self.logger.info(f"Match details: {match.group(0)}")
                    return platform
                else:
                    self.logger.info(f"❌ Pattern did not match for {platform}")
        self.logger.info(f"No platform detected for URL: {url} after checking all patterns")
        return None

    def is_video_url(self, url: str) -> bool:
        """Check if URL is from a supported video platform

        Args:
            url: URL to check

        Returns:
            True if URL is from supported platform, False otherwise
        """
        self.logger.info(f"Checking if URL is from a supported video platform: {url}")
        platform = self.detect_platform(url)
        is_supported = platform is not None

        if is_supported:
            self.logger.info(f"✅ URL validation result: SUPPORTED - URL is from {platform} platform: {url}")
        else:
            self.logger.info(f"❌ URL validation result: NOT SUPPORTED - URL does not match any known platform patterns: {url}")

        return is_supported

    def check_for_video(self, url: str) -> bool:
        """Check if a URL contains a video

        Args:
            url: URL to check

        Returns:
            True if URL contains a video, False otherwise
        """
        self.logger.info(f"Checking if URL contains a video: {url}")
        platform = self.detect_platform(url)

        if not platform:
            self.logger.info(f"❌ URL is not from a supported platform: {url}")
            return False

        # For platforms that always contain videos
        if platform in ['youtube', 'tiktok', 'facebook', 'twitter', '9gag', 'vk']:
            self.logger.info(f"✅ Platform {platform} typically contains videos: {url}")
            return True

        # For Threads, we need to check if it contains a video
        if platform == 'threads':
            driver = None
            try:
                # Setup Chrome options
                chrome_options = Options()
                chrome_options.add_argument("--headless")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")

                # Mobile emulation for Threads
                mobile_emulation = {
                    "deviceMetrics": {"width": 375, "height": 812, "pixelRatio": 3.0},
                    "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
                }
                chrome_options.add_experimental_option("mobileEmulation", mobile_emulation)

                # Initialize the Chrome driver
                driver_path = ChromeDriverManager().install()
                if os.path.basename(driver_path) != "chromedriver":
                    driver_dir = os.path.dirname(driver_path)
                    driver_path = os.path.join(driver_dir, "chromedriver")

                service = Service(driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)

                # Set page load timeout
                driver.set_page_load_timeout(30)

                # Navigate to the URL
                driver.get(url)

                # Handle the "continue in browser" popup
                try:
                    wait = WebDriverWait(driver, 15)
                    selectors = [
                        "//button[text()='Continue in browser']",
                        "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue in browser')]",
                        "//button[contains(text(), 'Continue')]",
                        "//*[contains(text(), 'Continue in browser')]",
                        "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                        "//a[.//text()[contains(., 'Continue')]] | //button[.//text()[contains(., 'Continue')]]",
                        "//*[contains(@class, 'continue') or contains(@id, 'continue') or contains(@class, 'browser') or contains(@id, 'browser')]"
                    ]

                    continue_button = None
                    for selector in selectors:
                        try:
                            continue_button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                            break
                        except (TimeoutException, NoSuchElementException):
                            continue

                    if continue_button:
                        driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                        time.sleep(1)

                        try:
                            continue_button.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", continue_button)

                        time.sleep(5)
                except Exception:
                    self.logger.warning("Could not handle 'continue in browser' popup")

                # Wait for the page to load
                time.sleep(5)

                # Check for video elements
                video_selectors = [
                    "//video",
                    "//div[contains(@class, 'video')]",
                    "//div[contains(@class, 'player')]",
                    "//div[contains(@class, 'media') and contains(@class, 'video')]",
                    "//div[contains(@aria-label, 'video')]",
                    "//div[contains(@role, 'button') and contains(@aria-label, 'Play')]"
                ]

                has_video = False
                for selector in video_selectors:
                    try:
                        elements = driver.find_elements(By.XPATH, selector)
                        if elements:
                            has_video = True
                            self.logger.info(f"✅ Found video element with selector: {selector}")
                            break
                    except Exception:
                        continue

                if has_video:
                    self.logger.info(f"✅ URL contains a video: {url}")
                else:
                    self.logger.info(f"❌ URL does not contain a video: {url}")

                return has_video

            except Exception as e:
                self.logger.error(f"Error checking if URL contains a video: {str(e)}")
                # If we can't check, assume it doesn't contain a video
                return False
            finally:
                if driver:
                    driver.quit()

        # For Instagram, assume it contains a video (we'll try to download it anyway)
        if platform == 'instagram':
            self.logger.info(f"✅ Assuming Instagram URL contains a video: {url}")
            return True

        # Default to True for other platforms
        self.logger.info(f"✅ Assuming URL contains a video for platform {platform}: {url}")
        return True

    def get_ydl_opts(self, platform: str) -> Dict[str, Any]:
        """Get yt-dlp options for specific platform

        Args:
            platform: Platform name

        Returns:
            Dictionary of yt-dlp options
        """
        self.logger.debug(f"Getting yt-dlp options for platform: {platform}")

        base_opts = {
            'outtmpl': os.path.join(self.download_dir, '%(title)s.%(ext)s'),
            'format': 'best[filesize<50M]/best',  # Telegram file size limit
            'noplaylist': True,
            'extractaudio': False,
            'audioformat': 'mp3',
            'embed_subs': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'no_warnings': True,
            'quiet': False,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'player_skip': ['webpage', 'configs'],
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }
        self.logger.debug(f"Base yt-dlp options configured with output template: {base_opts['outtmpl']}")

        # Platform-specific options
        platform_opts = {
            'youtube': {
                'format': 'best[height<=720][filesize<50M]/best[filesize<50M]/best',
            },
            'instagram': {
                'format': 'best[filesize<50M]/best',
            },
            'tiktok': {
                'format': 'best[filesize<50M]/best',
                # Reset http_headers so yt-dlp's native JS challenge solver uses its own UA
                'http_headers': {},
            },
            'facebook': {
                'format': 'best[filesize<50M]/best',
            },
            'twitter': {
                'format': 'best[filesize<50M]/best',
            },
            '9gag': {
                'format': 'best[ext=mp4][filesize<50M]/best[filesize<50M]/best',
            },
            'vk': {
                'format': 'best[height<=720][filesize<50M]/best[filesize<50M]/best',
            }
        }

        if platform in platform_opts:
            self.logger.debug(f"Applying platform-specific options for {platform}: {platform_opts[platform]}")
            opts = platform_opts[platform]
            # Merge http_headers instead of overwriting
            if 'http_headers' in opts:
                base_opts['http_headers'].update(opts.pop('http_headers'))
            # Merge extractor_args instead of overwriting
            if 'extractor_args' in opts:
                base_opts.setdefault('extractor_args', {}).update(opts.pop('extractor_args'))
            base_opts.update(opts)
        else:
            self.logger.debug(f"No platform-specific options found for {platform}, using base options")

        self.logger.debug(f"Final yt-dlp options for {platform}: format={base_opts['format']}")
        return base_opts

    def progress_hook(self, d):
        """Progress hook for yt-dlp to log download progress

        Args:
            d: Progress information dictionary from yt-dlp
        """
        if d['status'] == 'downloading':
            try:
                percent = d.get('_percent_str', 'N/A')
                speed = d.get('_speed_str', 'N/A')
                eta = d.get('_eta_str', 'N/A')
                filename = d.get('filename', 'unknown')

                self.logger.info(f"Download progress: {percent} at {speed}, ETA: {eta} for {filename}")
            except Exception as e:
                self.logger.error(f"Error in progress hook: {str(e)}")
        elif d['status'] == 'finished':
            self.logger.info(f"Download finished: {d.get('filename', 'unknown')}")
        elif d['status'] == 'error':
            self.logger.error(f"Download error: {d.get('error', 'unknown error')}")

    def _select_download_info(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize extractor results to a concrete downloadable entry."""
        download_info = dict(info)
        if info.get('_type') == 'playlist':
            entries = [entry for entry in (info.get('entries') or []) if isinstance(entry, dict)]
            if not entries:
                raise Exception('No downloadable entries found in playlist result')
            self.logger.info(
                f"Extractor returned playlist result with {len(entries)} entries; downloading first entry"
            )
            download_info = dict(entries[0])
        return download_info

    def _estimate_filesize(self, platform: str, download_info: Dict[str, Any]) -> int:
        """Estimate download size, with Twitter-specific corrections for playlist/HLS metadata."""
        filesize = download_info.get('filesize') or download_info.get('filesize_approx') or 0
        if filesize is None:
            filesize = 0

        if platform != 'twitter':
            return int(filesize)

        candidate_sizes = []
        requested_formats_raw = download_info.get('requested_formats')
        requested_formats = [fmt for fmt in requested_formats_raw if isinstance(fmt, dict)] if isinstance(requested_formats_raw, list) else []
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
                    f"Using corrected Twitter size estimate: {better_estimate / (1024*1024):.1f}MB "
                    f"instead of {filesize / (1024*1024):.1f}MB"
                )
                filesize = better_estimate

        if filesize > 100 * 1024 * 1024 and duration:
            estimated_bitrate = 2 * 1024 * 1024  # 2 Mbps in bits per second
            bitrate_estimate = duration * estimated_bitrate / 8
            if bitrate_estimate < filesize:
                self.logger.info(
                    f"Calculated fallback Twitter size estimate from duration: {bitrate_estimate / (1024*1024):.1f}MB"
                )
                filesize = bitrate_estimate

        return int(filesize)

    def _optimize_twitter_format(self, download_info: Dict[str, Any], size_limit_bytes: int) -> Dict[str, Any]:
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
            f"Switching Twitter format from {current_format_id or 'unknown'} "
            f"to {best_candidate.get('format_id')} to stay within size limit "
            f"({best_size / (1024*1024):.1f}MB)"
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

    def download_video(self, url: str, ignore_size_limit: bool = False) -> Optional[Dict[str, Any]]:
        """Download video from URL or take screenshot of Threads post

        Args:
            url: URL to process (video URL or Threads post URL)
            ignore_size_limit: If True, download even when the pre-download estimate exceeds 50MB

        Returns:
            Dictionary with download info if successful, None otherwise
            For videos: Contains 'filepath', 'title', 'platform', 'duration', 'filesize'
            For screenshots: Contains 'filepath', 'title', 'platform', 'filesize', 'is_screenshot'
        """
        self.logger.info(f"🔍 Processing download request for URL: {url}")

        # Check if URL is supported by any platform
        platform = self.detect_platform(url)
        if not platform:
            self.logger.error(f"❌ URL validation failed: Unsupported URL format: {url}")
            self.logger.info(f"No matching pattern found for URL: {url}")
            return None

        self.logger.info(f"✅ URL validation passed: URL is from {platform} platform")
        self.logger.info(f"Preparing to download video from {platform}: {url}")
        ydl_opts = self.get_ydl_opts(platform)

        # Add progress hook for real-time logging
        ydl_opts['progress_hooks'] = [self.progress_hook]

        try:
            self.logger.info(f"Extracting video information from {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info first
                info = ydl.extract_info(url, download=False)

                download_info = self._select_download_info(info)
                if platform == 'twitter':
                    download_info = self._optimize_twitter_format(download_info, 50 * 1024 * 1024)

                # Log video information
                title = download_info.get('title', info.get('title', 'Unknown'))
                duration = download_info.get('duration', info.get('duration', 'Unknown'))
                self.logger.info(f"Video info - Title: {title}, Duration: {duration}s")

                # Check file size before downloading
                filesize = self._estimate_filesize(platform, download_info)
                if filesize <= 0:
                    self.logger.warning("File size information not available")

                self.logger.info(f"Estimated file size: {filesize / (1024*1024):.1f}MB")

                if filesize > 50 * 1024 * 1024 and not ignore_size_limit:  # 50MB limit
                    self.logger.error(f"File too large: {filesize / (1024*1024):.1f}MB (limit: 50MB)")
                    return {
                        'error': 'file_too_large',
                        'message': f'Video file is too large to download (limit: 50MB)',
                        'filesize': filesize,
                        'title': title,
                        'platform': platform,
                        'duration': duration
                    }
                elif filesize > 50 * 1024 * 1024 and ignore_size_limit:
                    self.logger.warning(
                        f"Ignoring pre-download size limit for {platform}: {filesize / (1024*1024):.1f}MB"
                    )

                # Download the video by reusing already-fetched info to avoid
                # re-requesting the webpage (which causes 403 on TikTok's JS challenge).
                self.logger.info(f"Starting download of {title} from {platform}")
                ydl.process_info(download_info)

                # Get the downloaded file path
                filename = download_info.get('_filename') or ydl.prepare_filename(download_info)
                self.logger.info(f"Expected filename: {filename}")

                # Handle cases where the actual filename might be different
                if not os.path.exists(filename):
                    self.logger.info(f"File not found at expected path, searching for alternatives")
                    # Try to find the downloaded file
                    base_name = os.path.splitext(filename)[0]

                    # First check for playlist files (with #1, #2, etc. in the filename)
                    playlist_found = False
                    for i in range(1, 10):  # Check for up to 9 playlist items
                        for ext in ['.mp4', '.webm', '.mkv', '.avi', '.mov']:
                            test_path = f"{base_name} #{i}{ext}"
                            if os.path.exists(test_path):
                                filename = test_path
                                self.logger.info(f"Found playlist file at path: {filename}")
                                playlist_found = True
                                break
                        if playlist_found:
                            break

                    # If no playlist files found, try different extensions
                    if not playlist_found:
                        for ext in ['.mp4', '.webm', '.mkv', '.avi', '.mov']:
                            test_path = base_name + ext
                            if os.path.exists(test_path):
                                filename = test_path
                                self.logger.info(f"Found file at alternative path: {filename}")
                                break

                if not os.path.exists(filename):
                    self.logger.error(f"Downloaded file not found: {filename}")
                    return None

                actual_filesize = os.path.getsize(filename)
                self.logger.info(f"Download complete - File: {filename}, Size: {actual_filesize / (1024*1024):.1f}MB")

                # Log successful download with detailed information
                self.logger.info(f"✅ Download successful for URL: {url}")
                self.logger.info(f"Video details - Title: {title}, Platform: {platform}, Duration: {duration}s, Size: {actual_filesize / (1024*1024):.1f}MB")

                # Try to determine video dimensions to preserve aspect ratio when sending via Telegram
                width = download_info.get('width') or None
                height = download_info.get('height') or None
                # Fallback: inspect requested_formats or formats for dimensions
                try:
                    if (width is None or height is None) and isinstance(download_info.get('requested_formats'), list):
                        for f in download_info['requested_formats']:
                            if not width and f.get('width'):
                                width = f.get('width')
                            if not height and f.get('height'):
                                height = f.get('height')
                    if (width is None or height is None) and isinstance(download_info.get('formats'), list):
                        # Prefer the selected format_id
                        selected_id = download_info.get('format_id')
                        for f in download_info['formats']:
                            if selected_id and f.get('format_id') == selected_id:
                                width = width or f.get('width')
                                height = height or f.get('height')
                                break
                        # If still missing, take the best available non-audio format with dims
                        if width is None or height is None:
                            for f in reversed(download_info['formats']):
                                if f.get('vcodec', 'none') != 'none' and f.get('width') and f.get('height'):
                                    width = width or f.get('width')
                                    height = height or f.get('height')
                                    break
                except Exception as _:
                    pass

                return {
                    'filepath': filename,
                    'title': title,
                    'platform': platform,
                    'duration': duration,
                    'filesize': actual_filesize,
                    'url': url,
                    'width': width,
                    'height': height
                }

        except Exception as e:
            error_message = str(e).strip()
            if not error_message:
                error_message = f"{type(e).__name__}: {repr(e)}"
            self.logger.error(f"Error downloading {url}: {error_message}")

            # Log detailed error information
            self.logger.info(f"❌ Download failed for URL: {url}")
            self.logger.info(f"Platform detected: {platform}")
            self.logger.info(f"Error details: {error_message}")

            # Check if this is a TikTok photo URL (not a video)
            if platform == 'tiktok' and '/photo/' in error_message:
                self.logger.warning(f"Detected TikTok photo URL (not a video): {url}")
                self.logger.info(f"Special case: URL is a TikTok photo, not a video")
                return {
                    'error': 'tiktok_photo',
                    'message': 'This appears to be a TikTok photo, not a video.'
                }

            # Check if this is a content only available for registered users
            if "This content is only available for registered users" in error_message:
                self.logger.warning(f"Detected content only available for registered users: {url}")
                self.logger.info(f"Special case: Content requires authentication")
                return {
                    'error': 'registered_users_only',
                    'message': 'This video is only available for registered users who follow this account.'
                }

            # Check if this is a Threads post (which might not be a video)
            if platform == 'threads':
                self.logger.warning(f"Video download failed for Threads URL: {url}")
                self.logger.info(f"Attempting to take a screenshot instead")

                # Try to take a screenshot of the Threads post
                screenshot_result = self.take_screenshot(url)
                if screenshot_result:
                    self.logger.info(f"Successfully took screenshot of Threads post: {url}")
                    return screenshot_result
                else:
                    self.logger.error(f"Failed to take screenshot of Threads post: {url}")
                    return {
                        'error': 'threads_screenshot_failed',
                        'message': 'Failed to take screenshot of Threads post.'
                    }

            # 9GAG fallback: try extracting a direct MP4 URL from the page and download it.
            if platform == '9gag':
                self.logger.info("Attempting 9GAG direct MP4 fallback")
                try:
                    mp4_url = self._extract_9gag_mp4_url(url)
                    if mp4_url:
                        return self._download_direct_media(mp4_url, source_url=url, platform='9gag')
                    return {
                        'error': 'no_video_found',
                        'message': 'No downloadable video was found on this 9GAG post.'
                    }
                except Exception as fallback_error:
                    self.logger.error(f"9GAG fallback failed for {url}: {str(fallback_error)}")
                    return None

            return None

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
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://9gag.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9'
        }
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
                self.logger.warning(f"Could not fetch 9GAG page HTML from {page_url}: {str(exc)}")

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

    def _download_direct_media(self, media_url: str, source_url: str, platform: str) -> Optional[Dict[str, Any]]:
        """Download a direct media URL and return a result object compatible with bot sending logic."""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://9gag.com/',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9'
        }

        parsed = urlparse(media_url)
        basename = os.path.basename(parsed.path) or f"{platform}_video.mp4"
        if not basename.lower().endswith('.mp4'):
            basename = f"{basename}.mp4"

        timestamp = int(time.time())
        filename = os.path.join(self.download_dir, f"{timestamp}_{basename}")

        response = requests.get(media_url, headers=headers, timeout=30, stream=True)
        response.raise_for_status()

        content_length = int(response.headers.get('Content-Length', 0) or 0)
        if content_length > 50 * 1024 * 1024:
            return {
                'error': 'file_too_large',
                'message': 'Video file is too large to download (limit: 50MB)',
                'filesize': content_length,
                'title': basename,
                'platform': platform,
                'duration': None
            }

        written = 0
        with open(filename, 'wb') as output_file:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                output_file.write(chunk)
                written += len(chunk)
                if written > 50 * 1024 * 1024:
                    output_file.close()
                    self.cleanup_file(filename)
                    return {
                        'error': 'file_too_large',
                        'message': 'Video file is too large to download (limit: 50MB)',
                        'filesize': written,
                        'title': basename,
                        'platform': platform,
                        'duration': None
                    }

        # 9GAG direct files can use codec/container combinations that fail on iPhone.
        # Normalize to a more broadly compatible MP4 when tools are available.
        filename = self._ensure_ios_compatible_video(filename, platform)
        written = os.path.getsize(filename)

        if written > 50 * 1024 * 1024:
            self.cleanup_file(filename)
            return {
                'error': 'file_too_large',
                'message': 'Video file is too large to download (limit: 50MB)',
                'filesize': written,
                'title': basename,
                'platform': platform,
                'duration': None
            }

        width, height = self._probe_video_dimensions(filename)
        return {
            'filepath': filename,
            'title': os.path.splitext(basename)[0],
            'platform': platform,
            'duration': None,
            'filesize': written,
            'url': source_url,
            'width': width,
            'height': height
        }

    def _probe_video_dimensions(self, filepath: str):
        """Return (width, height) of the video file using ffprobe, or (None, None) on failure."""
        ffprobe_path = shutil.which('ffprobe')
        if not ffprobe_path or not os.path.exists(filepath):
            return None, None
        try:
            cmd = [
                ffprobe_path,
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'default=noprint_wrappers=1:nokey=0',
                filepath,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            width = height = None
            for line in result.stdout.splitlines():
                if line.startswith('width='):
                    width = int(line.split('=', 1)[1].strip())
                elif line.startswith('height='):
                    height = int(line.split('=', 1)[1].strip())
            return width, height
        except Exception:
            return None, None

    def _ensure_ios_compatible_video(self, filepath: str, platform: str) -> str:
        """Repackage/transcode fallback videos to improve iPhone playback compatibility."""
        if platform != '9gag' or not os.path.exists(filepath):
            return filepath

        ffmpeg_path = shutil.which('ffmpeg')
        ffprobe_path = shutil.which('ffprobe')
        if not ffmpeg_path or not ffprobe_path:
            self.logger.warning("ffmpeg/ffprobe not found, skipping iOS compatibility normalization")
            return filepath

        video_codec = None
        audio_codec = None
        pix_fmt = None
        try:
            probe_cmd = [
                ffprobe_path,
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_name,pix_fmt',
                '-of', 'default=noprint_wrappers=1:nokey=0',
                filepath,
            ]
            video_probe = subprocess.run(probe_cmd, capture_output=True, text=True, check=False)
            for line in video_probe.stdout.splitlines():
                if line.startswith('codec_name='):
                    video_codec = line.split('=', 1)[1].strip().lower()
                elif line.startswith('pix_fmt='):
                    pix_fmt = line.split('=', 1)[1].strip().lower()

            audio_cmd = [
                ffprobe_path,
                '-v', 'error',
                '-select_streams', 'a:0',
                '-show_entries', 'stream=codec_name',
                '-of', 'default=noprint_wrappers=1:nokey=0',
                filepath,
            ]
            audio_probe = subprocess.run(audio_cmd, capture_output=True, text=True, check=False)
            for line in audio_probe.stdout.splitlines():
                if line.startswith('codec_name='):
                    audio_codec = line.split('=', 1)[1].strip().lower()
                    break
        except Exception as probe_error:
            self.logger.warning(f"ffprobe failed, skipping compatibility check: {str(probe_error)}")
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
                ffmpeg_path,
                '-y',
                '-i', filepath,
                '-c:v', 'libx264',
                '-profile:v', 'main',
                '-level', '4.0',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-ac', '2',
                '-ar', '44100',
                output_path,
            ]
            self.logger.info("Transcoding 9GAG video for iPhone compatibility")
        else:
            cmd = [
                ffmpeg_path,
                '-y',
                '-i', filepath,
                '-c', 'copy',
                '-movflags', '+faststart',
                output_path,
            ]
            self.logger.info("Remuxing 9GAG video with faststart for better compatibility")

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0 or not os.path.exists(output_path):
                self.logger.warning("Compatibility normalization failed, using original fallback file")
                return filepath
            self.cleanup_file(filepath)
            return output_path
        except Exception as ffmpeg_error:
            self.logger.warning(f"Compatibility normalization error: {str(ffmpeg_error)}")
            return filepath

    def take_screenshot(self, url: str) -> Optional[Dict[str, Any]]:
        """Take a screenshot of a web page

        Args:
            url: URL to take screenshot of

        Returns:
            Dictionary with screenshot info if successful, None otherwise
            Contains: 'filepath', 'title', 'platform', 'filesize'
        """
        self.logger.info(f"Taking screenshot of URL: {url}")

        # Detect platform
        platform = self.detect_platform(url)

        # Setup Chrome options
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        # Use mobile emulation for Threads posts
        if platform == 'threads':
            self.logger.info("Using mobile emulation for Threads post")
            # Mobile emulation settings
            mobile_emulation = {
                "deviceMetrics": {"width": 375, "height": 812, "pixelRatio": 3.0},
                "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
            }
            chrome_options.add_experimental_option("mobileEmulation", mobile_emulation)
        else:
            # Default desktop window size for other platforms
            chrome_options.add_argument("--window-size=1280,1024")

        # Generate a unique filename for the screenshot
        timestamp = int(time.time())
        filename = os.path.join(self.download_dir, f"screenshot_{timestamp}.png")

        driver = None
        try:
            # Initialize the Chrome driver
            self.logger.info("Initializing Chrome driver")
            driver_path = ChromeDriverManager().install()
            # Ensure we're using the actual chromedriver executable, not a text file
            if os.path.basename(driver_path) != "chromedriver":
                driver_dir = os.path.dirname(driver_path)
                driver_path = os.path.join(driver_dir, "chromedriver")
                self.logger.info(f"Corrected ChromeDriver path to: {driver_path}")

            # Set executable permissions on the ChromeDriver
            try:
                import stat
                self.logger.info(f"Setting executable permissions on ChromeDriver: {driver_path}")
                os.chmod(driver_path, os.stat(driver_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                self.logger.info("Permissions set successfully")
            except Exception as e:
                self.logger.error(f"Error setting permissions on ChromeDriver: {str(e)}")

            service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=chrome_options)

            # Set page load timeout
            driver.set_page_load_timeout(30)

            # Navigate to the URL
            self.logger.info(f"Navigating to URL: {url}")
            driver.get(url)

            # Handle the "continue in browser" popup for Threads
            if platform == 'threads':
                try:
                    self.logger.info("Checking for 'continue in browser' popup")
                    # Wait for the popup to appear (up to 15 seconds)
                    wait = WebDriverWait(driver, 15)

                    # Try multiple selectors to find the "continue in browser" button
                    selectors = [
                        # Button with exact text
                        "//button[text()='Continue in browser']",
                        # Button with case-insensitive text
                        "//button[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue in browser')]",
                        # Button with partial text
                        "//button[contains(text(), 'Continue')]",
                        # Any element with continue text (might be a div or span)
                        "//*[contains(text(), 'Continue in browser')]",
                        # Any element with continue text (case insensitive)
                        "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                        # Any clickable element (a, button) with continue text in any child
                        "//a[.//text()[contains(., 'Continue')]] | //button[.//text()[contains(., 'Continue')]]",
                        # Last resort - any element with class or id containing 'continue' or 'browser'
                        "//*[contains(@class, 'continue') or contains(@id, 'continue') or contains(@class, 'browser') or contains(@id, 'browser')]"
                    ]

                    # Try each selector until one works
                    continue_button = None
                    for selector in selectors:
                        try:
                            self.logger.info(f"Trying selector: {selector}")
                            continue_button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                            self.logger.info(f"Found button with selector: {selector}")
                            break
                        except (TimeoutException, NoSuchElementException):
                            self.logger.info(f"Selector failed: {selector}")
                            continue

                    if continue_button:
                        self.logger.info("Found 'continue in browser' button, clicking it")
                        # Scroll to the button to ensure it's visible
                        driver.execute_script("arguments[0].scrollIntoView(true);", continue_button)
                        time.sleep(1)  # Small pause after scrolling

                        # Try JavaScript click if regular click fails
                        try:
                            continue_button.click()
                        except Exception as e:
                            self.logger.warning(f"Regular click failed: {str(e)}, trying JavaScript click")
                            driver.execute_script("arguments[0].click();", continue_button)

                        self.logger.info("Clicked 'continue in browser' button")
                        # Wait for the page to load after clicking the button
                        time.sleep(5)  # Increased wait time to ensure page loads

                        # Handle the second popup (Thread app vs Safari)
                        try:
                            self.logger.info("Checking for second popup (Thread app vs Safari)")
                            # Wait for the second popup to appear (up to 10 seconds)
                            wait = WebDriverWait(driver, 10)

                            # Try multiple selectors to find the "Continue" button in Safari option
                            safari_continue_selectors = [
                                # Button with exact text in Safari section
                                "//button[text()='Continue']",
                                # Any button containing "Continue" text
                                "//button[contains(text(), 'Continue')]",
                                # Any element with "Continue" text
                                "//*[contains(text(), 'Continue')]",
                                # Case-insensitive match for "continue"
                                "//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                                # Elements near "Safari" text
                                "//div[contains(text(), 'Safari')]/following::button",
                                "//div[contains(text(), 'Safari')]/..//button",
                                # Last resort - any button at the bottom of the page
                                "//div[contains(@style, 'bottom') or contains(@class, 'bottom')]//button[last()]"
                            ]

                            # Try each selector until one works
                            safari_continue_button = None
                            for selector in safari_continue_selectors:
                                try:
                                    self.logger.info(f"Trying selector for Safari continue: {selector}")
                                    safari_continue_button = wait.until(EC.element_to_be_clickable((By.XPATH, selector)))
                                    self.logger.info(f"Found Safari continue button with selector: {selector}")
                                    break
                                except (TimeoutException, NoSuchElementException):
                                    self.logger.info(f"Safari continue selector failed: {selector}")
                                    continue

                            if safari_continue_button:
                                self.logger.info("Found Safari 'Continue' button, clicking it")
                                # Scroll to the button to ensure it's visible
                                driver.execute_script("arguments[0].scrollIntoView(true);", safari_continue_button)
                                time.sleep(1)  # Small pause after scrolling

                                # Try JavaScript click if regular click fails
                                try:
                                    safari_continue_button.click()
                                except Exception as e:
                                    self.logger.warning(f"Regular click failed on Safari continue: {str(e)}, trying JavaScript click")
                                    driver.execute_script("arguments[0].click();", safari_continue_button)

                                self.logger.info("Clicked Safari 'Continue' button")
                                # Wait for the page to load after clicking the button
                                time.sleep(5)  # Wait time to ensure page loads
                            else:
                                self.logger.warning("Could not find Safari 'Continue' button with any selector")

                        except (TimeoutException, NoSuchElementException) as e:
                            self.logger.warning(f"Could not find Safari 'Continue' button: {str(e)}")
                            self.logger.info("Continuing without clicking the Safari button")
                        except Exception as e:
                            self.logger.warning(f"Error handling Safari 'Continue' popup: {str(e)}")
                            self.logger.info("Continuing without clicking the Safari button")
                    else:
                        self.logger.warning("Could not find 'continue in browser' button with any selector")

                except (TimeoutException, NoSuchElementException) as e:
                    self.logger.warning(f"Could not find 'continue in browser' button: {str(e)}")
                    self.logger.info("Continuing without clicking the button")
                except Exception as e:
                    self.logger.warning(f"Error handling 'continue in browser' popup: {str(e)}")
                    self.logger.info("Continuing without clicking the button")

            # Wait for the page to load
            time.sleep(5)

            # Take screenshot
            self.logger.info(f"Taking screenshot and saving to: {filename}")
            driver.save_screenshot(filename)

            # Get page title
            title = driver.title

            # Get platform from URL
            platform = self.detect_platform(url)

            # Get file size
            filesize = os.path.getsize(filename)

            self.logger.info(f"Screenshot taken successfully: {filename}")
            self.logger.info(f"Title: {title}, Platform: {platform}, Size: {filesize / (1024*1024):.1f}MB")

            return {
                'filepath': filename,
                'title': title,
                'platform': platform,
                'filesize': filesize,
                'url': url,
                'is_screenshot': True
            }

        except TimeoutException:
            self.logger.error(f"Timeout while loading URL: {url}")
            return None
        except WebDriverException as e:
            self.logger.error(f"WebDriver error while taking screenshot of {url}: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"Error taking screenshot of {url}: {str(e)}")
            return None
        finally:
            if driver:
                driver.quit()
                self.logger.info("Chrome driver closed")

    def cleanup_file(self, filepath: str) -> bool:
        """Remove downloaded file

        Args:
            filepath: Path to file to remove

        Returns:
            True if file was removed successfully, False otherwise
        """
        self.logger.debug(f"Attempting to clean up file: {filepath}")
        try:
            if os.path.exists(filepath):
                self.logger.debug(f"File exists, removing: {filepath}")
                os.remove(filepath)
                self.logger.info(f"Successfully removed file: {filepath}")
                return True
            else:
                self.logger.warning(f"File not found for cleanup: {filepath}")
        except Exception as e:
            self.logger.error(f"Error removing file {filepath}: {str(e)}")
        return False


# Test function
def test_downloader():
    """Test the video downloader with sample URLs"""
    downloader = VideoDownloader()

    test_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # Rick Roll
        "https://youtu.be/dQw4w9WgXcQ",  # Short YouTube URL
        "https://www.instagram.com/p/example/",  # Instagram (example)
        "https://www.instagram.com/reels/DJJ4ngeoYbx/",  # Instagram reels (from issue)
        "https://www.tiktok.com/@user/video/123456789",  # TikTok (example)
        "https://vt.tiktok.com/ZSkUakqex/",  # TikTok vt format (from issue)
        "https://www.facebook.com/watch/?v=123456789",  # Facebook (example)
        "https://twitter.com/username/status/1234567890",  # Twitter (example)
        "https://x.com/username/status/1234567890",  # X.com (example)
        "https://t.co/abcdef123",  # Twitter shortened URL (example)
        "https://www.9gag.com/gag/avyKyAd",  # 9GAG (example)
        "https://example.com/not-a-video"  # Not a video URL
    ]

    for url in test_urls:
        platform = downloader.detect_platform(url)
        is_video = downloader.is_video_url(url)
        print(f"URL: {url}")
        print(f"Platform: {platform}")
        print(f"Is video URL: {is_video}")
        print("-" * 50)


if __name__ == "__main__":
    test_downloader()
