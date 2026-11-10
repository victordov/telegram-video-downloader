"""DNS-over-HTTPS fallback for yt-dlp's curl_cffi handler (no network)."""

import pytest

import dns_fallback

_curlcffi = pytest.importorskip('yt_dlp.networking._curlcffi')
from yt_dlp.networking.exceptions import TransportError  # noqa: E402


class _Session:
    doh_url = None


class _FakeHandler:
    """Stands in for CurlCFFIRH: records the resolver each attempt used."""

    def __init__(self, failures):
        self.failures = list(failures)  # one entry per call: an exception or None
        self.used = []
        self.cleared = 0

    def _create_instance(self, cookiejar=None):
        return _Session()

    def _send(self, request):
        session = self._create_instance()
        self.used.append(session.doh_url)
        failure = self.failures.pop(0) if self.failures else None
        if failure:
            raise failure
        return 'response'

    def _clear_instances(self):
        self.cleared += 1


def _resolve_error():
    return TransportError(cause=Exception('curl: (28) Resolving timed out after 20000 milliseconds'))


@pytest.fixture
def handler(monkeypatch):
    cls = type('FakeRH', (_FakeHandler,), {})
    monkeypatch.setattr(_curlcffi, 'CurlCFFIRH', cls)
    monkeypatch.setitem(dns_fallback._state, 'doh_url', None)
    assert dns_fallback.install('https://1.1.1.1/dns-query')
    return cls


def test_switches_to_doh_when_plain_dns_fails_and_stays(handler):
    h = handler([_resolve_error()])
    assert h._send('req') == 'response'
    assert h.used == [None, 'https://1.1.1.1/dns-query']
    assert h.cleared == 1
    assert dns_fallback.active_doh_url() == 'https://1.1.1.1/dns-query'

    h2 = handler([])
    h2._send('req')
    assert h2.used == ['https://1.1.1.1/dns-query']  # no slow first attempt again


def test_switches_back_when_doh_fails(handler):
    dns_fallback._state['doh_url'] = 'https://1.1.1.1/dns-query'
    h = handler([_resolve_error()])
    h._send('req')
    assert h.used == ['https://1.1.1.1/dns-query', None]
    assert dns_fallback.active_doh_url() is None


def test_other_errors_are_not_retried(handler):
    h = handler([TransportError(cause=Exception('Connection reset by peer'))])
    with pytest.raises(TransportError):
        h._send('req')
    assert h.used == [None]
    assert dns_fallback.active_doh_url() is None


def test_empty_setting_installs_nothing():
    assert dns_fallback.install(None) is False
    assert dns_fallback.install('') is False


def test_probe_switches_early_when_dns_is_blocked(handler, monkeypatch):
    from curl_cffi import requests as curl_requests

    def blocked(*args, **kwargs):
        raise Exception('curl: (28) Resolving timed out after 5000 milliseconds')
    monkeypatch.setattr(curl_requests, 'head', blocked)
    assert dns_fallback.probe() == 'https://1.1.1.1/dns-query'
    h = handler([])
    h._send('req')
    assert h.used == ['https://1.1.1.1/dns-query']


def test_probe_keeps_plain_dns_when_it_works(handler, monkeypatch):
    from curl_cffi import requests as curl_requests
    monkeypatch.setattr(curl_requests, 'head', lambda *a, **k: None)
    assert dns_fallback.probe() is None
