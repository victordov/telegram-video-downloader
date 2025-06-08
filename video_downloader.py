"""
Video downloader module using yt-dlp for multiple platforms
Supports YouTube, Instagram, Facebook, and TikTok
"""

import os
import re
import tempfile
import logging
from typing import Optional, Dict, Any
import yt_dlp


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
            r'(?:https?://)?(?:www\.)?instagram\.com/stories/[^/]+/([0-9]+)'
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
        ]
    }

    def __init__(self, download_dir: str = None):
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
        for platform, patterns in self.URL_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url, re.IGNORECASE):
                    return platform
        return None

    def is_video_url(self, url: str) -> bool:
        """Check if URL is from a supported video platform

        Args:
            url: URL to check

        Returns:
            True if URL is from supported platform, False otherwise
        """
        return self.detect_platform(url) is not None

    def get_ydl_opts(self, platform: str) -> Dict[str, Any]:
        """Get yt-dlp options for specific platform

        Args:
            platform: Platform name

        Returns:
            Dictionary of yt-dlp options
        """
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
            },
            'facebook': {
                'format': 'best[filesize<50M]/best',
            }
        }

        if platform in platform_opts:
            base_opts.update(platform_opts[platform])

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

    def download_video(self, url: str) -> Optional[Dict[str, Any]]:
        """Download video from URL

        Args:
            url: Video URL to download

        Returns:
            Dictionary with download info if successful, None otherwise
            Contains: 'filepath', 'title', 'platform', 'duration', 'filesize'
        """
        platform = self.detect_platform(url)
        if not platform:
            self.logger.error(f"Unsupported URL: {url}")
            return None

        self.logger.info(f"Preparing to download video from {platform}: {url}")
        ydl_opts = self.get_ydl_opts(platform)

        # Add progress hook for real-time logging
        ydl_opts['progress_hooks'] = [self.progress_hook]

        try:
            self.logger.info(f"Extracting video information from {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Extract info first
                info = ydl.extract_info(url, download=False)

                # Log video information
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 'Unknown')
                self.logger.info(f"Video info - Title: {title}, Duration: {duration}s")

                # Check file size before downloading
                filesize = info.get('filesize') or info.get('filesize_approx', 0)
                self.logger.info(f"Estimated file size: {filesize / (1024*1024):.1f}MB")

                if filesize > 50 * 1024 * 1024:  # 50MB limit
                    self.logger.error(f"File too large: {filesize / (1024*1024):.1f}MB (limit: 50MB)")
                    return None

                # Download the video
                self.logger.info(f"Starting download of {title} from {platform}")
                ydl.download([url])

                # Get the downloaded file path
                filename = ydl.prepare_filename(info)
                self.logger.info(f"Expected filename: {filename}")

                # Handle cases where the actual filename might be different
                if not os.path.exists(filename):
                    self.logger.info(f"File not found at expected path, searching for alternatives")
                    # Try to find the downloaded file
                    base_name = os.path.splitext(filename)[0]
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

                return {
                    'filepath': filename,
                    'title': title,
                    'platform': platform,
                    'duration': duration,
                    'filesize': actual_filesize,
                    'url': url
                }

        except Exception as e:
            error_message = str(e)
            self.logger.error(f"Error downloading {url}: {error_message}")

            # Check if this is a TikTok photo URL (not a video)
            if platform == 'tiktok' and '/photo/' in error_message:
                self.logger.warning(f"Detected TikTok photo URL (not a video): {url}")
                return {
                    'error': 'tiktok_photo',
                    'message': 'This appears to be a TikTok photo, not a video.'
                }

            return None

    def cleanup_file(self, filepath: str) -> bool:
        """Remove downloaded file

        Args:
            filepath: Path to file to remove

        Returns:
            True if file was removed successfully, False otherwise
        """
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                return True
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
        "https://www.tiktok.com/@user/video/123456789",  # TikTok (example)
        "https://vt.tiktok.com/ZSkUakqex/",  # TikTok vt format (from issue)
        "https://www.facebook.com/watch/?v=123456789",  # Facebook (example)
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
