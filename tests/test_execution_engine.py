"""Execution engine persistence guardrails."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from execution.engine import ExecutionEngine
from models import (
    AsyncSessionLocal,
    Base,
    ExecutionMode,
    MarketType,
    Signal,
    Term,
    get_engine,
)


def _decision() -> dict:
    return {
        "decision": "SHORT",
        "confidence": 90,
        "entry_price": 100.0,
        "take_profit": 95.0,
        "stop_loss": 103.0,
        "risk_usd": 10.0,
        "reward_risk_ratio": 1.67,
        "leverage": 1,
        "justification": "unit test setup",
    }


async def _reset_db() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _signal_ids() -> list[int]:
    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(Signal.id))).scalars().all())


async def _mark_resolved(signal_id: int) -> None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(select(Signal).where(Signal.id == signal_id))
        ).scalar_one()
        row.raw_payload = {
            **(row.raw_payload or {}),
            "resolution": {"outcome": "TP"},
        }
        await session.commit()


def test_duplicate_open_signal_is_suppressed_until_resolved():
    async def run() -> None:
        await _reset_db()
        engine = ExecutionEngine()

        first = await engine.route(
            market=MarketType.CRYPTO,
            term=Term.SCALP,
            symbol="ZECUSDT",
            decision=_decision(),
            mode=ExecutionMode.SIGNAL_ONLY,
        )
        assert first["signal_id"] is not None

        duplicate = await engine.route(
            market=MarketType.CRYPTO,
            term=Term.SCALP,
            symbol="ZECUSDT",
            decision=_decision(),
            mode=ExecutionMode.SIGNAL_ONLY,
        )
        assert duplicate["signal_id"] is None
        assert duplicate["existing_signal_id"] == first["signal_id"]
        assert duplicate["reason"] == "duplicate_open_signal"
        assert await _signal_ids() == [first["signal_id"]]

        await _mark_resolved(first["signal_id"])
        next_signal = await engine.route(
            market=MarketType.CRYPTO,
            term=Term.SCALP,
            symbol="ZECUSDT",
            decision=_decision(),
            mode=ExecutionMode.SIGNAL_ONLY,
        )
        assert next_signal["signal_id"] is not None
        assert next_signal["signal_id"] != first["signal_id"]
        assert await _signal_ids() == [first["signal_id"], next_signal["signal_id"]]

    asyncio.run(run())
