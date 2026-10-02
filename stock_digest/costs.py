"""Per-search API cost estimates from raw Responses usage, before SDK parsing.

Rates checked 2026-10-01: https://developers.openai.com/api/docs/pricing
Cache accounting: https://developers.openai.com/api/docs/guides/prompt-caching
Search actions: https://developers.openai.com/api/docs/guides/tools-web-search
This is a usage estimate, not an invoice; never price an unknown model as free.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
import json
import re

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

PRICING_DATE = "2026-10-01"
PRICING_URL = "https://developers.openai.com/api/docs/pricing"
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Rates:
    input: str
    cached: str
    output: str
    write: str | None = None
    long_context: bool = False


# USD per million tokens, standard processing. Keep explicit aliases; only dated
# snapshots of these aliases inherit a price. Arbitrary prefixes must not match.
RATES = {
    "gpt-6-luna": Rates("0.10", "0.01", "0.50", "0.125", True),
    "gpt-6-sol": Rates("2.00", "0.20", "10.00", "2.50", True),
    "gpt-6.1-sol": Rates("2.00", "0.10", "10.00", "2.50", True),
    "gpt-6-astra": Rates("10.00", "1.00", "50.00", "12.50", True),
    "gpt-4.1": Rates("2.00", "0.50", "8.00"),
    "gpt-4.1-mini": Rates("0.40", "0.10", "1.60"),
    "gpt-4.1-nano": Rates("0.10", "0.025", "0.40"),
}
TOKEN_FIELDS = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")


def model_alias(model: str) -> str:
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)


def count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid usage count")
    return value


class CostTracker:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.pending: dict[int, str] = {}
        self.requests: dict[str, int] = {}
        self.problems: dict[str, set[str]] = {}
        self.seen: set[str] = set()
        self.sequence = 0

    def problem(self, kind: str, message: str):
        self.problems.setdefault(kind, set()).add(message)

    def start(self, kind: str) -> int:
        self.sequence += 1
        self.pending[self.sequence] = kind
        self.requests[kind] = self.requests.get(kind, 0) + 1
        return self.sequence

    def record(self, kind: str, response: dict, request: dict):
        response_id = response.get("id")
        if response_id and response_id in self.seen:
            return
        if response_id:
            self.seen.add(response_id)
        model = response.get("model") or request.get("model") or "unknown"
        alias = model_alias(model)
        rates = RATES.get(alias)
        row = self.rows.setdefault((kind, model), {
            "model": model, "model_calls": 0, "web_search_calls": 0,
            "llm_usd": Decimal(0), "web_search_usd": Decimal(0),
            **{field: 0 for field in TOKEN_FIELDS},
        })
        row["model_calls"] += 1
        search_calls = 0
        for item in response.get("output") or []:
            if item.get("type") != "web_search_call":
                continue
            action = (item.get("action") or {}).get("type")
            if action == "search" and item.get("status") == "completed":
                search_calls += 1
            elif action not in {"open_page", "find_in_page"} or item.get("status") != "completed":
                self.problem(kind, "Some web-search usage could not be priced.")
        row["web_search_calls"] += search_calls
        tools = request.get("tools") or []
        preview = any(t.get("type", "").startswith("web_search_preview") for t in tools)
        row["web_search_usd"] += Decimal("0.025" if preview and alias.startswith("gpt-4.") else "0.01") * search_calls
        if alias == "gpt-4.1-mini" and search_calls and not preview:
            # The fixed 8k search-content block is a separate published tool charge.
            row["web_search_usd"] += Decimal("0.40") * 8000 * search_calls / MILLION

        usage = response.get("usage")
        try:
            if not isinstance(usage, dict):
                raise ValueError("No usage")
            input_tokens, output_tokens = count(usage.get("input_tokens")), count(usage.get("output_tokens"))
            details = usage.get("input_tokens_details") or {}
            cached = count(details.get("cached_tokens", 0))
            writes = count(details.get("cache_write_tokens", 0))
            reasoning = count((usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0))
            if cached + writes > input_tokens or reasoning > output_tokens:
                raise ValueError("Inconsistent usage")
        except (ValueError, TypeError, AttributeError):
            self.problem(kind, "Some model responses did not report usable token counts.")
            return
        for field, value in zip(TOKEN_FIELDS, (input_tokens, cached, writes, output_tokens, reasoning)):
            row[field] += value
        if rates is None:
            self.problem(kind, f"No configured price for {model}.")
            return
        tier = response.get("service_tier") or request.get("service_tier") or "default"
        if tier not in {"default", "auto", "flex"} or (tier == "flex" and not rates.long_context):
            self.problem(kind, f"No configured price for service tier {tier}.")
            return
        if "cached_tokens" not in details or (rates.write and "cache_write_tokens" not in details):
            self.problem(kind, "Some prompt-cache token details were unavailable; standard input rates were assumed for them.")
        if writes and not rates.write:
            self.problem(kind, "Cache-write pricing is unavailable for this model.")
            return
        multiplier = Decimal("0.5") if tier == "flex" else Decimal(1)
        long = rates.long_context and input_tokens > 272_000
        input_multiplier = multiplier * (2 if long else 1)
        output_multiplier = multiplier * (Decimal("1.5") if long else 1)
        row["llm_usd"] += (
            ((input_tokens - cached - writes) * Decimal(rates.input) + cached * Decimal(rates.cached)
             + writes * Decimal(rates.write or rates.input)) * input_multiplier
            + output_tokens * Decimal(rates.output) * output_multiplier
        ) / MILLION

    def snapshot(self, *, complete: bool) -> dict:
        kinds = sorted(set(self.requests) | {kind for kind, _ in self.rows} | set(self.problems))
        by_agent = {}
        notes = set()
        for kind in kinds:
            rows = [row for (owner, _), row in self.rows.items() if owner == kind]
            problems = set(self.problems.get(kind, ()))
            pending = sum(owner == kind for owner in self.pending.values())
            if complete and pending:
                problems.add("Some requests ended without reported usage; their charges may be missing.")
            notes.update(problems)
            totals = {field: sum(row[field] for row in rows) for field in TOKEN_FIELDS + ("model_calls", "web_search_calls")}
            llm = sum((row["llm_usd"] for row in rows), Decimal(0))
            web = sum((row["web_search_usd"] for row in rows), Decimal(0))
            by_agent[kind] = {**totals, "requests": self.requests.get(kind, 0), "pending_requests": pending,
                              "llm_usd": float(llm), "web_search_usd": float(web), "known_total_usd": float(llm + web),
                              "total_usd": None if problems else float(llm + web),
                              "models": sorted(row["model"] for row in rows)}
        total = sum(row["known_total_usd"] for row in by_agent.values())
        return {"currency": "USD", "estimated": True, "complete": complete, "partial": bool(notes),
                "total_usd": None if notes else total, "known_total_usd": total,
                "llm_usd": sum(row["llm_usd"] for row in by_agent.values()),
                "web_search_usd": sum(row["web_search_usd"] for row in by_agent.values()),
                "by_agent": by_agent, "notes": sorted(notes),
                "pricing_as_of": PRICING_DATE, "pricing_url": PRICING_URL}


active_costs: ContextVar[CostTracker | None] = ContextVar("search_costs", default=None)


@contextmanager
def tracking_costs():
    tracker = CostTracker()
    token = active_costs.set(tracker)
    try:
        yield tracker
    finally:
        active_costs.reset(token)


def track_search_costs(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with tracking_costs():
            return function(*args, **kwargs)
    return wrapped


def cost_snapshot(*, complete: bool) -> dict:
    tracker = active_costs.get()
    return tracker.snapshot(complete=complete) if tracker else {}


def usage_client(api_key: str, kind: str, *, transport=None) -> AsyncOpenAI:
    """Observe each non-streaming HTTP response, including SDK retry attempts.

    Reading the body here preserves usage even when later schema/JSON parsing fails.
    Only counts/model IDs/prices are retained; prompts, articles and keys are not stored.
    """
    tracker = active_costs.get()
    if tracker is None:
        return AsyncOpenAI(api_key=api_key, max_retries=0)

    async def on_request(request: httpx.Request):
        if request.url.path.rstrip("/").endswith("/responses"):
            request.extensions["stock_digest_cost_id"] = tracker.start(kind)

    async def on_response(response: httpx.Response):
        request = response.request
        call_id = request.extensions.get("stock_digest_cost_id")
        if call_id is None:
            return
        tracker.pending.pop(call_id, None)
        try:
            if "text/event-stream" in response.headers.get("content-type", ""):
                tracker.problem(kind, "Streaming usage was unavailable for this request.")
                return
            await response.aread()
            data = response.json()
            if isinstance(data, dict) and (response.is_success or data.get("usage")):
                tracker.record(kind, data, json.loads(request.content))
            else:
                tracker.problem(kind, "Some failed API requests did not report usage; their charges may be missing.")
        except Exception:
            # Cost tracking must never make a successful research response fail.
            tracker.problem(kind, "Some API usage could not be read; the estimate may be incomplete.")

    http_client = DefaultAsyncHttpxClient(event_hooks={"request": [on_request], "response": [on_response]},
                                        **({"transport": transport} if transport is not None else {}))
    return AsyncOpenAI(api_key=api_key, max_retries=0, http_client=http_client)
