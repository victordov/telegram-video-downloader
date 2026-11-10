"""
Notice when a site stops working.

A single failed download usually means a private or deleted post. Several in
a row from the same site usually means the site changed and yt-dlp needs an
update. FailureMonitor turns that pattern into one alert for the admins (and
one "working again" note once a download succeeds), with a cooldown so a
broken site cannot flood their chat.
"""

import threading
import time
from typing import Callable, Dict, Optional

#: Error codes that point at the bot or the site rather than at the link.
#: Private, deleted, too large, live and similar outcomes are the user's link
#: being what it is, so they never count towards an alert.
BREAKAGE_CODES = frozenset({
    'download_failed', 'unsupported_by_extractor', 'rate_limited', 'unexpected',
})


class FailureMonitor:
    def __init__(self, threshold: int, cooldown_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold = max(1, threshold)
        self.cooldown = cooldown_seconds
        self._clock = clock
        self._streak: Dict[str, int] = {}
        self._last_alert: Dict[str, float] = {}
        self._alerted: set = set()
        self._lock = threading.Lock()

    def record_failure(self, platform: str, code: str, detail: str = '') -> Optional[str]:
        """Count a failure. Returns alert text when the admins should hear about it."""
        if code not in BREAKAGE_CODES:
            return None
        with self._lock:
            streak = self._streak.get(platform, 0) + 1
            self._streak[platform] = streak
            if streak < self.threshold:
                return None
            now = self._clock()
            last = self._last_alert.get(platform)
            if last is not None and now - last < self.cooldown:
                return None
            self._last_alert[platform] = now
            self._alerted.add(platform)
        detail = (detail or '').strip()
        return (
            f"{streak} downloads in a row from {platform} have failed."
            + (f"\nLast error: {detail[:300]}" if detail else '')
        )

    def record_success(self, platform: str) -> Optional[str]:
        """Reset the streak. Returns recovery text if this site had been alerted."""
        with self._lock:
            self._streak.pop(platform, None)
            if platform not in self._alerted:
                return None
            self._alerted.discard(platform)
        return f"Downloads from {platform} are working again."

    def streak(self, platform: str) -> int:
        with self._lock:
            return self._streak.get(platform, 0)
