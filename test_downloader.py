"""
Unit tests for VideoDownloader.

These never touch the network: yt-dlp and requests are stubbed. The live
download smoke tests live in test_simple.py behind RUN_NETWORK_TESTS=1.
"""

import os
import time

import pytest
from unittest.mock import Mock, patch

from video_downloader import ERROR_MESSAGES, MB, VideoDownloader


class FakeYDL:
    """Stand-in for yt_dlp.YoutubeDL that returns a canned info dict."""

    def __init__(self, opts):
        self.opts = opts
        self.processed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    info = {}
    written = b'fake-video-bytes'

    def extract_info(self, url, download=False, **kwargs):
        del url, download
        return dict(self.info)

    def process_ie_result(self, info, download=False, extra_info=None):
        """Real yt-dlp selects formats here; the fake has nothing to select."""
        return info

    def prepare_filename(self, info):
        return os.path.join(
            self.opts['paths']['home'],
            f"{info.get('title', 'video')}.{info.get('ext', 'mp4')}"
        )

    def process_info(self, info):
        self.processed.append(info)
        path = self.prepare_filename(info)
        info['_filename'] = path
        with open(path, 'wb') as handle:
            handle.write(self.written)


def ydl_factory(info, written=b'fake-video-bytes'):
    """Build a FakeYDL class bound to a specific info dict."""
    return type('BoundFakeYDL', (FakeYDL,), {'info': info, 'written': written})


# ----------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------

def test_output_template_includes_the_video_id(downloader):
    """Two different videos with the same title must not clobber each other."""
    opts = downloader.get_ydl_opts('youtube')
    assert '%(id)s' in opts['outtmpl']
    assert opts['paths']['home'] == downloader.download_dir


def test_long_titles_keep_the_folder_and_the_id(download_dir):
    """Regression: trim_file_name used to cut the absolute path, folder and id included."""
    import yt_dlp

    deep = os.path.join(download_dir, 'a-rather-long-folder-name-' * 4)
    os.makedirs(deep)
    downloader = VideoDownloader(download_dir=deep)
    try:
        ydl = yt_dlp.YoutubeDL(downloader.get_ydl_opts('youtube'))
        path = ydl.prepare_filename({'id': 'abcdefghijk', 'title': 'T' * 200, 'ext': 'mp4'})
    finally:
        downloader.close()

    assert os.path.dirname(path) == deep
    assert '[abcdefghijk]' in os.path.basename(path)


def test_format_selector_respects_the_size_limit(download_dir):
    small = VideoDownloader(download_dir=download_dir, max_filesize_mb=25)
    assert 'filesize<25M' in small.get_ydl_opts('youtube')['format']
    small.close()


def test_format_selector_caps_height_per_platform(downloader):
    assert 'height<=?720' in downloader.get_ydl_opts('youtube')['format']
    assert f'height<=?{downloader.max_video_height}' in downloader.get_ydl_opts('facebook')['format']


def test_tiktok_drops_the_shared_user_agent(downloader):
    """TikTok's JS challenge only passes with yt-dlp's own user agent."""
    assert 'User-Agent' not in downloader.get_ydl_opts('tiktok')['http_headers']
    assert 'User-Agent' in downloader.get_ydl_opts('youtube')['http_headers']


def test_platform_options_do_not_leak_between_calls(downloader):
    downloader.get_ydl_opts('tiktok')
    assert 'User-Agent' in downloader.get_ydl_opts('youtube')['http_headers']


def test_yt_dlp_is_quiet_and_uses_our_logger(downloader):
    opts = downloader.get_ydl_opts('youtube')
    assert opts['quiet'] is True
    assert opts['logger'] is not None
    assert opts['socket_timeout'] == downloader.socket_timeout


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, None), ('Unknown', None), (0, None), (-5, None),
    (12, 12.0), (12.5, 12.5), ('30', 30.0),
])
def test_normalize_duration(value, expected):
    assert VideoDownloader._normalize_duration(value) == expected


@pytest.mark.parametrize("message,code", [
    ("ERROR: Video unavailable", 'unavailable'),
    ("This content is only available for registered users", 'registered_users_only'),
    ("Sign in to confirm you're not a bot", 'login_required'),
    ("HTTP Error 429: Too Many Requests", 'rate_limited'),
    ("The uploader has not made this video available in your country", 'geo_restricted'),
    ("Requested format is not available", 'download_failed'),
    ("Unsupported URL: https://example.com", 'unsupported_by_extractor'),
])
def test_classify_error(message, code):
    assert VideoDownloader.classify_error(message) == code
    assert code in ERROR_MESSAGES


def test_select_download_info_picks_the_first_playlist_entry(downloader):
    info = {'_type': 'playlist', 'title': 'thread',
            'entries': [{'title': 'first'}, {'title': 'second'}]}
    assert downloader._select_download_info(info)['title'] == 'first'


def test_select_download_info_rejects_empty_playlists(downloader):
    with pytest.raises(ValueError):
        downloader._select_download_info({'_type': 'playlist', 'entries': []})


def test_extract_dimensions_falls_back_to_formats(downloader):
    info = {
        'format_id': 'hd',
        'formats': [
            {'format_id': 'sd', 'width': 640, 'height': 360, 'vcodec': 'h264'},
            {'format_id': 'hd', 'width': 1920, 'height': 1080, 'vcodec': 'h264'},
        ],
    }
    assert downloader._extract_dimensions(info) == (1920, 1080)


def test_normalize_progress():
    normalized = VideoDownloader.normalize_progress({
        'status': 'downloading', 'downloaded_bytes': 50, 'total_bytes': 200,
        'speed': 1024, 'eta': 3,
    })
    assert normalized['percent'] == 25.0
    assert normalized['total_bytes'] == 200
    assert normalized['speed'] == 1024


def test_normalize_progress_without_a_total():
    normalized = VideoDownloader.normalize_progress(
        {'status': 'downloading', 'downloaded_bytes': 50}
    )
    assert normalized['percent'] is None
    assert normalized['total_bytes'] is None


# ----------------------------------------------------------------------------
# download_video
# ----------------------------------------------------------------------------

def test_download_video_rejects_unsupported_urls(downloader):
    assert downloader.download_video('https://example.com/whatever') is None


def test_download_video_returns_a_normalized_result(downloader):
    info = {'title': 'A clip', 'ext': 'mp4', 'duration': 42, 'width': 720, 'height': 1280}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video('https://youtu.be/dQw4w9WgXcQ')

    assert result['platform'] == 'youtube'
    assert result['title'] == 'A clip'
    assert result['duration'] == 42.0
    assert (result['width'], result['height']) == (720, 1280)
    assert result['filesize'] == len(FakeYDL.written)
    assert os.path.exists(result['filepath'])


def test_download_video_never_returns_a_string_duration(downloader):
    """Regression: 'Unknown' used to crash the caption builder with ValueError."""
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory({'title': 'no duration'})):
        result = downloader.download_video('https://youtu.be/dQw4w9WgXcQ')
    assert result['duration'] is None


def test_download_video_rejects_a_large_estimate(downloader):
    info = {'title': 'huge', 'filesize': 200 * MB}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video('https://youtu.be/dQw4w9WgXcQ')

    assert result['error'] == 'file_too_large'
    assert result['filesize'] == 200 * MB


def test_ignore_size_limit_downloads_anyway(downloader):
    info = {'title': 'huge', 'filesize': 200 * MB}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video(
            'https://youtu.be/dQw4w9WgXcQ', ignore_size_limit=True
        )

    assert 'error' not in result
    assert os.path.exists(result['filepath'])


def test_oversized_file_is_caught_after_download(download_dir):
    """Regression: an unknown pre-download estimate let huge files slip through."""
    tiny = VideoDownloader(download_dir=download_dir, max_filesize_mb=1)
    info = {'title': 'no estimate'}
    payload = b'x' * (2 * MB)

    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info, payload)):
            result = tiny.download_video('https://youtu.be/dQw4w9WgXcQ')

        assert result['error'] == 'file_too_large'
        assert os.listdir(download_dir) == []
    finally:
        tiny.close()


def test_live_streams_are_rejected(downloader):
    info = {'title': 'stream', 'is_live': True}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video('https://youtu.be/dQw4w9WgXcQ')
    assert result['error'] == 'live_stream'


def test_extractor_failures_are_classified(downloader):
    class FailingYDL(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            raise Exception("ERROR: Video unavailable")

    with patch('video_downloader.yt_dlp.YoutubeDL', FailingYDL):
        result = downloader.download_video('https://youtu.be/dQw4w9WgXcQ')

    assert result['error'] == 'unavailable'
    assert result['message'] == ERROR_MESSAGES['unavailable']


def test_progress_callback_receives_normalized_updates(downloader):
    seen = []

    class ProgressYDL(FakeYDL):
        info = {'title': 'progress', 'ext': 'mp4'}

        def process_info(self, info):
            for hook in self.opts['progress_hooks']:
                hook({'status': 'downloading', 'downloaded_bytes': 5,
                      'total_bytes': 10, 'speed': 100, 'eta': 1})
            super().process_info(info)

    with patch('video_downloader.yt_dlp.YoutubeDL', ProgressYDL):
        downloader.download_video('https://youtu.be/dQw4w9WgXcQ', progress_callback=seen.append)

    assert seen and seen[0]['percent'] == 50.0


def test_progress_callback_errors_do_not_break_the_download(downloader):
    def exploding(_progress):
        raise RuntimeError("UI is on fire")

    class ProgressYDL(FakeYDL):
        info = {'title': 'progress', 'ext': 'mp4'}

        def process_info(self, info):
            for hook in self.opts['progress_hooks']:
                hook({'status': 'downloading', 'downloaded_bytes': 5, 'total_bytes': 10})
            super().process_info(info)

    with patch('video_downloader.yt_dlp.YoutubeDL', ProgressYDL):
        result = downloader.download_video(
            'https://youtu.be/dQw4w9WgXcQ', progress_callback=exploding
        )

    assert os.path.exists(result['filepath'])


def test_threads_failure_falls_back_to_a_screenshot(downloader):
    class FailingYDL(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            raise Exception("No video formats found")

    shot = {'filepath': '/tmp/shot.png', 'title': 'post', 'platform': 'threads',
            'filesize': 10, 'is_screenshot': True}
    downloader.take_screenshot = Mock(return_value=shot)

    with patch('video_downloader.yt_dlp.YoutubeDL', FailingYDL):
        result = downloader.download_video('https://www.threads.com/@u/post/ABC')

    assert result is shot
    downloader.take_screenshot.assert_called_once()


def test_threads_screenshot_failure_is_reported(downloader):
    class FailingYDL(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            raise Exception("No video formats found")

    downloader.take_screenshot = Mock(return_value=None)
    with patch('video_downloader.yt_dlp.YoutubeDL', FailingYDL):
        result = downloader.download_video('https://www.threads.com/@u/post/ABC')

    assert result['error'] == 'threads_screenshot_failed'


# ----------------------------------------------------------------------------
# Housekeeping
# ----------------------------------------------------------------------------

def test_cleanup_file(downloader, download_dir):
    path = os.path.join(download_dir, 'x.mp4')
    with open(path, 'wb') as handle:
        handle.write(b'x')

    assert downloader.cleanup_file(path) is True
    assert downloader.cleanup_file(path) is False
    assert downloader.cleanup_file(None) is False


def test_cleanup_old_files_removes_only_stale_entries(downloader, download_dir):
    stale = os.path.join(download_dir, 'stale.mp4')
    fresh = os.path.join(download_dir, 'fresh.mp4')
    for path in (stale, fresh):
        with open(path, 'wb') as handle:
            handle.write(b'x')

    old = time.time() - 24 * 3600
    os.utime(stale, (old, old))

    assert downloader.cleanup_old_files(max_age_hours=6) == 1
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)


def test_cleanup_old_files_is_disabled_at_zero(downloader, download_dir):
    path = os.path.join(download_dir, 'old.mp4')
    with open(path, 'wb') as handle:
        handle.write(b'x')
    os.utime(path, (0, 0))

    assert downloader.cleanup_old_files(max_age_hours=0) == 0
    assert os.path.exists(path)
