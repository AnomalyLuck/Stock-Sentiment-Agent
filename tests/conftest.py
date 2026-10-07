import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from stock_digest.models import Finding, MarketSnapshot, ResearchEvidence, Source
from stock_digest.news import WeekNews
from stock_digest.social.retrieval import Article

# Monday 2026-09-28, completed session: open 13:30 UTC, close 20:00 UTC.
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)


def make_market(**overrides) -> MarketSnapshot:
    fields = dict(
        ticker="NVDA", company="NVIDIA Corporation", exchange="XNAS", security_type="EQUITY",
        sector="Technology", price=Decimal("110"), comparison_close=Decimal("100"),
        absolute_change=Decimal("10"), percent_change=Decimal("10"),
        session_date=date(2026, 9, 28), comparison_date=date(2026, 9, 25),
        comparison_session_close=FRIDAY_CLOSE, session_open=OPEN, session_close=CLOSE, observed_at=CLOSE, as_of=AS_OF,
        price_type="completed regular-session close",
        provenance=["https://finance.yahoo.com/quote/NVDA/"], limitations=[],
    )
    fields.update(overrides)
    return MarketSnapshot(**fields)


def make_source(id=2, url="https://example.com/a", title="Example headline", **overrides) -> Source:
    fields = dict(id=id, url=url, title=title, publisher="example.com", retrieved_at=AS_OF, raw_material=[title])
    fields.update(overrides)
    return Source(**fields)


def make_article(title: str, hours_before_as_of: float = 1, source: str = "Reuters", url: str | None = None,
                 snippet: str = "") -> Article:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return Article(title=title, url=url or f"https://news.example/{slug}", source=source,
                   published_at=AS_OF - timedelta(hours=hours_before_as_of), raw_snippet=snippet)


def make_week(*articles: Article, providers: str = "Finnhub and Google News", notes=()) -> WeekNews:
    """A fetched week as news.fetch_week returns it: newest first."""
    ordered = sorted(articles, key=lambda article: article.published_at, reverse=True)
    return WeekNews(ordered, AS_OF, providers, list(notes))


def make_finding(url="https://example.com/a", **overrides) -> Finding:
    fields = dict(summary="Something happened.", url=url, reported_excerpt=None, published=None, updated=None,
                  timestamp_basis=None, content_kind="reporting", event_date=None, story_key="")
    fields.update(overrides)
    return Finding(**fields)


def evidence(*findings) -> ResearchEvidence:
    return ResearchEvidence(query="q", status="findings", findings=list(findings), coverage_issues=[])


@pytest.fixture
def market():
    return make_market()
