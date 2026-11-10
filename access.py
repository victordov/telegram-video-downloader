"""
Who may use the bot, and how often.

The repository is public, so anyone can find the bot and point it at
arbitrary links, spending the host's bandwidth and IP reputation. The policy
here keeps it to the people the owner chose, and the rate limiter stops one
of them (or a busy group) from monopolising it.
"""

import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple


def _ids(values: Iterable) -> frozenset:
    return frozenset(str(value).strip() for value in values if str(value).strip())


class AccessPolicy:
    """Allow list over chat IDs and user IDs.

    An empty ALLOWED_CHAT_IDS means the bot is open to everyone (the
    historical behaviour). Privileged and admin IDs are always allowed, and
    ``granted`` holds the chats an admin approved from Telegram, which are
    kept in the database rather than in .env.
    """

    def __init__(self, allowed: Iterable = (), privileged: Iterable = (), admins: Iterable = (),
                 granted: Iterable = ()):
        self.allowed = _ids(allowed)
        self.privileged = _ids(privileged)
        self.admins = _ids(admins)
        self.granted = set(_ids(granted))

    @property
    def is_open(self) -> bool:
        return not self.allowed

    @property
    def group_ids(self) -> List[str]:
        """Allowed groups (negative IDs): their members may use the bot privately too."""
        return sorted(chat for chat in self.allowed | self.granted if chat.startswith('-'))

    def grant(self, chat_id) -> None:
        self.granted.add(str(chat_id))

    def revoke(self, chat_id) -> None:
        self.granted.discard(str(chat_id))

    def is_allowed(self, chat_id: Optional[int], user_id: Optional[int]) -> bool:
        if self.is_open:
            return True
        candidates = {str(value) for value in (chat_id, user_id) if value is not None}
        return bool(candidates & (self.allowed | self.granted | self.privileged | self.admins))

    def is_admin(self, user_id: Optional[int]) -> bool:
        # A private chat's ID is the user's ID, so admin chat IDs double as user IDs.
        return user_id is not None and str(user_id) in self.admins

    def is_exempt_from_limits(self, chat_id: Optional[int], user_id: Optional[int]) -> bool:
        candidates = {str(value) for value in (chat_id, user_id) if value is not None}
        return bool(candidates & (self.privileged | self.admins))


class RateLimiter:
    """Sliding-window limit of ``limit`` events per ``window`` seconds per key."""

    def __init__(self, limit: int, window: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def try_acquire(self, key) -> Tuple[bool, float]:
        """Record one event for ``key`` if allowed.

        Returns (allowed, seconds_until_a_slot_frees_up).
        """
        if not self.enabled:
            return True, 0.0
        key = str(key)
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False, max(0.0, self.window - (now - events[0]))
            events.append(now)
            # Forget idle keys so the table cannot grow without bound.
            if len(self._events) > 10000:
                for stale in [k for k, v in self._events.items() if not v]:
                    del self._events[stale]
            return True, 0.0

    def refund(self, key) -> None:
        """Give back the most recent slot (e.g. the download never started)."""
        with self._lock:
            events = self._events.get(str(key))
            if events:
                events.pop()
