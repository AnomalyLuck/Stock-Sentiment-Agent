import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from conftest import AS_OF, FRIDAY_CLOSE, evidence, make_article, make_finding, make_market, make_source
from stock_digest.manager import (Issue, _base_packet, catalyst_packets, check_digest, dedupe_packets, eligible_packets,
                                  gather_or_cancel, move_explanations, parse_research, prune_digest, recent_headline_ids,
                                  renumber, repair_headline, set_source_timing)
from stock_digest.models import Claim, Digest, HeadlineCatalyst, Topic
from stock_digest.news import Screened, catalyst_since


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


# ---------------------------------------------------------------- screened catalysts as digest evidence

def screened(headline="NVIDIA authorizes a $60 billion buyback", status="authorized", catalyst_type="buyback",
             why="returns cash to shareholders", *articles) -> Screened:
    item = HeadlineCatalyst(article_ids=[f"a{n}" for n in range(1, len(articles) + 1)], headline=headline,
                            catalyst_type=catalyst_type, status=status, why=why)
    return Screened(item, list(articles))


def test_catalyst_packets_keep_provider_text_and_times(market):
    # Session Mon Sep 28 closes 20:00 UTC; AS_OF is 22:00 UTC.
    before = make_article("NVIDIA board approves $60 billion buyback", hours_before_as_of=8, source="Reuters",
                          snippet="The board  approved a $60 billion repurchase.")
    copy = make_article("Nvidia unveils $60B buyback plan", hours_before_as_of=7, source="CNBC")
    after = make_article("Nvidia in talks to buy Beta, sources say", hours_before_as_of=1, source="Bloomberg")
    sources, packets = catalyst_packets(
        [screened("NVIDIA authorizes a $60 billion buyback", "authorized", "buyback", "returns cash", before, copy),
         screened("NVIDIA reportedly in talks to buy Beta", "reported_talks", "rumor", "", after)],
        AS_OF, market, first_id=2)
    assert [s.id for s in sources] == [2, 3, 4]
    assert all(s.timestamp_provenance == "provider_feed" for s in sources)
    assert sources[0].publisher == "Reuters" and sources[0].raw_material == [before.title, "The board approved a $60 billion repurchase."]
    first, second, rumor = packets
    assert first["research_purpose"] == "catalyst" and first["catalyst_type"] == "buyback"
    assert first["confirmation_status"] == "confirmed" and first["move_relevance"] == "direct"
    assert first["supporting_material"] == before.title + "\nThe board approved a $60 billion repurchase."
    assert first["story_key"] == second["story_key"] != rumor["story_key"]
    assert first["later_than_price"] is False and rumor["later_than_price"] is True
    assert rumor["confirmation_status"] == "unconfirmed" and rumor["screen_status"] == "reported_talks"
    assert move_explanations(packets) == [2, 3]          # the after-close report never explains the move


def test_catalyst_reported_status_and_headline_list(market):
    article = make_article("Report: Nvidia wins Pentagon contract", hours_before_as_of=10)
    sources, packets = catalyst_packets([screened("NVIDIA reportedly wins a Pentagon contract", "reported",
                                                  "product", "", article)], AS_OF, market, first_id=2)
    assert packets[0]["confirmation_status"] == "reported"
    assert recent_headline_ids(packets, sources) == [2]


def test_catalyst_window_reaches_back_to_the_previous_close(market):
    # Monday evening: Friday's close is earlier than 24 hours ago, so the window opens there.
    assert catalyst_since(market, AS_OF) == FRIDAY_CLOSE
    # Mid-session Wednesday: the last 24 hours start before Tuesday's close.
    wednesday = make_market(comparison_session_close=datetime(2026, 9, 29, 20, 0, tzinfo=UTC))
    noon = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
    assert catalyst_since(wednesday, noon) == noon - timedelta(hours=24)
    assert catalyst_since(make_market(comparison_session_close=None), AS_OF) == AS_OF - timedelta(hours=24)
