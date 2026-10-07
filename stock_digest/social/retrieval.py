"""News retrieval (Finnhub + Google News RSS) and ticker resolution."""

import asyncio
import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from rapidfuzz import fuzz

from . import config
from .aliases import _core_company_name, names_for_ticker
from .cache import article_list_cache, ticker_cache

logger = logging.getLogger(__name__)

# Time-window keys -> lookback duration. Keys are shared with the frontend.
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

# Article-list cache TTLs per window (seconds). A cached entry is a snapshot of
# the window as it stood at fetch time, so the ttl is also the worst-case age of
# the newest story a repeat query can miss; _ttl_for_window caps it against the
# length of the window itself.
_WINDOW_TTL: dict[str, float] = {
    "15m": 300,
    "1h": 300,
    "4h": 300,
    "24h": 300,
    "48h": 300,
    "3d": 300,
    "1w": 900,
    "2w": 900,
    "1mo": 900,
    "3mo": 1800,
}

# No cached snapshot may be stale by more than this fraction of its own window.
_TTL_WINDOW_DIVISOR = 5


def _ttl_for_window(window: str) -> float:
    """Article-list cache TTL for a window, capped at a fifth of its length.

    A flat five minutes is negligible against a 3-day window and a third of a
    15-minute one, which is exactly where the empty/near-empty results live.
    """
    window_seconds = WINDOWS[window].total_seconds()
    maximum_staleness = window_seconds / _TTL_WINDOW_DIVISOR
    configured_ttl = _WINDOW_TTL[window]
    return min(configured_ttl, maximum_staleness)


# Fast-path map for common megacaps so we skip a symbol-lookup call.
COMMON_TICKERS: dict[str, str] = {
    "nvidia": "NVDA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "amazon": "AMZN",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "netflix": "NFLX",
    "amd": "AMD",
    "intel": "INTC",
    "broadcom": "AVGO",
    "oracle": "ORCL",
    "salesforce": "CRM",
    "jpmorgan": "JPM",
    "berkshire": "BRK.B",
    "walmart": "WMT",
    "disney": "DIS",
    "boeing": "BA",
    "palantir": "PLTR",
}

_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


class RateLimitedError(Exception):
    """Raised when Finnhub keeps returning 429 after retries."""


@dataclass
class Article:
    """Normalized news article."""

    title: str
    url: str
    source: str
    published_at: datetime  # UTC
    raw_snippet: str


@dataclass
class NewsResult:
    """A window of articles plus the moment the underlying fetch actually ran."""

    articles: list[Article]
    fetched_at: datetime  # UTC; earlier than now when served from cache


def window_cutoff(window: str, now: datetime | None = None) -> datetime:
    """Return the UTC datetime cutoff for a window key."""
    now = now or datetime.now(timezone.utc)
    return now - WINDOWS[window]


def filter_by_cutoff(articles: list[Article], cutoff: datetime) -> list[Article]:
    """Keep only articles published at or after the cutoff."""
    recent_articles = []
    for article in articles:
        if article.published_at >= cutoff:
            recent_articles.append(article)
    return recent_articles


def sort_newest_first(articles: list[Article]) -> list[Article]:
    """Sort articles by real publish timestamp, newest first."""
    return sorted(articles, key=lambda article: article.published_at, reverse=True)


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")


def first_sentence(text: str, limit: int = 240) -> str:
    """First sentence of a snippet, whitespace-normalized and length-capped."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    sentence = _SENTENCE_END_RE.split(text, 1)[0]
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rsplit(" ", 1)[0] + "…"


# --- Stage A relevance pre-filter (heuristic, no network) ----------------------

# Roundup/listicle title patterns - almost never single-company news.
ROUNDUP_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b\d+\s+(stocks|reasons|things|charts)\b",
        r"\bstocks?\s+to\s+(buy|watch|avoid|sell)\b",
        r"\bbest\b.*\bstocks?\b",
        r"\b(market movers|stocks on the move|movers & shakers|things to know)\b",
        r"\b(top|worst)\s+\d+\b",
    )
]

# High-signal outlets (kept; available for future down-ranking of the rest).
PREFERRED_SOURCES: set[str] = {
    "reuters",
    "bloomberg",
    "cnbc",
    "wsj",
    "wall street journal",
    "barron's",
    "barrons",
    "marketwatch",
    "the motley fool",
    "motley fool",
    "businesswire",
    "pr newswire",
    "sec",
    "company ir",
}

# Known low-signal content farms - always dropped.
BLOCKED_SOURCES: set[str] = {
    "insider monkey",
    "insidermonkey",
    "gurufocus",
    "24/7 wall st",
    "247wallst",
    "stockstory",
}


def is_relevant_headline(
    article: Article,
    company_name: str,
    ticker: str,
    aliases: tuple[str, ...] = (),
) -> bool:
    """Heuristic gate: company must be in the headline, no roundups, no junk sources.

    `aliases` are the names media actually uses for the ticker (LLM-derived
    per ticker via app.aliases; see names_for_ticker).
    """
    title = article.title.lower()

    core_name = _core_company_name(company_name)
    names = [core_name, *aliases]
    ticker_mentioned = re.search(rf"\b{re.escape(ticker.lower())}\b", title)
    name_mentioned = False
    for name in names:
        if len(name) > 2 and name in title:
            name_mentioned = True
            break
    if not ticker_mentioned and not name_mentioned:
        return False

    for pattern in ROUNDUP_PATTERNS:
        if pattern.search(title):
            return False

    return article.source.lower() not in BLOCKED_SOURCES


# --- Deduplication (exact URL + fuzzy title clusters) ---------------------------


# Query params that never identify an article; safe to strip when canonicalizing.
# Identity-bearing params (e.g. Finnhub's redirect links, https://finnhub.io/api/
# news?id=<hash>) must survive, so only strip known tracking noise.
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


def norm_title(t: str) -> str:
    """Normalize a headline for fuzzy matching (drop trailing source, punct, case)."""
    title = t.lower()
    title = re.sub(r"\s*[-|–—]\s*[^-|–—]+$", "", title)  # " - Reuters"
    title = re.sub(r"[^a-z0-9 ]", "", title)
    return re.sub(r"\s+", " ", title).strip()


def dedupe(articles: list[Article], threshold: int = 90) -> list[Article]:
    """Remove exact and near-duplicate articles, keeping the earliest-published
    representative of each cluster (the original report, not syndications)."""
    seen_urls: set[str] = set()
    kept: list[Article] = []
    kept_titles: list[str] = []
    oldest_first = sorted(articles, key=lambda article: article.published_at)
    for article in oldest_first:
        canonical_url = canon_url(article.url)
        if canonical_url in seen_urls:
            continue
        normalized_title = norm_title(article.title)
        duplicate_title = False
        for kept_title in kept_titles:
            if fuzz.token_set_ratio(normalized_title, kept_title) >= threshold:
                duplicate_title = True
                break
        if duplicate_title:
            continue
        seen_urls.add(canonical_url)
        kept.append(article)
        kept_titles.append(normalized_title)
    return kept


async def _finnhub_get(path: str, params: dict) -> httpx.Response:
    """GET a Finnhub endpoint with the API key and retry-with-backoff on 429."""
    params = {**params, "token": config.FINNHUB_API_KEY}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(4):
            response = await client.get(
                f"{config.FINNHUB_BASE_URL}{path}", params=params
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            await asyncio.sleep(2**attempt)
    raise RateLimitedError("Finnhub rate limit exceeded; try again shortly.")


# Finnhub silently truncates company-news to roughly the newest 250 items per
# request (the exact count wobbles, e.g. 249), so long windows on busy tickers
# must be paged backwards day by day. Batches smaller than the floor are taken
# as complete rather than truncated.
_FINNHUB_TRUNCATION_FLOOR = 200
_MAX_NEWS_PAGES = 60


async def _fetch_company_news(symbol: str, frm: date, to: date) -> list[dict]:
    """Fetch company-news for a date range, paging backwards past Finnhub's
    per-request item cap until the range start is covered."""
    start_date = frm
    end_date = to
    items: list[dict] = []
    for _ in range(_MAX_NEWS_PAGES):
        response = await _finnhub_get(
            "/company-news",
            {
                "symbol": symbol,
                "from": start_date.isoformat(),
                "to": end_date.isoformat(),
            },
        )
        batch = response.json() or []
        items.extend(batch)
        if not batch:
            break
        timestamps = []
        for item in batch:
            timestamp = item.get("datetime")
            if timestamp:
                timestamps.append(timestamp)
        if not timestamps:
            break
        oldest = datetime.fromtimestamp(min(timestamps), tz=timezone.utc).date()
        if oldest <= start_date or len(batch) < _FINNHUB_TRUNCATION_FLOOR:
            break  # range covered, or batch clearly wasn't truncated
        # Continue from the oldest day seen (its boundary overlap is removed by
        # URL dedupe). If a single day overflowed the cap, skip past it rather
        # than refetch the same newest-of-day items forever.
        if oldest < end_date:
            end_date = oldest
        else:
            end_date = end_date - timedelta(days=1)
        if end_date < start_date:
            break
    return items


async def resolve_ticker(query: str) -> tuple[str, str] | None:
    """Resolve a ticker or company name to (symbol, description); None if no match."""
    search_query = query.strip()
    if not search_query:
        return None
    key = search_query.lower()

    cached = ticker_cache.get(key)
    if cached is not None:
        # An empty tuple means a previous lookup found no ticker.
        if not cached:
            return None
        return cached

    if key in COMMON_TICKERS:
        result = (COMMON_TICKERS[key], search_query.title())
        ticker_cache.set(key, result, ttl=86400)
        return result

    response = await _finnhub_get("/search", {"q": search_query})
    data = response.json()
    results = data.get("result") or []
    result: tuple[str, str] | None = None

    # Prefer an exact symbol match, else the first plain-symbol hit, else the top hit.
    for match in results:
        if match.get("symbol", "").upper() == search_query.upper():
            result = (match["symbol"], match.get("description", match["symbol"]))
            break
    if result is None and _TICKER_RE.match(search_query):
        for match in results:
            if "." not in match.get("symbol", ""):
                result = (match["symbol"], match.get("description", match["symbol"]))
                break
    if result is None and results:
        match = results[0]
        result = (match["symbol"], match.get("description", match["symbol"]))

    ticker_cache.set(key, result or (), ttl=86400)
    return result


def _normalize_article(item: dict) -> Article | None:
    """Convert a raw Finnhub company-news item into an Article; None if unusable."""
    title = (item.get("headline") or "").strip()
    url = (item.get("url") or "").strip()
    timestamp = item.get("datetime")
    if not title or not url or not timestamp:
        return None
    return Article(
        title=title,
        url=url,
        source=(item.get("source") or "Unknown").strip(),
        published_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        raw_snippet=(item.get("summary") or "").strip(),
    )


# Google News RSS: free, keyless supplementary source aggregating thousands of
# outlets. Unofficial; article links are news.google.com redirects (unique per
# article, resolve in a browser).
_GOOGLE_NEWS_URL = "https://news.google.com/rss/search"


def parse_google_news_rss(xml_text: str) -> list[Article]:
    """Parse a Google News RSS feed into normalized Articles."""
    articles: list[Article] = []
    for item in ET.fromstring(xml_text).findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        publication_date = item.findtext("pubDate")
        source = (item.findtext("source") or "Google News").strip()
        if not title or not link or not publication_date:
            continue
        try:
            published = parsedate_to_datetime(publication_date)
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        # Titles carry a trailing " - Source" that duplicates the source field.
        if title.lower().endswith(f" - {source.lower()}"):
            title = title[: -(len(source) + 3)].rstrip()
        articles.append(
            Article(
                title=title,
                url=link,
                source=source,
                published_at=published.astimezone(timezone.utc),
                raw_snippet="",  # the RSS description is just a link, no text
            )
        )
    return articles


async def _fetch_google_news(
    search_name: str, ticker: str, window: str
) -> list[Article]:
    """Fetch Google News RSS for the company; failures are non-fatal."""
    if len(search_name) > 2:
        query = f'"{search_name}"'
    else:
        query = ticker
    days = max(1, math.ceil(WINDOWS[window].total_seconds() / 86400))
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(
                _GOOGLE_NEWS_URL,
                params={
                    "q": f"{query} when:{days}d",
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
            )
            response.raise_for_status()
        return parse_google_news_rss(response.text)
    except Exception:
        logger.warning("google news fetch failed; continuing without it", exc_info=True)
        return []


async def fetch_news(symbol: str, company_name: str, window: str) -> NewsResult:
    """Fetch, window-filter, relevance-filter, dedupe, and sort company news."""
    now = datetime.now(timezone.utc)
    cutoff = window_cutoff(window, now)

    cache_key = (symbol, window)
    cached = article_list_cache.get(cache_key)
    if cached is not None:
        fetched_at, articles = cached
        # The entry was cut at `fetched_at - window`, so it is a superset of the
        # right answer now: re-apply the current cutoff to drop the articles that
        # have aged out since. (Stories published since `fetched_at` are still
        # missing until the entry expires — that is what the ttl bounds.)
        return NewsResult(filter_by_cutoff(articles, cutoff), fetched_at)

    # One cached LLM call supplies the colloquial search term and match aliases.
    search_name, aliases = await names_for_ticker(symbol, company_name)
    # Finnhub takes whole dates; fetch by day, then filter on real timestamps.
    finnhub_items, google_articles = await asyncio.gather(
        _fetch_company_news(symbol, cutoff.date(), now.date()),
        _fetch_google_news(search_name, symbol, window),
    )
    articles = []
    for item in finnhub_items:
        article = _normalize_article(item)
        if article is not None:
            articles.append(article)
    articles.extend(google_articles)
    # Stage A pre-filter and dedupe keep junk out of the results.
    articles = filter_by_cutoff(articles, cutoff)
    relevant_articles = []
    for article in articles:
        if is_relevant_headline(article, company_name, symbol, aliases):
            relevant_articles.append(article)
    unique_articles = dedupe(relevant_articles)
    articles = sort_newest_first(unique_articles)

    article_list_cache.set(cache_key, (now, articles), ttl=_ttl_for_window(window))
    return NewsResult(articles, now)
