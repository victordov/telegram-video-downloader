"""
Tests for video detection (check_for_video) and the screenshot entry point.

The Threads probe used to spin up a headless Chrome session on every single
link, before the download even started. It is now a plain HTTP fetch, which is
what these tests exercise.
"""

from unittest.mock import Mock, patch

import pytest

from video_downloader import VideoDownloader


class FakeResponse:
    def __init__(self, text='', ok=True):
        self.text = text
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise Exception("HTTP error")


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.tiktok.com/@user/video/123456789",
    "https://x.com/user/status/123",
    "https://9gag.com/gag/avyKyAd",
    "https://vk.com/video-1_2",
    "https://www.instagram.com/reel/ABC123/",
])
def test_always_video_platforms_skip_the_probe(downloader, url):
    with patch.object(downloader, '_threads_has_video') as probe:
        assert downloader.check_for_video(url) is True
    probe.assert_not_called()


def test_unsupported_url_has_no_video(downloader):
    assert downloader.check_for_video("https://example.com/page") is False


@pytest.mark.parametrize("html", [
    '<meta property="og:video" content="https://cdn/x.mp4">',
    '{"video_versions":[{"url":"https://cdn/x.mp4"}]}',
    '<video src="blob:..."></video>',
    '<meta name="twitter:player:stream" content="https://cdn/x.mp4">',
])
def test_threads_video_markers_are_detected(downloader, html):
    downloader._session.get = Mock(return_value=FakeResponse(html))
    assert downloader.check_for_video("https://www.threads.com/@u/post/ABC") is True


def test_threads_text_post_has_no_video(downloader):
    downloader._session.get = Mock(
        return_value=FakeResponse('<html><body><p>just some words</p></body></html>')
    )
    assert downloader.check_for_video("https://www.threads.com/@u/post/ABC") is False


def test_threads_probe_failure_assumes_video(downloader):
    """When the probe is inconclusive, let yt-dlp decide - it falls back to a screenshot."""
    downloader._session.get = Mock(side_effect=Exception("connection reset"))
    assert downloader.check_for_video("https://www.threads.com/@u/post/ABC") is True


def test_threads_probe_uses_a_mobile_user_agent(downloader):
    getter = Mock(return_value=FakeResponse('<video>'))
    downloader._session.get = getter
    downloader.check_for_video("https://www.threads.com/@u/post/ABC")

    headers = getter.call_args.kwargs['headers']
    assert 'iPhone' in headers['User-Agent']


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
