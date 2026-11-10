"""
DNS fallback for yt-dlp's browser-impersonation requests (TikTok and others).

yt-dlp impersonates a browser through curl_cffi, whose bundled libcurl resolves
host names with c-ares: it sends plain DNS queries to the servers listed in
/etc/resolv.conf instead of asking the operating system. Some networks and VPNs
(Tailscale, corporate VPNs, captive portals) block plain DNS and only answer
the system resolver, so every impersonated request fails with "Could not
resolve host" or "Resolving timed out" while the rest of the Mac works fine.

install() wraps yt-dlp's curl_cffi handler: the first time a request fails to
resolve a host, it retries over DNS-over-HTTPS and keeps doing so for the rest
of the run. If DoH stops working later (the network changed again), it switches
back to plain DNS. On a network where plain DNS works nothing changes.
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {'doh_url': None, 'fallback': None}


def is_resolve_error(exc: BaseException) -> bool:
    """True when a yt-dlp TransportError was caused by a failed DNS lookup."""
    cause = getattr(exc, 'cause', None) or exc.__cause__
    text = str(cause or exc).lower()
    return 'could not resolve host' in text or 'resolving timed out' in text


def active_doh_url() -> Optional[str]:
    """The DoH server currently in use, or None while plain DNS is used."""
    return _state['doh_url']


def probe(host: str = 'www.google.com', timeout: float = 5.0) -> Optional[str]:
    """Check at startup whether curl_cffi's own DNS works; switch to DoH now if not.

    Saves the first impersonated download after a restart from waiting out a
    20-second DNS timeout. Returns the resolver in use afterwards.
    """
    if not _state['fallback'] or _state['doh_url']:
        return _state['doh_url']
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    try:
        curl_requests.head(f'https://{host}/', timeout=timeout, impersonate='chrome')
    except Exception as exc:  # noqa: BLE001 - any failure here is just a probe result
        if is_resolve_error(exc):
            with _lock:
                if not _state['doh_url']:
                    _state['doh_url'] = _state['fallback']
                    logger.warning("Plain DNS is blocked on this network; using "
                                   "DNS-over-HTTPS (%s) for yt-dlp", _state['doh_url'])
        else:
            logger.debug("DNS probe failed for another reason: %s", exc)
    return _state['doh_url']


def install(doh_url: Optional[str]) -> bool:
    """Patch yt-dlp's curl_cffi handler. Returns True when the patch is active."""
    if not doh_url:
        return False
    try:
        from yt_dlp.networking import _curlcffi
        from yt_dlp.networking.exceptions import TransportError
    except ImportError:  # curl_cffi not installed: yt-dlp uses requests/urllib
        return False
    handler = getattr(_curlcffi, 'CurlCFFIRH', None)
    if handler is None:
        return False

    with _lock:
        _state['fallback'] = doh_url
        if getattr(handler, '_dns_fallback_installed', False):
            return True

        original_create = handler._create_instance
        original_send = handler._send

        def _create_instance(self, *args, **kwargs):
            session = original_create(self, *args, **kwargs)
            session.doh_url = _state['doh_url']
            return session

        def _send(self, request):
            used = _state['doh_url']
            try:
                return original_send(self, request)
            except TransportError as exc:
                if not is_resolve_error(exc):
                    raise
                with _lock:
                    if _state['doh_url'] == used:  # nobody switched meanwhile
                        _state['doh_url'] = None if used else _state['fallback']
                        if _state['doh_url']:
                            logger.warning(
                                "Plain DNS is blocked on this network (%s); "
                                "using DNS-over-HTTPS (%s) for yt-dlp", exc, _state['doh_url'])
                        else:
                            logger.warning("DNS-over-HTTPS stopped working (%s); "
                                           "back to plain DNS", exc)
                self._clear_instances()  # cached sessions carry the old resolver
                return original_send(self, request)

        handler._create_instance = _create_instance
        handler._send = _send
        handler._dns_fallback_installed = True
    return True
