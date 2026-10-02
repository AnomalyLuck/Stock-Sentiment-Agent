"""Source-date parsing and eligibility bounds.

Pure functions only. Nothing here invents a publication time or timezone: a
date-only value stays date-only, and a clock time is trusted only when the
quoted dateline shows it together with a matching timezone.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_DASHES = str.maketrans({"‑": "-", "–": "-", "−": "-", "—": "-"})
_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
            "%d %B %Y", "%d %b %Y", "%d-%b-%Y", "%d-%B-%Y")
# Offsets (hours) each dateline zone label can denote.
_ZONES = {
    "et": {-4, -5}, "edt": {-4}, "est": {-5}, "eastern": {-4, -5}, "eastern daylight": {-4},
    "eastern standard": {-5}, "ct": {-5, -6}, "cdt": {-5}, "cst": {-6}, "central": {-5, -6},
    "mt": {-6, -7}, "mdt": {-6}, "mst": {-7}, "pt": {-7, -8}, "pdt": {-7}, "pst": {-8},
    "pacific": {-7, -8}, "utc": {0}, "gmt": {0}, "z": {0},
}
_ZONE_RE = re.compile(
    r"\b(eastern daylight|eastern standard|eastern|central|pacific|e[sd]?t|c[sd]?t|m[sd]?t|p[sd]?t|utc|gmt|z)\b"
    r"|(?<![\d:])([+-])(\d{2}):?(\d{2})\b"
)
_RELATIVE = re.compile(r"\b(\d+|an?|one)\s*(minute|min|hour|hr|day)s?\s+ago\b")


def is_date_only(value: str | None) -> bool:
    return bool(value and _DATE_ONLY.fullmatch(value))


def normalize_source_timestamp(value: str | None) -> str | None:
    """Return YYYY-MM-DD or an offset-aware ISO datetime; never guess a missing zone."""
    if not value or not isinstance(value, str):
        return None
    value = " ".join(value.translate(_DASHES).split())
    date_text = re.sub(r"\bSept\.?\b", "Sep", value, flags=re.I)
    date_text = re.sub(r"\b([A-Za-z]{3})\.", r"\1", date_text)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(date_text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.isoformat() if stamp.tzinfo is not None else None


def timestamp_bound(value: str | None) -> datetime | None:
    """Latest possible publication time; date-only values cover every civil timezone."""
    value = normalize_source_timestamp(value)
    if value is None:
        return None
    try:
        if is_date_only(value):
            # UTC-12 is the last civil timezone to finish a calendar day.
            return datetime.combine(date.fromisoformat(value) + timedelta(days=1), time(12), UTC)
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def earliest_bound(value: str | None) -> datetime | None:
    """Earliest possible publication time; UTC+14 is the first civil timezone to start a day."""
    value = normalize_source_timestamp(value)
    if value is None:
        return None
    try:
        if is_date_only(value):
            return datetime.combine(date.fromisoformat(value), time(), UTC) - timedelta(hours=14)
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def availability_bound(value: str | None, retrieved_at: datetime) -> datetime | None:
    """Cap a date-only bound at observed search retrieval, not an invented time."""
    normalized = normalize_source_timestamp(value)
    bound = timestamp_bound(normalized)
    if bound is None or not is_date_only(normalized):
        return bound
    # Dates in the future even in the earliest civil timezone keep their (future) bound.
    return min(bound, retrieved_at) if earliest_bound(normalized) <= retrieved_at else bound


def _implied_date(month: int, day: int, reference: datetime) -> date | None:
    """The most recent month/day on or before the reference day (plus one for timezones)."""
    latest = reference.astimezone(NY).date() + timedelta(days=1)
    for year in (latest.year, latest.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= latest:
            return candidate
    return None


def _relative_offsets(text: str):
    if re.search(r"\b(?:today|just now)\b", text):
        yield timedelta()
    if re.search(r"\byesterday\b", text):
        yield timedelta(days=1)
    for amount, unit in _RELATIVE.findall(text):
        count = 1 if amount in {"a", "an", "one"} else int(amount)
        if unit.startswith("day"):
            if count <= 7:
                yield timedelta(days=count)
        elif unit.startswith("h"):
            if count <= 72:
                yield timedelta(hours=count)
        elif count <= 600:
            yield timedelta(minutes=count)


def date_in_passage(value: str | None, passage: str | None, reference: datetime | None = None) -> bool:
    """Check that the quoted dateline actually shows the claimed date.

    Accepts full dates in common formats. With a retrieval ``reference`` it also
    accepts a year-less "Sep 28" (resolved to its most recent occurrence) and
    relative stamps such as "3 hours ago" or "yesterday". This is a consistency
    check on the extraction, not independent retrieval of the source date.
    """
    if not value or not passage or timestamp_bound(value) is None:
        return False
    normalized = normalize_source_timestamp(value)
    try:
        day = date.fromisoformat(normalized[:10])
    except ValueError:
        return False
    text = " ".join(passage.lower().translate(_DASHES).split())
    if normalized[:10] in text or day.strftime("%Y/%m/%d") in text:
        return True
    if re.search(rf"\b0?{day.month}/0?{day.day}/(?:{day.year}|{day.year % 100:02d})\b", text):
        return True
    abbreviation = "sept?" if day.month == 9 else day.strftime("%b").lower()
    month = rf"(?:{day.strftime('%B').lower()}|{abbreviation}\.?)"
    suffix = r"(?:st|nd|rd|th)?"
    if (re.search(rf"\b{month}\s+0?{day.day}{suffix},?\s+{day.year}\b", text)
            or re.search(rf"\b0?{day.day}{suffix}[\s-]+{month}[,\s-]+{day.year}\b", text)):
        return True
    if reference is None:
        return False
    no_year = r"(?!,?\s*\d{4})"
    if (re.search(rf"\b{month}\s+0?{day.day}{suffix}\b{no_year}", text)
            or re.search(rf"\b0?{day.day}{suffix}\s+{month}\b{no_year}", text)):
        if _implied_date(day.month, day.day, reference) == day:
            return True
    for offset in _relative_offsets(text):
        moment = reference - offset
        if day in {moment.astimezone(UTC).date(), moment.astimezone(NY).date()}:
            return True
    return False


def time_in_passage(value: str | None, passage: str | None) -> bool:
    """True only when the dateline shows the same clock time and a compatible timezone."""
    normalized = normalize_source_timestamp(value)
    if not normalized or is_date_only(normalized) or not passage:
        return False
    stamp = datetime.fromisoformat(normalized)
    text = " ".join(passage.lower().split())
    hour, minute = stamp.hour, stamp.minute
    h12, meridiem = hour % 12 or 12, "a" if hour < 12 else "p"
    clock = (re.search(rf"\b0?{hour}:{minute:02d}(?::\d{{2}})?(?!\s*[ap]\.?m)\b", text)
             or re.search(rf"\b0?{h12}:{minute:02d}(?::\d{{2}})?\s*{meridiem}\.?m\b", text)
             or (minute == 0 and re.search(rf"\b{h12}\s*{meridiem}\.?m\b", text)))
    if not clock:
        return False
    offset_hours = stamp.utcoffset().total_seconds() / 3600
    for match in _ZONE_RE.finditer(text):
        label, sign, hh, mm = match.groups()
        if label and offset_hours in _ZONES[label]:
            return True
        if sign and (1 if sign == "+" else -1) * (int(hh) + int(mm) / 60) == offset_hours:
            return True
    return False


def display_timestamp(value: str | None) -> str | None:
    """Readable date or New York time for terminal output; raw text if unparseable."""
    if not value:
        return None
    normalized = normalize_source_timestamp(value)
    if normalized is None:
        return value
    if is_date_only(normalized):
        return normalized
    return datetime.fromisoformat(normalized).astimezone(NY).strftime("%Y-%m-%d %H:%M %Z")


def relative_age(moment: datetime, now: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"
