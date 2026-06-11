"""Hermetic tests for v3.0 exit engineering + HMM regime module.

No network, no DB — pure logic on synthetic candles/returns.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from backtest.walk_forward import Trade, _resolve_forward
from data_fetchers.regime import annotate_regime, fit_hmm, filtered_p_high


def _bar(o, h, l, c, t=0):
    return {"open": o, "high": h, "low": l, "close": c, "close_time": t}


def _long_trade(entry=100.0, tp=110.0, sl=95.0):
    return Trade(
        direction="LONG", entry_idx=0, entry_time_ms=0,
        entry=entry, take_profit=tp, stop_loss=sl,
    )


# ---------------------------------------------------------------------------
# Breakeven
# ---------------------------------------------------------------------------

class TestBreakeven:
    def test_be_arms_next_bar_not_same_bar(self):
        # Bar 1 touches +1R (105) AND falls back through entry to 99 — the
        # original SL (95) is NOT hit. Same-bar arming would stop us at 100;
        # next-bar arming keeps us in. Bar 2 then hits entry → BE exit.
        candles = [
            _bar(100, 100, 100, 100, 0),
            _bar(100, 105.5, 99, 99.5, 1),   # trigger touched, falls back
            _bar(99.5, 101, 99.9, 100.5, 2), # low 99.9 < entry 100 → BE stop
        ]
        t = _resolve_forward(_long_trade(), candles, 1, breakeven_at_r=1.0)
        assert t.outcome == "BE"
        assert t.exit_idx == 2
        assert t.r_multiple == 0.0

    def test_sl_beats_be_trigger_same_bar(self):
        # One huge bar hits both the BE trigger AND the original SL —
        # conservative ordering credits the SL.
        candles = [
            _bar(100, 100, 100, 100, 0),
            _bar(100, 106, 94, 95, 1),
        ]
        t = _resolve_forward(_long_trade(), candles, 1, breakeven_at_r=1.0)
        assert t.outcome == "SL"
        assert t.r_multiple == pytest.approx(-1.0)

    def test_tp_still_wins_after_arming(self):
        candles = [
            _bar(100, 100, 100, 100, 0),
            _bar(100, 105.5, 100, 105, 1),    # arms BE
            _bar(105, 111, 104, 110, 2),      # TP 110 hit (low 104 > entry)
        ]
        t = _resolve_forward(_long_trade(), candles, 1, breakeven_at_r=1.0)
        assert t.outcome == "TP"
        assert t.r_multiple == pytest.approx(2.0)

    def test_short_mirror(self):
        trade = Trade(
            direction="SHORT", entry_idx=0, entry_time_ms=0,
            entry=100.0, take_profit=90.0, stop_loss=105.0,
        )
        candles = [
            _bar(100, 100, 100, 100, 0),
            _bar(100, 100.5, 94.5, 96, 1),    # -1R = 95 touched → arms
            _bar(96, 100.2, 96, 100.1, 2),    # high ≥ entry → BE
        ]
        t = _resolve_forward(trade, candles, 1, breakeven_at_r=1.0)
        assert t.outcome == "BE"


# ---------------------------------------------------------------------------
# Time barrier
# ---------------------------------------------------------------------------

class TestTimeExit:
    def test_time_exit_at_close(self):
        candles = [_bar(100, 101, 99, 100 + i * 0.1, i) for i in range(10)]
        t = _resolve_forward(
            _long_trade(tp=200, sl=50), candles, 1, max_holding_bars=5,
        )
        assert t.outcome == "TIME"
        assert t.exit_idx == 5
        # r = (close@5 − 100) / 50
        assert t.r_multiple == pytest.approx((candles[5]["close"] - 100) / 50)

    def test_sl_tp_beat_time_same_bar(self):
        candles = [_bar(100, 101, 99, 100, i) for i in range(6)]
        candles[5] = _bar(100, 111, 99, 110, 5)   # TP hits on the deadline bar
        t = _resolve_forward(
            _long_trade(), candles, 1, max_holding_bars=5,
        )
        assert t.outcome == "TP"

    def test_no_exit_stays_open(self):
        candles = [_bar(100, 101, 99, 100, i) for i in range(4)]
        t = _resolve_forward(_long_trade(), candles, 1)
        assert t.outcome == "OPEN"


# ---------------------------------------------------------------------------
# HMM regime
# ---------------------------------------------------------------------------

def _synthetic_two_regime(seed=11, n_low=200, n_high=120):
    rng = np.random.default_rng(seed)
    low = rng.normal(0.0, 0.005, n_low)
    high = rng.normal(0.0, 0.04, n_high)
    return np.concatenate([low, high])


class TestHMM:
    def test_fit_separates_variances(self):
        rets = _synthetic_two_regime()
        pi, A, means, vars_ = fit_hmm(rets)
        assert vars_[1] > vars_[0] * 5          # high-vol state clearly wider
        assert 0.5 <= A[0, 0] <= 1.0 and 0.5 <= A[1, 1] <= 1.0

    def test_filtered_prob_tracks_regime_shift(self):
        rets = _synthetic_two_regime()
        params = fit_hmm(rets)
        p = filtered_p_high(rets, *params)
        # Calm first half → low P(high); turbulent tail → high P(high)
        assert p[150] < 0.5
        assert p[-1] > 0.5

    def test_annotate_alignment_and_warmup(self):
        rets = _synthetic_two_regime()
        closes = 100 * np.exp(np.cumsum(rets))
        candles = [{"close": float(c)} for c in closes]
        out = annotate_regime(candles, min_obs=120, refit_every=50)
        assert len(out) == len(candles)
        assert all(v is None for v in out[:120])
        tail = [v for v in out[-10:] if v is not None]
        assert tail and all(0.0 <= v <= 1.0 for v in tail)
        assert sum(tail) / len(tail) > 0.5      # tail is in the high-vol block

    def test_short_history_returns_none(self):
        candles = [{"close": 100.0 + i} for i in range(50)]
        assert all(v is None for v in annotate_regime(candles))
