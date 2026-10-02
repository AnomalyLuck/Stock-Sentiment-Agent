"""Ticker parsing, issuer resolution and timezone selection.

Obvious symbols resolve without asking. A clarification (InputError) is raised only
when Yahoo cannot resolve the symbol or several different companies share it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..market import InputError, _configure_yahoo
from .models import Issuer

# Exchange prefixes ("NASDAQ:MSFT") → Yahoo exchange codes they may resolve to (US),
# or the Yahoo symbol suffix for non-US venues.
US_PREFIXES = {
    "NASDAQ": {"NMS", "NGM", "NCM"}, "NYSE": {"NYQ"}, "NYSEAMERICAN": {"ASE"}, "AMEX": {"ASE"},
    "NYSEARCA": {"PCX"}, "ARCA": {"PCX"}, "CBOE": {"BTS"}, "BATS": {"BTS"},
}
FOREIGN_SUFFIXES = {
    "TSX": "TO", "TSXV": "V", "LSE": "L", "LON": "L", "TYO": "T", "TSE": "T", "HKEX": "HK", "HKG": "HK",
    "ASX": "AX", "XETRA": "DE", "ETR": "DE", "FRA": "F", "EPA": "PA", "EURONEXT": "PA", "AMS": "AS",
    "SIX": "SW", "SWX": "SW", "NSE": "NS", "BSE": "BO", "KRX": "KS", "BIT": "MI", "BME": "MC", "STO": "ST",
}
SYMBOL = re.compile(r"[A-Z0-9]{1,6}(?:[.\-][A-Z]{1,3})?")


def parse_symbol(raw: str, exchange: str | None = None) -> tuple[str, str | None]:
    """Return (Yahoo symbol, US exchange prefix or None). Raises InputError on malformed input."""
    text = (raw or "").strip().upper().replace(" ", "")
    prefix = (exchange or "").strip().upper().replace(" ", "") or None
    if ":" in text:
        given, text = text.split(":", 1)
        if prefix and given != prefix:
            raise InputError(f"The ticker says {given} but the exchange option says {prefix}; use one.")
        prefix = given
    if not SYMBOL.fullmatch(text):
        raise InputError("Enter one ticker, such as NVDA, AAPL, BRK.B or NASDAQ:MSFT.")
    base, _, suffix = text.replace("-", ".").partition(".")
    if prefix in FOREIGN_SUFFIXES:
        if suffix:
            raise InputError(f"Use either {prefix}:{base} or a Yahoo suffix such as {base}.{FOREIGN_SUFFIXES[prefix]}, not both.")
        if prefix in {"HKEX", "HKG"} and base.isdigit():
            base = base.zfill(4)
        return f"{base}.{FOREIGN_SUFFIXES[prefix]}", None
    if prefix and prefix not in US_PREFIXES:
        raise InputError(f"Unknown exchange prefix {prefix}. Use, for example, NASDAQ:MSFT, NYSE:JPM or TSX:SHOP.")
    if len(suffix) == 1:          # US share class: BRK.B → BRK-B
        return f"{base}-{suffix}", prefix
    if suffix:                    # Yahoo venue suffix: SHOP.TO
        return f"{base}.{suffix}", None
    return base, prefix


def display_ticker(symbol: str) -> str:
    base, dash, share_class = symbol.partition("-")
    return f"{base}.{share_class}" if dash and len(share_class) == 1 else symbol


def resolve_issuer(raw: str, exchange: str | None = None) -> Issuer:
    """Resolve to one listed company with Yahoo Finance (synchronous; run in a thread)."""
    import yfinance as yf

    symbol, us_prefix = parse_symbol(raw, exchange)
    _configure_yahoo()
    try:
        info = yf.Ticker(symbol).get_info()
    except Exception:
        info = None
    if not isinstance(info, dict) or info.get("symbol", "").upper() != symbol or not (info.get("longName") or info.get("shortName")):
        raise InputError(_clarify(symbol, _search(symbol)))
    if info.get("quoteType") != "EQUITY":
        raise InputError(f"{display_ticker(symbol)} is a {str(info.get('quoteType') or 'non-equity').lower()} listing, "
                         "not a company stock. Enter a company's ticker.")
    if us_prefix and info.get("exchange") not in US_PREFIXES[us_prefix]:
        raise InputError(f"{display_ticker(symbol)} is listed on {info.get('fullExchangeName') or info.get('exchange')}, "
                         f"not {us_prefix}. Check the exchange or omit it.")
    cap = info.get("marketCap")
    return Issuer(
        ticker=display_ticker(symbol), symbol=symbol,
        exchange=str(info.get("fullExchangeName") or info.get("exchange") or "unknown"),
        company=info.get("longName") or info.get("shortName"),
        short_name=info.get("shortName"), website=info.get("website"),
        market_cap=float(cap) if isinstance(cap, (int, float)) and cap > 0 else None,
        currency=info.get("currency"), sector=info.get("sector"), industry=info.get("industry"),
    )


def _search(symbol: str) -> list[dict]:
    import yfinance as yf
    try:
        return [q for q in (yf.Search(symbol, max_results=10, news_count=0).quotes or []) if isinstance(q, dict)]
    except Exception:
        return []


def _clarify(symbol: str, quotes: list[dict]) -> str:
    """A clarification listing different companies that use this symbol, or a plain miss."""
    base = symbol.split(".")[0].split("-")[0]
    matches = {}
    for quote in quotes:
        candidate = str(quote.get("symbol", "")).upper()
        if quote.get("quoteType") == "EQUITY" and re.split(r"[.\-]", candidate)[0] == base:
            matches.setdefault(candidate, f"{candidate} ({quote.get('longname') or quote.get('shortname') or 'unknown'}, "
                                          f"{quote.get('exchDisp') or quote.get('exchange')})")
    if matches:
        listed = "; ".join(list(matches.values())[:5])
        return f"{display_ticker(symbol)} could not be resolved to one company. Did you mean: {listed}? Re-enter the exact symbol."
    return f"Yahoo Finance could not resolve {display_ticker(symbol)} to a listed company. Check the symbol or add an exchange (e.g. NASDAQ:MSFT)."


def resolve_timezone(name: str | None) -> str:
    """The requested IANA zone, else the system zone, else UTC."""
    if name:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            raise InputError(f"Unknown timezone {name!r}; use an IANA name such as America/New_York.") from None
    for candidate in (os.environ.get("TZ", "").lstrip(":"), _system_zone()):
        if candidate:
            try:
                ZoneInfo(candidate)
                return candidate
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return "UTC"


def _system_zone() -> str | None:
    try:
        target = str(Path("/etc/localtime").resolve())
    except OSError:
        return None
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else None

