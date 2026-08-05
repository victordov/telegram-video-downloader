"""
Integration tests: message in, video out.

The only thing stubbed is yt-dlp itself and the Telegram transport; the bot,
the config and the downloader all run for real.
"""

import os

import pytest
from unittest.mock import patch

import config
from test_bot import MockChat, MockMessage, MockUpdate
from test_downloader import ydl_factory


async def test_youtube_link_flows_from_message_to_video(bot, download_dir):
    bot.downloader.download_dir = download_dir
    info = {'title': 'Never Gonna Give You Up', 'ext': 'mp4',
            'duration': 213, 'width': 1280, 'height': 720}

    update = MockUpdate(MockMessage("watch this https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory(info)):
        await bot.handle_message(update, None)

    message = update.message
    message.reply_video.assert_awaited_once()

    kwargs = message.reply_video.await_args.kwargs
    assert 'Never Gonna Give You Up' in kwargs['caption']
    assert kwargs['duration'] == 213
    assert (kwargs['width'], kwargs['height']) == (1280, 720)

    # The status message is removed and nothing is left on disk.
    message.status_message.delete.assert_awaited_once()
    assert os.listdir(download_dir) == []
    assert bot.stats.succeeded == 1


async def test_group_message_needs_the_download_tag(bot, download_dir):
    bot.downloader.download_dir = download_dir
    update = MockUpdate(MockMessage(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        chat=MockChat(chat_id=-100, chat_type='supergroup', title='Chat'),
    ))

    with patch('video_downloader.yt_dlp.YoutubeDL', ydl_factory({'title': 'x'})):
        await bot.handle_message(update, None)

    update.message.reply_video.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


async def test_tiktok_downloads_in_groups_without_a_tag(bot, download_dir):
    bot.downloader.download_dir = download_dir
    update = MockUpdate(MockMessage(
        "https://www.tiktok.com/@user/video/123456789",
        chat=MockChat(chat_id=-100, chat_type='supergroup', title='Chat'),
    ))

    with patch('video_downloader.yt_dlp.YoutubeDL',
               ydl_factory({'title': 'a tiktok', 'ext': 'mp4'})):
        await bot.handle_message(update, None)

    update.message.reply_video.assert_awaited_once()


async def test_failed_extraction_leaves_no_files_behind(bot, download_dir):
    bot.downloader.download_dir = download_dir

    class FailingYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            raise Exception("ERROR: Video unavailable")

    update = MockUpdate(MockMessage("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    with patch('video_downloader.yt_dlp.YoutubeDL', FailingYDL):
        await bot.handle_message(update, None)

    update.message.reply_video.assert_not_awaited()
    assert os.listdir(download_dir) == []
    assert bot.stats.failed == 1
    assert any('removed or is unavailable' in (text or '')
               for text in update.message.status_message.texts)


async def test_two_links_in_one_message_produce_two_videos(bot, download_dir):
    bot.downloader.download_dir = download_dir
    update = MockUpdate(MockMessage(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ and "
        "https://www.tiktok.com/@user/video/123456789"
    ))

    counter = {'n': 0}
    base = ydl_factory({'ext': 'mp4'})

    class UniqueYDL(base):
        def extract_info(self, url, download=False):
            counter['n'] += 1
            return {'title': f'clip-{counter["n"]}', 'ext': 'mp4', 'duration': 5}

    with patch('video_downloader.yt_dlp.YoutubeDL', UniqueYDL):
        await bot.handle_message(update, None)

    assert update.message.reply_video.await_count == 2
    assert os.listdir(download_dir) == []


def test_config_defaults_are_sane():
    assert config.MAX_UPLOAD_SIZE_MB <= 2000
    assert config.MAX_CONCURRENT_DOWNLOADS >= 1
    assert config.TELEGRAM_VIDEO_SIZE_LIMIT_MB == 50
    assert os.path.isdir(config.DOWNLOAD_DIR)


@pytest.mark.parametrize("url", [
    "not_a_url", "https://invalid-domain.com/video", "https://www.youtube.com/invalid",
])
def test_unsupported_urls_are_rejected(downloader, url):
    assert downloader.detect_platform(url) is None
    assert downloader.is_video_url(url) is False
