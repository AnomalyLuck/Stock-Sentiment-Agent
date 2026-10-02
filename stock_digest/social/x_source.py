"""X (Twitter) source via twitterapi.io — one `Top` search, ranked locally.

`queryType=Top` is X's own "Top" search tab bounded by since_time/until_time:
a candidate pool that spans the window with real engagement (verified, see
tests/fixtures/x-api-contract.md). It is a pool, not an order — the provider
blends recency in, so the scorer here does the ranking. Every returned tweet
bills ($0.15 per 1,000), so the cache is a cost control, not a nicety.

The provider is scraper-backed and not licensed by X (see CLAUDE.md). The
adapter boundary — `XSource` (bottom of this file) -> `fetch_top_tweets` ->
`TwitterApiTransport` — exists so it can be swapped. Field shapes are the
provider's (camelCase, Twitter-classic timestamps); every one read below was
verified in Step 0.
"""

import asyncio
import html
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from . import config
from .aliases import names_for_ticker
from .cache import x_cache
from .reddit_source import match_ticker
from .retrieval import WINDOWS
from .social_types import SocialPost, SourceError

logger = logging.getLogger(__name__)


PROVIDER = "twitterapi"  # the only X_API_PROVIDER value this module serves
_SEARCH_PATH = "/twitter/tweet/advanced_search"

# Editorial choices, not deployment knobs (those live in config).
X_DISPLAY_CAP = 15
X_MAX_PER_AUTHOR = 2  # verified: three accounts owned a third of a Top page

# `Top` needs time to mean anything: under an hour nothing has earned
# engagement yet, so fall back to the newest posts and say so in the status.
_MIN_RANKABLE_SPAN = timedelta(hours=1)
LATEST_NOTE = "newest — too recent to rank"

_CREATED_AT_FMT = "%a %b %d %H:%M:%S %z %Y"  # "Fri Sep 11 17:50:00 +0000 2026"

# Filters. Each query operator is duplicated client-side on purpose: if the
# provider starts ignoring one, the pass here still holds.
_MIN_BODY_CHARS = 40
_MAX_CASHTAGS = 3  # more distinct symbols than this is a pump list
_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_PROMO_RE = re.compile(
    r"join my discord|free signals|\bdm me\b|link in bio|100% win rate",
    re.IGNORECASE,
)

# Scoring weights; tune against tests/fixtures/x-top-24h.json, which has a
# real spread (likes 1-929, bookmarks 0-138, followers 815-713k).
_W_ENGAGEMENT = 0.85
_W_RECENCY = 0.15
_W_INSTITUTIONAL = 0.10
_W_ENGAGEMENT_RATIO = 0.15
# Replies and bookmarks weigh double: a reply marks discussion, a bookmark
# marks someone deciding the post is worth returning to. viewCount is the
# author's reach, not the post's merit, and is ignored (as `views` is for
# YouTube).
_ENGAGEMENT_WEIGHTS = {
    "likeCount": 1.0,
    "replyCount": 2.0,
    "bookmarkCount": 2.0,
    "retweetCount": 1.5,
    "quoteCount": 1.5,
}
# Cashtag > company name > bare symbol; `Top` is a ranker, not a relevance
# guarantee, so a post that only happens to say NVDA ranks below one about it.
_RELEVANCE = {"cashtag": 1.3, "company_name": 1.1, "symbol": 0.9}
# The provider capitalises ("Business"); compare lowercased. Blue checks earn
# nothing — they're bought, not earned.
_INSTITUTIONAL_TYPES = frozenset({"business", "government"})
_RECENCY_HALF_LIFE_DIVISOR = 4  # half-life = window / 4: a tie-breaker inside Top

# Provider-drift alarm: a tweet missing any of these can't be shown at all.
_REQUIRED_FIELDS = ("id", "url", "text", "createdAt")
_MALFORMED_PAGE_FRACTION = 0.5

# Daily tweet budget (UTC day) and process-lifetime disable on auth failure.
_budget_day: date | None = None
_tweets_used = 0
_disabled_reason: str | None = None


def _roll_day() -> None:
    """Reset the tweet counter when the UTC date changes."""
    global _budget_day, _tweets_used
    today = datetime.now(timezone.utc).date()
    if _budget_day != today:
        _budget_day = today
        _tweets_used = 0


def budget_remaining() -> int:
    """Tweets left in today's budget."""
    _roll_day()
    return max(0, config.X_DAILY_TWEET_BUDGET - _tweets_used)


def _record_tweets(n: int) -> None:
    """Count billed tweets against today's budget."""
    global _tweets_used
    _roll_day()
    _tweets_used += n


class _RateLimited(Exception):
    """429 from the provider: return what we have, no in-request retry."""


class TwitterApiTransport:
    """Thin twitterapi.io client. Every tweet a call returns is billed."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def search(self, query: str, query_type: str, cursor: str = "") -> dict:
        """One advanced_search page with budget, auth-disable and 429 handling."""
        global _disabled_reason
        if _disabled_reason:
            raise SourceError(_disabled_reason)
        if budget_remaining() <= 0:
            logger.warning(
                "x: daily tweet budget (%d) reached", config.X_DAILY_TWEET_BUDGET
            )
            raise SourceError("X daily tweet budget reached")
        response = await self._client.get(
            f"{config.X_API_BASE}{_SEARCH_PATH}",
            params={"query": query, "queryType": query_type, "cursor": cursor},
            headers={"X-API-Key": config.X_API_KEY},
        )
        if response.status_code in (401, 403):
            # Bad key or empty credit: retrying burns time on every request.
            _disabled_reason = "X API key rejected (bad key or no credit)"
            logger.error("x: %s — disabling for process lifetime", _disabled_reason)
            raise SourceError(_disabled_reason)
        if response.status_code == 429:
            raise _RateLimited()
        response.raise_for_status()
        body = response.json()
        _record_tweets(len(body.get("tweets") or []))
        return body


# --- Pure logic (client-agnostic, unit-testable) ---------------------------------


def parse_created_at(raw: str) -> datetime:
    """Parse the provider's Twitter-classic timestamp to an aware UTC datetime.

    A wrong parse silently breaks the window filter in `fetch_social` and
    every post vanishes — hence the dedicated test.
    """
    return datetime.strptime(raw, _CREATED_AT_FMT).astimezone(timezone.utc)


def window_label(since: datetime, now: datetime | None = None) -> str:
    """Map a window start back to the nearest app window key (the cache key)."""
    span = (now or datetime.now(timezone.utc)) - since

    def distance_from_span(window: str) -> timedelta:
        return abs(WINDOWS[window] - span)

    return min(WINDOWS, key=distance_from_span)


def query_type_for(since: datetime, now: datetime | None = None) -> str:
    """`Top` for rankable windows; `Latest` when the span is under an hour."""
    span = (now or datetime.now(timezone.utc)) - since
    if span < _MIN_RANKABLE_SPAN:
        return "Latest"
    return "Top"


def build_query(ticker: str, search_name: str, since: datetime, until: datetime) -> str:
    """Cashtag-or-name query with retweet/reply/language filters and an epoch window.

    since_time/until_time are unix seconds — the vendor states the
    since:YYYY-MM-DD form is not supported. The archive reaches at least 60
    days back (verified), so no window the app offers needs a clamp.
    """
    ticker = ticker.upper()
    name = search_name.strip().lower()
    if not name or name == ticker.lower():
        subject = f"${ticker}"
    else:
        subject = f'(${ticker} OR "{name}")'
    return (
        f"{subject} -filter:retweets -filter:replies lang:en "
        f"since_time:{int(since.timestamp())} until_time:{int(until.timestamp())}"
    )


def cashtags(tweet: dict) -> list[str]:
    """Distinct cashtags, from entities.symbols (verified) or a text regex fallback."""
    entities = tweet.get("entities") or {}
    symbols = entities.get("symbols")
    found = []
    if isinstance(symbols, list):
        for symbol in symbols:
            if isinstance(symbol, dict):
                found.append(str(symbol.get("text") or ""))
    else:
        for match in _CASHTAG_RE.findall(tweet.get("text") or ""):
            found.append(match[1:])  # remove the leading dollar sign
    seen: list[str] = []
    for tag in found:
        tag = tag.upper()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def strip_body(text: str) -> str:
    """Post text minus URLs, cashtags and mentions, for length checks only."""
    text = _URL_RE.sub(" ", text)
    text = _CASHTAG_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def author_handle(tweet: dict) -> str:
    """`@userName`, or "unknown" when the provider carries no author."""
    author = tweet.get("author") or {}
    name = author.get("userName")
    if name:
        return f"@{name}"
    return "unknown"


def is_institutional(author: dict) -> bool:
    """Business/government verification (the provider sends "Business")."""
    return str(author.get("verifiedType") or "").lower() in _INSTITUTIONAL_TYPES


def ticker_match(
    tweet: dict, ticker: str, company: str, aliases: tuple[str, ...] = ()
) -> str | None:
    """Classify the tweet's ticker mention; None when it isn't about the ticker.

    The provider's parsed entities decide the cashtag; the text then goes
    through the shared classifier for the company name and bare symbol.
    """
    if ticker.upper() in cashtags(tweet):
        return "cashtag"
    return match_ticker(tweet.get("text") or "", ticker, company, aliases)


def drop_reason(
    tweet: dict,
    ticker: str,
    company: str,
    aliases: tuple[str, ...] = (),
    min_followers: int | None = None,
) -> str | None:
    """Why a tweet is excluded before scoring; None when it survives.

    An unattributable tweet (no author/userName) is exempt from the follower
    floor — there is nothing to measure — but earns no author bonus either.
    A missing `lang` is unknown, not foreign; only a present non-"en" drops.
    """
    if min_followers is None:
        min_followers = config.X_MIN_FOLLOWERS
    if tweet.get("retweeted_tweet"):
        return "retweet"  # someone else's post
    if tweet.get("isReply"):
        return "reply"
    if tweet.get("lang") not in (None, "", "en"):
        return "non-english"
    author = tweet.get("author") or {}
    if author.get("isAutomated"):
        return "automated"
    if author.get("userName") and int(author.get("followers") or 0) < min_followers:
        return "few-followers"
    if len(cashtags(tweet)) > _MAX_CASHTAGS:
        return "pump-list"
    text = tweet.get("text") or ""
    if len(strip_body(text)) < _MIN_BODY_CHARS:
        return "too-short"
    if _PROMO_RE.search(text):
        return "promo"
    if ticker_match(tweet, ticker, company, aliases) is None:
        return "off-ticker"
    return None


def is_well_formed(tweet: object) -> bool:
    """Whether a tweet carries every field the adapter reads (drift detector)."""
    if not isinstance(tweet, dict):
        return False
    for field in _REQUIRED_FIELDS:
        if not tweet.get(field):
            return False
    try:
        parse_created_at(str(tweet["createdAt"]))
    except ValueError:
        return False
    return True


def engagement(tweet: dict) -> float:
    """Weighted engagement: likes + 2×(replies, bookmarks) + 1.5×(retweets, quotes)."""
    total = 0
    for field, weight in _ENGAGEMENT_WEIGHTS.items():
        count = max(0, int(tweet.get(field) or 0))
        total += weight * count
    return total


def recency(age_hours: float, window_hours: float) -> float:
    """Half-life decay over a quarter of the window, clamped to [0, 1]."""
    half_life = max(window_hours / _RECENCY_HALF_LIFE_DIVISOR, 1e-9)
    age_hours = max(0.0, age_hours)
    decay = 0.5 ** (age_hours / half_life)
    return min(1.0, max(0.0, decay))


def _minmax(values: list[float]) -> list[float]:
    """Scale to 0..1 within the batch; a flat batch scores 0.5 (no divide by zero)."""
    if not values:
        return []
    lowest = min(values)
    highest = max(values)
    normalized_values = []
    for value in values:
        if highest == lowest:
            normalized_values.append(0.5)
        else:
            normalized_values.append((value - lowest) / (highest - lowest))
    return normalized_values


def score_tweets(
    tweets: list[dict],
    matches: list[str],
    since: datetime,
    now: datetime | None = None,
) -> list[float]:
    """Score each tweet (parallel to `tweets`); `matches[i]` is its ticker_match.

    score = (0.85·engagement + 0.15·recency) · relevance
            + 0.10·institutional + 0.15·engagement_per_follower

    Engagement and the per-follower ratio are log-scaled and min-max'd within
    the batch, so scores only mean anything relative to each other. Raw
    follower count is deliberately absent: the biggest cashtag accounts skew
    promotional, and engagement per follower is the signal that count inverts.
    """
    now = now or datetime.now(timezone.utc)
    window_hours = max((now - since).total_seconds() / 3600, 1e-9)

    log_engagements = []
    for tweet in tweets:
        raw_engagement = engagement(tweet)
        log_engagements.append(math.log1p(raw_engagement))
    engagement_scores = _minmax(log_engagements)

    follower_ratios: list[float] = []
    for tweet, log_engagement in zip(tweets, log_engagements):
        author = tweet.get("author") or {}
        followers = int(author.get("followers") or 0)
        if followers > 0:
            follower_ratio = log_engagement / math.log1p(followers)
        else:
            follower_ratio = 0.0
        follower_ratios.append(follower_ratio)
    follower_ratio_scores = _minmax(follower_ratios)

    scores: list[float] = []
    for index, tweet in enumerate(tweets):
        created_at = parse_created_at(tweet["createdAt"])
        age_hours = (now - created_at).total_seconds() / 3600
        author = tweet.get("author") or {}
        institutional = bool(author.get("userName")) and is_institutional(author)
        recency_score = recency(age_hours, window_hours)
        base_score = (
            _W_ENGAGEMENT * engagement_scores[index] + _W_RECENCY * recency_score
        )
        relevance = _RELEVANCE[matches[index]]
        score = (
            base_score * relevance
            + _W_INSTITUTIONAL * institutional
            + _W_ENGAGEMENT_RATIO * follower_ratio_scores[index]
        )
        scores.append(score)
    return scores


def select_top(
    tweets: list[dict],
    scores: list[float],
    per_author: int = X_MAX_PER_AUTHOR,
    cap: int = X_DISPLAY_CAP,
) -> list[dict]:
    """Best first, at most `per_author` per account and `cap` overall."""
    ranked_indices = sorted(
        range(len(tweets)), key=lambda index: scores[index], reverse=True
    )
    kept: list[dict] = []
    author_counts: dict[str, int] = {}
    for index in ranked_indices:
        if len(kept) >= cap:
            break
        tweet = tweets[index]
        author = author_handle(tweet)
        author_count = author_counts.get(author, 0)
        if author_count >= per_author:
            continue
        kept.append(tweet)
        author_counts[author] = author_count + 1
    return kept


# --- Orchestration ----------------------------------------------------------------


async def _fetch_pages(query: str, query_type: str, max_pages: int) -> list[dict]:
    """Follow next_cursor for up to `max_pages` pages; dedupe by id.

    Pages are sequential by construction (each cursor comes from the previous
    response), spaced by X_PAGE_INTERVAL_S. A 429 or a budget stop after the
    first page returns what has been fetched — it is already paid for.
    """
    fetched: list[dict] = []
    seen: set[str] = set()
    cursor = ""
    async with httpx.AsyncClient(timeout=5.0) as client:
        transport = TwitterApiTransport(client)
        for page in range(max_pages):
            if page and config.X_PAGE_INTERVAL_S > 0:
                await asyncio.sleep(config.X_PAGE_INTERVAL_S)
            try:
                body = await transport.search(query, query_type, cursor)
            except _RateLimited:
                # On a paid key this is the provider's limit changing, not a
                # bug here: log it and check the dashboard.
                logger.warning(
                    "x: rate limited on page %d; keeping %d", page + 1, len(fetched)
                )
                break
            except SourceError:
                if not fetched:
                    raise
                logger.warning(
                    "x: stopped before page %d; keeping %d", page + 1, len(fetched)
                )
                break
            tweets = body.get("tweets") or []  # verified shape, still not trusted
            if not tweets:
                break
            for tweet in tweets:
                tweet_id = ""
                if isinstance(tweet, dict):
                    tweet_id = str(tweet.get("id") or "")
                if tweet_id and tweet_id in seen:
                    continue
                if tweet_id:
                    seen.add(tweet_id)
                fetched.append(tweet)
            cursor = body.get("next_cursor") or ""
            # An under-full page is not the last page; trust has_next_page.
            if not body.get("has_next_page") or not cursor:
                break
    return fetched


async def fetch_top_tweets(
    ticker: str, company: str, since: datetime, now: datetime | None = None
) -> tuple[list[dict], str | None]:
    """Fetch, filter, score and select the window's top tweets; cached per window.

    Returns (tweets, note). `note` is set when the result is the newest posts
    rather than a ranking (sub-hour windows) so the source status can say so.
    """
    ticker = ticker.upper()
    now = now or datetime.now(timezone.utc)
    cache_key = ("x-top", ticker, window_label(since, now))
    cached = x_cache.get(cache_key)
    if cached is not None:
        return cached

    query_type = query_type_for(since, now)
    note = None
    max_pages = config.X_MAX_PAGES
    if query_type == "Latest":
        note = LATEST_NOTE
        max_pages = 1
    search_name, aliases = await names_for_ticker(ticker, company)
    query = build_query(ticker, search_name, since, now)

    used_before = _tweets_used
    raw = await _fetch_pages(query, query_type, max_pages)

    well_formed = []
    for tweet in raw:
        if is_well_formed(tweet):
            well_formed.append(tweet)
    malformed = len(raw) - len(well_formed)
    if malformed:
        logger.warning("x: %d/%d tweets missing required fields", malformed, len(raw))
        if malformed > len(raw) * _MALFORMED_PAGE_FRACTION:
            raise SourceError("X provider response shape changed")

    kept: list[dict] = []
    matches: list[str] = []
    dropped: dict[str, int] = {}
    for tweet in well_formed:
        reason = drop_reason(tweet, ticker, company, aliases)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        kept.append(tweet)
        matches.append(ticker_match(tweet, ticker, company, aliases) or "symbol")
    # Never display the provider's order: Top is a pool, not a ranking.
    scores = score_tweets(kept, matches, since, now)
    top = select_top(kept, scores)
    logger.info(
        "x: %s %s billed %d tweets (%d today), kept %d, showing %d; dropped %s",
        ticker,
        query_type,
        _tweets_used - used_before,
        _tweets_used,
        len(kept),
        len(top),
        dropped or "none",
    )

    result = (top, note)
    # Empty results cost the same as full ones; cache them just as long.
    x_cache.set(cache_key, result, ttl=config.X_CACHE_TTL)
    return result


# --- Adapter (the one class social.py sees) ------------------------------------------


class XSource:
    """The window's most popular X posts via twitterapi.io; needs a key."""

    name = "x"

    def __init__(self) -> None:
        self.enabled = bool(config.X_API_KEY and config.X_API_PROVIDER)
        # Set by fetch(); fetch_social folds it into the "ok (...)" status.
        self.status_note: str | None = None

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """Run the ranked fetch and map tweets to normalized posts."""
        if config.X_API_PROVIDER != PROVIDER:
            # Never fall through to this client for a provider it wasn't built for.
            raise SourceError(
                f"X provider '{config.X_API_PROVIDER}' is not supported yet"
            )
        tweets, self.status_note = await fetch_top_tweets(ticker, company, since)

        posts: list[SocialPost] = []
        for tweet in tweets:
            # Decoded, never edited: `title` is a card-heading truncation only.
            body = html.unescape(tweet["text"]).strip()
            likes = int(tweet.get("likeCount") or 0)
            retweets = int(tweet.get("retweetCount") or 0)
            quotes = int(tweet.get("quoteCount") or 0)
            posts.append(
                SocialPost(
                    type="x",
                    title=body.splitlines()[0][:120],
                    text=body,
                    url=tweet["url"],  # canonical x.com/{user}/status/{id}, verified
                    source="X",
                    author=author_handle(tweet),
                    published_at=parse_created_at(tweet["createdAt"]),
                    likes=likes + retweets + quotes,
                    comments=int(tweet.get("replyCount") or 0),
                )
            )
        return posts
