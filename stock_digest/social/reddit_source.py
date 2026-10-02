"""Reddit comment source via redditapis.com — two-stage retrieval.

Stage 1 (discovery) finds candidate posts: a ticker search per subreddit plus a
privileged megathread track (daily-discussion threads hold most per-ticker
chatter and never match a ticker query in the title). Stage 2 (extraction)
pulls each selected post's comment tree — one billable call per post, however
many comments come back — and greps it for ticker-relevant comments.

Everything above the transport (matching, filtering, ranking, selection) is
client-agnostic and pure; swapping redditapis.com for PRAW should touch only
`RedditApisTransport`. Verified API contract:
tests/fixtures/reddit-api-contract.md.
"""

import asyncio
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config
from .aliases import _core_company_name, names_for_ticker
from .cache import reddit_cache
from .social_types import SocialPost, SourceError

logger = logging.getLogger(__name__)


DEFAULT_SUBREDDITS = [
    "stocks",
    "investing",
    "wallstreetbets",
    "StockMarket",
    "ValueInvesting",
    "SecurityAnalysis",
    "options",
    "thetagang",
    "algotrading",
    "dividends",
    "Bogleheads",
    "Daytrading",
    "ETFs",
]

# Weights are deliberately flat (0.9-1.2) so the sub is a tiebreaker, not the
# ranking. Ticker-discussion subs sit slightly above; mechanics-heavy subs
# (options/thetagang), tooling (algotrading) and index-fund subs (Bogleheads)
# slightly below because ticker mentions there are often incidental.
SUBREDDIT_WEIGHTS = {
    "stocks": 1.2,
    "StockMarket": 1.2,
    "wallstreetbets": 1.1,
    "ValueInvesting": 1.1,
    "SecurityAnalysis": 1.1,
    "investing": 1.0,
    "Daytrading": 1.0,
    "dividends": 1.0,
    "ETFs": 1.0,
    "thetagang": 0.9,
    "options": 0.9,
    "algotrading": 0.9,
    "Bogleheads": 0.9,
}

# Site-wide discovery can surface any subreddit; unknown subs rank below every
# curated one (the lowest curated weight is 0.9).
_DEFAULT_SUBREDDIT_WEIGHT = 0.8

MEGATHREAD_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"daily discussion",
        r"what are your moves",
        r"rate my portfolio",
        r"daily advice",
        r"weekend discussion",
        r"moves tomorrow",
    )
]

# Tickers that are common English words: never accept a bare-symbol match.
STOPWORD_TICKERS = {
    "A",
    "ALL",
    "AN",
    "ANY",
    "ARE",
    "BE",
    "BIG",
    "BY",
    "CAR",
    "CASH",
    "COST",
    "DO",
    "EAT",
    "FOR",
    "GO",
    "GOOD",
    "HAS",
    "IT",
    "KEY",
    "LOVE",
    "NOW",
    "ON",
    "ONE",
    "OPEN",
    "OUT",
    "PLAY",
    "REAL",
    "RUN",
    "SAFE",
    "SEE",
    "SO",
    "TWO",
    "UP",
    "WELL",
}

# Ticker names/aliases are LLM-derived per ticker and shared with the news
# pipeline: see app.aliases.names_for_ticker (imported above).

# Returned globally per query, aggregated across threads.
REDDIT_TOP_COMMENTS = 90
# Thread diversity: a single rich thread can't fill every slot. Live NVDA/3d
# runs match 40-80 comments in a rich thread; 7 keeps the mix broad.
_MAX_COMMENTS_PER_THREAD = 7

_MEGATHREAD_BONUS = 2.5
_MIN_POST_COMMENTS = 3  # below this a tree call isn't worth the money
_GUARANTEED_MEGATHREAD_SLOTS = 2
_MAX_COMMENT_DEPTH = (
    10  # Reddit nests ~10 deep at most; the API caps trees at ~100 nodes
)
_DEPTH_FACTORS = {0: 1.0, 1: 0.7, 2: 0.5, 3: 0.4}
_DEEP_DEPTH_FACTOR = 0.3  # depth 4+: side-conversations, discounted but never dropped
# "thread" = no in-body mention, but the post itself is about the ticker, so
# the comment is relevant by context. Direct mentions outrank it.
_MATCH_CONFIDENCE = {"cashtag": 1.3, "company_name": 1.1, "symbol": 1.0, "thread": 0.9}
_MIN_BODY_CHARS = 40
_MAX_PREVIEW_CHARS = 3000
_EXCERPT_CHARS = 400

# Both invariant failures are silent at runtime — fail loudly at import.
assert set(DEFAULT_SUBREDDITS) <= set(SUBREDDIT_WEIGHTS), (
    "every default subreddit needs a weight"
)
assert config.REDDIT_MAX_SUBREDDITS_PER_QUERY >= len(DEFAULT_SUBREDDITS), (
    "subreddit cap must cover the default list"
)

# Daily call budget (UTC day) and process-lifetime disable on auth failure.
_budget_day: date | None = None
_calls_used = 0
_disabled_reason: str | None = None
# Megathread-drift alarm: last date each sub yielded a megathread.
_last_megathread_seen: dict[str, date] = {}


def _take_call_budget() -> bool:
    """Consume one API call from today's budget; False when spent."""
    global _budget_day, _calls_used
    today = datetime.now(timezone.utc).date()
    if _budget_day != today:
        _budget_day = today
        _calls_used = 0
    if _calls_used >= config.REDDIT_DAILY_CALL_BUDGET:
        return False
    _calls_used += 1
    return True


class _RateLimited(Exception):
    """429 from the API: return what we have, no in-request retry."""


class RedditApisTransport:
    """Thin redditapis.com client. Every method here costs one read call."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _get(self, path: str, params: dict) -> dict:
        """Authenticated GET with budget, auth-disable, and rate-limit handling."""
        global _disabled_reason
        if _disabled_reason:
            raise SourceError(_disabled_reason)
        if not _take_call_budget():
            logger.warning(
                "reddit: daily call budget (%d) reached",
                config.REDDIT_DAILY_CALL_BUDGET,
            )
            raise SourceError("Reddit daily call budget reached")
        response = await self._client.get(
            f"{config.REDDIT_API_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {config.REDDIT_API_KEY}"},
        )
        if response.status_code in (401, 403):
            # Bad key or exhausted credit: retrying burns time on every request.
            _disabled_reason = "Reddit API key rejected (bad key or no credit)"
            logger.error(
                "reddit: %s — disabling for process lifetime", _disabled_reason
            )
            raise SourceError(_disabled_reason)
        if response.status_code == 429:
            raise _RateLimited()
        response.raise_for_status()
        return response.json()

    async def search_posts(self, subreddit: str, q: str, t: str) -> list[dict]:
        """Track A: ticker search within one subreddit."""
        data = await self._get(
            "/api/reddit/search",
            {"q": q, "subreddit": subreddit, "sort": "comments", "t": t, "limit": 10},
        )
        return data.get("posts") or []

    async def search_all(self, q: str, t: str) -> list[dict]:
        """Track C: ticker search across all of Reddit (no subreddit filter)."""
        data = await self._get(
            "/api/reddit/search", {"q": q, "sort": "comments", "t": t, "limit": 25}
        )
        return data.get("posts") or []

    async def hot_posts(self, subreddit: str) -> list[dict]:
        """Track B: hot listing, where stickied megathreads live."""
        data = await self._get(
            "/api/reddit/posts", {"subreddit": subreddit, "sort": "hot", "limit": 5}
        )
        return data.get("posts") or []

    async def comments(self, permalink: str) -> list[dict]:
        """One post's comment tree (native listing children). One call, any size."""
        data = await self._get(
            "/api/reddit/comments",
            {"permalink": permalink, "sort": "top", "limit": 100},
        )
        return data.get("comments") or []


# --- Pure logic (client-agnostic, unit-testable) ---------------------------------


def is_megathread_title(title: str) -> bool:
    """True when a post title matches a known daily/weekly megathread pattern."""
    for pattern in MEGATHREAD_PATTERNS:
        if pattern.search(title):
            return True
    return False


def build_search_query(ticker: str, search_name: str) -> str:
    """Ticker OR colloquial-name query; stopword tickers search the name alone.

    Multiword names must be quoted: the search API ANDs loose tokens, so an
    unquoted "space exploration techn-cl a" matches nothing and poisons the
    whole OR query (verified live — it returned 0 posts where plain SPCX had 5).
    """
    ticker = ticker.upper()
    name = search_name.strip().lower()
    if name == ticker.lower():
        name = ""
    phrase = name
    if " " in name:
        phrase = f'"{name}"'
    if ticker in STOPWORD_TICKERS and phrase:
        return phrase
    if phrase:
        return f"{ticker} OR {phrase}"
    return ticker


def sitewide_search_query(ticker: str) -> str:
    """Track C query: the bare ticker, or the cashtag for stopword tickers.

    Site-wide, the company name is consumer chatter: "NVDA OR nvidia" returned
    25/25 GPU-driver and gaming threads (verified live). Investing threads write
    the symbol.
    """
    ticker = ticker.upper()
    if ticker in STOPWORD_TICKERS:
        return f"${ticker}"
    return ticker


def is_sitewide_post_relevant(post: dict, ticker: str) -> bool:
    """Site-wide hits must name the ticker (symbol or cashtag) in the title.

    A company-name-only title ("Nvidia DLSS 5 cuts framerates") is a consumer
    thread, not a stock thread; those are relevant by context in curated subs
    only. Stopword tickers can't match on the bare symbol, so they need the cashtag.
    """
    return match_ticker(post.get("title") or "", ticker, "") in ("cashtag", "symbol")


def match_ticker(
    body: str, ticker: str, company: str, aliases: tuple[str, ...] = ()
) -> str | None:
    """Classify a comment's ticker mention; None when it isn't about the ticker.

    Failure modes are asymmetric: a false positive is a garbage card, a false
    negative is invisible. Cashtag is highest-confidence; a bare symbol counts
    only in uppercase (lowercase prose never is the ticker); stopword tickers
    require the cashtag or the company name.
    """
    ticker = ticker.upper()
    if re.search(rf"\${re.escape(ticker)}\b", body, re.IGNORECASE):
        return "cashtag"
    # Resolvers hand back legal names ("NVIDIA CORP"); comments say "Nvidia".
    # Match on the core name, and only when it isn't itself a bare stopword.
    core_name = ""
    if company:
        core_name = _core_company_name(company)
    names = []
    if len(core_name) > 2 or " " in core_name:
        names.append(core_name)
    names.extend(aliases)
    for name in names:
        if name and re.search(rf"\b{re.escape(name)}(?:'s)?\b", body, re.IGNORECASE):
            return "company_name"
    if ticker not in STOPWORD_TICKERS and re.search(rf"\b{ticker}\b", body):
        return "symbol"  # case-sensitive: uppercase NVDA is the ticker, "on" is not
    return None


def make_excerpt(body: str, limit: int = _EXCERPT_CHARS) -> str:
    """Whitespace-normalize and cut at a sentence boundary near the limit."""
    text = re.sub(r"\s+", " ", body).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for boundary in (". ", "! ", "? "):
        boundary_position = cut.rfind(boundary)
        if boundary_position >= limit * 0.6:
            return cut[: boundary_position + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def comment_permalink(comment: dict, post_url: str) -> str:
    """Absolute deep link to the comment; built from post URL as a fallback."""
    permalink = comment.get("permalink")
    if permalink:
        return f"https://reddit.com{permalink}"
    return f"{post_url.rstrip('/')}/{comment.get('id', '')}/"


def flatten_tree(
    children: list[dict], max_depth: int = _MAX_COMMENT_DEPTH
) -> tuple[list[dict], int]:
    """Flatten a native listing tree to comment dicts; skip (and count) more-stubs.

    Each expansion of a "more" stub would be another billable call for deep,
    cold comments — never recurse into them.
    """
    comments: list[dict] = []
    skipped_more = 0

    def walk(nodes: list[dict], depth: int) -> None:
        nonlocal skipped_more
        for node in nodes:
            kind = node.get("kind")
            if kind == "more":
                skipped_more += 1
                continue
            if kind != "t1":
                continue
            data = node.get("data") or {}
            entry = dict(data)
            entry["depth"] = depth
            comments.append(entry)
            if depth < max_depth:
                replies = data.get("replies")
                if isinstance(replies, dict):
                    reply_data = replies.get("data") or {}
                    reply_children = reply_data.get("children") or []
                    walk(reply_children, depth + 1)

    walk(children, 0)
    return comments, skipped_more


def keep_comment(comment: dict) -> bool:
    """Baseline comment filter: removed/bot/pinned/too-short-without-cashtag."""
    body = (comment.get("body") or "").strip()
    if body in ("[deleted]", "[removed]") or not body:
        return False
    if comment.get("author") == "AutoModerator":
        return False
    if comment.get("stickied"):
        return False
    if len(body) > _MAX_PREVIEW_CHARS:
        return False  # DD-length; won't render as a card
    if len(body) < _MIN_BODY_CHARS:
        has_cashtag = re.search(r"\$[A-Za-z]{1,6}\b", body)
        if not has_cashtag:
            return False  # "NVDA calls 🚀" is not a take; "$NVDA to 200" has direction
    return True


def select_posts_for_fetch(
    posts: list[dict],
    max_trees: int = config.REDDIT_MAX_COMMENT_TREES_PER_QUERY,
    now: datetime | None = None,
) -> list[dict]:
    """Pick which posts get a (billable) comment-tree call.

    Selection is about *likelihood of containing relevant comments*, not display
    worthiness: log1p(comment count), subreddit weight, a large deliberate
    megathread bonus, and mild recency decay. Megathreads get guaranteed slots
    so a viral ticker post can't crowd them all out.
    """
    now = now or datetime.now(timezone.utc)

    def priority(post: dict) -> float:
        created_at = post.get("created_utc") or 0
        age_hours = max(0.0, (now.timestamp() - created_at) / 3600)
        megathread_bonus = 1.0
        if post.get("is_megathread"):
            megathread_bonus = _MEGATHREAD_BONUS
        subreddit_weight = SUBREDDIT_WEIGHTS.get(
            post.get("subreddit", ""), _DEFAULT_SUBREDDIT_WEIGHT
        )
        discussion_score = math.log1p(post.get("comments") or 0)
        recency_decay = math.exp(-age_hours / 96)
        return discussion_score * subreddit_weight * megathread_bonus * recency_decay

    eligible = []
    for post in posts:
        if (post.get("comments") or 0) >= _MIN_POST_COMMENTS:
            eligible.append(post)
    ranked = sorted(eligible, key=priority, reverse=True)

    megathreads = []
    for post in ranked:
        if post.get("is_megathread"):
            megathreads.append(post)
    selected: list[dict] = megathreads[:_GUARANTEED_MEGATHREAD_SLOTS]
    for post in ranked:
        if len(selected) >= max_trees:
            break
        if post not in selected:
            selected.append(post)

    if len(selected) < max_trees:
        # Thinly-discussed tickers: 1-2 comment posts about the stock are the
        # discussion — backfill with them rather than leave budget unused.
        lightly_discussed_posts = []
        for post in posts:
            comment_count = post.get("comments") or 0
            if 0 < comment_count < _MIN_POST_COMMENTS:
                lightly_discussed_posts.append(post)
        lightly_discussed_posts.sort(key=priority, reverse=True)
        for post in lightly_discussed_posts:
            if len(selected) >= max_trees:
                break
            selected.append(post)
    return selected[:max_trees]


def rank_comment(
    comment: dict, ticker_match: str, now: datetime | None = None
) -> float:
    """Display-rank a matched comment; comments decay faster than posts (48h)."""
    now = now or datetime.now(timezone.utc)
    upvotes = comment.get("score") or comment.get("ups") or 0
    created_at = comment.get("created_utc") or 0
    age_hours = max(0.0, (now.timestamp() - created_at) / 3600)
    depth_factor = _DEPTH_FACTORS.get(comment.get("depth", 0), _DEEP_DEPTH_FACTOR)
    subreddit_weight = SUBREDDIT_WEIGHTS.get(
        comment.get("subreddit", ""), _DEFAULT_SUBREDDIT_WEIGHT
    )
    # Negative scores: clamp for the sqrt but never discard — a downvoted
    # contrarian take on a hyped ticker can be the most informative thing there.
    vote_score = math.sqrt(max(upvotes, 0))
    recency_decay = math.exp(-age_hours / 48)
    match_confidence = _MATCH_CONFIDENCE[ticker_match]
    return (
        vote_score * subreddit_weight * recency_decay * depth_factor * match_confidence
    )


def _t_param(since: datetime, now: datetime | None = None) -> str:
    """Map a window start to the search API's coarse t bucket."""
    span = (now or datetime.now(timezone.utc)) - since
    if span <= timedelta(hours=1):
        return "hour"
    if span <= timedelta(days=1):
        return "day"
    if span <= timedelta(days=7):
        return "week"
    if span <= timedelta(days=31):
        return "month"
    return "year"


# --- Orchestration ----------------------------------------------------------------


async def _discover_posts(
    transport: RedditApisTransport, ticker: str, search_name: str, since: datetime
) -> list[dict]:
    """Find ticker posts and megathreads in the configured subreddits.

    Site-wide discovery is disabled; only the curated subreddits are searched.
    """
    time_bucket = _t_param(since)
    cache_key = ("reddit-discovery", "v3", ticker.upper(), time_bucket)
    cached = reddit_cache.get(cache_key)
    if cached is not None:
        return cached

    subreddits = DEFAULT_SUBREDDITS[: config.REDDIT_MAX_SUBREDDITS_PER_QUERY]
    query = build_search_query(ticker, search_name)
    requests = []
    for subreddit in subreddits:
        requests.append(transport.search_posts(subreddit, query, time_bucket))
    for subreddit in subreddits:
        requests.append(transport.hot_posts(subreddit))
    results = await asyncio.gather(*requests, return_exceptions=True)

    # gather preserves request order: searches first, then hot listings.
    subreddit_count = len(subreddits)
    search_results = results[:subreddit_count]
    hot_results = results[subreddit_count:]

    posts: dict[str, dict] = {}
    for batch in search_results:
        if isinstance(batch, BaseException):
            continue  # one failed subreddit never fails the request
        for post in batch:
            post["is_megathread"] = False
            post["discovered_via"] = "sub"
            posts.setdefault(post["id"], post)
    today = datetime.now(timezone.utc).date()
    for subreddit, batch in zip(subreddits, hot_results):
        if isinstance(batch, BaseException):
            continue
        found_megathread = False
        for post in batch:
            if post.get("stickied") and is_megathread_title(post.get("title") or ""):
                post["is_megathread"] = True
                posts[post["id"]] = post  # megathreads override a search duplicate
                found_megathread = True
        if found_megathread:
            _last_megathread_seen[subreddit] = today
        else:
            last_seen = _last_megathread_seen.get(subreddit)
            if last_seen and (today - last_seen).days >= 2:
                # Drift alarm: the sub probably renamed its daily thread.
                logger.warning(
                    "reddit: no megathread found in r/%s for 2+ days", subreddit
                )

    result = list(posts.values())
    # Cache negatives too (~300s): small caps would otherwise re-run the whole
    # fan-out on every page load and spend money to learn nothing.
    ttl = config.REDDIT_CACHE_TTL
    if not result:
        ttl = 300
    reddit_cache.set(cache_key, result, ttl=ttl)
    return result


async def _fetch_tree(transport: RedditApisTransport, post: dict) -> list[dict]:
    """Stage 2, one post: cached comment tree (cache the tree, not the filtered
    result, so a different ticker query reuses the same fetched megathread)."""
    permalink = post.get("permalink") or ""
    cache_key = ("reddit-tree", permalink)
    cached = reddit_cache.get(cache_key)
    if cached is not None:
        return cached
    tree = await transport.comments(permalink)
    ttl = config.REDDIT_CACHE_TTL
    if post.get("is_megathread"):
        ttl = config.REDDIT_MEGATHREAD_CACHE_TTL  # megathreads change more often
    reddit_cache.set(cache_key, tree, ttl=ttl)
    return tree


def _match_comments_in_tree(
    post: dict,
    tree: list[dict],
    ticker: str,
    company: str,
    aliases: tuple[str, ...],
    since: datetime,
) -> list[dict]:
    """Filter one post's comments and attach the context needed for ranking."""
    comments, skipped = flatten_tree(tree)
    if skipped:
        logger.info("reddit: skipped %d more-stubs in %s", skipped, post.get("id"))

    # In a ticker-specific thread, a comment can refer to "this company"
    # without naming it. General threads still require an explicit mention.
    title_match = match_ticker(post.get("title") or "", ticker, company, aliases)
    post_about_ticker = title_match is not None

    matched = []
    for comment in comments:
        if not keep_comment(comment):
            continue
        match_kind = match_ticker(comment.get("body") or "", ticker, company, aliases)
        if match_kind is None and post_about_ticker:
            match_kind = "thread"
        if match_kind is None:
            continue

        timestamp = comment.get("created_utc") or 0
        created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if created_at < since:
            continue

        matched.append(
            {
                "comment": comment,
                "matched_on": match_kind,
                "rank": rank_comment(comment, match_kind),
                "created": created_at,
                "post_title": post.get("title") or "",
                "post_url": post.get("url") or "",
                "is_megathread": bool(post.get("is_megathread")),
            }
        )

    # A sudden drop to zero matches can reveal a broken mention filter.
    logger.info(
        "reddit: %s matched %d/%d comments (megathread=%s)",
        post.get("id"),
        len(matched),
        len(comments),
        bool(post.get("is_megathread")),
    )
    return matched


async def fetch_comments_for_ticker(
    ticker: str, company: str, since: datetime
) -> list[dict]:
    """Full two-stage pipeline; returns the top comment dicts, ranked.

    Each returned dict carries the comment plus thread context
    (post_title/post_url/subreddit/is_megathread) and match/rank metadata.
    """
    calls_before = _calls_used
    search_name, aliases = await names_for_ticker(ticker, company)
    async with httpx.AsyncClient(timeout=5.0) as client:
        transport = RedditApisTransport(client)
        try:
            posts = await _discover_posts(transport, ticker, search_name, since)
        except _RateLimited:
            return []  # 429: return what we have (nothing yet), no retry
        selected = select_posts_for_fetch(posts)
        requests = []
        for post in selected:
            requests.append(_fetch_tree(transport, post))
        trees = await asyncio.gather(*requests, return_exceptions=True)

    matched: list[dict] = []
    for post, tree in zip(selected, trees):
        if isinstance(tree, BaseException):
            if isinstance(tree, SourceError):
                raise tree  # budget/auth: surface once, don't mask as empty
            continue
        tree_matches = _match_comments_in_tree(
            post, tree, ticker, company, aliases, since
        )
        matched.extend(tree_matches)

    matched.sort(key=lambda match: match["rank"], reverse=True)
    # Aggregate across threads: best few comments per thread, top N overall.
    kept: list[dict] = []
    per_thread: dict[str, int] = {}
    for match in matched:
        if len(kept) >= REDDIT_TOP_COMMENTS:
            break
        thread = match["post_url"]
        thread_count = per_thread.get(thread, 0)
        if thread_count >= _MAX_COMMENTS_PER_THREAD:
            continue
        kept.append(match)
        per_thread[thread] = thread_count + 1
    logger.info(
        "reddit: fetch for %s used %d calls (%d today); kept %d/%d from %d threads",
        ticker,
        _calls_used - calls_before,
        _calls_used,
        len(kept),
        len(matched),
        len(per_thread),
    )
    return kept


# --- Adapter (the one class social.py sees) ------------------------------------------


class RedditCommentSource:
    """Top ticker-relevant Reddit *comments* via redditapis.com (paid reads).

    Two round trips (post discovery, then comment trees) make this the slowest
    source; its own timeout keeps it from blocking the others.
    """

    name = "reddit"
    timeout = 30.0  # two-stage; must exceed both stage budgets

    def __init__(self) -> None:
        self.enabled = bool(config.REDDIT_API_KEY)

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """Run the two-stage comment pipeline and map to normalized posts."""
        matches = await fetch_comments_for_ticker(ticker, company, since)
        posts: list[SocialPost] = []
        for match in matches:
            comment = match["comment"]
            excerpt = make_excerpt(comment.get("body") or "")
            posts.append(
                SocialPost(
                    type="reddit",
                    title=excerpt.splitlines()[0][:120],
                    text=excerpt,
                    url=comment_permalink(comment, match["post_url"]),
                    source=f"r/{comment.get('subreddit') or ''}",
                    author=comment.get("author") or "[deleted]",
                    published_at=match["created"],
                    likes=int(comment.get("score") or 0),
                    comments=0,  # reply counts aren't carried per-comment
                    context=match["post_title"],
                    is_megathread=match["is_megathread"],
                    author_flair=comment.get("author_flair_text") or None,
                )
            )
        return posts
