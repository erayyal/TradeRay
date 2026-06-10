"""Parameter sweep harness — Phase 4-a/4-b.

Grid-sweeps the rule engine's TermParams over a (symbol set × parameter grid),
running the walk-forward replay for every combination, then ranks the results
by Deflated Sharpe Ratio with `n_trials` set to the FULL grid size — i.e. the
multiple-testing penalty reflects every variant we tried, not just the winner
(Bailey & López de Prado 2014; López de Prado 2018 ch.8 "backtest overfitting").

Candles are fetched ONCE per symbol and shared across all combos, so a
3-symbol × 216-combo sweep does 3 network fetches, not 648.

Usage (run inside the backend container — needs TA-Lib + Binance access):

    python -m backtest.sweep BTCUSDT,ETHUSDT,SOLUSDT CRYPTO MID_TERM \
        2024-01-01 2026-06-01 [--biases TF,MR] [--top 10] [--json out.json]

Output: per-combo pooled stats (all symbols' R-multiples concatenated) ranked
by DSR, plus per-symbol breakdown for the top sets.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import itertools
import json
import sys
from datetime import datetime
from typing import Any, Sequence

from agents.rule_engine import TermParams, params_for
from backtest.stats import summarize
from backtest.walk_forward import _fetch_history, run_walk_forward
from core.logger import get_logger
from models import MarketType, Term

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Default grid — TASK.md Phase 4-a spec. 3×4×3×3 = 108 combos per bias.
# With --biases TF,MR (Phase 4-b: Connors-style MR on daily) → 216 combos.
# ---------------------------------------------------------------------------

DEFAULT_GRID: dict[str, list] = {
    "atr_sl_mult": [1.5, 2.0, 2.5],
    "rr_target": [1.5, 2.0, 2.5, 3.0],
    "adx_min_for_trend": [20.0, 25.0, 30.0],
    "rel_volume_min": [0.8, 1.0, 1.2],
}

# MR-bias grid needs RSI thresholds too; keep the same structural axes but
# the RSI extremes are part of what defines an MR strategy on daily bars.
MR_RSI_GRID: list[tuple[float, float]] = [
    (10.0, 90.0),   # Connors RSI(2) classic (we run rsi_period from base params)
    (30.0, 70.0),   # Wilder RSI(14) classic
    (40.0, 60.0),   # loose
]


@dataclasses.dataclass
class ComboResult:
    combo: dict[str, Any]
    bias: str
    n_setups: int
    n_closed: int
    win_rate: float
    avg_r: float
    total_r: float
    sharpe_ann: float
    pvalue: float
    dsr: float
    per_symbol: dict[str, dict[str, Any]]


def _build_combos(base: TermParams, biases: Sequence[str]) -> list[TermParams]:
    """Expand the grid into concrete TermParams candidates."""
    combos: list[TermParams] = []
    axes = list(itertools.product(
        DEFAULT_GRID["atr_sl_mult"],
        DEFAULT_GRID["rr_target"],
        DEFAULT_GRID["adx_min_for_trend"],
        DEFAULT_GRID["rel_volume_min"],
    ))
    for bias in biases:
        if bias == "MR":
            rsi_axes = MR_RSI_GRID
        else:
            rsi_axes = [(base.rsi_long_max, base.rsi_short_min)]
        for (atr_m, rr, adx_min, rvol) in axes:
            for (rsi_lo, rsi_hi) in rsi_axes:
                combos.append(dataclasses.replace(
                    base,
                    bias=bias,  # type: ignore[arg-type]
                    atr_sl_mult=atr_m,
                    rr_target=rr,
                    adx_min_for_trend=adx_min,
                    rel_volume_min=rvol,
                    rsi_long_max=rsi_lo,
                    rsi_short_min=rsi_hi,
                ))
    return combos


def _combo_label(p: TermParams) -> dict[str, Any]:
    return {
        "bias": p.bias,
        "atr_sl_mult": p.atr_sl_mult,
        "rr_target": p.rr_target,
        "adx_min_for_trend": p.adx_min_for_trend,
        "rel_volume_min": p.rel_volume_min,
        "rsi_long_max": p.rsi_long_max,
        "rsi_short_min": p.rsi_short_min,
    }


async def run_sweep(
    *,
    symbols: list[str],
    market: MarketType,
    term: Term,
    start: datetime | None,
    end: datetime | None,
    biases: Sequence[str],
    n_bars: int = 3000,
) -> list[ComboResult]:
    base = params_for(market, term)
    combos = _build_combos(base, biases)
    n_trials = len(combos)
    log.info(
        "sweep.start", market=market.value, term=term.value,
        symbols=symbols, n_combos=n_trials,
    )

    # Fetch candle history once per symbol.
    candles_by_symbol: dict[str, list[dict]] = {}
    for sym in symbols:
        candles = await _fetch_history(sym, market, base.signal_interval, n_bars=n_bars)
        if candles:
            candles_by_symbol[sym] = candles
        else:
            log.warning("sweep.no_candles", symbol=sym)
    if not candles_by_symbol:
        return []

    results: list[ComboResult] = []
    for i, p in enumerate(combos):
        pooled_r: list[float] = []
        per_symbol: dict[str, dict[str, Any]] = {}
        n_setups = 0
        for sym, candles in candles_by_symbol.items():
            res = await run_walk_forward(
                symbol=sym, market=market, term=term,
                start=start, end=end, n_trials=n_trials,
                params=p, candles=list(candles),
            )
            closed = [t.r_multiple for t in res.trades if t.outcome in ("TP", "SL")]
            pooled_r.extend(closed)
            n_setups += res.n_setups
            per_symbol[sym] = {
                "setups": res.n_setups,
                "closed": len(closed),
                "total_r": round(sum(closed), 2),
            }

        s = summarize(pooled_r, r_multiples=pooled_r, n_trials=n_trials)
        results.append(ComboResult(
            combo=_combo_label(p),
            bias=p.bias,
            n_setups=n_setups,
            n_closed=s.n_trades,
            win_rate=s.win_rate,
            avg_r=s.avg_r,
            total_r=s.total_pnl,
            sharpe_ann=s.sharpe_annualized,
            pvalue=s.pvalue,
            dsr=s.dsr,
            per_symbol=per_symbol,
        ))
        if (i + 1) % 25 == 0:
            log.info("sweep.progress", done=i + 1, total=n_trials)

    results.sort(key=lambda r: (r.dsr, r.total_r), reverse=True)
    return results


def _print_report(results: list[ComboResult], top: int) -> None:
    print()
    print(f"== Sweep complete: {len(results)} combos, ranked by DSR ==")
    print(f"{'rank':>4} {'bias':>4} {'atr':>4} {'rr':>4} {'adx':>5} {'rvol':>5} "
          f"{'rsi':>9} {'n':>4} {'win%':>6} {'avgR':>6} {'totR':>7} {'Sharpe':>7} {'p':>6} {'DSR':>6}")
    for rank, r in enumerate(results[:top], 1):
        c = r.combo
        print(
            f"{rank:>4} {c['bias']:>4} {c['atr_sl_mult']:>4} {c['rr_target']:>4} "
            f"{c['adx_min_for_trend']:>5} {c['rel_volume_min']:>5} "
            f"{c['rsi_long_max']:.0f}/{c['rsi_short_min']:.0f}".rjust(0)
            + f" {r.n_closed:>4} {r.win_rate:>6.1%} {r.avg_r:>+6.2f} {r.total_r:>+7.1f} "
            f"{r.sharpe_ann:>+7.2f} {r.pvalue:>6.3f} {r.dsr:>6.3f}"
        )
    # Decision guidance (§11.5 ALGORITHM.md): DSR > 0.5 required for AUTO_BOT.
    best = results[0] if results else None
    print()
    if best and best.dsr > 0.5 and best.n_closed >= 20:
        print(f"✅ Best combo passes DSR>0.5 with {best.n_closed} trades — candidate for production params.")
    else:
        print("⚠️  No combo passes DSR>0.5 with ≥20 trades — keep SIGNAL-only, do NOT enable AUTO_BOT.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backtest.sweep")
    p.add_argument("symbols", help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    p.add_argument("market", choices=[m.value for m in MarketType])
    p.add_argument("term", choices=[t.value for t in Term])
    p.add_argument("start", help="YYYY-MM-DD")
    p.add_argument("end", help="YYYY-MM-DD")
    p.add_argument("--biases", default="TF,MR", help="comma list of TF,MR,HYB")
    p.add_argument("--n-bars", type=int, default=3000)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", default=None, help="write full ranked results here")
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        results = await run_sweep(
            symbols=[s.strip().upper() for s in args.symbols.split(",") if s.strip()],
            market=MarketType(args.market),
            term=Term(args.term),
            start=datetime.strptime(args.start, "%Y-%m-%d"),
            end=datetime.strptime(args.end, "%Y-%m-%d"),
            biases=[b.strip().upper() for b in args.biases.split(",") if b.strip()],
            n_bars=args.n_bars,
        )
    finally:
        try:
            from data_fetchers.market_fetcher import fetcher
            await fetcher._binance.close()
        except Exception:
            pass

    if not results:
        print("(no results — no candles fetched?)")
        return 1

    _print_report(results, args.top)

    if args.json:
        with open(args.json, "w") as f:
            json.dump([dataclasses.asdict(r) for r in results], f, indent=1)
        print(f"wrote {len(results)} combo results to {args.json}")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
