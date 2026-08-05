"""
Live smoke tests that hit the real internet.

They are skipped unless you opt in:

    RUN_NETWORK_TESTS=1 pytest test_simple.py -m network

Use them to sanity check that yt-dlp still works against the real platforms
after a dependency bump; everything else in the suite runs offline.
"""

import os

import pytest

from video_downloader import VideoDownloader

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.getenv('RUN_NETWORK_TESTS') != '1',
        reason="set RUN_NETWORK_TESTS=1 to run tests that hit the network",
    ),
]

# "Me at the zoo" - the oldest video on YouTube, 19 seconds, always available.
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_real_youtube_download(download_dir):
    downloader = VideoDownloader(download_dir=download_dir)
    try:
        result = downloader.download_video(YOUTUBE_URL)

        assert result is not None, "download returned nothing"
        assert 'error' not in result, result.get('message')
        assert os.path.exists(result['filepath'])
        assert result['filesize'] > 0
        assert result['duration'] and result['duration'] > 0
        assert result['platform'] == 'youtube'
    finally:
        if 'result' in dir() and isinstance(result, dict):
            downloader.cleanup_file(result.get('filepath'))
        downloader.close()


def test_real_youtube_progress_callback(download_dir):
    downloader = VideoDownloader(download_dir=download_dir)
    updates = []
    result = None
    try:
        result = downloader.download_video(YOUTUBE_URL, progress_callback=updates.append)
        assert result and 'error' not in result
        assert updates, "no progress updates were emitted"
        assert all('percent' in update for update in updates)
    finally:
        if isinstance(result, dict):
            downloader.cleanup_file(result.get('filepath'))
        downloader.close()


@pytest.mark.browser
def test_real_threads_screenshot(download_dir):
    downloader = VideoDownloader(download_dir=download_dir)
    result = None
    try:
        result = downloader.take_screenshot("https://www.threads.net/@meta")
        if result is None:
            pytest.skip("Chrome/chromedriver unavailable or Threads blocked the request")
        assert os.path.exists(result['filepath'])
        assert result['is_screenshot'] is True
    finally:
        if isinstance(result, dict):
            downloader.cleanup_file(result.get('filepath'))
        downloader.close()
