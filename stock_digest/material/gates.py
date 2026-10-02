"""Inclusion gates, event clustering and ranking. Pure functions only.

Models propose; these rules decide. Every published entry must pass the subject,
materiality, time and evidence gates here, and ranking never rescues an item that
fails one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..dates import is_date_only, normalize_source_timestamp
from ..manager import canonical_url as _canonical_url
from .models import (EVIDENCE_RANK, EXCLUDED_CATEGORIES, REPORTED_STATUSES, SIZE_GATED_CATEGORIES, Candidate,
                     Issuer, Mechanism, SourceRef, VerifiedEvent)

# Query parameters that only track a click; dropping them never changes the article.
TRACKING = re.compile(r"^(utm_|mc_|mkt_|ref$|referrer$|src$|cmpid$|ncid$|guccounter$|guce_|soc_|ocid$|taid$|"
                      r"cid$|smid$|sr_share$|.*clid$|__twitter|share$|via$)", re.I)
CLOCK_SKEW = timedelta(minutes=10)
MIN_RELATIVE_SIZE = 0.0001    # contracts/partnerships below 0.01% of market value are not material by size
# Bilateral deals count even when the counterparty leads the headline.
DEAL_CATEGORIES = {"partnership", "contract", "strategic_investment", "merger_acquisition", "financing"}
GENERIC = re.compile(
    r"\bai is growing\b|\breinforc\w* (?:its |the company's )?leadership\b|\binvestors? (?:may|might|could|will) react\b|"
    r"\b(?:investor|market) sentiment\b|\bstock (?:could|may|might) (?:rise|fall|move)\b|\bshares? (?:could|may|might) "
    r"(?:rise|fall|move|react)\b|\bpositive for the stock\b|\bgeneral (?:interest|attention)\b|\bbuzz\b|\bhype\b", re.I)
HEDGE = re.compile(r"\b(?:could|may|might|would|potential(?:ly)?|possible|if|pending|uncertain|not yet|unconfirmed|"
                   r"report(?:ed|edly|s)?|according to|people familiar|sources?|consider(?:s|ing|ed)?|talks?|"
                   r"plan(?:s|ned)?|seek(?:s|ing)?|expected|proposed|no (?:deal|decision|approval)|not (?:been|yet))\b|n't", re.I)
CONFIRMED = re.compile(r"\b(?:approved|has approved|signed|finali[sz]ed|completed|closed (?:the|its) (?:deal|acquisition)|"
                       r"won approval|granted|cleared)\b", re.I)
ATTRIBUTION = re.compile(r"\b(?:reportedly|according to|people familiar|sources? (?:said|say)|unconfirmed)\b", re.I)
# A named outlet doing the reporting: "Reuters reported", "The Financial Times reports".
OUTLET_REPORTED = re.compile(r"\b[A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*)*\s+report(?:ed|s)\b")


def canonical_url(value: str | None) -> str | None:
    """Canonical http(s) URL with tracking parameters, fragments and trailing slashes removed."""
    url = _canonical_url(value or "")
    if not url:
        return None
    parts = urlsplit(url)
    query = "&".join(pair for pair in parts.query.split("&") if pair and not TRACKING.match(pair.split("=", 1)[0]))
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme}://{host}{path}" + (f"?{query}" if query else "")


def publisher_of(url: str) -> str:
    host = urlsplit(url).hostname or "unknown"
    return host[4:] if host.startswith("www.") else host


def lineage(outlet: str | None, url: str) -> str:
    """Reporting lineage: the outlet that originated the story, else the publishing host."""
    value = (outlet or "").strip().lower()
    value = re.sub(r"^(?:the )|(?:\.com|\.net|\.org)$|[^a-z0-9]+", "", value)
    return value or re.sub(r"[^a-z0-9]+", "", publisher_of(url).rsplit(".", 1)[0])


# ---------------------------------------------------------------- time

@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tz: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


def make_window(run_at: datetime, hours: float, tz: str) -> Window:
    return Window(start=run_at - timedelta(hours=hours), end=run_at, tz=tz)


def placement(value: str | None, window: Window) -> str | None:
    """'inside', 'before', 'after', 'boundary' (date-only on a partial window day), or None (unparseable).

    A date-only value is compared by calendar day in the run timezone. The first and
    last days of the window are partial, so a date-only stamp on either is 'boundary'
    unless another source supplies a time.
    """
    normalized = normalize_source_timestamp(value)
    if not normalized:
        return None
    if is_date_only(normalized):
        day = date.fromisoformat(normalized)
        first, last = window.start.astimezone(window.zone).date(), window.end.astimezone(window.zone).date()
        if day < first:
            return "before"
        if day > last:
            return "after"
        if day == first and window.start.astimezone(window.zone).time() != datetime.min.time():
            return "boundary"
        return "inside"
    moment = datetime.fromisoformat(normalized)
    if moment < window.start:
        return "before"
    if moment > window.end + CLOCK_SKEW:
        return "after"
    return "inside"


def checked_date(value: str | None, basis: str | None, source: SourceRef | None) -> str | None:
    """Publication stamp: search-tool metadata first, then the model's stated date for this source.

    Datelines quoted in ``basis`` are preferred evidence but no longer required; a stated
    date must still be parseable and is still checked against the window.
    """
    if source and source.published:
        return source.published
    return normalize_source_timestamp(value)


def display_date(value: str, tz: str) -> str:
    normalized = normalize_source_timestamp(value) or value
    if is_date_only(normalized):
        return date.fromisoformat(normalized).strftime("%b %-d, %Y")
    return datetime.fromisoformat(normalized).astimezone(ZoneInfo(tz)).strftime("%b %-d, %Y")


def display_moment(moment: datetime, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%b %-d, %Y %H:%M %Z")


def sort_moment(value: str | None) -> float:
    normalized = normalize_source_timestamp(value)
    if not normalized:
        return 0.0
    if is_date_only(normalized):
        return datetime.fromisoformat(normalized + "T12:00:00+00:00").timestamp()
    return datetime.fromisoformat(normalized).timestamp()


# ---------------------------------------------------------------- gates

@dataclass
class Decision:
    passed: bool
    reason: str | None = None
    effective_date: str | None = None
    gate: str | None = None
    boundary: bool = False
    pending: bool = False       # only the publication time is unresolved; verification may settle it


def mechanism_reason(mechanism: Mechanism) -> str | None:
    """Reject empty or generic answers to 'it changes ___ through ___'."""
    changes, through = mechanism.changes.strip(), mechanism.through.strip()
    if not changes or len(through.split()) < 2:
        return "no concrete materiality mechanism"
    if GENERIC.search(changes) or GENERIC.search(through):
        return "generic materiality rationale"
    return None


def size_reason(category: str, amount_usd: float | None, issuer: Issuer) -> str | None:
    if (category in SIZE_GATED_CATEGORIES and amount_usd and amount_usd > 0 and issuer.market_cap
            and issuer.currency == "USD" and amount_usd / issuer.market_cap < MIN_RELATIVE_SIZE):
        return (f"${amount_usd:,.0f} is under {MIN_RELATIVE_SIZE:.1%} of the issuer's "
                f"${issuer.market_cap:,.0f} market value")
    return None


def time_gate(published: str | None, updated: str | None, new_fact: str | None,
              original: str | None, is_rehash: bool, window: Window) -> Decision:
    """Gate 3. Fresh publication, or a dated in-window update carrying a material new fact."""
    if is_rehash and not new_fact:
        return Decision(False, "repeats an older announcement without a material new fact", gate="time")
    if original and placement(original, window) == "before" and not new_fact:
        return Decision(False, "development predates the window and no material new fact was reported", gate="time")
    where = placement(published, window)
    if where == "inside":
        return Decision(True, effective_date=normalize_source_timestamp(published))
    if updated and new_fact and placement(updated, window) == "inside":
        return Decision(True, effective_date=normalize_source_timestamp(updated))
    if where == "boundary":
        # Date-only stamp on the window's first, partial day: accepted rather than dropped.
        return Decision(True, effective_date=normalize_source_timestamp(published), boundary=True)
    if where is None:
        return Decision(False, "publication date not shown by the evidence", gate="time", pending=True)
    if where == "after":
        return Decision(False, "publication date is after the run time", gate="time")
    return Decision(False, "published before the window (a refreshed page date does not make old news new)", gate="time")


def candidate_gate(candidate: Candidate, issuer: Issuer, window: Window, source: SourceRef | None) -> Decision:
    """All four gates on one discovery record."""
    if source is None:
        return Decision(False, "URL did not appear in the search tool's results", gate="evidence")
    if candidate.subject_role != "main" and not (candidate.subject_role == "secondary"
                                                 and candidate.category in DEAL_CATEGORIES):
        return Decision(False, f"company is not the main subject ({candidate.subject_role})", gate="subject")
    if candidate.category in EXCLUDED_CATEGORIES:
        return Decision(False, f"default exclusion ({candidate.category})", gate="materiality")
    if candidate.materiality == "none":
        return Decision(False, f"materiality assessed as {candidate.materiality}", gate="materiality")
    if reason := mechanism_reason(candidate.mechanism) or size_reason(candidate.category, candidate.amount_usd, issuer):
        return Decision(False, reason, gate="materiality")
    published = checked_date(candidate.published, candidate.timestamp_basis, source)
    if published is None and not candidate.likely_in_window:
        return Decision(False, "undated and appears to predate the window", gate="time")
    updated = checked_date(candidate.updated, candidate.timestamp_basis, None) if candidate.updated else None
    return time_gate(published, updated, candidate.new_fact, candidate.original_announcement_date,
                     candidate.is_rehash, window)


def event_gate(event: VerifiedEvent, issuer: Issuer, window: Window, published: str | None) -> Decision:
    """All four gates again on the verifier's reading of the clustered development."""
    if not event.verified:
        return Decision(False, event.failure_reason or "verifier could not substantiate the development", gate="evidence")
    if not event.company_is_main_subject:
        return Decision(False, "company is not the main subject on reading the source", gate="subject")
    if event.category in EXCLUDED_CATEGORIES:
        return Decision(False, f"default exclusion ({event.category})", gate="materiality")
    if reason := mechanism_reason(event.mechanism) or size_reason(event.category, event.amount_usd, issuer):
        return Decision(False, reason, gate="materiality")
    if not event.headline.strip() or not event.why.strip():
        return Decision(False, "verifier returned no entry text", gate="evidence")
    return time_gate(published, None, event.new_fact, event.original_announcement_date, event.is_rehash, window)


def status_language(text: str, status: str, publisher: str) -> tuple[str, str | None]:
    """Keep reported items reported. Returns (text, problem); problem means the wording claims an outcome."""
    if status not in REPORTED_STATUSES:
        return text, None
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if CONFIRMED.search(sentence) and not HEDGE.search(sentence):
            return text, f"reported development worded as a confirmed outcome: {sentence[:120]!r}"
    if not ATTRIBUTION.search(text) and not OUTLET_REPORTED.search(text):
        text = f"Reported by {publisher}: {text}"
    return text, None


# ---------------------------------------------------------------- clustering

def key_of(candidate: Candidate) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", candidate.development_key.lower()).strip("-")
    milestone = re.sub(r"[^a-z0-9]+", "-", candidate.milestone.lower()).strip("-")
    return f"{slug}|{milestone}" if slug else f"url:{canonical_url(candidate.url)}|{normalize_title(candidate.headline)}"


def normalize_title(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def group_candidates(candidates: list[Candidate]) -> list[list[int]]:
    """Deterministic first pass, transitively: same development key and milestone, or copies of one
    article (the same canonical URL and headline, e.g. tracking-parameter variants)."""
    parent = list(range(len(candidates)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first: dict = {}
    for index, candidate in enumerate(candidates):
        for link in (("key", key_of(candidate)),
                     ("copy", canonical_url(candidate.url), normalize_title(candidate.headline))):
            parent[find(index)] = find(first.setdefault(link, index))
    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def apply_merges(groups: list[list[int]], merges: list[list[int]]) -> tuple[list[list[int]], list[str]]:
    """Union groups the consolidator judged to be the same development. Invalid ids are ignored."""
    parent = list(range(len(groups)))
    notes = []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for ids in merges:
        valid = sorted({i for i in ids if isinstance(i, int) and 0 <= i < len(groups)})
        if len(valid) != len(set(ids)):
            notes.append(f"Consolidator referenced unknown group ids {sorted(set(ids) - set(valid))}; ignored.")
        for other in valid[1:]:
            parent[find(other)] = find(valid[0])
    merged: dict[int, list[int]] = {}
    for index, members in enumerate(groups):
        merged.setdefault(find(index), []).extend(members)
    return [sorted(members) for members in merged.values()], notes


@dataclass
class Cluster:
    event_id: str
    candidates: list[Candidate]
    decisions: list[Decision] = field(default_factory=list)

    def lineages(self) -> set[str]:
        """Independent reporting lineages; syndicated copies of one report count once."""
        return {lineage(c.original_outlet, c.url) for c in self.candidates}

    def best(self) -> Candidate:
        """Primary source: official/filing/regulator first, then original reporting, then earliest."""
        return max(self.candidates, key=lambda c: (EVIDENCE_RANK[c.source_type], c.source_type != "syndicated",
                                                   bool(c.published), -sort_moment(c.published)))

    def strength(self) -> tuple:
        """Verification order: in-window dated developments first, then by materiality and evidence."""
        levels = {"high": 2, "medium": 1}
        return (any(d.passed for d in self.decisions),
                max(levels.get(c.materiality, 0) for c in self.candidates),
                max(EVIDENCE_RANK[c.source_type] for c in self.candidates),
                max(sort_moment(c.published) for c in self.candidates))


def rank_key(event: VerifiedEvent, source_type: str, first_reported: str | None) -> list[float]:
    """Magnitude, directness, novelty, evidence quality and status, then recency."""
    evidence = EVIDENCE_RANK.get(source_type, 0) + (0 if event.status in REPORTED_STATUSES else 1)
    return [event.magnitude, event.directness, event.novelty, evidence, sort_moment(first_reported)]
