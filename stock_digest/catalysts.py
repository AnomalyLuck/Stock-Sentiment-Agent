"""Structured catalyst data from Yahoo Finance (and optionally SEC EDGAR).

Everything here is deterministic provider data: earnings dates and results,
consensus estimates and revisions, analyst rating changes, SEC filings, insider
transactions, same-session benchmark/peer moves, and an options straddle. Each
category fails independently; a failure becomes a diagnostic, never a crash.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import yfinance as yf

from .market import HISTORY_ARGS, NY, _configure_yahoo, _positive, _rows_for
from .models import Benchmark, MarketSnapshot

SECTOR_ETFS = {
    "technology": ("XLK", "Technology sector"), "communication-services": ("XLC", "Communication services"),
    "consumer-cyclical": ("XLY", "Consumer discretionary"), "consumer-defensive": ("XLP", "Consumer staples"),
    "energy": ("XLE", "Energy sector"), "financial-services": ("XLF", "Financial sector"),
    "healthcare": ("XLV", "Health care sector"), "industrials": ("XLI", "Industrials sector"),
    "basic-materials": ("XLB", "Materials sector"), "real-estate": ("XLRE", "Real estate sector"),
    "utilities": ("XLU", "Utilities sector"),
}
INDUSTRY_ETFS = {
    "semiconductors": ("SMH", "Semiconductors"), "semiconductor-equipment-materials": ("SMH", "Semiconductors"),
    "software-infrastructure": ("IGV", "Software"), "software-application": ("IGV", "Software"),
    "biotechnology": ("XBI", "Biotech"), "banks-regional": ("KRE", "Regional banks"),
    "oil-gas-e-p": ("XOP", "Oil & gas producers"), "aerospace-defense": ("ITA", "Aerospace & defense"),
    "gold": ("GDX", "Gold miners"), "airlines": ("JETS", "Airlines"), "internet-retail": ("XRT", "Retail"),
    "specialty-retail": ("XRT", "Retail"), "solar": ("TAN", "Solar"), "homebuilding": ("XHB", "Homebuilders"),
}
FILING_TYPES = {"8-K", "8-K/A", "6-K", "10-Q", "10-K", "20-F", "40-F", "SC 13D", "SC 13D/A", "SC 13G",
                "SC 13G/A", "S-1", "S-3", "S-4", "424B2", "424B3", "424B4", "424B5", "DEF 14A",
                "DEFM14A", "SC TO-T", "SC 14D9", "25-NSE", "15-12B"}
EIGHT_K_ITEMS = {
    "1.01": "Entry into a material definitive agreement", "1.02": "Termination of a material definitive agreement",
    "1.03": "Bankruptcy or receivership", "1.05": "Material cybersecurity incident",
    "2.01": "Completion of acquisition or disposition of assets", "2.02": "Results of operations and financial condition",
    "2.03": "Creation of a direct financial obligation", "2.05": "Costs associated with exit or disposal activities",
    "2.06": "Material impairments", "3.01": "Notice of delisting or listing-standard failure",
    "3.02": "Unregistered sales of equity securities", "4.01": "Change in certifying accountant",
    "4.02": "Non-reliance on previously issued financial statements", "5.01": "Change in control",
    "5.02": "Departure or appointment of directors or officers", "5.03": "Amendments to articles or bylaws",
    "5.07": "Shareholder vote results", "7.01": "Regulation FD disclosure", "8.01": "Other events",
    "9.01": "Financial statements and exhibits",
}


def _finite(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _row(frame, label: str) -> dict | None:
    if frame is None or getattr(frame, "empty", True) or label not in frame.index:
        return None
    return {key: _finite(value) if key != "currency" else value for key, value in frame.loc[label].items()}


def _earnings(stock, profile: dict, now: datetime) -> dict:
    """Next scheduled release and the latest reported quarter, with Yahoo's stated times."""
    result = {"next": None, "last_report": None}
    frame = stock.get_earnings_dates(limit=8)
    if frame is not None and not frame.empty:
        for stamp, row in frame.sort_index().iterrows():
            moment = stamp.to_pydatetime().astimezone(UTC)
            reported = _finite(row.get("Reported EPS"))
            if moment > now and reported is None and result["next"] is None:
                result["next"] = {"date": moment.astimezone(NY).date().isoformat(), "at": moment.isoformat()}
            elif moment <= now and reported is not None:
                result["last_report"] = {
                    "at": moment.isoformat(), "reported_eps": reported,
                    "eps_estimate": _finite(row.get("EPS Estimate")), "surprise_percent": _finite(row.get("Surprise(%)")),
                }
    if result["next"] is None and isinstance(profile.get("earningsTimestamp"), (int, float)):
        moment = datetime.fromtimestamp(profile["earningsTimestamp"], UTC)
        if moment > now:
            result["next"] = {"date": moment.astimezone(NY).date().isoformat(), "at": moment.isoformat()}
    if result["next"]:
        local = datetime.fromisoformat(result["next"]["at"]).astimezone(NY).time()
        # Yahoo often shows a placeholder mid-session time; only clear pre/post-market times count.
        result["next"]["timing"] = ("before_open" if local < time(9, 30) else
                                    "after_close" if local >= time(16) else "unknown")
        result["next"]["estimated"] = profile.get("isEarningsDateEstimate")
        start, end = profile.get("earningsTimestampStart"), profile.get("earningsTimestampEnd")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end - start > 86400:
            result["next"]["estimated"] = True  # A date range is an estimate.
    return result


def _estimates(stock) -> dict | None:
    eps, revenue = _row(stock.earnings_estimate, "0q"), _row(stock.revenue_estimate, "0q")
    if not eps and not revenue:
        return None
    history = stock.earnings_history
    last_quarter = None
    if history is not None and not history.empty:
        latest = history.sort_index().iloc[-1]
        last_quarter = {"period_end": history.sort_index().index[-1].date().isoformat(),
                        "eps_actual": _finite(latest.get("epsActual")), "eps_estimate": _finite(latest.get("epsEstimate"))}
    return {"eps": eps, "revenue": revenue, "eps_trend": _row(stock.eps_trend, "0q"),
            "eps_revisions": _row(stock.eps_revisions, "0q"), "last_quarter": last_quarter}


def _revenue_history(stock) -> list[dict]:
    frame = stock.quarterly_income_stmt
    if frame is None or frame.empty or "Total Revenue" not in frame.index:
        return []
    rows = []
    for column, value in frame.loc["Total Revenue"].items():
        if (amount := _finite(value)) is not None and amount > 0:
            rows.append({"period_end": column.date().isoformat(), "revenue": amount})
    return sorted(rows, key=lambda item: item["period_end"], reverse=True)


def _analyst_actions(stock, since: datetime) -> list[dict]:
    frame = stock.upgrades_downgrades
    if frame is None or frame.empty:
        return []
    actions = []
    for stamp, row in frame.sort_index(ascending=False).iterrows():
        # Yahoo's GradeDate comes from an epoch value; yfinance leaves it naive UTC.
        moment = stamp.to_pydatetime().replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.to_pydatetime()
        if moment < since:
            break
        actions.append({
            "at": moment.isoformat(), "firm": row.get("Firm"), "action": row.get("Action"),
            "to_grade": row.get("ToGrade") or None, "from_grade": row.get("FromGrade") or None,
            "price_target_action": row.get("priceTargetAction") or None,
            "price_target": _finite(row.get("currentPriceTarget")) or None,
            "prior_price_target": _finite(row.get("priorPriceTarget")) or None,
        })
    return actions[:8]


def _insider(stock, since: date) -> list[dict]:
    frame = stock.insider_transactions
    if frame is None or frame.empty:
        return []
    rows = []
    for _, row in frame.iterrows():
        start = row.get("Start Date")
        day = start.date() if hasattr(start, "date") else None
        text = str(row.get("Text") or "")
        if day is None or day < since or not any(word in text for word in ("Sale", "Purchase", "Buy")):
            continue
        rows.append({"transaction_date": day.isoformat(), "insider": row.get("Insider"), "position": row.get("Position"),
                     "description": text, "shares": _finite(row.get("Shares")), "value": _finite(row.get("Value"))})
    return sorted(rows, key=lambda item: item["value"] or 0, reverse=True)[:5]


def _yahoo_filings(stock, since: date, symbol: str) -> list[dict]:
    filings = []
    for item in stock.get_sec_filings() or []:
        day = item.get("date")
        if not isinstance(day, date) or day < since or item.get("type") not in FILING_TYPES:
            continue
        filings.append({"form": item["type"], "date": day.isoformat(), "accepted_at": None,
                        "title": item.get("title") or item["type"], "items": [],
                        "url": item.get("edgarUrl") or f"https://finance.yahoo.com/quote/{symbol}/sec-filings/",
                        "provider": "Yahoo Finance SEC filings"})
    return filings[:6]


def _edgar_filings(ticker: str, since: date, agent: str) -> list[dict]:
    """SEC EDGAR submissions: item codes and acceptance times. SEC requires a contact User-Agent."""
    headers = {"User-Agent": agent, "Accept": "application/json"}
    with httpx.Client(timeout=10, headers=headers) as client:
        mapping = client.get("https://www.sec.gov/files/company_tickers.json")
        mapping.raise_for_status()
        wanted = ticker.replace(".", "-").upper()
        cik = next((row["cik_str"] for row in mapping.json().values() if str(row.get("ticker")).upper() == wanted), None)
        if cik is None:
            return []
        response = client.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        response.raise_for_status()
    recent = response.json().get("filings", {}).get("recent", {})
    filings = []
    for index, form in enumerate(recent.get("form", [])):
        day = date.fromisoformat(recent["filingDate"][index])
        if day < since:
            break
        if form not in FILING_TYPES:
            continue
        accepted = recent.get("acceptanceDateTime", [None] * (index + 1))[index]
        accepted_at = None
        if accepted:
            # EDGAR stamps acceptance in US Eastern time despite the trailing "Z".
            accepted_at = datetime.fromisoformat(accepted.rstrip("Z").split(".")[0]).replace(tzinfo=NY).isoformat()
        items = [code.strip() for code in (recent.get("items", [""] * (index + 1))[index] or "").split(",") if code.strip()]
        accession = recent["accessionNumber"][index].replace("-", "")
        document = recent.get("primaryDocument", [""] * (index + 1))[index]
        described = [EIGHT_K_ITEMS.get(code, f"Item {code}") for code in items if code != "9.01"]
        filings.append({"form": form, "date": day.isoformat(), "accepted_at": accepted_at,
                        "title": f"{form}: " + ("; ".join(described) if described else
                                               recent.get("primaryDocDescription", [""] * (index + 1))[index] or form),
                        "items": items, "provider": "SEC EDGAR",
                        "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{document}"})
    return filings[:6]


def _benchmark(symbol: str, name: str, role: str, market: MarketSnapshot) -> Benchmark | None:
    """Same-session move on the stock's own price basis (completed close or same minute bar)."""
    stock = yf.Ticker(symbol)
    daily = stock.history(start=str(market.comparison_date), end=str(market.session_date + timedelta(days=1)),
                          interval="1d", **HISTORY_ARGS)
    if daily.empty or daily.index.tz is None:
        return None
    base_rows = _rows_for(daily, market.comparison_date)
    base = _positive(base_rows.iloc[0]["Close"]) if len(base_rows) == 1 else None
    if base is None:
        return None
    price = None
    basis = "completed regular-session close"
    if market.price_type == "completed regular-session close":
        rows = _rows_for(daily, market.session_date)
        price = _positive(rows.iloc[0]["Close"]) if len(rows) == 1 else None
    if price is None:
        # Intraday, or a daily close Yahoo has not populated yet: the bar ending at the observation.
        bars = stock.history(start=market.observed_at - timedelta(minutes=10), end=market.observed_at,
                             interval="1m", **HISTORY_ARGS)
        if bars.empty or bars.index.tz is None:
            return None
        ends = bars.index.tz_convert(UTC) + timedelta(minutes=1)
        bars = bars.loc[(ends <= market.observed_at) & (ends > market.observed_at - timedelta(minutes=5))]
        bars = bars[bars["Close"].notna()]
        if bars.empty:
            return None
        price = _positive(bars.iloc[-1]["Close"])
        basis = "regular-session minute-bar close at the same observation"
    if price is None:
        return None
    return Benchmark(symbol=symbol, name=name, role=role, percent_change=Decimal(100) * (price - base) / base, basis=basis)


def _peers(market: MarketSnapshot) -> list[tuple[str, str]]:
    if not market.industry_key:
        return []
    frame = yf.Industry(market.industry_key).top_companies
    if frame is None or frame.empty:
        return []
    own = {market.ticker.replace(".", "-"), market.ticker}
    peers = []
    for symbol, row in frame.iterrows():
        if symbol in own or row.get("name") == market.company or not str(symbol).replace("-", "").isalpha():
            continue
        peers.append((str(symbol), str(row.get("name") or symbol)))
        if len(peers) == 3:
            break
    return peers


def _benchmarks(market: MarketSnapshot, diagnostics: list[str]) -> list[Benchmark]:
    wanted = [("SPY", "S&P 500 (SPY)", "index"), ("QQQ", "Nasdaq-100 (QQQ)", "index")]
    if market.sector_key in SECTOR_ETFS:
        symbol, name = SECTOR_ETFS[market.sector_key]
        wanted.append((symbol, f"{name} ({symbol})", "sector"))
    if market.industry_key in INDUSTRY_ETFS:
        symbol, name = INDUSTRY_ETFS[market.industry_key]
        if symbol not in {item[0] for item in wanted}:
            wanted.append((symbol, f"{name} ({symbol})", "industry"))
    try:
        wanted += [(symbol, f"{name} ({symbol})", "peer") for symbol, name in _peers(market)]
    except Exception as exc:
        diagnostics.append(f"Yahoo industry peers unavailable ({type(exc).__name__}).")

    def one(item):
        try:
            return _benchmark(*item, market)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, wanted))
    missing = [item[0] for item, result in zip(wanted, results) if result is None]
    if missing:
        diagnostics.append("Benchmark/peer prices unavailable for " + ", ".join(missing) + ".")
    return [result for result in results if result is not None]


def fetch_catalysts(market: MarketSnapshot, profile: dict, news_days: int = 7) -> dict:
    """Collect every structured category; each failure is recorded and skipped."""
    _configure_yahoo()
    symbol = market.ticker.replace(".", "-")
    stock = yf.Ticker(symbol)
    now = datetime.now(UTC)
    since = now - timedelta(days=news_days)
    diagnostics: list[str] = []
    data: dict = {"retrieved_at": now.isoformat(), "symbol": symbol, "diagnostics": diagnostics}

    def attempt(label: str, function, default=None):
        try:
            return function()
        except Exception as exc:
            diagnostics.append(f"Yahoo {label} unavailable ({type(exc).__name__}).")
            return default

    data["earnings"] = attempt("earnings dates", lambda: _earnings(stock, profile, now), {"next": None, "last_report": None})
    data["estimates"] = attempt("analyst estimates", lambda: _estimates(stock))
    data["revenue_history"] = attempt("quarterly revenue", lambda: _revenue_history(stock), [])
    data["analyst_actions"] = attempt("analyst rating changes", lambda: _analyst_actions(stock, since), [])
    data["price_targets"] = attempt("analyst price targets", lambda: stock.analyst_price_targets or None)
    data["insider"] = attempt("insider transactions", lambda: _insider(stock, (since - timedelta(days=5)).date()), [])
    agent = os.environ.get("SEC_USER_AGENT", "").strip()
    filings = None
    if agent:
        try:
            filings = _edgar_filings(market.ticker, since.date(), agent)
        except Exception as exc:
            diagnostics.append(f"SEC EDGAR filings unavailable ({type(exc).__name__}); using Yahoo's filing list.")
    if filings is None:
        filings = attempt("SEC filings", lambda: _yahoo_filings(stock, since.date(), symbol), [])
    data["filings"] = filings
    data["benchmarks"] = attempt("benchmarks", lambda: _benchmarks(market, diagnostics), [])
    for key in ("dividendDate", "exDividendDate"):
        stamp = profile.get(key)
        data[key] = datetime.fromtimestamp(stamp, UTC).date().isoformat() if isinstance(stamp, (int, float)) else None
    return data


def fetch_implied_move(market: MarketSnapshot, event_date: date, timing: str) -> dict | None:
    """ATM straddle for the first expiry that includes the release, as a percent of the stock price."""
    _configure_yahoo()
    stock = yf.Ticker(market.ticker.replace(".", "-"))
    expiries = [date.fromisoformat(value) for value in stock.options]
    # An unknown or after-close release is not captured by a same-day expiry.
    usable = [day for day in expiries if day > event_date or (day == event_date and timing == "before_open")]
    if not usable or min(usable) > event_date + timedelta(days=14):
        return None
    expiry = min(usable)
    chain = stock.option_chain(expiry.isoformat())
    underlying = market.price
    strikes = sorted(set(chain.calls["strike"]).intersection(chain.puts["strike"]),
                     key=lambda strike: abs(Decimal(str(strike)) - underlying))
    if not strikes:
        return None
    strike = strikes[0]
    legs, methods = {}, set()
    for name, frame in (("call", chain.calls), ("put", chain.puts)):
        row = frame.loc[frame["strike"] == strike].iloc[0]
        bid, ask = _positive(row.get("bid")), _positive(row.get("ask"))
        if bid is not None and ask is not None and ask >= bid:
            legs[name], spread = (bid + ask) / 2, ask / bid
            methods.add("bid/ask midpoint" + (" (wide spread)" if spread > 2 else ""))
        else:
            last, traded = _positive(row.get("lastPrice")), row.get("lastTradeDate")
            traded_day = traded.to_pydatetime().astimezone(NY).date() if hasattr(traded, "to_pydatetime") else None
            if last is None or traded_day is None or (market.session_date - traded_day).days > 1:
                return None
            legs[name] = last
            methods.add("last trade")
    percent = (legs["call"] + legs["put"]) / underlying * 100
    return {
        "percent": str(percent.quantize(Decimal("0.1"))), "earnings_date": event_date.isoformat(),
        "observed_date": datetime.now(UTC).astimezone(NY).date().isoformat(), "expiry": expiry.isoformat(),
        "strike": str(strike), "call_price": str(legs["call"].quantize(Decimal("0.01"))),
        "put_price": str(legs["put"].quantize(Decimal("0.01"))),
        "methodology": (f"At-the-money {strike:g} straddle ({', '.join(sorted(methods))}) expiring {expiry}, "
                        f"divided by the ${market.price:,.2f} regular-session price; Yahoo Finance delayed option quotes. "
                        "It prices expected movement in either direction from now through expiry, not only the "
                        "earnings reaction."),
        "url": f"https://finance.yahoo.com/quote/{market.ticker.replace('.', '-')}/options/",
    }
