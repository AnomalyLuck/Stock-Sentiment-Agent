"""Orchestration for material company news.

resolve issuer → one 7-day headline fetch (news.fetch_week: Finnhub + Google News) → one screen
agent over the titles → Python gate (supplied articles only, each once) → entries, newest first.
The articles are never opened; every entry says so ("headline_only").
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from agents import RunConfig, Runner
from openai import AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

from ..agents import api_error_message, build_screen
from ..costs import usage_client
from ..market import DigestError, InputError
from ..models import CatalystScreen
from ..news import Screened, WeekNews, article_rows, fetch_week, screen_gate
from .gates import Window, display_date, make_window, status_language
from .identity import resolve_issuer
from .models import REPORTED_STATUSES, STATUS_LABELS, Entry, MaterialResult


class ResearchUnavailable(DigestError):
    """The headline screen could not run; never reported as 'no news'."""


@dataclass(frozen=True)
class MaterialRequest:
    ticker: str
    hours: float = 168
    timezone: str = "UTC"
    max_results: int | None = None
    language: str = "English"
    exchange: str | None = None


async def run_material(request: MaterialRequest, api_key: str, models: dict, stage,
                       *, deadline: float | None = None, run_config: RunConfig | None = None,
                       issuer=None, news=None) -> MaterialResult:
    """``issuer`` and ``news``, when given, are awaitables shared with the digest (runner.run_combined)."""
    run_at = datetime.now(UTC)
    window = make_window(run_at, request.hours, request.timezone)
    stage(f"Resolving {request.ticker}…")
    issuer = await (issuer if issuer is not None else asyncio.to_thread(resolve_issuer, request.ticker, request.exchange))
    stage(f"Resolved {issuer.ticker} to {issuer.company} ({issuer.exchange}).")
    stage("Reading the week's company headlines (Finnhub and Google News)…")
    week: WeekNews = await (news if news is not None else fetch_week(issuer.ticker, issuer.company))
    rows = article_rows(week, window.start)
    diagnostics = [f"News: {len(week.articles)} company headline(s) over 7 days from {week.providers}; "
                   f"{len(rows)} in the window screened by title only."]
    record: dict = {
        "ticker": issuer.ticker, "exchange": issuer.exchange, "company_name": issuer.company,
        "window_start": window.start.isoformat(), "window_end": window.end.isoformat(), "timezone": window.tz,
        "providers": week.providers, "articles": len(week.articles), "screened": len(rows), "events": [],
    }
    screened: list[Screened] = []
    if rows:
        stage(f"Screening {len(rows)} headline(s) from the last 7 days for material developments…")
        run_config = run_config or RunConfig(tracing_disabled=False, trace_include_sensitive_data=True)
        async with usage_client(api_key, "material") as client:
            try:
                result = await Runner.run(build_screen("material", models["research"], client), json.dumps({
                    "ticker": issuer.ticker, "company": issuer.company, "as_of": run_at.isoformat(),
                    "window_start": window.start.isoformat(), "language": request.language, "articles": rows,
                }), max_turns=1, run_config=run_config)
                screen = result.final_output_as(CatalystScreen)
            except (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError) as exc:
                raise InputError(api_error_message(exc)) from None
            except Exception as exc:
                # No exception bodies: they can contain request data, source text, or credentials.
                raise ResearchUnavailable(f"The headline screen failed ({api_error_message(exc)}). "
                                          "This is a research limitation, not a finding that there is no news.") from None
        screened, notes = screen_gate(screen, week, rows)
        diagnostics.extend(notes)

    entries: list[Entry] = []
    for number, kept in enumerate(screened, 1):
        entry, event_record = build_entry(f"E{number}", kept, window, request.language)
        record["events"].append(event_record)
        if entry:
            entries.append(entry)
    entries.sort(key=lambda e: e.first_reported_at or "", reverse=True)
    ranked = list(entries)
    if request.max_results is not None:
        entries = entries[:request.max_results]
    notes = list(week.notes)
    if not week.articles:
        notes.append("No headlines naming the company were found for the last 7 days.")
    diagnostics.append(f"Screen kept {len(screened)} development(s); published {len(entries)} of {len(ranked)}.")
    return MaterialResult(
        issuer=issuer, run_at=run_at, window_start=window.start, window_end=window.end, timezone=window.tz,
        hours=request.hours, max_results=request.max_results, entries=entries, qualified_entries=ranked,
        qualified_count=len(ranked), outcome="entries" if entries else "none_found",
        coverage_note=" ".join(notes) or None, record=record, diagnostics=diagnostics,
    )


def build_entry(event_id: str, kept: Screened, window: Window, language: str) -> tuple[Entry | None, dict]:
    """One screened development as an entry dated by its earliest cited article.

    Returns (entry or None, internal event record).
    """
    item, articles = kept.item, kept.articles
    first = min(articles, key=lambda article: article.published_at)
    first_reported = first.published_at.isoformat(timespec="seconds")
    record = {"event_id": event_id, "event_headline": item.headline, "event_category": item.catalyst_type,
              "event_status": item.status, "first_reported_at": first_reported,
              "articles": [article.url for article in articles], "inclusion_decision": "excluded",
              "exclusion_reason": None}
    # Plain "reported" items already show "— reported" and their outlet; talks and unconfirmed
    # reports also get the outlet in the text, and none may be worded as a confirmed outcome.
    why, problem = item.why, None
    if language.lower().startswith("en") and item.status != "reported":
        why, problem = status_language(item.why, item.status, articles[0].source)
    if problem:
        record["exclusion_reason"] = problem
        return None, record
    links = [{"url": article.url, "publisher": article.source, "title": article.title,
              "published": article.published_at.isoformat(timespec="seconds"), "snippets": []}
             for article in articles[:2]]
    record.update({"inclusion_decision": "included", "sources": links})
    return Entry(
        event_id=event_id, date=display_date(first_reported, window.tz), headline=item.headline,
        reported=item.status in REPORTED_STATUSES, status=item.status, status_label=STATUS_LABELS[item.status],
        category=item.catalyst_type, why=why, sources=links, first_reported_at=first_reported,
    ), record


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
