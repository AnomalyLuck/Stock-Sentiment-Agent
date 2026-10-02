from datetime import UTC, date, datetime
from decimal import Decimal

from conftest import AS_OF, make_market, make_source
from stock_digest.earnings import earnings_context, upcoming_earnings
from stock_digest.manager import _base_packet

EVENT = {"source_id": 2, "event_date": "2026-10-20", "event_status": "scheduled", "event_timing": "after_close",
         "supporting_material": "Yahoo calendar"}


def _metric(kind, year, quarter, value, metric="revenue", basis="GAAP"):
    unit = "USD" if metric == "revenue" else "USD/share"
    return {"metric": metric, "kind": kind, "fiscal_year": year, "fiscal_quarter": quarter, "basis": basis,
            "value": str(value), "unit": unit, "supporting_quote": "q"}


def test_structured_estimates_compute_comparisons(market):
    structured = {"analysis_source_id": 3, "financials_source_id": 4,
                  "estimates": {"eps": {"avg": 2.5, "yearAgoEps": 1.25, "numberOfAnalysts": 40},
                                "revenue": {"avg": 110e9, "yearAgoRevenue": 55e9, "numberOfAnalysts": 41},
                                "last_quarter": {"period_end": "2026-07-31", "eps_actual": 2.0}},
                  "revenue_history": [{"period_end": "2026-07-31", "revenue": 100e9}]}
    context = earnings_context(EVENT, [], [], market, structured)
    eps, revenue = context["metrics"]["eps"], context["metrics"]["revenue"]
    assert eps["comparisons"]["YoY"]["percent_change"] == "100.0"
    assert eps["comparisons"]["QoQ"]["percent_change"] == "25.0"
    assert revenue["comparisons"]["QoQ"]["percent_change"] == "10.0"
    assert revenue["comparisons"]["QoQ"]["baseline"]["source_id"] == 4


def test_research_estimate_must_be_for_the_quarter_being_reported(market):  # B12
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, research_purpose="earnings_history", earnings_metrics=[
        _metric("actual", 2026, 2, 90e9), _metric("consensus", 2027, 1, 150e9), _metric("consensus", 2026, 3, 100e9)])
    context = earnings_context(EVENT, [packet], [source], market)
    assert context["metrics"]["revenue"]["estimate"]["fiscal_quarter"] == 3


def test_revenue_unit_errors_are_rejected(market):  # B13
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, research_purpose="earnings_estimates", earnings_date="2026-10-20",
                          earnings_metrics=[_metric("consensus", 2026, 3, "46.7")])
    context = earnings_context(EVENT, [packet], [source], market)
    assert context["metrics"]["revenue"]["estimate"] is None


def test_options_observation_uses_new_york_date():  # B16
    # 02:00 UTC on Sep 29 is still Sep 28 in New York; a Sep 29 observation is in the future.
    market = make_market(as_of=datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, research_purpose="earnings_options", options_implied_move={
        "percent": "8", "earnings_date": "2026-10-20", "observed_date": "2026-09-29", "expiry": "2026-10-23",
        "methodology": None, "supporting_quote": "q"})
    context = earnings_context(EVENT, [packet], [source], market)
    assert context["options_implied_move"] is None


def test_issuer_confirmation_outranks_calendar(market):
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, earnings_date="2026-10-21", content_kind="dated_announcement",
                          confirmation_status="confirmed", supporting_material="IR release")
    event = upcoming_earnings([packet], market, [EVENT])
    assert event["event_status"] == "confirmed" and event["event_date"] == "2026-10-21"
    assert upcoming_earnings([], market, [EVENT])["event_status"] == "scheduled"
