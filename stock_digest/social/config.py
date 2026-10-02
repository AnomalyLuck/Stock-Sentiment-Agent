"""Social-source configuration, ported from Sentiment-Search `app/config.py`.

Values are read once at import from the environment, after merging the current
directory's .env as data (no shell execution or interpolation; exported variables
win), the same way `runner.load_settings` reads the digest settings. Only the names
below are taken from .env. Every key stays on the server: none is sent to the page.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

NAMES = (
    "OPENAI_API_KEY", "OPENAI_VERIFY_MODEL", "SOCIAL_ALIAS_MODEL",
    "REDDIT_API_KEY", "REDDIT_API_BASE", "REDDIT_CACHE_TTL", "REDDIT_MEGATHREAD_CACHE_TTL",
    "REDDIT_MAX_SUBREDDITS_PER_QUERY", "REDDIT_MAX_COMMENT_TREES_PER_QUERY", "REDDIT_DAILY_CALL_BUDGET",
    "X_API_PROVIDER", "X_API_KEY", "X_API_BASE", "X_MAX_PAGES", "X_DAILY_TWEET_BUDGET",
    "X_PAGE_INTERVAL_S", "X_MIN_FOLLOWERS", "X_CACHE_TTL",
    "YOUTUBE_API_KEY", "SEEKING_ALPHA_API_KEY",
)

_local = dotenv_values(Path.cwd() / ".env", interpolate=False)
for _name in NAMES:
    if _local.get(_name) is not None:
        os.environ.setdefault(_name, _local[_name])

# Company-alias lookup (aliases.py) uses this app's OpenAI key; without it, the
# Yahoo company name plus static seeds are used. Model defaults to the reviewer model.
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
ALIAS_MODEL: str = (os.getenv("SOCIAL_ALIAS_MODEL") or os.getenv("OPENAI_VERIFY_MODEL") or "gpt-4.1-mini").strip()

# Social sources (all optional; a missing key disables that source, no error).
# Reddit comments come from redditapis.com ($0.002/read) — see
# Sentiment-Search tests/fixtures/reddit-api-contract.md for the verified contract.
REDDIT_API_KEY: str = os.getenv("REDDIT_API_KEY", "")
REDDIT_API_BASE: str = os.getenv("REDDIT_API_BASE", "https://api.redditapis.com")
REDDIT_CACHE_TTL: int = int(os.getenv("REDDIT_CACHE_TTL", "900"))
REDDIT_MEGATHREAD_CACHE_TTL: int = int(os.getenv("REDDIT_MEGATHREAD_CACHE_TTL", "300"))
REDDIT_MAX_SUBREDDITS_PER_QUERY: int = int(os.getenv("REDDIT_MAX_SUBREDDITS_PER_QUERY", "13"))
REDDIT_MAX_COMMENT_TREES_PER_QUERY: int = int(os.getenv("REDDIT_MAX_COMMENT_TREES_PER_QUERY", "16"))
REDDIT_DAILY_CALL_BUDGET: int = int(os.getenv("REDDIT_DAILY_CALL_BUDGET", "1000"))
# X posts come from twitterapi.io ($0.15 per 1,000 tweets returned; every call
# bills) — see Sentiment-Search tests/fixtures/x-api-contract.md. "twitterapi" is the only
# supported provider value. Scraper-backed and not licensed by X.
X_API_PROVIDER: str = os.getenv("X_API_PROVIDER", "")
X_API_KEY: str = os.getenv("X_API_KEY", "")
X_API_BASE: str = os.getenv("X_API_BASE", "https://api.twitterapi.io")
X_MAX_PAGES: int = int(os.getenv("X_MAX_PAGES", "2"))  # 20 tweets/page, $0.003/page
# Counted in tweets returned (the billed unit); the default caps a runaway
# loop at ~$0.45/day. The provider dashboard's balance is the real ceiling.
X_DAILY_TWEET_BUDGET: int = int(os.getenv("X_DAILY_TWEET_BUDGET", "3000"))
# Pause between sequential pages. 0 on paid credits; a free-tier key needs 5.5
# (one request per 5 s) with X_MAX_PAGES=1 to fit the social source timeout.
X_PAGE_INTERVAL_S: float = float(os.getenv("X_PAGE_INTERVAL_S", "0"))
X_MIN_FOLLOWERS: int = int(os.getenv("X_MIN_FOLLOWERS", "250"))  # bot floor
X_CACHE_TTL: int = int(os.getenv("X_CACHE_TTL", "900"))
SEEKING_ALPHA_API_KEY: str = os.getenv("SEEKING_ALPHA_API_KEY", "")
YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")
