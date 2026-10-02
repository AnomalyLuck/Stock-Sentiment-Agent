from decimal import Decimal

from conftest import AS_OF, make_market, make_source
from stock_digest.models import Claim, Digest, Publication, Topic, move_phrase
from stock_digest.view import publication_view


def _publication(**overrides):
    source = make_source(id=2, publisher="Reuters", published="2026-09-28", eligible_at=AS_OF,
                         price_timing_unknown=True, unconfirmed_report=True)
    bad = make_source(id=3, url="javascript:alert(1)", title="Bad", eligible_at=AS_OF)
    digest = Digest(
        headline=Claim(text="NVIDIA is {move} after a deal report", sources=[1, 2]),
        topics=[Topic(heading=Claim(text="Deal talks", sources=[2]),
                      sentences=[Claim(text="Acme is weighing a bid.", sources=[2], support_status="unsupported")])],
        earnings_preview=Topic(heading=Claim(text="Earnings ahead", sources=[1]),
                               sentences=[Claim(text="Results are due Oct. 20.", sources=[1])]),
        coverage_notes=[])
    fields = dict(market=make_market(), generated_at=AS_OF, news_as_of=AS_OF, digest=digest, sources=[source, bad],
                  news_source_ids=[2], coverage=["A note."],
                  earnings={"event_date": "2026-10-20", "event_status": "confirmed", "event_timing": "after_close",
                            "metrics": {"eps": {"estimate": {"display": "$2.47", "analysts": 43},
                                                "comparisons": {"YoY": {"percent_change": "90.3"}}}},
                            "options_implied_move": {"percent": "8.2", "expiry": "2026-10-23"}})
    fields.update(overrides)
    return Publication(**fields)


def test_move_phrase():
    assert move_phrase(make_market()) == "up 10.00%"
    down = make_market(price=Decimal("90"), absolute_change=Decimal("-10"), percent_change=Decimal("-10"))
    assert move_phrase(down) == "down 10.00%"


def test_view_labels_and_order():
    view = publication_view(_publication(), "https://platform.openai.com/traces/trace?trace_id=x")
    assert view["headline"]["text"] == "NVIDIA is up 10.00% after a deal report"
    kinds = {flag["kind"] for flag in view["headline"]["flags"]}
    assert kinds == {"timing", "unconfirmed"}
    assert [s["kind"] for s in view["sections"]] == ["topic", "earnings"]  # earnings after the news sections
    sentence = view["sections"][0]["sentences"][0]
    assert {flag["kind"] for flag in sentence["flags"]} == {"unsupported", "timing", "unconfirmed"}
    assert view["earnings"]["stats"][0] == {"label": "EPS estimate", "value": "$2.47", "detail": "43 analysts · +90.3% vs. year ago"}
    assert view["earnings"]["stats"][1] == {"label": "Options-implied move", "value": "±8.2%", "detail": "through Oct 23"}
    assert view["price"] == "$110.00" and view["direction"] == "up" and view["session"].startswith("At close")


def test_view_drops_non_http_links_and_handles_fallback():
    view = publication_view(_publication(digest=None, coverage=["The narrative did not pass review. Only retrieved headlines are shown."]))
    assert next(s for s in view["sources"] if s["id"] == 3)["url"] is None
    assert view["headline"] is None and view["sections"] == [] and view["earnings"] is None
    assert view["notice"].startswith("The narrative did not pass review")


def test_headings_are_clean_and_flags_show_once_per_section():
    publication = _publication()
    topic = publication.digest.topics[0]
    topic.sentences.append(Claim(text="Talks are early.", sources=[2]))
    view = publication_view(publication)
    heading = view["sections"][0]["heading"]
    assert heading["sources"] == [] and heading["flags"] == []
    second = view["sections"][0]["sentences"][1]
    assert second["flags"] == [] and second["sources"] == [2]  # timing/unconfirmed already shown once
