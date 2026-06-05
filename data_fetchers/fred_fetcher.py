from __future__ import annotations

import re

import aiohttp

from config import settings
from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)

BASE = "https://api.stlouisfed.org/fred/series/observations"

# Macro series we care about for crypto positioning
SERIES = {
    "DFF": "fed_funds_rate",        # Federal Funds Effective Rate
    "DGS10": "us_10y_treasury",     # 10-year Treasury
    "T10Y2Y": "yield_curve_10y2y",  # 10Y minus 2Y
    "VIXCLS": "vix",                # CBOE Volatility Index
    "DTWEXBGS": "dxy",              # Trade-weighted USD index
}


_API_KEY_RE = re.compile(r"(api_key=)[^&'\"]+")


def _sanitize_error(exc: Exception) -> str:
    """Avoid leaking FRED API keys through aiohttp's URL-rich errors."""
    return _API_KEY_RE.sub(r"\1<redacted>", str(exc))


async def _fetch_series(session: aiohttp.ClientSession, series_id: str) -> float | None:
    params = {
        "series_id": series_id,
        "api_key": settings.fred_api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    async with session.get(BASE, params=params, timeout=15) as r:
        r.raise_for_status()
        data = await r.json()
    obs = data.get("observations", [])
    if not obs:
        return None
    val = obs[0].get("value")
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def fetch_fred() -> dict:
    if not settings.fred_api_key:
        log.warning("fred.skip", reason="no_api_key")
        return {"available": False}

    try:
        previous = await cache.get_json("macro:fred") or {}
    except Exception:
        previous = {}

    out: dict = {"available": True}
    async with aiohttp.ClientSession() as session:
        for series_id, name in SERIES.items():
            try:
                out[name] = await _fetch_series(session, series_id)
            except Exception as e:
                log.warning("fred.series_failed", series=series_id, err=_sanitize_error(e))
                # Keep the last known value warm when FRED rate-limits or flakes.
                # Macro gates prefer slightly-stale data over silently losing VIX.
                out[name] = previous.get(name)

    out["available"] = any(out.get(name) is not None for name in SERIES.values())
    await cache.set_json("macro:fred", out, ttl=2 * 3600)
    log.info("fred.refresh", **{k: v for k, v in out.items() if k != "available"})
    return out
