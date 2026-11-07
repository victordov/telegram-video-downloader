"""
Tests for platform detection and the screenshot entry point.
"""

from unittest.mock import Mock

import pytest

from video_downloader import VideoDownloader


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.tiktok.com/@user/video/123456789",
    "https://x.com/user/status/123",
    "https://9gag.com/gag/avyKyAd",
    "https://vk.com/video-1_2",
    "https://www.instagram.com/reel/ABC123/",
    "https://www.threads.com/@u/post/ABC",
])
def test_supported_platforms_are_detected(downloader, url):
    assert downloader.detect_platform(url) is not None


def test_unsupported_url_is_not_detected(downloader):
    assert downloader.detect_platform("https://example.com/page") is None


def test_chromedriver_path_is_cached(monkeypatch):
    """Resolving the driver hits the network; it must happen at most once."""
    monkeypatch.setattr(VideoDownloader, '_chromedriver_path', None, raising=False)
    installs = []

    class FakeManager:
        def install(self):
            installs.append(1)
            return '/fake/path/chromedriver'

    fake_module = Mock()
    fake_module.ChromeDriverManager = FakeManager
    monkeypatch.setitem(
        __import__('sys').modules, 'webdriver_manager.chrome', fake_module
    )
    monkeypatch.setattr('os.path.exists', lambda path: path == '/fake/path/chromedriver')
    monkeypatch.setattr('os.chmod', lambda *args, **kwargs: None)
    monkeypatch.setattr('os.stat', Mock(return_value=Mock(st_mode=0o644)))

    try:
        assert VideoDownloader._resolve_chromedriver() == '/fake/path/chromedriver'
        assert VideoDownloader._resolve_chromedriver() == '/fake/path/chromedriver'
        assert len(installs) == 1
    finally:
        VideoDownloader._chromedriver_path = None


def test_take_screenshot_without_selenium_returns_none(downloader, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith('selenium'):
            raise ImportError("no selenium here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', blocked)
    assert downloader.take_screenshot("https://www.threads.com/@u/post/ABC") is None
