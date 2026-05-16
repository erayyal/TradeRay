"""Backtest stats — Sharpe / bootstrap / Deflated Sharpe.

Hermetic: no network, no DB. Uses synthetic return series with known shape.
"""
from __future__ import annotations

import math
import random

from backtest.stats import (
    bootstrap_pvalue,
    deflated_sharpe,
    per_trade_sharpe,
    summarize,
)


def test_sharpe_zero_for_constant_returns():
    """All wins equal → zero std → Sharpe is undefined → returns 0."""
    assert per_trade_sharpe([0.01] * 50) == 0.0


def test_sharpe_positive_for_winning_strategy():
    rng = random.Random(1)
    returns = [rng.gauss(0.02, 0.05) for _ in range(200)]
    sr = per_trade_sharpe(returns)
    assert sr > 0.2  # positive drift / vol = positive Sharpe


def test_sharpe_negative_for_losing_strategy():
    rng = random.Random(2)
    returns = [rng.gauss(-0.02, 0.05) for _ in range(200)]
    assert per_trade_sharpe(returns) < 0


def test_bootstrap_pvalue_low_for_strong_signal():
    """Real edge → p-value should be small (< 0.05 typically)."""
    rng = random.Random(3)
    returns = [rng.gauss(0.05, 0.05) for _ in range(200)]
    p = bootstrap_pvalue(returns, n_boot=1500, seed=42)
    assert p < 0.05


def test_bootstrap_pvalue_high_for_no_signal():
    """No edge → p-value should NOT be significant (well above 0.05)."""
    rng = random.Random(4)
    returns = [rng.gauss(0.0, 0.05) for _ in range(200)]
    p = bootstrap_pvalue(returns, n_boot=1500, seed=42)
    assert p > 0.10


def test_bootstrap_pvalue_for_negative_sharpe_is_one():
    """Negative Sharpe can't be a positive-edge claim → p=1."""
    assert bootstrap_pvalue([-0.05, -0.05, -0.05]) == 1.0


def test_dsr_higher_with_more_trades_same_sharpe():
    """Larger sample → less uncertainty → higher P[SR_true > 0]."""
    rng = random.Random(5)
    small = [rng.gauss(0.02, 0.05) for _ in range(40)]
    rng = random.Random(5)
    large = [rng.gauss(0.02, 0.05) for _ in range(400)]
    assert deflated_sharpe(large, n_trials=1) > deflated_sharpe(small, n_trials=1)


def test_dsr_penalizes_more_trials():
    """Bailey-LdP: more trials → SR0 grows → DSR shrinks."""
    rng = random.Random(6)
    returns = [rng.gauss(0.02, 0.05) for _ in range(200)]
    p1 = deflated_sharpe(returns, n_trials=1)
    p_many = deflated_sharpe(returns, n_trials=1000)
    assert p_many <= p1


def test_summarize_reports_all_fields():
    rng = random.Random(7)
    returns = [rng.gauss(0.03, 0.05) for _ in range(100)]
    s = summarize(returns, r_multiples=returns, trades_per_year=252, n_trials=4)
    assert s.n_trades == 100
    assert s.wins + s.losses == 100
    assert 0 <= s.win_rate <= 1
    assert math.isfinite(s.sharpe_annualized)
    assert 0 <= s.pvalue <= 1
    assert 0 <= s.dsr <= 1


def test_summarize_empty_returns_safe():
    s = summarize([])
    assert s.n_trades == 0
    assert s.pvalue == 1.0
