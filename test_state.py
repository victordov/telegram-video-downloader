"""
Tests for the small stateful helpers: SQLite storage, access control, the
rate limiter, the breakage monitor and the yt-dlp updater.
"""

import datetime as dt
import subprocess
import time

import pytest

import updater
from access import AccessPolicy, RateLimiter
from monitoring import FailureMonitor
from storage import Storage


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------

@pytest.fixture
def storage():
    store = Storage(':memory:')
    yield store
    store.close()


def test_cache_round_trip(storage):
    storage.cache_put('video:youtube:abc', {'kind': 'video', 'file_id': 'F1'})
    assert storage.cache_get('video:youtube:abc') == {'kind': 'video', 'file_id': 'F1'}
    assert storage.cache_count() == 1

    storage.cache_delete('video:youtube:abc')
    assert storage.cache_get('video:youtube:abc') is None


def test_cache_entries_expire(storage, monkeypatch):
    storage.cache_put('k', {'file_id': 'F'})
    real_time = time.time
    monkeypatch.setattr('storage.time.time', lambda: real_time() + 3600)
    assert storage.cache_get('k', max_age_seconds=60) is None
    # Expired entries are removed, not just hidden.
    assert storage.cache_count() == 0


def test_stats_accumulate_per_platform(storage):
    storage.record_success('youtube', 100)
    storage.record_success('youtube', 50)
    storage.record_failure('tiktok', 'boom')

    stats = storage.platform_stats()
    assert stats['youtube']['succeeded'] == 2
    assert stats['youtube']['bytes_sent'] == 150
    assert stats['tiktok']['failed'] == 1
    assert stats['tiktok']['last_error'] == 'boom'
    assert storage.totals() == (2, 1, 150)


def test_state_survives_a_restart(tmp_path):
    path = str(tmp_path / 'nested' / 'bot.sqlite3')
    first = Storage(path)
    first.record_success('vimeo', 10)
    first.cache_put('k', {'file_id': 'F'})
    started = first.first_started_at
    first.close()

    second = Storage(path)
    try:
        assert second.totals() == (1, 0, 10)
        assert second.cache_get('k') == {'file_id': 'F'}
        assert second.first_started_at == started
    finally:
        second.close()


def test_link_tokens_are_short_stable_and_reversible(storage):
    url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=' + 'x' * 200
    token = storage.token_for(url)
    assert token == storage.token_for(url)
    assert len(f"a:{token}".encode()) <= 64  # Telegram's callback_data limit
    assert storage.url_for(token) == url
    assert storage.url_for('missing') is None


def test_meta_pop(storage):
    storage.meta_set('notice', 'hello')
    assert storage.meta_pop('notice') == 'hello'
    assert storage.meta_pop('notice') is None


# ----------------------------------------------------------------------------
# Access control
# ----------------------------------------------------------------------------

def test_empty_allow_list_is_open():
    policy = AccessPolicy()
    assert policy.is_open
    assert policy.is_allowed(1, 2)


def test_allow_list_matches_chat_or_user():
    policy = AccessPolicy(allowed=['-100123', '42'])
    assert policy.is_allowed(-100123, 7)      # anyone in an allowed group
    assert policy.is_allowed(-100999, 42)     # an allowed user in any group
    assert not policy.is_allowed(-100999, 7)
    assert not policy.is_allowed(7, None)


def test_privileged_and_admins_are_always_allowed():
    policy = AccessPolicy(allowed=['1'], privileged=['2'], admins=['3'])
    assert policy.is_allowed(2, 2)
    assert policy.is_allowed(None, 3)
    assert policy.is_admin(3) and not policy.is_admin(2)
    assert policy.is_exempt_from_limits(None, 2)


def test_rate_limiter_blocks_then_frees_a_slot():
    clock = FakeClock()
    limiter = RateLimiter(2, window=3600, clock=clock)

    assert limiter.try_acquire('u') == (True, 0.0)
    assert limiter.try_acquire('u') == (True, 0.0)
    allowed, wait = limiter.try_acquire('u')
    assert not allowed and wait == pytest.approx(3600)

    # Other users have their own budget.
    assert limiter.try_acquire('someone else')[0]

    clock.now += 3601
    assert limiter.try_acquire('u')[0]


def test_rate_limiter_disabled_at_zero():
    limiter = RateLimiter(0)
    assert not limiter.enabled
    assert all(limiter.try_acquire('u')[0] for _ in range(100))


def test_rate_limiter_refund():
    limiter = RateLimiter(1, clock=FakeClock())
    assert limiter.try_acquire('u')[0]
    limiter.refund('u')
    assert limiter.try_acquire('u')[0]


# ----------------------------------------------------------------------------
# Breakage monitor
# ----------------------------------------------------------------------------

def test_monitor_alerts_after_the_threshold_once_per_cooldown():
    clock = FakeClock()
    monitor = FailureMonitor(threshold=3, cooldown_seconds=600, clock=clock)

    assert monitor.record_failure('Instagram', 'download_failed') is None
    assert monitor.record_failure('Instagram', 'download_failed') is None
    alert = monitor.record_failure('Instagram', 'download_failed', 'Unable to extract')
    assert alert and '3 downloads in a row from Instagram' in alert and 'Unable to extract' in alert

    # Still failing, but inside the cooldown: no repeat.
    assert monitor.record_failure('Instagram', 'download_failed') is None
    clock.now += 601
    assert monitor.record_failure('Instagram', 'download_failed') is not None


def test_monitor_ignores_failures_caused_by_the_link():
    monitor = FailureMonitor(threshold=1, cooldown_seconds=0)
    for code in ('login_required', 'unavailable', 'file_too_large', 'live_stream', 'cancelled'):
        assert monitor.record_failure('YouTube', code) is None


def test_monitor_reports_recovery_only_after_an_alert():
    monitor = FailureMonitor(threshold=1, cooldown_seconds=0)
    assert monitor.record_success('TikTok') is None

    assert monitor.record_failure('TikTok', 'download_failed')
    assert 'working again' in monitor.record_success('TikTok')
    assert monitor.record_success('TikTok') is None


def test_success_resets_the_streak():
    monitor = FailureMonitor(threshold=2, cooldown_seconds=0)
    monitor.record_failure('VK', 'download_failed')
    monitor.record_success('VK')
    assert monitor.record_failure('VK', 'download_failed') is None


# ----------------------------------------------------------------------------
# yt-dlp updater
# ----------------------------------------------------------------------------

@pytest.mark.parametrize("version,expected", [
    ('2026.10.26', dt.date(2026, 10, 26)),
    ('2026.10.26.233507', dt.date(2026, 10, 26)),   # nightly builds
    ('unknown', None),
])
def test_version_date(version, expected):
    assert updater.version_date(version) == expected


def test_version_age_days():
    assert updater.version_age_days('2026.10.26', today=dt.date(2026, 11, 9)) == 14


@pytest.mark.parametrize("candidate,current,expected", [
    ('2026.11.01', '2026.10.26', True),
    ('2026.10.26', '2026.10.26', False),
    ('2026.10.26.1', '2026.10.26', True),
    ('2026.9.1', '2026.10.26', False),
    (None, '2026.10.26', False),
])
def test_is_newer(candidate, current, expected):
    assert updater.is_newer(candidate, current) is expected


def test_upgrade_refuses_a_read_only_environment(monkeypatch):
    monkeypatch.setattr(updater, 'can_self_update', lambda: (False, 'read-only'))
    result = updater.upgrade('2026.10.26')
    assert not result.ok and 'read-only' in result.message


def test_upgrade_rolls_back_a_release_that_does_not_import(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, 'can_self_update', lambda: (True, 'ok'))
    monkeypatch.setattr(updater, '_run_pip', lambda *args, **kw: calls.append(args)
                        or subprocess.CompletedProcess(args, 0, '', ''))
    monkeypatch.setattr(updater, '_version_on_disk', lambda: None)

    result = updater.upgrade('2026.10.26')

    assert not result.ok and 'restored' in result.message
    assert calls[-1] == ('yt-dlp==2026.10.26',)


def test_upgrade_reports_the_new_version(monkeypatch):
    monkeypatch.setattr(updater, 'can_self_update', lambda: (True, 'ok'))
    monkeypatch.setattr(updater, '_run_pip',
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, '', ''))
    monkeypatch.setattr(updater, '_version_on_disk', lambda: '2026.11.05')

    result = updater.upgrade('2026.10.26')
    assert result.ok and result.changed
    assert result.new_version == '2026.11.05'


def test_can_self_update_in_this_venv():
    allowed, reason = updater.can_self_update()
    assert isinstance(allowed, bool) and reason


def test_chat_access_decisions_are_stored():
    from storage import Storage
    store = Storage(':memory:')
    store.access_set(-100, Storage.ACCESS_ALLOWED, 'Friends')
    store.access_set('7', Storage.ACCESS_IGNORED, 'Spammer')

    assert store.access_status('-100') == 'allowed'
    assert store.access_entries(Storage.ACCESS_ALLOWED) == {'-100': {'status': 'allowed', 'title': 'Friends'}}
    assert list(store.access_entries()) == ['-100', '7']
    assert store.access_remove('7') and store.access_status('7') is None
    store.close()


def test_granted_chats_are_allowed_and_groups_are_listed():
    from access import AccessPolicy
    policy = AccessPolicy(allowed=['-500', '42'], granted=['-600'])
    assert policy.group_ids == ['-500', '-600']
    assert policy.is_allowed(-600, 9)
    policy.revoke('-600')
    assert not policy.is_allowed(-600, 9)
    policy.grant(-700)
    assert policy.is_allowed(-700, None)
