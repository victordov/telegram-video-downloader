"""
Tests for the downloader's newer abilities: more sites, cache keys, albums,
TikTok photo posts, audio/HD modes, shrinking oversized videos and cancelling.

yt-dlp and HTTP are faked; the one exception is the shrink test, which runs the
real ffmpeg on a generated clip (skipped when ffmpeg is not installed).
"""

import json
import os
import shutil
import subprocess
import threading
from unittest.mock import Mock, patch

import pytest

from test_downloader import FakeYDL, ydl_factory
from video_downloader import (
    MB, MODE_AUDIO, MODE_HD, VideoDownloader, _Job, platform_label,
)

FFMPEG = shutil.which('ffmpeg')
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and shutil.which('ffprobe')),
                                  reason="ffmpeg/ffprobe not installed")


class FakeResponse:
    def __init__(self, content=b'', text='', content_type='image/jpeg', status=200):
        self.content = content
        self.text = text
        self.headers = {'Content-Type': content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        pass


@pytest.fixture
def album_downloader(download_dir):
    instance = VideoDownloader(download_dir=download_dir, max_album_items=10)
    yield instance
    instance.close()


# ----------------------------------------------------------------------------
# Sites
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("url,platform", [
    ("https://www.reddit.com/r/videos/comments/abc123/some_title/", 'reddit'),
    ("https://www.reddit.com/r/videos/s/AbCdEf123", 'reddit'),
    ("https://v.redd.it/abc123xyz", 'reddit'),
    ("https://www.pinterest.com/pin/123456789012345678/", 'pinterest'),
    ("https://pin.it/AbCdEf1", 'pinterest'),
    ("https://vimeo.com/76979871", 'vimeo'),
    ("https://player.vimeo.com/video/76979871", 'vimeo'),
    ("https://clips.twitch.tv/FunnySlugName-abc123", 'twitch'),
    ("https://www.twitch.tv/someuser/clip/FunnySlugName-abc123", 'twitch'),
    ("https://bsky.app/profile/user.bsky.social/post/3l4abcdxyz2", 'bluesky'),
    ("https://www.dailymotion.com/video/x8abcd1", 'dailymotion'),
    ("https://dai.ly/x8abcd1", 'dailymotion'),
    ("https://www.tiktok.com/@user/photo/7400000000000000000", 'tiktok'),
])
def test_new_platforms_are_detected(downloader, url, platform):
    assert downloader.detect_platform(url) == platform


def test_platform_labels():
    assert platform_label('youtube') == 'YouTube'
    assert platform_label('9gag') == '9GAG'
    assert platform_label('bbc') == 'Bbc'
    assert platform_label(None) == 'Unknown'


def test_any_site_falls_back_to_yt_dlps_extractors(downloader):
    url = "https://www.bbc.co.uk/news/videos/c123abc456"
    assert downloader.resolve_platform(url) is None
    assert downloader.resolve_platform(url, any_site=True) == 'bbc'


def test_plain_web_pages_are_not_attempted(downloader):
    """The catch-all generic extractor would fetch any page; it is excluded."""
    assert downloader.generic_site("https://example.com/some/article") is None
    assert downloader.generic_site("not a url") is None


def test_direct_media_links_are_accepted(downloader):
    assert downloader.generic_site("https://cdn.example.com/clips/video.mp4?sig=1") == 'direct'


def test_unsupported_url_without_any_site_returns_none(downloader):
    assert downloader.download_video("https://www.bbc.co.uk/news/videos/c123") is None


# ----------------------------------------------------------------------------
# Cache keys
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://x.com/user/status/123?s=20", "https://twitter.com/other/status/123"),
    ("https://example.org/v/1?utm_source=x&id=5", "https://www.example.org/v/1/?id=5"),
])
def test_equivalent_links_share_a_cache_key(downloader, a, b):
    assert downloader.canonical_id(a) == downloader.canonical_id(b)


def test_different_videos_get_different_keys(downloader):
    assert (downloader.canonical_id("https://youtu.be/aaaaaaaaaaa")
            != downloader.canonical_id("https://youtu.be/bbbbbbbbbbb"))


# ----------------------------------------------------------------------------
# Multi-item posts
# ----------------------------------------------------------------------------

def carousel_info():
    return {
        '_type': 'playlist', 'id': 'POST1', 'title': 'Post by someone',
        'channel': 'someone', 'description': 'Look at these',
        'entries': [
            {'id': 'v1', 'title': 'first', 'ext': 'mp4', 'duration': 5,
             'formats': [{'format_id': 'f', 'url': 'https://cdn/v1.mp4'}]},
            {'id': 'p2', 'title': 'second', 'formats': [],
             'thumbnails': [{'url': 'https://cdn/small.jpg', 'width': 150, 'height': 150},
                            {'url': 'https://cdn/big.jpg', 'width': 1080, 'height': 1080}]},
            {'id': 'v3', 'title': 'third', 'ext': 'mp4', 'duration': 7,
             'formats': [{'format_id': 'f', 'url': 'https://cdn/v3.mp4'}]},
        ],
    }


def test_carousel_becomes_an_album_of_videos_and_photos(album_downloader):
    album_downloader._session.get = Mock(return_value=FakeResponse(b'\xff\xd8jpeg-bytes'))

    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(carousel_info())):
        result = album_downloader.download_video("https://www.instagram.com/p/POST1/")

    assert result['is_album'] is True
    assert [item['kind'] for item in result['items']] == ['video', 'photo', 'video']
    # The largest thumbnail is the photo itself.
    assert album_downloader._session.get.call_args.args[0] == 'https://cdn/big.jpg'
    assert all(os.path.exists(item['filepath']) for item in result['items'])
    assert result['filesize'] == sum(item['filesize'] for item in result['items'])
    # Post-level metadata reaches the album and its items.
    assert result['uploader'] == 'someone'
    assert result['uploader_url'] == 'https://www.instagram.com/someone/'
    assert result['description'] == 'Look at these'


def test_one_broken_item_does_not_sink_the_post(album_downloader):
    info = carousel_info()

    class FlakyYDL(ydl_factory(info)):
        def process_ie_result(self, entry, download=False, extra_info=None):
            if entry.get('id') == 'v3':
                raise Exception("ERROR: Video unavailable")
            return entry

    album_downloader._session.get = Mock(return_value=FakeResponse(b'\xff\xd8jpeg'))
    with patch('video_downloader.yt_dlp.YoutubeDL', FlakyYDL):
        result = album_downloader.download_video("https://www.instagram.com/p/POST1/")

    assert result['is_album'] and len(result['items']) == 2
    assert result['skipped'] == 1


def test_album_size_is_capped_by_max_album_items(download_dir):
    small = VideoDownloader(download_dir=download_dir, max_album_items=1)
    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(carousel_info())):
            result = small.download_video("https://www.instagram.com/p/POST1/")
    finally:
        small.close()
    assert not result.get('is_album')
    assert result['title'] == 'first'


def test_a_playlist_of_links_is_not_treated_as_an_album(album_downloader):
    """A YouTube playlist is many separate videos, not one post: only the first."""
    info = {'_type': 'playlist', 'id': 'PL', 'title': 'playlist',
            'entries': [{'_type': 'url', 'url': 'https://youtu.be/aaaaaaaaaaa'},
                        {'_type': 'url', 'url': 'https://youtu.be/bbbbbbbbbbb'}]}
    assert album_downloader._post_entries(info) is None


def test_lazy_entries_are_not_consumed(album_downloader):
    def generator():
        raise AssertionError("must not be iterated")
        yield  # pragma: no cover

    assert album_downloader._post_entries({'_type': 'playlist', 'entries': generator()}) is None


# ----------------------------------------------------------------------------
# TikTok photo posts
# ----------------------------------------------------------------------------

def tiktok_page(image_urls):
    data = {'__DEFAULT_SCOPE__': {'webapp.video-detail': {'itemInfo': {'itemStruct': {
        'id': '7400000000000000000',
        'desc': 'my holiday #travel',
        'author': {'uniqueId': 'traveller', 'nickname': 'The Traveller'},
        'imagePost': {'images': [{'imageURL': {'urlList': [u + '.heic', u + '.jpeg']}}
                                 for u in image_urls]},
    }}}}}
    return ('<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(data) + '</script></html>')


def test_tiktok_photo_post_downloads_every_image(album_downloader):
    urls = ['https://p16.tiktokcdn.com/a', 'https://p16.tiktokcdn.com/b']

    def fake_get(url, **kwargs):
        if 'tiktok.com/@' in url:
            return FakeResponse(text=tiktok_page(urls), content_type='text/html')
        return FakeResponse(b'\xff\xd8image')

    album_downloader._session.get = Mock(side_effect=fake_get)
    with patch('video_downloader.yt_dlp.YoutubeDL', side_effect=AssertionError("no yt-dlp")):
        result = album_downloader.download_video(
            "https://www.tiktok.com/@traveller/photo/7400000000000000000")

    assert result['is_album'] and len(result['items']) == 2
    assert all(item['kind'] == 'photo' for item in result['items'])
    # The JPEG rendition is chosen over HEIC.
    fetched = [call.args[0] for call in album_downloader._session.get.call_args_list]
    assert 'https://p16.tiktokcdn.com/a.jpeg' in fetched
    assert result['uploader_url'] == 'https://www.tiktok.com/@traveller'
    assert result['title'] == 'my holiday #travel'


def test_tiktok_short_link_to_a_photo_post_is_followed(album_downloader):
    target = "https://www.tiktok.com/@traveller/photo/7400000000000000000"

    class RedirectYDL(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            return {'_type': 'url', 'url': target}

    album_downloader._download_tiktok_photos = Mock(return_value={'is_album': True, 'items': []})
    with patch('video_downloader.yt_dlp.YoutubeDL', RedirectYDL):
        album_downloader.download_video("https://vm.tiktok.com/ZMabc123/")

    assert album_downloader._download_tiktok_photos.call_args.args[0] == target


def test_tiktok_photo_page_without_images_reports_a_photo_post(album_downloader):
    album_downloader._session.get = Mock(return_value=FakeResponse(text='<html></html>',
                                                                   content_type='text/html'))
    result = album_downloader.download_video(
        "https://www.tiktok.com/@u/photo/7400000000000000000")
    assert result['error'] == 'tiktok_photo'


# ----------------------------------------------------------------------------
# Audio and HD modes
# ----------------------------------------------------------------------------

def test_audio_mode_asks_for_aac_and_extracts_it(downloader):
    opts = downloader.get_ydl_opts('youtube', MODE_AUDIO)
    assert opts['format'].startswith('ba[acodec^=mp4a]')
    assert 'merge_output_format' not in opts
    if downloader.has_ffmpeg:
        assert opts['postprocessors'][0]['key'] == 'FFmpegExtractAudio'
        assert opts['postprocessors'][0]['preferredcodec'] == 'm4a'


def test_audio_mode_skips_platform_format_overrides(downloader):
    assert downloader.get_ydl_opts('9gag', MODE_AUDIO)['format'].startswith('ba')


def test_audio_result_is_marked_as_audio(downloader):
    info = {'title': 'song', 'ext': 'm4a', 'duration': 30, 'uploader': 'Band'}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video("https://youtu.be/dQw4w9WgXcQ", mode=MODE_AUDIO)
    assert result['kind'] == 'audio'
    assert result['uploader'] == 'Band'
    assert result['width'] is None


def test_hd_mode_lifts_the_platform_cap_and_size_limit(download_dir):
    hd = VideoDownloader(download_dir=download_dir, max_filesize_mb=10, upload_limit_mb=2000,
                         hd_max_height=1080)
    try:
        assert 'height<=?720' in hd.get_ydl_opts('youtube')['format']
        hd_format = hd.get_ydl_opts('youtube', MODE_HD)['format']
        assert 'height<=?1080' in hd_format and 'filesize<2000M' in hd_format

        info = {'title': 'big', 'ext': 'mp4', 'filesize': 500 * MB}
        with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
            result = hd.download_video("https://youtu.be/dQw4w9WgXcQ", mode=MODE_HD)
        assert 'error' not in result
    finally:
        hd.close()


def test_hd_available_when_the_site_has_taller_formats(downloader):
    info = {'formats': [{'height': 720, 'vcodec': 'avc1'}, {'height': 1080, 'vcodec': 'avc1'},
                        {'height': 2160, 'vcodec': 'vp9'}, {'height': 1440, 'vcodec': 'none'}]}
    assert downloader._hd_available(info, 720) is True
    assert downloader._hd_available(info, 1080) is False
    assert downloader._hd_available(info, None) is False


def test_results_carry_caption_metadata(downloader):
    info = {'title': 'clip', 'ext': 'mp4', 'uploader': 'Someone',
            'uploader_url': 'https://www.youtube.com/@someone',
            'description': 'about the clip', 'age_limit': 18,
            'height': 720, 'formats': [{'height': 1080, 'vcodec': 'avc1'}]}
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        result = downloader.download_video("https://youtu.be/dQw4w9WgXcQ")
    assert result['uploader'] == 'Someone'
    assert result['uploader_url'] == 'https://www.youtube.com/@someone'
    assert result['description'] == 'about the clip'
    assert result['age_limit'] == 18
    assert result['hd_available'] is True


# ----------------------------------------------------------------------------
# Shrinking oversized videos
# ----------------------------------------------------------------------------

def test_shrink_plan_picks_a_resolution_for_the_bitrate(downloader):
    video_kbps, audio_kbps, side = downloader._shrink_plan(60, 50 * MB)
    assert video_kbps > 5000 and side == 1080

    video_kbps, audio_kbps, side = downloader._shrink_plan(600, 50 * MB)
    assert 500 <= video_kbps < 900 and side == 480


def test_videos_too_long_to_shrink_have_no_plan(downloader):
    assert downloader._shrink_plan(3 * 3600, 50 * MB) is None
    assert downloader._shrink_plan(None, 50 * MB) is None


def test_compress_max_source_caps_what_is_downloaded_for_shrinking(download_dir):
    capped = VideoDownloader(download_dir=download_dir, compress_oversized=True,
                             compress_max_source_mb=100)
    try:
        if not capped.has_ffmpeg:
            pytest.skip("needs ffmpeg")
        assert capped._can_shrink(90 * MB, 120, 50 * MB)
        assert not capped._can_shrink(150 * MB, 120, 50 * MB)
    finally:
        capped.close()


def test_oversized_estimate_is_downloaded_and_shrunk(download_dir):
    shrinker = VideoDownloader(download_dir=download_dir, max_filesize_mb=1,
                               compress_oversized=True)
    if not shrinker.has_ffmpeg:
        pytest.skip("needs ffmpeg")

    def fake_shrink(path, target, duration, job=None):
        small = path + '.small.mp4'
        with open(small, 'wb') as handle:
            handle.write(b'x' * 100)
        os.remove(path)
        return small

    shrinker.shrink_to_fit = Mock(side_effect=fake_shrink)
    shrinker._probe_video_dimensions = Mock(return_value=(426, 240))
    # 20 seconds in 1MB is ~340kbps of video: small, but watchable.
    info = {'title': 'long', 'ext': 'mp4', 'duration': 20, 'filesize': 5 * MB}
    try:
        with patch('video_downloader.yt_dlp.YoutubeDL',
                   ydl_factory(info, written=b'v' * (2 * MB))):
            result = shrinker.download_video("https://youtu.be/dQw4w9WgXcQ")
    finally:
        shrinker.close()

    assert 'error' not in result, result
    assert result['compressed'] is True
    assert result['filesize'] == 100
    assert (result['width'], result['height']) == (426, 240)


def test_too_long_to_shrink_is_refused_before_downloading(download_dir):
    shrinker = VideoDownloader(download_dir=download_dir, max_filesize_mb=1,
                               compress_oversized=True)
    if not shrinker.has_ffmpeg:
        pytest.skip("needs ffmpeg")
    info = {'title': 'movie', 'ext': 'mp4', 'duration': 7200, 'filesize': 900 * MB}
    try:
        with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
            result = shrinker.download_video("https://youtu.be/dQw4w9WgXcQ")
    finally:
        shrinker.close()
    assert result['error'] == 'file_too_large'
    assert 'too long to fit' in result['message']


@needs_ffmpeg
def test_real_ffmpeg_shrinks_a_clip_under_the_target(download_dir):
    source = os.path.join(download_dir, 'noisy.mp4')
    # Random noise barely compresses, so this clip is several MB at 720p.
    subprocess.run([
        FFMPEG, '-y', '-loglevel', 'error', '-f', 'lavfi',
        '-i', 'nullsrc=s=1280x720:d=8,geq=random(1)*255:128:128',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '10', '-c:a', 'aac',
        '-shortest', source,
    ], check=True)
    assert os.path.getsize(source) > 2 * MB

    downloader = VideoDownloader(download_dir=download_dir, compress_oversized=True)
    events = []
    job = _Job(url='x', platform='youtube', mode='video', ignore_size_limit=False,
               cancel_event=None, progress_callback=events.append)
    try:
        output = downloader.shrink_to_fit(source, 1 * MB, 8.0, job)
        assert output and os.path.getsize(output) <= 1 * MB
        assert not os.path.exists(source)
        codecs = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name',
             '-of', 'csv=p=0', output], capture_output=True, text=True,
        ).stdout.split()
        assert codecs[:2] == ['h264', 'aac']
        assert any(event['status'] == 'compressing' for event in events)
    finally:
        downloader.close()


# ----------------------------------------------------------------------------
# Cancelling
# ----------------------------------------------------------------------------

def test_cancel_event_stops_a_running_download(downloader):
    cancel = threading.Event()

    class SlowYDL(FakeYDL):
        info = {'title': 'slow', 'ext': 'mp4'}

        def process_info(self, info):
            cancel.set()  # the user presses Cancel mid-transfer
            for hook in self.opts['progress_hooks']:
                hook({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 10})
            raise AssertionError("the hook should have aborted the download")

    with patch('video_downloader.yt_dlp.YoutubeDL', SlowYDL):
        result = downloader.download_video("https://youtu.be/dQw4w9WgXcQ", cancel_event=cancel)

    assert result['error'] == 'cancelled'


def test_cancel_before_the_download_starts(downloader):
    cancel = threading.Event()
    cancel.set()
    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory({'title': 'x', 'ext': 'mp4'})):
        result = downloader.download_video("https://youtu.be/dQw4w9WgXcQ", cancel_event=cancel)
    assert result['error'] == 'cancelled'
    assert os.listdir(downloader.download_dir) == []


def test_album_items_report_their_position(album_downloader):
    seen = []

    class ProgressYDL(ydl_factory(carousel_info())):
        def process_info(self, info):
            for hook in self.opts['progress_hooks']:
                hook({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 2})
            super().process_info(info)

    album_downloader._session.get = Mock(return_value=FakeResponse(b'\xff\xd8jpeg'))
    with patch('video_downloader.yt_dlp.YoutubeDL', ProgressYDL):
        album_downloader.download_video("https://www.instagram.com/p/POST1/",
                                        progress_callback=seen.append)

    assert {(p.get('item_index'), p.get('item_count')) for p in seen} >= {(1, 3), (3, 3)}


# ----------------------------------------------------------------------------
# Cookies only for sites that need a login
# ----------------------------------------------------------------------------

REDDIT = "https://www.reddit.com/r/videos/comments/abc123/some_title/"
NETSCAPE = "# Netscape HTTP Cookie File\n.reddit.com\tTRUE\t/\tTRUE\t2000000000\ttoken_v2\tabc\n"


def cookie_downloader(download_dir, **kwargs):
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome',
                               cookies_only_when_needed=True, **kwargs)
    instance._browser_cookies, instance._browser_cookies_at = NETSCAPE, 10**12
    return instance


def test_cookies_are_held_back_until_a_site_asks(download_dir):
    instance = cookie_downloader(download_dir)
    assert 'cookiefile' not in instance.get_ydl_opts('youtube')
    assert 'cookiesfrombrowser' not in instance.get_ydl_opts('youtube')
    instance.close()


def test_without_the_option_cookies_go_everywhere(download_dir):
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome')
    instance._browser_cookies, instance._browser_cookies_at = NETSCAPE, 10**12
    assert instance.get_ydl_opts('youtube')['cookiefile'].read() == NETSCAPE
    instance.close()


def test_a_login_wall_is_retried_with_cookies_and_remembered(download_dir):
    instance = cookie_downloader(download_dir)
    seen = []

    class LoginWalled(FakeYDL):
        info = {'title': 'clip', 'ext': 'mp4', 'duration': 5}

        def extract_info(self, url, download=False, **kwargs):
            seen.append('cookiefile' in self.opts)
            if 'cookiefile' not in self.opts:
                raise Exception("ERROR: [Reddit] abc123: Account authentication is required. "
                                "Use --cookies-from-browser or --cookies for the authentication.")
            return dict(self.info)

    with patch('video_downloader.yt_dlp.YoutubeDL', LoginWalled):
        result = instance.download_video(REDDIT)
        assert 'error' not in result and os.path.exists(result['filepath'])
        assert seen == [False, True]

        instance.download_video(REDDIT)  # the next Reddit link skips the anonymous try
        assert seen == [False, True, True]
    instance.close()


def test_without_cookies_a_login_wall_says_how_to_fix_it(downloader):
    class LoginWalled(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            raise Exception("ERROR: [vimeo] 1: Account authentication is required. Use --cookies")

    with patch('video_downloader.yt_dlp.YoutubeDL', LoginWalled):
        result = downloader.download_video("https://vimeo.com/76979871")
    assert result['error'] == 'login_required'
    assert 'COOKIES_FROM_BROWSER' in result['message']


def test_browser_cookies_are_read_once_and_kept_in_memory(download_dir, monkeypatch):
    import http.cookiejar
    from yt_dlp.cookies import YoutubeDLCookieJar

    calls = []

    def fake_load_cookies(cookie_file, browser, ydl):
        calls.append(browser)
        jar = YoutubeDLCookieJar()
        jar.set_cookie(http.cookiejar.Cookie(
            0, 'token_v2', 'abc', None, False, '.reddit.com', True, True, '/', True, True,
            2000000000, False, None, None, {}))
        return jar

    monkeypatch.setattr('yt_dlp.cookies.load_cookies', fake_load_cookies)
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome:Profile 1')
    first = instance.get_ydl_opts('reddit')['cookiefile'].read()
    second = instance.get_ydl_opts('reddit')['cookiefile'].read()

    assert calls == [('chrome', 'Profile 1')]
    assert first == second and 'token_v2\tabc' in first
    instance.close()


def test_an_unreadable_browser_is_not_retried_on_every_download(download_dir, monkeypatch):
    calls = []

    def broken(*args):
        calls.append(args)
        raise RuntimeError("keychain locked")

    monkeypatch.setattr('yt_dlp.cookies.load_cookies', broken)
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome')
    assert 'cookiefile' not in instance.get_ydl_opts('reddit')
    assert 'cookiefile' not in instance.get_ydl_opts('reddit')
    assert len(calls) == 1
    instance.close()


def test_an_unanswered_keychain_prompt_does_not_hang_downloads(download_dir, monkeypatch):
    release = threading.Event()

    def stuck(*args):
        release.wait(5)
        raise RuntimeError("prompt dismissed")

    monkeypatch.setattr('yt_dlp.cookies.load_cookies', stuck)
    monkeypatch.setattr(VideoDownloader, 'BROWSER_COOKIE_WAIT', 0.05)
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome')
    assert instance.browser_cookies() is None          # gave up waiting
    assert instance.browser_cookies() is None          # does not start a second read
    assert instance._cookie_loader.is_alive()
    release.set()
    instance._cookie_loader.join(2)
    instance.close()


def test_unreadable_cookies_are_not_retried_and_the_user_is_told(download_dir, monkeypatch):
    def broken(*args):
        raise RuntimeError("no cookies database")

    monkeypatch.setattr('yt_dlp.cookies.load_cookies', broken)
    instance = VideoDownloader(download_dir=download_dir, cookies_from_browser='chrome',
                               cookies_only_when_needed=True)
    attempts = []

    class LoginWalled(FakeYDL):
        def extract_info(self, url, download=False, **kwargs):
            attempts.append('cookiefile' in self.opts)
            raise Exception("ERROR: [Reddit] abc123: Account authentication is required.")

    with patch('video_downloader.yt_dlp.YoutubeDL', LoginWalled):
        result = instance.download_video(REDDIT)
    assert attempts == [False]
    assert 'could not read' in result['message']
    instance.close()
