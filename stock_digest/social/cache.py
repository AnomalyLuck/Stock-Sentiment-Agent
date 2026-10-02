"""In-memory TTL cache used for article lists, summaries, and ticker lookups."""

import time
from collections.abc import Callable
from typing import Any

# Expired entries are only noticed when their own key is read again, so a cache
# left to itself keeps every key nobody happens to query twice. Sweep the whole
# store at most this often (seconds) to bound that work on the write path.
_SWEEP_INTERVAL = 60.0


class TTLCache:
    """Cache values until their time-to-live (TTL) runs out.

    When full, remove the least recently used (LRU) entry. A TTL of None means
    no expiration, but the size limit still applies. Periodic sweeps remove
    expired entries even if nobody reads them again. Tests can supply a clock
    so they can advance time without waiting.
    """

    def __init__(
        self,
        maxsize: int = 1000,
        sweep_interval: float = _SWEEP_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Insertion order is the LRU order: oldest use first, newest last.
        self._store: dict[Any, tuple[Any, float | None]] = {}
        self._maxsize = maxsize
        self._sweep_interval = sweep_interval
        self._clock = clock
        self._next_sweep = clock() + sweep_interval

    def __len__(self) -> int:
        """Number of entries currently held (expired ones included until swept)."""
        return len(self._store)

    def get(self, key: Any) -> Any | None:
        """Return the cached value for key, or None if absent or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self._clock() > expires_at:
            del self._store[key]
            return None
        # Moving the entry to the end marks it as the most recently used.
        del self._store[key]
        self._store[key] = entry
        return value

    def set(self, key: Any, value: Any, ttl: float | None) -> None:
        """Store value under key, expiring after ttl seconds (None = never)."""
        now = self._clock()
        expires_at = None
        if ttl is not None:
            expires_at = now + ttl

        # Remove an existing entry before inserting it at the end.
        self._store.pop(key, None)
        self._store[key] = (value, expires_at)
        self._evict(now)

    def clear(self) -> None:
        """Drop every entry (tests, and manual invalidation)."""
        self._store.clear()

    def _evict(self, now: float) -> None:
        """Sweep when due, then remove least recently used entries until under cap."""
        if now >= self._next_sweep:
            self._sweep(now)
        while len(self._store) > self._maxsize:
            oldest_key = next(iter(self._store))
            del self._store[oldest_key]

    def _sweep(self, now: float) -> None:
        """Drop every expired entry; O(n), rate-limited to once per interval."""
        self._next_sweep = now + self._sweep_interval
        expired_keys = []
        for key, entry in self._store.items():
            _, expires_at = entry
            if expires_at is not None and now > expires_at:
                expired_keys.append(key)

        # Collect keys first: deleting during dictionary iteration is not allowed.
        for key in expired_keys:
            del self._store[key]


# Shared cache instances (process-local). Sizes are set by how big one entry is,
# not by how many we expect: a Reddit comment tree is ~100 comments, an article
# list is a whole window of news, a ticker or alias entry is a couple of strings.
article_list_cache = TTLCache(maxsize=200)  # key: (ticker, window)
short_summary_cache = TTLCache(maxsize=5000)  # key: article url
ticker_cache = TTLCache(maxsize=2000)  # key: normalized query
social_cache = TTLCache(maxsize=200)  # key: (ticker, window, sources)
# keys: ("reddit-discovery", ...) / ("reddit-tree", ...)
reddit_cache = TTLCache(maxsize=300)
# key: ("x-top", ticker, window); an entry is one ranked page or two of tweets.
# Every X call bills, so this is what stops a Social-tab reopen from paying twice.
x_cache = TTLCache(maxsize=200)
alias_cache = TTLCache(maxsize=2000)  # key: ticker; LLM-derived names, no expiry
