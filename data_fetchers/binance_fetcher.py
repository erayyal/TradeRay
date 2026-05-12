from __future__ import annotations

from typing import List

from binance import AsyncClient

from config import settings
from core.logger import get_logger
from core.redis_client import cache
from data_fetchers.technicals import compute_indicators

log = get_logger(__name__)

# python-binance interval strings
INTERVAL_MAP = {
    "1m": AsyncClient.KLINE_INTERVAL_1MINUTE,
    "5m": AsyncClient.KLINE_INTERVAL_5MINUTE,
    "15m": AsyncClient.KLINE_INTERVAL_15MINUTE,
    "1h": AsyncClient.KLINE_INTERVAL_1HOUR,
}


class BinanceFetcher:
    """Async Binance Futures fetcher.

    Holds a single shared AsyncClient bound to testnet/futures.
    Writes OHLCV + computed indicators + last price into Redis.
    """

    def __init__(self) -> None:
        self._client: AsyncClient | None = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = await AsyncClient.create(
                api_key=settings.binance_api_key,
                api_secret=settings.binance_api_secret,
                testnet=settings.binance_testnet,
            )
            log.info("binance.connected", testnet=settings.binance_testnet)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_connection()
            self._client = None

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Binance client not connected")
        return self._client

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> List[dict]:
        bn_interval = INTERVAL_MAP[interval]
        raw = await self.client.futures_klines(
            symbol=symbol, interval=bn_interval, limit=limit
        )
        candles = [
            {
                "open_time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "close_time": int(r[6]),
            }
            for r in raw
        ]
        return candles

    async def fetch_mark_price(self, symbol: str) -> float:
        data = await self.client.futures_mark_price(symbol=symbol)
        return float(data["markPrice"])

    async def refresh(self, symbol: str, interval: str) -> None:
        """One-shot: pull klines, compute indicators, push everything to Redis."""
        candles = await self.fetch_klines(symbol, interval)
        indicators = compute_indicators(candles)

        await cache.set_json(f"ohlcv:{symbol}:{interval}", candles, ttl=900)
        await cache.set_json(
            f"indicators:{symbol}:{interval}", indicators, ttl=900
        )

        # Track latest price off the close of the last candle for the UI
        last_close = candles[-1]["close"] if candles else None
        if last_close is not None:
            await cache.set_price(symbol, last_close)

        log.info(
            "binance.refresh",
            symbol=symbol,
            interval=interval,
            candles=len(candles),
            last_close=last_close,
        )


fetcher = BinanceFetcher()
