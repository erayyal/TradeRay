"""Walk-forward backtest framework for the TradeRay rule engine.

This package is intentionally minimal — it exists to *validate* the rule
engine before any production tuning, not to be a full backtesting platform.

Three guardrails from the literature:

  - **Walk-forward replay (Pardo 1992).** No look-ahead: at bar `t` the engine
    sees only candles `<= t`, never `t+1`. TP/SL fills are resolved on the
    NEXT bar's high/low only.
  - **Bootstrap permutation test (Aronson 2006).** Shuffles trade returns to
    build a null distribution and reports the p-value of the observed Sharpe.
  - **Deflated Sharpe Ratio (López de Prado 2018).** Penalizes Sharpe for the
    number of trials we plausibly ran (`n_trials` argument) — guards against
    multiple-testing illusions.

Usage:

    python -m backtest BTCUSDT CRYPTO MID_TERM 2024-01-01 2026-01-01

The CLI prints a compact report (trades, win-rate, Sharpe, p-value, DSR) and
optionally writes per-trade rows to CSV.
"""
from __future__ import annotations

from backtest.stats import bootstrap_pvalue, deflated_sharpe, summarize
from backtest.walk_forward import BacktestResult, run_walk_forward

__all__ = [
    "BacktestResult",
    "run_walk_forward",
    "summarize",
    "bootstrap_pvalue",
    "deflated_sharpe",
]
