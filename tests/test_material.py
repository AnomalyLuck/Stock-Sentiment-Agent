"""Acceptance fixtures for the material company news agent (offline; no search or model calls).

Numbers in test names refer to the specification's acceptance criteria.
"""
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import stock_digest.material.agents as material_agents
import stock_digest.material.pipeline as pipeline
import stock_digest.material.cache as material_cache
from stock_digest.market import InputError
from stock_digest.material.gates import (Cluster, apply_merges, candidate_gate, canonical_url, display_moment,
                                         group_candidates, make_window, mechanism_reason, placement, size_reason,
                                         status_language, time_gate)
from stock_digest.material.identity import _clarify, parse_symbol, resolve_timezone
from stock_digest.material.models import (Candidate, Consolidation, ClusterMerge, Issuer, SearchProfile, SourceRef,
                                          VerifiedEvent)
from stock_digest.material.render import markdown

RUN_AT = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
WINDOW = make_window(RUN_AT, 168, "America/New_York")   # Sep 23 14:00 EDT → Sep 30 14:00 EDT
MEGACAP = Issuer(ticker="NVDA", symbol="NVDA", exchange="NasdaqGS", company="NVIDIA Corporation",
                 market_cap=4.5e12, currency="USD")
SMALLCAP = Issuer(ticker="SMOL", symbol="SMOL", exchange="NasdaqCM", company="Smol Devices Inc.",
                  market_cap=1.0e9, currency="USD")
BUYBACK = {"changes": "capital returned to shareholders", "through": "a new $60 billion share repurchase authorization"}


@pytest.fixture(autouse=True)
def isolated_material_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("STOCK_DIGEST_CACHE_DIR", str(tmp_path / "cache"))
    # Evidence timestamps are stable between repeat runs in one test.
    now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(__import__(__name__), "stamp", lambda hours: (now - timedelta(hours=hours)).isoformat())


def cand(url="https://news.example/a", **overrides) -> Candidate:
    fields = dict(headline="NVIDIA authorizes additional $60B buyback", summary="NVIDIA's board authorized $60B.",
                  url=url, publisher="news.example", published="2026-09-29T08:00:00-04:00",
                  timestamp_basis="Sep 29, 2026 8:00 AM ET", category="capital_return", status="authorized",
                  subject_role="main", materiality="high", mechanism=BUYBACK, source_type="original_reporting",
                  original_outlet="News Example", development_key="buyback-60b", milestone="authorization")
    fields.update(overrides)
    return Candidate.model_validate(fields)


def ref(url="https://news.example/a", **overrides) -> SourceRef:
    fields = dict(url=url, title="t", publisher="news.example", retrieved_at=RUN_AT)
    fields.update(overrides)
    return SourceRef(**fields)


def gate(candidate, issuer=MEGACAP):
    return candidate_gate(candidate, issuer, WINDOW, ref(candidate.url))


# ---------------------------------------------------------------- 1, 2, 8, 9: deduplication

def test_1_tracking_variants_and_copies_of_one_buyback_form_one_group():
    copies = [cand("https://news.example/a?utm_source=x&utm_medium=y", development_key="nvda-repurchase-plan"),
              cand("https://www.news.example/a/", development_key="buyback-60b"),
              cand("https://wire.example/nvda-buyback", headline="Shares rise after record repurchase plan"),
              cand("https://blog.example/what-it-means", headline="What the buyback means for investors")]
    assert canonical_url(copies[0].url) == canonical_url(copies[1].url) == "https://news.example/a"
    assert group_candidates(copies) == [[0, 1, 2, 3]]


def test_2_one_article_with_buyback_and_unrelated_launch_yields_two_developments():
    article = "https://news.example/roundup-of-the-day"
    launch = cand(article, headline="NVIDIA unveils Rubin platform", category="product_platform", status="announced",
                  development_key="rubin-platform", milestone="announcement",
                  mechanism={"changes": "data-center product competitiveness and revenue",
                             "through": "a new accelerator generation shipping next year"})
    assert len(group_candidates([cand(article), launch])) == 2
    assert gate(cand(article)).passed and gate(launch).passed


def test_8_syndicated_copies_count_as_one_lineage():
    copies = [cand(f"https://site{i}.example/x", original_outlet="Reuters", source_type="syndicated") for i in range(5)]
    cluster = Cluster("E1", copies)
    assert cluster.lineages() == {"reuters"}
    original = cand("https://reuters.com/x", original_outlet="Reuters", source_type="original_reporting")
    assert Cluster("E1", copies + [original]).best() is original


def test_9_distinct_milestones_stay_separate_and_merges_only_union():
    announcement = cand(category="merger_acquisition", status="agreed", development_key="acme-deal", milestone="announcement")
    approval = cand("https://reg.example/ok", category="regulatory_market_access", status="approved",
                    development_key="acme-deal", milestone="regulatory-approval")
    groups = group_candidates([announcement, approval])
    assert len(groups) == 2
    merged, notes = apply_merges([[0], [1], [2]], [[0, 2], [7]])
    assert sorted(merged) == [[0, 2], [1]] and notes


# ---------------------------------------------------------------- 3, 4, 5: subject and materiality

@pytest.mark.parametrize("role", ["roundup", "listicle", "metadata_only", "incidental"])
def test_3_ticker_in_lists_roundups_or_tags_does_not_qualify(role):
    decision = gate(cand(subject_role=role, headline="Ten AI stocks to watch"))
    assert not decision.passed and decision.gate == "subject"


def test_4_partner_led_announcement_is_excluded_even_with_target_in_headline():
    partner = cand(headline="CloudCo launches service built on NVIDIA GPUs", subject_role="secondary",
                   category="product_platform")
    assert gate(partner).reason.startswith("company is not the main subject")
    # A material bilateral deal naming the target as counterparty still counts.
    deal = cand(headline="SpaceX partners with NVIDIA on orbital compute", subject_role="secondary",
                category="partnership", development_key="spacex-partnership",
                mechanism={"changes": "data-center demand", "through": "a multi-year compute partnership"})
    assert gate(deal).passed


def test_5_major_platform_qualifies_routine_items_do_not():
    platform = cand(category="product_platform", status="launched", development_key="ai-security-platform",
                    mechanism={"changes": "competitive position in enterprise AI security software",
                               "through": "a new platform sold to enterprise customers"})
    assert gate(platform).passed
    for category in ("security_patch", "ecosystem_integration", "developer_content", "routine_product"):
        assert not gate(cand(category=category)).passed


@pytest.mark.parametrize("mechanism", [
    {"changes": "AI is growing", "through": "the AI boom continues"},
    {"changes": "its leadership", "through": "this reinforces leadership in AI"},
    {"changes": "the stock", "through": "investors may react to the news"},
    {"changes": "", "through": ""},
])
def test_generic_materiality_rationales_are_rejected(mechanism):
    assert mechanism_reason(cand(mechanism=mechanism).mechanism)
    assert not gate(cand(mechanism=mechanism)).passed


# ---------------------------------------------------------------- 6: freshness

def test_6_refreshed_page_dates_do_not_make_old_news_new():
    old = cand(published="2026-08-20T08:00:00-04:00", timestamp_basis="Aug 20, 2026 8:00 AM ET",
               updated="2026-09-29", original_announcement_date="2026-08-20")
    assert not gate(old).passed
    assert not gate(cand(is_rehash=True)).passed
    updated = time_gate("2026-08-20", "2026-09-29", "regulators cleared the deal on Sep 29", None, False, WINDOW)
    assert updated.passed and updated.effective_date == "2026-09-29"


def test_publication_date_comes_from_metadata_or_the_stated_date():
    assert gate(cand(timestamp_basis="Updated recently")).passed          # stated in-window date accepted
    undated = cand(published=None, timestamp_basis=None)
    assert gate(undated).pending and not gate(undated).passed             # left for verification
    from_metadata = candidate_gate(undated, MEGACAP, WINDOW, ref(undated.url, published="2026-09-29"))
    assert from_metadata.passed
    stale_metadata = candidate_gate(cand(), MEGACAP, WINDOW, ref(cand().url, published="2026-08-01"))
    assert not stale_metadata.passed                                       # metadata outranks the stated date


# ---------------------------------------------------------------- 7: status

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


# ---------------------------------------------------------------- 10: relative size

def test_10_materiality_is_relative_to_issuer_size():
    contract = dict(category="contract", status="agreed", amount_usd=150e6, development_key="contract",
                    mechanism={"changes": "revenue from government customers", "through": "a $150M multi-year contract"})
    assert size_reason("contract", 150e6, MEGACAP)
    assert not gate(cand(**contract), MEGACAP).passed
    assert gate(cand(**contract), SMALLCAP).passed


# ---------------------------------------------------------------- 14: input, time and boundaries

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


def test_14_timezones_and_partial_day_boundaries():
    assert resolve_timezone("Asia/Tokyo") == "Asia/Tokyo"
    with pytest.raises(InputError):
        resolve_timezone("Mars/Base")
    assert display_moment(RUN_AT, "Asia/Tokyo") == "Oct 1, 2026 03:00 JST"
    assert display_moment(RUN_AT, "America/New_York") == "Sep 30, 2026 14:00 EDT"
    assert placement("2026-09-23", WINDOW) == "boundary"          # window opens at 14:00 that day
    assert placement("2026-09-23T15:00:00-04:00", WINDOW) == "inside"
    assert placement("2026-09-23T13:00:00-04:00", WINDOW) == "before"
    assert placement("2026-09-24", WINDOW) == "inside"
    assert placement("2026-10-02", WINDOW) == "after"
    boundary = gate(cand(published="2026-09-23", timestamp_basis="September 23, 2026"))
    assert boundary.passed and boundary.boundary                    # first partial day is accepted
    assert not gate(cand(published="2026-09-22", timestamp_basis="September 22, 2026")).passed


# ---------------------------------------------------------------- end to end with fakes

class FakeResult:
    def __init__(self, final_output, output=()):
        self.final_output = final_output
        self.raw_responses = [SimpleNamespace(output=list(output))]


def search_call(*urls, kind="search"):
    if kind == "search":
        return {"type": "web_search_call", "status": "completed",
                "action": {"type": "search", "sources": [{"type": "url", "url": u} for u in urls]}}
    return {"type": "web_search_call", "status": "completed", "action": {"type": "open_page", "url": urls[0]}}


def stamp(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()


def discovery_rows():
    when = stamp(20)
    basis = datetime.fromisoformat(when).strftime("%b %d, %Y %H:%M UTC")
    common = dict(published=when, timestamp_basis=basis, subject_role="main", materiality="high")
    return [
        {**common, "headline": "NVIDIA authorizes $60B buyback", "summary": "Board authorized $60B.",
         "url": "https://nvidianews.example/buyback?utm_source=feed", "category": "capital_return", "status": "authorized",
         "mechanism": BUYBACK, "source_type": "official_release", "original_outlet": "NVIDIA",
         "development_key": "buyback-60b", "milestone": "authorization"},
        {**common, "headline": "NVIDIA shares rise after record repurchase plan", "summary": "Copy of the news.",
         "url": "https://wire.example/nvda", "category": "capital_return", "status": "authorized",
         "mechanism": BUYBACK, "source_type": "syndicated", "original_outlet": "Wire",
         "development_key": "nvda-record-repurchase", "milestone": "authorization"},
        {**common, "headline": "NVIDIA unveils Rubin data-center platform", "summary": "New accelerator generation.",
         "url": "https://wire.example/nvda", "category": "product_platform", "status": "announced",
         "mechanism": {"changes": "data-center revenue and competitive position",
                       "through": "a new accelerator generation for cloud customers"},
         "source_type": "original_reporting", "original_outlet": "Wire", "development_key": "rubin-platform",
         "milestone": "announcement"},
        {**common, "headline": "Ten AI stocks to watch", "summary": "Listicle.", "url": "https://list.example/ten",
         "subject_role": "listicle", "category": "analyst_commentary", "status": "reported",
         "mechanism": {"changes": "", "through": ""}, "development_key": "list"},
        {**common, "headline": "New game adds DLSS", "summary": "Game update.", "url": "https://games.example/dlss",
         "category": "ecosystem_integration", "status": "launched", "materiality": "low",
         "mechanism": {"changes": "", "through": ""}, "development_key": "dlss-game"},
    ]


def verdict(payload):
    articles = json.loads(payload)["articles"]
    if any("insur" in a["headline"].lower() for a in articles):
        lead = articles[0]
        body = {"verified": True, "headline": "Nvidia discusses insurance for chip-backed loans",
                "why": "Nvidia held early talks with insurers to protect lenders to smaller GPU buyers; such cover "
                       "could ease financing for purchases, but no agreement was reported.",
                "category": "financing", "status": "reported_talks", "primary_url": lead["url"],
                "primary_publisher": "Financial Times", "primary_published": lead["published"],
                "primary_source_type": "syndicated", "primary_lineage": "Financial Times",
                "company_is_main_subject": True, "magnitude": 3, "directness": 3, "novelty": 4,
                "mechanism": {"changes": "financing access for smaller GPU buyers", "through": "insurance protecting lenders"}}
        return "```json\n" + json.dumps(body) + "\n```", [search_call(lead["url"], kind="open")]
    buyback = any("buyback" in a["headline"].lower() or "repurchase" in a["headline"].lower() for a in articles)
    url = "https://nvidianews.example/buyback" if buyback else "https://wire.example/nvda"
    when = stamp(20)
    body = {
        "verified": True, "headline": "NVIDIA authorizes additional $60B buyback" if buyback else "NVIDIA unveils Rubin platform",
        "why": ("The board authorized $60 billion of repurchases with no expiry; the authorization could raise capital "
                "returns, though actual purchases may vary.") if buyback else
               ("NVIDIA announced the Rubin accelerator generation for cloud data centers; it could support data-center "
                "revenue, but commercial impact is unproven."),
        "category": "capital_return" if buyback else "product_platform", "status": "authorized" if buyback else "announced",
        "primary_url": url, "primary_publisher": "NVIDIA" if buyback else "Wire", "primary_published": when,
        "primary_timestamp_basis": datetime.fromisoformat(when).strftime("%B %d, %Y %H:%M UTC"),
        "primary_source_type": "official_release" if buyback else "original_reporting",
        "primary_lineage": "NVIDIA" if buyback else "Wire",
        "secondary_url": "https://wire.example/nvda" if buyback else None, "secondary_lineage": "Wire",
        "company_is_main_subject": True, "mechanism": BUYBACK if buyback else
            {"changes": "data-center revenue", "through": "a new accelerator generation for cloud customers"},
        "magnitude": 5 if buyback else 4, "directness": 5 if buyback else 3, "novelty": 4,
        "key_facts": ["$60 billion authorization with no expiry"] if buyback else [],
        "uncertainties": ["Actual purchases may vary."] if buyback else [],
    }
    return "```json\n" + json.dumps(body) + "\n```", [search_call(url, kind="open"), search_call(url, "https://wire.example/nvda")]


def install_fakes(monkeypatch, fail_search=False, empty=False, leads=()):
    monkeypatch.setattr(pipeline, "resolve_issuer", lambda raw, exchange=None: MEGACAP)
    monkeypatch.setattr(pipeline, "fetch_feed_leads", lambda symbol, window: list(leads))
    calls = []

    async def fake_run(agent, payload, **kwargs):
        calls.append(agent.name)
        if agent.name == "MaterialProfile":
            return FakeResult(SearchProfile(aliases=["Nvidia"], brands=["GeForce"]))
        if agent.name in {"MaterialDiscovery", "MaterialFeed"}:
            if fail_search and agent.name == "MaterialDiscovery":
                raise ConnectionError("offline")
            body = json.loads(payload)
            query = body["query"]
            rows = discovery_rows() if (query.endswith(") news") and not empty) else []
            for index, lead in enumerate(body.get("leads", [])):
                rows.append({"lead_index": index, "headline": "Nvidia discusses insurance for chip-backed loans", "summary": lead["summary"],
                             "url": lead["url"], "publisher": "Financial Times", "published": lead["published"],
                             "category": "financing", "status": "reported_talks", "subject_role": "main",
                             "materiality": "medium", "source_type": "syndicated", "original_outlet": "Financial Times",
                             "mechanism": {"changes": "financing access for smaller GPU buyers",
                                           "through": "insurance protecting lenders"},
                             "development_key": "insurer-talks", "milestone": "talks"})
            text = "```json\n" + json.dumps({"status": "findings" if rows else "no_relevant_results", "candidates": rows}) + "\n```"
            return FakeResult(text, [search_call(*{r["url"] for r in rows})] if agent.name == "MaterialDiscovery" else [])
        if agent.name == "MaterialConsolidator":
            groups = json.loads(payload)["groups"]
            buyback = [g["group_id"] for g in groups if "buyback" in json.dumps(g).lower() or "repurchase" in json.dumps(g).lower()]
            return FakeResult(Consolidation(merges=[ClusterMerge(group_ids=buyback)] if len(buyback) > 1 else []))
        if agent.name == "MaterialVerifier":
            text, output = verdict(payload)
            return FakeResult(text, output)
        raise AssertionError(agent.name)

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=fake_run))
    return calls


def run(request=None):
    request = request or pipeline.MaterialRequest(ticker="NVDA", timezone="America/New_York")
    return asyncio.run(pipeline.run_material(request, "sk-test", {"research": "m", "writer": "m"}, lambda m: None))


def test_end_to_end_dedupes_filters_and_ranks(monkeypatch):
    calls = install_fakes(monkeypatch)
    result = run()
    assert [e.headline for e in result.entries] == ["NVIDIA authorizes additional $60B buyback", "NVIDIA unveils Rubin platform"]
    assert calls.count("MaterialVerifier") == 2
    buyback = result.entries[0]
    # 12: every entry has a source, a date, a rationale and a status.
    for entry in result.entries:
        assert entry.sources and entry.sources[0]["url"].startswith("https://") and entry.date and entry.why and entry.status
    assert [s["publisher"] for s in buyback.sources] == ["NVIDIA", "wire.example"]
    # The digest reads these: the verifier's facts, the primary source type and every ranked entry.
    assert buyback.key_facts == ["$60 billion authorization with no expiry"] and buyback.uncertainties == ["Actual purchases may vary."]
    assert buyback.sources[0]["source_type"] == "official_release" and "snippets" in buyback.sources[0]
    assert result.qualified_entries == result.entries
    excluded = {row["headline"]: row["gate"] for row in result.record["excluded_candidates"]}
    assert excluded["Ten AI stocks to watch"] == "subject" and excluded["New game adds DLSS"] == "materiality"
    text = markdown(result)
    # 13: one table, one row per development, no appendix of minor items.
    rows = [line for line in text.splitlines() if line.startswith("| ") and not line.startswith("| Date")]
    assert len(rows) == 2 and sum("buyback" in row.lower() for row in rows) == 1
    assert "Ten AI stocks" not in text and "DLSS" not in text
    assert text.startswith("**NVDA — material company developments**")
    assert "*Status: authorized.*" in text and "[NVIDIA](https://nvidianews.example/buyback)" in text


def test_max_results_is_disclosed(monkeypatch):
    install_fakes(monkeypatch)
    result = run(pipeline.MaterialRequest(ticker="NVDA", timezone="UTC", max_results=1))
    assert len(result.entries) == 1 and result.qualified_count == 2 and len(result.qualified_entries) == 2
    assert "Limited to the 1 strongest of 2 qualifying developments" in markdown(result)


def test_11_no_qualifying_results_is_a_concise_empty_state(monkeypatch):
    install_fakes(monkeypatch, empty=True)
    result = run()
    assert result.outcome == "none_found"
    assert "> No qualifying material, company-led developments were found for NVDA in the specified window." in markdown(result)


def test_11_search_failure_is_a_research_limitation_not_no_news(monkeypatch):
    install_fakes(monkeypatch, fail_search=True)
    with pytest.raises(pipeline.ResearchUnavailable, match="research limitation, not a finding"):
        run()


def test_verification_failure_falls_back_with_disclosure(monkeypatch):
    install_fakes(monkeypatch)
    original = pipeline.Runner.run

    async def flaky(agent, payload, **kwargs):
        if agent.name == "MaterialVerifier":
            raise TimeoutError
        return await original(agent, payload, **kwargs)

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=flaky))
    result = run()
    assert len(result.entries) == 2
    assert all(e.verification == "search_results_only" for e in result.entries)
    assert "rely on search-result content" in result.coverage_note


def test_ungrounded_urls_are_never_published(monkeypatch):
    install_fakes(monkeypatch)
    original = pipeline.Runner.run

    async def invent(agent, payload, **kwargs):
        result = await original(agent, payload, **kwargs)
        if agent.name == "MaterialVerifier":
            body = json.loads(result.final_output.strip("`json\n"))
            body["primary_url"] = "https://invented.example/story"
            body["secondary_url"] = "https://also-invented.example/x"
            result.final_output = json.dumps(body)
        return result

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=invent))
    urls = {s["url"] for e in run().entries for s in e.sources}
    assert not any("invented" in url for url in urls)


def test_undated_discovery_needs_verification_to_publish(monkeypatch):
    original_rows = discovery_rows

    def undated():
        rows = original_rows()[:1]
        rows[0].update(published=None, timestamp_basis=None)
        return rows

    monkeypatch.setattr(__import__(__name__), "discovery_rows", undated)
    install_fakes(monkeypatch)
    assert [e.headline for e in run().entries] == ["NVIDIA authorizes additional $60B buyback"]  # verifier dated it

    original = pipeline.Runner.run

    async def no_verifier(agent, payload, **kwargs):
        if agent.name == "MaterialVerifier":
            raise TimeoutError
        return await original(agent, payload, **kwargs)

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=no_verifier))
    monkeypatch.setenv("STOCK_DIGEST_CACHE_DIR", "off")
    result = run()
    assert result.entries == [] and result.record["events"][0]["exclusion_reason"] == "publication date could not be confirmed"


def test_unverifiable_items_fall_back_to_any_non_social_source():
    social = cand("https://www.reddit.com/r/x/1", source_type="aggregator", status="reported_talks",
                  original_outlet="Financial Times", summary="A post reproduces FT reporting on insurer talks.",
                  mechanism={"changes": "financing access for smaller chip buyers", "through": "insurance structures for lenders"})
    cluster = Cluster("E1", [social], [gate(social)])
    entry, record = pipeline.build_entry(cluster, None, {}, "timeout", {canonical_url(social.url): ref(social.url)},
                                         MEGACAP, WINDOW, "English")
    assert entry is None and "social-media post" in record["exclusion_reason"]
    aggregator = cand("https://aggregator.example/ft-insurers", source_type="aggregator", status="reported_talks",
                      original_outlet="Financial Times", summary="Nvidia held early talks with insurers.",
                      mechanism=social.mechanism)
    cluster = Cluster("E1", [social, aggregator], [gate(social), gate(aggregator)])
    entry, _ = pipeline.build_entry(cluster, None, {}, "timeout", {canonical_url(aggregator.url): ref(aggregator.url)},
                                    MEGACAP, WINDOW, "English")
    assert entry.sources[0]["url"] == aggregator.url and entry.why.startswith("Reported by Financial Times:")
    assert entry.rank_key[0] == 2                                   # ranked below reputable fallbacks
    reported = cand("https://ft.example/insurers", source_type="original_reporting", status="reported_talks",
                    original_outlet="Financial Times", summary="Nvidia held early talks with insurers.",
                    mechanism=social.mechanism)
    cluster = Cluster("E1", [social, reported], [gate(social), gate(reported)])
    entry, _ = pipeline.build_entry(cluster, None, {}, "timeout", {canonical_url(reported.url): ref(reported.url)},
                                    MEGACAP, WINDOW, "English")
    assert entry.sources[0]["url"] == reported.url and entry.why.startswith("Reported by Financial Times:")
    assert entry.verification == "search_results_only" and entry.rank_key[0] <= 3


def test_feed_leads_are_grounded_dated_discovery_sources(monkeypatch):
    lead = {"title": "Nvidia turns to insurers", "summary": "Nvidia held talks with insurers, the FT reported.",
            "url": "https://finance.yahoo.com/news/nvidia-insurers?.tsrc=feed", "publisher": "Financial Times",
            "published": stamp(30)}
    install_fakes(monkeypatch, leads=[lead])
    result = run()
    insurer = [e for e in result.entries if "insur" in e.headline.lower()]
    assert len(insurer) == 1 and insurer[0].reported and insurer[0].sources[0]["url"] == lead["url"]
    assert insurer[0].why.startswith("Reported by Financial Times:") and insurer[0].verification == "opened"


def test_staged_discovery_and_repeat_run_call_counts(monkeypatch):
    calls = install_fakes(monkeypatch)
    first = run()
    assert first.record["agent_runs"] == {           # 8 topic searches + 7 dated daily searches
        "profile": 1, "discovery": 15, "feed": 0, "consolidation": 1, "verification": 2,
    }
    calls.clear()
    second = run()
    assert second.record["agent_runs"] == {
        "profile": 0, "discovery": 15, "feed": 0, "consolidation": 1, "verification": 0,
    }
    assert len(calls) == 16
    assert second.record["cache_hits"] == {"profile": 1, "verification": 2}
    assert first.entries == second.entries
    assert all(e["verification_cached"] for e in second.record["events"])
    assert [e["verified_at"] for e in first.record["events"]] == [e["verified_at"] for e in second.record["events"]]


def test_all_40_feed_leads_use_one_call_without_web_tools(monkeypatch):
    leads = [{"title": f"Nvidia insurers {i}", "summary": "Nvidia held talks with insurers.",
              "url": f"https://feed.example/{i}", "publisher": "Financial Times", "published": stamp(20)}
             for i in range(40)]
    calls = install_fakes(monkeypatch, leads=leads)
    result = run()
    assert calls.count("MaterialFeed") == 1
    query = next(q for q in result.record["queries"] if q["purpose"] == "feed")
    assert query["candidates"] == 40 and query["invalid"] == 0 and query["failure"] is None
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key="sk-test")
    try:
        feed = pipeline.build_agents({"research": "m", "writer": "m"}, client)["feed"]
        assert feed.tools == [] and feed.model_settings.tool_choice is None
    finally:
        asyncio.run(client.close())


def test_feed_parser_uses_provider_metadata_and_detects_missing_leads():
    leads = [{"title": "Provider title", "summary": "Provider summary", "url": "https://provider.example/a",
              "publisher": "Provider", "published": stamp(20)}] * 2
    body = {"candidates": [{**cand().model_dump(), "lead_index": 0, "url": "https://invented.example/a",
                            "published": "2099-01-01"}]}
    rows, invalid, status = pipeline.parse_feed_candidates(json.dumps(body), leads)
    assert rows[0].url == leads[0]["url"] and rows[0].published == leads[0]["published"]
    assert rows[0].headline == "Provider title" and rows[0].summary == "Provider summary"
    assert invalid == 1 and status == "partial"


def test_successful_feed_does_not_mask_total_search_failure(monkeypatch):
    leads = [{"title": "Nvidia insurers", "summary": "Nvidia held talks with insurers.",
              "url": "https://feed.example/1", "publisher": "Financial Times", "published": stamp(20)}]
    install_fakes(monkeypatch, fail_search=True, leads=leads)
    with pytest.raises(pipeline.ResearchUnavailable):
        run()


def test_followups_only_target_gaps_and_are_bounded():
    profile = SearchProfile(brands=["GeForce"])
    rows = [cand(f"https://news.example/{i}", development_key=f"event-{i}") for i in range(3)]
    queries = [{"purpose": "official", "query": "NVIDIA announces", "failure": None, "invalid": 0}]
    assert len(pipeline.query_plan(MEGACAP, profile, WINDOW)) == 15
    assert pipeline.followup_plan(MEGACAP, profile, queries, rows, [gate(c) for c in rows]) == []
    assert len(pipeline.followup_plan(MEGACAP, profile, queries, [], [])) == 2
    queries[0]["failure"] = "timeout"
    pending = cand(published=None, timestamp_basis=None)
    follow = pipeline.followup_plan(MEGACAP, profile, queries, [pending], [gate(pending)])
    assert len(follow) == 2 and follow[0] == ("official", "NVIDIA announces")
    assert follow[1][0] == "undated"


def test_plan_searches_each_window_day_and_words_product_search_like_news():
    plan = pipeline.query_plan(MEGACAP, SearchProfile(), WINDOW)
    # Sep 23 14:00 EDT to Sep 30 14:00 EDT: the seven most recent local days, newest first.
    assert [q for p, q in plan if p == "daily"] == [f"NVIDIA news September {day}, 2026" for day in range(30, 23, -1)]
    queries = dict(plan)
    assert queries["products"] == "NVIDIA unveils OR launches OR delays OR reschedules product news this week"
    assert "contract partnership" in queries["deals"]          # moved from the old product search
    short = make_window(RUN_AT, 30, "America/New_York")        # Sep 29 08:00 EDT to Sep 30 14:00 EDT
    assert [q for p, q in pipeline.query_plan(MEGACAP, SearchProfile(), short) if p == "daily"] == [
        "NVIDIA news September 30, 2026", "NVIDIA news September 29, 2026"]


def test_category_label_copied_from_a_prompt_example_is_accepted():
    row = {**cand().model_dump(), "category": "excluded: unsupported_speculation"}
    text = "```json\n" + json.dumps({"status": "findings", "candidates": [row]}) + "\n```"
    rows, invalid, _ = pipeline.parse_candidates(text)
    assert invalid == 0 and rows[0].category == "unsupported_speculation"
    assert VerifiedEvent.model_validate({"verified": True, "category": "excluded:analyst_commentary"}).category \
        == "analyst_commentary"
    with pytest.raises(ValidationError):
        Candidate.model_validate({**row, "category": "excluded: not_a_category"})


def test_prompt_examples_list_bare_enum_values():
    for prompt in (material_agents.DISCOVERY, material_agents.FEED, material_agents.VERIFY):
        assert "excluded:" not in prompt


def test_empty_yahoo_feed_is_disclosed_not_treated_as_no_news(monkeypatch):
    install_fakes(monkeypatch)

    def refused(symbol, window):
        raise pipeline.FeedUnavailable("Yahoo Finance returned no headlines")

    monkeypatch.setattr(pipeline, "fetch_feed_leads", refused)
    result = run()
    assert result.entries and "Yahoo Finance returned no headlines" in result.coverage_note
    install_fakes(monkeypatch)                     # a working feed with nothing in the window adds no note
    assert "Yahoo" not in (run().coverage_note or "")


def test_fetch_feed_leads_raises_when_yahoo_returns_nothing(monkeypatch):
    fake = SimpleNamespace(Ticker=lambda symbol: SimpleNamespace(get_news=lambda count, tab: []))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    monkeypatch.setattr("stock_digest.market._configure_yahoo", lambda: None)
    with pytest.raises(pipeline.FeedUnavailable):
        pipeline.fetch_feed_leads("NVDA", WINDOW)


def test_changed_evidence_reverifies_only_affected_event(monkeypatch):
    calls = install_fakes(monkeypatch)
    run()
    original = discovery_rows

    def changed():
        rows = original()
        rows[0]["summary"] = "Board raised the authorization to $70 billion."
        rows[0]["updated"] = stamp(1)
        return rows

    monkeypatch.setattr(__import__(__name__), "discovery_rows", changed)
    calls.clear()
    result = run()
    assert calls.count("MaterialVerifier") == 1
    assert result.record["cache_hits"]["verification"] == 1


def test_event_key_ignores_order_and_tracking_but_detects_all_evidence():
    request = pipeline.MaterialRequest(ticker="NVDA")
    rows = [cand(f"https://news.example/{i}") for i in range(10)]
    cluster = Cluster("E1", rows)
    key = pipeline.event_cache_key(MEGACAP, "m", request, cluster, {})
    shuffled = [c.model_copy(deep=True) for c in reversed(rows)]
    shuffled[0].url += "?utm_source=feed"
    shuffled[0].development_key = "model-reworded-key"
    assert pipeline.event_cache_key(MEGACAP, "m", request, Cluster("E5", shuffled), {}) == key
    shuffled[0].new_fact = "Regulators granted approval."
    assert pipeline.event_cache_key(MEGACAP, "m", request, Cluster("E5", shuffled), {}) != key
    assert pipeline.event_cache_key(MEGACAP, "other-model", request, cluster, {}) != key
    assert pipeline.event_cache_key(SMALLCAP, "m", request, cluster, {}) != key
    registry = {canonical_url(rows[0].url): ref(rows[0].url, snippets=["New source content"])}
    assert pipeline.event_cache_key(MEGACAP, "m", request, cluster, registry) != key


def test_expired_event_reverifies_but_profile_survives(monkeypatch):
    calls = install_fakes(monkeypatch)
    now = material_cache.time.time()
    run()
    monkeypatch.setattr(material_cache.time, "time", lambda: now + material_cache.EVENT_TTL + 10)
    calls.clear()
    result = run()
    assert calls.count("MaterialVerifier") == 2 and calls.count("MaterialProfile") == 0
    assert result.record["cache_hits"] == {"profile": 1, "verification": 0}
    monkeypatch.setattr(material_cache.time, "time", lambda: now + material_cache.PROFILE_TTL + 10)
    calls.clear()
    run()
    assert calls.count("MaterialProfile") == 1 and calls.count("MaterialVerifier") == 2


def test_cache_failures_and_disabled_cache_do_not_block_research(monkeypatch, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setenv("STOCK_DIGEST_CACHE_DIR", str(blocked))
    calls = install_fakes(monkeypatch)
    result = run()
    assert len(result.entries) == 2
    assert any("cache unavailable" in note for note in result.diagnostics)
    monkeypatch.setenv("STOCK_DIGEST_CACHE_DIR", "off")
    calls.clear()
    run()
    run()
    assert calls.count("MaterialProfile") == 2 and calls.count("MaterialVerifier") == 4


def test_cache_corruption_is_a_miss_and_reads_do_not_extend_expiry(monkeypatch):
    cache = material_cache.MaterialCache()
    now = material_cache.time.time()
    monkeypatch.setattr(material_cache.time, "time", lambda: now)
    cache.put("key", {"ok": True}, 100)
    monkeypatch.setattr(material_cache.time, "time", lambda: now + 90)
    assert cache.get("key") == {"ok": True}
    monkeypatch.setattr(material_cache.time, "time", lambda: now + 101)
    assert cache.get("key") is None
    cache.path.write_bytes(b"corrupt database")
    assert cache.get("key") is None
    cache.put("key", {"ok": True}, 100)  # nonfatal even when a write fails


def test_cache_reuses_only_successful_opened_verifications(monkeypatch):
    install_fakes(monkeypatch)
    original = pipeline.Runner.run
    verifier_calls = []

    async def inaccessible(agent, payload, **kwargs):
        result = await original(agent, payload, **kwargs)
        if agent.name == "MaterialVerifier":
            verifier_calls.append(payload)
            result.raw_responses = []
        return result

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=inaccessible))
    first, second = run(), run()
    assert len(verifier_calls) == 4 and second.record["cache_hits"]["verification"] == 0
    assert all(e.verification == "search_results_only" for e in first.entries + second.entries)


def test_cached_verification_still_passes_through_current_window_gates(monkeypatch):
    install_fakes(monkeypatch)
    original = pipeline.Runner.run
    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(pipeline, "datetime", Clock)

    async def boundary_event(agent, payload, **kwargs):
        result = await original(agent, payload, **kwargs)
        if agent.name == "MaterialVerifier":
            body = pipeline.parse_block(result.final_output, "verified")
            if body["category"] == "capital_return":
                body["primary_published"] = (now - timedelta(hours=167.9)).isoformat()
                result.final_output = json.dumps(body)
        return result

    monkeypatch.setattr(pipeline, "Runner", SimpleNamespace(run=boundary_event))
    assert len(run().entries) == 2
    now += timedelta(minutes=10)
    result = run()
    assert result.record["cache_hits"]["verification"] == 2
    assert [e.category for e in result.entries] == ["product_platform"]
    assert any(e["verification_cached"] and e["inclusion_decision"] == "excluded" for e in result.record["events"])


def test_cache_remains_bounded_across_concurrent_writers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3

    monkeypatch.setattr(material_cache, "MAX_ENTRIES", 5)

    def write(index):
        cache = material_cache.MaterialCache()
        cache.put(str(index), {"value": index}, 100)
        assert cache.error is None

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(write, range(20)))
    cache = material_cache.MaterialCache()
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("SELECT count(*) FROM cache").fetchone()[0] == 5
