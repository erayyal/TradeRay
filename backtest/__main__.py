"""CLI entrypoint:

    python -m backtest BTCUSDT CRYPTO MID_TERM 2024-01-01 2026-01-01 \\
        [--n-trials 4] [--csv out.csv]

Prints a one-line summary + optional per-trade CSV.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import sys
from datetime import datetime

from backtest.walk_forward import run_walk_forward
from models import MarketType, Term


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m backtest")
    p.add_argument("symbol")
    p.add_argument("market", choices=[m.value for m in MarketType])
    p.add_argument("term", choices=[t.value for t in Term])
    p.add_argument("start", help="YYYY-MM-DD (inclusive)")
    p.add_argument("end", help="YYYY-MM-DD (inclusive)")
    p.add_argument("--n-bars", type=int, default=1500, help="max bars to pull")
    p.add_argument(
        "--n-trials", type=int, default=1,
        help="number of strategy variants tested (for Deflated Sharpe)",
    )
    p.add_argument("--csv", default=None, help="write per-trade rows here")
    return p


async def _amain(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    market = MarketType(args.market)
    term = Term(args.term)
    start = _parse_date(args.start)
    end = _parse_date(args.end)

    result = await run_walk_forward(
        symbol=args.symbol, market=market, term=term,
        start=start, end=end, n_bars=args.n_bars, n_trials=args.n_trials,
    )

    s = result.summary
    print()
    print(f"== TradeRay backtest: {args.symbol} {market.value}/{term.value} ==")
    print(f"interval={result.interval}  bars={result.n_bars}  setups={result.n_setups}")
    if s is None or s.n_trades == 0:
        print("(no closed trades in window)")
        return 0
    print(
        f"trades={s.n_trades}  wins={s.wins}  losses={s.losses}  "
        f"win_rate={s.win_rate:.1%}"
    )
    print(
        f"avg_R={s.avg_r:+.2f}  total_R={s.total_pnl:+.2f}  "
        f"Sharpe (per-trade)={s.sharpe_per_trade:+.2f}  "
        f"Sharpe (ann)={s.sharpe_annualized:+.2f}"
    )
    print(
        f"bootstrap p-value={s.pvalue:.4f}  "
        f"Deflated Sharpe (P[SR_true>0])={s.dsr:.3f}  "
        f"(n_trials={args.n_trials})"
    )

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            cols = list(dataclasses.fields(result.trades[0])) if result.trades else []
            if cols:
                writer = csv.DictWriter(f, fieldnames=[c.name for c in cols])
                writer.writeheader()
                for t in result.trades:
                    writer.writerow(dataclasses.asdict(t))
        print(f"wrote {len(result.trades)} trades to {args.csv}")

    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
