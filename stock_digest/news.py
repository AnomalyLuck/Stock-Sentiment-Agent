"""News for the digest and material news: one fetch of the ported Sentiment-Search retrieval.

`social/retrieval.py` is that repository's `app/retrieval.py` unchanged: Finnhub company-news
(paged past its ~250-item cap) plus Google News RSS, a company-in-the-headline filter, fuzzy
dedupe and a short cache. This module is the adapter around it. One 7-day fetch serves a run:
the digest screens the articles since the previous close (at least the last 24 hours) and
material news screens the whole week. The screens read titles only; `screen_gate` re-checks
every item they return against the articles they were given.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .material.identity import display_ticker
from .models import CatalystScreen, HeadlineCatalyst
from .social import config, retrieval
from .social.aliases import names_for_ticker
from .social.social import _event_loop

WEEK = "1w"
CATALYST_HOURS = 24
MAX_ROWS = 300          # newest first; more than a screen should read in one call
MAX_ARTICLES = 3        # per screened development


@dataclass
class WeekNews:
    articles: list[retrieval.Article]   # newest first, deduplicated, company named in each headline
    fetched_at: datetime
    providers: str                      # "Finnhub and Google News", or "Google News" after a fallback
    notes: list[str] = field(default_factory=list)   # reader-facing coverage limits


@dataclass
class Screened:
    item: HeadlineCatalyst
    articles: list[retrieval.Article]   # the supplied articles it cites, in the screen's order


def finnhub_symbol(ticker: str) -> str:
    """Finnhub writes US share classes with a dot (BRK.B), Yahoo with a dash (BRK-B)."""
    return display_ticker(ticker.strip().upper())


async def fetch_week(ticker: str, company: str) -> WeekNews:
    """The run's one news fetch: the last 7 days of headlines that name the company.

    Runs on the social tab's shared event loop, because the alias lookup inside retrieval keeps
    one OpenAI client per process and each digest run opens a new loop.
    """
    future = asyncio.run_coroutine_threadsafe(_week(finnhub_symbol(ticker), company), _event_loop())
    return await asyncio.wrap_future(future)


async def _week(symbol: str, company: str) -> WeekNews:
    # Retrieval strips legal suffixes but not commas ("Tesla, Inc." would match no headline).
    name = " ".join(company.replace(",", " ").split()) or symbol
    if not config.FINNHUB_API_KEY:
        note = "FINNHUB_API_KEY is not set, so news came from Google News only."
    else:
        try:
            result = await retrieval.fetch_news(symbol, name, WEEK)
            return WeekNews(result.articles, result.fetched_at, "Finnhub and Google News")
        except retrieval.RateLimitedError:
            note = "Finnhub was limiting requests, so news came from Google News only."
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            note = ("Finnhub's free tier does not cover this listing, so news came from Google News only." if status == 403
                    else "Finnhub rejected the API key, so news came from Google News only." if status == 401
                    else f"Finnhub returned HTTP {status}, so news came from Google News only.")
        except (httpx.HTTPError, ValueError) as exc:
            note = f"Finnhub was unavailable ({type(exc).__name__}), so news came from Google News only."
    now = datetime.now(timezone.utc)
    return WeekNews(await _google_week(symbol, name, now), now, "Google News", [note])


async def _google_week(symbol: str, name: str, now: datetime) -> list[retrieval.Article]:
    """retrieval.fetch_news without the Finnhub half (same filter, dedupe and order; not cached)."""
    search_name, aliases = await names_for_ticker(symbol, name)
    articles = await retrieval._fetch_google_news(search_name, symbol, WEEK)
    articles = retrieval.filter_by_cutoff(articles, retrieval.window_cutoff(WEEK, now))
    articles = [a for a in articles if retrieval.is_relevant_headline(a, name, symbol, aliases)]
    return retrieval.sort_newest_first(retrieval.dedupe(articles))


def catalyst_since(market, now: datetime) -> datetime:
    """Start of the digest's catalyst window: the last 24 hours, or since the close the move is
    measured from when that is earlier (a Monday run covers Friday's after-hours news)."""
    start = now - timedelta(hours=CATALYST_HOURS)
    close = getattr(market, "comparison_session_close", None)
    return min(start, close) if close else start


def article_rows(news: WeekNews, since: datetime) -> list[dict]:
    """The articles published since `since`, newest first, as a screen reads them: titles only.

    Ids index the whole week (a1 is the newest article), so both screens name articles alike.
    """
    rows = []
    for index, article in enumerate(news.articles):
        if article.published_at >= since:
            rows.append({"id": f"a{index + 1}", "title": article.title, "source": article.source,
                         "published": article.published_at.isoformat(timespec="minutes")})
    return rows[:MAX_ROWS]


def screen_gate(screen: CatalystScreen, news: WeekNews, rows: list[dict],
                limit: int | None = None) -> tuple[list[Screened], list[str]]:
    """Keep items that cite supplied articles, each article used once. Returns (kept, notes)."""
    supplied = {row["id"] for row in rows}
    used: set[str] = set()
    kept: list[Screened] = []
    dropped = Counter()
    for item in screen.catalysts:
        ids = [i for i in dict.fromkeys(item.article_ids) if i in supplied and i not in used][:MAX_ARTICLES]
        if not ids:
            dropped["cited no supplied article" if not supplied.intersection(item.article_ids)
                    else "repeated another item's articles"] += 1
            continue
        used.update(ids)
        item.headline = " ".join(item.headline.split())
        item.why = " ".join(item.why.split())
        kept.append(Screened(item, [news.articles[int(i[1:]) - 1] for i in ids]))
    notes = [f"Screen item dropped ({count}): {reason}." for reason, count in dropped.items()]
    if limit is not None and len(kept) > limit:
        notes.append(f"Screen returned {len(kept)} items; the first {limit} were kept.")
        kept = kept[:limit]
    return kept, notes
