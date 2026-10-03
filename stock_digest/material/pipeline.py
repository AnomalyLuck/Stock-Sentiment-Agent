"""Orchestration for the material company news agent.

resolve issuer → search profile → broad, official and targeted discovery → Python gates →
event clustering → per-event verification (opening sources) → gates again → rank → digest.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from agents import RunConfig, Runner
from openai import AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError
from pydantic import ValidationError

from ..agents import NEWSWIRES, api_error_message
from ..manager import core_name, gather_or_cancel
from ..costs import usage_client
from ..market import DigestError, InputError
from .agents import PROFILE, VERIFY, build_agents, official_discovery
from .cache import CACHE_VERSION, EVENT_TTL, PROFILE_TTL, MaterialCache, fingerprint
from ..dates import normalize_source_timestamp
from .gates import (Cluster, Decision, Window, apply_merges, candidate_gate, canonical_url, checked_date,
                    display_date, event_gate, group_candidates, lineage, make_window, placement, publisher_of,
                    rank_key, sort_moment, status_language)
from .identity import resolve_issuer
from .models import (EVIDENCE_RANK, REPORTED_STATUSES, STATUS_LABELS, CachedVerification, Candidate, Consolidation, Entry, Issuer, MaterialResult, SearchProfile,
                     SourceRef, VerifiedEvent)

SEARCH_CONCURRENCY = 8
DAILY_SEARCHES = 7          # one dated search per window day, newest first
FEED_COUNT = 40
MAX_FEED_CANDIDATES = 80
VERIFY_CONCURRENCY = 8
MAX_VERIFY = 24
FOLLOWUP_BUDGET = 240       # seconds of deadline needed before a follow-up discovery round
VERIFY_BUDGET = 130         # seconds of deadline needed before verification starts
CONSOLIDATE_BUDGET = 160
FALLBACK_SOURCE_TYPES = {"official_release", "filing", "regulator", "original_reporting", "syndicated"}
SOCIAL = re.compile(r"(?:^|\.)(?:reddit\.com|x\.com|twitter\.com|facebook\.com|stocktwits\.com|threads\.net|tiktok\.com)$")
REGULATORS = ["sec.gov", "ftc.gov", "justice.gov", "commerce.gov", "bis.doc.gov", "fda.gov", "europa.eu"]


class ResearchUnavailable(DigestError):
    """Search could not be performed; never reported as 'no news'."""


class FeedUnavailable(RuntimeError):
    """Yahoo returned no headlines at all, which in practice means it is refusing requests."""


@dataclass(frozen=True)
class MaterialRequest:
    ticker: str
    hours: float = 168
    timezone: str = "UTC"
    max_results: int | None = None
    language: str = "English"
    exchange: str | None = None


# ---------------------------------------------------------------- parsing

def parse_block(text: str, key: str) -> dict:
    """The first complete JSON object containing ``key``; never repairs truncated output."""
    decoder = json.JSONDecoder()
    cursor = 0
    while (start := (text or "").find("{", cursor)) >= 0:
        try:
            obj, cursor = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
    raise DigestError(f"The model response contained no complete JSON object with '{key}'.")


def parse_candidates(text: str) -> tuple[list[Candidate], int, str]:
    report = parse_block(text, "candidates")
    rows = report.get("candidates")
    if not isinstance(rows, list):
        raise DigestError("The discovery response contained no candidate list.")
    valid, invalid = [], 0
    for row in rows[:12]:
        try:
            valid.append(Candidate.model_validate(row))
        except ValidationError:
            invalid += 1
    return valid, invalid, str(report.get("status") or "")


def parse_feed_candidates(text: str, leads: list[dict]) -> tuple[list[Candidate], int, str]:
    """Expand compact classifications with provider-owned URLs, dates and content."""
    report = parse_block(text, "candidates")
    rows = report.get("candidates")
    if not isinstance(rows, list):
        raise DigestError("The feed response contained no candidate list.")
    valid, invalid, covered = [], 0, set()
    for row in rows[:MAX_FEED_CANDIDATES]:
        index = row.get("lead_index") if isinstance(row, dict) else None
        if type(index) is not int or not 0 <= index < len(leads):
            invalid += 1
            continue
        lead = leads[index]
        try:
            valid.append(Candidate.model_validate({
                **row, "url": lead["url"], "publisher": lead["publisher"], "published": lead["published"],
                "updated": None, "timestamp_basis": lead["published"],
                "headline": lead["title"][:200], "summary": lead["summary"][:600],
            }))
            covered.add(index)
        except ValidationError:
            invalid += 1
    invalid += len(rows[MAX_FEED_CANDIDATES:]) + len(set(range(len(leads))) - covered)
    return valid, invalid, "partial" if invalid else str(report.get("status") or "")


def collect_sources(result, retrieved_at: datetime) -> tuple[dict[str, SourceRef], int]:
    """URLs from search-tool metadata, citations and opened pages; never URLs written only in prose."""
    found: dict[str, SourceRef] = {}
    completed = 0

    def add(url: str | None, title: str | None = None, opened: bool = False, snippet: str | None = None):
        key = canonical_url(url)
        if not key:
            return
        ref = found.get(key) or SourceRef(url=url, title=title or publisher_of(key), publisher=publisher_of(key),
                                          retrieved_at=retrieved_at)
        if title and ref.title == ref.publisher:
            ref.title = title
        ref.opened = ref.opened or opened
        if snippet and snippet not in ref.snippets:
            ref.snippets.append(snippet[:1500])
        found[key] = ref

    for response in getattr(result, "raw_responses", []):
        for output in response.output:
            item = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                if item.get("status") == "completed":
                    completed += 1
                for source in action.get("sources") or []:
                    add(source.get("url"), source.get("title"), snippet=source.get("snippet") or source.get("text"))
                if action.get("type") in {"open_page", "find_in_page"} and item.get("status") == "completed":
                    add(action.get("url"), opened=True)
            elif item.get("type") == "message":
                for content in item.get("content") or []:
                    for note in content.get("annotations") or []:
                        if note.get("type") == "url_citation":
                            add(note.get("url"), note.get("title"))
    return found, completed


def fetch_feed_leads(symbol: str, window: Window) -> list[dict]:
    """Yahoo Finance's company news feed: provider-dated headlines inside the window (synchronous)."""
    import yfinance as yf
    from ..market import _configure_yahoo

    _configure_yahoo()
    rows = yf.Ticker(symbol).get_news(count=FEED_COUNT, tab="news")
    if not isinstance(rows, list) or not rows:
        # yfinance returns an empty list rather than raising when Yahoo answers HTTP 429.
        raise FeedUnavailable("Yahoo Finance returned no headlines")
    leads, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        content = row.get("content") if isinstance(row, dict) else None
        if not isinstance(content, dict) or not isinstance(content.get("title"), str):
            continue
        link = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        url = link.get("url") if isinstance(link, dict) else None
        published = normalize_source_timestamp(content.get("pubDate")) if isinstance(content.get("pubDate"), str) else None
        key = canonical_url(url)
        if not key or key in seen or placement(published, window) != "inside":
            continue
        seen.add(key)
        provider = content.get("provider") if isinstance(content.get("provider"), dict) else {}
        leads.append({"title": content["title"].strip(), "summary": str(content.get("summary") or "")[:500],
                      "url": url, "publisher": provider.get("displayName") or publisher_of(key),
                      "published": published})
    return leads


def feed_sources(leads: list[dict], retrieved_at: datetime) -> dict[str, SourceRef]:
    """Feed items are grounded sources with provider publication times."""
    return {canonical_url(lead["url"]): SourceRef(url=lead["url"], title=lead["title"], publisher=lead["publisher"],
                                                  published=lead["published"], retrieved_at=retrieved_at,
                                                  snippets=[lead["summary"]] if lead["summary"] else [])
            for lead in leads}


# ---------------------------------------------------------------- planning

def official_domains(issuer: Issuer, profile: SearchProfile) -> list[str]:
    hosts = []
    for url in [issuer.website, *profile.ir_urls, *profile.newsroom_urls]:
        host = urlsplit(url or "").hostname
        if host:
            hosts.append(host[4:] if host.startswith("www.") else host)
    return list(dict.fromkeys(hosts + NEWSWIRES + REGULATORS))[:20]


def query_plan(issuer: Issuer, profile: SearchProfile, window: Window) -> list[tuple[str, str]]:
    """Eight topic searches plus one dated search per window day; follow-ups fill gaps.

    Topic searches over a heavily covered company return its biggest or evergreen stories.
    Dated searches ("Tesla news September 28, 2026") surface each day's coverage, such as a
    product event being rescheduled, and the product search uses the verbs such news carries.
    """
    name = core_name(issuer.company)
    ticker = issuer.ticker
    plan = [
        ("broad", f"{name} ({ticker}) news"),
        ("official", f"{name} announces"),
        ("outlets", f"{name} Reuters OR Bloomberg news this week"),
        ("outlets", f"{name} CNBC OR Wall Street Journal OR Financial Times latest news"),
        ("capital", f"{name} earnings guidance buyback dividend financing"),
        ("deals", f"{name} acquisition merger investment contract partnership reportedly in talks"),
        ("products", f"{name} unveils OR launches OR delays OR reschedules product news this week"),
        ("risks", f"{name} regulatory legal incident leadership restructuring news"),
    ]
    first = window.start.astimezone(window.zone).date()
    day = window.end.astimezone(window.zone).date()
    for _ in range(DAILY_SEARCHES):
        if day < first:
            break
        plan.append(("daily", f"{name} news {day.strftime('%B')} {day.day}, {day.year}"))
        day -= timedelta(days=1)
    return plan


def followup_plan(issuer: Issuer, profile: SearchProfile, queries: list[dict],
                  candidates: list[Candidate], decisions: list[Decision]) -> list[tuple[str, str]]:
    """At most two searches: recover failures, resolve undated leads, then sparse coverage."""
    name = core_name(issuer.company)
    plan = [(q["purpose"] if q["purpose"] != "feed" else "feed_recovery", q["query"])
            for q in queries if q["failure"] or q["invalid"]]
    pending = group_candidates([c for c, d in zip(candidates, decisions) if d.pending])
    undated = [c for c, d in zip(candidates, decisions) if d.pending]
    plan.extend(("undated", f"{name} {undated[group[0]].headline} publication date") for group in pending)
    qualifying = [c for c, d in zip(candidates, decisions) if d.passed or d.pending]
    if len(group_candidates(qualifying)) < 3:
        names = [n for n in profile.brands + profile.subsidiaries if n and n.lower() not in name.lower()][:4]
        if names:
            plan.append(("brands", f"{name} {' OR '.join(names)} news this week"))
        alias = next((a for a in profile.aliases + profile.former_names if a and a.lower() != name.lower()), issuer.ticker)
        plan.append(("sparse", f"{alias} latest company developments this week"))
    return list(dict.fromkeys(plan))[:2]


def event_cache_key(issuer: Issuer, model: str, request: MaterialRequest, cluster: Cluster,
                    registry: dict[str, SourceRef]) -> str:
    """Conservative exact-evidence match; ordering and tracking parameters do not matter.

    Include *all* cluster evidence, not just the eight articles supplied to verification.
    Generated summaries may cause extra misses; prefer that to hiding a changed fact.
    """
    articles = []
    for candidate in cluster.candidates:
        row = candidate.model_dump(mode="json", exclude={"development_key", "subject_rationale"})
        row["url"] = canonical_url(candidate.url)
        source = registry.get(row["url"])
        row["evidence"] = ({"published": source.published, "snippets": sorted(source.snippets)}
                           if source else None)
        articles.append(json.dumps(row, sort_keys=True))
    return fingerprint({"version": CACHE_VERSION, "kind": "event", "model": model, "prompt": VERIFY,
                        "issuer": [issuer.symbol, issuer.company, issuer.exchange, issuer.currency],
                        "language": request.language, "timezone": request.timezone, "hours": request.hours,
                        "articles": sorted(set(articles))})


# ---------------------------------------------------------------- run

async def run_material(request: MaterialRequest, api_key: str, models: dict, stage,
                       *, deadline: float | None = None, run_config: RunConfig | None = None) -> MaterialResult:
    loop = asyncio.get_running_loop()

    def remaining() -> float:
        return float("inf") if deadline is None else deadline - loop.time()

    run_at = datetime.now(UTC)
    window = make_window(run_at, request.hours, request.timezone)
    stage(f"Resolving {request.ticker}…")
    issuer = await asyncio.to_thread(resolve_issuer, request.ticker, request.exchange)
    stage(f"Resolved {issuer.ticker} to {issuer.company} ({issuer.exchange}).")
    run_config = run_config or RunConfig(tracing_disabled=False, trace_include_sensitive_data=True)
    diagnostics: list[str] = []
    cache = MaterialCache()
    cache_hits = {"profile": 0, "verification": 0}
    agent_runs = {"profile": 0, "discovery": 0, "feed": 0, "consolidation": 0, "verification": 0}
    record: dict = {
        "ticker": issuer.ticker, "exchange": issuer.exchange, "company_name": issuer.company,
        "window_start": window.start.isoformat(), "window_end": window.end.isoformat(), "timezone": window.tz,
        "queries": [], "excluded_candidates": [], "events": [],
        "cache_hits": cache_hits, "agent_runs": agent_runs,
    }

    async with usage_client(api_key, "material") as client:
        agents = build_agents(models, client)
        issuer_json = {"ticker": issuer.ticker, "company": issuer.company, "exchange": issuer.exchange,
                       "market_cap_usd": issuer.market_cap if issuer.currency == "USD" else None,
                       "market_cap": issuer.market_cap, "currency": issuer.currency,
                       "sector": issuer.sector, "industry": issuer.industry}
        window_json = {"window_start": window.start.isoformat(), "window_end": window.end.isoformat()}

        # 1. Search profile.
        profile = SearchProfile(aliases=[core_name(issuer.company)] + ([issuer.short_name] if issuer.short_name else []))
        profile_key = fingerprint({"version": CACHE_VERSION, "kind": "profile", "model": models["research"],
                                   "prompt": PROFILE, "issuer": issuer.model_dump(exclude={"market_cap"})})
        cached_profile = cache.get(profile_key)
        if cached_profile is not None:
            try:
                profile = SearchProfile.model_validate(cached_profile["profile"])
                cache_hits["profile"] = 1
                stage("Reusing the cached company search profile…")
            except (KeyError, ValidationError):
                pass
        if not cache_hits["profile"]:
            stage("Building the company search profile (aliases, brands, investor-relations pages)…")
            try:
                agent_runs["profile"] += 1
                result = await Runner.run(agents["profile"], json.dumps({**issuer_json, "website": issuer.website}),
                                          max_turns=2, run_config=run_config)
                found = result.final_output
                profile = SearchProfile(
                    aliases=list(dict.fromkeys(profile.aliases + found.aliases))[:8],
                    former_names=found.former_names[:4], brands=found.brands[:6], subsidiaries=found.subsidiaries[:6],
                    ir_urls=[u for u in found.ir_urls if canonical_url(u)][:4],
                    newsroom_urls=[u for u in found.newsroom_urls if canonical_url(u)][:4])
                cache.put(profile_key, {"profile": profile.model_dump(mode="json")}, PROFILE_TTL)
            except (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                raise InputError(api_error_message(exc)) from None
            except Exception as exc:
                diagnostics.append(f"Search profile unavailable ({type(exc).__name__}); using the company name only.")
        record["profile"] = profile.model_dump()

        # 2. Discovery.
        registry: dict[str, SourceRef] = {}
        candidates: list[Candidate] = []
        decisions: list[Decision] = []
        semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
        official_agent = official_discovery(agents["discovery"], official_domains(issuer, profile))
        counts = {"ok": 0, "failed": 0, "failures": [], "search_ok": 0, "search_failed": 0}

        async def discover(purpose: str, query: str, leads: list[dict] | None = None):
            agent = agents["feed"] if purpose == "feed" else official_agent if purpose == "official" else agents["discovery"]
            async with semaphore:
                try:
                    agent_runs["feed" if purpose == "feed" else "discovery"] += 1
                    result = await Runner.run(agent, json.dumps({
                        "issuer": issuer_json, "profile": profile.model_dump(), **window_json, "query": query,
                        **({"leads": leads} if leads else {}),
                    }), max_turns=1, run_config=run_config)
                    if purpose == "feed":
                        found = {}  # Only the provider's registry can ground a feed classification.
                        rows, invalid, status = parse_feed_candidates(result.final_output, leads or [])
                    else:
                        found, completed = collect_sources(result, datetime.now(UTC))
                        if not completed:
                            raise DigestError("the hosted search did not complete")
                        rows, invalid, status = parse_candidates(result.final_output)
                    return purpose, query, rows, found, invalid, status, None
                except (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                    raise InputError(api_error_message(exc)) from None
                except DigestError as exc:
                    return purpose, query, [], {}, 0, "failed", str(exc)
                except Exception as exc:
                    return purpose, query, [], {}, 0, "failed", api_error_message(exc)

        def absorb(results) -> set[str]:
            new_keys = set()
            for purpose, query, rows, found, invalid, status, failure in results:
                record["queries"].append({"purpose": purpose, "query": query, "status": status,
                                          "candidates": len(rows), "invalid": invalid, "failure": failure})
                if failure:
                    counts["failed"] += 1
                    counts["search_failed"] += purpose != "feed"
                    counts["failures"].append(failure)
                    stage(f"Search failed ({purpose}): {failure}")
                    continue
                counts["ok"] += 1
                counts["search_ok"] += purpose != "feed"
                for key, ref in found.items():
                    registry.setdefault(key, ref)
                for row in rows:
                    key = canonical_url(row.url)
                    decision = candidate_gate(row, issuer, window, registry.get(key) if key else None)
                    candidates.append(row)
                    decisions.append(decision)
                    if decision.passed or decision.pending:
                        new_keys.add(row.development_key)
            return new_keys

        plan = query_plan(issuer, profile, window)
        feed_unavailable = False
        try:
            leads = await asyncio.wait_for(asyncio.to_thread(fetch_feed_leads, issuer.symbol, window), timeout=20)
        except Exception as exc:
            leads = []
            feed_unavailable = True
            diagnostics.append(f"Yahoo Finance news feed unavailable ({type(exc).__name__}).")
        leads = leads[:FEED_COUNT]
        registry.update(feed_sources(leads, datetime.now(UTC)))
        stage(f"Searching {len(plan)} queries (broad news, official announcements, major outlets, material event "
              f"types and one per window day) and classifying {len(leads)} dated Yahoo Finance headline(s)…")
        first_keys = absorb(await gather_or_cancel(
            *(discover(p, q) for p, q in plan),
            *([discover("feed", f"{core_name(issuer.company)} latest company developments", leads)] if leads else [])))
        if counts["search_ok"] == 0:
            reason = counts["failures"][0] if counts["failures"] else "no search completed"
            raise ResearchUnavailable(f"Research could not be completed: all {len(plan)} searches failed ({reason}). "
                                      "This is a research limitation, not a finding that there is no news.")
        stage(f"{counts['search_ok']} of {len(plan)} searches completed; "
              f"{sum(d.passed for d in decisions)} of {len(candidates)} candidate items passed the gates so far.")

        follow = followup_plan(issuer, profile, record["queries"], candidates, decisions)
        if follow and remaining() > FOLLOWUP_BUDGET:
            stage(f"Targeted follow-up searches ({len(follow)}) for failed queries, undated leads or sparse coverage…")
            before = len(candidates)
            new_keys = absorb(await gather_or_cancel(*(discover(p, q) for p, q in follow))) - first_keys
            diagnostics.append(f"Follow-up round: {len(candidates) - before} candidate(s), "
                               f"{len(new_keys)} new qualifying development key(s).")
        elif follow:
            diagnostics.append("Follow-up searches skipped to stay within the run deadline.")
        else:
            diagnostics.append("No follow-up searches needed: initial coverage was sufficient under the staged-search rules.")

        for row, decision in zip(candidates, decisions):
            if not decision.passed and not decision.pending:
                record["excluded_candidates"].append({"headline": row.headline, "url": row.url, "gate": decision.gate,
                                                      "exclusion_reason": decision.reason})

        # 3. Cluster passing candidates, and those whose publication time only verification can settle.
        keep = [i for i, d in enumerate(decisions) if d.passed or d.pending]
        pool = [candidates[i] for i in keep]
        pool_decisions = [decisions[i] for i in keep]
        groups = group_candidates(pool)
        if len(groups) > 1 and remaining() > CONSOLIDATE_BUDGET:
            stage(f"Consolidating {len(pool)} qualifying article(s) in {len(groups)} group(s) into distinct developments…")
            try:
                agent_runs["consolidation"] += 1
                result = await Runner.run(agents["consolidator"], json.dumps({
                    "company": issuer.company, "groups": [
                        {"group_id": gid, "articles": [{"headline": pool[i].headline, "summary": pool[i].summary,
                                                         "publisher": pool[i].publisher, "published": pool[i].published,
                                                         "category": pool[i].category, "status": pool[i].status,
                                                         "milestone": pool[i].milestone} for i in members]}
                        for gid, members in enumerate(groups)]}), max_turns=1, run_config=run_config)
                consolidation: Consolidation = result.final_output
                groups, notes = apply_merges(groups, [m.group_ids for m in consolidation.merges])
                diagnostics.extend(notes)
            except Exception as exc:
                diagnostics.append(f"Consolidation unavailable ({type(exc).__name__}); development-key grouping used.")
        clusters = [Cluster(event_id=f"E{n + 1}", candidates=[pool[i] for i in members],
                            decisions=[pool_decisions[i] for i in members])
                    for n, members in enumerate(groups)]
        clusters.sort(key=lambda c: c.strength(), reverse=True)

        # 4. Verify each development against its sources.
        verify_semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)
        verification_keys: dict[str, str] = {}
        verification_times: dict[str, datetime] = {}
        reused: set[str] = set()

        async def verify(cluster: Cluster):
            async with verify_semaphore:
                key = event_cache_key(issuer, models["research"], request, cluster, registry)
                verification_keys[cluster.event_id] = key
                cached = cache.get(key)
                if cached is not None:
                    try:
                        prior = CachedVerification.model_validate(cached)
                        primary = prior.sources.get(canonical_url(prior.event.primary_url))
                        if prior.event.verified and prior.event.page_accessible and primary and primary.opened:
                            cache_hits["verification"] += 1
                            reused.add(cluster.event_id)
                            verification_times[cluster.event_id] = prior.verified_at
                            return cluster, prior.event, prior.sources, None
                    except ValidationError:
                        pass
                if remaining() < VERIFY_BUDGET:
                    return cluster, None, {}, "skipped to stay within the run deadline"
                try:
                    agent_runs["verification"] += 1
                    result = await Runner.run(agents["verifier"], json.dumps({
                        "issuer": issuer_json, **window_json, "language": request.language,
                        "articles": [{"url": c.url, "publisher": c.publisher, "headline": c.headline,
                                      "summary": c.summary, "published": c.published, "updated": c.updated,
                                      "status": c.status, "category": c.category, "source_type": c.source_type,
                                      "original_outlet": c.original_outlet} for c in cluster.candidates[:8]],
                    }), max_turns=1, run_config=run_config)
                    found, _ = collect_sources(result, datetime.now(UTC))
                    verification_times[cluster.event_id] = datetime.now(UTC)
                    return cluster, VerifiedEvent.model_validate(parse_block(result.final_output, "verified")), found, None
                except (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                    raise InputError(api_error_message(exc)) from None
                except (DigestError, ValidationError) as exc:
                    return cluster, None, {}, f"unusable verifier output ({type(exc).__name__})"
                except Exception as exc:
                    return cluster, None, {}, api_error_message(exc)

        to_verify = clusters[:MAX_VERIFY]
        if to_verify:
            stage(f"Verifying {len(to_verify)} candidate development(s) against original sources…")
        verified = await gather_or_cancel(*(verify(c) for c in to_verify))
        verified += [(c, None, {}, f"beyond the {MAX_VERIFY}-event verification limit") for c in clusters[MAX_VERIFY:]]
        if cache_hits["verification"]:
            stage(f"Reused {cache_hits['verification']} unchanged development verification(s) from the last 30 minutes.")

    # 5. Gate again, rank and build entries.
    entries: list[Entry] = []
    unverified = boundary_material = 0
    for cluster, event, found, failure in verified:
        entry, event_record = build_entry(cluster, event, found, failure, registry, issuer, window, request.language)
        event_record["verification_cached"] = cluster.event_id in reused
        checked_at = verification_times.get(cluster.event_id)
        event_record["verified_at"] = checked_at.isoformat() if checked_at else None
        record["events"].append(event_record)
        if entry:
            entries.append(entry)
            unverified += entry.verification == "search_results_only"
            primary = found.get(canonical_url(event.primary_url)) if event else None
            if (event and event.verified and event.page_accessible and primary and primary.opened
                    and checked_at and cluster.event_id not in reused):
                source_keys = {canonical_url(c.url) for c in cluster.candidates}
                source_keys.update([canonical_url(event.primary_url), canonical_url(event.secondary_url)])
                cache.put(verification_keys[cluster.event_id], CachedVerification(
                    event=event, sources={**{k: v for k, v in registry.items() if k in source_keys}, **found},
                    verified_at=checked_at,
                ).model_dump(mode="json"), max(0, EVENT_TTL - (datetime.now(UTC) - checked_at).total_seconds()))
        elif event_record.get("boundary") and any(c.materiality == "high" for c in cluster.candidates):
            boundary_material += 1
    entries.sort(key=lambda e: e.rank_key, reverse=True)
    ranked = list(entries)
    qualified = len(entries)
    if request.max_results is not None:
        entries = entries[:request.max_results]
    notes = []
    if counts["search_failed"] and counts["search_failed"] * 4 >= counts["search_ok"] + counts["search_failed"]:
        notes.append(f"{counts['search_failed']} of {counts['search_ok'] + counts['search_failed']} searches failed, so coverage may be incomplete.")
    if feed_unavailable:
        notes.append("Yahoo Finance returned no headlines, possibly because it is limiting requests, "
                     "so this run relied on web search alone.")
    if any(q["purpose"] == "feed" and (q["failure"] or q["invalid"]) for q in record["queries"]):
        notes.append("Some feed headlines could not be classified; feed coverage may be incomplete.")
    if any(q["purpose"] != "feed" and q["invalid"] for q in record["queries"]):
        notes.append("Some search candidates could not be parsed; coverage may be incomplete.")
    if unverified and any(e.verification == "search_results_only" for e in entries):
        notes.append("Some entries could not be opened for full-text verification and rely on search-result content.")
    if boundary_material:
        notes.append("A potentially material item dated on the window's first, partial day was omitted because its exact time could not be confirmed.")
    diagnostics.append(f"Sources observed: {len(registry)}; candidates: {len(candidates)}; developments: {len(clusters)}; "
                       f"published: {len(entries)} of {qualified} qualifying.")
    diagnostics.append(f"Agent runs (excluding SDK retries/turns): {sum(agent_runs.values())}; "
                       f"cached profiles: {cache_hits['profile']}; cached verifications: {cache_hits['verification']}.")
    if cache.error:
        diagnostics.append(f"Local cache unavailable ({cache.error}); affected work ran without caching.")
    return MaterialResult(
        issuer=issuer, run_at=run_at, window_start=window.start, window_end=window.end, timezone=window.tz,
        hours=request.hours, max_results=request.max_results, entries=entries, qualified_entries=ranked,
        qualified_count=qualified,
        outcome="entries" if entries else "none_found", coverage_note=" ".join(notes) or None,
        record=record, diagnostics=diagnostics,
    )


def build_entry(cluster: Cluster, event: VerifiedEvent | None, found: dict[str, SourceRef], failure: str | None,
                registry: dict[str, SourceRef], issuer: Issuer, window: Window, language: str):
    """Apply the gates to one development. Returns (entry or None, internal event record)."""
    sources = {**registry, **found}
    best = cluster.best()
    passing = [(c, d) for c, d in zip(cluster.candidates, cluster.decisions) if d.passed]
    first_seen = min((d.effective_date for _, d in passing if d.effective_date), key=sort_moment, default=None)
    record = {
        "event_id": cluster.event_id, "event_headline": best.headline, "event_category": best.category,
        "event_status": best.status, "first_reported_at": first_seen, "candidate_count": len(cluster.candidates),
        "independent_lineages": sorted(cluster.lineages()),
        "duplicate_articles": [c.url for c in cluster.candidates if c is not best],
        "company_is_main_subject": True, "subject_rationale": best.subject_rationale,
        "materiality_rationale": f"changes {best.mechanism.changes} through {best.mechanism.through}",
        "verification_failure": failure, "inclusion_decision": "excluded", "exclusion_reason": None,
        "boundary": False,
    }
    refuted = event is not None and not event.verified and event.page_accessible
    if event is not None and not event.verified and not event.page_accessible:
        record["verification_failure"] = f"source inaccessible: {event.failure_reason or 'not stated'}"
    if refuted or (event is not None and event.verified):
        if refuted:
            record["exclusion_reason"] = event.failure_reason or "verifier could not substantiate the development"
            return None, record
        primary_key = canonical_url(event.primary_url)
        primary = sources.get(primary_key) if primary_key else None
        if primary is None:
            primary_key, primary = canonical_url(best.url), sources.get(canonical_url(best.url))
            event.primary_url, event.primary_publisher = best.url, best.publisher
            event.primary_published, event.primary_timestamp_basis = best.published, best.timestamp_basis
            event.primary_source_type, event.primary_lineage = best.source_type, best.original_outlet
        # The earliest checked reporting of the development anchors its date.
        stamps = [checked_date(event.primary_published, event.primary_timestamp_basis, primary), first_seen]
        published = min((s for s in stamps if s), key=sort_moment, default=None)
        decision = event_gate(event, issuer, window, published)
        record.update({
            "event_headline": event.headline, "event_category": event.category, "event_status": event.status,
            "first_reported_at": published, "event_date": event.event_date,
            "date_precision": "date" if published and len(published) == 10 else "timestamp",
            "company_is_main_subject": event.company_is_main_subject, "subject_rationale": event.subject_rationale,
            "materiality_rationale": f"changes {event.mechanism.changes} through {event.mechanism.through}",
            "novelty_rationale": event.novelty_rationale, "key_facts": event.key_facts,
            "uncertainties": event.uncertainties, "business_implication": event.why,
            "boundary": decision.boundary,
        })
        if not decision.passed:
            record["exclusion_reason"] = decision.reason
            return None, record
        publisher = event.primary_publisher or (primary.publisher if primary else publisher_of(event.primary_url))
        why, problem = (status_language(event.why.strip(), event.status, publisher)
                        if language.lower().startswith("en") else (event.why.strip(), None))
        if problem:
            record["exclusion_reason"] = problem
            return None, record
        links = [source_link(event.primary_url, publisher, primary, event.headline, event.primary_source_type)]
        secondary_key = canonical_url(event.secondary_url)
        primary_lineage = lineage(event.primary_lineage, event.primary_url)
        if (secondary_key and secondary_key != primary_key and secondary_key in sources
                and lineage(event.secondary_lineage, event.secondary_url) != primary_lineage):
            ref = sources[secondary_key]
            links.append(source_link(event.secondary_url, event.secondary_publisher or ref.publisher, ref, ref.title))
        record.update({"inclusion_decision": "included", "sources": links})
        entry = Entry(
            event_id=cluster.event_id, date=display_date(decision.effective_date, window.tz),
            headline=event.headline.strip(), reported=event.status in REPORTED_STATUSES,
            status=event.status, status_label=STATUS_LABELS[event.status], category=event.category, why=why,
            sources=links, verification="opened" if any(s.opened for s in found.values()) else "search_results_only",
            first_reported_at=decision.effective_date,
            rank_key=rank_key(event, event.primary_source_type, decision.effective_date),
            key_facts=[fact for fact in event.key_facts if fact.strip()][:8],
            uncertainties=[note for note in event.uncertainties if note.strip()][:4],
            event_date=normalize_source_timestamp(event.event_date),
        )
        return entry, record

    # Verification unavailable (access failure, error, or deadline): fall back to the gated discovery record.
    if not passing:
        record["boundary"] = any(d.boundary for d in cluster.decisions)
        record["exclusion_reason"] = ("date-only stamp on a partial window day; no timestamp could be confirmed"
                                      if record["boundary"] else "publication date could not be confirmed")
        return None, record
    # Unverified items may use any non-social source, attributed to the originating outlet.
    usable = [(c, d) for c, d in passing if not SOCIAL.search(publisher_of(c.url))]
    if not usable:
        record["exclusion_reason"] = "could not be verified and the only source was a social-media post"
        return None, record
    lead, decision = max(usable, key=lambda pair: EVIDENCE_RANK[pair[0].source_type])
    publisher = lead.publisher or publisher_of(lead.url)
    why = f"{lead.summary.strip()} This could affect {lead.mechanism.changes.strip().rstrip('.')}."
    why, problem = (status_language(why, lead.status, lead.original_outlet or publisher)
                    if language.lower().startswith("en") else (why, None))
    if problem:
        record["exclusion_reason"] = problem
        return None, record
    ref = sources.get(canonical_url(lead.url))
    links = [source_link(lead.url, publisher, ref, lead.headline, lead.source_type)]
    record.update({"inclusion_decision": "included", "sources": links, "first_reported_at": decision.effective_date})
    # Conservative scores: an unverified item never outranks a verified one of similar weight.
    reputable = lead.source_type in FALLBACK_SOURCE_TYPES
    rank = [{"high": 3, "medium": 2}.get(lead.materiality, 1) - (0 if reputable else 1), 2, 2, 0,
            sort_moment(decision.effective_date)]
    return Entry(
        event_id=cluster.event_id, date=display_date(decision.effective_date, window.tz),
        headline=re.sub(r"\s+", " ", lead.headline).strip(), reported=lead.status in REPORTED_STATUSES,
        status=lead.status, status_label=STATUS_LABELS[lead.status], category=lead.category, why=why, sources=links,
        verification="search_results_only", first_reported_at=decision.effective_date, rank_key=rank,
        event_date=normalize_source_timestamp(lead.event_date),
    ), record


def source_link(url: str, publisher: str, ref: SourceRef | None, fallback_title: str, source_type: str = "other") -> dict:
    """A published source link with the provider date and tool-returned snippets, when observed."""
    return {"url": url, "publisher": publisher, "title": ref.title if ref else fallback_title,
            "published": ref.published if ref else None, "snippets": list(ref.snippets[:2]) if ref else [],
            "source_type": source_type}


def material_once(request: MaterialRequest, settings, stage) -> tuple[MaterialResult, str]:
    """Run once synchronously under its own trace. Returns (result, trace URL); see runner.digest_once."""
    from agents import gen_trace_id, trace
    from ..runner import enable_tracing

    enable_tracing(settings.api_key)
    trace_id = gen_trace_id()
    trace_url = f"https://platform.openai.com/traces/trace?trace_id={trace_id}"

    async def run():
        with trace("Material Company News", trace_id=trace_id, metadata={"ticker": request.ticker}):
            deadline = asyncio.get_running_loop().time() + settings.timeout
            async with asyncio.timeout_at(deadline):
                return await run_material(request, settings.api_key, settings.models, stage, deadline=deadline)

    try:
        return asyncio.run(run()), trace_url
    except BaseException as exc:
        exc.trace_url = trace_url
        raise
