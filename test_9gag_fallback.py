"""
Minimal deterministic tests for 9GAG fallback logic.
"""

import os
import tempfile
from unittest.mock import patch

from video_downloader import VideoDownloader


class FakeResponse:
    def __init__(self, text="", headers=None, content_chunks=None, ok=True):
        self.text = text
        self.headers = headers or {}
        self._content_chunks = content_chunks or []
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise Exception("HTTP error")

    def iter_content(self, chunk_size=8192):
        del chunk_size
        for chunk in self._content_chunks:
            yield chunk


class FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        del url, download
        raise Exception("Simulated yt-dlp extractor failure")


def test_9gag_fallback_download():
    test_dir = tempfile.mkdtemp()
    downloader = VideoDownloader(download_dir=test_dir)

    page_html = '<meta property="og:video:url" content="https://img-9gag-fun.9cache.com/photo/avyKyAd_460svav1.mp4">'
    fake_page_response = FakeResponse(text=page_html)
    fake_media_response = FakeResponse(
        headers={"Content-Length": "12"},
        content_chunks=[b"hello", b" world!"]
    )

    with patch("video_downloader.yt_dlp.YoutubeDL", FakeYDL), \
         patch("video_downloader.requests.get", side_effect=[fake_page_response, fake_media_response]):
        result = downloader.download_video("https://9gag.com/gag/avyKyAd")

    assert result is not None
    assert result.get("platform") == "9gag"
    assert os.path.exists(result["filepath"])

    downloader.cleanup_file(result["filepath"])
    os.rmdir(test_dir)


if __name__ == "__main__":
    test_9gag_fallback_download()
    print("9GAG fallback test passed")

