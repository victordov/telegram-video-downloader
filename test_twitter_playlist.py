"""
Deterministic regression test for Twitter/X playlist-style extractor results.
"""

import os
import tempfile
from unittest.mock import patch

from video_downloader import VideoDownloader


class FakeTwitterYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
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
                    'filesize_approx': 52 * 1024 * 1024,
                    'formats': [
                        {
                            'format_id': 'http-2176',
                            'width': 720,
                            'height': 1280,
                            'vcodec': 'h264',
                            'acodec': 'aac',
                            'protocol': 'https',
                            'ext': 'mp4',
                            'filesize_approx': 21 * 1024 * 1024,
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
                            'filesize_approx': 52 * 1024 * 1024,
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

    def process_info(self, info):
        # Simulate yt-dlp behavior where passing playlist dict to process_info fails.
        if info.get('_type') == 'playlist':
            raise Exception('')

        filepath = self.prepare_filename(info)
        info['_filename'] = filepath
        with open(filepath, 'wb') as output:
            output.write(b'fake-twitter-video')

    def prepare_filename(self, info):
        outtmpl = self.opts['outtmpl']
        filename = outtmpl.replace('%(title)s', info.get('title', 'Unknown'))
        filename = filename.replace('%(ext)s', info.get('ext', 'mp4'))
        return filename


def test_twitter_playlist_download_uses_entry_info():
    test_dir = tempfile.mkdtemp()
    downloader = VideoDownloader(download_dir=test_dir)

    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', FakeTwitterYDL):
            result = downloader.download_video('https://x.com/user/status/1234567890123456789')

        assert result is not None
        assert result.get('platform') == 'twitter'
        assert result.get('title') == 'clip one'
        assert result.get('duration') == 7
        assert result.get('filesize') == len(b'fake-twitter-video')
        assert os.path.exists(result['filepath'])
    finally:
        if 'result' in locals() and result and result.get('filepath'):
            downloader.cleanup_file(result['filepath'])
        if os.path.exists(test_dir):
            os.rmdir(test_dir)


if __name__ == '__main__':
    test_twitter_playlist_download_uses_entry_info()
    print('Twitter playlist regression test passed')

