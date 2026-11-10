"""
Deterministic regression test for Twitter/X playlist-style extractor results.

Twitter tweets with several clips come back as a ``_type: playlist`` info dict.
Handing that straight to ``process_info`` fails, so the downloader normalizes it
to the first entry and picks a variant that fits under the size limit.
"""

import os

from unittest.mock import patch

from video_downloader import MB, VideoDownloader

TWEET_URL = 'https://x.com/user/status/1234567890123456789'


class FakeTwitterYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False, **kwargs):
        del url, download
        return {
            '_type': 'playlist',
            'title': 'tweet with multiple clips',
            'entries': [
                {
                    'id': 'clip_1',
                    'title': 'clip one',
                    'duration': 7,
                    'ext': 'mp4',
                    'format_id': 'http-10368',
                    'filesize_approx': 52 * MB,
                    'formats': [
                        {
                            'format_id': 'http-2176',
                            'width': 720,
                            'height': 1280,
                            'vcodec': 'h264',
                            'acodec': 'aac',
                            'protocol': 'https',
                            'ext': 'mp4',
                            'filesize_approx': 21 * MB,
                            'tbr': 2176,
                        },
                        {
                            'format_id': 'http-10368',
                            'width': 1080,
                            'height': 1920,
                            'vcodec': 'h264',
                            'acodec': 'aac',
                            'protocol': 'https',
                            'ext': 'mp4',
                            'filesize_approx': 52 * MB,
                            'tbr': 10368,
                        }
                    ]
                },
                {
                    'id': 'clip_2',
                    'title': 'clip two',
                    'duration': 5,
                    'ext': 'mp4',
                },
            ],
        }

    def process_ie_result(self, info, download=False, extra_info=None):
        """Real yt-dlp selects formats here; the fake has nothing to select."""
        return info

    def process_info(self, info):
        # Simulate yt-dlp behavior where passing a playlist dict to process_info fails.
        if info.get('_type') == 'playlist':
            raise Exception('')

        filepath = self.prepare_filename(info)
        info['_filename'] = filepath
        with open(filepath, 'wb') as output:
            output.write(b'fake-twitter-video')

    def prepare_filename(self, info):
        directory = self.opts['paths']['home']
        return os.path.join(
            directory,
            f"{info.get('title', 'Unknown')} [{info.get('id', 'x')}].{info.get('ext', 'mp4')}"
        )


def test_playlist_result_downloads_the_first_entry(download_dir):
    downloader = VideoDownloader(download_dir=download_dir)
    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', FakeTwitterYDL):
            result = downloader.download_video(TWEET_URL)

        assert result is not None and 'error' not in result
        assert result['platform'] == 'twitter'
        assert result['title'] == 'clip one'
        assert result['duration'] == 7
        assert result['filesize'] == len(b'fake-twitter-video')
        assert os.path.exists(result['filepath'])
    finally:
        downloader.close()


def test_oversized_variant_is_swapped_for_one_that_fits(download_dir):
    """The 52MB variant is over the limit, so the 21MB one is selected instead."""
    downloader = VideoDownloader(download_dir=download_dir)
    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', FakeTwitterYDL):
            result = downloader.download_video(TWEET_URL)

        assert result is not None and 'error' not in result
        # The optimizer swaps in the 720x1280 variant's metadata.
        assert (result['width'], result['height']) == (720, 1280)
    finally:
        downloader.close()


def test_size_estimate_prefers_the_smallest_credible_candidate(download_dir):
    downloader = VideoDownloader(download_dir=download_dir)
    try:
        entry = {
            'duration': 60,
            'filesize_approx': 500 * MB,
            'format_id': 'http-2176',
            'tbr': 2176,
            'formats': [{'format_id': 'http-2176', 'filesize_approx': 21 * MB, 'tbr': 2176}],
        }
        estimate = downloader._estimate_filesize('twitter', entry)
        # The 500MB "approx" figure is discarded in favour of the smallest
        # credible candidate: duration x bitrate (60s at 2176 kbps).
        assert estimate == int(60 * 2176 * 1000 / 8)
        assert estimate < 21 * MB
    finally:
        downloader.close()
