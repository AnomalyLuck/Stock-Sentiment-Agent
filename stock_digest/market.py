from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import yfinance as yf
from exchange_calendars.exchange_calendar_xnys import XNYSExchangeCalendar
from yfinance import cache
from yfinance.exceptions import YFRateLimitError, YFTickerMissingError

from .models import ExtendedHours, MarketSnapshot

NY = ZoneInfo("America/New_York")
DELAY = timedelta(minutes=15)
EXCHANGES = {"NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS", "NYQ": "XNYS",
             "ASE": "XASE", "PCX": "ARCX", "BTS": "BATS", "IEX": "IEXG"}


class InputError(ValueError):
    """An actionable input or configuration error (exit 2)."""


class DigestError(RuntimeError):
    """A safe-to-print runtime error (exit 1)."""


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper().replace("-", ".")
    if not re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", ticker):
        raise InputError("Enter one US stock symbol, such as NVDA or BRK.B (BRK-B also works).")
    return ticker


def number(value, label: str, *, positive: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DigestError(f"Yahoo Finance returned an invalid {label}.") from None
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise DigestError(f"Yahoo Finance returned an invalid {label}.")
    return result


async def fetch_market(ticker: str) -> tuple[MarketSnapshot, dict]:
    """Return the validated snapshot plus a small profile of Yahoo calendar fields."""
    # yfinance is synchronous; leave the event loop available to the run deadline.
    return await asyncio.to_thread(_fetch_market, ticker)


def _configure_yahoo() -> None:
    # Disable yfinance's default on-disk cookie/timezone/ISIN databases for this one-shot CLI.
    # These are private yfinance names (pinned <1.8); if a release renames them, yfinance
    # simply keeps its default caches rather than crashing the run.
    for manager, attribute, dummy in (("_TzCacheManager", "_tz_cache", "_TzCacheDummy"),
                                      ("_CookieCacheManager", "_Cookie_cache", "_CookieCacheDummy"),
                                      ("_ISINCacheManager", "_isin_cache", "_ISINCacheDummy")):
        try:
            setattr(getattr(cache, manager), attribute, getattr(cache, dummy)())
        except AttributeError:
            pass
    yf.config.debug.hide_exceptions = False
    yf.config.network.retries = 0
    # Provider log bodies can contain authentication crumbs; report safe errors below.
    logging.getLogger("yfinance").disabled = True


def _fetch_market(ticker: str) -> tuple[MarketSnapshot, dict]:
    _configure_yahoo()
    try:
        return _snapshot(ticker)
    except (InputError, DigestError):
        raise
    except YFRateLimitError:
        raise DigestError("Yahoo Finance rate limit reached. Try again later; no market API key is required.") from None
    except YFTickerMissingError:
        raise DigestError(f"Yahoo Finance has no usable price history for {ticker}. Check the symbol or retry later.") from None
    except Exception as exc:
        raise DigestError(f"Yahoo Finance request failed ({type(exc).__name__}). Check connectivity or retry later.") from None


HISTORY_ARGS = dict(auto_adjust=False, back_adjust=False, repair=False,
                    prepost=False, actions=False, keepna=True, rounding=False, timeout=15)
PROFILE_FIELDS = ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd", "isEarningsDateEstimate",
                  "exDividendDate", "dividendDate", "website", "industryKey", "sectorKey")


def choose_session(now: datetime, calendar) -> tuple[object, str, str | None]:
    """Pick the session to report: ('completed' | 'intraday', optional reader note).

    The app never reports a bar newer than now minus the 15-minute cutoff. In the
    first minutes after an open it reports the previous completed session; in the
    first 15 minutes after a close it reports the last eligible minute bar.
    """
    today = now.astimezone(NY).date()
    session = calendar.date_to_session(today, direction="previous")
    if calendar.session_open(session).to_pydatetime() > now:
        session = calendar.previous_session(session)
    opening = calendar.session_open(session).to_pydatetime()
    closing = calendar.session_close(session).to_pydatetime()
    if now - DELAY >= closing:
        return session, "completed", None
    if now - DELAY < opening + timedelta(minutes=1):
        return (calendar.previous_session(session), "completed",
                "The regular session opened less than 16 minutes ago; showing the previous completed session.")
    return session, "intraday", None


def _positive(value) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result > 0 else None


def _rows_for(frame, day):
    return frame.loc[frame.index.tz_convert(NY).date == day]


def _extended_hours(identity: dict, close_price: Decimal, closing: datetime, next_open: datetime,
                    as_of: datetime) -> ExtendedHours | None:
    """Latest pre-market or after-hours quote after this completed session and before the next open."""
    best = None
    for session, prefix in (("after-hours", "postMarket"), ("pre-market", "preMarket")):
        price, stamp = _positive(identity.get(prefix + "Price")), identity.get(prefix + "Time")
        if price is None or not isinstance(stamp, (int, float)):
            continue
        observed = datetime.fromtimestamp(stamp, UTC)
        if closing < observed <= as_of and observed < next_open and (best is None or observed > best[2]):
            best = (session, price, observed)
    if best is None:
        return None
    session, price, observed = best
    change = price - close_price
    return ExtendedHours(session=session, price=price, base=close_price, absolute_change=change,
                         percent_change=Decimal(100) * change / close_price, observed_at=observed)


def _snapshot(ticker: str) -> tuple[MarketSnapshot, dict]:
    symbol = ticker.replace(".", "-")
    stock = yf.Ticker(symbol)
    identity = stock.get_info()
    if not isinstance(identity, dict) or identity.get("symbol") != symbol:
        raise InputError(f"Yahoo Finance could not resolve {ticker} unambiguously.")
    if identity.get("quoteType") != "EQUITY" or identity.get("exchange") not in EXCHANGES:
        raise InputError("Only US exchange-listed equities (including ADRs) are supported; not OTC, funds, or crypto.")
    if identity.get("currency") != "USD" or identity.get("exchangeTimezoneName") != "America/New_York":
        raise InputError("Only USD listings using the US regular-session calendar are supported.")
    company = identity.get("longName") or identity.get("shortName")
    if not company:
        raise DigestError("Company identity is missing from Yahoo Finance.")

    now = datetime.now(UTC)
    today = now.astimezone(NY).date()
    calendar = XNYSExchangeCalendar(start=today - timedelta(days=90), end=today + timedelta(days=14))
    session, mode, session_note = choose_session(now, calendar)
    limitations = [
        "Yahoo Finance prices use split-adjusted Close, not dividend-adjusted Adj Close.",
        "The app imposes a 15-minute cutoff; Yahoo feed delay and venue coverage may vary.",
        "Observation time is the bar/session boundary; the last constituent trade time is unavailable.",
    ]
    if session_note:
        limitations.append(session_note)

    minute_bars = None
    if mode == "intraday":
        opening = calendar.session_open(session).to_pydatetime()
        closing = calendar.session_close(session).to_pydatetime()
        minutes = stock.history(start=opening, end=now, interval="1m", **HISTORY_ARGS)
        eligible = (now - DELAY).replace(second=0, microsecond=0)
        if not minutes.empty and minutes.index.tz is not None and not minutes.index.has_duplicates:
            starts = minutes.index.tz_convert(UTC)
            ends = starts + timedelta(minutes=1)
            minute_bars = minutes.loc[(starts >= opening) & (starts < closing)
                                      & (ends <= min(eligible, closing))].sort_index()
            minute_bars = minute_bars[minute_bars["Close"].notna()]
        if minute_bars is None or minute_bars.empty:
            # Halted or not yet traded today: report the last completed session instead.
            session, mode, minute_bars = calendar.previous_session(session), "completed", None
            limitations.append("No eligible regular-session minute bar was available today; "
                               "showing the previous completed session.")
    opening = calendar.session_open(session).to_pydatetime()
    closing = calendar.session_close(session).to_pydatetime()
    previous = calendar.previous_session(session)
    session_date, comparison_date = session.date(), previous.date()

    # Yahoo's Close is split-adjusted. Never use dividend-adjusted Adj Close or
    # yfinance's default auto_adjust=True, which would alter the comparison basis.
    # Forty extra calendar days supply the 20-session average volume.
    daily = stock.history(start=str(comparison_date - timedelta(days=40)), end=str(session_date + timedelta(days=1)),
                          interval="1d", **HISTORY_ARGS)
    if daily.empty or daily.index.tz is None or daily.index.has_duplicates:
        raise DigestError("Yahoo Finance returned missing or ambiguous daily history.")

    def daily_row(day):
        rows = _rows_for(daily, day)
        if len(rows) != 1:
            raise DigestError(f"Yahoo Finance did not return the regular-session record for {day}.")
        return rows.iloc[0]

    comparison = number(daily_row(comparison_date)["Close"], "daily close")
    session_row = daily_row(session_date)
    volume = None
    if mode == "completed":
        observed = closing
        price_type = "completed regular-session close"
        price = _positive(session_row["Close"])
        if price is None:
            # Yahoo leaves the day's daily Close empty for hours after the bell. Its quote
            # carries the official regular-market close, stamped at the closing print.
            quote_price = _positive(identity.get("regularMarketPrice"))
            stamp = identity.get("regularMarketTime")
            quote_time = datetime.fromtimestamp(stamp, UTC) if isinstance(stamp, (int, float)) else None
            if quote_price is not None and quote_time and closing - timedelta(minutes=1) <= quote_time <= closing + timedelta(hours=6):
                price = quote_price
                limitations.append("Yahoo's daily close record was not yet populated; the close is the Yahoo "
                                   "quote's regular-market price stamped at the closing print.")
            else:
                raise DigestError(f"Yahoo Finance has not yet published the {session_date} close; retry later.")
        daily_volume = _positive(session_row["Volume"])
        if daily_volume is not None:
            volume = daily_volume
            limitations.append("Volume is Yahoo's reported daily volume for the session.")
    else:
        bars = minute_bars
        observed = bars.index[-1].to_pydatetime().astimezone(UTC) + timedelta(minutes=1)
        price = number(bars.iloc[-1]["Close"], "minute-bar close")
        if "Volume" in bars and bars["Volume"].notna().all():
            volume = sum((number(v, "volume", positive=False) for v in bars["Volume"]), Decimal(0))
            limitations.append("Volume sums returned Yahoo regular-session bars through the observation; "
                               "missing intervals are not imputed.")
        price_type = "regular-session minute-bar close"

    # Opening gap and move since the open, from the same daily/minute records.
    open_price = None
    if minute_bars is not None and minute_bars.index[0].to_pydatetime().astimezone(UTC) == opening:
        open_price = _positive(minute_bars.iloc[0]["Open"])
    if open_price is None:
        open_price = _positive(session_row["Open"])
    gap = since_open = None
    if open_price is not None:
        gap = Decimal(100) * (open_price - comparison) / comparison
        since_open = Decimal(100) * (price - open_price) / open_price

    # Relative volume: session volume over the prior 20 sessions' average daily volume.
    average = relative = None
    prior = daily.loc[daily.index.tz_convert(NY).date < session_date, "Volume"].dropna()
    prior = [v for v in (_positive(x) for x in prior.tail(20)) if v is not None]
    if volume is not None and len(prior) >= 10:
        average = sum(prior, Decimal(0)) / len(prior)
        relative = volume / average

    as_of = datetime.now(UTC)
    next_open = calendar.session_open(calendar.next_session(session)).to_pydatetime()
    if mode == "intraday" and (now < closing <= as_of or now < next_open <= as_of):
        raise DigestError("The session changed while prices were fetched. Run again for a consistent snapshot.")
    if mode == "intraday" and as_of - DELAY - observed > timedelta(minutes=5):
        limitations.append(f"The latest eligible minute bar ends at {observed.astimezone(NY):%H:%M} ET, more than "
                           "5 minutes before the 15-minute cutoff; the stock may be thinly traded or halted.")
    extended = None
    if mode == "completed":
        extended = _extended_hours(identity, price, closing, next_open, as_of)
    change = price - comparison
    short_name = identity.get("shortName")
    snapshot = MarketSnapshot(
        ticker=ticker, company=company, short_name=short_name if short_name != company else None,
        exchange=EXCHANGES[identity["exchange"]],
        security_type="EQUITY", sector=identity.get("sector") or identity.get("industry"),
        industry=identity.get("industry"), sector_key=identity.get("sectorKey"),
        industry_key=identity.get("industryKey"), website=identity.get("website"),
        price=price, comparison_close=comparison, absolute_change=change,
        percent_change=Decimal(100) * change / comparison,
        session_date=session_date, comparison_date=comparison_date,
        comparison_session_close=calendar.session_close(previous).to_pydatetime(),
        session_open=opening, session_close=closing, observed_at=observed, as_of=as_of,
        price_type=price_type, volume=volume,
        volume_start=opening if volume is not None else None,
        volume_end=observed if volume is not None else None,
        average_volume=average, relative_volume=relative,
        session_open_price=open_price, gap_percent=gap, since_open_percent=since_open,
        extended_hours=extended,
        provenance=[f"https://finance.yahoo.com/quote/{symbol}/",
                    f"https://finance.yahoo.com/quote/{symbol}/history/"],
        limitations=limitations,
    )
    return snapshot, {key: identity.get(key) for key in PROFILE_FIELDS}
