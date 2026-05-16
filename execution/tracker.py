"""TradeRay PnL & Order-Lifecycle Tracker.

Three responsibilities, all idempotent and safe to run on a 5-minute timer:

  1. sync_binance_orders()        — reconcile Trade rows against Binance.
                                    Detects entry fills, SL/TP hits, computes
                                    realized_pnl_usd, transitions PENDING→OPEN
                                    →CLOSED, or →CANCELED if the entry was
                                    rejected/expired.

  2. sync_theoretical_signals()   — replay candles since each unresolved
                                    Signal's creation; if TP or SL was
                                    touched, store a `resolution` block in
                                    the Signal's raw_payload JSON column
                                    (no schema change needed).

  3. manage_stale_orders()        — TTL fallback. Any PENDING (unfilled)
                                    Trade older than the configured horizon
                                    (default 24h) is canceled on Binance and
                                    marked CANCELED in the DB.

Plus two helpers used by the Orchestrator:

  - get_pending_trade_for_symbol(symbol) → dict | None
        Snapshot of the most recent PENDING Trade for a symbol, ready to
        embed into the Master Trader's user payload.

  - cancel_pending_for_symbol(symbol, reason)
        Layer 2 of the staleness manager: cancel an order on demand
        (e.g. when the AI returns decision="CANCEL_PENDING").

All functions log loud and never raise out of the scheduler — callers are
batch jobs, not user requests; a failure on one trade must not block
twenty others.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from core.logger import get_logger
from core.telegram_notifier import (
    fire,
    notify_order_canceled,
    notify_signal_resolved,
    notify_trade_closed,
)
from data_fetchers.market_fetcher import fetcher as market_fetcher
from data_fetchers.technicals import compute_indicators
from execution.binance_executor import BinanceFilterError, replace_stop_loss
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


# ---------------------------------------------------------------------------
# Internal Binance client accessor.
#
# We deliberately reuse the AsyncClient created by market_fetcher rather than
# opening a second one — keeps us on a single connection pool and a single
# rate-limit envelope. The `_binance` attribute is module-internal but stable;
# if it ever changes shape, the breakage is loud and confined to this file.
# ---------------------------------------------------------------------------

async def _binance_client():
    return await market_fetcher._binance.client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_NEGATIVE_STATUSES = {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
_PENDING_STATUSES = {"NEW", "PARTIALLY_FILLED"}


def _safe_float(v: Any, default: float | None = None) -> float | None:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pending-order snapshot for the Master Trader prompt
# ---------------------------------------------------------------------------

async def get_pending_trade_for_symbol(symbol: str) -> dict[str, Any] | None:
    """Return the most recent PENDING Trade for a symbol, or None.

    The returned dict is shaped for direct embedding in the user payload —
    short keys, ISO timestamps, no SQLAlchemy objects.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(Trade)
                .where(
                    Trade.symbol == symbol,
                    Trade.status == TradeStatus.PENDING,
                )
                .order_by(Trade.created_at.desc())
            )
        ).scalars().first()

    if row is None:
        return None

    age_hours = (
        datetime.now(timezone.utc) - row.created_at
    ).total_seconds() / 3600.0

    return {
        "trade_id": row.id,
        "client_order_id": row.client_order_id,
        "symbol": row.symbol,
        "side": row.side,
        "entry_price": row.entry_price,
        "take_profit": row.take_profit,
        "stop_loss": row.stop_loss,
        "qty": row.quantity_base,
        "leverage": row.leverage,
        "created_at": row.created_at.isoformat(),
        "age_hours": round(age_hours, 2),
        "binance_order_ids": dict(row.binance_order_ids or {}),
    }


# ---------------------------------------------------------------------------
# Layer A: reconcile real Binance trades
# ---------------------------------------------------------------------------

async def sync_binance_orders() -> dict[str, int]:
    """Walk every Trade in {PENDING, OPEN} and reconcile with Binance.

    Returns a small counter dict for observability.
    """
    counts = {"checked": 0, "filled_entry": 0, "closed": 0, "canceled": 0, "errors": 0}

    async with AsyncSessionLocal() as session:
        trades = (
            await session.execute(
                select(Trade).where(
                    Trade.status.in_([TradeStatus.PENDING, TradeStatus.OPEN])
                )
            )
        ).scalars().all()

    if not trades:
        return counts

    log.info("tracker.sync_binance.start", n=len(trades))

    client = await _binance_client()
    for trade in trades:
        counts["checked"] += 1
        try:
            outcome = await _reconcile_one_trade(client, trade)
            if outcome == "filled_entry":
                counts["filled_entry"] += 1
            elif outcome == "closed":
                counts["closed"] += 1
            elif outcome == "canceled":
                counts["canceled"] += 1
        except Exception as e:
            counts["errors"] += 1
            log.exception(
                "tracker.reconcile_failed", trade_id=trade.id, err=str(e)
            )

    log.info("tracker.sync_binance.done", **counts)
    return counts


async def _reconcile_one_trade(client, trade: Trade) -> str | None:
    """Reconcile one Trade with Binance. Returns the transition that occurred."""
    bn_ids = dict(trade.binance_order_ids or {})
    entry_id = bn_ids.get("entry")
    sl_id = bn_ids.get("sl")
    tp_id = bn_ids.get("tp")

    if not entry_id:
        return None  # Nothing to check (shouldn't happen for live trades)

    # ---- 1. Entry order ----
    entry_order = await client.futures_get_order(symbol=trade.symbol, orderId=entry_id)
    entry_status = entry_order.get("status")

    if entry_status in _FINAL_NEGATIVE_STATUSES:
        # Entry never filled; mark CANCELED + best-effort cancel any leftover brackets.
        await _cancel_open_brackets(client, trade.symbol, sl_id, tp_id)
        reason = f"entry_{entry_status.lower()}"
        await _mark_trade(
            trade.id,
            status=TradeStatus.CANCELED,
            closed_at=datetime.now(timezone.utc),
            extra_meta={"cancel_reason": reason},
        )
        log.info(
            "tracker.entry_terminal", trade_id=trade.id,
            entry_status=entry_status, symbol=trade.symbol,
        )
        fire(notify_order_canceled(symbol=trade.symbol, reason=reason))
        return "canceled"

    if entry_status in _PENDING_STATUSES:
        # Still resting on the book — staleness manager will handle it.
        return None

    # entry_status == "FILLED" — entry hit. May still be open (brackets active)
    # or may already be closed (SL/TP fired before this sync ran).
    avg_entry = _safe_float(entry_order.get("avgPrice"), trade.entry_price) or trade.entry_price

    transitioned_to_open = False
    if trade.status == TradeStatus.PENDING:
        await _mark_trade(trade.id, status=TradeStatus.OPEN)
        transitioned_to_open = True
        log.info(
            "tracker.entry_filled", trade_id=trade.id, symbol=trade.symbol,
            avg_entry=avg_entry,
        )

    # ---- 2. Brackets ----
    sl_order = await client.futures_get_order(symbol=trade.symbol, orderId=sl_id) if sl_id else None
    tp_order = await client.futures_get_order(symbol=trade.symbol, orderId=tp_id) if tp_id else None

    sl_filled = bool(sl_order) and sl_order.get("status") == "FILLED"
    tp_filled = bool(tp_order) and tp_order.get("status") == "FILLED"

    if not (sl_filled or tp_filled):
        return "filled_entry" if transitioned_to_open else None

    # ---- 3. Position closed — compute realized PnL ----
    if sl_filled and tp_filled:
        # Race condition (closePosition=True should prevent this, but defensive).
        sl_t = int(sl_order.get("updateTime", 0))
        tp_t = int(tp_order.get("updateTime", 0))
        if sl_t <= tp_t:
            outcome, src_order, fallback_price = "SL", sl_order, trade.stop_loss
        else:
            outcome, src_order, fallback_price = "TP", tp_order, trade.take_profit
    elif sl_filled:
        outcome, src_order, fallback_price = "SL", sl_order, trade.stop_loss
    else:
        outcome, src_order, fallback_price = "TP", tp_order, trade.take_profit

    exit_price = _safe_float(src_order.get("avgPrice"), fallback_price) or fallback_price
    qty = trade.quantity_base
    is_long = trade.side == "LONG"

    pnl = (exit_price - avg_entry) * qty if is_long else (avg_entry - exit_price) * qty

    closed_ts_ms = int(src_order.get("updateTime", 0))
    closed_at = (
        datetime.fromtimestamp(closed_ts_ms / 1000, tz=timezone.utc)
        if closed_ts_ms else datetime.now(timezone.utc)
    )

    await _mark_trade(
        trade.id,
        status=TradeStatus.CLOSED,
        realized_pnl_usd=pnl,
        closed_at=closed_at,
        extra_meta={"close_outcome": outcome, "exit_price": exit_price},
    )
    log.info(
        "tracker.trade_closed",
        trade_id=trade.id, symbol=trade.symbol, side=trade.side,
        outcome=outcome, entry=avg_entry, exit=exit_price, pnl=pnl,
    )
    fire(
        notify_trade_closed(
            symbol=trade.symbol, outcome=outcome, pnl_usd=pnl,
        )
    )
    return "closed"


async def _cancel_open_brackets(client, symbol: str, sl_id, tp_id) -> None:
    """Best-effort: cancel any bracket order that may still be live."""
    for label, oid in (("sl", sl_id), ("tp", tp_id)):
        if not oid:
            continue
        try:
            await client.futures_cancel_order(symbol=symbol, orderId=oid)
        except Exception as e:
            # Already filled / already canceled — that's fine, just log.
            log.debug(
                "tracker.bracket_cancel_skipped",
                symbol=symbol, label=label, err=str(e),
            )


async def _mark_trade(
    trade_id: int,
    *,
    status: TradeStatus | None = None,
    realized_pnl_usd: float | None = None,
    closed_at: datetime | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    """Idempotent partial update on a Trade row."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Trade).where(Trade.id == trade_id))
        ).scalar_one_or_none()
        if row is None:
            return
        if status is not None:
            row.status = status
        if realized_pnl_usd is not None:
            row.realized_pnl_usd = realized_pnl_usd
        if closed_at is not None:
            row.closed_at = closed_at
        if extra_meta:
            meta = dict(row.binance_order_ids or {})
            meta.update(extra_meta)
            row.binance_order_ids = meta
            flag_modified(row, "binance_order_ids")
        await session.commit()


# ---------------------------------------------------------------------------
# Layer B: theoretical signal resolution (replay-based)
# ---------------------------------------------------------------------------

async def sync_theoretical_signals(lookback_days: int = 30) -> dict[str, int]:
    """For each non-WAIT signal still unresolved, replay the candles since
    creation; if TP or SL was touched, persist the resolution into raw_payload.

    Resolution shape (saved into Signal.raw_payload["resolution"]):
        {
          "status":              "RESOLVED_TP" | "RESOLVED_SL",
          "outcome":             "TP" | "SL",
          "exit_price":          <float>,
          "resolved_at_ms":      <int>,
          "resolved_at":         "<iso>",
          "theoretical_pnl_usd": <float>,
          "size_base":           <float>
        }
    """
    counts = {"checked": 0, "resolved": 0, "still_open": 0, "errors": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    async with AsyncSessionLocal() as session:
        signals = (
            await session.execute(
                select(Signal).where(
                    Signal.created_at >= cutoff,
                    Signal.action != SignalAction.WAIT,
                )
            )
        ).scalars().all()

    unresolved = [s for s in signals if not (s.raw_payload or {}).get("resolution")]
    if not unresolved:
        return counts

    log.info(
        "tracker.sync_signals.start", n=len(unresolved),
        lookback_days=lookback_days,
    )

    # Group by (symbol, market) so we fetch each candle history once.
    groups: dict[tuple[str, MarketType], list[Signal]] = {}
    for s in unresolved:
        groups.setdefault((s.symbol, s.market), []).append(s)

    for (symbol, market), sigs in groups.items():
        try:
            counts["checked"] += len(sigs)
            resolved_n, open_n = await _resolve_signal_group(symbol, market, sigs)
            counts["resolved"] += resolved_n
            counts["still_open"] += open_n
        except Exception as e:
            counts["errors"] += len(sigs)
            log.exception(
                "tracker.signal_group_failed",
                symbol=symbol, market=market.value, err=str(e),
            )

    log.info("tracker.sync_signals.done", **counts)
    return counts


async def _resolve_signal_group(
    symbol: str, market: MarketType, signals: list[Signal]
) -> tuple[int, int]:
    """Fetch candles spanning all signals in this group and replay each."""
    earliest = min(s.created_at for s in signals)
    age_hours = (datetime.now(timezone.utc) - earliest).total_seconds() / 3600.0

    # Pick replay granularity by age:
    #   - <24h   → 15m candles (precise touch detection)
    #   - 1–7d   → 1h
    #   - >7d    → 1d (cheaper, less precise — acceptable for old signals)
    if age_hours <= 24:
        interval = "15m"
    elif age_hours <= 168:
        interval = "1h"
    else:
        interval = "1d"

    candles = await market_fetcher.fetch_ohlcv(symbol, market, interval, limit=500)
    if not candles:
        log.warning(
            "tracker.no_candles", symbol=symbol, market=market.value, interval=interval,
        )
        return (0, 0)

    resolved = 0
    still_open = 0
    for s in signals:
        outcome = await _resolve_one_signal(s, candles)
        if outcome:
            resolved += 1
        else:
            still_open += 1
    return resolved, still_open


async def _resolve_one_signal(signal: Signal, candles: list[dict]) -> bool:
    """Walk candles after signal creation, find first TP / SL touch.

    Returns True if a resolution was persisted, False if the signal is
    still open (no touch yet).
    """
    entry, tp, sl = signal.entry_price, signal.take_profit, signal.stop_loss
    if entry is None or tp is None or sl is None:
        return False

    sig_ms = int(signal.created_at.timestamp() * 1000)
    relevant = [c for c in candles if c.get("close_time", 0) >= sig_ms]
    if not relevant:
        return False

    is_long = signal.action == SignalAction.LONG
    outcome: str | None = None
    exit_price: float | None = None
    exit_time_ms: int | None = None

    for c in relevant:
        if is_long:
            # Conservative: if both TP and SL hit in the same candle, assume SL
            # touched first (worse outcome). This matches risk-management bias.
            if c["low"] <= sl:
                outcome, exit_price, exit_time_ms = "SL", sl, c["close_time"]
                break
            if c["high"] >= tp:
                outcome, exit_price, exit_time_ms = "TP", tp, c["close_time"]
                break
        else:
            if c["high"] >= sl:
                outcome, exit_price, exit_time_ms = "SL", sl, c["close_time"]
                break
            if c["low"] <= tp:
                outcome, exit_price, exit_time_ms = "TP", tp, c["close_time"]
                break

    if outcome is None:
        return False

    # Theoretical PnL using the size the executor would have computed.
    risk = signal.risk_usd or 0.0
    risk_per_unit = abs(entry - sl)
    size_base = (risk / risk_per_unit) if risk_per_unit > 0 else 0.0
    pnl = (
        (exit_price - entry) * size_base
        if is_long
        else (entry - exit_price) * size_base
    )

    resolution = {
        "status": "RESOLVED_TP" if outcome == "TP" else "RESOLVED_SL",
        "outcome": outcome,
        "exit_price": exit_price,
        "resolved_at_ms": exit_time_ms,
        "resolved_at": datetime.fromtimestamp(
            (exit_time_ms or 0) / 1000, tz=timezone.utc
        ).isoformat(),
        "theoretical_pnl_usd": pnl,
        "size_base": size_base,
    }

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Signal).where(Signal.id == signal.id))
        ).scalar_one_or_none()
        if row is None:
            return False
        payload = dict(row.raw_payload or {})
        payload["resolution"] = resolution
        row.raw_payload = payload
        flag_modified(row, "raw_payload")
        await session.commit()

    log.info(
        "tracker.signal_resolved",
        signal_id=signal.id, symbol=signal.symbol, outcome=outcome,
        pnl=round(pnl, 4),
    )
    fire(
        notify_signal_resolved(
            market=signal.market.value,
            symbol=signal.symbol,
            outcome=outcome,
            pnl_usd=pnl,
        )
    )
    return True


# ---------------------------------------------------------------------------
# Layer C: staleness manager (TTL fallback) + on-demand cancel
# ---------------------------------------------------------------------------

async def manage_stale_orders(ttl_hours: int = 24) -> dict[str, int]:
    """Cancel any PENDING (unfilled) Trade older than `ttl_hours`."""
    counts = {"checked": 0, "canceled": 0, "errors": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    async with AsyncSessionLocal() as session:
        stale = (
            await session.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.PENDING,
                    Trade.created_at < cutoff,
                )
            )
        ).scalars().all()

    counts["checked"] = len(stale)
    if not stale:
        return counts

    log.info("tracker.stale.start", n=len(stale), ttl_hours=ttl_hours)
    for trade in stale:
        try:
            await cancel_trade(trade.id, reason=f"stale_{ttl_hours}h_ttl")
            counts["canceled"] += 1
        except Exception as e:
            counts["errors"] += 1
            log.exception(
                "tracker.stale_cancel_failed", trade_id=trade.id, err=str(e),
            )

    log.info("tracker.stale.done", **counts)
    return counts


async def cancel_trade(trade_id: int, *, reason: str = "manual") -> bool:
    """Cancel every Binance order for a Trade and mark it CANCELED.

    Idempotent: if already terminal, no-op.
    """
    async with AsyncSessionLocal() as session:
        trade = (
            await session.execute(select(Trade).where(Trade.id == trade_id))
        ).scalar_one_or_none()

    if trade is None:
        log.warning("tracker.cancel_trade_not_found", trade_id=trade_id)
        return False

    if trade.status not in {TradeStatus.PENDING, TradeStatus.OPEN}:
        log.info(
            "tracker.cancel_trade_skip",
            trade_id=trade_id, status=trade.status.value,
        )
        return False

    client = await _binance_client()
    bn_ids = dict(trade.binance_order_ids or {})

    # Cancel each leg individually — closePosition=True brackets are conditional
    # but we still try to cancel them to keep the book clean.
    for label in ("entry", "sl", "tp"):
        oid = bn_ids.get(label)
        if not oid:
            continue
        try:
            await client.futures_cancel_order(symbol=trade.symbol, orderId=oid)
            log.info(
                "tracker.canceled_leg",
                trade_id=trade_id, label=label, order_id=oid,
            )
        except Exception as e:
            log.warning(
                "tracker.cancel_leg_skipped",
                trade_id=trade_id, label=label, order_id=oid, err=str(e),
            )

    await _mark_trade(
        trade_id,
        status=TradeStatus.CANCELED,
        closed_at=datetime.now(timezone.utc),
        extra_meta={"cancel_reason": reason},
    )
    # Single Telegram alert for both TTL-stale cancellations and AI-driven
    # CANCEL_PENDING cancellations — `reason` disambiguates in the message.
    fire(notify_order_canceled(symbol=trade.symbol, reason=reason))
    return True


async def cancel_pending_for_symbol(
    symbol: str, *, reason: str = "ai_invalidated"
) -> int:
    """Cancel ALL currently-PENDING Trades for `symbol`. Returns count canceled.

    Used by the orchestrator when the Master Trader returns
    decision="CANCEL_PENDING" — Layer 2 of the staleness manager.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Trade).where(
                    Trade.symbol == symbol,
                    Trade.status == TradeStatus.PENDING,
                )
            )
        ).scalars().all()

    if not rows:
        log.info("tracker.cancel_pending_none", symbol=symbol)
        return 0

    canceled = 0
    for t in rows:
        try:
            if await cancel_trade(t.id, reason=reason):
                canceled += 1
        except Exception as e:
            log.exception(
                "tracker.cancel_pending_failed",
                trade_id=t.id, symbol=symbol, err=str(e),
            )

    log.info(
        "tracker.cancel_pending_done",
        symbol=symbol, canceled=canceled, requested=len(rows), reason=reason,
    )
    return canceled


# ---------------------------------------------------------------------------
# Layer D: Chandelier trailing-exit (Chuck LeBeau)
#
# For OPEN trades whose underlying Signal is term=MID_TERM (or SHORT_TERM):
#   exit = HighestHigh(N) − 3 × ATR(22)       on LONG (mirror on SHORT).
# Stops are ratcheted ONE WAY ONLY — they can only tighten in the trade's
# favour. If the new chandelier level is no better than the existing SL, we
# leave the order untouched. This is the classic trend-following trailing-stop
# formulation that lets winners run while preserving asymmetric R:R.
#
# Why not Binance's native TRAILING_STOP_MARKET? Their trailing percentage is
# fixed at order-placement time; chandelier is volatility-adaptive (ATR
# changes as the trade progresses). We re-compute server-side every 30 min,
# cancel + replace the closePosition STOP_MARKET, and persist the new level.
# ---------------------------------------------------------------------------

_CHANDELIER_TRAIL_TERMS: frozenset[Term] = frozenset({Term.MID_TERM, Term.SHORT_TERM})
_CHANDELIER_ATR_MULT: float = 3.0
_CHANDELIER_ATR_PERIOD: int = 22


def _chandelier_interval_for(term: Term) -> str:
    """Daily candles for MID_TERM, 4h for SHORT_TERM.

    Matches the rule engine's signal interval one step above the entry TF, so
    the trailing stop reads the same regime the entry was based on.
    """
    if term == Term.MID_TERM:
        return "1d"
    return "4h"


async def update_chandelier_stops() -> dict[str, int]:
    """Walk every OPEN trade and tighten the SL if Chandelier says so.

    Idempotent and ratchet-only:
      - LONG  : new_sl = max(current_sl, highest_high_since_entry − 3×ATR)
      - SHORT : new_sl = min(current_sl, lowest_low_since_entry   + 3×ATR)
    A trade whose chandelier level hasn't improved is left alone (zero cost).

    Returns counter dict for observability.
    """
    counts = {"checked": 0, "tightened": 0, "skipped_no_improvement": 0, "errors": 0}

    async with AsyncSessionLocal() as session:
        # Join trades → signals to filter by term in one query.
        rows = (
            await session.execute(
                select(Trade, Signal)
                .join(Signal, Trade.signal_id == Signal.id)
                .where(
                    Trade.status == TradeStatus.OPEN,
                    Signal.term.in_(list(_CHANDELIER_TRAIL_TERMS)),
                )
            )
        ).all()

    if not rows:
        return counts

    log.info("tracker.chandelier.start", n=len(rows))

    for trade, signal in rows:
        counts["checked"] += 1
        try:
            outcome = await _trail_one_trade(trade, signal)
            if outcome == "tightened":
                counts["tightened"] += 1
            elif outcome == "no_improvement":
                counts["skipped_no_improvement"] += 1
        except Exception as e:
            counts["errors"] += 1
            log.exception(
                "tracker.chandelier.failed",
                trade_id=trade.id, symbol=trade.symbol, err=str(e),
            )

    log.info("tracker.chandelier.done", **counts)
    return counts


async def _trail_one_trade(trade: Trade, signal: Signal) -> str | None:
    """Compute the Chandelier level for one trade; cancel + replace SL if better."""
    interval = _chandelier_interval_for(signal.term)

    # Fetch enough candles to span entry → now + the ATR warmup window.
    # 200 daily candles ≈ 6+ months, enough for ATR(22) + any reasonable hold.
    candles = await market_fetcher.fetch_ohlcv(
        trade.symbol, signal.market, interval, limit=200,
    )
    if not candles or len(candles) < _CHANDELIER_ATR_PERIOD + 2:
        log.debug(
            "tracker.chandelier.insufficient_candles",
            symbol=trade.symbol, n=len(candles) if candles else 0,
        )
        return None

    # ATR on the trailing interval — use a focused lookbacks dict so we don't
    # waste time on RSI/MACD/etc. We still need enough bars for the indicator.
    ind = compute_indicators(
        candles,
        lookbacks={"atr": _CHANDELIER_ATR_PERIOD},
    )
    if ind.get("error"):
        log.debug(
            "tracker.chandelier.indicators_failed", symbol=trade.symbol,
            err=ind.get("error"),
        )
        return None
    atr = ind.get("atr")
    if not atr or atr <= 0:
        return None

    # Slice candles to those AFTER entry — the trail is anchored on the entry.
    entry_ms = int(trade.created_at.timestamp() * 1000)
    post_entry = [c for c in candles if c.get("close_time", 0) >= entry_ms]
    if not post_entry:
        # Not even one bar closed since entry — give the trade a chance.
        return None

    is_long = trade.side == "LONG"
    if is_long:
        anchor = max(c["high"] for c in post_entry)
        chandelier_sl = anchor - _CHANDELIER_ATR_MULT * atr
        # Don't trail beyond the current price — would be an instant-stop.
        last_close = float(candles[-1]["close"])
        chandelier_sl = min(chandelier_sl, last_close * 0.999)
        # Ratchet: only raise the stop.
        if chandelier_sl <= trade.stop_loss:
            return "no_improvement"
    else:
        anchor = min(c["low"] for c in post_entry)
        chandelier_sl = anchor + _CHANDELIER_ATR_MULT * atr
        last_close = float(candles[-1]["close"])
        chandelier_sl = max(chandelier_sl, last_close * 1.001)
        if chandelier_sl >= trade.stop_loss:
            return "no_improvement"

    # Cancel old SL + place new STOP_MARKET via the executor.
    bn_ids = dict(trade.binance_order_ids or {})
    old_sl_id = bn_ids.get("sl")
    try:
        new = await replace_stop_loss(
            symbol=trade.symbol,
            side_long=is_long,
            old_sl_order_id=old_sl_id,
            new_sl_price=chandelier_sl,
        )
    except BinanceFilterError as e:
        log.warning(
            "tracker.chandelier.filter_rejected",
            trade_id=trade.id, symbol=trade.symbol, err=str(e),
        )
        return None

    # Persist new SL price + new orderId. Append to trail history for audit.
    bn_ids["sl"] = new["order_id"]
    trail_history = bn_ids.get("trail_history") or []
    trail_history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "old_sl": trade.stop_loss,
        "new_sl": new["stop_price"],
        "anchor": anchor,
        "atr": atr,
        "interval": interval,
    })
    bn_ids["trail_history"] = trail_history[-25:]  # cap log size

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Trade).where(Trade.id == trade.id))
        ).scalar_one_or_none()
        if row is not None:
            row.stop_loss = new["stop_price"]
            row.binance_order_ids = bn_ids
            flag_modified(row, "binance_order_ids")
            await session.commit()

    log.info(
        "tracker.chandelier.tightened",
        trade_id=trade.id, symbol=trade.symbol, side=trade.side,
        old_sl=round(trade.stop_loss, 6) if trade.stop_loss else None,
        new_sl=round(new["stop_price"], 6),
        anchor=round(anchor, 6), atr=round(atr, 6),
    )
    return "tightened"


__all__ = [
    "get_pending_trade_for_symbol",
    "sync_binance_orders",
    "sync_theoretical_signals",
    "manage_stale_orders",
    "update_chandelier_stops",
    "cancel_trade",
    "cancel_pending_for_symbol",
]
