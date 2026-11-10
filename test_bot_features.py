"""
Bot-level tests for access control, rate limiting, the file-ID cache, albums,
audio, inline buttons, captions, inline mode, alerts, updates and webhooks.

Telegram and the downloader are stubbed, as in test_bot.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import InlineQueryResultCachedVideo, InputMediaPhoto, InputMediaVideo
from telegram.error import BadRequest

import config
import telegram_bot
from test_bot import MockChat, MockMessage, MockUpdate, MockUser, make_video, stub_download, visible_length
from video_downloader import MB, MODE_AUDIO

YOUTUBE = "https://youtu.be/dQw4w9WgXcQ"
OWNER_ID = 100000001


def sent(kind, file_id):
    """What Telegram returns for a sent message, reduced to the file ID."""
    if kind == 'photo':
        return SimpleNamespace(photo=[SimpleNamespace(file_id=file_id + '-small'),
                                      SimpleNamespace(file_id=file_id)])
    return SimpleNamespace(**{kind: SimpleNamespace(file_id=file_id)})


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(telegram_bot, 'PRIVATE_CHAT_IDS', [str(OWNER_ID)])
    return MockChat(chat_id=OWNER_ID, chat_type='private')


def restricted(bot, allowed=(), admins=()):
    """Switch the bot to an allow list."""
    from access import AccessPolicy
    bot.access = AccessPolicy(allowed=allowed, privileged=telegram_bot.PRIVATE_CHAT_IDS,
                              admins=admins)


# ----------------------------------------------------------------------------
# Access control
# ----------------------------------------------------------------------------

async def test_strangers_are_told_the_bot_is_private_with_their_id(bot):
    restricted(bot, allowed=['42'])
    bot.process_video_url = AsyncMock()
    message = MockMessage(YOUTUBE, chat=MockChat(chat_id=7), user=MockUser(7))

    await bot.handle_message(MockUpdate(message), None)

    bot.process_video_url.assert_not_awaited()
    text = message.reply_text.await_args.args[0]
    assert 'private' in text and '<code>7</code>' in text


async def test_the_private_notice_is_not_repeated(bot):
    restricted(bot, allowed=['42'])
    for message_id in (1, 2):
        message = MockMessage(YOUTUBE, message_id=message_id, chat=MockChat(chat_id=7),
                              user=MockUser(7))
        await bot.handle_message(MockUpdate(message), None)
    message.reply_text.assert_not_awaited()


async def test_unknown_groups_are_ignored_silently(bot):
    restricted(bot, allowed=['42'])
    bot.process_video_url = AsyncMock()
    message = MockMessage(YOUTUBE + " #download",
                          chat=MockChat(chat_id=-100, chat_type='group'), user=MockUser(7))
    await bot.handle_message(MockUpdate(message), None)
    bot.process_video_url.assert_not_awaited()
    message.reply_text.assert_not_awaited()


async def test_allowed_users_work_in_any_group(bot):
    restricted(bot, allowed=['42'])
    bot.process_video_url = AsyncMock()
    message = MockMessage(YOUTUBE + " #download",
                          chat=MockChat(chat_id=-100, chat_type='group'), user=MockUser(42))
    await bot.handle_message(MockUpdate(message), None)
    bot.process_video_url.assert_awaited_once()


async def test_privileged_chat_is_always_allowed(bot, owner):
    restricted(bot, allowed=['42'])
    bot.process_video_url = AsyncMock()
    await bot.handle_message(MockUpdate(MockMessage(YOUTUBE, chat=owner,
                                                    user=MockUser(OWNER_ID))), None)
    bot.process_video_url.assert_awaited_once()


async def test_status_is_private_too(bot):
    restricted(bot, allowed=['42'])
    message = MockMessage("/status", chat=MockChat(chat_id=7), user=MockUser(7))
    await bot.status_command(MockUpdate(message), None)
    assert 'private' in message.reply_text.await_args.args[0]


# ----------------------------------------------------------------------------
# Rate limiting
# ----------------------------------------------------------------------------

async def test_rate_limit_stops_the_next_download(bot, tmp_path, monkeypatch):
    from access import RateLimiter
    monkeypatch.setattr(config, 'RATE_LIMIT_PER_HOUR', 1)
    bot.rate_limiter = RateLimiter(1)
    stub_download(bot, make_video(tmp_path))
    bot._send_video_with_retry = AsyncMock(return_value=True)

    user = MockUser(5)
    first = MockMessage("https://youtu.be/aaaaaaaaaaa", user=user)
    await bot.process_video_url(first, "https://youtu.be/aaaaaaaaaaa")
    second = MockMessage("https://youtu.be/bbbbbbbbbbb", user=user)
    await bot.process_video_url(second, "https://youtu.be/bbbbbbbbbbb")

    assert bot.downloader.download_video.call_count == 1
    assert 'last hour' in second.reply_text.await_args.args[0]


async def test_privileged_chats_skip_the_rate_limit(bot, tmp_path, owner):
    from access import RateLimiter
    bot.rate_limiter = RateLimiter(1)
    stub_download(bot, make_video(tmp_path))
    bot._send_video_with_retry = AsyncMock(return_value=True)

    for video_id in ('aaaaaaaaaaa', 'bbbbbbbbbbb'):
        await bot.process_video_url(MockMessage("x", chat=owner), f"https://youtu.be/{video_id}")

    assert bot.downloader.download_video.call_count == 2


# ----------------------------------------------------------------------------
# File-ID cache
# ----------------------------------------------------------------------------

async def test_a_repeated_link_is_resent_from_the_cache(bot, tmp_path):
    stub_download(bot, make_video(tmp_path))
    first = MockMessage(YOUTUBE)
    first.reply_video = AsyncMock(return_value=sent('video', 'VIDEO-1'))
    await bot.process_video_url(first, YOUTUBE)

    # Same video, different spelling of the link.
    second = MockMessage("again")
    await bot.process_video_url(second, "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=share")

    assert bot.downloader.download_video.call_count == 1
    kwargs = second.reply_video.await_args.kwargs
    assert kwargs['video'] == 'VIDEO-1'
    assert 'Clip' in kwargs['caption']
    assert bot.stats.cache_hits == 1


async def test_a_stale_file_id_falls_back_to_downloading(bot, tmp_path):
    bot.storage.cache_put(bot._cache_key(YOUTUBE, 'video'),
                          {'kind': 'video', 'file_id': 'GONE', 'url': YOUTUBE})
    stub_download(bot, make_video(tmp_path))
    bot._send_video_with_retry = AsyncMock(return_value=True)

    message = MockMessage(YOUTUBE)
    message.reply_video = AsyncMock(side_effect=BadRequest("Wrong file identifier"))
    await bot.process_video_url(message, YOUTUBE)

    assert bot.downloader.download_video.call_count == 1
    assert bot.storage.cache_get(bot._cache_key(YOUTUBE, 'video')) is None


async def test_audio_and_video_are_cached_separately(bot, tmp_path):
    bot.storage.cache_put(bot._cache_key(YOUTUBE, 'video'),
                          {'kind': 'video', 'file_id': 'V', 'url': YOUTUBE})
    assert bot._cached(bot._cache_key(YOUTUBE, MODE_AUDIO)) is None


async def test_screenshots_are_not_cached(bot):
    result = {'is_screenshot': True, 'telegram_file': {'kind': 'photo', 'file_id': 'P'}}
    bot._cache_delivery('video:threads:x', result)
    assert bot.storage.cache_count() == 0


# ----------------------------------------------------------------------------
# Albums
# ----------------------------------------------------------------------------

def album(tmp_path, kinds=('video', 'photo', 'video')):
    items = []
    for index, kind in enumerate(kinds):
        name = f"item{index}.{'mp4' if kind == 'video' else 'jpg'}"
        path = tmp_path / name
        path.write_bytes(b'x' * 64)
        items.append({'filepath': str(path), 'kind': kind, 'filesize': 64,
                      'title': f'item {index}', 'platform': 'instagram'})
    return {'is_album': True, 'items': items, 'kind': 'album', 'title': 'Post by someone',
            'platform': 'instagram', 'url': 'https://www.instagram.com/p/POST1/',
            'filesize': 64 * len(items), 'duration': None}


async def test_album_is_sent_as_one_media_group(bot, tmp_path):
    result = album(tmp_path)
    stub_download(bot, result)
    message = MockMessage("https://www.instagram.com/p/POST1/")
    message.reply_media_group = AsyncMock(return_value=(
        sent('video', 'V0'), sent('photo', 'P1'), sent('video', 'V2'),
    ))

    await bot.process_video_url(message, "https://www.instagram.com/p/POST1/")

    media = message.reply_media_group.await_args.kwargs['media']
    assert [type(m) for m in media] == [InputMediaVideo, InputMediaPhoto, InputMediaVideo]
    assert 'Post by someone' in media[0].caption and media[1].caption is None
    # Files go up as bytes: PTB would turn a path into a file:// URI.
    assert not isinstance(media[0].media, str)
    assert all(not (tmp_path / f"item{i}.{'mp4' if i != 1 else 'jpg'}").exists() for i in range(3))
    assert bot.stats.succeeded == 1

    # And the whole album comes back from the cache next time.
    again = MockMessage("again")
    await bot.process_video_url(again, "https://www.instagram.com/p/POST1/")
    resent = again.reply_media_group.await_args.kwargs['media']
    assert [m.media for m in resent] == ['V0', 'P1', 'V2']


def test_album_groups_respect_telegrams_limits(bot):
    entries = [telegram_bot.AlbumEntry(kind='photo', source=str(i), size=1) for i in range(12)]
    entries.insert(5, telegram_bot.AlbumEntry(kind='document', source='doc', size=1))
    groups = bot._album_groups(entries)
    assert [len(group) for group in groups] == [5, 1, 7]
    assert groups[1][0].kind == 'document'
    assert all(len(group) <= 10 for group in groups)


def test_album_groups_split_on_upload_size(bot, monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_SIZE_MB', 50)
    entries = [telegram_bot.AlbumEntry(kind='video', source=str(i), size=30 * MB) for i in range(3)]
    assert [len(group) for group in bot._album_groups(entries)] == [1, 1, 1]


# ----------------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------------

async def test_audio_command_requests_audio_mode(bot):
    bot.process_video_url = AsyncMock()
    context = Mock()
    context.args = [YOUTUBE]
    await bot.audio_command(MockUpdate(MockMessage("/audio")), context)
    assert bot.process_video_url.await_args.kwargs['mode'] == MODE_AUDIO


async def test_audio_is_sent_with_title_and_performer(bot, tmp_path):
    path = tmp_path / 'song.m4a'
    path.write_bytes(b'a' * 100)
    result = {'filepath': str(path), 'kind': 'audio', 'title': 'Song', 'uploader': 'Band',
              'duration': 61.5, 'filesize': 100, 'platform': 'youtube', 'url': YOUTUBE}
    message = MockMessage("x")
    message.reply_audio = AsyncMock(return_value=sent('audio', 'AUDIO-1'))

    assert await bot._send_audio(message, result, message.status_message) is True
    kwargs = message.reply_audio.await_args.kwargs
    assert kwargs['title'] == 'Song' and kwargs['performer'] == 'Band'
    assert kwargs['duration'] == 61
    assert result['telegram_file']['file_id'] == 'AUDIO-1'


async def test_audio_that_is_not_m4a_goes_as_a_file(bot, tmp_path):
    path = tmp_path / 'song.webm'
    path.write_bytes(b'a')
    message = MockMessage("x")
    await bot._send_audio(message, {'filepath': str(path), 'kind': 'audio', 'filesize': 1},
                          message.status_message)
    message.reply_document.assert_awaited_once()
    message.reply_audio.assert_not_awaited()


# ----------------------------------------------------------------------------
# Buttons
# ----------------------------------------------------------------------------

def button_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def test_videos_get_an_audio_button(bot, tmp_path):
    stub_download(bot, make_video(tmp_path))
    message = MockMessage(YOUTUBE)
    await bot.process_video_url(message, YOUTUBE)

    markup = message.reply_video.await_args.kwargs['reply_markup']
    data = button_data(markup)
    assert len(data) == 1 and data[0].startswith('a:')
    assert bot.storage.url_for(data[0][2:]) == YOUTUBE


async def test_hd_button_only_for_privileged_chats(bot, tmp_path, owner):
    result = dict(make_video(tmp_path), hd_available=True, url=YOUTUBE)
    assert len(button_data(bot._media_buttons(MockMessage("x"), result, 'video'))) == 1
    assert len(button_data(bot._media_buttons(MockMessage("x", chat=owner), result, 'video'))) == 2


async def test_status_message_has_a_cancel_button(bot, tmp_path):
    stub_download(bot, make_video(tmp_path))
    bot._send_video_with_retry = AsyncMock(return_value=True)
    message = MockMessage(YOUTUBE)
    await bot.process_video_url(message, YOUTUBE)

    markup = message.reply_text.await_args.kwargs['reply_markup']
    assert button_data(markup)[0].startswith('x:')


def callback(data, user_id, message=None):
    query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id),
                            message=message, answer=AsyncMock())
    return SimpleNamespace(callback_query=query), query


async def test_cancel_button_sets_the_event_for_the_owner_only(bot):
    job = telegram_bot.ActiveJob(job_id='job1', user_id=5, chat_id=1, url=YOUTUBE)
    bot.jobs['job1'] = job

    update, query = callback('x:job1', user_id=6)
    await bot.button_callback(update, None)
    assert not job.cancel_event.is_set()
    assert 'Only the person' in query.answer.await_args.args[0]

    update, query = callback('x:job1', user_id=5)
    await bot.button_callback(update, None)
    assert job.cancel_event.is_set()


async def test_audio_button_downloads_the_audio(bot, monkeypatch):
    from telegram import Message
    bot.process_video_url = AsyncMock()
    token = bot.storage.token_for(YOUTUBE)
    message = Mock(spec=Message)
    message.chat = MockChat()

    update, query = callback(f'a:{token}', user_id=5, message=message)
    await bot.button_callback(update, None)

    args, kwargs = bot.process_video_url.await_args
    assert args[1] == YOUTUBE and kwargs['mode'] == MODE_AUDIO and kwargs['user_id'] == 5


async def test_hd_button_is_refused_outside_privileged_chats(bot):
    from telegram import Message
    bot.process_video_url = AsyncMock()
    message = Mock(spec=Message)
    message.chat = MockChat(chat_id=999)

    update, query = callback(f'h:{bot.storage.token_for(YOUTUBE)}', user_id=999, message=message)
    await bot.button_callback(update, None)
    bot.process_video_url.assert_not_awaited()


async def test_expired_buttons_say_so(bot):
    from telegram import Message
    message = Mock(spec=Message)
    message.chat = MockChat()
    update, query = callback('a:unknowntoken', user_id=5, message=message)
    await bot.button_callback(update, None)
    assert 'expired' in query.answer.await_args.args[0]


async def test_download_timeout_cancels_the_worker(bot, monkeypatch):
    monkeypatch.setattr(config, 'DOWNLOAD_TIMEOUT_SECONDS', 30)
    seen = {}

    def slow(url, **kwargs):
        seen['event'] = kwargs['cancel_event']
        return {'error': 'cancelled'}

    async def fake_wait_for(awaitable, timeout):
        await awaitable
        raise asyncio.TimeoutError

    bot.downloader.download_video = Mock(side_effect=slow)
    monkeypatch.setattr(asyncio, 'wait_for', fake_wait_for)
    await bot.process_video_url(MockMessage(YOUTUBE), YOUTUBE)
    assert seen['event'].is_set()


async def test_cancelled_downloads_are_not_failures(bot):
    stub_download(bot, {'error': 'cancelled', 'platform': 'youtube'})
    message = MockMessage(YOUTUBE)
    await bot.process_video_url(message, YOUTUBE)
    assert bot.stats.failed == 0
    assert any('Cancelled' in (text or '') for text in message.status_message.texts)


# ----------------------------------------------------------------------------
# Captions
# ----------------------------------------------------------------------------

def test_caption_links_the_author_and_folds_the_description(bot):
    caption = bot._create_caption({
        'title': 'Clip', 'platform': 'youtube', 'duration': 10, 'url': YOUTUBE,
        'uploader': 'Some <One>', 'uploader_url': 'https://www.youtube.com/@one',
        'description': 'A longer story about the clip & more.',
    }, 1.0)
    assert '👤 <a href="https://www.youtube.com/@one">Some &lt;One&gt;</a>' in caption
    assert '<blockquote expandable>A longer story about the clip &amp; more.</blockquote>' in caption
    assert '📱 YouTube' in caption


def test_caption_skips_a_description_that_repeats_the_title(bot):
    caption = bot._create_caption({'title': 'my holiday #travel', 'platform': 'tiktok',
                                   'description': 'my holiday #travel'}, 1.0)
    assert 'blockquote' not in caption


def test_long_title_and_description_stay_under_the_limit(bot):
    caption = bot._create_caption({
        'title': '🎬' * 600, 'platform': 'youtube', 'duration': 90, 'url': YOUTUBE,
        'uploader': 'u' * 100, 'uploader_url': 'https://x.com/u',
        'description': 'd' * 5000, 'compressed': True,
    }, 1.0, is_document=True)
    assert telegram_bot.tg_len(telegram_bot.html.unescape(
        telegram_bot.re.sub(r'<[^>]+>', '', caption))) <= config.MAX_CAPTION_LENGTH
    assert visible_length(caption) <= config.MAX_CAPTION_LENGTH
    assert 'Compressed to fit' in caption


async def test_adult_content_is_sent_behind_a_spoiler(bot, tmp_path):
    result = dict(make_video(tmp_path), age_limit=18)
    message = MockMessage("x")
    await bot._send_as_video(message, result, message.status_message, 1.0)
    assert message.reply_video.await_args.kwargs['has_spoiler'] is True


async def test_normal_content_has_no_spoiler(bot, tmp_path):
    message = MockMessage("x")
    await bot._send_as_video(message, make_video(tmp_path), message.status_message, 1.0)
    assert 'has_spoiler' not in message.reply_video.await_args.kwargs


def test_progress_shows_the_shrinking_stage():
    text = telegram_bot.render_progress('youtube', {
        'status': 'compressing', 'percent': 40.0, 'target_bytes': 50 * MB,
        'item_index': 2, 'item_count': 3,
    })
    assert 'Shrinking to fit 50.0 MB' in text and '40%' in text and 'item 2/3' in text


async def test_progress_edits_keep_the_cancel_button(bot):
    from test_bot import MockStatusMessage
    status = MockStatusMessage()
    markup = object()
    reporter = telegram_bot.ProgressReporter(bot, status, asyncio.get_running_loop(),
                                             'youtube', interval=0, reply_markup=markup)
    reporter({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 2, 'percent': 50})
    await asyncio.sleep(0.05)
    assert status.edit_text.await_args.kwargs['reply_markup'] is markup


# ----------------------------------------------------------------------------
# Inline mode and deep links
# ----------------------------------------------------------------------------

def inline_update(text, user_id=5):
    query = SimpleNamespace(query=text, from_user=SimpleNamespace(id=user_id), answer=AsyncMock())
    return SimpleNamespace(inline_query=query), query


async def test_inline_mode_shares_a_cached_video(bot):
    bot.storage.cache_put(bot._cache_key(YOUTUBE, 'video'), {
        'kind': 'video', 'file_id': 'VID', 'caption': '🎥 <b>Clip</b>', 'title': 'Clip',
        'platform': 'youtube', 'url': YOUTUBE,
    })
    update, query = inline_update(YOUTUBE)
    await bot.inline_query(update, None)

    results = query.answer.await_args.args[0]
    assert isinstance(results[0], InlineQueryResultCachedVideo)
    assert results[0].video_file_id == 'VID'


async def test_inline_mode_offers_a_private_chat_for_new_links(bot):
    update, query = inline_update(YOUTUBE)
    await bot.inline_query(update, None)

    assert query.answer.await_args.args[0] == []
    button = query.answer.await_args.kwargs['button']
    assert button.start_parameter == 'dl_' + bot.storage.token_for(YOUTUBE)


async def test_inline_mode_is_private_too(bot):
    restricted(bot, allowed=['42'])
    update, query = inline_update(YOUTUBE, user_id=7)
    await bot.inline_query(update, None)
    assert 'private' in query.answer.await_args.kwargs['button'].text


async def test_deep_link_downloads_the_link(bot):
    bot.process_video_url = AsyncMock()
    token = bot.storage.token_for(YOUTUBE)
    context = Mock()
    context.args = ['dl_' + token]
    await bot.start_command(MockUpdate(MockMessage("/start")), context)
    assert bot.process_video_url.await_args.args[1] == YOUTUBE


async def test_inline_prefetch_uploads_to_the_cache_chat(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'CACHE_CHAT_ID', '-1001')
    bot.application = SimpleNamespace(bot=SimpleNamespace(
        send_video=AsyncMock(return_value=sent('video', 'PREFETCHED')),
    ))
    stub_download(bot, make_video(tmp_path))

    key = bot._cache_key(YOUTUBE, 'video')
    await bot._inline_prefetch(YOUTUBE, key, 'youtube')

    kwargs = bot.application.bot.send_video.await_args.kwargs
    assert kwargs['chat_id'] == '-1001' and kwargs['disable_notification'] is True
    assert bot._cached(key)['file_id'] == 'PREFETCHED'


# ----------------------------------------------------------------------------
# Alerts, statistics and maintenance
# ----------------------------------------------------------------------------

async def test_admins_hear_when_a_site_keeps_failing(bot, monkeypatch):
    monkeypatch.setattr(config, 'ALERT_FAILURE_THRESHOLD', 3)
    bot.notify_admins = AsyncMock()
    stub_download(bot, {'error': 'download_failed', 'platform': 'instagram',
                        'detail': 'Unable to extract shared data'})

    for index in range(3):
        url = f"https://www.instagram.com/p/POST{index}/"
        await bot.process_video_url(MockMessage(url), url)
    await asyncio.sleep(0)

    bot.notify_admins.assert_awaited_once()
    text = bot.notify_admins.await_args.args[0]
    assert 'Instagram' in text and 'Unable to extract' in text and 'yt-dlp' in text


async def test_private_posts_do_not_alert(bot):
    bot.notify_admins = AsyncMock()
    stub_download(bot, {'error': 'login_required', 'platform': 'instagram'})
    for index in range(5):
        url = f"https://www.instagram.com/p/POST{index}/"
        await bot.process_video_url(MockMessage(url), url)
    await asyncio.sleep(0)
    bot.notify_admins.assert_not_awaited()


async def test_status_shows_lifetime_stats_and_yt_dlp(bot):
    bot.storage.record_success('tiktok', 10 * MB)  # e.g. from before a restart
    message = MockMessage("/status")
    await bot.status_command(MockUpdate(message), None)
    text = message.reply_text.await_args.args[0]
    assert 'Since' in text and 'TikTok (1)' in text
    assert 'yt-dlp' in text


async def test_update_command_is_admin_only(bot):
    bot.check_for_update = AsyncMock(return_value='done')
    message = MockMessage("/update", user=MockUser(7))
    await bot.update_command(MockUpdate(message), None)
    bot.check_for_update.assert_not_awaited()


async def test_admin_update_command_runs_the_update(bot):
    restricted(bot, admins=['7'])
    bot.check_for_update = AsyncMock(return_value='Updated yt-dlp 1 → 2.')
    message = MockMessage("/update", user=MockUser(7))
    await bot.update_command(MockUpdate(message), None)
    bot.check_for_update.assert_awaited_once()
    assert 'Updated' in message.status_message.texts[-1]


async def test_check_for_update_upgrades_and_schedules_a_restart(bot, monkeypatch):
    import updater
    monkeypatch.setattr(updater, 'installed_version', lambda: '2026.10.26')
    monkeypatch.setattr(updater, 'latest_version', lambda: '2026.11.05')
    monkeypatch.setattr(updater, 'can_self_update', lambda: (True, 'ok'))
    monkeypatch.setattr(updater, 'upgrade', lambda current: updater.UpgradeResult(
        True, current, '2026.11.05', 'Updated yt-dlp 2026.10.26 → 2026.11.05.'))
    bot.request_restart = Mock()

    text = await bot.check_for_update(manual=True)

    assert 'Restarting' in text
    bot.request_restart.assert_called_once()
    assert 'restarted' in bot.storage.meta_get('pending_admin_notice')


async def test_check_for_update_does_nothing_when_current(bot, monkeypatch):
    import updater
    monkeypatch.setattr(updater, 'installed_version', lambda: '2026.11.05')
    monkeypatch.setattr(updater, 'latest_version', lambda: '2026.11.05')
    monkeypatch.setattr(updater, 'upgrade', Mock(side_effect=AssertionError))
    assert 'latest' in await bot.check_for_update(manual=True)


async def test_no_new_downloads_while_a_restart_is_pending(bot, tmp_path):
    stub_download(bot, make_video(tmp_path))
    bot._restart_task = Mock()
    message = MockMessage(YOUTUBE)
    await bot.process_video_url(message, YOUTUBE)
    bot.downloader.download_video.assert_not_called()
    assert 'restarting' in message.reply_text.await_args.args[0]


async def test_restart_waits_for_active_downloads(bot, monkeypatch):
    bot.application = SimpleNamespace(stop_running=Mock())
    bot.stats.active = 1
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock(side_effect=lambda _s: setattr(bot.stats, 'active', 0)))
    await bot._restart_when_idle()
    assert bot._restart_requested
    bot.application.stop_running.assert_called_once()


# ----------------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------------

def test_webhook_mode_uses_run_webhook(bot, monkeypatch):
    monkeypatch.setattr(config, 'WEBHOOK_URL', 'https://bot.example.com')
    monkeypatch.setattr(config, 'WEBHOOK_PATH', 'telegram')
    monkeypatch.setattr(config, 'WEBHOOK_SECRET', 'secret123')
    app = Mock()
    bot.build_application = Mock(return_value=app)

    bot.run()

    kwargs = app.run_webhook.call_args.kwargs
    assert kwargs['webhook_url'] == 'https://bot.example.com/telegram'
    assert kwargs['secret_token'] == 'secret123'
    assert kwargs['url_path'] == 'telegram'
    app.run_polling.assert_not_called()


def test_polling_is_the_default(bot):
    app = Mock()
    bot.build_application = Mock(return_value=app)
    bot.run()
    app.run_polling.assert_called_once()
    app.run_webhook.assert_not_called()


def test_webhook_secret_defaults_to_a_token_derived_value():
    assert config.WEBHOOK_SECRET and len(config.WEBHOOK_SECRET) >= 32
    assert config.TELEGRAM_BOT_TOKEN not in config.WEBHOOK_SECRET


# ----------------------------------------------------------------------------
# Approving chats from Telegram, and members of allowed groups
# ----------------------------------------------------------------------------

ADMIN_ID = 1


def fake_telegram(bot, member_status='member'):
    """bot.application.bot with the calls the access code makes."""
    api = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status=member_status)),
        get_chat=AsyncMock(return_value=SimpleNamespace(title='Friends', username=None)),
        send_message=AsyncMock(),
    )
    bot.application = SimpleNamespace(bot=api)
    return api


@pytest.fixture
def gated(bot, monkeypatch):
    """Allow list of one group, one admin, and a Telegram that answers."""
    monkeypatch.setattr(config, 'ADMIN_CHAT_IDS', [str(ADMIN_ID)])
    restricted(bot, allowed=['-500'], admins=[str(ADMIN_ID)])
    bot.process_video_url = AsyncMock()
    fake_telegram(bot)
    return bot


def group_link(message_id=1, chat_id=-100, user_id=7):
    return MockMessage(YOUTUBE + " #download", message_id=message_id,
                       chat=MockChat(chat_id=chat_id, chat_type='supergroup', title='Friends'),
                       user=MockUser(user_id))


async def test_an_unknown_group_sends_the_admins_an_allow_prompt(gated):
    gated.notify_admins = AsyncMock()
    await gated.handle_message(MockUpdate(group_link()), None)
    await gated.handle_message(MockUpdate(group_link(message_id=2)), None)

    gated.notify_admins.assert_awaited_once()  # once a day, not once a link
    text = gated.notify_admins.await_args.args[0]
    assert 'Friends' in text and '-100' in text
    assert button_data(gated.notify_admins.await_args.kwargs['reply_markup']) == ['ga:-100', 'gi:-100']


async def test_a_stranger_is_told_the_owner_was_asked(gated):
    fake_telegram(gated, member_status='left')
    gated.notify_admins = AsyncMock()
    message = MockMessage(YOUTUBE, chat=MockChat(chat_id=7), user=MockUser(7))
    await gated.handle_message(MockUpdate(message), None)
    assert 'asked its owner' in message.reply_text.await_args.args[0]


async def test_allow_button_lets_the_chat_in_and_survives_a_restart(gated):
    update, query = callback('ga:-100', user_id=ADMIN_ID)
    await gated.button_callback(update, None)

    assert gated.storage.access_status('-100') == 'allowed'
    await asyncio.sleep(0)
    gated.application.bot.send_message.assert_awaited()  # "I can download links here now"

    await gated.handle_message(MockUpdate(group_link(message_id=3)), None)
    gated.process_video_url.assert_awaited_once()

    # A fresh instance (a restart) reads the decision back from the database.
    from access import AccessPolicy
    reloaded = AccessPolicy(allowed=['-500'], granted=gated.storage.access_entries('allowed'))
    assert reloaded.is_allowed(-100, 7)


async def test_only_admins_can_answer_access_prompts(gated):
    update, query = callback('ga:-100', user_id=7)
    await gated.button_callback(update, None)
    assert gated.storage.access_status('-100') is None
    assert not gated.access.is_allowed(-100, 7)
    assert query.answer.await_args.kwargs.get('show_alert')


async def test_ignored_chats_stop_prompting(gated):
    gated.notify_admins = AsyncMock()
    update, _ = callback('gi:-100', user_id=ADMIN_ID)
    await gated.button_callback(update, None)

    await gated.handle_message(MockUpdate(group_link(message_id=4)), None)
    gated.notify_admins.assert_not_awaited()
    gated.process_video_url.assert_not_awaited()


async def test_members_of_an_allowed_group_can_use_it_privately(gated):
    api = gated.application.bot
    for message_id in (1, 2):
        message = MockMessage(YOUTUBE, message_id=message_id, chat=MockChat(chat_id=7),
                              user=MockUser(7))
        await gated.handle_message(MockUpdate(message), None)

    assert gated.process_video_url.await_count == 2
    api.get_chat_member.assert_awaited_once_with(-500, 7)  # cached after the first check


async def test_people_who_left_the_group_are_refused(gated):
    fake_telegram(gated, member_status='left')
    gated.notify_admins = AsyncMock()
    message = MockMessage(YOUTUBE, chat=MockChat(chat_id=7), user=MockUser(7))
    await gated.handle_message(MockUpdate(message), None)
    gated.process_video_url.assert_not_awaited()


async def test_group_members_can_use_inline_mode(gated):
    update, query = inline_update(YOUTUBE, user_id=7)
    await gated.inline_query(update, None)
    assert query.answer.await_args.kwargs['button'].start_parameter.startswith('dl_')


async def test_access_command_lists_chats_and_can_forget_them(gated):
    gated.access.grant('-100')
    gated.storage.access_set('-100', 'allowed', 'Friends')
    message = MockMessage("/access", user=MockUser(ADMIN_ID))
    await gated.access_command(MockUpdate(message), None)

    text = message.reply_text.await_args.args[0]
    assert 'Friends' in text and '-500' in text
    assert button_data(message.reply_text.await_args.kwargs['reply_markup']) == ['gr:-100']

    update, _ = callback('gr:-100', user_id=ADMIN_ID)
    await gated.button_callback(update, None)
    assert gated.storage.access_status('-100') is None
    assert not gated.access.is_allowed(-100, 7)


async def test_access_command_is_admin_only(gated):
    message = MockMessage("/access", user=MockUser(7))
    await gated.access_command(MockUpdate(message), None)
    assert 'admins' in message.reply_text.await_args.args[0]
