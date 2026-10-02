"""JSON view model for the browser UI.

All wording decisions (labels, substitutions, number formats) happen here so the
page only places text. Every string is cleaned of control characters, and only
http(s) URLs are passed through as links.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

from .dates import NY, display_timestamp
from .models import Claim, Publication, move_phrase, rounded
from .render import FOOTER, clean

RUMOR_LABEL = re.compile(r"\bunconfirmed\b|\breportedly\b|\breported talks\b", re.I)
COMPLETED = "completed regular-session close"


def _url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    return value if parts.scheme in {"http", "https"} and parts.hostname else None


def _direction(value: Decimal) -> str:
    value = rounded(value)
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _signed_money(value: Decimal) -> str:
    value = rounded(value)
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _percent(value: Decimal, places: str = "0.01") -> str:
    digits = len(places.split(".")[1])
    return f"{rounded(value, places):+.{digits}f}%"


def _compact(value: Decimal) -> str:
    for size, suffix in ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"), (Decimal(10) ** 3, "K")):
        if abs(value) >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


def _time(value) -> str:
    return value.astimezone(NY).strftime("%-I:%M %p ET")


def _earnings(context: dict | None) -> dict | None:
    """Deterministic figures for the earnings section, straight from the host calculations."""
    if not context:
        return None
    stats = []
    metrics = context.get("metrics") or {}
    for name, label in (("eps", "EPS estimate"), ("revenue", "Revenue estimate")):
        entry = metrics.get(name) or {}
        estimate = entry.get("estimate")
        if not estimate:
            continue
        value = str(estimate.get("display") or estimate.get("value"))
        value = value.replace(" trillion", "T").replace(" billion", "B").replace(" million", "M")
        analysts = estimate.get("analysts")
        yoy = (entry.get("comparisons") or {}).get("YoY") or {}
        detail = []
        if analysts:
            detail.append(f"{analysts} analysts")
        if yoy.get("percent_change") is not None:
            detail.append(f"{float(yoy['percent_change']):+.1f}% vs. year ago")
        stats.append({"label": label, "value": clean(value), "detail": clean(" · ".join(detail))})
    options = context.get("options_implied_move")
    if options:
        detail = f"through {date.fromisoformat(options['expiry']):%b %-d}" if options.get("expiry") else ""
        stats.append({"label": "Options-implied move", "value": f"±{float(options['percent']):.1f}%",
                      "detail": clean(detail)})
    status = context.get("event_status", "")
    timing = {"before_open": "before the open", "after_close": "after the close"}.get(context.get("event_timing"), "")
    return {"date": context.get("event_date"), "status": status,
            "timing": timing, "stats": stats}


def publication_view(publication: Publication, trace_url: str | None = None) -> dict:
    market = publication.market
    completed = market.price_type == COMPLETED
    uncertain = {source.id for source in publication.sources if source.price_timing_unknown}
    unconfirmed = {source.id: source.publisher for source in publication.sources if source.unconfirmed_report}

    def claim(value: Claim) -> dict:
        flags = []
        if value.support_status == "unsupported":
            flags.append({"kind": "unsupported", "label": "Unverified",
                          "detail": "Not fully substantiated by the cited sources; the citations give context only."})
        if uncertain.intersection(value.sources):
            flags.append({"kind": "timing", "label": "Time unknown",
                          "detail": "Published the same day at an unknown time, so it may not precede the price move."})
        if not RUMOR_LABEL.search(value.text):
            publishers = list(dict.fromkeys(unconfirmed[ref] for ref in value.sources if ref in unconfirmed))
            if publishers:
                flags.append({"kind": "unconfirmed", "label": "Unconfirmed report",
                              "detail": "Reported by " + ", ".join(publishers) + "; not confirmed by the company."})
        return {"text": clean(value.text.replace("{move}", move_phrase(market))),
                "sources": list(dict.fromkeys(value.sources)), "flags": flags}

    def section(topic, kind: str) -> dict:
        heading = claim(topic.heading)
        # Headings read like titles: no citation chips; only an unsupported heading keeps its label.
        heading["flags"] = [flag for flag in heading["flags"] if flag["kind"] == "unsupported"]
        heading["sources"] = []
        sentences, seen = [], set()
        for sentence in topic.sentences:
            item = claim(sentence)
            item["flags"] = [flag for flag in item["flags"] if flag["kind"] not in seen]
            seen.update(flag["kind"] for flag in item["flags"])
            sentences.append(item)
        return {"kind": kind, "heading": heading, "sentences": sentences}

    digest = publication.digest
    sections = []
    if digest is not None:
        sections = [section(topic, "topic") for topic in digest.topics]
        if digest.earnings_preview is not None:
            sections.append(section(digest.earnings_preview, "earnings"))

    extended = None
    if market.extended_hours is not None:
        ext = market.extended_hours
        extended = {"label": "After hours" if ext.session == "after-hours" else "Pre-market",
                    "price": f"${rounded(ext.price):,.2f}", "change": _signed_money(ext.absolute_change),
                    "percent": _percent(ext.percent_change), "direction": _direction(ext.percent_change),
                    "time": _time(ext.observed_at)}

    stats = [{"label": "Previous close", "value": f"${rounded(market.comparison_close):,.2f}",
              "detail": market.comparison_date.strftime("%b %-d")}]
    if market.session_open_price is not None:
        stats.append({"label": "Open", "value": f"${rounded(market.session_open_price):,.2f}",
                      "detail": f"gap {_percent(market.gap_percent, '0.1')}"})
        stats.append({"label": "Since open", "value": _percent(market.since_open_percent, "0.1"), "detail": ""})
    if market.volume is not None:
        stats.append({"label": "Volume" + ("" if completed else " so far"), "value": _compact(market.volume),
                      "detail": f"{market.relative_volume:.2f}× 20-day avg" if market.relative_volume is not None else ""})
    benchmarks = [{"name": clean(b.name), "role": b.role, "percent": _percent(b.percent_change, "0.1"),
                   "direction": _direction(b.percent_change)} for b in market.benchmarks]

    by_id = {source.id: source for source in publication.sources}
    headlines = []
    for ref in publication.news_source_ids:
        source = by_id[ref]
        headlines.append({"id": ref, "title": clean(source.title), "publisher": clean(source.publisher),
                          "published": clean(display_timestamp(source.published or source.updated) or ""),
                          "url": _url(source.url), "time_unknown": source.price_timing_unknown,
                          "unconfirmed": source.unconfirmed_report})
    sources = [{"id": 1, "publisher": "Yahoo Finance", "title": "Market data: prices, volume, benchmarks",
                "published": _time(market.observed_at), "url": _url(market.provenance[0]) if market.provenance else None}]
    for source in publication.sources:
        sources.append({"id": source.id, "publisher": clean(source.publisher),
                        "title": clean(source.title) if source.title != source.url else "",
                        "published": clean(display_timestamp(source.updated or source.published) or ""),
                        "url": _url(source.url)})

    notice = None
    if digest is None:
        notice = next((note for note in publication.coverage if "narrative" in note.lower() or "news/context" in note.lower()
                       or "no relevant findings" in note.lower()), None)
        notice = clean(notice or ("Only retrieved headlines are available." if headlines else "Price-only result."))
    return {
        "ticker": market.ticker, "company": clean(market.company),
        "price": f"${rounded(market.price):,.2f}", "change": _signed_money(market.absolute_change),
        "percent": _percent(market.percent_change), "direction": _direction(market.percent_change),
        "session": ("At close · " + market.session_date.strftime("%a %b %-d")) if completed
                   else f"As of {_time(market.observed_at)}",
        "extended": extended, "stats": stats, "benchmarks": benchmarks,
        "updated_at": (publication.news_as_of or publication.generated_at).isoformat(),
        "headline": claim(digest.headline) if digest is not None else None,
        "sections": sections, "earnings": _earnings(publication.earnings) if digest is not None else None,
        "notice": notice, "headlines": headlines, "sources": sources,
        "coverage": [clean(note) for note in publication.coverage],
        "timestamps": {"price_observed": _time(market.observed_at) + " " + market.session_date.strftime("%b %-d"),
                       "news_checked": _time(publication.news_as_of) if publication.news_as_of else None,
                       "generated": _time(publication.generated_at)},
        "disclosure": FOOTER, "trace_url": _url(trace_url),
    }
