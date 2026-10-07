"""Records for material company news.

The week's headlines are screened by one agent (stock_digest/news.py, agents.py); each kept
development becomes an Entry citing the provider articles it was read from.
"""
from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..models import CatalystType, Status

REPORTED_STATUSES = {"reported", "reported_talks", "under_consideration", "unconfirmed_report"}
STATUS_LABELS = {
    "announced": "announced", "authorized": "authorized", "agreed": "agreed", "approved": "approved",
    "completed": "completed", "launched": "launched", "filed": "filed", "ruled": "ruled", "scheduled": "scheduled",
    "reported": "reported", "reported_talks": "reported talks", "under_consideration": "reported; under consideration",
    "unconfirmed_report": "unconfirmed report",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class Issuer(Contract):
    ticker: str                      # display symbol, e.g. NVDA or BRK.B
    symbol: str                      # Yahoo symbol, e.g. BRK-B or SHOP.TO
    exchange: str                    # Yahoo exchange name, e.g. NasdaqGS
    company: str
    short_name: str | None = None
    website: str | None = None
    market_cap: float | None = None
    currency: str | None = None
    sector: str | None = None
    industry: str | None = None


class Entry(Contract):
    """One published row of the digest."""
    event_id: str
    date: str                       # display date in the run timezone
    headline: str
    reported: bool
    status: Status
    status_label: str
    category: CatalystType
    why: str
    sources: list[dict]             # [{"url", "publisher", "title", "published", "snippets"}], one or two
    # "headline_only": screened from provider titles; the articles were not opened.
    verification: Literal["headline_only"] = "headline_only"
    first_reported_at: str | None = None


class MaterialResult(Contract):
    issuer: Issuer
    run_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    timezone: str
    hours: float
    max_results: int | None = None
    entries: list[Entry] = Field(default_factory=list)
    # Every qualifying entry, newest first, before max_results is applied.
    qualified_entries: list[Entry] = Field(default_factory=list)
    qualified_count: int = 0
    outcome: Literal["entries", "none_found"] = "entries"
    coverage_note: str | None = None
    record: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
