from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime

from rich.console import Console
from rich.text import Text

from .dates import NY, display_timestamp, relative_age
from .models import Claim, Publication, extended_phrase, move_phrase, price_phrase, rounded

FOOTER = "AI-generated; may contain errors. Informational only—not investment advice or a research report."
# Strip CSI, OSC (including hyperlink escapes), and other escape sequences first.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
DATA_NOTE = ("Prices: Yahoo Finance regular session with a 15-minute app cutoff; extended-hours quotes are "
             "shown separately. Headlines and summaries are provider or search content, not full-article checks.")
MAX_COVERAGE_NOTES = 5  # the data note plus the four highest-priority run notes


def clean(value: str) -> str:
    value = ESCAPES.sub("", value)
    return " ".join("".join(
        c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\t"
    ).split())


def stamp(value: datetime, now: datetime | None = None) -> str:
    text = value.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S %Z") + " (America/New_York)"
    return text + (f" · {relative_age(value, now)}" if now is not None else "")


def _percent(value) -> str:
    return f"{rounded(value, '0.1'):+.1f}%"


def render(publication: Publication, *, color: bool, width: int, show_footer: bool = True) -> str:
    """Build the entire validated publication before any stdout write."""
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=color, color_system="standard" if color else None,
                      no_color=not color, width=width, highlight=False, markup=False)
    market = publication.market
    now = publication.generated_at
    uncertain_ids = {source.id for source in publication.sources if source.price_timing_unknown}
    unconfirmed_sources = {source.id: source.publisher for source in publication.sources if source.unconfirmed_report}

    def line(value="", style=None):
        console.print(Text(clean(value), style=style if color else None), overflow="fold")

    def claim(value: Claim, move=False) -> str:
        content = value.display_text(move_phrase(market) if move else None,
                                     timing_uncertain=bool(uncertain_ids.intersection(value.sources)))
        if not re.search(r"\bunconfirmed\b|\breportedly\b|\breported talks\b", value.text, re.I):
            publishers = list(dict.fromkeys(unconfirmed_sources[ref] for ref in value.sources if ref in unconfirmed_sources))
            if publishers:
                content = "Unconfirmed report from " + ", ".join(publishers) + ": " + content
        return clean(content) + " " + "".join(f"[{ref}]" for ref in dict.fromkeys(value.sources))

    line(f"{market.ticker} — {market.company}", "bold")
    line(price_phrase(market) + " [1]", "green" if market.absolute_change >= 0 else "red")
    if market.extended_hours is not None:
        ext = market.extended_hours
        line(f"{extended_phrase(market)} · {stamp(ext.observed_at, now)} [1]",
             "green" if ext.absolute_change >= 0 else "red")
    line(f"{market.exchange} · {market.currency} · {market.price_type} · Session: {market.session_date}")
    session_facts = []
    if market.session_open_price is not None:
        session_facts.append(f"Open ${market.session_open_price:,.2f} (gap {_percent(market.gap_percent)}; "
                             f"{_percent(market.since_open_percent)} since open)")
    if market.volume is not None:
        volume = f"Volume {market.volume:,.0f}"
        if market.relative_volume is not None:
            so_far = " so far" if market.price_type != "completed regular-session close" else ""
            volume += f" ({market.relative_volume:.2f}× 20-session average{so_far})"
        session_facts.append(volume)
    if session_facts:
        line(" · ".join(session_facts) + " [1]")
    if market.benchmarks:
        line("Same session: " + " · ".join(f"{b.name} {_percent(b.percent_change)}" for b in market.benchmarks) + " [1]")
    line(f"As of: {stamp(market.as_of, now)}")
    if publication.news_as_of is not None:
        line(f"News checked through: {stamp(publication.news_as_of, now)}")
    line(f"Price observed: {stamp(market.observed_at, now)} (bar/session end)")
    line(f"Generated: {stamp(publication.generated_at)}")
    line(f"Comparison close: ${market.comparison_close:,.4f} on {market.comparison_date} [1]")
    by_id = {source.id: source for source in publication.sources}
    if publication.news_source_ids:
        line()
        line("Recent headlines", "bold dark_orange")
        for ref in publication.news_source_ids:
            source = by_id[ref]
            published = display_timestamp(source.published or source.updated)
            timing = " · exact time unknown" if source.price_timing_unknown else ""
            status = "Unconfirmed report · " if source.unconfirmed_report else ""
            line(f"{published or 'Publication date unavailable'} · {status}{source.publisher}: {source.title}{timing} [{ref}]")
    line()
    if publication.digest is None:
        if not publication.news_source_ids:
            line("Price-only result", "bold dark_orange")
    else:
        line(claim(publication.digest.headline, move=True), "bold dark_orange")
        topics = list(publication.digest.topics)
        if publication.digest.earnings_preview:
            topics.append(publication.digest.earnings_preview)
        for topic in topics:
            line()
            line(claim(topic.heading), "bold dark_orange")
            line(" ".join(claim(sentence) for sentence in topic.sentences))
    line()
    line("Sources", "bold")
    urls = " · ".join(clean(reference) for reference in market.provenance)
    console.print(Text(f"[1] Yahoo Finance market data · {urls}"), soft_wrap=True)
    used = set(publication.news_source_ids)
    if publication.digest:
        claims = publication.digest.located_claims().values()
        used.update(ref for item in claims for ref in item.sources)
    for source in publication.sources:
        if source.id not in used:
            continue
        parts = [source.publisher]
        if source.title and source.title != source.url:
            parts.append(f"“{source.title}”")
        published = display_timestamp(source.updated or source.published)
        if published:
            parts.append(published)
        parts.append(source.url)
        console.print(Text(f"[{source.id}] " + " · ".join(clean(part) for part in parts)), soft_wrap=True)
    if show_footer:
        line()
        notes = [DATA_NOTE] + publication.coverage[:MAX_COVERAGE_NOTES - 1]
        line("Coverage: " + " ".join(notes))
        line(FOOTER, "dim")
    return buffer.getvalue()
