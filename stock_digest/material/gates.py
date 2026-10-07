"""Material-news checks and date helpers. Pure functions only.

The screen proposes developments; `news.screen_gate` keeps only those citing supplied articles,
and `status_language` keeps reported developments worded as reports.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..dates import is_date_only, normalize_source_timestamp
from .models import REPORTED_STATUSES

HEDGE = re.compile(r"\b(?:could|may|might|would|potential(?:ly)?|possible|if|pending|uncertain|not yet|unconfirmed|"
                   r"report(?:ed|edly|s)?|according to|people familiar|sources?|consider(?:s|ing|ed)?|talks?|"
                   r"plan(?:s|ned)?|seek(?:s|ing)?|expected|proposed|no (?:deal|decision|approval)|not (?:been|yet))\b|n't", re.I)
CONFIRMED = re.compile(r"\b(?:approved|has approved|signed|finali[sz]ed|completed|closed (?:the|its) (?:deal|acquisition)|"
                       r"won approval|granted|cleared)\b", re.I)
ATTRIBUTION = re.compile(r"\b(?:reportedly|according to|people familiar|sources? (?:said|say)|unconfirmed)\b", re.I)
# A named outlet doing the reporting: "Reuters reported", "The Financial Times reports".
OUTLET_REPORTED = re.compile(r"\b[A-Z][\w&.'’-]*(?:\s+[A-Z][\w&.'’-]*)*\s+report(?:ed|s)\b")


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


def display_date(value: str, tz: str) -> str:
    normalized = normalize_source_timestamp(value) or value
    if is_date_only(normalized):
        return date.fromisoformat(normalized).strftime("%b %-d, %Y")
    return datetime.fromisoformat(normalized).astimezone(ZoneInfo(tz)).strftime("%b %-d, %Y")


def display_moment(moment: datetime, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%b %-d, %Y %H:%M %Z")


# ---------------------------------------------------------------- wording

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
