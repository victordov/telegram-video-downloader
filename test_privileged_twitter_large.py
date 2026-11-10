"""
Regression test for privileged large-video downloads.

Two things are pinned down here:

1. The bot goes through ``VideoDownloader.download_video`` rather than driving
   yt-dlp itself (an inline copy used to break on Twitter playlist results).
2. A privileged chat asks for the size limit to be bypassed up front, in a
   single extraction pass, instead of downloading twice.
"""

import os

import pytest
from unittest.mock import AsyncMock, Mock

import config
import telegram_bot
from test_bot import MockChat, MockMessage

# Arbitrary placeholder. Real chat IDs belong in PRIVATE_CHAT_IDS in .env, never
# in the repository - the tests monkeypatch the list, so the value is irrelevant.
PRIVILEGED_CHAT_ID = 100000001
TWEET_URL = 'https://x.com/user/status/1234567890123456789'


@pytest.fixture
def privileged(monkeypatch):
    monkeypatch.setattr(telegram_bot, 'PRIVATE_CHAT_IDS', [str(PRIVILEGED_CHAT_ID)])


async def test_privileged_large_download_uses_the_shared_downloader(bot, privileged,
                                                                    download_dir, monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_SIZE_MB', 2000)

    downloaded_path = os.path.join(download_dir, 'clip-one.mp4')
    with open(downloaded_path, 'wb') as output:
        output.write(b'fake video bytes')

    calls = []

    def fake_download(url, ignore_size_limit=False, progress_callback=None, **kwargs):
        calls.append((url, ignore_size_limit))
        return {
            'filepath': downloaded_path,
            'title': 'clip one',
            'platform': 'twitter',
            'duration': 42,
            'filesize': 120 * 1024 * 1024,
            'width': 720,
            'height': 1280,
        }

    bot.downloader.download_video = Mock(side_effect=fake_download)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage(TWEET_URL, chat=MockChat(chat_id=PRIVILEGED_CHAT_ID,
                                                   chat_type='private'))
    await bot.process_video_url(message, TWEET_URL)

    # One extraction pass, with the limit waived up front.
    assert calls == [(TWEET_URL, True)]
    bot._send_video_with_retry.assert_awaited_once()
    message.status_message.delete.assert_awaited_once()
    assert not os.path.exists(downloaded_path)


async def test_privileged_group_chat_is_not_privileged(bot, privileged, download_dir):
    """The bypass is deliberately limited to private chats."""
    calls = []

    def fake_download(url, ignore_size_limit=False, progress_callback=None, **kwargs):
        calls.append((url, ignore_size_limit))
        return {'error': 'file_too_large', 'message': 'too big', 'filesize': 120 * 1024 * 1024}

    bot.downloader.download_video = Mock(side_effect=fake_download)

    message = MockMessage(TWEET_URL, chat=MockChat(chat_id=PRIVILEGED_CHAT_ID,
                                                   chat_type='supergroup'))
    await bot.process_video_url(message, TWEET_URL)

    assert calls == [(TWEET_URL, False)]
    assert any('too big' in (text or '') for text in message.status_message.texts)


async def test_privileged_download_still_respects_the_upload_ceiling(bot, privileged,
                                                                     download_dir, monkeypatch):
    """Bypassing the download limit does not make a 120MB upload possible on the public API."""
    monkeypatch.setattr(config, 'MAX_UPLOAD_SIZE_MB', 50)
    monkeypatch.setattr(config, 'USE_LOCAL_BOT_API', False)

    downloaded_path = os.path.join(download_dir, 'huge.mp4')
    with open(downloaded_path, 'wb') as output:
        output.write(b'x')

    bot.downloader.download_video = Mock(return_value={
        'filepath': downloaded_path, 'title': 'huge', 'platform': 'twitter',
        'duration': 42, 'filesize': 120 * 1024 * 1024,
    })
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage(TWEET_URL, chat=MockChat(chat_id=PRIVILEGED_CHAT_ID,
                                                   chat_type='private'))
    await bot.process_video_url(message, TWEET_URL)

    bot._send_video_with_retry.assert_not_awaited()
    assert not os.path.exists(downloaded_path)
    texts = " ".join(text or '' for text in message.status_message.texts)
    assert 'upload limit' in texts
    assert 'TELEGRAM_API_BASE_URL' in texts
