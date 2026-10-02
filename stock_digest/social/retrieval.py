"""The pieces of Sentiment-Search `app/retrieval.py` the social sources use.

Only the window table and URL canonicalization are ported; Finnhub news and ticker
resolution are not (this app resolves tickers with Yahoo Finance, see social.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Time-window keys -> lookback duration. "48h" is the Social Sentiment tab's fixed
# window; the others stay because x_source.window_label maps a span to its nearest key.
WINDOWS: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
    "48h": timedelta(hours=48),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "2w": timedelta(weeks=2),
    "1mo": timedelta(days=30),
    "3mo": timedelta(days=90),
}


def window_cutoff(window: str, now: datetime | None = None) -> datetime:
    """Return the UTC datetime cutoff for a window key."""
    now = now or datetime.now(timezone.utc)
    return now - WINDOWS[window]


_TRACKING_PARAMS: set[str] = {"fbclid", "gclid", "msclkid", "ref", "src", "cmpid"}


def canon_url(u: str) -> str:
    """Canonicalize a URL for exact-dup detection (strip fragment, tracking params,
    trailing slash) while preserving identity-bearing query params like ?id=."""
    parts = urlsplit(u.strip())
    kept_parameters = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        normalized_name = name.lower()
        if normalized_name in _TRACKING_PARAMS:
            continue
        if normalized_name.startswith("utm_"):
            continue
        kept_parameters.append((name, value))
    query = urlencode(kept_parameters)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))
