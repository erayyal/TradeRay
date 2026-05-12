"""TA-Lib indicator computation with timeframe-aware lookbacks.

`compute_indicators(candles, lookbacks=None)` accepts a `lookbacks` dict so the
caller (orchestrator / market_fetcher) can scale RSI/MACD/BB/ATR/EMA periods
to the active interval — e.g. RSI(9) on 5m vs RSI(14) on 4h+.

Lookback dict shape (all keys optional; defaults below fill missing values):
    {
        "rsi":      14,                 # int
        "macd":     (12, 26, 9),        # (fast, slow, signal)
        "bbands":   20,                 # int
        "atr":      14,                 # int
        "ema_fast": 50,                 # int
        "ema_slow": 200,                # int
    }
"""
from __future__ import annotations

from typing import Any, List, Sequence

import numpy as np
import talib


# ---------------------------------------------------------------------------
# Defaults — used if caller passes no `lookbacks` (preserves prior behavior)
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACKS: dict[str, Any] = {
    "rsi": 14,
    "macd": (12, 26, 9),
    "bbands": 20,
    "atr": 14,
    "ema_fast": 50,
    "ema_slow": 200,
}


def _merge_lookbacks(lookbacks: dict[str, Any] | None) -> dict[str, Any]:
    """Fill any missing keys from DEFAULT_LOOKBACKS without mutating the input."""
    if not lookbacks:
        return dict(DEFAULT_LOOKBACKS)
    merged = dict(DEFAULT_LOOKBACKS)
    merged.update(lookbacks)
    # Coerce macd to tuple if a list slipped in (JSON round-trip)
    if isinstance(merged["macd"], list):
        merged["macd"] = tuple(merged["macd"])
    return merged


def _last(arr: np.ndarray) -> float | None:
    if arr.size == 0:
        return None
    v = arr[-1]
    return float(v) if not np.isnan(v) else None


def _tail(arr: np.ndarray, n: int = 20) -> List[float | None]:
    if arr.size == 0:
        return []
    out: List[float | None] = []
    for v in arr[-n:]:
        out.append(float(v) if not np.isnan(v) else None)
    return out


def _min_required_bars(lb: dict[str, Any]) -> int:
    """Minimum candles needed for the slowest indicator to produce a value."""
    macd_slow = lb["macd"][1] if isinstance(lb["macd"], (tuple, list)) else 26
    return max(
        int(lb["rsi"]),
        int(macd_slow) + int(lb["macd"][2] if isinstance(lb["macd"], (tuple, list)) else 9),
        int(lb["bbands"]),
        int(lb["atr"]),
        int(lb["ema_fast"]),
        # ema_slow is allowed to be larger than the candle history — we shrink it below
        min(int(lb["ema_slow"]), 50),
    )


def compute_indicators(
    candles: Sequence[dict],
    lookbacks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute RSI / MACD / Bollinger / ATR / EMA snapshots from OHLCV.

    Returns a dict of latest values + the recent series tail (last 20 points)
    so the LLM can reason about momentum, not just levels.

    Args:
        candles: list of {open_time, open, high, low, close, volume, close_time}.
        lookbacks: optional dict overriding any subset of DEFAULT_LOOKBACKS.

    Returns:
        - On success: a dict matching the schema below.
        - On insufficient data: {"error": "insufficient_data", "n": <count>}.
    """
    lb = _merge_lookbacks(lookbacks)
    min_bars = _min_required_bars(lb)

    if len(candles) < min_bars:
        return {"error": "insufficient_data", "n": len(candles), "min_required": min_bars}

    high = np.array([c["high"] for c in candles], dtype=np.float64)
    low = np.array([c["low"] for c in candles], dtype=np.float64)
    close = np.array([c["close"] for c in candles], dtype=np.float64)

    # Indicators ------------------------------------------------------------
    rsi = talib.RSI(close, timeperiod=int(lb["rsi"]))

    macd_fast, macd_slow, macd_signal = lb["macd"]
    macd, macd_sig, macd_hist = talib.MACD(
        close,
        fastperiod=int(macd_fast),
        slowperiod=int(macd_slow),
        signalperiod=int(macd_signal),
    )

    bb_upper, bb_middle, bb_lower = talib.BBANDS(
        close, timeperiod=int(lb["bbands"]), nbdevup=2, nbdevdn=2
    )

    atr = talib.ATR(high, low, close, timeperiod=int(lb["atr"]))

    ema_fast = talib.EMA(close, timeperiod=int(lb["ema_fast"]))
    # ema_slow is best-effort: if user asked for EMA(200) but we only have
    # 120 bars, shrink to len-1 so we still emit a value rather than NaN.
    ema_slow_period = min(int(lb["ema_slow"]), max(2, len(close) - 1))
    ema_slow = talib.EMA(close, timeperiod=ema_slow_period)

    last_close = float(close[-1])

    # Bollinger position: 0 = at lower band, 1 = at upper band
    bb_pos = None
    bbu, bbl = _last(bb_upper), _last(bb_lower)
    if bbu is not None and bbl is not None:
        rng = bbu - bbl
        if rng > 0:
            bb_pos = (last_close - bbl) / rng

    return {
        "lookbacks_used": {
            "rsi": int(lb["rsi"]),
            "macd": [int(macd_fast), int(macd_slow), int(macd_signal)],
            "bbands": int(lb["bbands"]),
            "atr": int(lb["atr"]),
            "ema_fast": int(lb["ema_fast"]),
            "ema_slow": int(ema_slow_period),
        },
        "last_close": last_close,
        "rsi": _last(rsi),
        "macd": _last(macd),
        "macd_signal": _last(macd_sig),
        "macd_hist": _last(macd_hist),
        "bb_upper": _last(bb_upper),
        "bb_middle": _last(bb_middle),
        "bb_lower": _last(bb_lower),
        "bb_position": bb_pos,
        "atr": _last(atr),
        "atr_pct": (_last(atr) / last_close) if _last(atr) else None,
        "ema_fast": _last(ema_fast),
        "ema_slow": _last(ema_slow),
        "above_ema_slow": (
            last_close > _last(ema_slow) if _last(ema_slow) is not None else None
        ),
        "ema_cross": (
            (_last(ema_fast) > _last(ema_slow))
            if _last(ema_fast) is not None and _last(ema_slow) is not None
            else None
        ),
        "series": {
            "close": _tail(close),
            "rsi": _tail(rsi),
            "macd_hist": _tail(macd_hist),
        },
    }


__all__ = ["compute_indicators", "DEFAULT_LOOKBACKS"]
