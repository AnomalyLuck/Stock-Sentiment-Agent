"""Small persistent cache of successful material-news work, never whole digests."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import time

PROFILE_TTL = 7 * 24 * 3600
EVENT_TTL = 30 * 60
MAX_ENTRIES = 1000
CACHE_VERSION = 1


def fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class MaterialCache:
    """Per-operation SQLite connections support concurrent UI and CLI runs.

    Cache failures are misses, not research failures. Reads never extend the TTL.
    Only public evidence/model outputs are stored; no credentials or requests.
    """

    def __init__(self):
        directory = os.environ.get("STOCK_DIGEST_CACHE_DIR", "").strip()
        self.path = None if directory.lower() == "off" else (
            Path(directory).expanduser() if directory else Path.home() / ".cache" / "stock-digest"
        ) / "material.sqlite3"
        self.error: str | None = None

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS cache "
                               "(key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL)")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def get(self, key: str) -> dict | None:
        if self.path is None:
            return None
        try:
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT value FROM cache WHERE key = ? AND expires > ?",
                                         (key, time.time())).fetchone()
            value = json.loads(row[0]) if row else None
            return value if isinstance(value, dict) else None
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.error = type(exc).__name__
            return None

    def put(self, key: str, value: dict, ttl: float) -> None:
        if self.path is None:
            return
        try:
            now = time.time()
            encoded = json.dumps(value, ensure_ascii=False)
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM cache WHERE expires <= ?", (now,))
                connection.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)",
                                   (key, encoded, now + ttl, now))
                connection.execute("DELETE FROM cache WHERE key IN "
                                   "(SELECT key FROM cache ORDER BY created DESC, key LIMIT -1 OFFSET ?)",
                                   (MAX_ENTRIES,))
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.error = type(exc).__name__
