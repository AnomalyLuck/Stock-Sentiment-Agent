import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from conftest import AS_OF, CLOSE, evidence, make_finding, make_market, make_source
from stock_digest.manager import (Issue, _base_packet, check_digest, company_relevance, dedupe_packets,
                                  eligible_packets, gather_or_cancel, material_evidence, move_explanations, parse_research,
                                  prune_digest, renumber, repair_headline, set_source_timing, yahoo_news_packets)
from stock_digest.material.models import Entry, Issuer, MaterialResult
from stock_digest.models import Claim, Digest, Topic


def test_update_only_finding_is_kept(market):  # B1
    source = make_source()
    finding = make_finding(updated="2026-09-28", timestamp_basis="Updated September 28, 2026")
    packets, omitted = eligible_packets(evidence(finding), [source], market, 3)
    assert len(packets) == 1 and not omitted


def test_fresh_news_with_past_event_date_is_ordinary_news(market):  # B2
    source = make_source()
    finding = make_finding(published="2026-09-28", timestamp_basis="September 28, 2026",
                           event_date=date(2026, 9, 27))
    packets, omitted = eligible_packets(evidence(finding), [source], market, 3)
    assert len(packets) == 1 and packets[0]["event_date"] is None


def test_reported_future_event_is_news_not_an_event(market):  # B2
    source = make_source()
    finding = make_finding(published="2026-09-28", timestamp_basis="September 28, 2026",
                           event_date=date(2026, 10, 15), content_kind="reporting")
    packets, _ = eligible_packets(evidence(finding), [source], market, 3)
    assert len(packets) == 1 and packets[0]["event_date"] is None


def test_old_dated_announcement_of_upcoming_event_is_kept(market):
    source = make_source()
    finding = make_finding(published="2026-09-01", timestamp_basis="September 1, 2026",
                           event_date=date(2026, 10, 15), content_kind="dated_announcement")
    packets, _ = eligible_packets(evidence(finding), [source], market, 3)
    assert packets[0]["event_date"] == "2026-10-15"


def test_unquoted_model_time_is_reduced_to_a_date(market):  # B9
    source = make_source()
    finding = make_finding(published="2026-09-28T09:00:00-04:00", timestamp_basis="September 28, 2026")
    packets, _ = eligible_packets(evidence(finding), [source], market, 3)
    assert source.published == "2026-09-28"
    assert packets[0]["price_timing_unknown"] is True  # date-only same day: can't be ordered


def test_quoted_time_orders_the_story_before_the_close(market):  # 4.2 / B9
    source = make_source()
    finding = make_finding(published="2026-09-28T09:00:00-04:00", timestamp_basis="Sep 28, 2026 9:00 AM ET")
    packets, _ = eligible_packets(evidence(finding), [source], market, 3)
    assert packets[0]["later_than_price"] is False and packets[0]["price_timing_unknown"] is False


def test_feed_title_twin_supplies_timestamp(market):  # 4.2
    source = make_source(title="Nvidia jumps on new chip deal")
    twin = make_source(id=9, url="https://finance.yahoo.com/x", title="Nvidia jumps on new chip deal",
                       published="2026-09-28T14:00:00+00:00", timestamp_provenance="provider_feed")
    feed = {"nvidia jumps on new chip deal": twin}
    packets, _ = eligible_packets(evidence(make_finding()), [source], market, 3, feed)
    assert packets and packets[0]["later_than_price"] is False


def test_research_cannot_overwrite_provider_feed_dates(market):  # B16
    source = make_source(published="2026-09-28T14:00:00+00:00", timestamp_provenance="provider_feed")
    finding = make_finding(updated="2026-09-28", timestamp_basis="Updated September 28, 2026")
    eligible_packets(evidence(finding), [source], market, 3)
    assert source.timestamp_provenance == "provider_feed" and source.updated is None


def test_weekend_date_only_article_is_later_not_unknown():  # 4.2
    friday_close = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
    source = make_source(published="2026-09-27")
    set_source_timing(source, friday_close)
    assert source.price_timing_unknown is False and source.eligible_at > friday_close


def _feed_row(title, minutes_ago=60, url=None):
    stamp = (AS_OF - timedelta(minutes=minutes_ago)).isoformat()
    return {"content": {"title": title, "summary": "", "pubDate": stamp, "provider": {"displayName": "Wire"},
                        "canonicalUrl": {"url": url or f"https://news.example/{abs(hash(title))}"}}}


def test_relevance_short_ticker_and_multiword_company():  # B3
    disney = SimpleNamespace(ticker="A", company="The Walt Disney Company", short_name=None)
    relevant = company_relevance(disney)
    assert relevant("Disney raises park prices")
    assert not relevant("Markets wrap: a quiet day for stocks")
    assert not relevant("A quiet day for stocks")
    assert relevant("Shares of $A rose")


def test_relevance_common_word_ticker_and_brand_alias():  # B3
    allstate = SimpleNamespace(ticker="ALL", company="The Allstate Corporation", short_name=None)
    assert not company_relevance(allstate)("ALL eyes on the Fed")
    assert company_relevance(allstate)("Allstate beats estimates (NYSE: ALL)")
    alphabet = SimpleNamespace(ticker="GOOGL", company="Alphabet Inc.", short_name=None)
    assert company_relevance(alphabet)("Google unveils new model")
    bofa = SimpleNamespace(ticker="BAC", company="Bank of America Corporation", short_name=None)
    assert company_relevance(bofa)("Bank of America raises dividend")
    assert not company_relevance(bofa)("America's economy adds jobs")


def test_yahoo_packets_use_relevance():  # B3
    market = make_market(ticker="A", company="The Walt Disney Company")
    rows = [_feed_row("Markets wrap: a quiet day for stocks"), _feed_row("Disney raises park prices")]
    sources, packets, omitted = yahoo_news_packets(rows, AS_OF, market)
    assert [s.title for s in sources] == ["Disney raises park prices"]
    # Company-named and recent, but published after the close: never offered as a move explanation.
    assert packets[0]["move_relevance"] == "direct" and packets[0]["later_than_price"] is True
    assert move_explanations(packets) == []


def test_dedupe_by_source_and_syndicated_title():  # B7
    a = make_source(id=2, title="Acme to buy Beta")
    b = make_source(id=3, url="https://other.example/b", title="Acme to buy Beta")
    packets = [_base_packet(a, research_purpose="news", supporting_material="x"),
               _base_packet(a, research_purpose="news", supporting_material="y", confirmation_status="unconfirmed"),
               _base_packet(b, research_purpose="news", supporting_material="z")]
    kept = dedupe_packets(packets, [a, b])
    assert len(kept) == 1 and kept[0]["confirmation_status"] == "unconfirmed"


def _digest(*topics, headline="{move}; Acme agreed to buy Beta"):
    return Digest(headline=Claim(text=headline, sources=[1, 2]), topics=list(topics), coverage_notes=[])


def _topic(heading="Deal news", *sentences):
    sentences = sentences or ("Acme agreed to buy Beta.",)
    return Topic(heading=Claim(text=heading, sources=[2]), sentences=[Claim(text=s, sources=[2]) for s in sentences])


def test_old_earnings_history_cannot_back_news_claims(market):  # B8
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, research_purpose="earnings_history", earnings_only=True)
    issues = check_digest(_digest(_topic()), market, [source], [packet], editorial_checks=False)
    assert {issue.location for issue in issues} >= {"topic_1_heading", "topic_1_sentence_1", "headline"}


def test_prune_removes_only_flagged_claims():  # B10
    digest = _digest(_topic("Deal news", "One.", "Two."), _topic("Other"))
    pruned, removed = prune_digest(digest, {"topic_1_sentence_2", "topic_2_heading"})
    assert removed == 3 and len(pruned.topics) == 1 and len(pruned.topics[0].sentences) == 1
    assert prune_digest(digest, {"topic_1_heading", "topic_2_heading"})[0] is None


def test_headline_repair_keeps_writer_context(market):  # B11
    digest = _digest(_topic(), headline="Acme agreed to buy Beta")
    repair_headline(digest, market)
    assert digest.headline.text == "Acme agreed to buy Beta" and 1 in digest.headline.sources
    digest = _digest(_topic(), headline="NVIDIA is {move}; deal {move}")
    repair_headline(digest, market)
    assert digest.headline.text.count("{move}") == 1
    digest = Digest(headline=Claim(text="Many sources", sources=[2, 3, 4, 5, 6, 7]), topics=[_topic()], coverage_notes=[])
    repair_headline(digest, market)
    assert digest.headline.sources[0] == 1 and len(digest.headline.sources) == 6
    digest = _digest(_topic(), headline="{move}")
    repair_headline(digest, market)
    assert digest.headline.text == "NVIDIA closed {move}"  # verified fallback, rendered "NVIDIA closed up 10.00%"


def test_headline_causal_language_is_an_editorial_issue(market):  # 4.2
    source = make_source(eligible_at=AS_OF)
    packet = _base_packet(source, research_purpose="news", later_than_price=None, price_timing_unknown=True)
    digest = _digest(_topic(), headline="{move} because Acme agreed to buy Beta")
    assert any(i.location == "headline" for i in check_digest(digest, market, [source], [packet]))
    digest = _digest(_topic(), headline="{move} as Acme agreed to buy Beta")
    assert any(i.location == "headline" for i in check_digest(digest, market, [source], [packet]))
    packet["later_than_price"], packet["price_timing_unknown"] = False, False
    assert not any(i.location == "headline" for i in check_digest(digest, market, [source], [packet]))


def test_empty_digest_is_flagged(market):  # B16
    issues = check_digest(Digest(headline=Claim(text="{move}", sources=[1]), topics=[], coverage_notes=[]),
                          market, [], [], editorial_checks=False)
    assert Issue("overall", "The digest has no topics.") in issues


def test_parse_research_tolerates_unknown_classification():
    text = '```json\n{"status": "findings", "findings": [{"summary": "s", "url": "https://x.com/a", ' \
           '"content_kind": "reporting", "catalyst_type": "weird", "move_relevance": "maybe", ' \
           '"event_date": "2026-10-15T00:00:00Z"}]}\n```'
    parsed, _ = parse_research(text)
    finding = parsed.findings[0]
    assert finding.catalyst_type == "other" and finding.move_relevance == "context"
    assert finding.event_date == date(2026, 10, 15)


def test_gather_or_cancel_cancels_siblings():  # B16
    cancelled = []

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        asyncio.run(gather_or_cancel(slow(), fail()))
    assert cancelled == [True]


def test_renumber_compacts_citations():
    digest = _digest(Topic(heading=Claim(text="H", sources=[179]), sentences=[Claim(text="S.", sources=[40, 179])]))
    digest.headline.sources = [1, 40]
    sources = [make_source(id=179, url="https://a.example/"), make_source(id=40, url="https://b.example/"),
               make_source(id=7, url="https://c.example/")]
    renamed, shown, headlines = renumber(digest, sources, [7])
    assert headlines == [2]
    assert renamed.headline.sources == [1, 3] and renamed.topics[0].sentences[0].sources == [3, 4]
    assert [(s.id, s.url) for s in shown] == [(2, "https://c.example/"), (3, "https://b.example/"), (4, "https://a.example/")]
    assert digest.headline.sources == [1, 40]  # the original draft is not mutated


def test_writer_schema_requires_a_topic():
    from agents import AgentOutputSchema
    from stock_digest.models import WriterDigest
    schema = AgentOutputSchema(WriterDigest).json_schema()
    assert schema["properties"]["topics"]["minItems"] == 1
    draft = WriterDigest(headline=Claim(text="{move}", sources=[1]), topics=[_topic()], coverage_notes=[])
    pruned, _ = prune_digest(draft.model_copy(update={"earnings_preview": _topic("Next event")}), {"topic_1_heading"})
    assert pruned.topics == [] and pruned.earnings_preview is not None


# ---------------------------------------------------------------- material-news developments as digest evidence

BUYBACK_URL = "https://nvidianews.example/buyback"


def material_result(*entries) -> MaterialResult:
    issuer = Issuer(ticker="NVDA", symbol="NVDA", exchange="NasdaqGS", company="NVIDIA Corporation")
    return MaterialResult(issuer=issuer, run_at=AS_OF, window_start=AS_OF - timedelta(days=7), window_end=AS_OF,
                          timezone="America/New_York", hours=168, entries=list(entries), qualified_entries=list(entries))


def entry(first_reported_at, url=BUYBACK_URL, status="authorized", category="capital_return", source_type="official_release",
          verification="opened", event_id="E1", **overrides) -> Entry:
    fields = dict(event_id=event_id, date="Sep 28, 2026", headline="NVIDIA authorizes $60B buyback", reported=False,
                  status=status, status_label=status, category=category, why="The board authorized $60 billion of repurchases.",
                  sources=[{"url": url, "publisher": "NVIDIA", "title": "NVIDIA announces buyback", "published": None,
                            "snippets": ["Board approves $60B repurchase"], "source_type": source_type}],
                  verification=verification, first_reported_at=first_reported_at,
                  key_facts=["$60 billion authorization", "no expiry"], uncertainties=["Actual purchases may vary."])
    fields.update(overrides)
    return Entry(**fields)


def test_material_today_and_previous_session_only(market):
    # Session Mon Sep 28 (close 20:00 UTC), previous session Fri Sep 25: Thursday's item stays in the Material tab.
    result = material_result(entry("2026-09-28T08:00:00-04:00"),
                             entry("2026-09-25", event_id="E2", url="https://wire.example/friday"),
                             entry("2026-09-24T09:00:00-04:00", event_id="E3", url="https://wire.example/thursday"),
                             entry(None, event_id="E4", url="https://wire.example/undated"))
    new_sources, packets, note = material_evidence(result, market, [])
    assert [s.url for s in new_sources] == [BUYBACK_URL, "https://wire.example/friday"] and [s.id for s in new_sources] == [2, 3]
    assert "2 of 4" in note and "1 older than the previous session" in note and "1 undated" in note
    today = packets[0]
    assert today["material"] and today["research_purpose"] == "material" and today["move_relevance"] == "direct"
    assert today["later_than_price"] is False and not today["price_timing_unknown"]
    assert today["confirmation_status"] == "confirmed" and today["catalyst_type"] == "buyback"
    assert today["content_kind"] == "dated_announcement" and today["evidence_provenance"] == "material_agent_verified_reading"
    assert "$60 billion authorization" in today["supporting_material"] and "Actual purchases may vary." in today["supporting_material"]
    assert new_sources[0].timestamp_provenance == "material_verified" and new_sources[0].raw_material == ["Board approves $60B repurchase"]
    assert new_sources[0].published == "2026-09-28T08:00:00-04:00"
    assert move_explanations(packets) == [2, 3]


def test_material_timing_follows_the_price_observation(market):
    late = entry("2026-09-28T17:30:00-04:00")                       # after the 16:00 ET close
    date_only = entry("2026-09-28", event_id="E2", url="https://wire.example/day", verification="search_results_only")
    _, packets, _ = material_evidence(material_result(late, date_only), market, [])
    assert packets[0]["later_than_price"] is True and move_explanations(packets) == [3]
    assert packets[1]["price_timing_unknown"] and packets[1]["later_than_price"] is None
    assert packets[1]["evidence_provenance"] == "material_agent_search_results"
    assert packets[1]["timestamp_provenance"] == "research_extraction"


def test_material_source_without_a_page_title_shows_the_development(market):
    untitled = entry("2026-09-28T08:00:00-04:00", sources=[{"url": "https://blogs.nvidia.example/post", "publisher": "blogs.nvidia.example",
                                                           "title": "blogs.nvidia.example", "snippets": [], "source_type": "official_release"}])
    new_sources, _, _ = material_evidence(material_result(untitled), market, [])
    assert new_sources[0].title == "NVIDIA authorizes $60B buyback" and new_sources[0].publisher == "blogs.nvidia.example"


def test_material_status_category_and_event_mapping(market):
    talks = entry("2026-09-28T08:00:00-04:00", status="reported_talks", category="financing", source_type="original_reporting")
    reported = entry("2026-09-28T08:00:00-04:00", event_id="E2", url="https://wire.example/r", status="reported", category="contract")
    scheduled = entry("2026-09-28T08:00:00-04:00", event_id="E3", url="https://wire.example/s", status="scheduled",
                      category="earnings", event_date="2026-10-15")
    past_event = entry("2026-09-28T08:00:00-04:00", event_id="E4", url="https://wire.example/p", event_date="2026-09-20")
    _, packets, _ = material_evidence(material_result(talks, reported, scheduled, past_event), market, [])
    assert [p["confirmation_status"] for p in packets] == ["unconfirmed", "reported", "confirmed", "confirmed"]
    assert [p["catalyst_type"] for p in packets] == ["financing", "other", "earnings", "buyback"]
    assert packets[0]["content_kind"] == "reporting" and packets[2]["content_kind"] == "dated_announcement"
    assert packets[2]["event_date"] == "2026-10-15" and packets[3]["event_date"] is None
    assert move_explanations(packets) == [2, 3, 5]      # a dated upcoming event is not a move explanation
    assert packets[0]["material_status"] == "reported_talks"


def test_material_reuses_an_existing_source_and_wins_dedupe(market):
    feed = make_source(id=2, url=BUYBACK_URL, title="NVIDIA announces buyback", published=(CLOSE - timedelta(hours=3)).isoformat(),
                       timestamp_provenance="provider_feed")
    set_source_timing(feed, market.observed_at)
    feed_packet = _base_packet(feed, research_purpose="news_feed", supporting_material="NVIDIA announces buyback",
                               later_than_price=False, story_key="nvidia announces buyback")
    new_sources, packets, _ = material_evidence(material_result(entry("2026-09-28")), market, [feed])
    assert new_sources == [] and packets[0]["source_id"] == 2
    assert feed.timestamp_provenance == "provider_feed" and not feed.price_timing_unknown     # provider time kept
    assert "Board approves $60B repurchase" in feed.raw_material
    kept = dedupe_packets(packets + [feed_packet], [feed])
    assert len(kept) == 1 and kept[0]["material"] and kept[0]["research_purpose"] == "news_feed"
