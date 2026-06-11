"""Crypto Fear & Greed Index — alternative.me free API.

GET https://api.alternative.me/fng/?limit=1 → no key, no rate-limit drama.
Cached in Redis for an hour (the index itself updates daily).

Instrumentation-first integration (v3.0): the value flows into macro_lite,
the DecisionAudit trace and the AI sentiment context. NO hard trading rule
is keyed off it yet — once a few weeks of joint (FNG × signal outcome) data
accumulate, a sweep can test extreme-FNG gates the same way every other
parameter earned its place.
"""
from __future__ import annotations

from typing import Any

import aiohttp

from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_CACHE_KEY = "macro:fng"
_CACHE_TTL = 3600


async def fetch_fear_greed() -> dict[str, Any] | None:
    """Return {"value": int 0-100, "label": str, "as_of": str} or None.

    Redis-cached 1h; network failures fail-open (None) and never raise.
    """
    try:
        cached = await cache.get_json(_CACHE_KEY)
        if cached:
            return cached
    except Exception as e:
        log.debug("fng.cache_read_failed", err=str(e))

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_FNG_URL) as resp:
                if resp.status != 200:
                    log.warning("fng.api_error", status=resp.status)
                    return None
                payload = await resp.json()
    except Exception as e:
        log.warning("fng.fetch_failed", err=str(e))
        return None

    try:
        row = (payload.get("data") or [])[0]
        out = {
            "value": int(row["value"]),
            "label": str(row.get("value_classification") or ""),
            "as_of": str(row.get("timestamp") or ""),
        }
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("fng.parse_failed", err=str(e))
        return None

    try:
        await cache.set_json(_CACHE_KEY, out, ttl=_CACHE_TTL)
    except Exception as e:
        log.debug("fng.cache_write_failed", err=str(e))
    return out


__all__ = ["fetch_fear_greed"]
