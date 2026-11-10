"""
Shared pytest setup.

The environment is pinned *before* ``config`` is imported anywhere so the suite
never picks up the developer's real ``.env`` (``load_dotenv`` does not override
variables that are already set).
"""

import os
import shutil
import tempfile

import pytest

_TEST_DOWNLOAD_DIR = tempfile.mkdtemp(prefix='tvb-tests-')

os.environ.update({
    'TELEGRAM_BOT_TOKEN': '123456:test-token',
    'TELEGRAM_API_BASE_URL': '',
    'PRIVATE_CHAT_IDS': '',
    'DOWNLOAD_DIR': _TEST_DOWNLOAD_DIR,
    'LOG_LEVEL': 'WARNING',
    'GENERATE_THUMBNAILS': 'false',
    'REPLY_ON_UNSUPPORTED_LINKS': 'true',
    'HINT_ON_UNTAGGED_LINKS': 'false',
    'CLEANUP_MAX_AGE_HOURS': '0',
    'PROGRESS_UPDATE_INTERVAL': '2',
    # Each bot instance gets its own throwaway database.
    'DATABASE_PATH': ':memory:',
    'ALLOWED_CHAT_IDS': '',
    'ADMIN_CHAT_IDS': '',
    'RATE_LIMIT_PER_HOUR': '0',
    'CACHE_TTL_DAYS': '30',
    'CACHE_CHAT_ID': '',
    'TRY_ANY_LINK': 'true',
    'COMPRESS_OVERSIZED': 'true',
    'MAX_ALBUM_ITEMS': '10',
    'YTDLP_AUTO_UPDATE': 'false',
    'WEBHOOK_URL': '',
})


@pytest.fixture
def download_dir():
    """A throwaway directory for a single test."""
    path = tempfile.mkdtemp(prefix='tvb-case-')
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def downloader(download_dir):
    from video_downloader import VideoDownloader

    instance = VideoDownloader(download_dir=download_dir)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def bot():
    """A bot instance with a clean statistics/dedup state."""
    from telegram_bot import TelegramVideoBot

    return TelegramVideoBot('123456:test-token')


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_DOWNLOAD_DIR, ignore_errors=True)
