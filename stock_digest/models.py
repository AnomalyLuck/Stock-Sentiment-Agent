from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


CatalystType = Literal[
    "earnings", "guidance", "buyback", "dividend", "merger_acquisition", "rumor", "analyst", "product",
    "legal_regulatory", "leadership", "financing", "insider", "macro_sector", "other",
]
CATALYST_TYPES = set(CatalystType.__args__)


class ExtendedHours(Contract):
    session: Literal["pre-market", "after-hours"]
    price: Decimal = Field(gt=0)
    base: Decimal = Field(gt=0)
    absolute_change: Decimal
    percent_change: Decimal
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def valid_change(self) -> ExtendedHours:
        if self.absolute_change != self.price - self.base:
            raise ValueError("Invalid extended-hours change")
        if self.percent_change != Decimal(100) * self.absolute_change / self.base:
            raise ValueError("Invalid extended-hours percentage")
        return self


class Benchmark(Contract):
    symbol: str
    name: str
    role: Literal["index", "sector", "industry", "peer"]
    percent_change: Decimal
    basis: str


class MarketSnapshot(Contract):
    ticker: str
    company: str
    short_name: str | None = None
    exchange: str
    security_type: str
    sector: str | None
    industry: str | None = None
    sector_key: str | None = None
    industry_key: str | None = None
    website: str | None = None
    currency: Literal["USD"] = "USD"
    price: Decimal = Field(gt=0)
    comparison_close: Decimal = Field(gt=0)
    absolute_change: Decimal
    percent_change: Decimal
    session_date: date
    comparison_date: date
    session_open: AwareDatetime
    session_close: AwareDatetime
    observed_at: AwareDatetime
    as_of: AwareDatetime
    price_type: Literal["completed regular-session close", "regular-session minute-bar close"]
    volume: Decimal | None = Field(default=None, ge=0)
    volume_start: AwareDatetime | None = None
    volume_end: AwareDatetime | None = None
    average_volume: Decimal | None = Field(default=None, gt=0)
    relative_volume: Decimal | None = Field(default=None, ge=0)
    session_open_price: Decimal | None = Field(default=None, gt=0)
    gap_percent: Decimal | None = None
    since_open_percent: Decimal | None = None
    extended_hours: ExtendedHours | None = None
    benchmarks: list[Benchmark] = Field(default_factory=list)
    feed: str = "Yahoo Finance via yfinance; application-imposed 15-minute cutoff; provider delay/venue coverage may vary"
    adjustment: str = "Split-adjusted price and comparison close; not dividend-adjusted"
    provenance: list[str]
    limitations: list[str]

    @model_validator(mode="after")
    def valid_snapshot(self) -> MarketSnapshot:
        if not self.session_open < self.observed_at <= self.session_close:
            raise ValueError("Price observation is outside the regular session")
        if self.observed_at > self.as_of or self.comparison_date >= self.session_date:
            raise ValueError("Invalid observation or comparison date")
        if self.absolute_change != self.price - self.comparison_close:
            raise ValueError("Invalid absolute change")
        if self.percent_change != Decimal(100) * self.absolute_change / self.comparison_close:
            raise ValueError("Invalid percentage change")
        if self.price_type == "completed regular-session close" and self.observed_at != self.session_close:
            raise ValueError("A completed session must use its closing observation")
        if self.volume is not None and not (
            self.volume_start == self.session_open and self.volume_end == self.observed_at
        ):
            raise ValueError("Volume must identify its actual regular-session period")
        if self.relative_volume is not None and (self.volume is None or self.average_volume is None
                                                 or self.relative_volume != self.volume / self.average_volume):
            raise ValueError("Relative volume must be session volume over the stated average")
        if self.extended_hours is not None and not (
            self.session_close < self.extended_hours.observed_at <= self.as_of
            and self.price_type == "completed regular-session close"
            and self.extended_hours.base == self.price
        ):
            raise ValueError("Extended-hours quotes must follow a completed session and use its close")
        return self


def rounded(value: Decimal, places: str = "0.01") -> Decimal:
    result = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return abs(result) if result == 0 else result


def _change_phrase(price: Decimal, change: Decimal, percent: Decimal, basis: str) -> str:
    change = rounded(change)
    sign = "+" if change >= 0 else "-"
    return f"${rounded(price):,.2f}, {sign}${abs(change):,.2f} ({rounded(percent):+.2f}%) vs. {basis}"


def price_phrase(market: MarketSnapshot) -> str:
    return _change_phrase(market.price, market.absolute_change, market.percent_change, "previous session close")


def move_phrase(market: MarketSnapshot) -> str:
    """Compact direction and percentage for headlines: "up 1.68%", "down 1.89%", "unchanged"."""
    percent = rounded(market.percent_change)
    if percent == 0:
        return "unchanged"
    return f"{'up' if percent > 0 else 'down'} {abs(percent):.2f}%"


def extended_phrase(market: MarketSnapshot) -> str | None:
    ext = market.extended_hours
    if ext is None:
        return None
    return f"{ext.session.capitalize()}: " + _change_phrase(ext.price, ext.absolute_change, ext.percent_change, "regular-session close")


class EarningsMetric(Contract):
    metric: Literal["revenue", "eps"]
    kind: Literal["consensus", "actual", "guidance"]
    fiscal_year: int = Field(ge=2000, le=2200)
    fiscal_quarter: int = Field(ge=1, le=4)
    basis: Literal["GAAP", "non-GAAP", "unknown"]
    value: Decimal
    unit: Literal["USD", "USD/share"]
    supporting_quote: str = Field(min_length=1, max_length=600)


class OptionsImpliedMove(Contract):
    percent: Decimal = Field(gt=0, le=1000)
    earnings_date: date
    observed_date: date
    expiry: date | None = None
    methodology: str | None = Field(default=None, max_length=300)
    supporting_quote: str = Field(min_length=1, max_length=600)


class Finding(Contract):
    summary: str = Field(max_length=1000)
    url: str = Field(pattern=r"^https?://")
    reported_excerpt: str | None = Field(max_length=1200)
    published: str | None
    updated: str | None
    timestamp_basis: str | None
    content_kind: Literal["dated_announcement", "reporting", "mutable_page"]
    confirmation_status: Literal["confirmed", "reported", "unconfirmed"] = "reported"
    catalyst_type: CatalystType = "other"
    move_relevance: Literal["direct", "context"] = "context"
    event_date: date | None
    earnings_date: date | None = None
    story_key: str
    earnings_metrics: list[EarningsMetric] = Field(default_factory=list, max_length=8)
    options_implied_move: OptionsImpliedMove | None = None


class ResearchEvidence(Contract):
    query: str
    status: Literal["findings", "no_relevant_results", "evidence_unavailable"]
    findings: list[Finding] = Field(max_length=6)
    coverage_issues: list[str] = Field(max_length=6)

    @model_validator(mode="after")
    def consistent_status(self) -> ResearchEvidence:
        if bool(self.findings) != (self.status == "findings"):
            raise ValueError("Research status must agree with whether findings were supplied")
        return self


class Source(Contract):
    id: int
    url: str
    title: str
    publisher: str
    retrieved_at: AwareDatetime
    # Only material returned in tool metadata/annotations, never the agent's prose.
    raw_material: list[str]
    published: str | None = None
    updated: str | None = None
    eligible_at: AwareDatetime | None = None
    price_timing_unknown: bool = False
    unconfirmed_report: bool = False
    # "material_verified": the material-news agent opened the page and confirmed the date.
    timestamp_provenance: Literal["unavailable", "tool_metadata", "research_extraction", "provider_feed",
                                  "provider_structured", "material_verified"] = "unavailable"


class Claim(Contract):
    text: str = Field(min_length=1, max_length=900)
    sources: list[int] = Field(min_length=1, max_length=6)
    support_status: Literal["supported", "unsupported"] = "supported"

    def display_text(self, move: str | None = None, *, timing_uncertain: bool = False) -> str:
        content = self.text.replace("{move}", move) if move is not None else self.text
        if timing_uncertain:
            content += " (same-day report; exact publication time unknown)"
        if self.support_status == "unsupported":
            return "Unsupported claim — not substantiated by the cited sources: " + content
        return content


class Topic(Contract):
    heading: Claim
    sentences: list[Claim] = Field(min_length=1, max_length=4)


class Digest(Contract):
    headline: Claim
    earnings_preview: Topic | None = None
    topics: list[Topic] = Field(max_length=5)
    coverage_notes: list[str] = Field(max_length=6)

    def located_claims(self) -> dict[str, Claim]:
        claims = {"headline": self.headline}
        if self.earnings_preview is not None:
            claims["earnings_heading"] = self.earnings_preview.heading
            for index, sentence in enumerate(self.earnings_preview.sentences, 1):
                claims[f"earnings_sentence_{index}"] = sentence
        for index, topic in enumerate(self.topics, 1):
            claims[f"topic_{index}_heading"] = topic.heading
            for sentence_index, sentence in enumerate(topic.sentences, 1):
                claims[f"topic_{index}_sentence_{sentence_index}"] = sentence
        return claims


class WriterDigest(Digest):
    """The writer's output schema: at least one topic is required (strict output enforces it).

    Internal pruning uses Digest, which may keep only an earnings preview.
    """
    topics: list[Topic] = Field(min_length=1, max_length=5)


class VerificationIssue(Contract):
    category: Literal["support", "contradiction", "citation", "timing", "attribution", "uncertainty", "numbers", "recommendation", "style"]
    location: str = Field(pattern=r"^(headline|earnings_(heading|sentence_[1-4])|topic_[1-5]_(heading|sentence_[1-4])|coverage_note_[1-6]|overall)$")
    claim: str
    evidence: str
    correction: str


class VerificationResult(Contract):
    passed: bool
    issues: list[VerificationIssue]


class Publication(Contract):
    market: MarketSnapshot
    generated_at: AwareDatetime
    news_as_of: AwareDatetime | None = None
    digest: Digest | None
    sources: list[Source]
    news_source_ids: list[int] = Field(default_factory=list, max_length=6)
    # Reader-facing limitations; pipeline details go to diagnostics (printed with --debug).
    coverage: list[str]
    diagnostics: list[str] = Field(default_factory=list)
    # Host-computed earnings context (consensus, comparisons, implied move) for display.
    earnings: dict | None = None
