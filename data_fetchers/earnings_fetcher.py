"""Earnings calendar fetcher for US equities (SP500 / NASDAQ).

Goal: tell the rule engine whether `symbol` is within ±1 trading day of a
known earnings announcement — PEAD literature (Bernard & Thomas 1989,
*Journal of Accounting Research*) shows the immediate ±1 day window
contains the largest implied-move spike + the largest realized-move
surprise, both of which violate ATR-based stop sizing.

Strategy:
  - yfinance is the cheap, key-less source. `Ticker.calendar` returns the
    next scheduled earnings date for most US equities. It's slow (HTTP +
    HTML parse on yfinance's side) and unreliable for older/illiquid
    tickers — so we cache aggressively (24h TTL).
  - Stored in Redis under `earnings:{symbol}` so all workers share one
    fetch per symbol per day.
  - Network/parse failures degrade gracefully — the gate returns False
    (no blackout) rather than blocking entries on yfinance flakiness.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)


_EARNINGS_CACHE_TTL_SEC: int = 24 * 60 * 60  # 24 hours
_EARNINGS_BLACKOUT_DAYS: int = 1  # ±1 trading day around earnings


def _yfinance_next_earnings_sync(symbol: str) -> str | None:
    """Blocking: query yfinance for the next earnings date.

    Returns ISO date string (YYYY-MM-DD) or None if no upcoming earnings
    is known. yfinance is rate-limited and flaky — caller must wrap with
    asyncio.to_thread + try/except.
    """
    import yfinance as yf
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        # yfinance returns dict-like with 'Earnings Date' key (list of pd.Timestamps)
        if cal and isinstance(cal, dict):
            dates = cal.get("Earnings Date")
            if dates:
                # `dates` is usually a list — pick the earliest future one
                future_dates = []
                today = datetime.now(timezone.utc).date()
                for d in (dates if isinstance(dates, list) else [dates]):
                    try:
                        date_obj = d.date() if hasattr(d, "date") else d
                        if date_obj >= today:
                            future_dates.append(date_obj)
                    except Exception:
                        continue
                if future_dates:
                    return min(future_dates).isoformat()
    except Exception as e:
        log.debug("earnings.yfinance_failed", symbol=symbol, err=str(e))
    return None


async def fetch_next_earnings_date(symbol: str) -> str | None:
    """Return the next earnings date (ISO) for `symbol`, cached 24h.

    Never raises — returns None on any failure. The blackout gate
    treats None as "no scheduled earnings known", i.e. no blackout.
    """
    cache_key = f"earnings:{symbol}"
    try:
        cached = await cache.client.get(cache_key)
        if cached is not None:
            return cached or None  # empty string sentinel = "checked, none found"
    except Exception as e:
        log.debug("earnings.redis_read_failed", symbol=symbol, err=str(e))

    # Cache miss → fetch off-thread
    try:
        date_str = await asyncio.to_thread(_yfinance_next_earnings_sync, symbol)
    except Exception as e:
        log.warning("earnings.fetch_failed", symbol=symbol, err=str(e))
        date_str = None

    # Write back (empty string sentinel so we don't refetch on every cycle)
    try:
        await cache.client.set(
            cache_key, date_str or "", ex=_EARNINGS_CACHE_TTL_SEC
        )
    except Exception as e:
        log.debug("earnings.redis_write_failed", symbol=symbol, err=str(e))

    return date_str


def is_in_earnings_blackout(
    next_earnings_iso: str | None,
    *,
    now_utc: datetime | None = None,
    window_days: int = _EARNINGS_BLACKOUT_DAYS,
) -> bool:
    """True iff `now` is within ±`window_days` of the scheduled earnings.

    Bernard-Thomas (1989) showed the highest-vol window is the announcement
    day plus the immediately-following session. We veto the ±1 day window
    to keep ATR-based stops valid.
    """
    if not next_earnings_iso:
        return False
    try:
        earnings_date = datetime.fromisoformat(next_earnings_iso).date()
    except ValueError:
        return False
    today = (now_utc or datetime.now(timezone.utc)).date()
    delta = abs((earnings_date - today).days)
    return delta <= window_days


__all__ = [
    "fetch_next_earnings_date",
    "is_in_earnings_blackout",
]
