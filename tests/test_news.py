"""The news adapter around the ported Sentiment-Search retrieval (offline; Finnhub and Google faked)."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import stock_digest.news as news
from conftest import AS_OF, make_article, make_week
from stock_digest.models import CatalystScreen, HeadlineCatalyst
from stock_digest.social import retrieval


def test_finnhub_symbols():
    assert news.finnhub_symbol("brk-b") == "BRK.B"
    assert news.finnhub_symbol("BRK.B") == "BRK.B"
    assert news.finnhub_symbol("NVDA") == "NVDA"
    assert news.finnhub_symbol("SHOP.TO") == "SHOP.TO"


@pytest.fixture
def providers(monkeypatch):
    """Finnhub+Google via fetch_news, and Google alone, as switchable fakes."""
    state = {"finnhub": None, "calls": []}
    now = datetime.now(UTC)    # the Google-only path cuts the week against the real clock
    google = [retrieval.Article("Shopify to buy Beta", "https://news.example/shop", "Reuters", now - timedelta(hours=2), ""),
              retrieval.Article("Markets wrap: stocks drift", "https://news.example/wrap", "Reuters", now - timedelta(hours=1), ""),
              retrieval.Article("Shopify opens office", "https://news.example/old", "Reuters", now - timedelta(days=9), "")]

    async def fake_fetch_news(symbol, company, window):
        state["calls"].append(("fetch_news", symbol, company, window))
        if state["finnhub"]:
            raise state["finnhub"]
        return retrieval.NewsResult([make_article("Acme to buy Beta"), make_article("Acme beats estimates", source="Yahoo")], AS_OF)

    async def fake_names(symbol, company):
        return company.lower(), ()

    async def fake_google(search_name, ticker, window):
        state["calls"].append(("google", search_name, ticker, window))
        return google

    monkeypatch.setattr(news.config, "FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr(news.retrieval, "fetch_news", fake_fetch_news)
    monkeypatch.setattr(news.retrieval, "_fetch_google_news", fake_google)
    monkeypatch.setattr(news, "names_for_ticker", fake_names)
    return state


def week(ticker="BRK-B", company="Berkshire Hathaway Inc."):
    return asyncio.run(news.fetch_week(ticker, company))


def test_one_week_fetch_through_finnhub_and_google(providers):
    result = week()
    assert providers["calls"] == [("fetch_news", "BRK.B", "Berkshire Hathaway Inc.", "1w")]
    assert result.providers == "Finnhub and Google News" and len(result.articles) == 2 and not result.notes


def test_commas_are_dropped_from_the_company_name(providers):
    week(company="Tesla, Inc.")
    assert providers["calls"][0][2] == "Tesla Inc."


def _status_error(code):
    request = httpx.Request("GET", "https://finnhub.io/api/v1/company-news")
    return httpx.HTTPStatusError("refused", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize("error, phrase", [
    (_status_error(403), "free tier does not cover this listing"),
    (_status_error(401), "rejected the API key"),
    (retrieval.RateLimitedError("slow down"), "limiting requests"),
    (httpx.ConnectError("down"), "unavailable (ConnectError)"),
])
def test_finnhub_failures_fall_back_to_google_with_a_note(providers, error, phrase):
    providers["finnhub"] = error
    result = week("SHOP.TO", "Shopify Inc.")
    # Same filters as retrieval.fetch_news: the company named in the title, the last 7 days only.
    assert result.providers == "Google News" and [a.title for a in result.articles] == ["Shopify to buy Beta"]
    assert phrase in result.notes[0] and "Google News only" in result.notes[0]
    assert providers["calls"][-1] == ("google", "shopify inc.", "SHOP.TO", "1w")


def test_missing_key_skips_finnhub(providers, monkeypatch):
    monkeypatch.setattr(news.config, "FINNHUB_API_KEY", "")
    result = week()
    assert [call[0] for call in providers["calls"]] == ["google"]
    assert "FINNHUB_API_KEY is not set" in result.notes[0]


def test_rows_and_gate_keep_supplied_articles_once():
    old, deal, copy = (make_article("Acme opens office", 30), make_article("Acme to buy Beta", 3),
                       make_article("Acme agrees Beta deal", 2))
    fetched = make_week(old, deal, copy)
    rows = news.article_rows(fetched, AS_OF - timedelta(hours=24))
    assert [(row["id"], row["title"]) for row in rows] == [("a1", copy.title), ("a2", deal.title)]

    def item(ids, headline="Acme agrees to buy Beta"):
        return HeadlineCatalyst(article_ids=ids, headline=headline, catalyst_type="merger_acquisition",
                                status="agreed", why="  a large   deal ")

    screen = CatalystScreen(catalysts=[item(["a2", "a1", "a2"]), item(["a3"], "outside the window"),
                                       item(["a1"], "a repeat"), item(["a2", "a9"], "another repeat")])
    kept, notes = news.screen_gate(screen, fetched, rows)
    assert len(kept) == 1 and kept[0].articles == [deal, copy] and kept[0].item.why == "a large deal"
    assert notes == ["Screen item dropped (1): cited no supplied article.",
                     "Screen item dropped (2): repeated another item's articles."]
    limited, notes = news.screen_gate(CatalystScreen(catalysts=[item(["a1"]), item(["a2"], "second")]), fetched, rows, 1)
    assert len(limited) == 1 and notes == ["Screen returned 2 items; the first 1 were kept."]
