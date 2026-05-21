"""APScheduler wiring driven by the MarketConfig table + tracker maintenance jobs.

Two job families:

  1. Market-cycle jobs  — one per enabled market, fired at the cadence
     implied by its Term:
         SCALP       → every 5  minutes
         SHORT_TERM  → every 1  hour
         MID_TERM    → every 24 hours
     Each tick re-reads MarketConfig from the DB so UI edits to enabled,
     term, execution_mode, or symbols_csv take effect on the next tick
     without a process restart.

  2. Tracker maintenance jobs — three globally-scheduled jobs that run
     every 5 minutes regardless of market state:
         tracker:binance_orders     → reconcile Trade rows with Binance
                                       (entry fills, SL/TP hits, realized PnL)
         tracker:signal_resolution  → replay candles and resolve theoretical
                                       PnL for non-WAIT signals
         tracker:stale_orders       → cancel any unfilled Limit Order older
                                       than 24h (Layer 1 of staleness mgr;
                                       Layer 2 is AI-driven CANCEL_PENDING
                                       inside the orchestrator)

Job-level safety: max_instances=1, coalesce=True, misfire_grace_time=60.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from agents.orchestrator import run_market_cycle
from core.logger import get_logger
from config import settings
from core.redis_client import cache
from core.telegram_notifier import (
    fire,
    is_configured as telegram_is_configured,
    notify_cost_budget_alert,
    notify_daily_digest,
)
from data_fetchers.fred_fetcher import fetch_fred
from execution.tracker import (
    manage_stale_orders,
    sync_binance_orders,
    sync_theoretical_signals,
    update_chandelier_stops,
)
from models import (
    AsyncSessionLocal,
    MarketConfig,
    MarketType,
    Signal,
    SignalAction,
    Term,
    Trade,
    TradeStatus,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Term → tick interval (seconds) for market-cycle jobs.
# ---------------------------------------------------------------------------

TERM_INTERVAL_SECONDS: dict[Term, int] = {
    Term.SCALP: 5 * 60,
    Term.SHORT_TERM: 60 * 60,
    Term.MID_TERM: 24 * 60 * 60,
}

# Tracker jobs all run on the same 5-minute heartbeat. Cheap, and frequent
# enough that the UI's PnL numbers + active-order list stay current.
TRACKER_INTERVAL_SECONDS: int = 5 * 60

# Chandelier trailing exit runs less frequently — it touches the exchange to
# cancel + replace SL orders. 30 minutes is plenty for MID_TERM (daily candle)
# and SHORT_TERM (4h candle) trades; it'd be wasted bandwidth at 5 min.
CHANDELIER_INTERVAL_SECONDS: int = 30 * 60

# Macro snapshot refresh — independent of use_ai. The rule engine's VIX gate
# (Whaley 2000/2009) reads `macro:fred` from Redis; without this background
# refresh, rule-only mode would never see a VIX value and the gate would
# silently no-op. 15 min cadence is plenty — FRED publishes daily and we
# cache for 1h anyway.
MACRO_REFRESH_INTERVAL_SECONDS: int = 15 * 60

# LLM cost budget check — every 30 min. Fires a Telegram alert at most once
# per UTC day when the running spend crosses `settings.llm_daily_budget_usd`.
# A Redis flag `cost:alert_fired:<YYYY-MM-DD>` debounces.
COST_BUDGET_INTERVAL_SECONDS: int = 30 * 60

_MARKET_JOB_PREFIX = "market_cycle:"
_TRACKER_JOB_PREFIX = "tracker:"


def _market_job_id(market: MarketType) -> str:
    return f"{_MARKET_JOB_PREFIX}{market.value}"


# ---------------------------------------------------------------------------
# Job bodies
# ---------------------------------------------------------------------------

async def _is_system_enabled() -> bool:
    """Master switch — `config:system_enabled` Redis flag.

    Default is OFF (False). The dashboard's master toggle writes "1" or "0".
    When OFF, every market_cycle_job early-returns: no data fetch, no LLM
    calls, no orders. Tracker jobs (Binance reconciliation, signal
    resolution, stale-order TTL) are NOT gated — they still need to run to
    resolve in-flight trades even if the user paused new decision-making.
    """
    try:
        from core.redis_client import cache
        val = await cache.client.get("config:system_enabled")
        return val == "1"
    except Exception as e:
        # Fail-safe: if Redis is unreachable, stay paused rather than
        # silently spending tokens.
        log.warning("scheduler.system_flag_read_failed", err=str(e))
        return False


async def _market_cycle_job(market_value: str) -> None:
    """Re-read MarketConfig fresh on every tick so UI edits propagate."""
    # Master switch — gate every market cycle behind a single Redis flag.
    if not await _is_system_enabled():
        log.info("scheduler.system_paused", market=market_value)
        return

    try:
        market = MarketType(market_value)
    except ValueError:
        log.warning("scheduler.invalid_market", market=market_value)
        return

    async with AsyncSessionLocal() as session:
        cfg = (
            await session.execute(
                select(MarketConfig).where(MarketConfig.market == market)
            )
        ).scalar_one_or_none()

    if cfg is None:
        log.warning("scheduler.config_missing", market=market_value)
        return
    if not cfg.enabled:
        log.info("scheduler.skipped_disabled", market=market_value)
        return

    try:
        await run_market_cycle(cfg)
    except Exception as e:
        log.exception("scheduler.cycle_crashed", market=market_value, err=str(e))


# Tracker job wrappers — each catches its own exceptions so a single bad
# trade reconciliation can't take down the whole tracker.

async def _tracker_sync_binance_orders_job() -> None:
    try:
        result = await sync_binance_orders()
        log.info("scheduler.tracker.binance_done", **(result or {}))
    except Exception as e:
        log.exception("scheduler.tracker.binance_crashed", err=str(e))


async def _tracker_sync_signals_job() -> None:
    try:
        result = await sync_theoretical_signals()
        log.info("scheduler.tracker.signals_done", **(result or {}))
    except Exception as e:
        log.exception("scheduler.tracker.signals_crashed", err=str(e))


async def _tracker_stale_orders_job() -> None:
    try:
        result = await manage_stale_orders()
        log.info("scheduler.tracker.stale_done", **(result or {}))
    except Exception as e:
        log.exception("scheduler.tracker.stale_crashed", err=str(e))


async def _cost_budget_job() -> None:
    """Sum today's LLM spend; fire one Telegram alert if it crosses budget.

    Idempotency: a single Redis flag `cost:alert_fired:<YYYY-MM-DD>` (24h TTL)
    debounces. The bot keeps trading either way — this is observability, not
    an enforcement gate (enforcement would risk killing AI in the middle of
    a setup the user wants verified).
    """
    budget = float(settings.llm_daily_budget_usd or 0.0)
    if budget <= 0 or not telegram_is_configured():
        return

    from sqlalchemy import func as sqla_func, select as sqla_select

    from models import LLMCostLog

    today = datetime.now(timezone.utc).date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    flag_key = f"cost:alert_fired:{today.isoformat()}"

    try:
        already_fired = await cache.client.get(flag_key)
        if already_fired:
            return
    except Exception as e:
        log.debug("scheduler.cost_budget_flag_read_failed", err=str(e))

    async with AsyncSessionLocal() as session:
        total = (
            await session.execute(
                sqla_select(
                    sqla_func.coalesce(
                        sqla_func.sum(LLMCostLog.estimated_cost_usd), 0.0
                    )
                ).where(LLMCostLog.created_at >= day_start)
            )
        ).scalar() or 0.0

        agent_row = (
            await session.execute(
                sqla_select(
                    LLMCostLog.agent_label,
                    sqla_func.sum(LLMCostLog.estimated_cost_usd).label("c"),
                )
                .where(LLMCostLog.created_at >= day_start)
                .group_by(LLMCostLog.agent_label)
                .order_by(sqla_func.sum(LLMCostLog.estimated_cost_usd).desc())
                .limit(1)
            )
        ).first()
        top_agent = agent_row[0] if agent_row else None

    if total < budget:
        log.debug(
            "scheduler.cost_budget_ok",
            today_usd=round(total, 4), budget_usd=budget,
        )
        return

    log.warning(
        "scheduler.cost_budget_exceeded",
        today_usd=round(total, 4), budget_usd=budget, top_agent=top_agent,
    )
    fire(notify_cost_budget_alert(
        daily_usd=total, budget_usd=budget, top_agent=top_agent,
    ))
    try:
        await cache.client.set(flag_key, "1", ex=24 * 3600)
    except Exception as e:
        log.warning("scheduler.cost_budget_flag_write_failed", err=str(e))


async def _macro_refresh_job() -> None:
    """Background FRED refresh — keeps `macro:fred` warm for rule-only gates.

    The Sentiment Scanner path already refreshes this when use_ai=True, but
    rule-only markets never trigger that path. The VIX (US) and yield-curve
    gates depend on it, so we refresh independently. Cheap (one HTTP call,
    response is tiny) and gated by `settings.fred_api_key` inside fetch_fred.
    """
    try:
        await fetch_fred()
    except Exception as e:
        log.exception("scheduler.macro_refresh_crashed", err=str(e))


async def _tracker_chandelier_job() -> None:
    """Chandelier trailing-exit tightener (Chuck LeBeau).

    Lower frequency than the other tracker jobs because it actually mutates
    exchange state (cancel + place new SL). Idempotent: when nothing's worth
    tightening, the function returns quickly with zero side effects.
    """
    try:
        result = await update_chandelier_stops()
        log.info("scheduler.tracker.chandelier_done", **(result or {}))
    except Exception as e:
        log.exception("scheduler.tracker.chandelier_crashed", err=str(e))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC")


async def configure_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register all jobs: per-market cycle jobs + the three tracker jobs.

    Idempotent — safe to call repeatedly (`reload_jobs` does exactly that).
    """
    # ---- 1. Market-cycle jobs (per MarketConfig row) ------------------------
    async with AsyncSessionLocal() as session:
        configs = (await session.execute(select(MarketConfig))).scalars().all()

    expected_market_ids: set[str] = set()
    for cfg in configs:
        jid = _market_job_id(cfg.market)
        expected_market_ids.add(jid)

        if not cfg.enabled:
            if scheduler.get_job(jid):
                scheduler.remove_job(jid)
                log.info("scheduler.job_disabled", job_id=jid)
            continue

        seconds = TERM_INTERVAL_SECONDS[cfg.term]
        scheduler.add_job(
            _market_cycle_job,
            trigger=IntervalTrigger(seconds=seconds),
            args=[cfg.market.value],
            id=jid,
            name=f"{cfg.market.value} ({cfg.term.value})",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )
        log.info(
            "scheduler.job_scheduled",
            job_id=jid,
            market=cfg.market.value,
            term=cfg.term.value,
            interval_seconds=seconds,
            execution_mode=cfg.execution_mode.value,
            symbols=cfg.symbols,
        )

    # Sweep orphaned market-cycle jobs (rows that disappeared)
    for job in list(scheduler.get_jobs()):
        if (
            job.id.startswith(_MARKET_JOB_PREFIX)
            and job.id not in expected_market_ids
        ):
            scheduler.remove_job(job.id)
            log.info("scheduler.job_orphan_removed", job_id=job.id)

    # ---- 2. Tracker maintenance jobs (always-on, 5min) ----------------------
    _configure_tracker_jobs(scheduler)

    # ---- 3. Daily Telegram digest (midnight UTC) ----------------------------
    _configure_digest_job(scheduler)


def _configure_tracker_jobs(scheduler: AsyncIOScheduler) -> None:
    """Register the three tracker jobs.

    Each runs every 5 minutes. They're independent of MarketConfig — even
    if every market is disabled, we still need to reconcile in-flight trades
    and resolve historical signals.
    """
    common = {
        "trigger": IntervalTrigger(seconds=TRACKER_INTERVAL_SECONDS),
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 60,
        "replace_existing": True,
    }

    scheduler.add_job(
        _tracker_sync_binance_orders_job,
        id=f"{_TRACKER_JOB_PREFIX}binance_orders",
        name="Tracker: Binance order reconciliation (entry fills, SL/TP, PnL)",
        **common,
    )
    scheduler.add_job(
        _tracker_sync_signals_job,
        id=f"{_TRACKER_JOB_PREFIX}signal_resolution",
        name="Tracker: theoretical signal resolution (TP/SL replay)",
        **common,
    )
    scheduler.add_job(
        _tracker_stale_orders_job,
        id=f"{_TRACKER_JOB_PREFIX}stale_orders",
        name="Tracker: stale Limit Order TTL cancellation (24h)",
        **common,
    )

    # Chandelier runs on a slower cadence — exchange-mutating, MID/SHORT term only.
    scheduler.add_job(
        _tracker_chandelier_job,
        trigger=IntervalTrigger(seconds=CHANDELIER_INTERVAL_SECONDS),
        id=f"{_TRACKER_JOB_PREFIX}chandelier",
        name="Tracker: Chandelier trailing-stop tightener (LeBeau)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )

    # Macro snapshot refresh — independent of use_ai flag so rule-only gates
    # (VIX, yield curve) always have fresh data to read.
    scheduler.add_job(
        _macro_refresh_job,
        trigger=IntervalTrigger(seconds=MACRO_REFRESH_INTERVAL_SECONDS),
        id=f"{_TRACKER_JOB_PREFIX}macro_refresh",
        name="Tracker: macro snapshot (FRED — VIX/DXY/curve)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # fire once immediately
    )

    # LLM cost budget — once-per-day alert when spend crosses ceiling.
    scheduler.add_job(
        _cost_budget_job,
        trigger=IntervalTrigger(seconds=COST_BUDGET_INTERVAL_SECONDS),
        id=f"{_TRACKER_JOB_PREFIX}cost_budget",
        name="Tracker: LLM cost budget watchdog (daily ceiling)",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
    )

    log.info(
        "scheduler.tracker_jobs_scheduled",
        interval_seconds=TRACKER_INTERVAL_SECONDS,
        chandelier_interval_seconds=CHANDELIER_INTERVAL_SECONDS,
        macro_refresh_interval_seconds=MACRO_REFRESH_INTERVAL_SECONDS,
        cost_budget_interval_seconds=COST_BUDGET_INTERVAL_SECONDS,
        jobs=[
            f"{_TRACKER_JOB_PREFIX}binance_orders",
            f"{_TRACKER_JOB_PREFIX}signal_resolution",
            f"{_TRACKER_JOB_PREFIX}stale_orders",
            f"{_TRACKER_JOB_PREFIX}chandelier",
            f"{_TRACKER_JOB_PREFIX}macro_refresh",
            f"{_TRACKER_JOB_PREFIX}cost_budget",
        ],
    )


async def reload_jobs(scheduler: AsyncIOScheduler) -> None:
    """Hook for the dashboard / admin endpoints after MarketConfig edits."""
    await configure_jobs(scheduler)
    log.info("scheduler.reloaded", n_jobs=len(scheduler.get_jobs()))


# ---------------------------------------------------------------------------
# Daily digest job (midnight UTC) — Telegram summary of the day that ended
# ---------------------------------------------------------------------------

_DIGEST_JOB_ID: str = "telegram:daily_digest"


async def send_daily_digest() -> None:
    """Compute the previous 24h performance and ship a Telegram summary.

    Window: yesterday's full LOCAL day (Europe/Istanbul). The cron trigger
    fires at 00:00 Istanbul; we report on the calendar day that just ended
    in TR time. Postgres timestamp columns are timezone-aware, so the
    UTC-converted window endpoints filter correctly server-side.

    Realized PnL = closed Crypto AUTO_BOT trades whose `closed_at` falls in
    the window. Theoretical PnL = signals whose `raw_payload["resolution"]`
    was written with `resolved_at` in the window.

    Silently no-ops when Telegram isn't configured.
    """
    if not telegram_is_configured():
        log.debug("scheduler.digest.skipped", reason="telegram_not_configured")
        return

    # Build the window in Istanbul time, then convert endpoints to UTC for
    # the DB query. ZoneInfo is stdlib (Python 3.9+) — no extra dep.
    from zoneinfo import ZoneInfo
    TR = ZoneInfo("Europe/Istanbul")

    now_tr = datetime.now(TR)
    end_tr = datetime(now_tr.year, now_tr.month, now_tr.day, tzinfo=TR)
    start_tr = end_tr - timedelta(days=1)
    # Convert to UTC for the timezone-aware comparison in Postgres
    start = start_tr.astimezone(timezone.utc)
    end = end_tr.astimezone(timezone.utc)

    # ---- Realized Crypto PnL ----------------------------------------------
    async with AsyncSessionLocal() as session:
        trades = (
            await session.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.CLOSED,
                    Trade.closed_at >= start,
                    Trade.closed_at < end,
                )
            )
        ).scalars().all()

    crypto_pnl = sum((t.realized_pnl_usd or 0.0) for t in trades)
    crypto_wins = sum(1 for t in trades if (t.realized_pnl_usd or 0) > 0)
    crypto_losses = len(trades) - crypto_wins

    # ---- Theoretical signal PnL (resolutions inside the window) -----------
    cohort_floor = start - timedelta(days=30)
    async with AsyncSessionLocal() as session:
        signals = (
            await session.execute(
                select(Signal).where(
                    Signal.created_at >= cohort_floor,
                    Signal.action != SignalAction.WAIT,
                )
            )
        ).scalars().all()

    summary: dict[str, dict[str, Any]] = {}
    for s in signals:
        res = (s.raw_payload or {}).get("resolution")
        if not res:
            continue
        resolved_iso = res.get("resolved_at")
        if not resolved_iso:
            continue
        try:
            resolved_dt = datetime.fromisoformat(
                str(resolved_iso).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if not (start <= resolved_dt < end):
            continue

        bucket = summary.setdefault(
            s.market.value,
            {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        )
        bucket["n"] += 1
        bucket["pnl"] += float(res.get("theoretical_pnl_usd") or 0.0)
        if res.get("outcome") == "TP":
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1

    date_str = start_tr.strftime("%Y-%m-%d")  # TR-local date label

    # Quiet-mode: if literally nothing happened, skip the Telegram message.
    # "0 trades / 0 signals" notifications are pure noise; the user already
    # checks the dashboard when they want a status read. Still log the
    # decision so we know the job ran (and can prove it on review days).
    if len(trades) == 0 and not summary:
        log.info(
            "scheduler.digest.skipped_empty",
            date=date_str, tz="Europe/Istanbul",
        )
        return

    await notify_daily_digest(
        date_str=date_str,
        crypto_pnl=crypto_pnl,
        crypto_wins=crypto_wins,
        crypto_losses=crypto_losses,
        crypto_n_trades=len(trades),
        signal_summary=summary,
    )

    log.info(
        "scheduler.digest.sent",
        date=date_str,
        tz="Europe/Istanbul",
        crypto_trades=len(trades),
        crypto_pnl=round(crypto_pnl, 2),
        n_markets_with_signals=len(summary),
    )


async def _daily_digest_job_wrapper() -> None:
    """Outer shield so a digest crash never propagates into APScheduler."""
    try:
        await send_daily_digest()
    except Exception as e:
        log.exception("scheduler.digest.crashed", err=str(e))


def _configure_digest_job(scheduler: AsyncIOScheduler) -> None:
    """Register the midnight digest job (idempotent via replace_existing).

    Fires at LOCAL midnight (Europe/Istanbul, UTC+3) — matches the user's
    calendar day. The digest's `start` window is still UTC-based internally
    (the Trade/Signal rows are timezone-aware), but the trigger and the
    `date_str` header read like a natural Turkish "günsonu" report.
    """
    scheduler.add_job(
        _daily_digest_job_wrapper,
        trigger=CronTrigger(hour=0, minute=0, timezone="Europe/Istanbul"),
        id=_DIGEST_JOB_ID,
        name="Telegram: midnight daily digest (Europe/Istanbul)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # 10-min grace so a slightly-late tick still fires (instead of skipping
        # the whole day) but doesn't spam if the process was offline for hours.
        misfire_grace_time=600,
    )
    log.info("scheduler.digest_job_scheduled", job_id=_DIGEST_JOB_ID, tz="Europe/Istanbul")


__all__ = [
    "build_scheduler",
    "configure_jobs",
    "reload_jobs",
    "send_daily_digest",
    "TERM_INTERVAL_SECONDS",
    "TRACKER_INTERVAL_SECONDS",
    "CHANDELIER_INTERVAL_SECONDS",
    "MACRO_REFRESH_INTERVAL_SECONDS",
    "COST_BUDGET_INTERVAL_SECONDS",
]
