"""Records for the material company news agent.

Model output is parsed into these contracts, then every inclusion decision is
re-checked in Python (see gates.py). Profiles and verified evidence may be cached.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

Category = Literal[
    # Potentially material
    "earnings", "guidance", "capital_return", "financing", "merger_acquisition", "divestiture",
    "strategic_investment", "product_platform", "contract", "partnership", "regulatory_market_access",
    "litigation", "investigation", "incident", "leadership", "operations",
    # Default exclusions
    "routine_product", "developer_content", "ecosystem_integration", "security_patch", "marketing_award_event",
    "analyst_commentary", "price_action", "macro_sector", "unsupported_speculation", "other",
]
EXCLUDED_CATEGORIES = {
    "routine_product", "developer_content", "ecosystem_integration", "security_patch", "marketing_award_event",
    "analyst_commentary", "price_action", "macro_sector", "unsupported_speculation", "other",
}
# Categories where a disclosed amount is the main evidence of scale.
SIZE_GATED_CATEGORIES = {"contract", "partnership"}


def _bare_category(value):
    """Accept a label copied from a prompt example, e.g. "excluded: routine_product"."""
    if isinstance(value, str):
        value = value.strip()
        if value.lower().startswith("excluded:"):
            value = value.split(":", 1)[1].strip()
    return value


# A category as written by a model. Internal records such as Entry use Category directly.
ModelCategory = Annotated[Category, BeforeValidator(_bare_category)]

Status = Literal[
    "announced", "authorized", "agreed", "approved", "completed", "launched", "filed", "ruled",
    "scheduled", "reported", "reported_talks", "under_consideration", "unconfirmed_report",
]
REPORTED_STATUSES = {"reported", "reported_talks", "under_consideration", "unconfirmed_report"}
STATUS_LABELS = {
    "announced": "announced", "authorized": "authorized", "agreed": "agreed", "approved": "approved",
    "completed": "completed", "launched": "launched", "filed": "filed", "ruled": "ruled", "scheduled": "scheduled",
    "reported": "reported", "reported_talks": "reported talks", "under_consideration": "reported; under consideration",
    "unconfirmed_report": "unconfirmed report",
}
SubjectRole = Literal["main", "secondary", "incidental", "roundup", "listicle", "metadata_only"]
SourceType = Literal["official_release", "filing", "regulator", "original_reporting", "syndicated", "aggregator", "other"]
EVIDENCE_RANK = {"official_release": 3, "filing": 3, "regulator": 3, "original_reporting": 2,
                 "syndicated": 1, "aggregator": 0, "other": 0}


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


class SearchProfile(Contract):
    """Discovery aliases. Aliases find stories; they never qualify one by themselves."""
    aliases: list[str] = Field(default_factory=list)
    former_names: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    subsidiaries: list[str] = Field(default_factory=list)
    ir_urls: list[str] = Field(default_factory=list)
    newsroom_urls: list[str] = Field(default_factory=list)


class Mechanism(Contract):
    """'This development could matter because it changes ___ through ___.'"""
    changes: str = ""
    through: str = ""


class Candidate(Contract):
    """One development extracted from one search result."""
    headline: str = Field(max_length=200)
    summary: str = Field(max_length=600)
    url: str
    publisher: str | None = None
    published: str | None = None
    updated: str | None = None
    timestamp_basis: str | None = None
    event_date: str | None = None
    original_announcement_date: str | None = None
    new_fact: str | None = None
    is_rehash: bool = False
    likely_in_window: bool = True       # the model's reading when no publication date is shown
    category: ModelCategory = "other"
    status: Status = "reported"
    subject_role: SubjectRole = "incidental"
    subject_rationale: str = ""
    materiality: Literal["high", "medium", "low", "none"] = "none"
    mechanism: Mechanism = Field(default_factory=Mechanism)
    amount_usd: float | None = None
    source_type: SourceType = "other"
    original_outlet: str | None = None
    development_key: str = ""
    milestone: str = ""


class VerifiedEvent(Contract):
    """The verifier's reading of one clustered development after opening its sources."""
    verified: bool
    failure_reason: str | None = None
    page_accessible: bool = True
    headline: str = Field(default="", max_length=140)
    why: str = Field(default="", max_length=700)
    category: ModelCategory = "other"
    status: Status = "reported"
    primary_url: str | None = None
    primary_publisher: str | None = None
    primary_published: str | None = None
    primary_timestamp_basis: str | None = None
    primary_source_type: SourceType = "other"
    primary_lineage: str | None = None
    secondary_url: str | None = None
    secondary_publisher: str | None = None
    secondary_lineage: str | None = None
    event_date: str | None = None
    original_announcement_date: str | None = None
    new_fact: str | None = None
    is_rehash: bool = False
    company_is_main_subject: bool = False
    subject_rationale: str = ""
    mechanism: Mechanism = Field(default_factory=Mechanism)
    novelty_rationale: str = ""
    key_facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    amount_usd: float | None = None
    magnitude: int = Field(default=1, ge=1, le=5)
    directness: int = Field(default=1, ge=1, le=5)
    novelty: int = Field(default=1, ge=1, le=5)


class ClusterMerge(Contract):
    group_ids: list[int]
    reason: str = ""


class Consolidation(Contract):
    merges: list[ClusterMerge] = Field(default_factory=list)


class SourceRef(Contract):
    """A source URL observed in search-tool metadata (never one written only in model prose)."""
    url: str
    title: str
    publisher: str
    published: str | None = None
    retrieved_at: AwareDatetime
    snippets: list[str] = Field(default_factory=list)
    opened: bool = False


class CachedVerification(Contract):
    event: VerifiedEvent
    sources: dict[str, SourceRef]
    verified_at: AwareDatetime


class Entry(Contract):
    """One published row of the digest."""
    event_id: str
    date: str                       # display date in the run timezone
    headline: str
    reported: bool
    status: Status
    status_label: str
    category: Category
    why: str
    sources: list[dict]             # [{"url", "publisher", "title", "published", "snippets"}], one or two
    verification: Literal["opened", "search_results_only"]
    first_reported_at: str | None = None
    rank_key: list[float] = Field(default_factory=list)
    # The verifier's reading after opening the sources; empty when the entry relies on search results only.
    key_facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    event_date: str | None = None


class MaterialResult(Contract):
    issuer: Issuer
    run_at: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    timezone: str
    hours: float
    max_results: int | None = None
    entries: list[Entry] = Field(default_factory=list)
    # Every qualifying entry in rank order, before max_results is applied (the digest reads these).
    qualified_entries: list[Entry] = Field(default_factory=list)
    qualified_count: int = 0
    outcome: Literal["entries", "none_found"] = "entries"
    coverage_note: str | None = None
    record: dict = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)
