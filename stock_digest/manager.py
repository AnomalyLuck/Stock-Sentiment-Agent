from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agents import ModelBehaviorError, RunConfig, Runner
from openai import APIError, AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError
from pydantic import ValidationError

from .agents import NEWSWIRES, REVISION, api_error_message, build_agents, domain_research
from .catalysts import fetch_catalysts, fetch_implied_move
from .costs import usage_client
from .dates import (NY, availability_bound, date_in_passage, earliest_bound, is_date_only,
                    normalize_source_timestamp, time_in_passage, timestamp_bound)
from .earnings import earnings_context, upcoming_earnings
from .market import DigestError, InputError, fetch_market, fetch_news
from .models import (CATALYST_TYPES, Claim, Digest, EarningsMetric, Finding, OptionsImpliedMove, Publication,
                     ResearchEvidence, Source, VerificationResult, extended_phrase, move_phrase, price_phrase)

# Seconds of run deadline that later stages need before they are started.
FOLLOWUP_BUDGET = 150
VERIFY_BUDGET = 100
REVISION_BUDGET = 150
CATALYST_WAIT = 30
NEWS_PURPOSES = {"news", "news_feed", "earnings_event"}
STRUCTURED_PURPOSES = {"structured_analyst", "structured_filing", "structured_earnings", "structured_insider"}
RUMOR_LABEL = re.compile(r"\bunconfirmed\b|\breportedly\b|\breported talks\b", re.I)
CERTAIN_CAUSE = re.compile(r"\b(?:because|driven by|due to|caused by|thanks to)\b", re.I)
TIMED_LINK = re.compile(r"\b(?:as|amid|following|after)\b", re.I)


def canonical_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        if any(ord(c) < 33 or ord(c) == 127 for c in value):
            return None
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))
    except ValueError:
        return None


def normalized_title(value: str) -> str:
    return re.sub(r"\W+", " ", value.lower()).strip()


def price_timing_unknown(value: str | None, retrieved_at: datetime, observed_at: datetime) -> bool:
    """A date-only stamp whose possible publication window straddles the price observation."""
    normalized = normalize_source_timestamp(value)
    if not is_date_only(normalized):
        return False
    return earliest_bound(normalized) <= observed_at < availability_bound(normalized, retrieved_at)


def set_source_timing(source: Source, observed_at: datetime) -> None:
    bounds = [availability_bound(value, source.retrieved_at) for value in (source.published, source.updated)]
    source.eligible_at = max((bound for bound in bounds if bound is not None), default=None)
    source.price_timing_unknown = any(price_timing_unknown(value, source.retrieved_at, observed_at)
                                      for value in (source.published, source.updated))


def later_than_price(source: Source, observed_at: datetime) -> bool | None:
    if source.price_timing_unknown or source.eligible_at is None:
        return None
    return source.eligible_at > observed_at


# ---------------------------------------------------------------- research parsing

def parse_research(text: str) -> tuple[ResearchEvidence, list[str]]:
    """Accept complete JSON objects regardless of Markdown wrapper formatting.

    Validate findings separately so one bad record cannot erase valid siblings.
    Never repair incomplete claims, quotations, or URLs. A broken outer document
    can only contribute complete, independently decoded finding objects.
    """
    decoder = json.JSONDecoder()
    report = None
    records = []
    cursor = 0
    while (start := text.find("{", cursor)) >= 0:
        try:
            obj, end = decoder.raw_decode(text, start)
            cursor = end
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("findings"), list):
            report, records = obj, obj["findings"]
            break
        if isinstance(obj, dict) and "summary" in obj and "url" in obj:
            records.append(obj)
    if report is None and not records:
        raise DigestError("Research returned no complete JSON findings (the response may have been truncated).")
    notes = []
    if report is None:
        notes.append("Research response formatting was incomplete; only complete, valid finding records were retained.")
    valid = []
    invalid = 0
    for item in records[:6]:
        if not isinstance(item, dict):
            invalid += 1
            continue
        item = {key: value for key, value in item.items() if key in Finding.model_fields}
        for key in ("reported_excerpt", "published", "updated", "timestamp_basis", "event_date", "earnings_date"):
            item.setdefault(key, None)
        item.setdefault("story_key", "")
        # Classification fields are optional hints; an unknown label must not drop the finding.
        if item.get("catalyst_type") not in CATALYST_TYPES:
            item["catalyst_type"] = "other"
        if item.get("move_relevance") not in {"direct", "context"}:
            item["move_relevance"] = "context"
        for key in ("published", "updated", "event_date", "earnings_date"):
            if isinstance(item[key], str):
                normalized = normalize_source_timestamp(item[key]) or item[key]
                # Event fields are dates; drop any time component a model attached.
                item[key] = normalized[:10] if key in {"event_date", "earnings_date"} else normalized
        metrics = item.pop("earnings_metrics", [])
        item["earnings_metrics"] = []
        if isinstance(metrics, list):
            for metric in metrics[:8]:
                try:
                    item["earnings_metrics"].append(EarningsMetric.model_validate(metric))
                except ValidationError:
                    notes.append("An incomplete earnings metric was omitted; its source finding was retained.")
        options = item.pop("options_implied_move", None)
        if options is not None:
            try:
                item["options_implied_move"] = OptionsImpliedMove.model_validate(options)
            except ValidationError:
                notes.append("An incomplete options-implied move was omitted; its source finding was retained.")
        try:
            valid.append(Finding.model_validate(item))
        except ValidationError:
            invalid += 1
    if invalid:
        notes.append(f"{invalid} malformed research finding(s) omitted; valid findings from the same query were retained.")
    if len(records) > 6:
        notes.append("Research findings beyond the six-per-query limit were omitted.")
    declared_status = report.get("status") if report else None
    status = "findings" if valid else (
        "no_relevant_results" if not records and declared_status == "no_relevant_results" else "evidence_unavailable"
    )
    return ResearchEvidence(query="", status=status, findings=valid, coverage_issues=[]), notes


def extract_sources(result, retrieved_at: datetime) -> tuple[list[Source], int]:
    """Use SDK output objects, not URLs or alleged quotations in generated prose."""
    registry: dict[str, Source] = {}
    calls = []

    def collect(metadata: dict):
        url = canonical_url(metadata.get("url", ""))
        if not url:
            return
        source = registry.setdefault(url, Source(
            id=0, url=url, title=metadata.get("title") or url,
            publisher=urlsplit(url).hostname or "Unknown publisher",
            retrieved_at=retrieved_at, raw_material=[],
        ))
        for key in ("title", "text", "snippet", "content", "description"):
            material = metadata.get(key)
            if isinstance(material, str) and material.strip() and material not in source.raw_material:
                source.raw_material.append(material)
        if metadata.get("title"):
            source.title = metadata["title"]
        for field, keys in (("published", ("published_at", "published_date", "publication_date")),
                            ("updated", ("updated_at", "updated_date", "last_updated"))):
            for key in keys:
                # Store normalized values so every later consumer can parse them.
                value = normalize_source_timestamp(metadata.get(key))
                if value and timestamp_bound(value):
                    existing = getattr(source, field)
                    # Use the later timestamp on inconsistent duplicate records.
                    if existing is None or timestamp_bound(value) > timestamp_bound(existing):
                        setattr(source, field, value)
                        source.timestamp_provenance = "tool_metadata"

    for response in result.raw_responses:
        for output in response.output:
            item = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
            if item.get("type") == "web_search_call":
                calls.append(item)
                if item.get("status") == "completed":
                    for source in (item.get("action") or {}).get("sources") or []:
                        collect(source)
            elif item.get("type") == "message":
                for content in item.get("content") or []:
                    for annotation in content.get("annotations") or []:
                        if annotation.get("type") == "url_citation":
                            collect(annotation)
    completed = [call for call in calls if call.get("status") == "completed"]
    if len(completed) != 1 or (completed[0].get("action") or {}).get("type") != "search":
        raise DigestError("Research did not complete exactly one hosted search.")
    # max_tool_calls can leave an unexecuted open_page attempt marked 'searching'.
    # It contributes no source metadata and cannot erase the completed search.
    return list(registry.values()), len(completed)


def _base_packet(source: Source, **fields) -> dict:
    packet = {
        "source_id": source.id, "researcher_summary_not_independent_evidence": None,
        "supporting_material": None, "evidence_provenance": None, "reported_timestamp_basis": None,
        "timestamp_provenance": source.timestamp_provenance, "content_kind": "reporting",
        "confirmation_status": "reported", "catalyst_type": "other", "move_relevance": "context",
        "event_date": None, "earnings_date": None, "later_than_price": None, "price_timing_unknown": False,
        "story_key": "", "earnings_metrics": [], "options_implied_move": None, "structured": False,
        "earnings_only": False, "material": False,
    }
    packet.update(fields)
    return packet


def eligible_packets(evidence, sources, market, days, feed_titles: dict[str, Source] | None = None):
    packets = []
    omitted = Counter()
    seen_stories = set()
    by_url = {source.url: source for source in sources}
    feed_titles = feed_titles or {}
    for finding in evidence.findings:
        source = by_url.get(canonical_url(finding.url))
        if source is None:
            omitted["no matching URL in search metadata/citations"] += 1
            continue
        raw = "\n".join(source.raw_material)
        passage = finding.reported_excerpt
        provenance = "research_extraction"
        if passage and passage in raw:
            provenance = "tool_returned_material"
        elif not passage:
            passage = source.title if source.title != source.url else None
            provenance = "tool_returned_title_only"
        published, updated = source.published, source.updated
        basis = finding.timestamp_basis
        timestamp_provenance = source.timestamp_provenance
        provider_dated = timestamp_provenance in {"provider_feed", "provider_structured"}
        if not provider_dated:
            # Research dates must be quoted; a model-written clock time counts only if the
            # dateline shows it with a matching timezone, otherwise only its date is kept.
            def substantiated(value):
                if not date_in_passage(value, basis, source.retrieved_at):
                    return None
                normalized = normalize_source_timestamp(value)
                return normalized if is_date_only(normalized) or time_in_passage(normalized, basis) else normalized[:10]
            if not published and (value := substantiated(finding.published)):
                published, timestamp_provenance = value, "research_extraction"
            if not updated and (value := substantiated(finding.updated)):
                updated, timestamp_provenance = value, "research_extraction"
            if not published and not updated and source.title != source.url:
                # The same headline in Yahoo's feed carries a provider timestamp.
                twin = feed_titles.get(normalized_title(source.title))
                if twin is not None:
                    published, timestamp_provenance = twin.published, "provider_feed"
        pub_bound = availability_bound(published, source.retrieved_at)
        update_bound = availability_bound(updated, source.retrieved_at)
        bound = max((b for b in (pub_bound, update_bound) if b is not None), default=None)
        # An acknowledged update cannot be silently ignored if its time is unsubstantiated.
        uncertain_update = bool(finding.updated and update_bound is None and not provider_dated)
        if not passage:
            omitted["no supporting passage or source title"] += 1
            continue
        if bound is None or uncertain_update or (finding.content_kind == "mutable_page" and update_bound is None):
            omitted["publication/update date missing or not substantiated by a quoted dateline"] += 1
            continue
        news_cutoff = max(market.as_of, source.retrieved_at)
        if bound > news_cutoff:
            omitted["publication/update not established before the cutoff"] += 1
            continue
        event_date = finding.event_date
        if event_date:
            local_day = market.as_of.astimezone(NY).date()
            # Past events, far-off events, and events only reported by others are ordinary news.
            if not (local_day <= event_date <= local_day + timedelta(days=30)) or finding.content_kind != "dated_announcement":
                event_date = None
        if event_date is None and bound < news_cutoff - timedelta(days=days):
            omitted["news outside the search window"] += 1
            continue
        story = normalized_title(finding.story_key)
        title = normalized_title(source.title)
        if not (finding.earnings_metrics or finding.options_implied_move) and (
            (story and story in seen_stories) or (source.title != source.url and title in seen_stories)
        ):
            continue
        seen_stories.update((story, title))
        if not provider_dated:
            source.published, source.updated = published, updated
            source.timestamp_provenance = timestamp_provenance
        set_source_timing(source, market.observed_at)
        packets.append(_base_packet(
            source,
            researcher_summary_not_independent_evidence=finding.summary,
            supporting_material=passage, evidence_provenance=provenance,
            reported_timestamp_basis=basis, timestamp_provenance=timestamp_provenance,
            content_kind=finding.content_kind, confirmation_status=finding.confirmation_status,
            catalyst_type=finding.catalyst_type, move_relevance=finding.move_relevance,
            event_date=str(event_date) if event_date else None,
            earnings_date=str(finding.earnings_date) if finding.earnings_date else None,
            later_than_price=later_than_price(source, market.observed_at),
            price_timing_unknown=source.price_timing_unknown, story_key=story,
            earnings_metrics=[metric.model_dump(mode="json") for metric in finding.earnings_metrics],
            options_implied_move=finding.options_implied_move.model_dump(mode="json") if finding.options_implied_move else None,
        ))
    return packets, omitted


# ---------------------------------------------------------------- Yahoo news relevance

LEGAL_WORDS = {"inc", "incorporated", "corp", "corporation", "company", "co", "ltd", "limited", "plc",
               "holdings", "holding", "group", "sa", "nv", "ag", "se", "llc", "lp", "the", "class",
               "adr", "ads", "common", "stock", "shares", "ordinary", "&", "and"}
GENERIC_WORDS = {
    "the", "of", "and", "bank", "general", "united", "american", "america", "americas", "national",
    "international", "first", "global", "advanced", "applied", "digital", "new", "north", "south", "east",
    "west", "super", "micro", "technology", "technologies", "tech", "systems", "software", "energy",
    "pharmaceuticals", "pharmaceutical", "pharma", "therapeutics", "semiconductor", "semiconductors",
    "financial", "services", "bancorp", "communications", "networks", "industries", "entertainment",
    "brands", "foods", "resources", "solutions", "partners", "capital", "health", "healthcare", "labs",
    "laboratories", "sciences", "biosciences", "bio", "devices", "instruments", "electric", "power",
    "petroleum", "oil", "gas", "motor", "motors", "automotive", "airlines", "airline", "air", "express",
    "trust", "insurance", "realty", "properties", "medical", "data", "media", "interactive", "retail",
    "stores", "restaurants", "hotels", "resorts", "lines", "mining", "gold", "steel", "chemical",
    "chemicals", "materials", "logistics", "transport", "platforms", "computer", "computers", "one",
    "enterprises", "worldwide", "royal", "street", "wireless", "electronics", "semi", "machines", "business",
}
COMMON_TICKER_WORDS = {
    "ALL", "ON", "IT", "NOW", "LOW", "KEY", "SO", "HAS", "AN", "BE", "ARE", "CAN", "FOR", "AND", "THE",
    "ONE", "OUT", "BIG", "CAR", "DAY", "FUN", "GO", "NEW", "OPEN", "PLAY", "RUN", "SEE", "TWO", "WELL",
    "WAY", "YOU", "ANY", "CAT", "EAT", "FAST", "FIVE", "GOOD", "HOME", "HOPE", "JOB", "LIFE", "LOVE",
    "MAIN", "MAN", "NICE", "PEAK", "REAL", "SAFE", "SHOP", "TRUE", "WING", "ZIP", "CEO", "AI", "EV",
    "US", "UK", "IPO", "ETF", "SEC", "FDA", "GDP", "CPI", "FED", "API", "AM", "PM", "OR", "IN", "AT",
    "BY", "UP", "MORE", "BEST", "CASH", "FLY", "HE", "SHE", "WE", "MY", "ME", "NO", "YES", "TECH", "NICE",
}
BRAND_ALIASES = {"GOOGL": ("Google",), "GOOG": ("Google",), "META": ("Facebook", "Instagram", "WhatsApp"),
                 "BAC": ("BofA",), "AXP": ("Amex",), "JNJ": ("J&J",), "PG": ("P&G",), "KO": ("Coke",),
                 "GM": ("GM",), "BRK.A": ("Berkshire",), "BRK.B": ("Berkshire",), "HPE": ("HPE",)}


def core_name(name: str) -> str:
    words = [w.strip(",.()") for w in name.replace(",", " ").split()]
    words = [w for w in words if w]
    while words and words[0].lower() == "the":
        words.pop(0)
    while words and words[-1].lower().rstrip(".") in LEGAL_WORDS:
        words.pop()
    return " ".join(words) or name


def company_aliases(market) -> list[str]:
    names = []
    for name in filter(None, (market.company, getattr(market, "short_name", None))):
        core = core_name(name)
        names.append(core)
        distinctive = [w for w in core.split() if len(w) > 2 and w.lower() not in GENERIC_WORDS]
        names.extend(distinctive[:2])
    names.extend(BRAND_ALIASES.get(market.ticker, ()))
    return list(dict.fromkeys(names))


def company_relevance(market):
    """Match company names case-insensitively, and the ticker only in unambiguous forms."""
    names = company_aliases(market)
    name_re = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(name) for name in names) + r")(?!\w)", re.I)
    tickers = {market.ticker, market.ticker.replace(".", "-")}
    forms = []
    for ticker in tickers:
        t = re.escape(ticker)
        forms += [rf"\${t}\b", rf"\({t}\)", rf"\b(?:NYSE|NASDAQ|Nasdaq|NYSE American|AMEX|Cboe)\s*:\s*{t}\b"]
        if len(ticker.replace(".", "").replace("-", "")) >= 3 and ticker not in COMMON_TICKER_WORDS:
            forms.append(rf"(?<![A-Za-z0-9$.\-]){t}(?![A-Za-z0-9])")
    ticker_re = re.compile("|".join(forms))
    return lambda text: bool(name_re.search(text) or ticker_re.search(text))


def yahoo_news_packets(rows, retrieved_at: datetime, market, first_id: int = 2) -> tuple[list[Source], list[dict], Counter]:
    """Read provider titles, summaries and dates without asking a model to extract them."""
    sources = []
    packets = []
    omitted = Counter()
    seen = set()
    # Yahoo's ticker stream can contain unrelated stories; require a visible match.
    relevant = company_relevance(market)
    for row in rows:
        content = row.get("content")
        if not isinstance(content, dict):
            omitted["malformed feed item"] += 1
            continue
        title = content.get("title")
        summary = content.get("summary")
        summary = summary if isinstance(summary, str) else ""
        if not isinstance(title, str) or not title.strip() or not relevant(title + " " + summary):
            omitted["not visibly company-related"] += 1
            continue
        link = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        url = canonical_url(link.get("url", "")) if isinstance(link, dict) else None
        published = normalize_source_timestamp(content.get("pubDate")) if isinstance(content.get("pubDate"), str) else None
        bound = timestamp_bound(published)
        if not url or bound is None:
            omitted["missing feed URL/publication date"] += 1
            continue
        if not retrieved_at - timedelta(days=3) <= bound <= retrieved_at:
            omitted["feed date outside the current 72-hour window"] += 1
            continue
        key = normalized_title(title)
        if url in seen or key in seen:
            continue
        seen.update((url, key))
        publisher = content.get("provider") or {}
        publisher = publisher.get("displayName") if isinstance(publisher, dict) else None
        source = Source(id=first_id + len(sources), url=url, title=title,
                        publisher=publisher if isinstance(publisher, str) else (urlsplit(url).hostname or "Yahoo Finance"),
                        retrieved_at=retrieved_at, raw_material=[title, summary], published=published,
                        eligible_at=bound, timestamp_provenance="provider_feed")
        set_source_timing(source, market.observed_at)
        sources.append(source)
        packets.append(_base_packet(
            source, research_purpose="news_feed", company_in_headline=relevant(title),
            # A company-named headline from the last 36 hours may bear on the move (timing still applies).
            move_relevance="direct" if relevant(title) and bound >= market.observed_at - timedelta(hours=36) else "context",
            researcher_summary_not_independent_evidence=summary or title,
            supporting_material=title + ("\n" + summary if summary else ""),
            evidence_provenance="provider_headline_and_summary",
            reported_timestamp_basis="Yahoo Finance pubDate: " + published, story_key=key,
            later_than_price=later_than_price(source, market.observed_at),
            price_timing_unknown=source.price_timing_unknown,
        ))
    return sources, packets, omitted


# ---------------------------------------------------------------- structured catalysts

def _money(value: float | None) -> str:
    if value is None:
        return "n/a"
    for size, suffix in ((1e12, "trillion"), (1e9, "billion"), (1e6, "million")):
        if abs(value) >= size:
            return f"${value / size:,.2f} {suffix}"
    return f"${value:,.2f}"


def structured_evidence(catalysts: dict, market, days: int, next_id: int) -> tuple[list[Source], list[dict], dict]:
    """Turn provider records into sources and evidence packets with per-record timing."""
    retrieved = datetime.fromisoformat(catalysts["retrieved_at"])
    symbol = catalysts["symbol"]
    sources: list[Source] = []
    packets: list[dict] = []
    ids: dict = {}
    cutoff = retrieved - timedelta(days=days)

    def add_source(url, title, publisher, published=None) -> Source:
        source = Source(id=next_id + len(sources), url=url, title=title, publisher=publisher,
                        retrieved_at=retrieved, raw_material=[], published=published or retrieved.isoformat(),
                        timestamp_provenance="provider_structured")
        set_source_timing(source, market.observed_at)
        sources.append(source)
        return source

    def timing(stamp: str) -> dict:
        normalized = normalize_source_timestamp(stamp)
        unknown = price_timing_unknown(normalized, retrieved, market.observed_at)
        bound = availability_bound(normalized, retrieved)
        return {"later_than_price": None if unknown else bound > market.observed_at, "price_timing_unknown": unknown}

    analysis_url = f"https://finance.yahoo.com/quote/{symbol}/analysis/"
    actions = [a for a in catalysts.get("analyst_actions") or [] if datetime.fromisoformat(a["at"]) >= cutoff]
    estimates = catalysts.get("estimates")
    targets = catalysts.get("price_targets")
    if actions or estimates or targets:
        analysis = add_source(analysis_url, "Analyst estimates, revisions, price targets and rating changes",
                              "Yahoo Finance")
        ids["analysis"] = analysis.id
        for action in actions:
            verb = {"up": "upgraded", "down": "downgraded", "init": "initiated coverage", "reit": "reiterated",
                    "main": "maintained"}.get(action["action"], action["action"] or "updated")
            grade = f" at {action['to_grade']}" if action["to_grade"] else ""
            if action["from_grade"] and action["action"] in {"up", "down"}:
                grade = f" to {action['to_grade']} from {action['from_grade']}"
            target = ""
            if action["price_target"]:
                verb_target = {"Raises": "raised", "Lowers": "lowered", "Maintains": "maintained",
                               "Announces": "set"}.get(action["price_target_action"], "set")
                target = f"; price target {verb_target} to ${action['price_target']:,.2f}"
                if action["prior_price_target"] and action["prior_price_target"] != action["price_target"]:
                    target += f" from ${action['prior_price_target']:,.2f}"
            text = f"{action['firm']} {verb}{grade}{target} ({action['at'][:16].replace('T', ' ')} UTC)."
            packets.append(_base_packet(
                analysis, research_purpose="structured_analyst", supporting_material=text,
                researcher_summary_not_independent_evidence=text, evidence_provenance="provider_structured_data",
                reported_timestamp_basis="Yahoo Finance rating-change timestamp: " + action["at"],
                confirmation_status="confirmed", catalyst_type="analyst",
                move_relevance="direct" if action["action"] in {"up", "down", "init"} else "context",
                structured=True, story_key=normalized_title(f"{action['firm']} {action['action']}"), **timing(action["at"]),
            ))
        if targets:
            text = ("Analyst price targets (retrieval-time snapshot): mean " + _money(targets.get("mean"))
                    + ", median " + _money(targets.get("median")) + ", range " + _money(targets.get("low"))
                    + " to " + _money(targets.get("high")) + ".")
            packets.append(_base_packet(
                analysis, research_purpose="structured_context", supporting_material=text,
                researcher_summary_not_independent_evidence=text, evidence_provenance="provider_structured_data",
                reported_timestamp_basis="Retrieved " + retrieved.isoformat(), confirmation_status="confirmed",
                catalyst_type="analyst", structured=True, story_key="analyst price targets",
                later_than_price=None, price_timing_unknown=False,
            ))
    if catalysts.get("revenue_history"):
        ids["financials"] = add_source(f"https://finance.yahoo.com/quote/{symbol}/financials/",
                                       "Quarterly income statement", "Yahoo Finance").id
    earnings = catalysts.get("earnings") or {}
    if earnings.get("next") or earnings.get("last_report"):
        calendar = add_source(f"https://finance.yahoo.com/calendar/earnings?symbol={symbol}",
                              "Earnings calendar and reported results", "Yahoo Finance")
        ids["calendar"] = calendar.id
        report = earnings.get("last_report")
        if report and datetime.fromisoformat(report["at"]) >= cutoff:
            revenue = (catalysts.get("revenue_history") or [{}])[0]
            text = (f"Reported quarterly results at {report['at'][:16].replace('T', ' ')} UTC: EPS "
                    f"{report['reported_eps']:.2f} vs. {report['eps_estimate']:.2f} consensus "
                    f"(surprise {report['surprise_percent']:+.2f}%)" if report["eps_estimate"] is not None else
                    f"Reported quarterly EPS {report['reported_eps']:.2f} at {report['at'][:16]} UTC")
            if revenue.get("revenue"):
                text += f"; total revenue {_money(revenue['revenue'])} for the period ended {revenue['period_end']}"
            packets.append(_base_packet(
                calendar, research_purpose="structured_earnings", supporting_material=text + ".",
                researcher_summary_not_independent_evidence=text + ".", evidence_provenance="provider_structured_data",
                reported_timestamp_basis="Yahoo Finance earnings timestamp: " + report["at"],
                content_kind="dated_announcement", confirmation_status="confirmed", catalyst_type="earnings",
                move_relevance="direct", structured=True, story_key="quarterly results", **timing(report["at"]),
            ))
        upcoming = earnings.get("next")
        if upcoming:
            status = "estimated" if upcoming.get("estimated") else "scheduled"
            ids["event"] = {
                "source_id": calendar.id, "event_date": upcoming["date"], "event_status": status,
                "event_timing": upcoming.get("timing", "unknown"),
                "supporting_material": f"Yahoo Finance earnings calendar: next earnings release {upcoming['date']} "
                                       f"({status}; release timing {upcoming.get('timing', 'unknown').replace('_', ' ')}).",
                "content_kind": "mutable_page", "confirmation_status": "reported",
            }
    for filing in catalysts.get("filings") or []:
        stamp = filing["accepted_at"] or filing["date"]
        source = add_source(filing["url"], filing["title"], filing["provider"], published=stamp)
        items = filing.get("items") or []
        catalyst = next((kind for code in items for kind in [ITEM_TYPES.get(code)] if kind), None)
        if catalyst is None:
            catalyst = "earnings" if filing["form"] in {"10-Q", "10-K", "20-F"} else (
                "financing" if filing["form"].startswith(("424B", "S-1", "S-3")) else
                "merger_acquisition" if filing["form"] in {"S-4", "DEFM14A", "SC TO-T", "SC 14D9", "SC 13D", "SC 13D/A"} else "other")
        accepted = f", accepted {filing['accepted_at']}" if filing["accepted_at"] else ""
        text = f"{filing['form']} filed {filing['date']}{accepted}: {filing['title']}."
        packets.append(_base_packet(
            source, research_purpose="structured_filing", supporting_material=text,
            researcher_summary_not_independent_evidence=text, evidence_provenance="provider_structured_data",
            reported_timestamp_basis=f"{filing['provider']} filing date: {stamp}", content_kind="dated_announcement",
            confirmation_status="confirmed", catalyst_type=catalyst,
            move_relevance="direct" if catalyst != "other" or filing["form"] == "8-K" else "context",
            structured=True, story_key=normalized_title(filing["url"]), **timing(stamp),
        ))
    insider = [row for row in catalysts.get("insider") or []]
    if insider:
        source = add_source(f"https://finance.yahoo.com/quote/{symbol}/insider-transactions/",
                            "Insider transactions", "Yahoo Finance")
        for row in insider:
            value = f"; value {_money(row['value'])}" if row["value"] else ""
            shares = f"{row['shares']:,.0f} shares" if row["shares"] else "shares"
            text = (f"Transaction date {row['transaction_date']} (Yahoo does not give the disclosure date): "
                    f"{row['insider']}, {row['position']}: {row['description']} {shares}{value}.")
            packets.append(_base_packet(
                source, research_purpose="structured_insider", supporting_material=text,
                researcher_summary_not_independent_evidence=text, evidence_provenance="provider_structured_data",
                reported_timestamp_basis="Transaction date " + row["transaction_date"], confirmation_status="confirmed",
                catalyst_type="insider", structured=True, story_key=normalized_title(text[:60]),
                later_than_price=None, price_timing_unknown=True,
            ))
    for key, label in (("exDividendDate", "ex-dividend date"),):
        day = catalysts.get(key)
        today = market.as_of.astimezone(NY).date()
        if day and ids.get("calendar") and today.isoformat() <= day:
            calendar = next(s for s in sources if s.id == ids["calendar"])
            within = datetime.fromisoformat(day).date() <= today + timedelta(days=30)
            packets.append(_base_packet(
                calendar, event_date=day if within else None,
                research_purpose="structured_context", supporting_material=f"Yahoo Finance {label}: {day}.",
                researcher_summary_not_independent_evidence=f"Upcoming {label} {day}.",
                evidence_provenance="provider_structured_data", content_kind="mutable_page",
                confirmation_status="reported", catalyst_type="dividend", structured=True,
                story_key=label, later_than_price=None,
            ))
    return sources, packets, ids


ITEM_TYPES = {"2.02": "earnings", "2.01": "merger_acquisition", "5.01": "merger_acquisition", "5.02": "leadership",
              "2.03": "financing", "3.02": "financing", "1.03": "legal_regulatory", "3.01": "legal_regulatory",
              "4.02": "legal_regulatory", "1.05": "legal_regulatory"}


# ---------------------------------------------------------------- material-news developments

# Material-news statuses the company, a filing or the deciding authority confirmed, versus talks and rumors.
MATERIAL_CONFIRMED = {"announced", "authorized", "agreed", "approved", "completed", "launched", "filed", "ruled", "scheduled"}
MATERIAL_UNCONFIRMED = {"reported_talks", "under_consideration", "unconfirmed_report"}
MATERIAL_OFFICIAL_SOURCES = {"official_release", "filing", "regulator"}
MATERIAL_CATALYSTS = {
    "earnings": "earnings", "guidance": "guidance", "capital_return": "buyback", "financing": "financing",
    "merger_acquisition": "merger_acquisition", "divestiture": "merger_acquisition",
    "strategic_investment": "merger_acquisition", "product_platform": "product",
    "regulatory_market_access": "legal_regulatory", "litigation": "legal_regulatory",
    "investigation": "legal_regulatory", "incident": "legal_regulatory", "leadership": "leadership",
}


def local_day(value: str | None) -> date | None:
    """The New York calendar day of a date-only or offset-aware stamp; None when unparseable."""
    normalized = normalize_source_timestamp(value)
    if not normalized:
        return None
    if is_date_only(normalized):
        return date.fromisoformat(normalized)
    return datetime.fromisoformat(normalized).astimezone(NY).date()


def material_evidence(result, market, sources: list[Source]) -> tuple[list[Source], list[dict], str]:
    """Material-news developments first reported today or in the previous session, as sources and packets.

    The material agent already verified these developments by opening their sources. Its key
    facts are agent prose, so they go into the packet's supporting material (labeled as such),
    while only tool-returned snippets become raw source material. Entries older than the
    previous trading session stay in the Material news tab. Returns (new sources, packets, note).
    """
    by_url = {source.url: source for source in sources}
    new_sources: list[Source] = []
    packets: list[dict] = []
    skipped = Counter()
    entries = list(getattr(result, "qualified_entries", None) or result.entries)
    for entry in entries:
        day = local_day(entry.first_reported_at)
        if day is None:
            skipped["undated"] += 1
            continue
        if day < market.comparison_date:
            skipped["older than the previous session"] += 1
            continue
        link = entry.sources[0] if entry.sources else {}
        url = canonical_url(link.get("url") or "")
        if not url:
            skipped["no usable source URL"] += 1
            continue
        opened = entry.verification == "opened"
        provenance = "material_verified" if opened else "research_extraction"
        snippets = [s.strip() for s in link.get("snippets") or [] if isinstance(s, str) and s.strip()]
        source = by_url.get(url)
        if source is None:
            publisher = link.get("publisher") or urlsplit(url).hostname or "Unknown publisher"
            # Search metadata often lacks a page title (the registry then holds the host); show the development instead.
            title = link.get("title") or ""
            if not title or title in {publisher, urlsplit(url).hostname, link.get("url")}:
                title = entry.headline
            source = Source(id=len(sources) + len(new_sources) + 2, url=url, title=title, publisher=publisher,
                            retrieved_at=result.run_at, raw_material=snippets,
                            published=normalize_source_timestamp(entry.first_reported_at), timestamp_provenance=provenance)
            new_sources.append(source)
            by_url[url] = source
        else:
            source.raw_material = list(dict.fromkeys(source.raw_material + snippets))
            if source.published is None and source.updated is None:
                source.published = normalize_source_timestamp(entry.first_reported_at)
                source.timestamp_provenance = provenance
        set_source_timing(source, market.observed_at)
        status = entry.status
        confirmation = ("confirmed" if status in MATERIAL_CONFIRMED else
                        "unconfirmed" if status in MATERIAL_UNCONFIRMED else "reported")
        event_date = None
        event_day = local_day(entry.event_date)
        today = market.as_of.astimezone(NY).date()
        if event_day and confirmation == "confirmed" and today <= event_day <= today + timedelta(days=30):
            event_date = event_day.isoformat()
        facts = [entry.headline, entry.why, *entry.key_facts]
        material = "\n".join(dict.fromkeys(f.strip() for f in facts if isinstance(f, str) and f.strip()))
        if entry.uncertainties:
            material += "\nStated uncertainties: " + " ".join(u.strip() for u in entry.uncertainties if u.strip())
        packets.append(_base_packet(
            source, research_purpose="material", material=True, company_in_headline=True,
            researcher_summary_not_independent_evidence=entry.why, supporting_material=material,
            evidence_provenance="material_agent_verified_reading" if opened else "material_agent_search_results",
            reported_timestamp_basis=f"Material-news agent first report: {entry.first_reported_at}",
            timestamp_provenance=source.timestamp_provenance,
            content_kind="dated_announcement" if link.get("source_type") in MATERIAL_OFFICIAL_SOURCES else "reporting",
            confirmation_status=confirmation, catalyst_type=MATERIAL_CATALYSTS.get(entry.category, "other"),
            move_relevance="direct", event_date=event_date, story_key=normalized_title(entry.event_id),
            later_than_price=later_than_price(source, market.observed_at),
            price_timing_unknown=source.price_timing_unknown,
            material_status=entry.status_label, material_category=entry.category,
        ))
    note = (f"Material news: {len(packets)} of {len(entries)} qualifying development(s) dated today or the previous "
            f"session supplied to the writer")
    if skipped:
        note += " (" + "; ".join(f"{count} {reason}" for reason, count in skipped.most_common()) + ")"
    return new_sources, packets, note + "."


# ---------------------------------------------------------------- cleanup and selection

def dedupe_packets(packets: list[dict], sources: list[Source]) -> list[dict]:
    """One news packet per source and per headline; metric/event packets stay distinct."""
    titles = {source.id: normalized_title(source.title) for source in sources if source.title != source.url}
    kept: dict = {}
    by_title: dict = {}
    for packet in packets:
        if packet["earnings_metrics"] or packet["options_implied_move"]:
            key = ("metrics", packet["source_id"], json.dumps(packet["earnings_metrics"], sort_keys=True),
                   json.dumps(packet["options_implied_move"], sort_keys=True))
        elif packet["structured"]:
            key = ("structured", packet["source_id"], packet["supporting_material"])
        elif packet["event_date"] or packet["earnings_date"]:
            key = ("event", packet["source_id"], packet["event_date"], packet["earnings_date"])
        else:
            key = ("news", packet["source_id"])
            title = titles.get(packet["source_id"])
            if title and title in by_title and key not in kept:
                key = by_title[title]  # A syndicated copy of an already-kept story.
            elif title:
                by_title.setdefault(title, key)
        if key in kept:
            prior = kept[key]
            # Keep the most cautious status and the direct-relevance hint from any copy.
            if packet["confirmation_status"] == "unconfirmed":
                prior["confirmation_status"] = "unconfirmed"
            if packet["move_relevance"] == "direct":
                prior["move_relevance"] = "direct"
            if prior.get("research_purpose") not in NEWS_PURPOSES and packet.get("research_purpose") in NEWS_PURPOSES:
                prior["research_purpose"] = packet["research_purpose"]
            continue
        kept[key] = packet
    return list(kept.values())


def recent_headline_ids(packets, sources) -> list[int]:
    """Keep actual source titles visible independently of writer topic selection."""
    eligible = {packet["source_id"] for packet in packets
                if (packet.get("research_purpose") in {"news", "news_feed"} or packet.get("material"))
                and not packet.get("earnings_only")}
    direct = {packet["source_id"] for packet in packets if packet.get("company_in_headline")}
    candidates = [source for source in sources if source.id in eligible and source.title != source.url]
    candidates.sort(key=lambda source: (source.timestamp_provenance == "provider_feed", source.id in direct, source.eligible_at), reverse=True)
    selected, titles = [], set()
    for source in candidates:
        title = normalized_title(source.title)
        if title not in titles:
            selected.append(source.id)
            titles.add(title)
        if len(selected) == 6:
            break
    return selected


def move_explanations(packets: list[dict]) -> list[int]:
    """Evidence that could bear on the measured move: direct, not later than the price, not old."""
    ids = []
    for packet in packets:
        if (packet["move_relevance"] == "direct" and packet["later_than_price"] is not True
                and not packet["earnings_only"] and not packet["event_date"]
                and packet.get("research_purpose") != "structured_context"):
            ids.append(packet["source_id"])
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------- digest checks

@dataclass
class Issue:
    location: str
    message: str


def fallback_headline(market) -> Claim:
    """A plain, verified headline used when the writer's headline cannot be published."""
    verb = "closed" if market.price_type == "completed regular-session close" else "is"
    return Claim(text=f"{core_name(market.company)} {verb} {{move}}", sources=[1])


def repair_headline(digest: Digest, market) -> str | None:
    """Repair the optional price token and the market-data citation, keeping the writer's wording."""
    headline = digest.headline
    repairs = []
    rendered = price_phrase(market)
    if rendered in headline.text and "{move}" not in headline.text:
        # The long price line belongs in the header; the headline uses the compact token.
        headline.text = headline.text.replace(rendered, "{move}", 1)
        repairs.append("shortened the price phrase")
    if headline.text.count("{move}") > 1:
        first = headline.text.index("{move}") + len("{move}")
        headline.text = " ".join((headline.text[:first] + headline.text[first:].replace("{move}", "")).split())
        repairs.append("removed a repeated price placeholder")
    if not headline.text.replace("{move}", "").strip(" ;,.:-"):
        digest.headline = fallback_headline(market)
        return "Headline adjusted: no headline text; using the verified price summary."
    if 1 not in digest.headline.sources:
        digest.headline.sources = [1] + digest.headline.sources[:5]
        repairs.append("added market-data citation [1]")
    return "Headline adjusted: " + "; ".join(repairs) + "." if repairs else None


def check_digest(digest: Digest, market, sources: list[Source], evidence: list[dict], earnings=None, news_as_of=None,
                 *, editorial_checks=True) -> list[Issue]:
    issues: list[Issue] = []
    allowed = {source.id for source in sources if source.eligible_at and source.eligible_at <= (news_as_of or market.as_of)}
    allowed.add(1)
    unconfirmed = {packet["source_id"] for packet in evidence if packet["confirmation_status"] == "unconfirmed"}
    # Sources that only back old earnings history may appear in the earnings preview, nowhere else.
    news_ids = {packet["source_id"] for packet in evidence if not packet.get("earnings_only")}
    earnings_ids = {packet["source_id"] for packet in evidence} - news_ids
    earnings_ids -= {1}
    before_price = {packet["source_id"] for packet in evidence if packet["later_than_price"] is False}
    if digest.headline.text.count("{move}") > 1:
        issues.append(Issue("headline", "Headline price placeholder is repeated after automatic repair."))
    if 1 not in digest.headline.sources:
        issues.append(Issue("headline", "Headline market-data citation [1] is missing after automatic repair."))
    if editorial_checks:
        text = digest.headline.text
        if CERTAIN_CAUSE.search(text):
            issues.append(Issue("headline", "Headline asserts certain causation; use a semicolon or hedged wording."))
        elif TIMED_LINK.search(text) and not before_price.intersection(digest.headline.sources):
            issues.append(Issue("headline", "Headline links the move to news not established before the price; "
                                            "use a semicolon, 'coincides with', or 'may be linked to'."))
        if earnings is not None and digest.earnings_preview is None:
            issues.append(Issue("overall", "Upcoming earnings require an earnings_preview covering revenue, EPS, growth "
                                           "comparisons, and the options-implied move or explicit unavailable fields."))
        if earnings is None and digest.earnings_preview is not None:
            issues.append(Issue("earnings_heading", "No eligible upcoming earnings event was established; omit earnings_preview."))
    if not digest.topics and digest.earnings_preview is None:
        issues.append(Issue("overall", "The digest has no topics."))
    for location, claim in digest.located_claims().items():
        in_preview = location.startswith("earnings_")
        if any(ref not in allowed for ref in claim.sources):
            issues.append(Issue(location, "References an unknown or temporally ineligible source."))
        elif not in_preview and earnings_ids.intersection(claim.sources):
            issues.append(Issue(location, "Cites an earnings-history source older than the news window outside the earnings preview."))
        if editorial_checks and unconfirmed.intersection(claim.sources) and not RUMOR_LABEL.search(claim.text):
            issues.append(Issue(location, "Cites an unconfirmed report: label it 'unconfirmed', 'reportedly', or "
                                          "'reported talks', and attribute the outlet."))
        if location != "headline" and "{move}" in claim.text:
            issues.append(Issue(location, "Reserve the exact price token for the headline."))
        if re.search(r"\[\d+\]|https?://", claim.text):
            issues.append(Issue(location, "Citations must use the structured sources field."))
    return issues


def prune_digest(digest: Digest, locations: set[str], fallback: Claim | None = None) -> tuple[Digest | None, int]:
    """Drop flagged claims instead of the whole narrative. Returns (digest or None, claims removed)."""
    removed = 0
    headline = digest.headline
    if "headline" in locations:
        headline, removed = fallback or Claim(text="{move}", sources=[1]), removed + 1

    def prune(topic, prefix):
        nonlocal removed
        if topic is None:
            return None
        if f"{prefix}_heading" in locations:
            removed += 1 + len(topic.sentences)
            return None
        kept = [s for i, s in enumerate(topic.sentences, 1) if f"{prefix}_sentence_{i}" not in locations]
        removed += len(topic.sentences) - len(kept)
        return topic.model_copy(update={"sentences": kept}) if kept else None

    preview = prune(digest.earnings_preview, "earnings")
    topics = [t for i, topic in enumerate(digest.topics, 1) if (t := prune(topic, f"topic_{i}")) is not None]
    notes = [n for i, n in enumerate(digest.coverage_notes, 1) if f"coverage_note_{i}" not in locations]
    if not topics and preview is None:
        return None, removed
    return Digest(headline=headline, earnings_preview=preview, topics=topics, coverage_notes=notes), removed


def label_unsupported_summaries(digest: Digest) -> None:
    """Conservatively qualify summaries sharing context with unsupported details."""
    unsupported_sources = set()
    for topic in ([digest.earnings_preview] if digest.earnings_preview else []) + digest.topics:
        sentence_sources = {
            ref for sentence in topic.sentences if sentence.support_status == "unsupported"
            for ref in sentence.sources if ref != 1
        }
        if sentence_sources.intersection(topic.heading.sources):
            topic.heading.support_status = "unsupported"
        unsupported_sources.update(sentence_sources)
        if topic.heading.support_status == "unsupported":
            unsupported_sources.update(ref for ref in topic.heading.sources if ref != 1)
    if unsupported_sources.intersection(digest.headline.sources):
        digest.headline.support_status = "unsupported"


def label_support_gaps(digest: Digest, verdict: VerificationResult) -> VerificationResult:
    """Resolve only located support gaps by adding a mandatory visible label."""
    claims = digest.located_claims()
    remaining = []
    for issue in verdict.issues:
        claim = claims.get(issue.location)
        if issue.category == "support" and claim is not None:
            claim.support_status = "unsupported"
        else:
            remaining.append(issue)
    label_unsupported_summaries(digest)
    # A failed verdict with no actionable issues is never an approval.
    approved = not remaining and (verdict.passed or bool(verdict.issues))
    return verdict.model_copy(update={"passed": approved, "issues": remaining})


def review_summary(python_issues: list[Issue], verdict: VerificationResult | None) -> str:
    """Explain rejection using safe locations/categories, never unpublished prose."""
    messages = [f"{issue.location.replace('_', ' ')}: {issue.message}" for issue in python_issues]
    labels = {
        "support": "insufficient factual support", "contradiction": "contradicts supplied evidence",
        "citation": "citation mismatch",
        "timing": "timing/cutoff problem", "attribution": "missing attribution",
        "uncertainty": "uncertainty not preserved", "numbers": "numeric inconsistency",
        "recommendation": "unsupported recommendation/forecast", "style": "editorial issue",
    }
    for issue in (verdict.issues if verdict else []):
        messages.append(f"{issue.location.replace('_', ' ')}: {labels[issue.category]}")
    messages = list(dict.fromkeys(messages))
    if not messages:
        return "verifier did not approve the draft or provide actionable issue details"
    return "; ".join(messages[:4]) + (f"; {len(messages) - 4} more issue(s)" if len(messages) > 4 else "")


def renumber(digest: Digest | None, sources: list[Source], headline_ids: list[int]):
    """Compact citation numbers to 2..N in reading order (headlines, then claims); [1] stays market data."""
    order = list(headline_ids)
    if digest is not None:
        digest = digest.model_copy(deep=True)
        order += [ref for claim in digest.located_claims().values() for ref in claim.sources]
    order += [source.id for source in sources]
    mapping = {1: 1}
    for ref in dict.fromkeys(order):
        if ref != 1:
            mapping[ref] = len(mapping) + 1
    if digest is not None:
        for claim in digest.located_claims().values():
            claim.sources = [mapping[ref] for ref in claim.sources]
    renamed = sorted((source.model_copy(update={"id": mapping[source.id]}) for source in sources), key=lambda s: s.id)
    return digest, renamed, [mapping[ref] for ref in headline_ids]


async def gather_or_cancel(*coroutines):
    """Like gather, but a raised error cancels the sibling tasks instead of leaving them running."""
    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def company_domain(website: str | None) -> str | None:
    host = urlsplit(website or "").hostname
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- orchestration

async def run_digest(ticker: str, openai_key: str, models: dict | str, stage, *, verify: bool = True,
                     deadline: float | None = None, material=None) -> Publication:
    """One digest run.

    ``material``, when given, is an awaitable material-news run (``MaterialResult``) for the same
    company. The digest then skips its own company-development searches, waits for that run, and
    feeds its developments from today or the previous session to the writer (``material_evidence``).
    A failed material run fails the digest.
    """
    if isinstance(models, str):
        models = {"research": models, "writer": models, "verifier": models}
    loop = asyncio.get_running_loop()

    def remaining() -> float:
        return float("inf") if deadline is None else deadline - loop.time()

    stage("Fetching company and regular-session prices…")
    market, profile = await fetch_market(ticker)
    coverage: list[str] = []          # reader-facing
    diagnostics: list[str] = list(market.limitations)
    stage("Fetching structured catalysts (earnings, estimates, rating changes, filings, peers) in the background…")
    catalysts_task = asyncio.ensure_future(asyncio.to_thread(fetch_catalysts, market, profile))
    sources: list[Source] = []
    packets: list[dict] = []
    stage("Fetching recent Yahoo Finance headlines…")
    try:
        rows, retrieved_at = await fetch_news(ticker)
        sources, packets, feed_omissions = yahoo_news_packets(rows, retrieved_at, market)
        stage(f"Yahoo news: {len(packets)} dated, company-related headline(s) retained.")
        diagnostics.append("Yahoo news titles, summaries and publication timestamps are provider-supplied; full articles were not independently fetched.")
        if not packets and feed_omissions:
            stage("Yahoo news filtering: " + "; ".join(f"{count} {reason}" for reason, count in feed_omissions.most_common(2)) + ".")
    except DigestError as exc:
        stage(str(exc) + " Continuing with web research.")
        diagnostics.append(str(exc))
    feed_titles = {normalized_title(source.title): source for source in sources}
    successes = 0
    no_results = 0
    extracted_count = 0
    omissions = Counter()
    semaphore = asyncio.Semaphore(3)
    async with usage_client(openai_key, "digest") as client:
        research, writer, verifier = build_agents(models, client, verify=verify)
        domain = company_domain(market.website)
        press_agent = domain_research(research, ([domain] if domain else []) + NEWSWIRES)
        run_config = RunConfig(tracing_disabled=False, trace_include_sensitive_data=True)

        async def search(query: str, days: int, purpose="news", event=None, agent=None):
            async with semaphore:
                try:
                    search_as_of = datetime.now(UTC)
                    result = await Runner.run(agent or research, json.dumps({
                        "query": query, "as_of": search_as_of.isoformat(),
                        "price_observed_at": market.observed_at.isoformat(),
                        "news_start": (search_as_of - timedelta(days=days)).isoformat(),
                        "events_through": (market.as_of + timedelta(days=30)).isoformat(),
                        "research_purpose": purpose,
                        "upcoming_earnings": event,
                    }), max_turns=1, run_config=run_config)
                    evidence, parsing_notes = parse_research(result.final_output)
                    evidence.query = query  # The manager owns the executed query, not the model.
                    found, _ = extract_sources(result, datetime.now(UTC))
                    return evidence, found, None, parsing_notes
                except (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                    raise InputError(api_error_message(exc)) from None
                except DigestError as exc:
                    # App-authored messages are safe to print and explain the actual problem.
                    failure = f"Research query failed ({purpose}): {exc}"
                    stage(failure)
                    return None, [], failure, []
                except Exception as exc:
                    # No exception bodies: they can contain request data, source text, or credentials.
                    failure = f"Research query failed ({purpose}): {api_error_message(exc)}"
                    stage(failure)
                    return None, [], failure, []

        def merge(result, days, purpose="news"):
            nonlocal successes, no_results, extracted_count
            evidence, found, failure, parsing_notes = result
            diagnostics.extend(parsing_notes)
            if failure:
                diagnostics.append(failure)
                return
            successes += 1
            extracted_count += len(evidence.findings)
            if evidence.status == "no_relevant_results":
                no_results += 1
            existing = {s.url: s for s in sources}
            for source in found:
                if source.url in existing:
                    prior = existing[source.url]
                    prior.retrieved_at = max(prior.retrieved_at, source.retrieved_at)
                    prior.raw_material = list(dict.fromkeys(prior.raw_material + source.raw_material))
                    if prior.title == prior.url and source.title != source.url:
                        prior.title = source.title
                    # Provider-dated sources keep their provider timestamps.
                    if prior.timestamp_provenance not in {"provider_feed", "provider_structured"}:
                        for field in ("published", "updated"):
                            value = getattr(source, field)
                            old = getattr(prior, field)
                            if value and (not old or timestamp_bound(value) > timestamp_bound(old)):
                                setattr(prior, field, value)
                else:
                    source.id = len(sources) + 2
                    sources.append(source)
                    existing[source.url] = source
            accepted, omitted = eligible_packets(evidence, sources, market, days, feed_titles)
            for packet in accepted:
                packet["query"] = evidence.query
                packet["research_purpose"] = purpose
            packets.extend(accepted)
            omissions.update(omitted)

        name = core_name(market.company)
        topic = market.industry or market.sector or "stock market"
        rumor_query = f"{name} reportedly in talks people familiar with the matter"
        # (query, search days, news window, purpose, agent, covered by the material-news agent)
        plan = [item[:5] for item in (
            (f"{name} ({ticker}) stock news today", 3, 3, "news", None, False),
            (f"{name} press release announcement", 3, 3, "news", press_agent, True),
            (f"{ticker} {name} analyst upgrade downgrade price target", 3, 3, "news", None, False),
            (f"{name} earnings date guidance buyback dividend announcement", 45, 3, "earnings_event", None, False),
            (f"{name} acquisition merger deal talks", 3, 3, "news", None, True),
            (rumor_query, 3, 3, "news", None, True),
            (f"{topic} stocks today stock market news", 3, 3, "news", None, False),
        ) if material is None or not item[5]]
        stage(f"Researching {len(plan)} focused queries (up to three at once)…")
        results = await gather_or_cancel(*(search(query, search_days, purpose, agent=agent)
                                           for query, search_days, _, purpose, agent in plan))
        for result, (_, _, window, purpose, _) in zip(results, plan):
            # Only dated future announcements get the older-publication exception.
            merge(result, window, purpose)
        query_count = len(plan)
        days = 3
        news_count = sum(packet.get("research_purpose") in NEWS_PURPOSES for packet in packets)
        # The material-news agent already covers the week, so the expansion query runs only without it.
        if material is None and successes and news_count < 3 and remaining() > FOLLOWUP_BUDGET:
            days = 7
            reasons = "; ".join(f"{count} {reason}" for reason, count in omissions.most_common(2))
            detail = f" ({reasons})" if reasons else ""
            stage(f"Only {news_count} usable news findings across Yahoo news and {extracted_count} extracted search findings{detail}; expanding one query to seven days…")
            coverage.append("News window expanded from 72 hours to 7 days because eligible evidence was sparse.")
            merge(await search(f"{name} ({ticker}) stock news this week", 7), 7)
            query_count += 1

        catalysts = None
        try:
            catalysts = await asyncio.wait_for(asyncio.shield(catalysts_task), timeout=max(1, min(CATALYST_WAIT, remaining() - VERIFY_BUDGET)))
        except Exception as exc:
            diagnostics.append(f"Structured catalyst data unavailable ({type(exc).__name__}).")
        structured_ids: dict = {}
        if catalysts:
            diagnostics.extend(catalysts["diagnostics"])
            market.benchmarks = catalysts.get("benchmarks") or []
            new_sources, new_packets, structured_ids = structured_evidence(catalysts, market, days, len(sources) + 2)
            sources.extend(new_sources)
            packets.extend(new_packets)
            stage(f"Structured data: {len(new_packets)} record(s), {len(market.benchmarks)} benchmark/peer move(s).")

        event = upcoming_earnings(packets, market, [structured_ids["event"]] if "event" in structured_ids else [])
        structured_earnings = None
        if event:
            stage(f"Upcoming earnings {event['event_date']} ({event['event_status']}); gathering estimates and the options-implied move…")
            if "event" in structured_ids and structured_ids["event"]["event_date"] != event["event_date"]:
                diagnostics.append(f"Yahoo's calendar date ({structured_ids['event']['event_date']}) differs from the "
                                   f"{event['event_status']} date used ({event['event_date']}).")
            structured_earnings = {"analysis_source_id": structured_ids.get("analysis"),
                                   "financials_source_id": structured_ids.get("financials"),
                                   "estimates": (catalysts or {}).get("estimates") if structured_ids.get("analysis") else None,
                                   "revenue_history": (catalysts or {}).get("revenue_history") or [], "options": None}
            try:
                options = await asyncio.wait_for(asyncio.to_thread(
                    fetch_implied_move, market, datetime.fromisoformat(event["event_date"]).date(),
                    event.get("event_timing", "unknown")), timeout=20)
            except Exception as exc:
                options = None
                diagnostics.append(f"Yahoo option chain unavailable ({type(exc).__name__}).")
            if options:
                source = Source(id=len(sources) + 2, url=options.pop("url"), title=f"Option chain, {options['expiry']} expiry",
                                publisher="Yahoo Finance", retrieved_at=datetime.now(UTC),
                                raw_material=[], published=datetime.now(UTC).isoformat(),
                                timestamp_provenance="provider_structured")
                set_source_timing(source, market.observed_at)
                sources.append(source)
                structured_earnings["options"] = {**options, "source_id": source.id,
                                                  "supporting_quote": options["methodology"]}
            event_context = {"date": event["event_date"], "announcement": event["supporting_material"]}
            event_prefix = f"{name} ({ticker}) earnings release {event['event_date']} "
            followups = []
            if not structured_earnings["estimates"]:
                followups.append((event_prefix + "consensus revenue EPS estimates fiscal quarter analyst expectations", 14, "earnings_estimates"))
                followups.append((event_prefix + "previous quarter and year-ago actual revenue diluted EPS results", 550, "earnings_history"))
            if not structured_earnings["options"]:
                followups.append((event_prefix + "options implied expected earnings move percentage straddle", 7, "earnings_options"))
            if followups and remaining() < FOLLOWUP_BUDGET:
                diagnostics.append("Earnings follow-up searches skipped: the run deadline was near.")
                followups = []
            if followups:
                extra = await gather_or_cancel(*(search(query, window_days, purpose, event_context)
                                                 for query, window_days, purpose in followups))
                for result, (_, window_days, purpose) in zip(extra, followups):
                    merge(result, window_days, purpose)
                query_count += len(followups)
        if material is not None:
            stage("Waiting for the material-news developments…")
            try:
                material_result = await material
            except Exception as exc:
                raise DigestError("The digest was not written because the material-news research failed; "
                                  "see the Material news result for the reason.") from exc
            if material_result.issuer.symbol.upper() != market.ticker.replace(".", "-").upper():
                raise DigestError(f"The material-news run resolved {material_result.issuer.symbol}, not {market.ticker}.")
            new_sources, new_packets, note = material_evidence(material_result, market, sources)
            sources.extend(new_sources)
            packets = new_packets + packets      # first, so feed/search copies of the same URL merge into them
            diagnostics.append(note)
            stage(note)
            if not new_packets:
                coverage.append("The material-news check found no company development dated today or the previous session.")
        packets = dedupe_packets(packets, sources)
        # A later response can expose a conflicting/newer update on a previously
        # accepted URL. Recheck the merged registry before publication eligibility.
        news_as_of = datetime.now(UTC)
        for source in sources:
            if source.timestamp_provenance != "provider_structured":
                set_source_timing(source, market.observed_at)
        eligible_ids = {s.id for s in sources if s.eligible_at and s.eligible_at <= news_as_of}
        packets = [packet for packet in packets if packet["source_id"] in eligible_ids]
        by_id = {s.id: s for s in sources}
        for packet in packets:
            source = by_id[packet["source_id"]]
            if not packet["structured"]:
                packet["price_timing_unknown"] = source.price_timing_unknown
                packet["later_than_price"] = later_than_price(source, market.observed_at)
            # Earnings-history research older than the news window supports only the preview.
            packet["earnings_only"] = bool(packet.get("research_purpose", "").startswith("earnings_")
                                           and packet.get("research_purpose") != "earnings_event"
                                           and source.eligible_at < news_as_of - timedelta(days=days))
        for reason, count in omissions.items():
            diagnostics.append(f"{count} finding(s) omitted: {reason}.")
        feed_count = sum(packet.get("research_purpose") == "news_feed" for packet in packets)
        structured_count = sum(packet["structured"] for packet in packets)
        material_count = sum(packet["material"] for packet in packets)
        stage(f"Research complete: {successes}/{query_count} queries succeeded; {len(packets)} usable finding(s), "
              f"including {feed_count} Yahoo headline(s), {structured_count} structured record(s)"
              + (f" and {material_count} material-news development(s)." if material is not None else "."))

        preview = None

        def publication(digest=None, used=(), headline_ids=()):
            digest, shown, headline_ids = renumber(digest, [s for s in sources if s.id in set(used)], list(headline_ids))
            return Publication(market=market, generated_at=datetime.now(UTC), news_as_of=news_as_of, digest=digest,
                               sources=shown, news_source_ids=headline_ids, coverage=list(dict.fromkeys(coverage)),
                               diagnostics=list(dict.fromkeys(diagnostics)), earnings=preview)

        if not packets:
            if successes == 0:
                coverage.append("News/context unavailable. All research queries failed or were unavailable.")
            elif no_results == successes:
                coverage.append("Searches completed with no relevant findings. No clear company-specific catalyst was identified in the sources checked.")
            else:
                coverage.append("News/context unavailable: searches ran, but no findings met source-content and cutoff checks. This does not establish an absence of news or a catalyst.")
            reasons = "; ".join(f"{count} {reason}" for reason, count in omissions.items())
            if not reasons:
                reasons = ("all research queries failed" if not successes else
                           "searches returned no eligible findings; some responses may have been unavailable or malformed")
            stage(f"Price-only result: {reasons}.")
            return publication()

        used = {packet["source_id"] for packet in packets}
        if structured_earnings:
            used.update(filter(None, (structured_earnings.get("analysis_source_id"), structured_earnings.get("financials_source_id"),
                                      (structured_earnings.get("options") or {}).get("source_id"))))
        if event:
            used.add(event["source_id"])
        supplied_sources = [source for source in sources if source.id in used]
        news_source_ids = recent_headline_ids(packets, supplied_sources)
        unconfirmed_ids = {packet["source_id"] for packet in packets if packet["confirmation_status"] == "unconfirmed"}
        for source in supplied_sources:
            source.unconfirmed_report = source.id in unconfirmed_ids
        uncertain_ids = {source.id for source in supplied_sources if source.price_timing_unknown}
        if uncertain_ids:
            diagnostics.append("Date-only same-day articles are labeled; they may coincide with the move but cannot be ordered before it.")
        preview = earnings_context(event, packets, supplied_sources, market, structured_earnings)
        explanations = move_explanations(packets)
        catalyst_status = ("research_unavailable" if not successes and not structured_count
                           else "candidates" if explanations else "none_identified")
        if any(p["evidence_provenance"] == "research_extraction" or p["timestamp_provenance"] == "research_extraction" for p in packets):
            diagnostics.append("Some passages/dates were extracted by the research model from cited search results; original article text was not independently retrieved.")
        payload = {
            "market_source_id": 1, "market": market.model_dump(mode="json"),
            "price_phrase": price_phrase(market), "move_phrase": move_phrase(market),
            "extended_hours_phrase": extended_phrase(market),
            "news_window_days": days, "news_as_of": news_as_of.isoformat(),
            "material_window": ({"from": market.comparison_date.isoformat(), "to": market.session_date.isoformat(),
                                 "developments": material_count} if material is not None else None),
            "today_local": news_as_of.astimezone(NY).strftime("%A, %B %-d, %Y (America/New_York)"),
            "catalyst_status": catalyst_status, "possible_move_explanations": explanations,
            "source_registry": [s.model_dump(mode="json", exclude={"raw_material"}) for s in supplied_sources],
            "evidence": packets, "coverage": coverage, "earnings_context": preview,
        }

        def fallback(reason: str):
            coverage.append(reason + (" Only retrieved headlines are shown." if news_source_ids else " Showing prices only."))
            return publication(used=news_source_ids, headline_ids=news_source_ids)

        def publish(digest: Digest, verification_note: str):
            unsupported_count = sum(c.support_status == "unsupported" for c in digest.located_claims().values())
            if unsupported_count:
                coverage.append(f"{unsupported_count} claim(s) are labeled unsupported; their citations provide context, not substantiation.")
            if verification_note:
                coverage.append(verification_note)
            # Writer notes are lowest priority; the renderer shows only the first few notes.
            coverage.extend(digest.coverage_notes[:2])
            cited = {ref for claim in digest.located_claims().values() for ref in claim.sources}
            return publication(digest, used=cited | set(news_source_ids), headline_ids=news_source_ids)

        stage("Writing the digest…")
        try:
            result = await Runner.run(writer, json.dumps(payload), max_turns=1, run_config=run_config)
            digest = result.final_output_as(Digest)
        except (APIError, ModelBehaviorError, ValidationError) as exc:
            stage(f"Writer unavailable: {api_error_message(exc)}")
            return fallback("The narrative could not be generated.")

        verify_now = verify and remaining() >= VERIFY_BUDGET
        if verify and not verify_now:
            coverage.append("Model verification was skipped because the run deadline was near; only basic code checks were applied.")
        attempts = 2 if verify_now else 1
        for attempt in range(attempts):
            adjustment = repair_headline(digest, market)
            if adjustment:
                stage(adjustment)
            label_unsupported_summaries(digest)
            issues = check_digest(digest, market, supplied_sources, packets, preview, news_as_of, editorial_checks=verify_now)
            verdict = None
            if verify_now:
                stage("Checking citations, timing, and factual support…")
                try:
                    review = await Runner.run(verifier, json.dumps({
                        **payload, "draft": digest.model_dump(mode="json"),
                        "rendered_claims": {
                            location: claim.display_text(move_phrase(market) if location == "headline" else None,
                                                         timing_uncertain=bool(uncertain_ids.intersection(claim.sources)))
                            for location, claim in digest.located_claims().items()
                        },
                        "python_issues": [issue.__dict__ for issue in issues],
                    }), max_turns=1, run_config=run_config)
                    verdict = label_support_gaps(digest, review.final_output_as(VerificationResult))
                except (APIError, ModelBehaviorError, ValidationError) as exc:
                    stage(f"Verifier unavailable: {api_error_message(exc)} Applying basic code checks only.")
                    coverage.append("Model verification failed to run; only basic code checks were applied.")
                    verify_now, verdict = False, None
                    issues = check_digest(digest, market, supplied_sources, packets, preview, news_as_of, editorial_checks=False)
            note = ("Narrative checked by a model reviewer; review is not a guarantee of accuracy." if verify_now
                    else "Model verification was disabled; only basic code checks were applied." if not verify else "")
            if not issues and (verdict is None or (verdict.passed and not verdict.issues)):
                return publish(digest, note)
            final = not verify_now or attempt == attempts - 1 or remaining() < REVISION_BUDGET
            if final:
                blocking_overall = any(issue.location == "overall" and issue.message == "The digest has no topics." for issue in issues)
                verifier_issues = verdict.issues if verdict else []
                blocking_overall = blocking_overall or any(
                    issue.location == "overall" and issue.category not in {"style", "support"} for issue in verifier_issues)
                locations = {issue.location for issue in issues} | {issue.location for issue in verifier_issues}
                pruned, removed = prune_digest(digest, locations - {"overall"}, fallback_headline(market))
                what = "review" if verify_now else "basic checks"
                if blocking_overall or pruned is None:
                    stage(f"Narrative blocked by {what}: {review_summary(issues, verdict)}.")
                    return fallback(f"The narrative did not pass {what}.")
                remaining_issues = [issue for issue in check_digest(pruned, market, supplied_sources, packets, preview,
                                                                    news_as_of, editorial_checks=False)]
                if remaining_issues:
                    stage(f"Narrative blocked by {what}: {review_summary(remaining_issues, None)}.")
                    return fallback(f"The narrative did not pass {what}.")
                if removed:
                    stage(f"Removed {removed} flagged claim(s): {review_summary(issues, verdict)}.")
                    coverage.append(f"{removed} claim(s) were removed after failing {what}.")
                return publish(pruned, note)
            stage(f"Review requires changes: {review_summary(issues, verdict)}.")
            stage("Revising once using the review findings…")
            try:
                revision = await Runner.run(writer.clone(instructions=REVISION), json.dumps({
                    **payload, "draft": digest.model_dump(mode="json"),
                    "python_issues": [issue.__dict__ for issue in issues],
                    "revision_issues": verdict.model_dump(mode="json") if verdict else None,
                }), max_turns=1, run_config=run_config)
                digest = revision.final_output_as(Digest)
            except (APIError, ModelBehaviorError, ValidationError) as exc:
                stage(f"Revision unavailable: {api_error_message(exc)} Reviewing the original draft once more.")
    raise DigestError("Digest could not be completed.")
