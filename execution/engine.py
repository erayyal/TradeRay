"""Multi-market execution engine.

Routing matrix:
                       │ AUTO_BOT          │ SIGNAL_ONLY
    ───────────────────┼───────────────────┼─────────────────
    Crypto             │ Place real orders │ Log signal only
    BIST / SP500 / NQ  │ HARD-BLOCKED *    │ Log signal only

  * Even if a MarketConfig row says AUTO_BOT for a traditional market, the
    engine downgrades it to SIGNAL_ONLY in code. This is a programmatic safety
    rail — TradeRay does not route equity orders through any broker API.

Actionable decisions create `Signal` rows; WAITs live in `decision_audits`.
`Trade` rows are written only when an order actually hits Binance.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from core.logger import get_logger
from core.telegram_notifier import fire, notify_signal_logged
from execution.binance_executor import place_decision
from execution.portfolio_guard import check_portfolio_gates
from execution.risk_manager import RiskRejection, validate_decision
from models import (
    AsyncSessionLocal,
    ExecutionMode,
    MarketType,
    Signal,
    SignalAction,
    Term,
    Trade,
    TradeStatus,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Hard-coded safety: traditional markets are NEVER allowed on the auto path.
# This set is the single source of truth — both the routing logic and the
# explicit guard `_assert_auto_allowed()` consult it.
# ---------------------------------------------------------------------------

_AUTO_BOT_ALLOWED: frozenset[MarketType] = frozenset({MarketType.CRYPTO})

_DUPLICATE_SIGNAL_WINDOW: dict[Term, timedelta] = {
    Term.SCALP: timedelta(hours=6),
    Term.SHORT_TERM: timedelta(days=3),
    Term.MID_TERM: timedelta(days=14),
}


def _assert_auto_allowed(market: MarketType) -> None:
    """Raises if a non-Crypto market is somehow reaching the order path.

    Defense-in-depth: this is checked AFTER the mode coercion below, so it
    should be unreachable in practice — but if it ever fires, that's a bug
    we want loud and immediate, not a silent accidental order on a real
    broker integration we add later.
    """
    if market not in _AUTO_BOT_ALLOWED:
        raise RuntimeError(
            f"FATAL: AUTO_BOT path reached for {market.value} — "
            f"traditional markets are signal-only. This is a routing bug."
        )


# ---------------------------------------------------------------------------
# Public engine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """Stateless façade — call `route()` once per (market, decision) pair."""

    async def route(
        self,
        *,
        market: MarketType,
        term: Term,
        symbol: str,
        decision: dict[str, Any],
        mode: ExecutionMode,
        quant_score: float | None = None,
        sentiment_score: float | None = None,
        fear_greed_index: int | None = None,
        macro_regime: str | None = None,
    ) -> dict[str, Any]:
        """Route an AI decision through the safety + execution pipeline.

        Returns a dict containing:
          - signal_id        : DB id of the persisted Signal
          - trade_id         : DB id of the Trade row, or None
          - executed         : bool — did real orders go to Binance?
          - effective_mode   : ExecutionMode actually used (after safety coercion)
          - reason           : short string explaining why we did/didn't trade
        """
        # 1. Coerce mode for safety. Traditional markets are forced SIGNAL_ONLY
        #    even if the caller (or DB) passes AUTO_BOT.
        effective_mode = mode
        coerced = False
        if mode == ExecutionMode.AUTO_BOT and market not in _AUTO_BOT_ALLOWED:
            effective_mode = ExecutionMode.SIGNAL_ONLY
            coerced = True
            log.warning(
                "engine.mode_coerced",
                symbol=symbol, market=market.value,
                requested=mode.value, applied=effective_mode.value,
                reason="traditional_market_signal_only",
            )

        action = decision.get("decision", "WAIT")

        # 1b. ZERO-TOLERANCE TP/SL gate.
        # A LONG / SHORT decision without complete entry + TP + SL is INVALID
        # — it must NOT be persisted to the DB and NOT be routed to Binance.
        # The signal is rejected outright. This blocks malformed AI output
        # AND any future bug in the rule engine that produces partial plans.
        if action in ("LONG", "SHORT"):
            tp = decision.get("take_profit")
            sl = decision.get("stop_loss")
            entry = decision.get("entry") or decision.get("entry_price")
            if tp is None or sl is None or entry is None:
                log.warning(
                    "engine.rejected_missing_tp_sl",
                    symbol=symbol, market=market.value, action=action,
                    has_entry=entry is not None,
                    has_tp=tp is not None,
                    has_sl=sl is not None,
                )
                return {
                    "signal_id": None,
                    "trade_id": None,
                    "executed": False,
                    "effective_mode": effective_mode,
                    "reason": "rejected_missing_tp_sl",
                }

        # 2. WAIT short-circuit — do NOT persist WAIT to the signals table.
        # Every cycle of every symbol produces a WAIT >95% of the time; writing
        # those to `signals` floods the table (240 rows/day per market) and
        # the UI's "Latest Signals" panel becomes useless.
        # The audit trail is already covered by `decision_audit` (every cycle
        # exit writes one row there, including WAITs with full logic_trace).
        if action == "WAIT":
            return {
                "signal_id": None,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": "decision_wait",
            }

        # 2b. Do not spam the same open setup every scheduler tick. A fresh
        # signal is allowed once the prior one resolves or the term-specific
        # suppression window expires.
        duplicate_signal_id = await self._find_recent_open_signal(
            market=market,
            term=term,
            symbol=symbol,
            action=action,
        )
        if duplicate_signal_id is not None:
            log.info(
                "engine.signal_duplicate_suppressed",
                existing_signal_id=duplicate_signal_id,
                symbol=symbol,
                market=market.value,
                term=term.value,
                action=action,
            )
            return {
                "signal_id": None,
                "existing_signal_id": duplicate_signal_id,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": "duplicate_open_signal",
            }

        # 2c. Portfolio-level risk gates — daily loss kill-switch, SL cooldown,
        # concurrency/heat caps. Applies to BOTH signal-only and auto-bot paths
        # (a signal stream polluted by revenge re-entries and correlated
        # pile-ons is as misleading as the trades would be).
        allow, guard_reason = await check_portfolio_gates(
            market=market, term=term, symbol=symbol, direction=action,
        )
        if not allow:
            return {
                "signal_id": None,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": f"portfolio_guard:{guard_reason}",
            }

        # 3. Persist the actionable signal — UI / backtest / audit depend on it.
        signal_id = await self._persist_signal(
            market=market,
            term=term,
            symbol=symbol,
            decision=decision,
            quant_score=quant_score,
            sentiment_score=sentiment_score,
            fear_greed_index=fear_greed_index,
            macro_regime=macro_regime,
        )

        # 4. SIGNAL_ONLY (either by request or by coercion) → done.
        # Fire a Telegram alert so the user sees the signal in real time.
        # AUTO_BOT path does NOT alert here — the executor below sends a
        # richer "trade placed" alert covering the same event.
        if effective_mode == ExecutionMode.SIGNAL_ONLY:
            fire(
                notify_signal_logged(
                    market=market.value,
                    side=action,
                    symbol=symbol,
                    entry=decision.get("entry") or decision.get("entry_price"),
                    take_profit=decision.get("take_profit"),
                    stop_loss=decision.get("stop_loss"),
                )
            )
            return {
                "signal_id": signal_id,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": "signal_only_mode" if not coerced else "signal_only_coerced",
            }

        # 5. AUTO_BOT path. Re-assert market eligibility, validate risk,
        #    place orders, persist Trade row.
        _assert_auto_allowed(market)

        try:
            validate_decision(decision)
        except RiskRejection as e:
            log.warning("engine.risk_rejected", symbol=symbol, err=str(e))
            return {
                "signal_id": signal_id,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": f"risk_rejected:{e}",
            }

        try:
            order_snapshot = await place_decision(decision)
        except Exception as e:
            log.exception("engine.execution_failed", symbol=symbol, err=str(e))
            return {
                "signal_id": signal_id,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": f"execution_error:{e.__class__.__name__}",
            }

        if order_snapshot is None:
            # validate_decision passed but executor still returned None — treat as wait
            return {
                "signal_id": signal_id,
                "trade_id": None,
                "executed": False,
                "effective_mode": effective_mode,
                "reason": "executor_returned_none",
            }

        trade_id = await self._persist_trade(signal_id=signal_id, snapshot=order_snapshot)

        return {
            "signal_id": signal_id,
            "trade_id": trade_id,
            "executed": True,
            "effective_mode": effective_mode,
            "reason": "executed",
        }

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    async def _find_recent_open_signal(
        self,
        *,
        market: MarketType,
        term: Term,
        symbol: str,
        action: str,
    ) -> int | None:
        try:
            action_enum = SignalAction(action)
        except ValueError:
            return None

        cutoff = datetime.now(timezone.utc) - _DUPLICATE_SIGNAL_WINDOW[term]
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(Signal)
                    .where(
                        Signal.market == market,
                        Signal.term == term,
                        Signal.symbol == symbol,
                        Signal.action == action_enum,
                        Signal.created_at >= cutoff,
                    )
                    .order_by(Signal.created_at.desc())
                    .limit(25)
                )
            ).scalars().all()

        for row in rows:
            if not (row.raw_payload or {}).get("resolution"):
                return row.id
        return None

    async def _persist_signal(
        self,
        *,
        market: MarketType,
        term: Term,
        symbol: str,
        decision: dict[str, Any],
        quant_score: float | None,
        sentiment_score: float | None,
        fear_greed_index: int | None,
        macro_regime: str | None,
    ) -> int:
        action = decision.get("decision", "WAIT")
        try:
            action_enum = SignalAction(action)
        except ValueError:
            action_enum = SignalAction.WAIT

        async with AsyncSessionLocal() as session:
            row = Signal(
                symbol=symbol,
                market=market,
                term=term,
                action=action_enum,
                confidence=int(decision.get("confidence_level") or decision.get("confidence") or 0),
                entry_price=decision.get("entry_price") or decision.get("entry"),
                take_profit=decision.get("take_profit"),
                stop_loss=decision.get("stop_loss"),
                risk_usd=decision.get("risk_usd"),
                reward_risk_ratio=decision.get("reward_risk_ratio"),
                leverage=decision.get("leverage"),
                quant_score=quant_score,
                sentiment_score=sentiment_score,
                fear_greed_index=fear_greed_index,
                macro_regime=macro_regime,
                justification=(decision.get("justification") or decision.get("rationale") or "")[:1024],
                raw_payload=decision,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            log.info(
                "engine.signal_logged",
                signal_id=row.id, symbol=symbol, market=market.value, action=action,
            )
            return row.id

    async def _persist_trade(self, *, signal_id: int, snapshot: dict[str, Any]) -> int:
        async with AsyncSessionLocal() as session:
            row = Trade(
                signal_id=signal_id,
                client_order_id=snapshot["client_id"],
                symbol=snapshot["symbol"],
                side=snapshot["side"],
                entry_price=float(snapshot["entry_price"]),
                take_profit=float(snapshot["take_profit"]),
                stop_loss=float(snapshot["stop_loss"]),
                quantity_base=float(snapshot["qty"]),
                leverage=int(snapshot["leverage"]),
                status=TradeStatus.PENDING,
                binance_order_ids={
                    "entry": snapshot.get("entry_order_id"),
                    "sl": snapshot.get("sl_order_id"),
                    "tp": snapshot.get("tp_order_id"),
                },
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            log.info("engine.trade_logged", trade_id=row.id, symbol=snapshot["symbol"])
            return row.id


# Module-level singleton — stateless, safe to share
engine = ExecutionEngine()


__all__ = ["ExecutionEngine", "engine"]
