"""
Small SQLite store for the state that should survive a restart.

- media cache:  link -> Telegram file IDs, so a repeated link is resent
                instantly without downloading or uploading anything
- statistics:   lifetime per-site counters behind /status
- link tokens:  short IDs standing in for long URLs in buttons and deep links
                (Telegram caps callback data at 64 bytes)
- meta:         small key/value notes, e.g. "yt-dlp was just updated"
- chat access:  chats the owner approved (or chose to ignore) from Telegram

Everything goes through one connection guarded by a lock. The queries are
tiny, so calling them straight from the event loop is fine.
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_cache (
    key         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_stats (
    platform        TEXT PRIMARY KEY,
    succeeded       INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    bytes_sent      INTEGER NOT NULL DEFAULT 0,
    last_success_at REAL,
    last_failure_at REAL,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS link_tokens (
    token       TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_access (
    chat_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    updated_at  REAL NOT NULL
);
"""


class Storage:
    """Thread-safe wrapper around a single SQLite database file."""

    def __init__(self, path: str):
        self.path = path
        if path != ':memory:':
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # Autocommit: every statement is its own transaction.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ':memory:':
                self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.executescript(_SCHEMA)
        if self.meta_get('first_started_at') is None:
            self.meta_set('first_started_at', str(time.time()))

    def _execute(self, sql: str, params: Tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # -- media cache -----------------------------------------------------

    def cache_get(self, key: str, max_age_seconds: Optional[float] = None) -> Optional[Dict[str, Any]]:
        row = self._execute(
            'SELECT payload, created_at FROM media_cache WHERE key = ?', (key,)
        ).fetchone()
        if row is None:
            return None
        if max_age_seconds is not None and time.time() - row['created_at'] > max_age_seconds:
            self.cache_delete(key)
            return None
        try:
            return json.loads(row['payload'])
        except ValueError:
            self.cache_delete(key)
            return None

    def cache_put(self, key: str, payload: Dict[str, Any]) -> None:
        self._execute(
            'INSERT OR REPLACE INTO media_cache (key, payload, created_at) VALUES (?, ?, ?)',
            (key, json.dumps(payload), time.time()),
        )

    def cache_delete(self, key: str) -> None:
        self._execute('DELETE FROM media_cache WHERE key = ?', (key,))

    def cache_count(self) -> int:
        return self._execute('SELECT COUNT(*) FROM media_cache').fetchone()[0]

    # -- statistics ------------------------------------------------------

    def record_success(self, platform: str, size: int) -> None:
        self._execute(
            'INSERT INTO platform_stats (platform, succeeded, bytes_sent, last_success_at) '
            'VALUES (?, 1, ?, ?) '
            'ON CONFLICT(platform) DO UPDATE SET succeeded = succeeded + 1, '
            'bytes_sent = bytes_sent + excluded.bytes_sent, last_success_at = excluded.last_success_at',
            (platform, max(0, int(size or 0)), time.time()),
        )

    def record_failure(self, platform: str, error: Optional[str] = None) -> None:
        self._execute(
            'INSERT INTO platform_stats (platform, failed, last_failure_at, last_error) '
            'VALUES (?, 1, ?, ?) '
            'ON CONFLICT(platform) DO UPDATE SET failed = failed + 1, '
            'last_failure_at = excluded.last_failure_at, last_error = excluded.last_error',
            (platform, time.time(), (error or '')[:300] or None),
        )

    def platform_stats(self) -> Dict[str, Dict[str, Any]]:
        rows = self._execute('SELECT * FROM platform_stats').fetchall()
        return {row['platform']: dict(row) for row in rows}

    def totals(self) -> Tuple[int, int, int]:
        """(succeeded, failed, bytes_sent) over the bot's whole life."""
        row = self._execute(
            'SELECT COALESCE(SUM(succeeded), 0), COALESCE(SUM(failed), 0), '
            'COALESCE(SUM(bytes_sent), 0) FROM platform_stats'
        ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    @property
    def first_started_at(self) -> float:
        try:
            return float(self.meta_get('first_started_at') or time.time())
        except ValueError:
            return time.time()

    # -- link tokens -----------------------------------------------------

    def token_for(self, url: str) -> str:
        """A short, stable token for a URL (safe for callback data and /start)."""
        token = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
        self._execute(
            'INSERT OR IGNORE INTO link_tokens (token, url, created_at) VALUES (?, ?, ?)',
            (token, url, time.time()),
        )
        return token

    def url_for(self, token: str) -> Optional[str]:
        row = self._execute('SELECT url FROM link_tokens WHERE token = ?', (token,)).fetchone()
        return row['url'] if row else None

    # -- meta ------------------------------------------------------------

    def meta_get(self, key: str) -> Optional[str]:
        row = self._execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self._execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))

    def meta_pop(self, key: str) -> Optional[str]:
        value = self.meta_get(key)
        if value is not None:
            self._execute('DELETE FROM meta WHERE key = ?', (key,))
        return value

    # -- chat access -----------------------------------------------------

    ACCESS_ALLOWED = 'allowed'
    ACCESS_IGNORED = 'ignored'

    def access_set(self, chat_id, status: str, title: str = '') -> None:
        self._execute(
            'INSERT OR REPLACE INTO chat_access (chat_id, status, title, updated_at) '
            'VALUES (?, ?, ?, ?)',
            (str(chat_id), status, title or '', time.time()),
        )

    def access_status(self, chat_id) -> Optional[str]:
        row = self._execute('SELECT status FROM chat_access WHERE chat_id = ?',
                            (str(chat_id),)).fetchone()
        return row['status'] if row else None

    def access_entries(self, status: Optional[str] = None) -> Dict[str, Dict[str, str]]:
        """chat_id -> {'status', 'title'}, oldest decision first."""
        sql = 'SELECT chat_id, status, title FROM chat_access'
        params: Tuple = ()
        if status is not None:
            sql += ' WHERE status = ?'
            params = (status,)
        rows = self._execute(sql + ' ORDER BY updated_at', params).fetchall()
        return {row['chat_id']: {'status': row['status'], 'title': row['title']} for row in rows}

    def access_remove(self, chat_id) -> bool:
        return self._execute('DELETE FROM chat_access WHERE chat_id = ?',
                             (str(chat_id),)).rowcount > 0

    # -- housekeeping ----------------------------------------------------

    def prune(self, cache_max_age_seconds: Optional[float],
              token_max_age_seconds: float = 90 * 86400) -> int:
        """Drop expired cache entries and old button tokens. Returns rows removed."""
        removed = 0
        now = time.time()
        if cache_max_age_seconds is not None:
            removed += self._execute(
                'DELETE FROM media_cache WHERE created_at < ?', (now - cache_max_age_seconds,)
            ).rowcount
        removed += self._execute(
            'DELETE FROM link_tokens WHERE created_at < ?', (now - token_max_age_seconds,)
        ).rowcount
        return removed
