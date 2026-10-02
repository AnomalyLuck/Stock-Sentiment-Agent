from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd

import stock_digest.catalysts as catalysts
from conftest import make_market


class FakeTicker:
    options = ("2026-10-16", "2026-10-23", "2026-10-30")

    def __init__(self, symbol):
        pass

    def option_chain(self, expiry):
        assert expiry == "2026-10-23"
        frame = lambda bid, ask: pd.DataFrame({"strike": [105.0, 110.0, 115.0], "bid": [bid] * 3, "ask": [ask] * 3,
                                               "lastPrice": [0.0] * 3, "lastTradeDate": [pd.NaT] * 3})
        return SimpleNamespace(calls=frame(4.8, 5.2), puts=frame(3.8, 4.2))


def test_implied_move_uses_first_expiry_after_release(monkeypatch):
    monkeypatch.setattr(catalysts.yf, "Ticker", FakeTicker)
    move = catalysts.fetch_implied_move(make_market(), date(2026, 10, 20), "after_close")
    assert move["expiry"] == "2026-10-23" and move["strike"] == "110.0"
    assert move["percent"] == "8.2"  # (5.0 + 4.0) / 110
    assert "not only the earnings reaction" in move["methodology"]


import httpx

from conftest import AS_OF
from stock_digest.manager import structured_evidence


def test_edgar_filings_parse_items_and_eastern_acceptance(monkeypatch):
    def handler(request):
        if request.url.path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}})
        assert request.headers["User-Agent"] == "Test Agent test@example.com"
        return httpx.Response(200, json={"filings": {"recent": {
            "form": ["8-K", "4", "8-K"], "filingDate": ["2026-09-28", "2026-09-27", "2026-08-01"],
            "acceptanceDateTime": ["2026-09-28T07:05:12.000Z", "2026-09-27T18:00:00.000Z", "2026-08-01T16:00:00.000Z"],
            "items": ["2.02,9.01", "", "8.01"], "accessionNumber": ["0001045810-26-000080", "x", "y"],
            "primaryDocument": ["nvda-8k.htm", "f4.xml", "old.htm"], "primaryDocDescription": ["8-K", "4", "8-K"]}}})

    real_client = httpx.Client
    monkeypatch.setattr(catalysts.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    filings = catalysts._edgar_filings("NVDA", date(2026, 9, 21), "Test Agent test@example.com")
    assert len(filings) == 1  # Form 4 is not a tracked type; the August filing is outside the window
    filing = filings[0]
    assert filing["title"] == "8-K: Results of operations and financial condition"
    assert filing["accepted_at"] == "2026-09-28T07:05:12-04:00"  # EDGAR's "Z" is really Eastern time
    assert filing["url"].endswith("/1045810/000104581026000080/nvda-8k.htm")


def test_structured_evidence_builds_timed_packets():
    market = make_market()
    data = {
        "retrieved_at": AS_OF.isoformat(), "symbol": "NVDA",
        "analyst_actions": [{"at": "2026-09-28T11:30:00+00:00", "firm": "Acme Securities", "action": "up",
                             "to_grade": "Buy", "from_grade": "Hold", "price_target_action": "Raises",
                             "price_target": 150.0, "prior_price_target": 120.0}],
        "estimates": None, "price_targets": None, "revenue_history": [],
        "earnings": {"next": None, "last_report": {"at": "2026-09-27T20:05:00+00:00", "reported_eps": 1.1,
                                                    "eps_estimate": 1.0, "surprise_percent": 10.0}},
        "filings": [{"form": "8-K", "date": "2026-09-28", "accepted_at": "2026-09-28T07:05:12-04:00",
                     "title": "8-K: Results of operations and financial condition", "items": ["2.02", "9.01"],
                     "url": "https://www.sec.gov/Archives/edgar/data/1/2/a.htm", "provider": "SEC EDGAR"}],
        "insider": [], "dividendDate": None, "exDividendDate": None,
    }
    sources, packets, ids = structured_evidence(data, market, 3, 2)
    by_purpose = {p["research_purpose"]: p for p in packets}
    analyst = by_purpose["structured_analyst"]
    assert "Acme Securities upgraded to Buy from Hold; price target raised to $150.00 from $120.00" in analyst["supporting_material"]
    assert analyst["later_than_price"] is False and analyst["move_relevance"] == "direct"
    filing = by_purpose["structured_filing"]
    assert filing["catalyst_type"] == "earnings" and filing["later_than_price"] is False
    assert by_purpose["structured_earnings"]["supporting_material"].startswith("Reported quarterly results")
    assert [s.id for s in sources] == list(range(2, 2 + len(sources)))
