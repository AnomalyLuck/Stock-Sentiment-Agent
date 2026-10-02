"""Social post retrieval: pluggable per-platform sources, a fixed window, ordering.

Ported from Sentiment-Search `app/social.py` and the `/api/social` handler in
`app/main.py`. Each platform lives in its own `*_source.py` module (copied unchanged)
and exposes one adapter class with `name`, `enabled` and `fetch(ticker, company,
since)`; this module owns what they share. Port changes, all made here rather than
in the adapters:

- Tickers resolve through Yahoo Finance (this app's resolver), not Finnhub.
- Each retrieval fixes one UTC interval [now - 48h, now]; every post is checked
  against both ends on publication time, and anything outside is counted, not shown.
- Duplicates are removed by provider and post id (parsed from the post URL), never by
  text similarity, so two people posting the same words stay two posts.
- The merged top-150 ranking and per-source caps are not applied: every post the
  adapters return is listed, newest first. Limits inside the adapters (X's top 15,
  Reddit's top 90, one page per keyless source) are reported per provider instead.
- No whole-response cache, so Refresh fetches a new window. The X and Reddit caches
  inside the adapters still apply; they are cost controls.
- Everything runs on one background event loop, so the adapters' module-level caches
  and budgets are only touched from one thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import (
    config,
    hackernews_source,
    reddit_source,
    seeking_alpha_source,
    stocktwits_source,
    x_source,
    youtube_source,
)
from .cache import ticker_cache
from .retrieval import WINDOWS, canon_url
from .security import provider_error_message, redact_credentials
from .social_types import SocialPost, SourceError

logger = logging.getLogger(__name__)

SOCIAL_WINDOW = "48h"
SOURCE_TIMEOUT = 8.0  # seconds; a slow source never blocks the response
REQUEST_TIMEOUT = 90.0  # resolution + alias lookup + the slowest source (Reddit, 30 s)
_TICKER_TTL = 86400


# --- Orchestration ---------------------------------------------------------------


def all_sources() -> list:
    """Instantiate every adapter; enablement is decided per-adapter from config."""
    return [
        reddit_source.RedditCommentSource(),
        stocktwits_source.StockTwitsSource(),
        hackernews_source.HackerNewsSource(),
        youtube_source.YouTubeSource(),
        x_source.XSource(),
        seeking_alpha_source.SeekingAlphaSource(),
    ]


@dataclass
class SocialResult:
    """One retrieval: the resolved ticker, its fixed interval, posts and per-source status."""

    ticker: str
    company: str
    window_start: datetime  # UTC, inclusive
    window_end: datetime  # UTC, inclusive; the retrieval time
    posts: list[SocialPost]  # newest first
    status: dict[str, str]  # name -> "ok" | "ok (note)" | "not configured" | "unavailable: ..."
    returned: dict[str, int]  # name -> posts the adapter returned
    shown: dict[str, int]  # name -> posts kept after window checks and dedupe
    excluded: dict[str, int] = field(default_factory=dict)  # reason -> count


def fixed_interval(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The retrieval's [start, end] in UTC: end is now, start exactly 48 hours earlier."""
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return end - WINDOWS[SOCIAL_WINDOW], end


def window_check(post: SocialPost, since: datetime, until: datetime) -> str | None:
    """Why a post falls outside [since, until] by publication time; None when inside."""
    published = post.published_at
    if not isinstance(published, datetime) or published.tzinfo is None:
        return "missing or invalid publication time"
    if published < since:
        return "published before the window"
    if published > until:
        return "published after the retrieval time"
    return None


_ID_PATTERNS = {
    "x": ("path", re.compile(r"/status/(\d+)")),
    "stocktwits": ("path", re.compile(r"/message/(\d+)")),
    "reddit": ("path", re.compile(r"/comments/[^/]+/[^/]*/([^/]+)")),
    "hackernews": ("query", "id"),
    "youtube": ("query", "v"),
}


def post_id(post: SocialPost) -> str | None:
    """The provider's post id, read from the canonical URL each adapter builds."""
    rule = _ID_PATTERNS.get(post.type)
    if not rule or not post.url:
        return None
    parts = urlsplit(post.url)
    kind, pattern = rule
    if kind == "query":
        values = parse_qs(parts.query).get(pattern) or [""]
        return values[0] or None
    match = pattern.search(parts.path)
    return match.group(1) if match else None


def dedupe_key(post: SocialPost) -> tuple:
    """Provider + id; else provider + canonical URL; else provider + author + time + text."""
    identifier = post_id(post)
    if identifier:
        return (post.type, "id", identifier)
    if post.url:
        return (post.type, "url", canon_url(post.url))
    return (post.type, "post", post.author, post.published_at.isoformat(), post.text or post.title)


def dedupe_by_id(posts: list[SocialPost]) -> tuple[list[SocialPost], int]:
    """Drop repeat records of the same provider post; returns (kept, dropped)."""
    seen: set[tuple] = set()
    kept: list[SocialPost] = []
    for post in posts:
        key = dedupe_key(post)
        if key in seen:
            continue
        seen.add(key)
        kept.append(post)
    return kept, len(posts) - len(kept)


def select_posts(
    batches: dict[str, list[SocialPost]], since: datetime, until: datetime
) -> tuple[list[SocialPost], dict[str, int], dict[str, int]]:
    """Window-check, dedupe and order every adapter's posts, newest first.

    Returns (posts, shown per source, excluded per reason).
    """
    excluded: dict[str, int] = {}
    inside: list[SocialPost] = []
    for posts in batches.values():
        for post in posts:
            reason = window_check(post, since, until)
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
            else:
                inside.append(post)
    unique, duplicates = dedupe_by_id(inside)
    if duplicates:
        excluded["duplicate of another record"] = duplicates
    shown: dict[str, int] = {name: 0 for name in batches}
    for post in unique:
        shown[post.type] = shown.get(post.type, 0) + 1
    ordered = sorted(unique, key=lambda post: (post.published_at, post.type, post_id(post) or ""), reverse=True)
    return ordered, shown, excluded


async def resolve(query: str) -> tuple[str, str]:
    """Resolve a ticker with Yahoo Finance to (display ticker, company); cached a day."""
    from ..material.identity import parse_symbol, resolve_issuer

    parse_symbol(query)  # malformed input raises InputError before any network call
    key = query.strip().upper()
    cached = ticker_cache.get(key)
    if cached is not None:
        return cached
    issuer = await asyncio.to_thread(resolve_issuer, query)
    result = (issuer.ticker, issuer.company)
    ticker_cache.set(key, result, ttl=_TICKER_TTL)
    return result


async def fetch_social(
    query: str,
    only: frozenset[str] | None = None,
    now: datetime | None = None,
) -> SocialResult:
    """Resolve the ticker, run every selected source on one fixed window, and merge.

    `only` restricts which sources run (by adapter name), as in the source branch.
    Status maps each selected source name to "ok" | "not configured" |
    "unavailable: ..." exactly as the branch reports it.
    """
    symbol, company = await resolve(query)
    since, until = fixed_interval(now)
    sources = all_sources()
    if only is not None:
        sources = [source for source in sources if source.name in only]
    status: dict[str, str] = {}

    async def fetch_source(source) -> list[SocialPost]:
        if not source.enabled:
            status[source.name] = "not configured"
            return []
        try:
            timeout = getattr(source, "timeout", SOURCE_TIMEOUT)
            posts = await asyncio.wait_for(
                source.fetch(symbol, company, since),
                timeout,
            )
            # A source can qualify its own success (X: "newest — too recent to
            # rank") so the UI can say so instead of pretending.
            note = getattr(source, "status_note", None)
            if note:
                status[source.name] = f"ok ({note})"
            else:
                status[source.name] = "ok"
            return posts
        except Exception as exc:  # noqa: BLE001 - isolate failures in each source
            logger.warning("Social source %s failed: %s", source.name, exc)
            # SourceError carries our own actionable quota/configuration text.
            # Never forward arbitrary upstream exception messages to clients.
            if isinstance(exc, SourceError) and str(exc):
                safe_message = redact_credentials(str(exc))
                reason = safe_message.splitlines()[0][:120]
            else:
                reason = provider_error_message(exc)
            status[source.name] = f"unavailable: {reason}"
            return []

    results = await asyncio.gather(*(fetch_source(source) for source in sources))
    batches = {source.name: posts for source, posts in zip(sources, results)}
    posts, shown, excluded = select_posts(batches, since, until)
    return SocialResult(
        ticker=symbol, company=company, window_start=since, window_end=until, posts=posts,
        status=status, returned={name: len(batch) for name, batch in batches.items()},
        shown=shown, excluded=excluded,
    )


# --- One event loop for every request -----------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _event_loop() -> asyncio.AbstractEventLoop:
    """Start (once) the daemon thread whose loop runs every social retrieval."""
    global _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="social-loop", daemon=True).start()
            _loop = loop
    return _loop


def run_blocking(query: str, only: frozenset[str] | None = None) -> SocialResult:
    """Run `fetch_social` on the shared loop from a request thread and wait for it."""
    future = asyncio.run_coroutine_threadsafe(fetch_social(query, only), _event_loop())
    try:
        return future.result(timeout=REQUEST_TIMEOUT)
    except TimeoutError:
        future.cancel()
        raise


# --- Response shaping ---------------------------------------------------------------

SOURCE_LABELS = {
    "x": "X", "stocktwits": "StockTwits", "reddit": "Reddit", "hackernews": "Hacker News",
    "youtube": "YouTube", "seeking_alpha": "Seeking Alpha",
}
REQUIRED_SETTINGS = {
    "x": "X_API_PROVIDER=twitterapi and X_API_KEY", "reddit": "REDDIT_API_KEY",
    "youtube": "YOUTUBE_API_KEY",  # Seeking Alpha has no provider to configure yet
}


def source_limits(name: str, returned: int) -> tuple[str, bool]:
    """What the branch's adapter for `name` can return, and whether a cap was hit.

    Built from the adapters' own constants so the text follows their configuration.
    """
    if name == "x":
        text = (f"Top {x_source.X_DISPLAY_CAP} posts at most (no more than {x_source.X_MAX_PER_AUTHOR} per account), "
                f"chosen by the branch's ranking from up to {config.X_MAX_PAGES} pages of X's Top search via "
                f"twitterapi.io. Replies, reposts, non-English posts, accounts under {config.X_MIN_FOLLOWERS} "
                "followers, short posts, cashtag lists and promotions are excluded. "
                f"Results are reused for {config.X_CACHE_TTL // 60} minutes to limit cost.")
        return text, returned >= x_source.X_DISPLAY_CAP
    if name == "reddit":
        text = (f"At most {reddit_source.REDDIT_TOP_COMMENTS} comments ({reddit_source._MAX_COMMENTS_PER_THREAD} per thread), "
                f"chosen by the branch's ranking from up to {config.REDDIT_MAX_COMMENT_TREES_PER_QUERY} threads in "
                f"{len(reddit_source.DEFAULT_SUBREDDITS)} investing subreddits. Comment text is a "
                f"{reddit_source._EXCERPT_CHARS}-character excerpt; open the link for the full comment. "
                f"Threads are reused for up to {config.REDDIT_CACHE_TTL // 60} minutes.")
        return text, returned >= reddit_source.REDDIT_TOP_COMMENTS
    if name == "stocktwits":
        return ("Only the newest page of the public stream (up to 30 messages) is read; "
                "older messages in the window are not retrieved."), returned >= 30
    if name == "hackernews":
        return "Up to 100 newest stories and comments that mention the company name.", returned >= 100
    if name == "youtube":
        return (f"Up to 50 most-viewed videos from one search page; English only, with at least "
                f"{youtube_source.YOUTUBE_MIN_VIEWS:,} views and {youtube_source.YOUTUBE_MIN_COMMENTS} comments. "
                "The text is the video description."), returned >= 50
    return "No provider integration is implemented yet.", False


def engagement(post: SocialPost) -> list[dict]:
    """Labelled counts as the branch's adapter produced them.

    X and StockTwits fold reposts into `likes`, so the labels say so. Hacker News
    and YouTube turn an unreported count into 0, so a 0 there is left out rather
    than shown as a real zero.
    """
    labels = {
        "x": ("likes + reposts + quotes", "replies"),
        "stocktwits": ("likes + reshares", "replies"),
        "reddit": ("points", None),
        "hackernews": ("points", "comments"),
        "youtube": ("likes", "comments"),
    }.get(post.type, ("likes", "comments"))
    counts = []
    for label, value in zip(labels, (post.likes, post.comments)):
        if label is None:
            continue
        if value == 0 and post.type in ("hackernews", "youtube"):
            continue
        counts.append({"label": label, "value": value})
    if post.views:
        counts.append({"label": "views", "value": post.views})
    return counts


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def post_response(post: SocialPost) -> dict:
    """The branch's JSON fields for a post, plus id, sentiment and labelled engagement."""
    comments = []
    for comment in post.top_comments:
        comments.append(
            {
                "author": comment.author,
                "text": comment.text,
                "likes": comment.likes,
                "published_at": _iso(comment.published_at),
            }
        )

    return {
        "id": post_id(post),
        "type": post.type,
        "title": post.title,
        "text": post.text,
        "url": post.url,
        "source": post.source,
        "author": post.author,
        "published_at": _iso(post.published_at),
        "likes": post.likes,
        "comments": post.comments,
        "views": post.views,
        "context": post.context,
        "is_megathread": post.is_megathread,
        "author_flair": post.author_flair,
        "top_comments": comments,
        # None of the branch's adapters supplies a sentiment label or score.
        "sentiment": None,
        "engagement": engagement(post),
    }


def social_response(query: str, result: SocialResult) -> dict:
    """The `/api/social` body: the branch's fields plus the window and per-source coverage."""
    providers = []
    order = list(SOURCE_LABELS)
    for name, state in sorted(result.status.items(), key=lambda item: order.index(item[0]) if item[0] in order else len(order)):
        kind = "ok" if state.startswith("ok") else "not_configured" if state == "not configured" else "unavailable"
        limits, capped = source_limits(name, result.returned.get(name, 0))
        message = state[len("unavailable: "):] if kind == "unavailable" else None
        if kind == "not_configured" and name in REQUIRED_SETTINGS:
            message = f"Set {REQUIRED_SETTINGS[name]} in .env and restart."
        elif kind == "ok" and state != "ok":
            message = state[len("ok ("):-1]
        providers.append({
            "name": name, "label": SOURCE_LABELS.get(name, name), "state": kind, "message": message,
            "returned": result.returned.get(name, 0), "shown": result.shown.get(name, 0),
            "limits": limits, "capped": kind == "ok" and capped,
        })
    return {
        "query": query,
        "ticker": result.ticker,
        "company": result.company,
        "window": SOCIAL_WINDOW,
        "window_start": _iso(result.window_start),
        "window_end": _iso(result.window_end),
        "retrieved_at": _iso(result.window_end),
        "sources": result.status,
        "providers": providers,
        "excluded": result.excluded,
        # The adapters read a bounded slice of each platform, so coverage is never
        # exhaustive; the page says which limits applied.
        "complete": False,
        "posts": [post_response(post) for post in result.posts],
    }


def parse_sources(sources: str | None) -> frozenset[str] | None:
    """Convert a comma-separated source list into unique, trimmed names."""
    if not sources:
        return None
    names = frozenset(name.strip() for name in sources.split(",") if name.strip())
    return names or None

