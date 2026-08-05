"""
Regression test for privileged Twitter large-video downloads.
Ensures the bot reuses VideoDownloader's single-entry logic instead of a raw yt-dlp playlist download.
"""

import asyncio
import os
import tempfile
from typing import cast
from unittest.mock import AsyncMock, Mock

import telegram_bot
from telegram import Message
from telegram_bot import TelegramVideoBot
from test_bot import MockChat, MockMessage


async def test_privileged_large_twitter_download_uses_shared_downloader():
    original_private_chat_ids = telegram_bot.PRIVATE_CHAT_IDS[:]
    test_dir = tempfile.mkdtemp()

    try:
        telegram_bot.PRIVATE_CHAT_IDS = ['100000001']

        bot = TelegramVideoBot('dummy_token')
        bot.downloader.download_dir = test_dir

        message = MockMessage(
            'https://x.com/user/status/1234567890123456789',
            message_id=1,
            chat=MockChat(chat_id=100000001, chat_type='private')
        )

        processing_msg = AsyncMock()
        processing_msg.edit_text = AsyncMock()
        processing_msg.delete = AsyncMock()
        message.reply_text = AsyncMock(return_value=processing_msg)

        downloaded_path = os.path.join(test_dir, 'clip-one.mp4')
        with open(downloaded_path, 'wb') as output:
            output.write(b'fake video bytes')

        download_calls = []

        def fake_download(url, ignore_size_limit=False):
            download_calls.append((url, ignore_size_limit))
            if not ignore_size_limit:
                return {
                    'error': 'file_too_large',
                    'message': 'Video file is too large to download (limit: 50MB)',
                    'filesize': 52 * 1024 * 1024,
                    'title': 'clip one',
                    'platform': 'twitter',
                    'duration': 42,
                }
            return {
                'filepath': downloaded_path,
                'title': 'clip one',
                'platform': 'twitter',
                'duration': 42,
                'filesize': os.path.getsize(downloaded_path),
                'width': 720,
                'height': 1280,
            }

        bot.downloader.check_for_video = Mock(return_value=True)
        bot.downloader.download_video = Mock(side_effect=fake_download)
        bot.downloader.cleanup_file = Mock(side_effect=lambda filepath: os.remove(filepath) if os.path.exists(filepath) else None)
        bot._send_video_with_retry = AsyncMock(return_value=True)

        await bot.process_video_url(cast(Message, message), 'https://x.com/user/status/1234567890123456789')

        assert download_calls == [
            ('https://x.com/user/status/1234567890123456789', False),
            ('https://x.com/user/status/1234567890123456789', True),
        ]
        bot._send_video_with_retry.assert_awaited_once()
        assert not os.path.exists(downloaded_path)
    finally:
        telegram_bot.PRIVATE_CHAT_IDS = original_private_chat_ids
        if os.path.exists(test_dir):
            try:
                os.rmdir(test_dir)
            except OSError:
                pass


if __name__ == '__main__':
    asyncio.run(test_privileged_large_twitter_download_uses_shared_downloader())
    print('Privileged Twitter large-video regression test passed')



