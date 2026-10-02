from datetime import UTC, datetime, timedelta
from decimal import Decimal

from conftest import AS_OF, CLOSE, make_market, make_source
from stock_digest.models import Benchmark, Claim, Digest, ExtendedHours, Publication, Topic
from stock_digest.render import render


def test_render_handles_non_iso_dates_and_details():  # B4, 4.6
    market = make_market(extended_hours=ExtendedHours(
        session="after-hours", price=Decimal("111"), base=Decimal("110"), absolute_change=Decimal("1"),
        percent_change=Decimal(100) / Decimal(110), observed_at=CLOSE + timedelta(hours=1)),
        benchmarks=[Benchmark(symbol="SPY", name="S&P 500 (SPY)", role="index", percent_change=Decimal("0.5"),
                              basis="completed regular-session close")])
    source = make_source(published="September 28, 2026", eligible_at=AS_OF, publisher="Reuters")
    digest = Digest(headline=Claim(text="{move}; example", sources=[1, 2]),
                    topics=[Topic(heading=Claim(text="Heading", sources=[2]),
                                  sentences=[Claim(text="Sentence.", sources=[2])])], coverage_notes=[])
    publication = Publication(market=market, generated_at=AS_OF, news_as_of=AS_OF, digest=digest,
                              sources=[source], news_source_ids=[2], coverage=["A note."], diagnostics=["internal"])
    output = render(publication, color=False, width=200)
    assert "2026-09-28 · Reuters: Example headline [2]" in output
    assert "[2] Reuters · “Example headline” · 2026-09-28 · https://example.com/a" in output
    assert "After-hours: $111.00, +$1.00 (+0.91%) vs. regular-session close" in output
    assert "S&P 500 (SPY) +0.5%" in output and "Generated:" in output
    assert "A note." in output and "internal" not in output
