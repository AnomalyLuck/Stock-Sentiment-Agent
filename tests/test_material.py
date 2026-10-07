"""Material company news, offline: the week's headlines screened into entries (no fetch or model calls).

Numbers in test names refer to the specification's acceptance criteria that still apply.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import stock_digest.material.pipeline as pipeline
from conftest import make_week
from stock_digest.market import InputError
from stock_digest.material.gates import display_moment, status_language
from stock_digest.material.identity import _clarify, parse_symbol, resolve_timezone
from stock_digest.material.models import Issuer
from stock_digest.material.render import markdown, material_view
from stock_digest.models import CatalystScreen, HeadlineCatalyst
from stock_digest.social.retrieval import Article

RUN_AT = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
MEGACAP = Issuer(ticker="NVDA", symbol="NVDA", exchange="NasdaqGS", company="NVIDIA Corporation",
                 market_cap=4.5e12, currency="USD")


def recent(title: str, hours_ago: float, source: str = "Reuters") -> Article:
    """An article dated against the real clock: run_material's window ends now."""
    slug = "-".join(title.lower().split())[:40]
    return Article(title=title, url=f"https://news.example/{slug}", source=source,
                   published_at=datetime.now(UTC) - timedelta(hours=hours_ago), raw_snippet="")


BUYBACK = recent("NVIDIA board approves $60 billion buyback", 30, "Reuters")
BUYBACK_COPY = recent("Nvidia unveils $60B repurchase plan", 29, "CNBC")
LAUNCH = recent("NVIDIA launches Rubin data-center platform", 5, "Bloomberg")
TALKS = recent("Nvidia in talks to invest in Beta, sources say", 60, "Bloomberg")
LISTICLE = recent("10 AI stocks to buy now, including Nvidia", 2, "Motley Fool")
LAST_WEEK = recent("Nvidia opens a new office", 24 * 8, "Business Wire")
WEEK = (BUYBACK, BUYBACK_COPY, LAUNCH, TALKS, LISTICLE, LAST_WEEK)


def item(article_ids, headline, catalyst_type, status, why=""):
    return HeadlineCatalyst(article_ids=article_ids, headline=headline, catalyst_type=catalyst_type,
                            status=status, why=why)


def default_screen(rows):
    ids = {row["title"]: row["id"] for row in rows}
    return CatalystScreen(catalysts=[
        item([ids[BUYBACK.title], ids[BUYBACK_COPY.title]], "NVIDIA authorizes a $60 billion buyback", "buyback",
             "authorized", "returns cash to shareholders"),
        item([ids[LAUNCH.title]], "NVIDIA launches its Rubin data-center platform", "product", "launched",
             "a new flagship product line"),
        item(["a99"], "Invented development", "other", "reported"),                     # cites nothing supplied
        item([ids[BUYBACK_COPY.title]], "Duplicate of the buyback", "buyback", "authorized"),  # reuses an article
    ])


class FakeResult:
    def __init__(self, final_output):
        self.final_output = final_output

    def final_output_as(self, _cls):
        return self.final_output


def install_fakes(monkeypatch, articles=WEEK, screen=default_screen, notes=()):
    seen = {"calls": [], "payloads": []}

    def fake_resolve(ticker, exchange=None):
        seen["calls"].append("resolve")
        return MEGACAP

    async def fake_week(ticker, company):
        seen["calls"].append("fetch")
        week = make_week(*articles, notes=notes)
        week.fetched_at = datetime.now(UTC)
        return week

    async def fake_run(agent, payload, **kwargs):
        seen["calls"].append(agent.name)
        payload = json.loads(payload)
        seen["payloads"].append(payload)
        if isinstance(screen, Exception):
            raise screen
        return FakeResult(screen(payload["articles"]))

    monkeypatch.setattr(pipeline, "resolve_issuer", fake_resolve)
    monkeypatch.setattr(pipeline, "fetch_week", fake_week)
    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=fake_run))
    return seen


def run(request=None, **shared):
    request = request or pipeline.MaterialRequest(ticker="NVDA", timezone="America/New_York")
    return asyncio.run(pipeline.run_material(request, "sk-test", {"research": "m", "writer": "m"}, lambda m: None, **shared))


# ---------------------------------------------------------------- end to end with fakes

def test_end_to_end_screens_the_week_into_entries(monkeypatch):
    seen = install_fakes(monkeypatch)
    result = run()
    assert seen["calls"] == ["resolve", "fetch", "MaterialScreen"]
    # The screen reads titles in the 7-day window only, newest first, and is told the language.
    payload = seen["payloads"][0]
    assert [row["title"] for row in payload["articles"]] == [LISTICLE.title, LAUNCH.title, BUYBACK_COPY.title,
                                                             BUYBACK.title, TALKS.title]
    assert set(payload["articles"][0]) == {"id", "title", "source", "published"} and payload["language"] == "English"
    # Newest first; the invented and the duplicate items are dropped by the gate.
    assert [e.headline for e in result.entries] == ["NVIDIA launches its Rubin data-center platform",
                                                    "NVIDIA authorizes a $60 billion buyback"]
    assert any("cited no supplied article" in note for note in result.diagnostics)
    assert any("repeated another item's articles" in note for note in result.diagnostics)
    buyback = result.entries[1]
    assert buyback.verification == "headline_only" and buyback.category == "buyback"
    assert buyback.status_label == "authorized" and not buyback.reported
    assert [s["publisher"] for s in buyback.sources] == ["Reuters", "CNBC"]
    assert buyback.first_reported_at == BUYBACK.published_at.isoformat(timespec="seconds")   # the earliest article
    # 12: every entry has a source, a date, a rationale and a status.
    for entry in result.entries:
        assert entry.sources and entry.sources[0]["url"].startswith("https://") and entry.date and entry.why and entry.status
    text = markdown(result)
    rows = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith("| Date")]
    assert len(rows) == 2 and "10 AI stocks" not in text and "the articles were not opened" in text
    view = material_view(result)
    assert view["entries"][0]["verification"] == "headline_only" and view["entries"][1]["category"] == "buyback"


def test_reported_developments_stay_attributed(monkeypatch):
    def screen(rows):
        talks = next(row["id"] for row in rows if row["title"] == TALKS.title)
        return CatalystScreen(catalysts=[item([talks], "NVIDIA reportedly in talks to invest in Beta", "rumor",
                                              "reported_talks", "a possible strategic investment")])
    install_fakes(monkeypatch, screen=screen)
    entry = run().entries[0]
    assert entry.reported and entry.status_label == "reported talks"
    assert entry.why == "Reported by Bloomberg: a possible strategic investment"


def test_max_results_is_disclosed(monkeypatch):
    install_fakes(monkeypatch)
    result = run(pipeline.MaterialRequest(ticker="NVDA", timezone="UTC", max_results=1))
    assert len(result.entries) == 1 and result.qualified_count == 2 and len(result.qualified_entries) == 2
    assert "Limited to the 1 most recent of 2 qualifying developments" in markdown(result)


def test_11_no_qualifying_results_is_a_concise_empty_state(monkeypatch):
    install_fakes(monkeypatch, screen=lambda rows: CatalystScreen(catalysts=[]))
    result = run()
    assert result.outcome == "none_found"
    assert "> No qualifying material, company-led developments were found for NVDA in the specified window." in markdown(result)


def test_no_headlines_skips_the_screen_and_says_so(monkeypatch):
    seen = install_fakes(monkeypatch, articles=())
    result = run()
    assert "MaterialScreen" not in seen["calls"] and result.outcome == "none_found"
    assert "No headlines naming the company were found" in result.coverage_note


def test_11_screen_failure_is_a_research_limitation_not_no_news(monkeypatch):
    install_fakes(monkeypatch, screen=RuntimeError("model down"))
    with pytest.raises(pipeline.ResearchUnavailable, match="research limitation, not a finding"):
        run()


def test_provider_fallback_is_disclosed(monkeypatch):
    install_fakes(monkeypatch, notes=["Finnhub's free tier does not cover this listing, so news came from Google News only."])
    assert "Google News only" in run().coverage_note


def test_shared_issuer_and_news_are_not_fetched_again(monkeypatch):
    seen = install_fakes(monkeypatch, screen=lambda rows: CatalystScreen(catalysts=[
        item([rows[0]["id"]], "NVIDIA launches Rubin", "product", "launched", "new product")]))

    async def go():
        async def issuer():
            return MEGACAP

        async def news():
            return make_week(LAUNCH)
        request = pipeline.MaterialRequest(ticker="NVDA")
        return await pipeline.run_material(request, "sk-test", {"research": "m"}, lambda m: None,
                                           issuer=issuer(), news=news())

    result = asyncio.run(go())
    assert [e.headline for e in result.entries] == ["NVIDIA launches Rubin"]
    assert seen["calls"] == ["MaterialScreen"]


# ---------------------------------------------------------------- 7: status wording

def test_7_reported_approval_stays_reported():
    text, problem = status_language("Beijing approved NVIDIA's chip sales to major customers.", "under_consideration", "Reuters")
    assert problem
    text, problem = status_language("Reuters reported Beijing may allow major customers to buy NVIDIA chips; "
                                    "no approval has been granted.", "under_consideration", "Reuters")
    assert problem is None and text.startswith("Reuters reported")
    text, problem = status_language("Beijing may let customers buy the chips, which could reopen sales.",
                                    "under_consideration", "Reuters")
    assert problem is None and text.startswith("Reported by Reuters:")
    assert status_language("The board approved a $60B buyback.", "authorized", "NVIDIA") == (
        "The board approved a $60B buyback.", None)


# ---------------------------------------------------------------- 14: input and time

def test_14_ticker_forms_and_ambiguity():
    assert parse_symbol("nvda") == ("NVDA", None)
    assert parse_symbol("NASDAQ:MSFT") == ("MSFT", "NASDAQ")
    assert parse_symbol("brk.b") == ("BRK-B", None)
    assert parse_symbol("TSX:SHOP") == ("SHOP.TO", None)
    assert parse_symbol("HKEX:700") == ("0700.HK", None)
    assert parse_symbol("SHOP.TO") == ("SHOP.TO", None)
    for bad in ("", "NOT A TICKER", "FOO:BAR", "TOOLONGSYMBOL"):
        with pytest.raises(InputError):
            parse_symbol(bad)
    with pytest.raises(InputError):
        parse_symbol("NYSE:MSFT", exchange="NASDAQ")
    quotes = [{"symbol": "ABC", "quoteType": "EQUITY", "longname": "Alpha Corp", "exchDisp": "NYSE"},
              {"symbol": "ABC.TO", "quoteType": "EQUITY", "longname": "Beta Ltd", "exchDisp": "Toronto"}]
    assert "Did you mean" in _clarify("ABC.L", quotes) and "Beta Ltd" in _clarify("ABC.L", quotes)


def test_14_timezones():
    assert resolve_timezone("Asia/Tokyo") == "Asia/Tokyo"
    with pytest.raises(InputError):
        resolve_timezone("Mars/Base")
    assert display_moment(RUN_AT, "Asia/Tokyo") == "Oct 1, 2026 03:00 JST"
    assert display_moment(RUN_AT, "America/New_York") == "Sep 30, 2026 14:00 EDT"
