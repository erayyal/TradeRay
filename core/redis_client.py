from __future__ import annotations

import json
from typing import Any, Optional

import redis.asyncio as redis

from config import settings
from core.logger import get_logger

log = get_logger(__name__)

# Key conventions
#   ohlcv:{symbol}:{interval}      -> JSON list of latest candles
#   indicators:{symbol}:{interval} -> JSON dict of TA-Lib indicator snapshot
#   price:{symbol}                 -> latest mark price (float as str)
#   sentiment:cryptopanic          -> JSON list of latest news + score
#   macro:fred                     -> JSON dict of macro series
#   onchain:defillama              -> JSON dict of TVL / chain stats
#   decision:{symbol}:latest       -> JSON of last Master Trader output
#   order:{order_id}               -> JSON snapshot of an order placement


class RedisCache:
    """Async Redis wrapper with JSON helpers and a single shared connection pool."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = redis.from_url(
                self._url,
                encoding="utf-8",
                decode_responses=True,
                health_check_interval=30,
            )
            await self._client.ping()
            log.info("redis.connected", url=self._url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("Redis not connected — call connect() first")
        return self._client

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, default=str)
        if ttl:
            await self.client.set(key, payload, ex=ttl)
        else:
            await self.client.set(key, payload)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.client.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_price(self, symbol: str, price: float) -> None:
        await self.client.set(f"price:{symbol}", str(price), ex=120)

    async def get_price(self, symbol: str) -> float | None:
        raw = await self.client.get(f"price:{symbol}")
        return float(raw) if raw is not None else None


cache = RedisCache(settings.redis_url)
