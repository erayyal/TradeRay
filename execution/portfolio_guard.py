"""Portfolio-level risk gates — prop-desk style controls.

These run AFTER the signal-level rule engine and BEFORE a signal is persisted
or an order is routed. They protect against three classic failure modes the
per-trade risk model cannot see (Carver 2015 "Systematic Trading" §9 system
risk overlays; Tharp 2008 position-sizing heat caps; standard prop-desk
daily-loss discipline):

  1. DAILY LOSS KILL-SWITCH — when today's combined realized PnL (closed
     trades) + theoretical PnL (resolved signals) drops below
     -(portfolio_notional × daily_loss_limit_pct), block all NEW entries
     until UTC midnight. Losing days cluster (volatility clustering,
     Mandelbrot/Engle); cutting exposure after a bad day is the cheapest
     drawdown control available.

  2. SL COOLDOWN — after a stop-loss on (symbol, term), block re-entry in the
     SAME direction for a term-scaled window. Prevents whipsaw re-entries
     into the same failing setup (the rule engine's conditions usually still
     hold right after an SL — that's exactly when they're least reliable).

  3. CONCURRENCY / HEAT CAP — cap simultaneous open exposures per market and
     globally. Signals within a market are correlated (BTC/ETH/SOL ρ > 0.8;
     S&P names share factor exposure), so N open positions ≠ N independent
     bets. The cap bounds worst-case portfolio heat at
     max_open_total × risk_pct.

All gates are pure-decision functions over small snapshots; the DB access is
isolated in `_load_snapshot` so the logic is unit-testable without a DB.
Failures are fail-open with a loud log: a broken guard must not silently
halt signal production.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from config import settings
from core.logger import get_logger
from models import (
    AsyncSessionLocal,
    MarketType,
    Signal,
    SignalAction,
    Term,
    Trade,
    TradeStatus,
)

log = get_logger(__name__)


# Cooldown windows per term — roughly 2× the signal interval's natural
# re-evaluation horizon. SCALP whipsaws resolve in hours; MID_TERM in days.
SL_COOLDOWN: dict[Term, timedelta] = {
    Term.SCALP: timedelta(hours=4),
    Term.SHORT_TERM: timedelta(hours=24),
    Term.MID_TERM: timedelta(days=3),
}

# How far back unresolved signals count as "open exposure". Mirrors the
# engine's duplicate-suppression windows — beyond this, a stale unresolved
# signal is treated as abandoned rather than open.
OPEN_EXPOSURE_WINDOW: dict[Term, timedelta] = {
    Term.SCALP: timedelta(hours=6),
    Term.SHORT_TERM: timedelta(days=3),
    Term.MID_TERM: timedelta(days=14),
}


@dataclass
class PortfolioSnapshot:
    """Everything the gate logic needs, loaded in one place."""
    today_realized_pnl_usd: float = 0.0
    today_theoretical_pnl_usd: float = 0.0
    open_signals_by_market: dict[str, int] = field(default_factory=dict)
    open_trades_by_market: dict[str, int] = field(default_factory=dict)
    # Most recent SL resolution for (symbol, term): (resolved_at, direction)
    last_sl: tuple[datetime, str] | None = None


# ---------------------------------------------------------------------------
# Pure decision logic (unit-testable)
# ---------------------------------------------------------------------------

def evaluate_portfolio_gates(
    snapshot: PortfolioSnapshot,
    *,
    market: MarketType,
    term: Term,
    direction: str,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Return (allow, reason). Pure function of the snapshot."""
    now = now or datetime.now(timezone.utc)

    # Gate 1 — daily loss kill-switch.
    limit_pct = settings.daily_loss_limit_pct
    if limit_pct > 0:
        total_today = (
            snapshot.today_realized_pnl_usd + snapshot.today_theoretical_pnl_usd
        )
        limit_usd = settings.portfolio_notional * limit_pct
        if total_today <= -limit_usd:
            return False, (
                f"daily_loss_limit: today's PnL {total_today:+.0f} USD breaches "
                f"-{limit_usd:.0f} USD ({limit_pct:.0%} of portfolio) — "
                f"no new entries until UTC midnight"
            )

    # Gate 2 — SL cooldown (same symbol+term+direction).
    if settings.sl_cooldown_enabled and snapshot.last_sl is not None:
        resolved_at, sl_direction = snapshot.last_sl
        window = SL_COOLDOWN.get(term, timedelta(hours=24))
        if sl_direction == direction and (now - resolved_at) < window:
            remaining = window - (now - resolved_at)
            return False, (
                f"sl_cooldown: last {direction} stopped out "
                f"{(now - resolved_at).total_seconds() / 3600:.1f}h ago — "
                f"{remaining.total_seconds() / 3600:.1f}h cooldown remaining"
            )

    # Gate 3 — concurrency / portfolio heat caps.
    mkt = market.value
    open_in_market = (
        snapshot.open_signals_by_market.get(mkt, 0)
        + snapshot.open_trades_by_market.get(mkt, 0)
    )
    if settings.max_open_per_market > 0 and open_in_market >= settings.max_open_per_market:
        return False, (
            f"max_open_per_market: {open_in_market} open exposures in {mkt} "
            f"(cap {settings.max_open_per_market})"
        )
    open_total = (
        sum(snapshot.open_signals_by_market.values())
        + sum(snapshot.open_trades_by_market.values())
    )
    if settings.max_open_total > 0 and open_total >= settings.max_open_total:
        return False, (
            f"max_open_total: {open_total} open exposures across all markets "
            f"(cap {settings.max_open_total})"
        )

    return True, "portfolio gates clear"


# ---------------------------------------------------------------------------
# Snapshot loader (DB access isolated here)
# ---------------------------------------------------------------------------

async def _load_snapshot(
    *, symbol: str, term: Term
) -> PortfolioSnapshot:
    snap = PortfolioSnapshot()
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    max_window = max(OPEN_EXPOSURE_WINDOW.values())

    async with AsyncSessionLocal() as session:
        # Today's realized PnL from closed real trades.
        trades_today = (
            await session.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.CLOSED,
                    Trade.closed_at >= midnight,
                )
            )
        ).scalars().all()
        snap.today_realized_pnl_usd = sum(
            t.realized_pnl_usd or 0.0 for t in trades_today
        )

        # Open real trades per market (PENDING+OPEN), joined to signals for market.
        open_trades = (
            await session.execute(
                select(Trade, Signal)
                .join(Signal, Trade.signal_id == Signal.id)
                .where(Trade.status.in_([TradeStatus.PENDING, TradeStatus.OPEN]))
            )
        ).all()
        for _t, s in open_trades:
            mkt = s.market.value
            snap.open_trades_by_market[mkt] = snap.open_trades_by_market.get(mkt, 0) + 1

        # Recent non-WAIT signals — used for theoretical PnL today, open
        # exposure counts, and the per-(symbol, term) SL cooldown lookup.
        recent_signals = (
            await session.execute(
                select(Signal).where(
                    Signal.created_at >= now - max_window,
                    Signal.action != SignalAction.WAIT,
                )
            )
        ).scalars().all()

        traded_signal_ids = {s.id for _t, s in open_trades}
        for s in recent_signals:
            payload = s.raw_payload or {}
            resolution = payload.get("resolution") or {}
            mkt = s.market.value

            if resolution:
                # Theoretical PnL of signals RESOLVED today.
                resolved_at = _parse_iso(resolution.get("resolved_at"))
                if resolved_at is not None and resolved_at >= midnight:
                    snap.today_theoretical_pnl_usd += float(
                        resolution.get("theoretical_pnl_usd") or 0.0
                    )
                # SL cooldown candidate for this exact (symbol, term).
                if (
                    resolution.get("outcome") == "SL"
                    and s.symbol == symbol
                    and s.term == term
                    and resolved_at is not None
                ):
                    direction = s.action.value
                    if snap.last_sl is None or resolved_at > snap.last_sl[0]:
                        snap.last_sl = (resolved_at, direction)
            else:
                # Unresolved → open theoretical exposure (unless a real trade
                # already counted it, and only within the term's window).
                window = OPEN_EXPOSURE_WINDOW.get(s.term, max_window)
                if s.created_at >= now - window and s.id not in traded_signal_ids:
                    snap.open_signals_by_market[mkt] = (
                        snap.open_signals_by_market.get(mkt, 0) + 1
                    )

    return snap


def _parse_iso(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def check_portfolio_gates(
    *,
    market: MarketType,
    term: Term,
    symbol: str,
    direction: str,
) -> tuple[bool, str]:
    """Engine-facing entrypoint. Fail-open on infrastructure errors."""
    try:
        snapshot = await _load_snapshot(symbol=symbol, term=term)
    except Exception as e:
        log.exception("portfolio_guard.snapshot_failed", err=str(e))
        return True, "portfolio guard unavailable (fail-open)"
    allow, reason = evaluate_portfolio_gates(
        snapshot, market=market, term=term, direction=direction,
    )
    if not allow:
        log.info(
            "portfolio_guard.blocked",
            symbol=symbol, market=market.value, term=term.value,
            direction=direction, reason=reason,
        )
    return allow, reason


__all__ = [
    "PortfolioSnapshot",
    "evaluate_portfolio_gates",
    "check_portfolio_gates",
    "SL_COOLDOWN",
]
