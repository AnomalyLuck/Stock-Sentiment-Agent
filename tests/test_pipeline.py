"""Offline end-to-end runs of run_digest with Yahoo, the news fetch and OpenAI replaced by fakes."""
import asyncio
import json
from types import SimpleNamespace

from agents import ModelBehaviorError

import stock_digest.manager as manager
from conftest import AS_OF, make_article, make_market, make_week
from stock_digest.models import CatalystScreen, Claim, Digest, HeadlineCatalyst, Topic, VerificationResult

DEAL = make_article("Acme to buy Beta for $2.1 billion", hours_before_as_of=14, source="Reuters",
                    url="https://news.example/acme-deal", snippet="Acme agreed to buy Beta in cash.")
SATURDAY = make_article("Acme names a new finance chief", hours_before_as_of=50, source="CNBC")
OLD = make_article("Acme opens a new office", hours_before_as_of=24 * 6, source="Business Wire")
LIST = make_article("3 chip stocks to buy now, including Acme", hours_before_as_of=2, source="Motley Fool")


class FakeResult:
    def __init__(self, final_output, raw_responses=()):
        self.final_output, self.raw_responses = final_output, list(raw_responses)

    def final_output_as(self, _cls):
        return self.final_output


def no_findings():
    text = "```json\n" + json.dumps({"query": "q", "status": "no_relevant_results", "findings": []}) + "\n```"
    return FakeResult(text, [SimpleNamespace(output=[{"type": "web_search_call", "status": "completed",
                                                      "action": {"type": "search", "sources": []}}])])


def deal_screen(payload):
    """Keep the deal (by id), skip the stock list."""
    ids = {row["title"]: row["id"] for row in payload["articles"]}
    return CatalystScreen(catalysts=[HeadlineCatalyst(
        article_ids=[ids[DEAL.title]], headline="Acme agrees to buy Beta for $2.1 billion",
        catalyst_type="merger_acquisition", status="agreed", why="a large cash acquisition")])


def patch_providers(monkeypatch, writer_output, verdict=None, screen=deal_screen):
    async def fake_market(ticker):
        return make_market(), {}

    async def fake_week(ticker, company):
        seen["fetches"] += 1
        return make_week(DEAL, SATURDAY, OLD, LIST)

    def fake_catalysts(market, profile):
        return {"retrieved_at": AS_OF.isoformat(), "symbol": "NVDA", "diagnostics": [], "earnings": {},
                "estimates": None, "revenue_history": [], "analyst_actions": [], "price_targets": None,
                "insider": [], "filings": [], "benchmarks": [], "dividendDate": None, "exDividendDate": None}

    async def fake_run(agent, payload, **kwargs):
        seen["calls"].append(agent.name)
        payload = json.loads(payload)
        if agent.name == "CatalystScreen":
            seen["screened"] = [row["title"] for row in payload["articles"]]
            if isinstance(screen, Exception):
                raise screen
            return FakeResult(screen(payload))
        if agent.name == "StockResearch":
            seen["queries"].append(payload["query"])
            return no_findings()
        if agent.name == "StockWriter":
            if isinstance(writer_output, Exception):
                raise writer_output
            return FakeResult(writer_output(payload))
        return FakeResult(verdict)

    seen = {"calls": [], "queries": [], "screened": [], "fetches": 0}
    monkeypatch.setattr(manager, "fetch_market", fake_market)
    monkeypatch.setattr(manager, "fetch_week", fake_week)
    monkeypatch.setattr(manager, "fetch_catalysts", fake_catalysts)
    monkeypatch.setattr(manager, "Runner", SimpleNamespace(run=fake_run))
    return seen


def writer_with(bad_citation: bool):
    def write(payload):
        deal = next(p["source_id"] for p in payload["evidence"] if p["research_purpose"] == "catalyst")
        topics = [Topic(heading=Claim(text="Acme deal", sources=[deal]),
                        sentences=[Claim(text="On Sept. 28, Acme agreed to buy Beta.", sources=[deal])])]
        if bad_citation:
            topics.append(Topic(heading=Claim(text="Mystery", sources=[99]),
                                sentences=[Claim(text="Unknown claim.", sources=[99])]))
        return Digest(headline=Claim(text="{move}; Acme agreed to buy Beta", sources=[1, deal]),
                      topics=topics, coverage_notes=[])
    return write


def run(verify, news=None):
    return asyncio.run(manager.run_digest("NVDA", "sk-test", "gpt-test", lambda _msg: None, verify=verify, news=news))


def test_default_mode_prunes_invalid_claims(monkeypatch):  # B10
    seen = patch_providers(monkeypatch, writer_with(bad_citation=True))
    publication = run(verify=False)
    assert [t.heading.text for t in publication.digest.topics] == ["Acme deal"]
    assert any("removed after failing basic checks" in note for note in publication.coverage)
    assert "StockVerifier" not in seen["calls"]
    assert [s.url for s in publication.sources] == [DEAL.url] and publication.sources[0].id == 2


def test_verified_publish(monkeypatch):  # 4.5
    seen = patch_providers(monkeypatch, writer_with(bad_citation=False), VerificationResult(passed=True, issues=[]))
    publication = run(verify=True)
    assert publication.digest is not None and "StockVerifier" in seen["calls"]
    assert any("checked by a model reviewer" in note for note in publication.coverage)
    assert publication.digest.topics[0].heading.text == "Acme deal"


def test_writer_failure_falls_back_to_headlines(monkeypatch):
    patch_providers(monkeypatch, ModelBehaviorError("bad output"))
    publication = run(verify=True)
    assert publication.digest is None and publication.news_source_ids
    assert any("could not be generated" in note for note in publication.coverage)


def test_screen_reads_titles_since_the_previous_close_and_only_earnings_are_searched(monkeypatch):
    def check_payload(payload):
        catalyst = next(p for p in payload["evidence"] if p["research_purpose"] == "catalyst")
        assert catalyst["source_id"] in payload["possible_move_explanations"]     # published before the close
        assert payload["catalyst_window"]["catalysts"] == 1
        assert catalyst["supporting_material"] == DEAL.title + "\nAcme agreed to buy Beta in cash."
        return writer_with(bad_citation=False)(payload)

    seen = patch_providers(monkeypatch, check_payload)
    run(verify=False)
    # Monday run: Saturday's headline is inside the window (since Friday's close), last week's is not.
    assert seen["screened"] == [LIST.title, DEAL.title, SATURDAY.title]
    assert len(seen["queries"]) == 1 and "earnings release date" in seen["queries"][0]
    assert seen["fetches"] == 1


def test_shared_news_is_not_fetched_again(monkeypatch):
    seen = patch_providers(monkeypatch, writer_with(bad_citation=False))

    async def shared():
        return make_week(DEAL, providers="Google News", notes=["Finnhub's free tier does not cover this listing."])

    publication = run(verify=False, news=shared())
    assert seen["fetches"] == 0 and publication.digest is not None
    assert "Finnhub's free tier does not cover this listing." in publication.coverage


def test_news_failure_leaves_a_price_only_digest(monkeypatch):
    seen = patch_providers(monkeypatch, writer_with(bad_citation=False))

    async def failing():
        raise RuntimeError("both providers down")

    publication = run(verify=False, news=failing())
    assert publication.digest is None and "CatalystScreen" not in seen["calls"]
    assert any("could not be retrieved" in note for note in publication.coverage)


def test_screen_failure_is_reported_not_treated_as_no_news(monkeypatch):
    patch_providers(monkeypatch, writer_with(bad_citation=False), screen=RuntimeError("model down"))
    publication = run(verify=False)
    assert publication.digest is None
    assert any("headline screen did not run" in note for note in publication.coverage)
    assert not any("No clear company-specific catalyst" in note for note in publication.coverage)


def test_empty_screen_reports_no_catalyst(monkeypatch):
    patch_providers(monkeypatch, writer_with(bad_citation=False), screen=lambda payload: CatalystScreen(catalysts=[]))
    publication = run(verify=False)
    assert publication.digest is None
    assert any("No clear company-specific catalyst was identified in the headlines checked." in note
               for note in publication.coverage)
