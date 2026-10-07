"""The digest in the output contract's Markdown, and the same content for the browser UI."""
from __future__ import annotations

from ..render import clean
from .gates import display_moment
from .models import MaterialResult


def empty_message(ticker: str) -> str:
    return f"No qualifying material, company-led developments were found for {ticker} in the specified window."


def window_text(result: MaterialResult) -> str:
    return (f"{display_moment(result.window_start, result.timezone)} to "
            f"{display_moment(result.window_end, result.timezone)}, {result.timezone}")


def limit_note(result: MaterialResult) -> str | None:
    if result.max_results is None or result.qualified_count <= result.max_results:
        return None
    return (f"Limited to the {result.max_results} most recent of {result.qualified_count} qualifying developments, "
            "as requested.")


def _cell(text: str) -> str:
    return clean(text).replace("|", "\\|")


def _link_text(text: str) -> str:
    return _cell(text).replace("[", "(").replace("]", ")")


def markdown(result: MaterialResult) -> str:
    ticker = result.issuer.ticker
    lines = [f"**{ticker} — material company developments**", "",
             f"**Window:** {window_text(result)}. One entry per distinct development; "
             "dates below are announcement or reporting dates. Screened from headlines; "
             "the articles were not opened."]
    if note := limit_note(result):
        lines += ["", note]
    lines.append("")
    if not result.entries:
        lines.append(f"> {empty_message(ticker)}")
    else:
        lines += [f"| Date | Development | Why it could matter to {ticker} |", "|---|---|---|"]
        for entry in result.entries:
            headline = entry.headline + (" — reported" if entry.reported else "")
            links = " ".join(f"[{_link_text(s['publisher'])}]({s['url'].replace(' ', '%20').replace(')', '%29')})"
                             for s in entry.sources)
            lines.append(f"| {entry.date} | **{_cell(headline)}** | {_cell(entry.why)} "
                         f"*Status: {entry.status_label}.* {links} |")
    if result.coverage_note:
        lines += ["", f"*Coverage note: {_cell(result.coverage_note)}*"]
    return "\n".join(lines) + "\n"


def material_view(result: MaterialResult, trace_url: str | None = None) -> dict:
    """JSON for the UI. Untrusted text stays text; the page inserts it with textContent."""
    issuer = result.issuer
    return {
        "ticker": issuer.ticker, "company": issuer.company, "exchange": issuer.exchange,
        "window": window_text(result), "timezone": result.timezone, "hours": result.hours,
        "entries": [{
            "date": e.date, "headline": e.headline, "reported": e.reported, "status": e.status_label,
            "category": e.category.replace("_", " "), "why": e.why, "verification": e.verification,
            "sources": [{"url": s["url"], "publisher": s["publisher"], "title": s.get("title")} for s in e.sources],
        } for e in result.entries],
        "empty_message": None if result.entries else empty_message(issuer.ticker),
        "limit_note": limit_note(result),
        "coverage_note": result.coverage_note,
        "markdown": markdown(result),
        "generated": display_moment(result.run_at, result.timezone),
        "trace_url": trace_url,
    }
