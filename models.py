"""SQLAlchemy 2.0 async ORM layer for TradeRay.

Default backend is SQLite via aiosqlite. Swap to PostgreSQL by setting
DATABASE_URL=postgresql+asyncpg://user:pass@host/db in .env — no code changes.

Schema:
  - signals       : actionable multi-market alerts (LONG/SHORT) are logged here
  - trades        : Crypto-only — only persisted when an order actually hits Binance
  - market_config : per-market runtime config (active flag, term, execution mode)
"""
from __future__ import annotations

import enum
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    event,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool


# ---------------------------------------------------------------------------
# Enums — all string-valued so they round-trip cleanly through JSON / Postgres
# ---------------------------------------------------------------------------

class MarketType(str, enum.Enum):
    CRYPTO = "CRYPTO"
    BIST = "BIST"
    SP500 = "SP500"
    NASDAQ = "NASDAQ"


class ExecutionMode(str, enum.Enum):
    AUTO_BOT = "AUTO_BOT"
    SIGNAL_ONLY = "SIGNAL_ONLY"


class Term(str, enum.Enum):
    SCALP = "SCALP"             # 5m + 15m
    SHORT_TERM = "SHORT_TERM"   # 1h + 4h, ~1 week horizon
    MID_TERM = "MID_TERM"       # 1d, ~1 month horizon


class SignalAction(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"     # entry limit placed, not filled
    OPEN = "OPEN"           # filled, brackets active
    CLOSED = "CLOSED"       # SL/TP hit or manually closed
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"   # blocked by risk manager


# --- Audit trail enums ----------------------------------------------------

class AuditCategory(str, enum.Enum):
    BOT = "BOT"        # Crypto AUTO_BOT — real orders attempted
    SIGNAL = "SIGNAL"  # Signal-only path (incl. coerced traditional markets)


class AuditMode(str, enum.Enum):
    AI_ENABLED = "AI_ENABLED"
    RULE_BASED_ONLY = "RULE_BASED_ONLY"


class AuditOutcome(str, enum.Enum):
    EXECUTED    = "EXECUTED"     # Order actually placed on exchange
    SIGNAL_SENT = "SIGNAL_SENT"  # Signal logged, no order (signal-only mode)
    WAITED      = "WAITED"       # Decision = WAIT, no action taken
    REJECTED    = "REJECTED"     # Risk / filter / TP_SL gate rejected
    ERROR       = "ERROR"        # Exception in the cycle pipeline


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Single declarative base — keeps metadata coherent for migrations."""


# ---------------------------------------------------------------------------
# Signal — every AI alert across every market lands here
# ---------------------------------------------------------------------------

class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    market: Mapped[MarketType] = mapped_column(
        SAEnum(MarketType, native_enum=False), index=True, nullable=False
    )
    term: Mapped[Term] = mapped_column(
        SAEnum(Term, native_enum=False), nullable=False
    )
    action: Mapped[SignalAction] = mapped_column(
        SAEnum(SignalAction, native_enum=False), index=True, nullable=False
    )
    confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Trade plan (null on WAIT)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    risk_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reward_risk_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    leverage: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Component scores (so we can post-hoc analyse why the brain decided what it did)
    quant_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sentiment_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fear_greed_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    macro_regime: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    justification: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    trades: Mapped[list["Trade"]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_signals_market_created", "market", "created_at"),
        Index("ix_signals_symbol_created", "symbol", "created_at"),
    )


# ---------------------------------------------------------------------------
# Trade — crypto-only, written ONLY when real orders are placed
# ---------------------------------------------------------------------------

class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )

    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG/SHORT
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    quantity_base: Mapped[float] = mapped_column(Float, nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TradeStatus] = mapped_column(
        SAEnum(TradeStatus, native_enum=False),
        default=TradeStatus.PENDING,
        nullable=False,
        index=True,
    )

    binance_order_ids: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    realized_pnl_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    signal: Mapped["Signal"] = relationship(back_populates="trades")


# ---------------------------------------------------------------------------
# MarketConfig — runtime control surface for each market
# ---------------------------------------------------------------------------

class MarketConfig(Base):
    __tablename__ = "market_config"

    market: Mapped[MarketType] = mapped_column(
        SAEnum(MarketType, native_enum=False), primary_key=True
    )
    # COLD START defaults — every flag off. User opts in from the dashboard.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    use_ai: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    term: Mapped[Term] = mapped_column(
        SAEnum(Term, native_enum=False), default=Term.SHORT_TERM, nullable=False
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        SAEnum(ExecutionMode, native_enum=False),
        default=ExecutionMode.SIGNAL_ONLY,
        nullable=False,
    )
    # Empty by default — Dynamic Screener supplies symbols per cycle.
    # The dashboard can override with a comma-separated list to pin specific tickers.
    symbols_csv: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols_csv.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# LLMCostLog — one row per Anthropic Messages API call
#
# Persisted by orchestrator._log_llm_cost() after every call_agent() success.
# The orchestrator computes `estimated_cost_usd` from the token counts using
# Anthropic's published per-million pricing (currently $15 in / $75 out for
# Opus 4.7 — see orchestrator.LLM_PRICING).
# ---------------------------------------------------------------------------

class LLMCostLog(Base):
    __tablename__ = "llm_cost_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Attribution — nullable because future call sites may not be symbol-scoped
    market: Mapped[Optional[MarketType]] = mapped_column(
        SAEnum(MarketType, native_enum=False), nullable=True, index=True,
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    agent_label: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # "quant" | "sentiment" | "master"

    # Model + token counts straight from the Anthropic Messages API response
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    estimated_cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    __table_args__ = (
        # Dashboard's "today's cost" filter needs (created_at) — already covered
        # by single-column index above. Composite (created_at, market) helps
        # the per-market breakdown query.
        Index("ix_llm_cost_logs_created_market", "created_at", "market"),
        Index("ix_llm_cost_logs_created_agent", "created_at", "agent_label"),
    )


# ---------------------------------------------------------------------------
# DecisionAudit — full transparency log of EVERY symbol cycle outcome.
#
# One row per `run_symbol_cycle` exit, regardless of branch (executed,
# signal-only, waited, rejected, error). The `logic_trace` JSON field
# captures the full reasoning at every stage — indicators, rule-engine
# verdict, AI analysis (when use_ai=True), validation results, execution
# metadata. The dashboard's "Decision Trace" tab reads this directly.
#
# Why a separate table from `signals`?
#   - `signals` row only exists when a non-WAIT decision was made AND
#     passed the strict TP/SL gate. Audit MUST also capture WAITs and
#     pre-persistence rejections.
#   - Audit is observational + immutable; signals are operational state.
# ---------------------------------------------------------------------------

class DecisionAudit(Base):
    __tablename__ = "decision_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    category: Mapped[AuditCategory] = mapped_column(
        SAEnum(AuditCategory, native_enum=False), nullable=False, index=True,
    )
    market: Mapped[MarketType] = mapped_column(
        SAEnum(MarketType, native_enum=False), nullable=False, index=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mode: Mapped[AuditMode] = mapped_column(
        SAEnum(AuditMode, native_enum=False), nullable=False, index=True,
    )
    outcome: Mapped[AuditOutcome] = mapped_column(
        SAEnum(AuditOutcome, native_enum=False), nullable=False, index=True,
    )

    # JSON trace of the full reasoning path. Shape (all keys optional):
    #   {
    #     "indicators":   { rsi, macd_hist, ema_slow, atr, last_close, ... },
    #     "rule_engine":  { decision, confidence, justification, tp, sl },
    #     "ai_analysis":  null OR {
    #         "quant_report": {...}, "sentiment_report": {...},
    #         "master_decision": {...},   # raw Master Trader JSON, untruncated
    #     },
    #     "validation":   { entry, tp, sl, rr, risk_usd, gates_passed: [...] },
    #     "execution":    { mode_requested, mode_applied, executed,
    #                        trade_id, signal_id, route_reason }
    #   }
    logic_trace: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    reason: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    __table_args__ = (
        Index("ix_audit_created_outcome", "created_at", "outcome"),
        Index("ix_audit_market_symbol", "market", "symbol"),
        Index("ix_audit_category_mode", "category", "mode"),
    )


# ---------------------------------------------------------------------------
# Engine + session factory + helpers
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///traderay.db")
_IS_SQLITE: bool = DATABASE_URL.startswith("sqlite")
_IS_POSTGRES: bool = DATABASE_URL.startswith("postgresql")

# ---------------------------------------------------------------------------
# Engine configuration — covers two production backends with one factory.
#
# ── SQLite (local dev, default) ─────────────────────────────────────────────
# Concurrent async writers (orchestrator + tracker + UI) without WAL guarantee
# `database is locked`. We:
#   - extend the aiosqlite-level connection lock timeout to 30s (connect_args)
#   - issue PRAGMA journal_mode=WAL on every new connection — readers stop
#     blocking writers; writers stop blocking readers
#   - PRAGMA synchronous=NORMAL — fsync at WAL checkpoints only, not every commit
#   - PRAGMA busy_timeout=10000 — 10s wait at the SQLite level if the writer
#     lock is held (complementary to the connect_args timeout)
#   - PRAGMA foreign_keys=ON — SQLite ships with FKs disabled by default
# These pragmas are connection-scoped — the event listener re-applies them
# whenever the pool spins up a fresh connection.
#
# ── PostgreSQL via asyncpg, behind PgBouncer (Supabase pooler :6543) ────────
# Supabase's transaction-mode pooler at port 6543 does NOT support server-side
# prepared statements. asyncpg's default behavior caches and re-uses prepared
# statements by name — with PgBouncer rotating physical backends mid-session,
# the cached statement name doesn't exist on the next connection and EVERY
# query fails with `prepared statement "__asyncpg_…" does not exist`.
#
# The fix is to disable prepared statement caching at BOTH layers:
#   - `statement_cache_size=0`           : asyncpg-side caching off
#   - `prepared_statement_cache_size=0`  : SQLAlchemy asyncpg-adapter caching off
# With both set to 0, every query is issued as a simple unnamed query —
# safe under transaction-pooled PgBouncer.
#
# For Supabase's SESSION-mode pooler (port :5432) these flags are harmless
# (caching just gets disabled needlessly); leaving them on simplifies the
# config and prevents accidental misuse of the wrong endpoint.
# ---------------------------------------------------------------------------

_engine_kwargs: dict[str, Any] = {
    "echo": os.getenv("DB_ECHO", "false").lower() == "true",
    "future": True,
    "pool_pre_ping": True,
}
if _IS_SQLITE:
    _engine_kwargs["connect_args"] = {"timeout": 30}
elif _IS_POSTGRES:
    _engine_kwargs["connect_args"] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }

# NullPool for Streamlit / short-lived async clients.
#
# Streamlit calls `asyncio.run(coro)` on every script rerun, which creates
# a fresh event loop each time. SQLAlchemy's default pool keeps asyncpg
# connections alive across calls — those connections are bound to the
# FIRST loop and explode with "Future attached to a different loop" the
# next time Streamlit reruns.
#
# NullPool opens a brand-new connection per checkout and closes it on
# release — every connection lives entirely inside the one asyncio.run
# loop that created it. Slight overhead, but dashboards are low-traffic
# (1 user, 30s auto-refresh) so it's invisible in practice.
#
# Backend uses the default pool because main.py runs ONE persistent loop.
if os.getenv("SQLA_DISABLE_POOL", "false").lower() == "true":
    _engine_kwargs["poolclass"] = NullPool
    _engine_kwargs.pop("pool_pre_ping", None)  # NullPool has no ping concept

_engine = create_async_engine(DATABASE_URL, **_engine_kwargs)


if _IS_SQLITE:

    @event.listens_for(_engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        """Apply WAL + busy_timeout + FK pragmas on every new pooled connection.

        Listening on `_engine.sync_engine` (not the AsyncEngine) is the
        correct hook for aiosqlite — the event runs synchronously inside
        the connection-establishment path before any user code sees the
        connection.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine, expire_on_commit=False, class_=AsyncSession
)


def get_engine():
    return _engine


async def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every boot."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Use as: `async with session_scope() as s: ...`"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def log_decision_audit(
    *,
    category: AuditCategory,
    market: MarketType,
    symbol: str,
    mode: AuditMode,
    outcome: AuditOutcome,
    logic_trace: dict[str, Any],
    reason: str,
) -> None:
    """Persist one DecisionAudit row. Best-effort — never raises.

    Audit logging MUST NOT break the trading cycle. Any failure here is
    swallowed and logged at WARNING — the next cycle still runs.
    """
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                DecisionAudit(
                    category=category,
                    market=market,
                    symbol=symbol,
                    mode=mode,
                    outcome=outcome,
                    logic_trace=logic_trace,
                    reason=(reason or "")[:512],
                )
            )
            await session.commit()
    except Exception as e:
        # Lazy logger import to avoid circular dep at module top
        from core.logger import get_logger
        get_logger(__name__).warning("audit.log_failed", err=str(e))


async def seed_default_market_config() -> None:
    """COLD START seeder — one row per MarketType, every flag OFF.

    All MarketConfig rows start with:
      - enabled=False           : bot doesn't trade this market
      - use_ai=False            : rule engine drives decisions (zero token cost)
      - execution_mode=SIGNAL_ONLY : no real orders even if `enabled` flips on
      - symbols_csv=""          : Dynamic Screener fills the symbol list each cycle

    The user enables exactly what they want from the dashboard sidebar.
    NO HARDCODED SYMBOLS anywhere — every list is screener-driven.
    """
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(MarketConfig.market))).scalars().all()
        existing_set = set(existing)
        for market in MarketType:
            if market in existing_set:
                continue
            session.add(
                MarketConfig(
                    market=market,
                    enabled=False,
                    use_ai=False,
                    term=Term.SHORT_TERM,
                    execution_mode=ExecutionMode.SIGNAL_ONLY,
                    symbols_csv="",
                )
            )
        await session.commit()


__all__ = [
    "Base",
    "MarketType",
    "ExecutionMode",
    "Term",
    "SignalAction",
    "TradeStatus",
    "AuditCategory",
    "AuditMode",
    "AuditOutcome",
    "Signal",
    "Trade",
    "MarketConfig",
    "LLMCostLog",
    "DecisionAudit",
    "AsyncSessionLocal",
    "get_engine",
    "init_db",
    "session_scope",
    "seed_default_market_config",
    "log_decision_audit",
    "DATABASE_URL",
]
