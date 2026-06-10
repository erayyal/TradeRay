"""Hermetic tests for backtest/sweep.py grid construction."""
from __future__ import annotations

from agents.rule_engine import params_for
from backtest.sweep import DEFAULT_GRID, MR_RSI_GRID, _build_combos
from models import MarketType, Term


def _axes_size() -> int:
    return (
        len(DEFAULT_GRID["atr_sl_mult"])
        * len(DEFAULT_GRID["rr_target"])
        * len(DEFAULT_GRID["adx_min_for_trend"])
        * len(DEFAULT_GRID["rel_volume_min"])
    )


def test_tf_grid_size():
    base = params_for(MarketType.CRYPTO, Term.MID_TERM)
    combos = _build_combos(base, ["TF"])
    assert len(combos) == _axes_size()
    assert all(c.bias == "TF" for c in combos)


def test_mr_grid_expands_rsi_axes():
    base = params_for(MarketType.CRYPTO, Term.MID_TERM)
    combos = _build_combos(base, ["MR"])
    assert len(combos) == _axes_size() * len(MR_RSI_GRID)


def test_combo_preserves_base_intervals():
    base = params_for(MarketType.CRYPTO, Term.MID_TERM)
    combos = _build_combos(base, ["TF", "MR"])
    assert all(c.signal_interval == base.signal_interval for c in combos)
    assert all(c.risk_pct == base.risk_pct for c in combos)


def test_combos_are_distinct():
    base = params_for(MarketType.CRYPTO, Term.MID_TERM)
    combos = _build_combos(base, ["TF", "MR"])
    labels = {
        (c.bias, c.atr_sl_mult, c.rr_target, c.adx_min_for_trend,
         c.rel_volume_min, c.rsi_long_max, c.rsi_short_min)
        for c in combos
    }
    assert len(labels) == len(combos)
