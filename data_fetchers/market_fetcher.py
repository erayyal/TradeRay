"""Unified async market data fetcher for TradeRay.

Adapts two backends behind one interface:
  - Binance (perpetual futures testnet) via python-binance AsyncClient
  - Yahoo Finance (BIST .IS, S&P 500, NASDAQ) via yfinance — wrapped in
    asyncio.to_thread because yfinance is sync.

Term → interval mapping:
  Scalp       → 5m, 15m, 1h
  Short-Term  → 1h, 4h, 1d    (yfinance: 4h resampled from 1h)
  Mid-Term    → 1d

Output contract: list[dict] of OHLCV candles with keys
  open_time (ms epoch), open, high, low, close, volume, close_time (ms epoch)

Dynamic Screener:
  `get_dynamic_symbols(market, limit=5)` returns a "fırsat avcılığı" pick of
  the most-tradable symbols right now:
    Crypto    : top USDT perp pairs by 24h quote volume.
    Equities  : highest absolute % move on the day from a curated index pool
                (BIST 30 / S&P 500 mega-caps / NASDAQ-100 mega-caps).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
from binance import AsyncClient

from config import settings
from core.logger import get_logger
from models import MarketType, Term

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Term ↔ interval mapping
# ---------------------------------------------------------------------------

TERM_INTERVALS: dict[Term, list[str]] = {
    # Include the rule engine's confirmation timeframe in every bundle.
    # Without these, otherwise-valid setups are forced to WAIT by
    # `_confirm_direction(..., confirm_interval=...)`.
    Term.SCALP: ["5m", "15m", "1h"],
    Term.SHORT_TERM: ["1h", "4h", "1d"],
    Term.MID_TERM: ["1d"],
}


def intervals_for(term: Term) -> list[str]:
    return TERM_INTERVALS[term]


# ---------------------------------------------------------------------------
# Dynamic TA-Lib lookback windows.
# ---------------------------------------------------------------------------

INDICATOR_LOOKBACKS: dict[str, dict[str, int | tuple[int, int, int]]] = {
    "5m":  {"rsi": 9,  "macd": (8, 21, 5),  "bbands": 20, "atr": 14, "ema_fast": 21,  "ema_slow": 100},
    "15m": {"rsi": 14, "macd": (12, 26, 9), "bbands": 20, "atr": 14, "ema_fast": 50,  "ema_slow": 200},
    "1h":  {"rsi": 14, "macd": (12, 26, 9), "bbands": 20, "atr": 14, "ema_fast": 50,  "ema_slow": 200},
    "4h":  {"rsi": 14, "macd": (12, 26, 9), "bbands": 20, "atr": 14, "ema_fast": 50,  "ema_slow": 200},
    "1d":  {"rsi": 14, "macd": (12, 26, 9), "bbands": 20, "atr": 14, "ema_fast": 50,  "ema_slow": 200},
}


def lookbacks_for(interval: str) -> dict:
    return INDICATOR_LOOKBACKS.get(interval, INDICATOR_LOOKBACKS["1h"])


# ---------------------------------------------------------------------------
# Static screening pools for traditional markets.
# Curated mega-cap subsets — full S&P 500 (500 names) is too slow for a free
# yfinance bulk download. These cover the main flow that drives the index.
# ---------------------------------------------------------------------------

# KOZAL.IS / KOZAA.IS removed 2026-06-11 — Yahoo returns 404/"possibly
# delisted" for both, polluting every BIST cycle's logs with fetch errors.
BIST_POOL: list[str] = [
    "THYAO.IS", "ASELS.IS", "GARAN.IS", "ISCTR.IS", "AKBNK.IS",
    "YKBNK.IS", "KCHOL.IS", "SAHOL.IS", "EREGL.IS", "BIMAS.IS",
    "FROTO.IS", "TUPRS.IS", "ARCLK.IS", "SISE.IS",
    "PETKM.IS", "TCELL.IS", "EKGYO.IS", "HEKTS.IS", "KRDMD.IS",
    "ENJSA.IS", "AEFES.IS", "MGROS.IS", "TKFEN.IS",
    "TOASO.IS", "VESTL.IS", "PGSUS.IS", "TAVHL.IS", "HALKB.IS",
]

SP500_POOL: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B",
    "LLY", "AVGO", "JPM", "TSLA", "V", "WMT", "UNH", "XOM",
    "MA", "JNJ", "COST", "PG", "ORCL", "HD", "MRK", "ABBV",
    "BAC", "NFLX", "CVX", "CRM", "AMD", "KO", "PEP",
]

NASDAQ_POOL: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO",
    "TSLA", "COST", "PEP", "ADBE", "NFLX", "AMD", "CSCO",
    "TMUS", "CMCSA", "INTC", "INTU", "QCOM", "TXN", "AMGN",
    "ISRG", "BKNG", "HON", "AMAT", "ADI", "LRCX", "GILD",
    "SBUX", "MU",
]

_POOLS: dict[MarketType, list[str]] = {
    MarketType.BIST: BIST_POOL,
    MarketType.SP500: SP500_POOL,
    MarketType.NASDAQ: NASDAQ_POOL,
}


# ---------------------------------------------------------------------------
# Binance adapter
# ---------------------------------------------------------------------------

_BINANCE_INTERVAL: dict[str, str] = {
    "5m":  AsyncClient.KLINE_INTERVAL_5MINUTE,
    "15m": AsyncClient.KLINE_INTERVAL_15MINUTE,
    "1h":  AsyncClient.KLINE_INTERVAL_1HOUR,
    "4h":  AsyncClient.KLINE_INTERVAL_4HOUR,
    "1d":  AsyncClient.KLINE_INTERVAL_1DAY,
}


class _BinanceAdapter:
    """Thin async wrapper around python-binance.

    Two clients are kept side-by-side:
      - `client()`       → respects `settings.binance_testnet`; used by the
                            executor for ORDER PLACEMENT (so paper trading
                            stays on testnet).
      - `read_client()`  → always mainnet, no API key required; used for
                            klines / 24h ticker / funding / OI. Testnet's
                            market data is sparse and untrustworthy (often
                            ~0 volume), which silently broke the rule
                            engine's volume gate; mainnet public endpoints
                            give real data while keeping order placement
                            sandboxed.
    """

    def __init__(self) -> None:
        self._client: AsyncClient | None = None
        self._read_client: AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()

    async def client(self) -> AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await AsyncClient.create(
                        api_key=settings.binance_api_key,
                        api_secret=settings.binance_api_secret,
                        testnet=settings.binance_testnet,
                    )
                    log.info("market.binance.connected", testnet=settings.binance_testnet)
        return self._client

    async def read_client(self) -> AsyncClient:
        """Mainnet read-only client. No API key — public endpoints only."""
        if self._read_client is None:
            async with self._read_lock:
                if self._read_client is None:
                    self._read_client = await AsyncClient.create(
                        api_key="", api_secret="", testnet=False,
                    )
                    log.info("market.binance.read_connected", testnet=False)
        return self._read_client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_connection()
            self._client = None
        if self._read_client is not None:
            await self._read_client.close_connection()
            self._read_client = None

    async def fetch(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        bn = _BINANCE_INTERVAL.get(interval)
        if bn is None:
            raise ValueError(f"binance: unsupported interval {interval!r}")
        c = await self.read_client()
        raw = await c.futures_klines(symbol=symbol, interval=bn, limit=limit)
        return [
            {
                "open_time":  int(r[0]),
                "open":       float(r[1]),
                "high":       float(r[2]),
                "low":        float(r[3]),
                "close":      float(r[4]),
                "volume":     float(r[5]),
                "close_time": int(r[6]),
            }
            for r in raw
        ]

    async def top_by_volume(
        self, limit: int, *, quote: str = "USDT", min_quote_volume: float = 1e7
    ) -> list[str]:
        """24h ticker stats → top USDT perpetuals by quote volume."""
        c = await self.read_client()
        tickers = await c.futures_ticker()
        usdt = []
        for t in tickers:
            sym = t.get("symbol", "")
            # Standard USDT perpetuals only (skip BUSD, USDC pairs and dated futures)
            if not sym.endswith(quote):
                continue
            if "_" in sym:  # filters dated futures like BTCUSDT_240329
                continue
            try:
                qv = float(t.get("quoteVolume", 0) or 0)
            except (TypeError, ValueError):
                continue
            if qv < min_quote_volume:
                continue
            usdt.append((sym, qv))
        usdt.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in usdt[:limit]]

    # -- Microstructure: funding rate + open interest -----------------------

    async def funding_rate(self, symbol: str) -> dict | None:
        """Most recent funding rate for a Binance USDT-margined perp.

        Returns:
            {
              "symbol":          str,
              "funding_rate":    float,   # 8h funding rate, e.g. 0.0001 = 0.01%
              "annualized_pct":  float,   # rate × 3 × 365 × 100
              "funding_time_ms": int
            }
        """
        try:
            c = await self.read_client()
            rows = await c.futures_funding_rate(symbol=symbol, limit=1)
            if not rows:
                return None
            r = rows[0]
            rate = float(r.get("fundingRate", 0) or 0)
            return {
                "symbol": symbol,
                "funding_rate": rate,
                "annualized_pct": rate * 3 * 365 * 100,
                "funding_time_ms": int(r.get("fundingTime", 0) or 0),
            }
        except Exception as e:
            log.warning("market.funding_rate_failed", symbol=symbol, err=str(e))
            return None

    async def open_interest(self, symbol: str) -> dict | None:
        """Current open interest in BASE units for a Binance perp.

        Returns:
            {
              "symbol":               str,
              "open_interest_base":   float,   # contracts, in BASE units
              "open_interest_usd":    float,   # base × mark_price (quote-denominated)
              "mark_price":           float,
              "as_of_ms":             int
            }
        """
        try:
            c = await self.read_client()
            oi = await c.futures_open_interest(symbol=symbol)
            base = float(oi.get("openInterest", 0) or 0)
            mark = await c.futures_mark_price(symbol=symbol)
            mark_price = float(mark.get("markPrice", 0) or 0)
            return {
                "symbol": symbol,
                "open_interest_base": base,
                "open_interest_usd": base * mark_price,
                "mark_price": mark_price,
                "as_of_ms": int(oi.get("time", 0) or 0),
            }
        except Exception as e:
            log.warning("market.open_interest_failed", symbol=symbol, err=str(e))
            return None


# ---------------------------------------------------------------------------
# yfinance adapter — for BIST / S&P 500 / NASDAQ
# ---------------------------------------------------------------------------

_YF_INTERVAL: dict[str, str] = {
    "5m":  "5m",
    "15m": "15m",
    "1h":  "60m",
    "1d":  "1d",
}

_YF_PERIOD: dict[str, str] = {
    "5m":  "5d",
    "15m": "1mo",
    "1h":  "2mo",
    "4h":  "6mo",
    "1d":  "2y",
}


def _yf_dataframe_to_candles(df: "pd.DataFrame") -> list[dict]:
    if df is None or df.empty:
        return []
    df = df.dropna(how="any").copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize(timezone.utc)
    out: list[dict] = []
    for ts, row in df.iterrows():
        ms = int(ts.timestamp() * 1000)
        out.append(
            {
                "open_time":  ms,
                "open":       float(row["Open"]),
                "high":       float(row["High"]),
                "low":        float(row["Low"]),
                "close":      float(row["Close"]),
                "volume":     float(row.get("Volume", 0) or 0),
                "close_time": ms,
            }
        )
    return out


def _resample_to_4h(df_1h: "pd.DataFrame") -> "pd.DataFrame":
    # pandas 2.2+ deprecated uppercase frequency aliases — "4H" → "4h".
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return df_1h.resample("4h", label="right", closed="right").agg(agg).dropna()


def _yfinance_fetch_sync(symbol: str, interval: str) -> list[dict]:
    import yfinance as yf

    if interval == "4h":
        period = _YF_PERIOD["4h"]
        raw = yf.download(
            tickers=symbol, period=period, interval="60m",
            progress=False, auto_adjust=False, threads=False,
        )
        if raw is None or raw.empty:
            return []
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        return _yf_dataframe_to_candles(_resample_to_4h(raw))

    yf_iv = _YF_INTERVAL.get(interval)
    if yf_iv is None:
        raise ValueError(f"yfinance: unsupported interval {interval!r}")
    period = _YF_PERIOD[interval]
    raw = yf.download(
        tickers=symbol, period=period, interval=yf_iv,
        progress=False, auto_adjust=False, threads=False,
    )
    if raw is None or raw.empty:
        return []
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return _yf_dataframe_to_candles(raw)


def _yfinance_screener_sync(pool: list[str], limit: int) -> list[str]:
    """Bulk-fetch 2 daily bars for the entire pool, sort by abs % change.

    Fallback for ties / missing data: keeps the order from the input pool
    (so curated mega-caps win against thinly-traded mid-caps).
    """
    import yfinance as yf

    if not pool:
        return []
    tickers_str = " ".join(pool)
    df = yf.download(
        tickers=tickers_str,
        period="5d",
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=True,
        group_by="ticker",
    )
    if df is None or df.empty:
        return pool[:limit]

    movers: list[tuple[str, float]] = []
    for sym in pool:
        try:
            sub = df[sym] if sym in df.columns.get_level_values(0) else df
            closes = sub["Close"].dropna()
            if len(closes) < 2:
                continue
            prev, last = float(closes.iloc[-2]), float(closes.iloc[-1])
            if prev <= 0:
                continue
            pct = (last - prev) / prev
            movers.append((sym, abs(pct)))
        except Exception:
            continue

    movers.sort(key=lambda x: x[1], reverse=True)
    picks = [s for s, _ in movers[:limit]]
    # Pad from the curated pool if screening produced fewer than `limit`
    if len(picks) < limit:
        for s in pool:
            if s not in picks:
                picks.append(s)
            if len(picks) >= limit:
                break
    return picks[:limit]


# ---------------------------------------------------------------------------
# Public façade
# ---------------------------------------------------------------------------

class MarketFetcher:
    """One fetcher to rule them all.

    Usage:
        mf = MarketFetcher()
        candles = await mf.fetch_ohlcv("BTCUSDT", MarketType.CRYPTO, "15m")
        bundle  = await mf.fetch_term("THYAO.IS", MarketType.BIST, Term.SHORT_TERM)
        picks   = await mf.get_dynamic_symbols(MarketType.CRYPTO, limit=5)
    """

    def __init__(self) -> None:
        self._binance = _BinanceAdapter()

    async def close(self) -> None:
        await self._binance.close()

    # -- single interval -----------------------------------------------------

    async def fetch_ohlcv(
        self,
        symbol: str,
        market: MarketType,
        interval: str,
        *,
        limit: int = 300,
    ) -> list[dict]:
        if market == MarketType.CRYPTO:
            return await self._binance.fetch(symbol, interval, limit=limit)
        return await asyncio.to_thread(_yfinance_fetch_sync, symbol, interval)

    async def fetch_last_price(self, symbol: str, market: MarketType) -> float | None:
        if market == MarketType.CRYPTO:
            client = await self._binance.client()
            data = await client.futures_mark_price(symbol=symbol)
            return float(data["markPrice"])
        candles = await self.fetch_ohlcv(symbol, market, "1d", limit=1)
        return candles[-1]["close"] if candles else None

    # -- Microstructure (façade) --------------------------------------------

    async def fetch_funding_rate(self, symbol: str) -> dict | None:
        """Crypto-only — most recent 8h funding rate for `symbol`.

        Returns None for non-crypto symbols so the orchestrator can short-
        circuit cleanly.
        """
        if not symbol.endswith("USDT"):
            return None
        return await self._binance.funding_rate(symbol)

    async def fetch_open_interest(self, symbol: str) -> dict | None:
        """Crypto-only — current open interest (base + USD-denominated)."""
        if not symbol.endswith("USDT"):
            return None
        return await self._binance.open_interest(symbol)

    async def fetch_usdtry(self) -> dict | None:
        """USD/TRY daily rate via yfinance — drives the BIST macro overlay.

        Returns:
            {
              "rate":          float,    # latest USDTRY close
              "prev_rate":     float,    # prior daily close
              "pct_change_1d": float,    # (rate - prev) / prev
              "as_of_ms":      int
            }

        The BIST rulebook §2 ("TL macro overlay") is the single most-load-
        bearing concept for Turkish equities — a TL weakness day implies
        export-heavy industrials (THYAO/TUPRS/FROTO/EREGL) are translation-
        gain candidates regardless of pure technicals.
        """
        try:
            candles = await asyncio.to_thread(
                _yfinance_fetch_sync, "USDTRY=X", "1d"
            )
            if not candles or len(candles) < 2:
                return None
            last = candles[-1]
            prev = candles[-2]
            prev_close = float(prev["close"])
            last_close = float(last["close"])
            if prev_close <= 0:
                return None
            return {
                "rate": last_close,
                "prev_rate": prev_close,
                "pct_change_1d": (last_close - prev_close) / prev_close,
                "as_of_ms": int(last["close_time"]),
            }
        except Exception as e:
            log.warning("market.usdtry_failed", err=str(e))
            return None

    # -- bundle by Term ------------------------------------------------------

    async def fetch_term(
        self,
        symbol: str,
        market: MarketType,
        term: Term,
        *,
        limit: int = 300,
    ) -> dict[str, list[dict]]:
        intervals = intervals_for(term)
        tasks = [
            asyncio.create_task(self.fetch_ohlcv(symbol, market, iv, limit=limit))
            for iv in intervals
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        bundle: dict[str, list[dict]] = {}
        for iv, res in zip(intervals, results):
            if isinstance(res, Exception):
                log.warning(
                    "market.fetch_failed",
                    symbol=symbol, market=market.value, interval=iv, err=str(res),
                )
                bundle[iv] = []
            else:
                bundle[iv] = res

        log.info(
            "market.fetch_term",
            symbol=symbol, market=market.value, term=term.value,
            counts={iv: len(c) for iv, c in bundle.items()},
        )
        return bundle

    # -- dynamic screener ----------------------------------------------------

    async def get_dynamic_symbols(
        self, market: MarketType, *, limit: int = 5
    ) -> list[str]:
        """Return the top-`limit` "opportunity" symbols right now.

        Crypto    : top USDT perp pairs by 24h quote volume.
        Equities  : top movers by absolute daily % change from a curated
                    index pool — keeps the screen on liquid, well-known names.

        Falls back to a sensible default list on any error so the orchestrator
        never gets an empty universe.
        """
        try:
            if market == MarketType.CRYPTO:
                picks = await self._binance.top_by_volume(limit=limit)
                if not picks:
                    raise RuntimeError("binance ticker returned no USDT pairs")
                log.info(
                    "screener.crypto", picks=picks, by="24h_quote_volume"
                )
                return picks

            pool = _POOLS.get(market)
            if not pool:
                log.warning("screener.no_pool", market=market.value)
                return []
            picks = await asyncio.to_thread(_yfinance_screener_sync, pool, limit)
            log.info(
                "screener.equity", market=market.value, picks=picks,
                by="abs_daily_pct_change",
            )
            return picks
        except Exception as e:
            log.exception("screener.failed", market=market.value, err=str(e))
            # Fall back to the head of the static pool (or a Binance default)
            if market == MarketType.CRYPTO:
                return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"][:limit]
            return (_POOLS.get(market) or [])[:limit]


# Module-level singleton for convenient import
fetcher = MarketFetcher()


__all__ = [
    "MarketFetcher",
    "fetcher",
    "TERM_INTERVALS",
    "INDICATOR_LOOKBACKS",
    "intervals_for",
    "lookbacks_for",
    "BIST_POOL",
    "SP500_POOL",
    "NASDAQ_POOL",
]
