"""
Handler, formatting and flow tests for the Telegram bot.

Everything here is offline: the downloader is stubbed out so the tests exercise
the bot's own logic (URL extraction, de-duplication, chat rules, captions,
sending and cleanup) without touching the network.
"""

import asyncio
import html
import re
import threading

import httpx
import pytest
from unittest.mock import AsyncMock, Mock

import config
import telegram_bot
from telegram_bot import format_duration, format_size, progress_bar


# ----------------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------------

class MockChat:
    """Minimal chat object used by bot handlers."""

    def __init__(self, chat_id: int = 123456, chat_type: str = "private", title: str = None):
        self.id = chat_id
        self.type = chat_type
        self.title = title


class MockUser:
    """Minimal user object used by bot handlers."""

    def __init__(self, user_id: int = 999999):
        self.id = user_id


class MockStatusMessage:
    """Stands in for the "processing" message the bot edits as it works."""

    def __init__(self):
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()

    @property
    def texts(self):
        return [call.args[0] if call.args else call.kwargs.get('text')
                for call in self.edit_text.await_args_list]


class MockMessage:
    """Mock Telegram message for testing"""

    def __init__(self, text: str, message_id: int = 1, chat: MockChat = None,
                 caption: str = None):
        self.text = text
        self.caption = caption
        self.message_id = message_id
        self.chat = chat or MockChat()
        self.entities = []
        self.caption_entities = []
        self.reply_to_message = None
        self.status_message = MockStatusMessage()
        self.reply_text = AsyncMock(return_value=self.status_message)
        self.reply_video = AsyncMock()
        self.reply_document = AsyncMock()
        self.reply_photo = AsyncMock()
        self.delete = AsyncMock()


class MockUpdate:
    """Mock Telegram update for testing"""

    def __init__(self, message: MockMessage, user: MockUser = None):
        self.update_id = 1
        self.message = message
        self.effective_message = message
        self.effective_chat = message.chat
        self.effective_user = user or MockUser()


def stub_download(bot, result, record=None):
    """Point the bot's downloader at a canned result."""

    def _download(url, ignore_size_limit=False, progress_callback=None):
        if record is not None:
            record.append((url, ignore_size_limit, threading.current_thread().name))
        return dict(result, url=url) if isinstance(result, dict) else result

    bot.downloader.download_video = Mock(side_effect=_download)
    return bot.downloader.download_video


def make_video(tmp_path, size=1024, name='clip.mp4'):
    path = tmp_path / name
    path.write_bytes(b'x' * size)
    return {
        'filepath': str(path),
        'title': 'Clip',
        'platform': 'youtube',
        'duration': 12,
        'filesize': size,
        'width': 720,
        'height': 1280,
    }


# Arbitrary placeholder. Real chat IDs belong in PRIVATE_CHAT_IDS in .env, never
# in the repository - the tests monkeypatch the list, so the value is irrelevant.
PRIVILEGED_CHAT_ID = 100000001


def visible_length(caption: str) -> int:
    """Length Telegram actually counts: entities parsed, tags stripped."""
    return len(html.unescape(re.sub(r'<[^>]+>', '', caption)))


# ----------------------------------------------------------------------------
# URL extraction
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Check out https://youtu.be/dQw4w9WgXcQ", ["https://youtu.be/dQw4w9WgXcQ"]),
    ("No links here", []),
    ("Trailing punctuation https://youtu.be/abc123defgh.", ["https://youtu.be/abc123defgh"]),
    ("In parens (https://youtu.be/abc123defgh)", ["https://youtu.be/abc123defgh"]),
    ("Balanced (https://en.wikipedia.org/wiki/Foo_(bar))",
     ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    ("Comma separated https://a.com/1, https://b.com/2",
     ["https://a.com/1", "https://b.com/2"]),
])
def test_extract_urls(bot, text, expected):
    assert bot.extract_urls(text) == expected


def test_extract_urls_deduplicates(bot):
    text = "https://youtu.be/abc123defgh and again https://youtu.be/abc123defgh"
    assert bot.extract_urls(text) == ["https://youtu.be/abc123defgh"]


def test_urls_from_message_includes_caption_and_text_links(bot):
    from telegram import MessageEntity

    message = MockMessage(None, caption="see the caption https://youtu.be/abc123defgh")
    entity = Mock()
    entity.type = MessageEntity.TEXT_LINK
    entity.url = "https://x.com/user/status/123"
    message.caption_entities = [entity]

    urls = bot.urls_from_message(message)
    assert "https://youtu.be/abc123defgh" in urls
    assert "https://x.com/user/status/123" in urls


@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/shorts/abc123", "youtube"),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "youtube"),
    ("https://www.instagram.com/p/ABC123/", "instagram"),
    ("https://www.instagram.com/reel/XYZ789/", "instagram"),
    ("https://instagram.com/tv/DEF456/", "instagram"),
    ("https://www.tiktok.com/@user/video/123456789", "tiktok"),
    ("https://vm.tiktok.com/ZMeABC123/", "tiktok"),
    ("https://tiktok.com/t/ZTdABC123/", "tiktok"),
    ("https://www.facebook.com/user/videos/123456789", "facebook"),
    ("https://www.facebook.com/watch/?v=123456789", "facebook"),
    ("https://www.facebook.com/reel/123456789", "facebook"),
    ("https://fb.watch/abc123def/", "facebook"),
    # Regression: this used to be asserted as *unsupported* even though the
    # twitter pattern has always matched it.
    ("https://www.twitter.com/user/status/123", "twitter"),
    ("https://x.com/user/status/123", "twitter"),
    ("https://www.threads.com/@user/post/ABC123", "threads"),
    ("https://9gag.com/gag/avyKyAd", "9gag"),
    ("https://vk.com/video-12345_67890", "vk"),
    ("https://vkvideo.ru/clip-1_2", "vk"),
    ("https://www.google.com", None),
    ("https://example.com/video.mp4", None),
])
def test_detect_platform(bot, url, platform):
    assert bot.downloader.detect_platform(url) == platform
    assert bot.downloader.is_video_url(url) is (platform is not None)


# ----------------------------------------------------------------------------
# De-duplication
# ----------------------------------------------------------------------------

async def test_duplicate_message_is_ignored(bot):
    bot.process_video_url = AsyncMock()
    update = MockUpdate(MockMessage("https://youtu.be/dQw4w9WgXcQ", message_id=1))

    await bot.handle_message(update, None)
    assert bot.process_video_url.await_count == 1

    await bot.handle_message(update, None)
    assert bot.process_video_url.await_count == 1


async def test_same_message_id_in_different_chats_is_not_dropped(bot):
    """Regression: message IDs are only unique per chat, not globally."""
    bot.process_video_url = AsyncMock()

    for chat_id in (111, 222):
        update = MockUpdate(MockMessage(
            "https://youtu.be/dQw4w9WgXcQ", message_id=7, chat=MockChat(chat_id=chat_id)
        ))
        await bot.handle_message(update, None)

    assert bot.process_video_url.await_count == 2


def test_processed_message_cache_is_bounded(bot, monkeypatch):
    monkeypatch.setattr(config, 'PROCESSED_MESSAGE_CACHE_SIZE', 5)
    for message_id in range(50):
        assert bot.remember_message(1, message_id) is True

    assert len(bot.processed_messages) == 5
    # The oldest entries were evicted, the newest are still remembered.
    assert bot.remember_message(1, 49) is False
    assert bot.remember_message(1, 0) is True


# ----------------------------------------------------------------------------
# Chat rules
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("chat_type,platform,tag,expected", [
    ('private', 'youtube', False, True),
    ('private', 'tiktok', False, True),
    ('group', 'tiktok', False, True),
    ('group', 'youtube', False, False),
    ('group', 'youtube', True, True),
    ('supergroup', 'threads', False, False),
    ('supergroup', 'threads', True, True),
])
def test_should_download(bot, chat_type, platform, tag, expected):
    assert bot.should_download(chat_type, platform, tag) is expected


async def test_group_link_without_tag_is_skipped(bot):
    bot.process_video_url = AsyncMock()
    update = MockUpdate(MockMessage(
        "https://youtu.be/dQw4w9WgXcQ", chat=MockChat(chat_id=-100, chat_type='group')
    ))
    await bot.handle_message(update, None)
    bot.process_video_url.assert_not_awaited()


async def test_group_link_with_tag_is_downloaded(bot):
    bot.process_video_url = AsyncMock()
    update = MockUpdate(MockMessage(
        "https://youtu.be/dQw4w9WgXcQ #download",
        chat=MockChat(chat_id=-100, chat_type='group'),
    ))
    await bot.handle_message(update, None)
    bot.process_video_url.assert_awaited_once()


async def test_multiple_links_are_processed_concurrently(bot):
    bot.process_video_url = AsyncMock()
    update = MockUpdate(MockMessage(
        "https://youtu.be/dQw4w9WgXcQ and https://x.com/user/status/123"
    ))
    await bot.handle_message(update, None)
    assert bot.process_video_url.await_count == 2


async def test_unsupported_link_gets_a_hint_in_private_chat(bot):
    message = MockMessage("https://example.com/not-a-video")
    await bot.handle_message(MockUpdate(message), None)
    message.reply_text.assert_awaited_once()
    assert "don't recognise" in message.reply_text.await_args.args[0]


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("command", ['start_command', 'help_command', 'status_command'])
async def test_commands_reply(bot, command):
    message = MockMessage(f"/{command}")
    await getattr(bot, command)(MockUpdate(message), None)
    message.reply_text.assert_awaited_once()
    assert message.reply_text.await_args.args[0]


async def test_status_reports_counters(bot):
    bot.stats.record_success('youtube', 5 * 1024 * 1024)
    bot.stats.record_failure()

    message = MockMessage("/status")
    await bot.status_command(MockUpdate(message), None)

    text = message.reply_text.await_args.args[0]
    assert "1</b> ok" in text
    assert "1</b> failed" in text
    assert "Youtube (1)" in text


async def test_dl_command_forces_download_in_a_group(bot):
    bot.process_video_url = AsyncMock()
    message = MockMessage("/dl", chat=MockChat(chat_id=-100, chat_type='group'))
    context = Mock()
    context.args = ["https://youtu.be/dQw4w9WgXcQ"]

    await bot.download_command(MockUpdate(message), context)
    bot.process_video_url.assert_awaited_once()


async def test_dl_command_reads_the_replied_message(bot):
    bot.process_video_url = AsyncMock()
    message = MockMessage("/dl", chat=MockChat(chat_id=-100, chat_type='group'))
    message.reply_to_message = MockMessage("look https://youtu.be/dQw4w9WgXcQ", message_id=2)
    context = Mock()
    context.args = []

    await bot.download_command(MockUpdate(message), context)
    bot.process_video_url.assert_awaited_once()


async def test_dl_command_without_a_link_explains_itself(bot):
    bot.process_video_url = AsyncMock()
    message = MockMessage("/dl")
    context = Mock()
    context.args = []

    await bot.download_command(MockUpdate(message), context)
    bot.process_video_url.assert_not_awaited()
    message.reply_text.assert_awaited_once()


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, "0 B"), (0, "0 B"), (512, "512 B"),
    (1536, "2 KB"), (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB"),
])
def test_format_size(value, expected):
    assert format_size(value) == expected


@pytest.mark.parametrize("value,expected", [
    (None, None), (0, None), ('Unknown', None),
    (12, "0:12"), (75, "1:15"), (3725, "1:02:05"),
])
def test_format_duration(value, expected):
    assert format_duration(value) == expected


def test_progress_bar():
    assert progress_bar(0, width=10) == '░' * 10
    assert progress_bar(100, width=10) == '█' * 10
    assert progress_bar(50, width=10) == '█' * 5 + '░' * 5
    assert len(progress_bar(None, width=10)) == 10


def test_caption_escapes_html_metacharacters(bot):
    caption = bot._create_caption(
        {'title': 'A <b>bold</b> & "quoted" title', 'platform': 'youtube',
         'duration': 90, 'filesize': 1024},
        1.0,
    )
    assert '&lt;b&gt;bold&lt;/b&gt;' in caption
    assert '&amp;' in caption
    assert caption.startswith('🎥 <b>')


def test_caption_is_truncated_to_the_telegram_limit(bot):
    caption = bot._create_caption(
        {'title': 'x' * 5000, 'platform': 'youtube', 'duration': 90,
         'filesize': 1024, 'url': 'https://youtu.be/abc'},
        1.0, is_document=True,
    )
    assert visible_length(caption) <= config.MAX_CAPTION_LENGTH
    assert '…' in caption


def test_caption_omits_unknown_duration(bot):
    caption = bot._create_caption(
        {'title': 'Clip', 'platform': 'vk', 'duration': None, 'filesize': 1024}, 1.0
    )
    assert '⏱️' not in caption


# ----------------------------------------------------------------------------
# Download / delivery flow
# ----------------------------------------------------------------------------

async def test_download_runs_off_the_event_loop(bot, tmp_path):
    """Regression: blocking yt-dlp calls used to freeze every other chat."""
    record = []
    stub_download(bot, make_video(tmp_path), record)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("https://youtu.be/dQw4w9WgXcQ")
    await bot.process_video_url(message, "https://youtu.be/dQw4w9WgXcQ")

    assert len(record) == 1
    _, _, thread_name = record[0]
    assert thread_name != threading.main_thread().name


async def test_successful_video_cleans_up_and_removes_status(bot, tmp_path):
    result = make_video(tmp_path)
    stub_download(bot, result)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("https://youtu.be/dQw4w9WgXcQ")
    await bot.process_video_url(message, "https://youtu.be/dQw4w9WgXcQ")

    message.status_message.delete.assert_awaited_once()
    assert not (tmp_path / 'clip.mp4').exists()
    assert bot.stats.succeeded == 1
    assert bot.stats.per_platform == {'youtube': 1}


async def test_screenshot_is_sent_then_cleaned_up(bot, tmp_path):
    """Regression: screenshots leaked files and left the status message behind."""
    shot = tmp_path / 'shot.png'
    shot.write_bytes(b'p' * 128)
    stub_download(bot, {
        'filepath': str(shot), 'title': 'A Threads post', 'platform': 'threads',
        'filesize': 128, 'is_screenshot': True,
    })

    message = MockMessage("https://www.threads.com/@u/post/ABC")
    await bot.process_video_url(message, "https://www.threads.com/@u/post/ABC")

    message.reply_photo.assert_awaited_once()
    message.status_message.delete.assert_awaited_once()
    assert not shot.exists()


async def test_failed_download_reports_the_error_and_counts_it(bot):
    stub_download(bot, {'error': 'login_required', 'message': 'This content is private.'})

    message = MockMessage("https://www.instagram.com/p/ABC123/")
    await bot.process_video_url(message, "https://www.instagram.com/p/ABC123/")

    assert bot.stats.failed == 1
    assert any('private' in (text or '') for text in message.status_message.texts)
    message.status_message.delete.assert_not_awaited()


async def test_download_timeout_is_reported(bot, monkeypatch):
    monkeypatch.setattr(config, 'DOWNLOAD_TIMEOUT_SECONDS', 30)

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    bot.downloader.download_video = Mock(
        side_effect=AssertionError("should not run to completion")
    )
    monkeypatch.setattr(asyncio, 'wait_for', fake_wait_for)

    message = MockMessage("https://youtu.be/dQw4w9WgXcQ")
    await bot.process_video_url(message, "https://youtu.be/dQw4w9WgXcQ")

    assert bot.stats.failed == 1
    assert any('timed out' in (text or '') for text in message.status_message.texts)


async def test_oversized_download_is_rejected_before_upload(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_SIZE_MB', 1)
    result = make_video(tmp_path, size=2 * 1024 * 1024)
    stub_download(bot, result)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("https://youtu.be/dQw4w9WgXcQ")
    await bot.process_video_url(message, "https://youtu.be/dQw4w9WgXcQ")

    bot._send_video_with_retry.assert_not_awaited()
    assert not (tmp_path / 'clip.mp4').exists()
    assert any('upload limit' in (text or '') for text in message.status_message.texts)


async def test_privileged_chat_bypasses_the_size_limit(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot, 'PRIVATE_CHAT_IDS', [str(PRIVILEGED_CHAT_ID)])
    record = []
    stub_download(bot, make_video(tmp_path), record)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("https://x.com/u/status/1",
                          chat=MockChat(chat_id=PRIVILEGED_CHAT_ID, chat_type='private'))
    await bot.process_video_url(message, "https://x.com/u/status/1")

    assert record[0][1] is True


async def test_non_privileged_chat_keeps_the_size_limit(bot, tmp_path):
    record = []
    stub_download(bot, make_video(tmp_path), record)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("https://x.com/u/status/1", chat=MockChat(chat_id=999))
    await bot.process_video_url(message, "https://x.com/u/status/1")

    assert record[0][1] is False


async def test_concurrent_downloads_are_bounded(bot, tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(config, 'MAX_CONCURRENT_DOWNLOADS', 2)
    lock = threading.Lock()
    state = {'running': 0, 'peak': 0}

    def slow_download(url, ignore_size_limit=False, progress_callback=None):
        with lock:
            state['running'] += 1
            state['peak'] = max(state['peak'], state['running'])
        try:
            _time.sleep(0.05)
            return make_video(tmp_path, name=f"clip-{url[-1]}.mp4")
        finally:
            with lock:
                state['running'] -= 1

    bot.downloader.download_video = Mock(side_effect=slow_download)
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage("many links")
    await asyncio.gather(*(
        bot.process_video_url(message, f"https://youtu.be/dQw4w9WgXc{i}") for i in range(6)
    ))

    assert state['peak'] <= 2


# ----------------------------------------------------------------------------
# Sending
# ----------------------------------------------------------------------------

async def test_large_file_is_sent_as_a_document(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_SIZE_MB', 200)
    result = make_video(tmp_path, size=1024)
    result['filesize'] = 120 * 1024 * 1024

    message = MockMessage("x")
    assert await bot._send_video_with_retry(message, result, message.status_message) is True
    message.reply_document.assert_awaited_once()
    message.reply_video.assert_not_awaited()


async def test_video_send_falls_back_to_document(bot, tmp_path):
    from telegram.error import BadRequest

    result = make_video(tmp_path)
    message = MockMessage("x")
    message.reply_video = AsyncMock(side_effect=BadRequest("Unsupported video format"))

    assert await bot._send_video_with_retry(message, result, message.status_message) is True
    message.reply_video.assert_awaited_once()
    message.reply_document.assert_awaited_once()


async def test_upload_retries_on_network_errors(bot, tmp_path, monkeypatch):
    from telegram.error import TimedOut

    monkeypatch.setattr(config, 'MAX_UPLOAD_RETRIES', 3)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())

    result = make_video(tmp_path)
    message = MockMessage("x")
    message.reply_video = AsyncMock(side_effect=[TimedOut(), TimedOut(), None])

    assert await bot._send_as_video(message, result, message.status_message, 1.0) is None
    assert message.reply_video.await_count == 3


# ----------------------------------------------------------------------------
# Upload timeouts
#
# python-telegram-bot swaps `write_timeout` for `media_write_timeout` on any
# request carrying a file, and that default is 20 seconds. A 20MB video on a
# slow uplink blows straight through it, so both the builder and every
# individual media call have to override it.
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("sender,reply_method", [
    ('_send_as_video', 'reply_video'),
    ('_send_as_document', 'reply_document'),
    ('_send_screenshot', 'reply_photo'),
])
async def test_media_sends_override_the_20s_default(bot, tmp_path, sender, reply_method):
    result = make_video(tmp_path)
    message = MockMessage("x")

    args = (message, result, message.status_message)
    if sender != '_send_screenshot':
        args = args + (1.0,)
    await getattr(bot, sender)(*args)

    kwargs = getattr(message, reply_method).await_args.kwargs
    assert kwargs['write_timeout'] == config.UPLOAD_TIMEOUT_SECONDS
    assert kwargs['read_timeout'] == config.UPLOAD_TIMEOUT_SECONDS
    assert kwargs['write_timeout'] > 20


def test_upload_timeout_cannot_be_configured_below_a_minute(monkeypatch):
    """A sub-minute media timeout is the exact bug this guards against."""
    import importlib

    monkeypatch.setenv('UPLOAD_TIMEOUT_SECONDS', '15')
    reloaded = importlib.reload(config)
    try:
        assert reloaded.UPLOAD_TIMEOUT_SECONDS == 60
    finally:
        monkeypatch.delenv('UPLOAD_TIMEOUT_SECONDS', raising=False)
        importlib.reload(config)


def test_built_application_raises_ptbs_media_timeout(bot):
    """The builder must override media_write_timeout, not just write_timeout."""
    application = bot.build_application()
    request = application.bot.request

    assert request._media_write_timeout == config.UPLOAD_TIMEOUT_SECONDS
    assert request._media_write_timeout > 20
    assert request._client.timeout.read == config.UPLOAD_TIMEOUT_SECONDS


def test_write_timeout_is_the_retryable_kind():
    from telegram.error import TimedOut

    error = TimedOut()
    error.__cause__ = httpx.WriteTimeout("body never finished sending")
    assert telegram_bot.upload_retry_is_safe(error) is True


def test_read_timeout_is_not_retried():
    """Telegram may already have posted the video; retrying would duplicate it."""
    from telegram.error import TimedOut

    error = TimedOut()
    error.__cause__ = httpx.ReadTimeout("upload sent, reply never arrived")
    assert telegram_bot.upload_retry_is_safe(error) is False


async def test_read_timeout_stops_after_one_attempt(bot, tmp_path, monkeypatch):
    from telegram.error import TimedOut

    monkeypatch.setattr(config, 'MAX_UPLOAD_RETRIES', 3)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())

    def read_timeout():
        error = TimedOut()
        error.__cause__ = httpx.ReadTimeout("reply never arrived")
        return error

    result = make_video(tmp_path)
    message = MockMessage("x")
    message.reply_video = AsyncMock(side_effect=read_timeout())

    assert await bot._send_video_with_retry(message, result, message.status_message) is False
    # One attempt only, and no document fallback - both would risk a duplicate post.
    assert message.reply_video.await_count == 1
    message.reply_document.assert_not_awaited()
    assert any('may still have been posted' in (text or '')
               for text in message.status_message.texts)


async def test_upload_gives_up_after_max_retries(bot, tmp_path, monkeypatch):
    from telegram.error import TimedOut

    monkeypatch.setattr(config, 'MAX_UPLOAD_RETRIES', 2)
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())

    result = make_video(tmp_path)
    message = MockMessage("x")
    message.reply_video = AsyncMock(side_effect=TimedOut())
    message.reply_document = AsyncMock(side_effect=TimedOut())

    assert await bot._send_video_with_retry(message, result, message.status_message) is False
    assert message.reply_video.await_count == 2
    assert message.reply_document.await_count == 2


# ----------------------------------------------------------------------------
# Progress reporting
# ----------------------------------------------------------------------------

async def test_progress_reporter_throttles_edits(bot):
    """yt-dlp fires the hook many times a second; Telegram rate limits edits."""
    status = MockStatusMessage()
    reporter = telegram_bot.ProgressReporter(
        bot, status, asyncio.get_running_loop(), 'youtube', interval=60
    )

    for downloaded in range(1, 20):
        reporter({'status': 'downloading', 'downloaded_bytes': downloaded * 1024,
                  'total_bytes': 20 * 1024, 'percent': downloaded * 5})

    await asyncio.sleep(0)
    assert status.edit_text.await_count <= 1


async def test_progress_reporter_ignores_non_download_events(bot):
    status = MockStatusMessage()
    reporter = telegram_bot.ProgressReporter(
        bot, status, asyncio.get_running_loop(), 'youtube', interval=0
    )

    reporter({'status': 'finished', 'downloaded_bytes': 100})
    await asyncio.sleep(0)
    status.edit_text.assert_not_awaited()


async def test_progress_reporter_edits_the_status_message(bot):
    status = MockStatusMessage()
    reporter = telegram_bot.ProgressReporter(
        bot, status, asyncio.get_running_loop(), 'youtube', interval=0
    )

    reporter({'status': 'downloading', 'downloaded_bytes': 5 * 1024 * 1024,
              'total_bytes': 10 * 1024 * 1024, 'percent': 50.0, 'speed': 1024 * 1024,
              'eta': 5})
    await asyncio.sleep(0.05)

    status.edit_text.assert_awaited()
    text = status.edit_text.await_args.args[0]
    assert '50%' in text and 'MB/s' in text and 'ETA' in text


async def test_video_send_passes_dimensions_and_duration(bot, tmp_path):
    result = make_video(tmp_path)
    message = MockMessage("x")

    await bot._send_as_video(message, result, message.status_message, 1.0)

    kwargs = message.reply_video.await_args.kwargs
    assert kwargs['width'] == 720 and kwargs['height'] == 1280
    assert kwargs['duration'] == 12
    assert kwargs['supports_streaming'] is True
