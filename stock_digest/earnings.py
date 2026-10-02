"""Prepare earnings comparisons with Decimal arithmetic.

Structured Yahoo Finance consensus, history and option quotes are preferred.
Cited research extractions are a fallback, restricted to the quarter being
reported and sanity-checked for magnitude.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from .dates import NY

MIN_REVENUE = Decimal(1_000_000)  # A quarterly revenue below $1M is almost surely a unit error.
EVENT_RANK = {"confirmed": 0, "scheduled": 1, "reported": 2, "estimated": 3}


def upcoming_earnings(packets: list[dict], market, extra_events: list[dict] = ()) -> dict | None:
    """Earliest-ranked earnings release within 30 days.

    Issuer announcements outrank Yahoo's calendar, which outranks other reporting;
    an estimated calendar date ranks last.
    """
    today = market.as_of.astimezone(NY).date()
    events = []
    for packet in packets:
        day = packet.get("earnings_date")
        # General event dates can be dividends/keynotes mentioned in earnings reports.
        # Only the explicit earnings_date field may trigger earnings research.
        if day and today <= date.fromisoformat(day) <= today + timedelta(days=30):
            confirmed = packet["content_kind"] == "dated_announcement" and packet["confirmation_status"] == "confirmed"
            events.append({**packet, "event_date": day, "event_status": "confirmed" if confirmed else "reported",
                           "event_timing": "unknown"})
    for event in extra_events:
        if today <= date.fromisoformat(event["event_date"]) <= today + timedelta(days=30):
            events.append(event)
    if not events:
        return None
    return min(events, key=lambda item: (EVENT_RANK[item["event_status"]], item["event_date"]))


def _next_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter + 1) if quarter < 4 else (year + 1, 1)


def display_amount(value: Decimal, name: str) -> str:
    """Rounded reader-facing figure; comparisons are computed from the unrounded values."""
    if name == "eps":
        return f"${value.quantize(Decimal('0.01')):,.2f}"
    for size, suffix in ((Decimal(10) ** 12, "trillion"), (Decimal(10) ** 9, "billion"), (Decimal(10) ** 6, "million")):
        if abs(value) >= size:
            return f"${(value / size).quantize(Decimal('0.01')):,.2f} {suffix}"
    return f"${value:,.0f}"


def _compare(expected: Decimal, baseline: Decimal) -> dict:
    result = {"absolute_change": str(expected - baseline), "percent_change": None, "unavailable_reason": None}
    if baseline <= 0:
        result["unavailable_reason"] = "Percentage growth not meaningful from a zero/negative baseline; show dollar change."
    else:
        result["percent_change"] = str(((expected / baseline - 1) * 100).quantize(Decimal("0.1")))
    return result


def _structured_metrics(structured: dict, sector_key: str | None = None) -> dict | None:
    """Consensus, year-ago and last-quarter actuals, all on Yahoo's own period alignment."""
    estimates = structured.get("estimates") or {}
    analysis_id, financials_id = structured["analysis_source_id"], structured.get("financials_source_id")
    last_quarter = estimates.get("last_quarter") or {}
    period = (f"the quarter after the period ended {last_quarter['period_end']}"
              if last_quarter.get("period_end") else "Yahoo Finance's current (unreported) quarter")
    metrics = {}
    for name, block, year_ago_key in (("eps", estimates.get("eps"), "yearAgoEps"),
                                      ("revenue", estimates.get("revenue"), "yearAgoRevenue")):
        entry = {"estimate": None, "comparisons": {}, "missing_reason": None}
        if not block or block.get("avg") is None:
            entry["missing_reason"] = "Yahoo Finance returned no consensus estimate for this quarter."
            metrics[name] = entry
            continue
        value = Decimal(str(block["avg"]))
        entry["estimate"] = {
            "metric": name, "kind": "consensus", "value": str(value), "display": display_amount(value, name),
            "low": block.get("low"), "high": block.get("high"),
            "analysts": int(block["numberOfAnalysts"]) if block.get("numberOfAnalysts") else None,
            "unit": "USD/share" if name == "eps" else "USD", "period": period, "source_id": analysis_id,
            "basis": ("Yahoo Finance consensus EPS; estimates and actuals share Yahoo's reported (typically adjusted) basis"
                      if name == "eps" else "Yahoo Finance consensus revenue"),
        }
        baselines = {}
        if block.get(year_ago_key) is not None:
            baselines["YoY"] = {"value": str(Decimal(str(block[year_ago_key]))), "period": "same quarter a year earlier",
                                "source_id": analysis_id}
        if name == "eps" and last_quarter.get("eps_actual") is not None:
            baselines["QoQ"] = {"value": str(Decimal(str(last_quarter["eps_actual"]))),
                                "period": f"quarter ended {last_quarter['period_end']}", "source_id": analysis_id}
        if name == "revenue" and financials_id is not None:
            history = structured.get("revenue_history") or []
            latest = next((row for row in history if not last_quarter.get("period_end")
                           or row["period_end"][:7] == last_quarter["period_end"][:7]), None)
            if latest:
                baselines["QoQ"] = {"value": str(Decimal(str(latest["revenue"]))),
                                    "period": f"quarter ended {latest['period_end']} (reported total revenue)",
                                    "source_id": financials_id}
        for label in ("QoQ", "YoY"):
            baseline = baselines.get(label)
            comparison = {"baseline": baseline, "percent_change": None, "absolute_change": None,
                          "unavailable_reason": None, "comparability_note": None}
            if baseline is None:
                comparison["unavailable_reason"] = "Comparable prior-period actual unavailable."
            else:
                baseline["display"] = display_amount(Decimal(baseline["value"]), name)
                comparison.update(_compare(value, Decimal(baseline["value"])))
                if name == "revenue" and label == "QoQ" and sector_key == "financial-services":
                    # Bank and insurer consensus revenue often uses a managed/net definition.
                    comparison["comparability_note"] = (
                        "Consensus revenue is compared with Yahoo's reported total revenue; for financial companies "
                        "the two definitions can differ. Label this growth claim unsupported and disclose that.")
            entry["comparisons"][label] = comparison
        metrics[name] = entry
    if all(entry["estimate"] is None for entry in metrics.values()):
        return None
    return metrics


def _research_metrics(packets: list[dict], registry: dict, event: dict, structured_revenue: Decimal | None) -> dict:
    records = []
    for packet in packets:
        if packet.get("research_purpose") not in {"earnings_estimates", "earnings_history"}:
            continue
        for metric in packet.get("earnings_metrics", []):
            if metric["unit"] != ("USD" if metric["metric"] == "revenue" else "USD/share"):
                continue
            value = Decimal(metric["value"])
            if metric["metric"] == "revenue":
                # Reject unit errors ("46.7" meaning billions) and wildly inconsistent figures.
                if value < MIN_REVENUE:
                    continue
                if structured_revenue and not structured_revenue / 10 <= value <= structured_revenue * 10:
                    continue
            records.append({**metric, "source_id": packet["source_id"], "for_event": packet.get("earnings_date") == event["event_date"],
                            "source_date": registry[packet["source_id"]].updated or registry[packet["source_id"]].published})
    records.sort(key=lambda item: registry[item["source_id"]].eligible_at, reverse=True)
    actuals = [item for item in records if item["kind"] == "actual"]
    # The quarter being reported follows the latest reported actual.
    expected = _next_quarter(*max((item["fiscal_year"], item["fiscal_quarter"]) for item in actuals)) if actuals else None
    estimates = [item for item in records if item["kind"] == "consensus"
                 and (expected is None or (item["fiscal_year"], item["fiscal_quarter"]) == expected)]
    if expected is None:
        # Without history, trust only estimates the source ties to this release date.
        estimates = [item for item in estimates if item["for_event"]]
    target = (estimates[0]["fiscal_year"], estimates[0]["fiscal_quarter"]) if estimates else None
    metrics = {}
    for name in ("revenue", "eps"):
        estimate = next((item for item in estimates if item["metric"] == name and
                         (item["fiscal_year"], item["fiscal_quarter"]) == target), None)
        entry = {"estimate": estimate, "comparisons": {}, "missing_reason": None}
        if estimate is None:
            entry["missing_reason"] = "No eligible consensus estimate found for the quarter being reported."
            metrics[name] = entry
            continue
        year, quarter = target
        periods = {"QoQ": (year, quarter - 1) if quarter > 1 else (year - 1, 4), "YoY": (year - 1, quarter)}
        for label, period in periods.items():
            baseline = next((item for item in actuals if item["metric"] == name and
                             (item["basis"] == estimate["basis"] or
                              (name == "revenue" and {item["basis"], estimate["basis"]} <= {"GAAP", "unknown"})) and
                             (item["fiscal_year"], item["fiscal_quarter"]) == period), None)
            comparison = {"baseline": baseline, "percent_change": None, "absolute_change": None,
                          "unavailable_reason": None, "comparability_note": None}
            if name == "eps" and estimate["basis"] == "unknown":
                comparison["unavailable_reason"] = "Accounting basis unspecified; comparable growth unavailable."
            elif baseline is None:
                comparison["unavailable_reason"] = "Comparable prior-period actual unavailable."
            else:
                if "unknown" in {estimate["basis"], baseline["basis"]}:
                    comparison["comparability_note"] = (
                        "Arithmetic comparison of reported revenue amounts; accounting-basis comparability "
                        "is unconfirmed. Label this growth claim unsupported and disclose that assumption."
                    )
                comparison.update(_compare(Decimal(estimate["value"]), Decimal(baseline["value"])))
            entry["comparisons"][label] = comparison
        metrics[name] = entry
    return metrics


def _research_options(packets: list[dict], registry: dict, event: dict, market) -> dict | None:
    event_day = date.fromisoformat(event["event_date"])
    today = market.as_of.astimezone(NY).date()
    options = []
    for packet in packets:
        value = packet.get("options_implied_move")
        if not value or value["earnings_date"] != event["event_date"]:
            continue
        observed = date.fromisoformat(value["observed_date"])
        expiry = date.fromisoformat(value["expiry"]) if value["expiry"] else None
        # Dated recent reporting, for this event; an unknown release time excludes same-day expiry.
        if not market.session_date - timedelta(days=7) <= observed <= today:
            continue
        if observed > event_day or (expiry and not event_day < expiry <= event_day + timedelta(days=14)):
            continue
        options.append({**value, "source_id": packet["source_id"]})
    options.sort(key=lambda item: (item["observed_date"], registry[item["source_id"]].eligible_at), reverse=True)
    return options[0] if options else None


def earnings_context(event: dict | None, packets: list[dict], sources: list, market, structured: dict | None = None) -> dict | None:
    if event is None:
        return None
    registry = {source.id: source for source in sources}
    structured = structured or {}
    metrics = _structured_metrics(structured, getattr(market, "sector_key", None)) if structured.get("estimates") else None
    metrics_source = "Yahoo Finance structured estimates"
    if metrics is None:
        revenue = ((structured.get("estimates") or {}).get("revenue") or {}).get("avg")
        metrics = _research_metrics(packets, registry, event, Decimal(str(revenue)) if revenue else None)
        metrics_source = "cited research extractions"
    revisions = None
    estimates = structured.get("estimates") or {}
    if estimates.get("eps_trend") or estimates.get("eps_revisions"):
        trend, changes = estimates.get("eps_trend") or {}, estimates.get("eps_revisions") or {}
        cents = lambda value: None if value is None else f"${value:,.2f}"  # noqa: E731 - display rounding only
        revisions = {"eps_consensus_now": cents(trend.get("current")), "eps_consensus_30_days_ago": cents(trend.get("30daysAgo")),
                     "eps_consensus_90_days_ago": cents(trend.get("90daysAgo")),
                     "upward_revisions_last_30_days": changes.get("upLast30days"),
                     "downward_revisions_last_30_days": changes.get("downLast30days"),
                     "source_id": structured["analysis_source_id"]}
    options = structured.get("options")
    if options is None:
        options = _research_options(packets, registry, event, market)
    return {
        "event_date": event["event_date"], "event_source_id": event["source_id"],
        "event_status": event["event_status"], "event_timing": event.get("event_timing", "unknown"),
        "event_evidence": event["supporting_material"], "metrics_source": metrics_source, "metrics": metrics,
        "estimate_revisions": revisions,
        "options_implied_move": options,
        "options_missing_reason": None if options else "No usable dated options-implied move found for this release.",
        "calculation": "Expected growth = (consensus / comparable actual - 1) * 100; estimates are not actual results.",
    }
