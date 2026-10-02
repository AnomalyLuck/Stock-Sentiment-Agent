"""Offline end-to-end runs of run_digest with Yahoo and OpenAI replaced by fakes."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agents import ModelBehaviorError

import stock_digest.manager as manager
from conftest import AS_OF, CLOSE, make_market
from stock_digest.market import DigestError
from stock_digest.material.models import Entry, Issuer, MaterialResult
from stock_digest.models import Claim, Digest, Topic, VerificationResult

ARTICLE = "https://news.example/acme-deal"
BUYBACK = "https://nvidianews.example/buyback"


class FakeResult:
    def __init__(self, final_output, raw_responses=()):
        self.final_output, self.raw_responses = final_output, list(raw_responses)

    def final_output_as(self, _cls):
        return self.final_output


def research_result():
    finding = {"summary": "Acme agreed to buy Beta.", "url": ARTICLE, "reported_excerpt": None,
               "published": "2026-09-28T08:00:00-04:00", "updated": None,
               "timestamp_basis": "Sep 28, 2026 8:00 AM ET", "content_kind": "dated_announcement",
               "confirmation_status": "confirmed", "catalyst_type": "merger_acquisition",
               "move_relevance": "direct", "event_date": None, "earnings_date": None, "story_key": "acme-beta"}
    text = "```json\n" + json.dumps({"query": "q", "status": "findings", "findings": [finding]}) + "\n```"
    call = {"type": "web_search_call", "status": "completed",
            "action": {"type": "search", "sources": [{"url": ARTICLE, "title": "Acme to buy Beta"}]}}
    return FakeResult(text, [SimpleNamespace(output=[call])])


def feed_rows():
    stamp = (CLOSE - timedelta(hours=3)).isoformat()
    return [{"content": {"title": "NVIDIA shares climb", "summary": "", "pubDate": stamp,
                         "provider": {"displayName": "Wire"}, "canonicalUrl": {"url": "https://wire.example/nvda"}}}]


def patch_providers(monkeypatch, writer_output, verdict=None):
    async def fake_market(ticker):
        return make_market(), {}

    async def fake_news(ticker):
        return feed_rows(), AS_OF

    def fake_catalysts(market, profile):
        return {"retrieved_at": AS_OF.isoformat(), "symbol": "NVDA", "diagnostics": [], "earnings": {},
                "estimates": None, "revenue_history": [], "analyst_actions": [], "price_targets": None,
                "insider": [], "filings": [], "benchmarks": [], "dividendDate": None, "exDividendDate": None}

    calls = []

    async def fake_run(agent, payload, **kwargs):
        calls.append(agent.name)
        if agent.name == "StockResearch":
            queries.append(json.loads(payload)["query"])
            return research_result()
        if agent.name == "StockWriter":
            if isinstance(writer_output, Exception):
                raise writer_output
            return FakeResult(writer_output(json.loads(payload)))
        return FakeResult(verdict)

    monkeypatch.setattr(manager, "fetch_market", fake_market)
    monkeypatch.setattr(manager, "fetch_news", fake_news)
    monkeypatch.setattr(manager, "fetch_catalysts", fake_catalysts)
    monkeypatch.setattr(manager, "Runner", SimpleNamespace(run=fake_run))
    return calls


queries: list[str] = []   # research queries seen by the fake Runner (reset per test by patch_providers callers)


def writer_with(bad_citation: bool):
    def write(payload):
        deal = next(p["source_id"] for p in payload["evidence"] if p.get("story_key") == "acme beta")
        topics = [Topic(heading=Claim(text="Acme deal", sources=[deal]),
                        sentences=[Claim(text="On Sept. 28, Acme agreed to buy Beta.", sources=[deal])])]
        if bad_citation:
            topics.append(Topic(heading=Claim(text="Mystery", sources=[99]),
                                sentences=[Claim(text="Unknown claim.", sources=[99])]))
        return Digest(headline=Claim(text="{move}; Acme agreed to buy Beta", sources=[1, deal]),
                      topics=topics, coverage_notes=[])
    return write


def run(verify, material=None):
    queries.clear()
    return asyncio.run(manager.run_digest("NVDA", "sk-test", "gpt-test", lambda _msg: None, verify=verify, material=material))


def test_default_mode_prunes_invalid_claims(monkeypatch):  # B10
    calls = patch_providers(monkeypatch, writer_with(bad_citation=True))
    publication = run(verify=False)
    assert [t.heading.text for t in publication.digest.topics] == ["Acme deal"]
    assert any("removed after failing basic checks" in note for note in publication.coverage)
    assert "StockVerifier" not in calls
    # Compact numbering: the two headline/claim sources are 2 and 3.
    assert sorted(s.id for s in publication.sources) == [2, 3]


def test_verified_publish(monkeypatch):  # 4.5
    calls = patch_providers(monkeypatch, writer_with(bad_citation=False), VerificationResult(passed=True, issues=[]))
    publication = run(verify=True)
    assert publication.digest is not None and "StockVerifier" in calls
    assert any("checked by a model reviewer" in note for note in publication.coverage)
    # The dated deal announcement preceded the close, so it is a move-explanation candidate.
    assert publication.digest.topics[0].heading.text == "Acme deal"


def test_writer_failure_falls_back_to_headlines(monkeypatch):
    patch_providers(monkeypatch, ModelBehaviorError("bad output"))
    publication = run(verify=True)
    assert publication.digest is None and publication.news_source_ids
    assert any("could not be generated" in note for note in publication.coverage)


# ---------------------------------------------------------------- with the material-news run supplied

async def material_run(symbol="NVDA"):
    issuer = Issuer(ticker=symbol.replace("-", "."), symbol=symbol, exchange="NasdaqGS", company="NVIDIA Corporation")
    buyback = Entry(event_id="E1", date="Sep 28, 2026", headline="NVIDIA authorizes $60B buyback", reported=False,
                    status="authorized", status_label="authorized", category="capital_return",
                    why="The board authorized $60 billion of repurchases.", verification="opened",
                    sources=[{"url": BUYBACK, "publisher": "NVIDIA", "title": "NVIDIA announces buyback", "published": None,
                              "snippets": [], "source_type": "official_release"}],
                    first_reported_at="2026-09-28T08:00:00-04:00", key_facts=["$60 billion authorization"])
    old = buyback.model_copy(update={"event_id": "E2", "first_reported_at": "2026-09-22", "sources": [{"url": "https://wire.example/old"}]})
    return MaterialResult(issuer=issuer, run_at=AS_OF, window_start=AS_OF - timedelta(days=7), window_end=AS_OF,
                          timezone="UTC", hours=168, entries=[buyback, old], qualified_entries=[buyback, old], qualified_count=2)


def writer_citing_material(payload):
    packet = next(p for p in payload["evidence"] if p.get("material"))
    assert payload["evidence"][0] is packet and payload["material_window"]["developments"] == 1
    assert packet["source_id"] in payload["possible_move_explanations"]
    topics = [Topic(heading=Claim(text="Record buyback", sources=[packet["source_id"]]),
                    sentences=[Claim(text="NVIDIA shares rose today after the board authorized a $60 billion buyback.",
                                     sources=[packet["source_id"]])])]
    return Digest(headline=Claim(text="NVIDIA climbs after authorizing a $60 billion buyback", sources=[1, packet["source_id"]]),
                  topics=topics, coverage_notes=[])


def test_material_run_replaces_company_development_searches(monkeypatch):
    patch_providers(monkeypatch, writer_citing_material, VerificationResult(passed=True, issues=[]))
    publication = run(verify=True, material=material_run())
    assert len(queries) == 4
    assert not any(word in q for q in queries for word in ("press release", "acquisition", "reportedly", "this week"))
    assert publication.digest.topics[0].heading.text == "Record buyback"
    cited = next(s for s in publication.sources if s.url == BUYBACK)
    assert cited.timestamp_provenance == "material_verified" and cited.published == "2026-09-28T08:00:00-04:00"
    assert any(note.startswith("Material news: 1 of 2") for note in publication.diagnostics)


def test_material_run_failure_fails_the_digest(monkeypatch):
    patch_providers(monkeypatch, writer_citing_material)

    async def failing():
        raise DigestError("Research could not be completed.")

    with pytest.raises(DigestError, match="material-news research failed"):
        run(verify=False, material=failing())


def test_material_run_for_another_company_is_rejected(monkeypatch):
    patch_providers(monkeypatch, writer_citing_material)
    with pytest.raises(DigestError, match="resolved AMD, not NVDA"):
        run(verify=False, material=material_run("AMD"))


def test_without_material_the_seven_queries_still_run(monkeypatch):
    patch_providers(monkeypatch, writer_with(bad_citation=False))
    run(verify=False)
    assert len(queries) == 8 and any("this week" in q for q in queries)   # 7 focused queries plus the 7-day expansion
