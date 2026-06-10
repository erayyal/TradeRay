"""Walk-forward backtest harness for the TradeRay rule engine.

Replays historical candles one bar at a time, asking `generate_rule_decision`
what to do given the indicators computed on the *trailing* window (no
look-ahead). Resolves each entry by walking forward bar-by-bar until either
TP or SL is touched (touch-on-bar resolution with conservative same-bar
priority: SL wins on ambiguity).

This is intentionally simpler than the production engine:
  - No funding/macro/calendar gates (set `macro_lite=None`).
  - No confirmation-TF — uses a single signal interval only.
  - No position sizing or vol-targeting (assumes 1R = stop-loss distance,
    reports returns in R-multiples).

Output is a `BacktestResult` with the per-trade record + summary stats.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

from agents.rule_engine import TermParams, generate_rule_decision, params_for
from backtest.stats import Summary, summarize
from core.logger import get_logger
from data_fetchers.market_fetcher import fetcher, lookbacks_for
from data_fetchers.technicals import compute_indicators
from models import MarketType, Term

log = get_logger(__name__)


# Minimum bars before the first decision can run — gives ATR/ADX/MACD time
# to warm up. ATR(14) + EMA(50) + MACD(12,26,9) all stabilize by bar ~75.
_WARMUP_BARS: int = 80


# Walk-forward decision cadence — re-evaluate at every CLOSED bar of the
# signal interval. Keep simple: 1 bar per decision step.
_STEP_BARS: int = 1


@dataclass
class Trade:
    direction: Literal["LONG", "SHORT"]
    entry_idx: int
    entry_time_ms: int
    entry: float
    take_profit: float
    stop_loss: float
    exit_idx: int | None = None
    exit_time_ms: int | None = None
    exit_price: float | None = None
    outcome: Literal["TP", "SL", "OPEN"] = "OPEN"
    r_multiple: float = 0.0


@dataclass
class BacktestResult:
    symbol: str
    market: MarketType
    term: Term
    interval: str
    n_bars: int
    n_setups: int
    trades: list[Trade] = field(default_factory=list)
    summary: Summary | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "market": self.market.value,
            "term": self.term.value,
            "interval": self.interval,
            "n_bars": self.n_bars,
            "n_setups": self.n_setups,
            "trades": [dataclasses.asdict(t) for t in self.trades],
            "summary": dataclasses.asdict(self.summary) if self.summary else None,
        }


def _params_for_backtest(market: MarketType, term: Term) -> TermParams:
    """Production params but with confirm_interval cleared (single-TF replay)."""
    p = params_for(market, term)
    return dataclasses.replace(p, confirm_interval=None)


def _resolve_forward(
    trade: Trade, candles: list[dict], start_idx: int,
) -> Trade:
    """Walk bars after `start_idx` until TP or SL touches.

    Conservative tie-break: if TP and SL are both in the same bar's [low, high],
    we credit the SL (worse case for the strategy). This matches the
    intra-bar uncertainty handling in `tracker._resolve_one_signal`.
    """
    risk = abs(trade.entry - trade.stop_loss)
    is_long = trade.direction == "LONG"
    for i in range(start_idx, len(candles)):
        c = candles[i]
        high, low = c["high"], c["low"]
        sl_hit = (low <= trade.stop_loss) if is_long else (high >= trade.stop_loss)
        tp_hit = (high >= trade.take_profit) if is_long else (low <= trade.take_profit)

        if sl_hit and tp_hit:
            outcome, price = "SL", trade.stop_loss
        elif sl_hit:
            outcome, price = "SL", trade.stop_loss
        elif tp_hit:
            outcome, price = "TP", trade.take_profit
        else:
            continue

        r = ((price - trade.entry) / risk) if is_long else ((trade.entry - price) / risk)
        return dataclasses.replace(
            trade,
            exit_idx=i,
            exit_time_ms=c.get("close_time"),
            exit_price=price,
            outcome=outcome,
            r_multiple=r,
        )

    return trade  # still open at end of history


def _filter_window(
    candles: list[dict], start: datetime | None, end: datetime | None,
) -> list[dict]:
    if start is None and end is None:
        return candles
    start_ms = int(start.replace(tzinfo=timezone.utc).timestamp() * 1000) if start else 0
    end_ms = int(end.replace(tzinfo=timezone.utc).timestamp() * 1000) if end else 1 << 62
    return [c for c in candles if start_ms <= c.get("close_time", 0) <= end_ms]


async def _fetch_history(
    symbol: str, market: MarketType, interval: str, *, n_bars: int,
) -> list[dict]:
    """Pull `n_bars` candles. Binance caps at 1500 per call — chunk if needed.

    The market_fetcher already paginates internally for Binance; for
    yfinance it returns whatever the period setting supports (typically up to
    2 years of daily data). For backtest we want as much history as we can
    get — pass the upper bound and let the fetcher cap.
    """
    return await fetcher.fetch_ohlcv(symbol, market, interval, limit=n_bars)


async def run_walk_forward(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    start: datetime | None = None,
    end: datetime | None = None,
    n_bars: int = 1500,
    n_trials: int = 1,
    params: TermParams | None = None,
    candles: list[dict] | None = None,
) -> BacktestResult:
    """Replay history bar-by-bar and produce a `BacktestResult`.

    Important defaults:
      - `n_trials=1`  → DSR is computed as if this were the only configuration
        ever tested. If you sweep parameters externally, pass the count of
        variants you tried.
      - One trade at a time per symbol — if an entry is open, additional
        signals are ignored until that trade closes. Matches the live
        execution invariant: one active position per symbol.
      - `params` overrides the production parameter table (sweep harness);
        confirm_interval is cleared either way (single-TF replay).
      - `candles` lets the caller pass pre-fetched history so a sweep over
        hundreds of parameter combos fetches each symbol's candles once.
    """
    if params is not None:
        p = dataclasses.replace(params, confirm_interval=None)
    else:
        p = _params_for_backtest(market, term)
    interval = p.signal_interval

    if candles is None:
        candles = await _fetch_history(symbol, market, interval, n_bars=n_bars)
    candles = _filter_window(candles, start, end)
    if not candles:
        log.warning(
            "backtest.no_candles", symbol=symbol, market=market.value, interval=interval,
        )
        return BacktestResult(
            symbol=symbol, market=market, term=term, interval=interval,
            n_bars=0, n_setups=0, trades=[], summary=None,
        )

    lookbacks = lookbacks_for(interval)
    trades: list[Trade] = []
    open_trade: Trade | None = None
    n_setups = 0

    for idx in range(_WARMUP_BARS, len(candles), _STEP_BARS):
        # If a trade is open, advance it bar-by-bar (don't re-enter).
        if open_trade is not None:
            resolved = _resolve_forward(open_trade, candles, idx)
            if resolved.outcome != "OPEN":
                trades.append(resolved)
                open_trade = None
            else:
                continue

        window = candles[: idx + 1]
        indicators = compute_indicators(window, lookbacks=lookbacks)
        if indicators.get("error"):
            continue

        # Wrap into the per-interval dict shape the rule engine expects.
        decision = generate_rule_decision(
            symbol=symbol,
            market=market,
            term=term,
            primary_interval=interval,
            indicators={interval: indicators},
            macro_lite=None,
            next_earnings_iso=None,
            params_override=p,
        )

        if decision["decision"] in ("LONG", "SHORT"):
            n_setups += 1
            open_trade = Trade(
                direction=decision["decision"],
                entry_idx=idx,
                entry_time_ms=int(candles[idx].get("close_time", 0)),
                entry=float(decision["entry_price"]),
                take_profit=float(decision["take_profit"]),
                stop_loss=float(decision["stop_loss"]),
            )

    # Any trade still open at the end is dropped from stats (no exit price).
    closed = [t for t in trades if t.outcome in ("TP", "SL")]
    r_returns = [t.r_multiple for t in closed]

    summary = summarize(
        r_returns,
        r_multiples=r_returns,
        trades_per_year=_trades_per_year_for(interval),
        n_trials=n_trials,
    )

    result = BacktestResult(
        symbol=symbol, market=market, term=term, interval=interval,
        n_bars=len(candles), n_setups=n_setups, trades=trades, summary=summary,
    )

    log.info(
        "backtest.done",
        symbol=symbol, market=market.value, term=term.value,
        interval=interval, n_bars=len(candles), n_setups=n_setups,
        n_closed=len(closed),
        win_rate=round(summary.win_rate, 3),
        sharpe_ann=round(summary.sharpe_annualized, 3),
        pvalue=round(summary.pvalue, 4),
        dsr=round(summary.dsr, 4),
    )
    return result


_TRADES_PER_YEAR_HINT: dict[str, int] = {
    "5m": 365 * 24 * 12,
    "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2,
    "1h": 365 * 24,
    "4h": 365 * 6,
    "1d": 252,
    "1w": 52,
}


def _trades_per_year_for(interval: str) -> int:
    """Coarse annualization factor — assumes we'd trade roughly every bar.

    Realistically the strategy trades far less often (most bars are WAITs);
    this factor over-states activity which makes annualized Sharpe slightly
    pessimistic. That's the safe error direction.
    """
    return _TRADES_PER_YEAR_HINT.get(interval, 252)


__all__ = ["BacktestResult", "Trade", "run_walk_forward"]
