"""TA-Lib indicator computation with timeframe-aware lookbacks.

Computes the full indicator stack needed by the literature-grounded rule
engine (see RESEARCH_ALGORITHM.md §3 + §9):

  - RSI (Wilder, 14)              — overbought / oversold (Wilder 1978)
  - RSI(2)                        — Connors short-term mean-reversion signal
  - MACD (12,26,9)                — momentum
  - Bollinger (20, 2σ)            — vol regime + mean-reversion bands
  - ATR (Wilder, 14)              — vol-aware risk sizing (Wilder 1978)
  - ADX + ±DI (Wilder, 14)        — trend strength regime (Wilder 1978)
  - EMA fast / EMA slow           — trend filter (BLL 1992)
  - Volume SMA (20) + rel_volume  — confirmation gate (Karpoff 1987)

`lookbacks` lets the caller override any period per active interval — the
orchestrator passes the dict from `market_fetcher.lookbacks_for(iv)`.
"""
from __future__ import annotations

import time
from typing import Any, List, Sequence

import numpy as np
import talib


# ---------------------------------------------------------------------------
# Defaults — used if caller passes no `lookbacks`
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACKS: dict[str, Any] = {
    "rsi": 14,                # Wilder
    "rsi_short": 2,           # Connors RSI(2) — always also computed
    "macd": (12, 26, 9),
    "bbands": 20,
    "atr": 14,                # Wilder
    "adx": 14,                # Wilder (DMI)
    "ema_fast": 50,
    "ema_slow": 200,
    "volume_ma": 20,
}


def _merge_lookbacks(lookbacks: dict[str, Any] | None) -> dict[str, Any]:
    if not lookbacks:
        return dict(DEFAULT_LOOKBACKS)
    merged = dict(DEFAULT_LOOKBACKS)
    merged.update(lookbacks)
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
    return [float(v) if not np.isnan(v) else None for v in arr[-n:]]


def _min_required_bars(lb: dict[str, Any]) -> int:
    macd_slow = lb["macd"][1] if isinstance(lb["macd"], (tuple, list)) else 26
    macd_signal = lb["macd"][2] if isinstance(lb["macd"], (tuple, list)) else 9
    return max(
        int(lb["rsi"]),
        int(lb["rsi_short"]),
        int(macd_slow) + int(macd_signal),
        int(lb["bbands"]),
        int(lb["atr"]),
        int(lb["adx"]) * 2,  # ADX needs ~2× period to stabilize
        int(lb["ema_fast"]),
        min(int(lb["ema_slow"]), 50),
        int(lb["volume_ma"]),
    )


def compute_indicators(
    candles: Sequence[dict],
    lookbacks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the full RSI / MACD / Bollinger / ATR / ADX / EMA / Volume stack.

    Returns the latest value of every indicator plus recent series tails
    (last 20 points) so the rule engine and LLM can reason about momentum,
    not just levels.

    Returns {"error": "insufficient_data", ...} when the candle history is
    too short for the slowest indicator to produce a value.
    """
    lb = _merge_lookbacks(lookbacks)
    min_bars = _min_required_bars(lb)

    # Drop the still-open last bar so indicators reflect closed-bar state.
    # Why: Binance returns the currently-forming bar with a `close_time` in
    # the future and a partial `volume`. Computing rel_volume = last_vol /
    # SMA(20, vol) on that bar produces ~0, which trips the volume gate on
    # every cycle and is the root cause of "0 signals for a week".
    if candles:
        last_close_ms = candles[-1].get("close_time")
        if last_close_ms is not None and last_close_ms > int(time.time() * 1000):
            candles = candles[:-1]

    if len(candles) < min_bars:
        return {"error": "insufficient_data", "n": len(candles), "min_required": min_bars}

    high = np.array([c["high"] for c in candles], dtype=np.float64)
    low = np.array([c["low"] for c in candles], dtype=np.float64)
    close = np.array([c["close"] for c in candles], dtype=np.float64)
    volume = np.array([c.get("volume", 0) or 0 for c in candles], dtype=np.float64)

    # ─── RSI(14) + RSI(2) ────────────────────────────────────────────────
    rsi = talib.RSI(close, timeperiod=int(lb["rsi"]))
    rsi_short = talib.RSI(close, timeperiod=int(lb["rsi_short"]))

    # ─── MACD ────────────────────────────────────────────────────────────
    macd_fast, macd_slow, macd_signal = lb["macd"]
    macd, macd_sig, macd_hist = talib.MACD(
        close,
        fastperiod=int(macd_fast),
        slowperiod=int(macd_slow),
        signalperiod=int(macd_signal),
    )

    # ─── Bollinger Bands ─────────────────────────────────────────────────
    bb_upper, bb_middle, bb_lower = talib.BBANDS(
        close, timeperiod=int(lb["bbands"]), nbdevup=2, nbdevdn=2
    )

    # ─── ATR ─────────────────────────────────────────────────────────────
    atr = talib.ATR(high, low, close, timeperiod=int(lb["atr"]))

    # ─── ADX + DI± (regime detector) ─────────────────────────────────────
    adx = talib.ADX(high, low, close, timeperiod=int(lb["adx"]))
    plus_di = talib.PLUS_DI(high, low, close, timeperiod=int(lb["adx"]))
    minus_di = talib.MINUS_DI(high, low, close, timeperiod=int(lb["adx"]))

    # ─── EMAs ────────────────────────────────────────────────────────────
    ema_fast = talib.EMA(close, timeperiod=int(lb["ema_fast"]))
    ema_slow_period = min(int(lb["ema_slow"]), max(2, len(close) - 1))
    ema_slow = talib.EMA(close, timeperiod=ema_slow_period)

    # ─── Volume MA + relative volume ─────────────────────────────────────
    vol_ma = talib.SMA(volume, timeperiod=int(lb["volume_ma"]))
    last_vol = _last(volume)
    last_vol_ma = _last(vol_ma)
    rel_volume = (last_vol / last_vol_ma) if (last_vol and last_vol_ma) else None

    # ─── Derived ─────────────────────────────────────────────────────────
    last_close = float(close[-1])

    bb_pos = None
    bbu, bbl = _last(bb_upper), _last(bb_lower)
    if bbu is not None and bbl is not None:
        rng = bbu - bbl
        if rng > 0:
            bb_pos = (last_close - bbl) / rng

    # Trend regime label (per Wilder + practitioner convention):
    #   ADX > 25 → trending
    #   ADX < 20 → ranging
    #   20 ≤ ADX ≤ 25 → transitional
    adx_last = _last(adx)
    if adx_last is None:
        adx_regime = None
    elif adx_last > 25:
        adx_regime = "trending"
    elif adx_last < 20:
        adx_regime = "ranging"
    else:
        adx_regime = "transitional"

    return {
        "lookbacks_used": {
            "rsi": int(lb["rsi"]),
            "rsi_short": int(lb["rsi_short"]),
            "macd": [int(macd_fast), int(macd_slow), int(macd_signal)],
            "bbands": int(lb["bbands"]),
            "atr": int(lb["atr"]),
            "adx": int(lb["adx"]),
            "ema_fast": int(lb["ema_fast"]),
            "ema_slow": int(ema_slow_period),
            "volume_ma": int(lb["volume_ma"]),
        },
        "last_close": last_close,
        # RSI
        "rsi": _last(rsi),
        "rsi_short": _last(rsi_short),
        # MACD
        "macd": _last(macd),
        "macd_signal": _last(macd_sig),
        "macd_hist": _last(macd_hist),
        # Bollinger
        "bb_upper": _last(bb_upper),
        "bb_middle": _last(bb_middle),
        "bb_lower": _last(bb_lower),
        "bb_position": bb_pos,
        # ATR
        "atr": _last(atr),
        "atr_pct": (_last(atr) / last_close) if _last(atr) else None,
        # ADX (NEW)
        "adx": adx_last,
        "plus_di": _last(plus_di),
        "minus_di": _last(minus_di),
        "adx_regime": adx_regime,
        # EMA
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
        # Volume (NEW)
        "volume_last": last_vol,
        "volume_ma": last_vol_ma,
        "rel_volume": rel_volume,
        # Series tails
        "series": {
            "close": _tail(close),
            "rsi": _tail(rsi),
            "macd_hist": _tail(macd_hist),
            "adx": _tail(adx),
        },
    }


def compute_indicator_series(
    candles: Sequence[dict],
    lookbacks: dict[str, Any] | None = None,
) -> list[dict[str, Any] | None]:
    """Full-history indicator computation — ONE talib pass, per-bar dicts out.

    The walk-forward backtest previously called `compute_indicators(window)`
    for every bar, recomputing every talib series over the whole expanding
    window each time — O(T²), which made deep low-TF sweeps (15m over months)
    take days. talib is designed for full-series computation: RSI[t], ATR[t],
    etc. each use only data ≤ t (strictly causal), so we can compute the
    entire series once and index per bar — O(T).

    Returns a list aligned 1:1 with `candles`; element `i` is the indicator
    dict the rule engine reads at bar `i`, or None during the warmup window
    (where the slowest indicator is still NaN). Field set matches the keys
    `generate_rule_decision` consumes.
    """
    lb = _merge_lookbacks(lookbacks)
    n = len(candles)
    min_bars = _min_required_bars(lb)
    if n < min_bars:
        return [None] * n

    high = np.array([c["high"] for c in candles], dtype=np.float64)
    low = np.array([c["low"] for c in candles], dtype=np.float64)
    close = np.array([c["close"] for c in candles], dtype=np.float64)
    volume = np.array([c.get("volume", 0) or 0 for c in candles], dtype=np.float64)

    rsi = talib.RSI(close, timeperiod=int(lb["rsi"]))
    rsi_short = talib.RSI(close, timeperiod=int(lb["rsi_short"]))
    macd_fast, macd_slow, macd_signal = lb["macd"]
    _macd, _sig, macd_hist = talib.MACD(
        close, fastperiod=int(macd_fast), slowperiod=int(macd_slow),
        signalperiod=int(macd_signal),
    )
    bb_upper, _bb_mid, bb_lower = talib.BBANDS(
        close, timeperiod=int(lb["bbands"]), nbdevup=2, nbdevdn=2
    )
    atr = talib.ATR(high, low, close, timeperiod=int(lb["atr"]))
    adx = talib.ADX(high, low, close, timeperiod=int(lb["adx"]))
    plus_di = talib.PLUS_DI(high, low, close, timeperiod=int(lb["adx"]))
    minus_di = talib.MINUS_DI(high, low, close, timeperiod=int(lb["adx"]))
    ema_fast = talib.EMA(close, timeperiod=int(lb["ema_fast"]))
    ema_slow_period = min(int(lb["ema_slow"]), max(2, n - 1))
    ema_slow = talib.EMA(close, timeperiod=ema_slow_period)
    vol_ma = talib.SMA(volume, timeperiod=int(lb["volume_ma"]))

    def _v(arr: np.ndarray, i: int) -> float | None:
        x = arr[i]
        return float(x) if not np.isnan(x) else None

    out: list[dict[str, Any] | None] = [None] * n
    for i in range(n):
        atr_i = _v(atr, i)
        adx_i = _v(adx, i)
        bbu, bbl = _v(bb_upper, i), _v(bb_lower, i)
        ema_s = _v(ema_slow, i)
        last_close = float(close[i])

        bb_pos = None
        if bbu is not None and bbl is not None and (bbu - bbl) > 0:
            bb_pos = (last_close - bbl) / (bbu - bbl)

        if adx_i is None:
            adx_regime = None
        elif adx_i > 25:
            adx_regime = "trending"
        elif adx_i < 20:
            adx_regime = "ranging"
        else:
            adx_regime = "transitional"

        vma = _v(vol_ma, i)
        rel_vol = (float(volume[i]) / vma) if (vma and volume[i]) else None

        out[i] = {
            "last_close": last_close,
            "rsi": _v(rsi, i),
            "rsi_short": _v(rsi_short, i),
            "macd_hist": _v(macd_hist, i),
            "bb_position": bb_pos,
            "atr": atr_i,
            "atr_pct": (atr_i / last_close) if atr_i else None,
            "adx": adx_i,
            "plus_di": _v(plus_di, i),
            "minus_di": _v(minus_di, i),
            "adx_regime": adx_regime,
            "ema_fast": _v(ema_fast, i),
            "ema_slow": ema_s,
            "above_ema_slow": (last_close > ema_s) if ema_s is not None else None,
            "rel_volume": rel_vol,
        }
    return out


__all__ = ["compute_indicators", "compute_indicator_series", "DEFAULT_LOOKBACKS"]
