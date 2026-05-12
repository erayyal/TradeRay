"""TradeRay — Global Financial Terminal entrypoint.

Boots the async stack in one process:
  1. ORM schema (creates tables, seeds default MarketConfig rows)
  2. Redis cache (for the dashboard + per-tick state)
  3. APScheduler (one job per enabled market, derived from MarketConfig)
  4. A "kickstart" cycle so the UI has data before the first tick fires
  5. Blocks on SIGINT / SIGTERM and shuts the world down cleanly

Usage:
    # Terminal 1 — backend (data + agents + execution)
    python main.py

    # Terminal 2 — dashboard
    streamlit run ui/dashboard.py

Required env (see .env.example):
    ANTHROPIC_API_KEY      (Opus 4.7 vision support → ANTHROPIC_MODEL=claude-opus-4-7)
    BINANCE_API_KEY / BINANCE_API_SECRET   (testnet by default)
    DATABASE_URL           (default: sqlite+aiosqlite:///traderay.db)
    REDIS_URL              (default: redis://localhost:6379/0)
"""
from __future__ import annotations

import asyncio
import signal
from contextlib import suppress

from sqlalchemy import select

from config import settings
from core.logger import configure_logging, get_logger
from core.redis_client import cache
from data_fetchers.market_fetcher import fetcher
from models import (
    AsyncSessionLocal,
    MarketConfig,
    MarketType,
    init_db,
    seed_default_market_config,
)
from scheduler.jobs import build_scheduler, configure_jobs

configure_logging()
log = get_logger("traderay.main")


# ---------------------------------------------------------------------------
# Default symbol roster (only used on the very first boot — afterwards the
# UI / DB owns this state). Edit MarketConfig.symbols_csv at runtime to
# change the watchlist without restarting the process.
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS: dict[MarketType, list[str]] = {
    MarketType.CRYPTO: ["BTCUSDT", "ETHUSDT"],
    MarketType.BIST:   ["THYAO.IS", "ASELS.IS", "GARAN.IS"],
    MarketType.SP500:  ["AAPL", "MSFT", "^GSPC"],
    MarketType.NASDAQ: ["NVDA", "TSLA", "^IXIC"],
}


# ---------------------------------------------------------------------------
# Boot-time helpers
# ---------------------------------------------------------------------------

async def _kickstart() -> None:
    """Run one cycle per enabled market RIGHT NOW so the dashboard isn't
    empty during the first scheduler interval (which can be up to 24h on
    MID_TERM markets).
    """
    # Imported here to break a circular import: orchestrator imports
    # market_fetcher / engine, which the scheduler also imports.
    from agents.orchestrator import run_market_cycle

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(MarketConfig).where(MarketConfig.enabled.is_(True))
        )
        configs = result.scalars().all()

    log.info("kickstart.start", n_markets=len(configs))
    for cfg in configs:
        try:
            await run_market_cycle(cfg)
        except Exception as e:
            log.exception(
                "kickstart.market_failed", market=cfg.market.value, err=str(e)
            )
    log.info("kickstart.done")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info(
        "traderay.starting",
        model=settings.anthropic_model,
        binance_testnet=settings.binance_testnet,
        markets=[m.value for m in MarketType],
    )

    # 1. ORM schema + default config seed (idempotent)
    await init_db()
    await seed_default_market_config(DEFAULT_SYMBOLS)
    log.info("traderay.db_ready")

    # 2. Redis (UI live state + per-tick scratchpad)
    await cache.connect()

    # 3. Scheduler — read DB, register one job per enabled market
    scheduler = build_scheduler()
    await configure_jobs(scheduler)
    scheduler.start()
    log.info(
        "traderay.scheduler_started", n_jobs=len(scheduler.get_jobs())
    )

    # 4. Kickstart in the background — don't block the scheduler behind it.
    #    A long kickstart shouldn't delay the first SCALP tick.
    asyncio.create_task(_kickstart())

    # 5. Wait for SIGINT / SIGTERM
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):  # Windows: SIGTERM not handled
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("traderay.shutting_down")
        scheduler.shutdown(wait=False)
        await fetcher.close()
        await cache.close()
        log.info("traderay.stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
