from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from stock_digest.models import Finding, MarketSnapshot, ResearchEvidence, Source

# Monday 2026-09-28, completed session: open 13:30 UTC, close 20:00 UTC.
OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
AS_OF = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)


def make_market(**overrides) -> MarketSnapshot:
    fields = dict(
        ticker="NVDA", company="NVIDIA Corporation", exchange="XNAS", security_type="EQUITY",
        sector="Technology", price=Decimal("110"), comparison_close=Decimal("100"),
        absolute_change=Decimal("10"), percent_change=Decimal("10"),
        session_date=date(2026, 9, 28), comparison_date=date(2026, 9, 25),
        session_open=OPEN, session_close=CLOSE, observed_at=CLOSE, as_of=AS_OF,
        price_type="completed regular-session close",
        provenance=["https://finance.yahoo.com/quote/NVDA/"], limitations=[],
    )
    fields.update(overrides)
    return MarketSnapshot(**fields)


def make_source(id=2, url="https://example.com/a", title="Example headline", **overrides) -> Source:
    fields = dict(id=id, url=url, title=title, publisher="example.com", retrieved_at=AS_OF, raw_material=[title])
    fields.update(overrides)
    return Source(**fields)


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
