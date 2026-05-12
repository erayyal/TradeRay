"""TradeRay — Advanced Streamlit Dashboard (resolution-aware).

Reads from the SQLite/Postgres ORM (signals, trades, market_config) and
Redis (latest decisions + cached prices). Writes user toggles back to
MarketConfig + a Redis flag for the dynamic screener.

PnL Matrix data sources, by priority:

    Realized PnL (Crypto AUTO_BOT)
        Trade.realized_pnl_usd  ←  set by tracker.sync_binance_orders()

    Theoretical PnL (all signals)
        Per-signal preference:
          1. Signal.raw_payload["resolution"]  ←  set by tracker.sync_theoretical_signals()
                                                  Authoritative — replay-confirmed
                                                  win/loss with exact exit price.
          2. Snapshot heuristic                 ←  computed in this file.
                                                  current_price vs entry/TP/SL,
                                                  used only when no resolution
                                                  exists yet ("Floating / MTM").

Layout:
  Sidebar         : Market controls (enable, term, exec_mode, screener)
  PnL Matrix      : Daily / Weekly / Monthly with resolved + floating split
  Tab 1           : Active Signals (with resolution status badge)
  Tab 2           : Executed Trades
  Tab 3           : Latest Decisions (chart-aware decision viewer)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import redis.asyncio as redis_asyncio
import streamlit as st
from sqlalchemy import select

from config import settings
from models import (
    AsyncSessionLocal,
    ExecutionMode,
    LLMCostLog,
    MarketConfig,
    MarketType,
    Signal,
    SignalAction,
    Term,
    Trade,
    TradeStatus,
)


# ============================================================================
# Page setup
# ============================================================================

st.set_page_config(
    page_title="TradeRay — Global Financial Terminal",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="expanded",
)

# Auto-refresh every 30 seconds
st.markdown("<meta http-equiv='refresh' content='30'>", unsafe_allow_html=True)


# ============================================================================
# Async helpers
# ============================================================================

def _run_async(coro):
    return asyncio.run(coro)


# -- DB reads ----------------------------------------------------------------

async def _aload_market_configs() -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(MarketConfig))).scalars().all()
    return [
        {
            "market": r.market.value,
            "enabled": r.enabled,
            "term": r.term.value,
            "execution_mode": r.execution_mode.value,
            "symbols_csv": r.symbols_csv,
            "last_run_at": r.last_run_at,
        }
        for r in rows
    ]


async def _asave_market_config(
    *, market: MarketType, enabled: bool, term: Term,
    execution_mode: ExecutionMode, symbols_csv: str,
) -> None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(MarketConfig).where(MarketConfig.market == market)
            )
        ).scalar_one_or_none()
        if row is None:
            row = MarketConfig(market=market)
            session.add(row)
        row.enabled = enabled
        row.term = term
        row.execution_mode = execution_mode
        row.symbols_csv = symbols_csv
        await session.commit()


async def _aload_signals(
    *, days_back: int = 30, market: str | None = None
) -> list[dict[str, Any]]:
    """Load signals + raw_payload (the tracker stores resolution there)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    stmt = (
        select(Signal)
        .where(Signal.created_at >= cutoff)
        .order_by(Signal.created_at.desc())
    )
    if market:
        stmt = stmt.where(Signal.market == MarketType(market))
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "market": r.market.value,
            "term": r.term.value,
            "symbol": r.symbol,
            "action": r.action.value,
            "confidence": r.confidence,
            "entry": r.entry_price,
            "tp": r.take_profit,
            "sl": r.stop_loss,
            "risk_usd": r.risk_usd,
            "rr": r.reward_risk_ratio,
            "leverage": r.leverage,
            "quant_score": r.quant_score,
            "sentiment_score": r.sentiment_score,
            "macro_regime": r.macro_regime,
            "justification": r.justification,
            "raw_payload": r.raw_payload or {},
        }
        for r in rows
    ]


async def _aload_cost_logs(*, days_back: int = 7) -> list[dict[str, Any]]:
    """Load LLM cost rows from the last `days_back` days, newest first.

    Default 7d keeps the KPI math cheap (a busy day produces ~1k–5k rows).
    The KPI strip filters down to "today" in Python; the tab table caller
    can re-filter or slice further.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(LLMCostLog)
                .where(LLMCostLog.created_at >= cutoff)
                .order_by(LLMCostLog.created_at.desc())
            )
        ).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "market": r.market.value if r.market else None,
            "symbol": r.symbol,
            "agent_label": r.agent_label,
            "model": r.model,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "estimated_cost_usd": r.estimated_cost_usd,
        }
        for r in rows
    ]


async def _aload_trades(*, days_back: int = 90) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    stmt = select(Trade).where(Trade.created_at >= cutoff).order_by(
        Trade.created_at.desc()
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "closed_at": r.closed_at,
            "symbol": r.symbol,
            "side": r.side,
            "entry": r.entry_price,
            "tp": r.take_profit,
            "sl": r.stop_loss,
            "qty": r.quantity_base,
            "leverage": r.leverage,
            "status": r.status.value,
            "realized_pnl_usd": r.realized_pnl_usd,
            "client_order_id": r.client_order_id,
        }
        for r in rows
    ]


# -- Redis reads -------------------------------------------------------------

async def _aload_redis_state() -> dict[str, Any]:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    out: dict[str, Any] = {"prices": {}, "decisions": {}, "screener_flags": {}}
    try:
        cfgs = await _aload_market_configs()
        for cfg in cfgs:
            for sym in [s.strip() for s in cfg["symbols_csv"].split(",") if s.strip()]:
                p = await r.get(f"price:{sym}")
                out["prices"][sym] = float(p) if p else None
                d = await r.get(f"decision:{sym}:latest")
                if d:
                    try:
                        out["decisions"][sym] = json.loads(d)
                    except json.JSONDecodeError:
                        pass
            flag = await r.get(f"config:{cfg['market']}:dynamic_screener")
            out["screener_flags"][cfg["market"]] = (flag == "1")
    finally:
        await r.aclose()
    return out


async def _aset_screener_flag(market: str, value: bool) -> None:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set(f"config:{market}:dynamic_screener", "1" if value else "0")
    finally:
        await r.aclose()


async def _aread_system_enabled() -> bool:
    """Master switch state — `config:system_enabled` Redis key. False default."""
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        return (await r.get("config:system_enabled")) == "1"
    finally:
        await r.aclose()


async def _awrite_system_enabled(value: bool) -> None:
    r = redis_asyncio.from_url(settings.redis_url, decode_responses=True)
    try:
        await r.set("config:system_enabled", "1" if value else "0")
    finally:
        await r.aclose()


# -- Streamlit cache wrappers ------------------------------------------------

@st.cache_data(ttl=10)
def load_market_configs() -> list[dict[str, Any]]:
    return _run_async(_aload_market_configs())


@st.cache_data(ttl=10)
def load_signals(days_back: int = 30) -> list[dict[str, Any]]:
    return _run_async(_aload_signals(days_back=days_back))


@st.cache_data(ttl=10)
def load_trades(days_back: int = 90) -> list[dict[str, Any]]:
    return _run_async(_aload_trades(days_back=days_back))


@st.cache_data(ttl=10)
def load_cost_logs(days_back: int = 7) -> list[dict[str, Any]]:
    return _run_async(_aload_cost_logs(days_back=days_back))


@st.cache_data(ttl=10)
def load_redis_state() -> dict[str, Any]:
    return _run_async(_aload_redis_state())


@st.cache_data(ttl=5)
def load_system_enabled() -> bool:
    """Cached read of the master switch (5s TTL — propagation matters)."""
    return _run_async(_aread_system_enabled())


# ============================================================================
# Resolution helpers — single source of truth for "what does the tracker say
# about this signal" — used by both the PnL math and the signal table badge.
# ============================================================================

def _signal_resolution(signal: dict[str, Any]) -> dict[str, Any] | None:
    """Return the tracker-written resolution dict, or None if not resolved."""
    return (signal.get("raw_payload") or {}).get("resolution") or None


def _resolution_status(signal: dict[str, Any]) -> str:
    """Short label for the Active Signals table."""
    if signal["action"] == SignalAction.WAIT.value:
        return "—"
    res = _signal_resolution(signal)
    if not res:
        return "OPEN"
    return res.get("status", "OPEN")


# ============================================================================
# PnL math
# ============================================================================

def _bucket_window(now: datetime) -> dict[str, datetime]:
    today_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return {
        "daily": today_utc,
        "weekly": today_utc - timedelta(days=today_utc.weekday()),
        "monthly": datetime(now.year, now.month, 1, tzinfo=timezone.utc),
    }


def compute_realized_pnl(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Crypto AUTO_BOT realized PnL — only Trades that have closed."""
    now = datetime.now(timezone.utc)
    windows = _bucket_window(now)
    closed = [
        t for t in trades
        if t["status"] == TradeStatus.CLOSED.value
        and t["realized_pnl_usd"] is not None
        and t["closed_at"] is not None
    ]

    out: dict[str, dict[str, Any]] = {}
    for label, start in windows.items():
        slice_ = [t for t in closed if t["closed_at"] >= start]
        wins = [t for t in slice_ if (t["realized_pnl_usd"] or 0) > 0]
        losses = [t for t in slice_ if (t["realized_pnl_usd"] or 0) <= 0]
        total_pnl = sum((t["realized_pnl_usd"] or 0) for t in slice_)
        out[label] = {
            "n_trades": len(slice_),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "win_rate": (len(wins) / len(slice_)) if slice_ else 0.0,
            "pnl_usd": total_pnl,
            "avg_pnl_per_trade": (total_pnl / len(slice_)) if slice_ else 0.0,
        }
    return out


def compute_theoretical_pnl(
    signals: list[dict[str, Any]], current_prices: dict[str, float | None]
) -> dict[str, dict[str, Any]]:
    """Theoretical PnL with resolution-first / snapshot-fallback logic.

    For each non-WAIT signal in the window:
      1. If raw_payload["resolution"] exists  → use the tracker's authoritative
         result (`outcome` and `theoretical_pnl_usd`). Counts as resolved.
      2. Otherwise → snapshot heuristic against the current market price.
         Counts as floating MTM (open) — even if the snapshot says "TP touched
         right now", we treat it as floating until the tracker confirms.

    Position size for fallback: risk_usd / |entry - SL| (matches the executor's
    sizing math). Win caps at +risk_usd × R:R, loss floors at -risk_usd.
    """
    now = datetime.now(timezone.utc)
    windows = _bucket_window(now)

    out: dict[str, dict[str, Any]] = {}
    for label, start in windows.items():
        slice_ = [
            s for s in signals
            if s["created_at"] >= start
            and s["action"] in (SignalAction.LONG.value, SignalAction.SHORT.value)
            and s["entry"] is not None and s["sl"] is not None and s["tp"] is not None
            and s["risk_usd"] is not None and s["risk_usd"] > 0
        ]

        wins_resolved = 0
        losses_resolved = 0
        open_n = 0
        pnl_resolved = 0.0
        pnl_floating_mtm = 0.0

        for s in slice_:
            # ---- 1. Resolution path (authoritative) -------------------------
            res = _signal_resolution(s)
            if res:
                outcome = res.get("outcome")  # "TP" | "SL"
                pnl = res.get("theoretical_pnl_usd") or 0.0
                pnl_resolved += pnl
                if outcome == "TP":
                    wins_resolved += 1
                else:
                    losses_resolved += 1
                continue

            # ---- 2. Snapshot path (floating MTM) ---------------------------
            entry, sl, tp, risk = s["entry"], s["sl"], s["tp"], s["risk_usd"]
            current = current_prices.get(s["symbol"])
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                continue
            size_base = risk / risk_per_unit
            is_long = s["action"] == SignalAction.LONG.value

            if current is None:
                open_n += 1
                continue

            # We deliberately do NOT count snapshot TP/SL touches as resolved
            # — that's the tracker's job. Until the tracker confirms with a
            # candle replay, anything still in raw_payload-without-resolution
            # is "open" for accounting purposes.
            open_n += 1
            if is_long:
                pnl_floating_mtm += (current - entry) * size_base
            else:
                pnl_floating_mtm += (entry - current) * size_base

        n_total = len(slice_)
        n_resolved = wins_resolved + losses_resolved
        out[label] = {
            "n_signals": n_total,
            "n_wins": wins_resolved,
            "n_losses": losses_resolved,
            "n_open": open_n,
            "win_rate": (wins_resolved / n_resolved) if n_resolved else 0.0,
            "pnl_resolved": pnl_resolved,
            "pnl_floating_mtm": pnl_floating_mtm,
            "pnl_total": pnl_resolved + pnl_floating_mtm,
        }
    return out


# ============================================================================
# UI — Sidebar
# ============================================================================

def _term_index(value: str) -> int:
    order = [Term.SCALP.value, Term.SHORT_TERM.value, Term.MID_TERM.value]
    try:
        return order.index(value)
    except ValueError:
        return 1


def render_sidebar(configs: list[dict[str, Any]], screener_flags: dict[str, bool]) -> None:
    st.sidebar.title("⚙️ Market Controls")
    st.sidebar.caption(
        f"Model: `{settings.anthropic_model}` · "
        f"Binance **{'Testnet' if settings.binance_testnet else 'LIVE'}**"
    )

    # -------- MASTER SWITCH --------
    # When OFF (default), the scheduler skips every market cycle — no LLM
    # calls, no orders, no token spend. Tracker jobs still run to resolve
    # in-flight trades, but no new decisions are made.
    system_enabled = load_system_enabled()
    status_emoji = "🟢" if system_enabled else "🔴"
    status_word = "RUNNING" if system_enabled else "PAUSED"
    st.sidebar.markdown(f"### {status_emoji} System: **{status_word}**")
    new_state = st.sidebar.toggle(
        "Enable bot (master switch)",
        value=system_enabled,
        key="master_switch",
        help=(
            "When OFF, every market cycle is skipped. No LLM calls, no orders, "
            "no token spend. Tracker still reconciles open positions."
        ),
    )
    if new_state != system_enabled:
        _run_async(_awrite_system_enabled(new_state))
        st.cache_data.clear()
        st.success(
            "✅ Bot RESUMED — cycles will fire on next tick."
            if new_state else
            "⏸ Bot PAUSED — no new cycles will run."
        )
        st.rerun()
    st.sidebar.markdown("---")

    cfg_by_market = {c["market"]: c for c in configs}

    for market_value in (m.value for m in MarketType):
        cfg = cfg_by_market.get(market_value)
        if cfg is None:
            st.sidebar.warning(f"No config row for {market_value} — boot the backend.")
            continue

        with st.sidebar.expander(f"🌐 {market_value}", expanded=(market_value == "CRYPTO")):
            enabled = st.toggle(
                "Enabled", value=cfg["enabled"], key=f"enabled:{market_value}",
            )

            term_value = st.radio(
                "Term",
                options=[Term.SCALP.value, Term.SHORT_TERM.value, Term.MID_TERM.value],
                index=_term_index(cfg["term"]),
                horizontal=False,
                key=f"term:{market_value}",
            )

            if market_value == MarketType.CRYPTO.value:
                exec_mode = st.radio(
                    "Execution",
                    options=[ExecutionMode.SIGNAL_ONLY.value, ExecutionMode.AUTO_BOT.value],
                    index=(0 if cfg["execution_mode"] == ExecutionMode.SIGNAL_ONLY.value else 1),
                    horizontal=True,
                    key=f"exec:{market_value}",
                    help="AUTO_BOT places real orders on Binance Testnet.",
                )
            else:
                st.info(
                    f"🔒 {market_value} is **SIGNAL_ONLY** (hard-locked in execution/engine.py)."
                )
                exec_mode = ExecutionMode.SIGNAL_ONLY.value

            screener_on = st.toggle(
                "Dynamic screener",
                value=screener_flags.get(market_value, False),
                key=f"screener:{market_value}",
                help=(
                    "If ON, the orchestrator overrides the symbol list with "
                    "`fetcher.get_dynamic_symbols()` on each tick."
                ),
            )

            symbols_csv = st.text_input(
                "Symbols (comma-separated)",
                value=cfg["symbols_csv"],
                key=f"symbols:{market_value}",
                disabled=screener_on,
                help="Disabled while dynamic screener is ON.",
            )

            if cfg["last_run_at"]:
                st.caption(f"Last run: {cfg['last_run_at']:%Y-%m-%d %H:%M UTC}")

            if st.button("Apply", key=f"apply:{market_value}", type="primary"):
                _run_async(
                    _asave_market_config(
                        market=MarketType(market_value),
                        enabled=enabled,
                        term=Term(term_value),
                        execution_mode=ExecutionMode(exec_mode),
                        symbols_csv=symbols_csv,
                    )
                )
                _run_async(_aset_screener_flag(market_value, screener_on))
                st.cache_data.clear()
                st.success(f"{market_value} updated. New cadence applies on next reload.")
                st.rerun()


# ============================================================================
# UI — PnL Matrix
# ============================================================================

def _render_pnl_card(*, title: str, realized: dict, theoretical: dict) -> None:
    st.subheader(title)
    cols = st.columns(2)

    with cols[0]:
        st.markdown("**🪙 Crypto AUTO_BOT — Realized**")
        st.metric(
            "PnL (USD)",
            f"${realized['pnl_usd']:,.2f}",
            delta=(
                f"{realized['n_wins']}W / {realized['n_losses']}L "
                f"({realized['win_rate']:.0%})"
            ),
        )
        st.caption(
            f"{realized['n_trades']} closed trades · "
            f"avg ${realized['avg_pnl_per_trade']:,.2f}"
        )

    with cols[1]:
        st.markdown("**📡 All Signals — Theoretical**")
        st.metric(
            "PnL (USD)",
            f"${theoretical['pnl_total']:,.2f}",
            delta=(
                f"{theoretical['n_wins']}W / {theoretical['n_losses']}L "
                f"· {theoretical['n_open']} open"
            ),
        )
        st.caption(
            f"resolved ${theoretical['pnl_resolved']:,.2f} · "
            f"floating MTM ${theoretical['pnl_floating_mtm']:,.2f} · "
            f"win rate {theoretical['win_rate']:.0%}"
        )


def render_pnl_matrix(trades: list[dict], signals: list[dict], prices: dict[str, float | None]) -> None:
    realized = compute_realized_pnl(trades)
    theoretical = compute_theoretical_pnl(signals, prices)

    st.markdown("### 💰 PnL Matrix")
    st.caption(
        "**Realized PnL** = closed Crypto AUTO_BOT trades (Trade.realized_pnl_usd, "
        "set by the tracker after Binance reconciliation). "
        "**Theoretical PnL** = signal performance, resolution-first: the tracker "
        "replays candles and writes `Signal.raw_payload['resolution']` with the "
        "exact win/loss; signals without a resolution are shown as floating MTM "
        "(current price vs entry). Win rate counts only resolved signals."
    )

    cols = st.columns(3)
    with cols[0]:
        _render_pnl_card(title="📅 Daily", realized=realized["daily"], theoretical=theoretical["daily"])
    with cols[1]:
        _render_pnl_card(title="📆 Weekly (Mon→now)", realized=realized["weekly"], theoretical=theoretical["weekly"])
    with cols[2]:
        _render_pnl_card(title="🗓️ Monthly (MTD)", realized=realized["monthly"], theoretical=theoretical["monthly"])


# ============================================================================
# UI — Tabs
# ============================================================================

def render_signals_tab(signals: list[dict]) -> None:
    st.markdown("### 📡 Active Signals (last 30 days)")

    fcols = st.columns([1, 1, 1, 2])
    with fcols[0]:
        market_filter = st.selectbox(
            "Market", options=["ALL"] + [m.value for m in MarketType], index=0,
        )
    with fcols[1]:
        action_filter = st.selectbox(
            "Action", options=["ALL", "LONG", "SHORT", "WAIT"], index=0,
        )
    with fcols[2]:
        resolution_filter = st.selectbox(
            "Resolution",
            options=["ALL", "OPEN", "RESOLVED_TP", "RESOLVED_SL"],
            index=0,
        )

    rows = signals
    if market_filter != "ALL":
        rows = [s for s in rows if s["market"] == market_filter]
    if action_filter != "ALL":
        rows = [s for s in rows if s["action"] == action_filter]
    if resolution_filter != "ALL":
        rows = [s for s in rows if _resolution_status(s) == resolution_filter]

    if not rows:
        st.info("No signals match the current filter.")
        return

    # Decorate each row with resolution badge + theoretical PnL (if resolved)
    enriched = []
    for s in rows:
        res = _signal_resolution(s)
        enriched.append(
            {
                **s,
                "resolution": _resolution_status(s),
                "theoretical_pnl_usd": (res or {}).get("theoretical_pnl_usd"),
                "exit_price": (res or {}).get("exit_price"),
            }
        )

    df = pd.DataFrame(enriched)
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
    cols_to_show = [
        "created_at", "market", "term", "symbol", "action", "resolution",
        "confidence", "entry", "tp", "sl", "exit_price", "rr",
        "theoretical_pnl_usd", "risk_usd", "quant_score", "sentiment_score",
        "macro_regime",
    ]
    cols_to_show = [c for c in cols_to_show if c in df.columns]
    st.dataframe(df[cols_to_show], use_container_width=True, hide_index=True)


def render_trades_tab(trades: list[dict]) -> None:
    st.markdown("### 🪙 Executed Crypto Trades (last 90 days)")
    if not trades:
        st.info("No trades executed yet — Crypto must be in AUTO_BOT mode.")
        return

    df = pd.DataFrame(trades)
    df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%Y-%m-%d %H:%M")
    if "closed_at" in df.columns:
        df["closed_at"] = pd.to_datetime(df["closed_at"]).dt.strftime("%Y-%m-%d %H:%M")
    cols = [
        "created_at", "closed_at", "symbol", "side", "entry", "tp", "sl",
        "qty", "leverage", "status", "realized_pnl_usd", "client_order_id",
    ]
    cols = [c for c in cols if c in df.columns]
    st.dataframe(df[cols], use_container_width=True, hide_index=True)


def render_latest_decision_tab(decisions: dict[str, dict], prices: dict[str, float | None]) -> None:
    st.markdown("### 🧠 Latest Decisions per Symbol")
    if not decisions:
        st.info("No decisions cached in Redis yet — wait for the first scheduler tick.")
        return

    items = sorted(
        decisions.items(),
        key=lambda kv: kv[1].get("produced_at", ""),
        reverse=True,
    )

    for symbol, bundle in items:
        d = bundle.get("decision", {})
        action = d.get("decision", "—")
        confidence = d.get("confidence_level", d.get("confidence", 0)) or 0
        market = bundle.get("market", "?")
        term = bundle.get("term", "?")
        produced = bundle.get("produced_at", "")
        price = prices.get(symbol)
        pending = bundle.get("pending_order")

        color = {
            "LONG": "🟢", "SHORT": "🔴", "WAIT": "🟡", "CANCEL_PENDING": "🟣",
        }.get(action, "⚪")
        header = (
            f"{color} {symbol} · {market} · {term} · {action} "
            f"({int(confidence)}%) · {produced[:16]}"
        )
        if pending:
            header += " · 📌 pending order on book"

        with st.expander(header, expanded=(action not in ("WAIT", "CANCEL_PENDING"))):
            top = st.columns([2, 1])
            with top[0]:
                st.markdown(
                    f"**Justification:** {d.get('justification') or d.get('rationale') or '—'}"
                )
                if d.get("chart_observations"):
                    st.markdown("**Chart observations:**")
                    for obs in d["chart_observations"]:
                        st.markdown(f"- {obs}")
                if d.get("rulebook_references"):
                    st.markdown(
                        "**Rulebook refs:** " + ", ".join(d["rulebook_references"])
                    )
                if d.get("conflict_flags"):
                    st.warning("Conflict flags: " + ", ".join(d["conflict_flags"]))
                if action == "CANCEL_PENDING" and d.get("cancel_target_client_id"):
                    st.error(
                        f"🛑 AI canceled pending order: "
                        f"`{d['cancel_target_client_id']}`"
                    )
                if pending:
                    st.info(
                        f"📌 Pending {pending.get('side')} order resting at "
                        f"${pending.get('entry_price'):,.2f} "
                        f"(age: {pending.get('age_hours', 0):.1f}h, "
                        f"trade #{pending.get('trade_id')})"
                    )

            with top[1]:
                st.metric("Live Price", f"{price:,.2f}" if price else "—")
                if action in ("LONG", "SHORT"):
                    st.metric("Entry", f"{d.get('entry_price') or d.get('entry'):,.2f}")
                    st.metric("Take-Profit", f"{d.get('take_profit'):,.2f}")
                    st.metric("Stop-Loss", f"{d.get('stop_loss'):,.2f}")
                    st.caption(
                        f"R:R {d.get('reward_risk_ratio', 0):.2f} · "
                        f"Risk ${d.get('risk_usd', 0):,.2f} · "
                        f"Lev {d.get('leverage', 1)}x"
                    )

            sub = st.columns(3)
            with sub[0]:
                if st.button("📋 Full decision JSON", key=f"json:{symbol}"):
                    st.code(json.dumps(d, indent=2, default=str), language="json")
            with sub[1]:
                if st.button("📊 Quant report", key=f"quant:{symbol}"):
                    st.json(bundle.get("quant"))
            with sub[2]:
                if st.button("📰 Sentiment report", key=f"sent:{symbol}"):
                    st.json(bundle.get("sentiment"))


# ============================================================================
# LLM cost aggregation + rendering
# ============================================================================

def compute_today_cost(cost_logs: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up the cost logs that fall inside the current UTC day.

    Returns:
        {
          "n_calls":           int,
          "total_cost_usd":    float,
          "total_input":       int,
          "total_output":      int,
          "by_agent":          {agent_label: {"n", "cost", "input", "output"}},
          "by_market":         {market_value: {"n", "cost"}},
        }
    """
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    todays = [r for r in cost_logs if r["created_at"] >= start]

    by_agent: dict[str, dict[str, Any]] = {}
    by_market: dict[str, dict[str, Any]] = {}

    total_cost = 0.0
    total_in = 0
    total_out = 0
    for r in todays:
        cost = float(r["estimated_cost_usd"] or 0.0)
        in_t = int(r["input_tokens"] or 0)
        out_t = int(r["output_tokens"] or 0)
        total_cost += cost
        total_in += in_t
        total_out += out_t

        a = by_agent.setdefault(
            r["agent_label"] or "unknown",
            {"n": 0, "cost": 0.0, "input": 0, "output": 0},
        )
        a["n"] += 1
        a["cost"] += cost
        a["input"] += in_t
        a["output"] += out_t

        if r["market"]:
            m = by_market.setdefault(
                r["market"], {"n": 0, "cost": 0.0}
            )
            m["n"] += 1
            m["cost"] += cost

    return {
        "n_calls": len(todays),
        "total_cost_usd": total_cost,
        "total_input": total_in,
        "total_output": total_out,
        "by_agent": by_agent,
        "by_market": by_market,
    }


def render_costs_tab(cost_logs: list[dict[str, Any]]) -> None:
    st.markdown("### 💰 LLM API Costs (today, UTC)")

    today = compute_today_cost(cost_logs)

    # Today's headline metrics
    cols = st.columns(4)
    cols[0].metric("API Calls", today["n_calls"])
    cols[1].metric("Cost (USD)", f"${today['total_cost_usd']:,.4f}")
    cols[2].metric("Input Tokens", f"{today['total_input']:,}")
    cols[3].metric("Output Tokens", f"{today['total_output']:,}")

    # By-agent breakdown — quick read on which agent is burning the budget
    if today["by_agent"]:
        st.markdown("#### By agent")
        rows = [
            {
                "Agent": agent,
                "Calls": stats["n"],
                "Input tokens": stats["input"],
                "Output tokens": stats["output"],
                "Cost (USD)": round(stats["cost"], 4),
            }
            for agent, stats in sorted(
                today["by_agent"].items(),
                key=lambda kv: kv[1]["cost"],
                reverse=True,
            )
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if today["by_market"]:
        st.markdown("#### By market")
        rows = [
            {
                "Market": market,
                "Calls": stats["n"],
                "Cost (USD)": round(stats["cost"], 4),
            }
            for market, stats in sorted(
                today["by_market"].items(),
                key=lambda kv: kv[1]["cost"],
                reverse=True,
            )
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Raw call log — today's calls newest-first
    st.markdown("#### Per-call log (today)")
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_rows = [r for r in cost_logs if r["created_at"] >= start]
    if not today_rows:
        st.info("No LLM calls logged yet today.")
    else:
        df = pd.DataFrame(today_rows)
        df["created_at"] = pd.to_datetime(df["created_at"]).dt.strftime("%H:%M:%S")
        df = df.rename(columns={
            "created_at": "Time (UTC)",
            "market": "Market",
            "symbol": "Symbol",
            "agent_label": "Agent",
            "model": "Model",
            "input_tokens": "In",
            "output_tokens": "Out",
            "estimated_cost_usd": "Cost (USD)",
        })
        cols_show = [
            "Time (UTC)", "Market", "Symbol", "Agent", "Model",
            "In", "Out", "Cost (USD)",
        ]
        cols_show = [c for c in cols_show if c in df.columns]
        st.dataframe(df[cols_show], use_container_width=True, hide_index=True)


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    configs = load_market_configs()
    redis_state = load_redis_state()
    signals = load_signals(days_back=30)
    trades = load_trades(days_back=90)
    cost_logs = load_cost_logs(days_back=7)

    st.title("📈 TradeRay — Global Financial Terminal")
    st.caption(
        f"As of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} · "
        f"Auto-refresh every 30s · "
        f"Redis: `{settings.redis_url}`"
    )

    render_sidebar(configs, redis_state.get("screener_flags", {}))

    # Top KPI strip (6 metrics; "API Cost (today)" sits last because it's
    # observational rather than position-state)
    today_cost = compute_today_cost(cost_logs)
    kpi = st.columns(6)
    n_active_signals = sum(1 for s in signals if s["action"] != "WAIT")
    n_resolved_signals = sum(
        1 for s in signals if _signal_resolution(s) is not None
    )
    n_open_trades = sum(1 for t in trades if t["status"] in ("PENDING", "OPEN"))
    kpi[0].metric("Active Markets", sum(1 for c in configs if c["enabled"]))
    kpi[1].metric("Signals (30d)", len(signals))
    kpi[2].metric("Non-WAIT", n_active_signals)
    kpi[3].metric("Resolved", n_resolved_signals)
    kpi[4].metric("Open Trades", n_open_trades)
    kpi[5].metric(
        "Today's API Cost",
        f"${today_cost['total_cost_usd']:,.2f}",
        delta=f"{today_cost['n_calls']} calls",
        delta_color="off",
    )

    st.divider()
    render_pnl_matrix(trades, signals, redis_state["prices"])

    st.divider()
    t1, t2, t3, t4 = st.tabs(
        ["📡 Signals", "🪙 Trades", "🧠 Latest Decisions", "💰 API Costs"]
    )
    with t1:
        render_signals_tab(signals)
    with t2:
        render_trades_tab(trades)
    with t3:
        render_latest_decision_tab(redis_state["decisions"], redis_state["prices"])
    with t4:
        render_costs_tab(cost_logs)


main()
