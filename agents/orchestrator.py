"""TradeRay agent pipeline orchestrator — final wiring.

Wires the data fetchers, the three Claude agents, the chart-vision module,
the execution engine, and the PnL tracker into one coherent cycle.

Key wiring fixed in this revision:
  - Master Trader prompt is built dynamically per market via
    `build_master_trader_prompt(market)` so the right strategy rulebook is
    injected on every call (still cached per-market for prompt-cache hits).
  - The dynamic screener flag (Redis: `config:{market}:dynamic_screener`)
    overrides the configured symbol list when set.
  - Indicator computation receives the correct `lookbacks` dict per
    interval, so SCALP's 5m gets RSI(9)/MACD(8,21,5) and longer timeframes
    get RSI(14)/MACD(12,26,9).
  - Before calling the Master Trader we look up any UNFILLED Limit Order
    for the symbol and embed it in the user payload as `pending_order`.
  - If the Master Trader returns `decision="CANCEL_PENDING"`, we route to
    the tracker's cancel path instead of the execution engine.

Flow per `run_market_cycle(market_config)`:
  0. Resolve the active symbol list (dynamic screener vs configured list).
  1. Refresh shared macro context (CryptoPanic + FRED + DefiLlama) — once.
  2. For each symbol:
     a. fetch_term() pulls every interval the active Term needs.
     b. Quant Analyst + Sentiment Scanner run concurrently.
     c. Render the candle chart on the primary interval (off-thread).
     d. Look up any pending Limit Order for this symbol.
     e. Master Trader receives JSON + Base64 image + pending_order and emits
        a decision.
     f. Decision routing:
          - CANCEL_PENDING → tracker.cancel_pending_for_symbol()
          - LONG / SHORT / WAIT → execution.engine.route()
  3. Stamp `MarketConfig.last_run_at`.

Every step is wrapped in `_safe()` — a failure in one symbol does NOT abort
the cycle, and a failure in one cycle does NOT abort the scheduler.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, TypeVar

from sqlalchemy import update

from agents.llm_client import LLMResponseError, call_agent
from agents.prompts import (
    QUANT_SYSTEM_PROMPT,
    SENTIMENT_SYSTEM_PROMPT,
    build_master_trader_prompt,
)
from config import settings
from core.logger import get_logger
from core.redis_client import cache
from data_fetchers.defillama_fetcher import fetch_defillama
from data_fetchers.fred_fetcher import fetch_fred
from data_fetchers.market_fetcher import fetcher, lookbacks_for
from data_fetchers.news_fetcher import fetch_latest_news
from data_fetchers.technicals import compute_indicators
from execution.engine import engine
from execution.tracker import (
    cancel_pending_for_symbol,
    get_pending_trade_for_symbol,
)
from models import (
    AsyncSessionLocal,
    ExecutionMode,
    LLMCostLog,
    MarketConfig,
    MarketType,
    Term,
)
from vision_utils import build_vision_message, render_chart_base64

log = get_logger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Term → primary interval. Drives:
#   - the candle chart rendered for the Master Trader's vision input
#   - the Quant Analyst's `primary_interval` hint
# ---------------------------------------------------------------------------

PRIMARY_INTERVAL: dict[Term, str] = {
    Term.SCALP: "15m",
    Term.SHORT_TERM: "4h",
    Term.MID_TERM: "1d",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe(label: str, coro: Awaitable[T]) -> T | None:
    """Await `coro` and convert any exception into a logged None."""
    try:
        return await coro
    except Exception as e:
        log.exception("orchestrator.step_failed", step=label, err=str(e))
        return None


def _normalize_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Bridge new prompt-schema names to the executor's legacy keys."""
    if "entry" not in decision and decision.get("entry_price") is not None:
        decision["entry"] = decision["entry_price"]
    if "confidence" not in decision and decision.get("confidence_level") is not None:
        decision["confidence"] = decision["confidence_level"]
    return decision


async def _read_screener_flag(market: MarketType) -> bool:
    """Read the dynamic-screener toggle from Redis (set by the dashboard)."""
    try:
        val = await cache.client.get(f"config:{market.value}:dynamic_screener")
        return val == "1"
    except Exception as e:
        log.warning("orchestrator.screener_flag_read_failed", err=str(e))
        return False


# ---------------------------------------------------------------------------
# LLM cost tracking
#
# Pricing is per-model, in USD per 1,000,000 tokens. Source: Anthropic public
# pricing page (cached). When swapping models via ANTHROPIC_MODEL, update
# the entry here too — `_DEFAULT_PRICING` is the conservative upper bound we
# fall back to for unknown model IDs so cost reports never under-estimate.
# ---------------------------------------------------------------------------

LLM_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0},
    "claude-opus-4-6":   {"input":  5.0, "output": 25.0},
    "claude-sonnet-4-6": {"input":  3.0, "output": 15.0},
    "claude-haiku-4-5":  {"input":  1.0, "output":  5.0},
}
_DEFAULT_PRICING: dict[str, float] = LLM_PRICING["claude-opus-4-7"]


def _compute_llm_cost_usd(
    input_tokens: int, output_tokens: int, model: str
) -> float:
    """Compute the USD cost of an Anthropic call from per-million pricing."""
    p = LLM_PRICING.get(model, _DEFAULT_PRICING)
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000.0


async def _log_llm_cost(
    *,
    market: MarketType | None,
    symbol: str | None,
    agent_label: str,
    usage: dict[str, Any],
) -> None:
    """Persist a single LLM call's token usage + estimated cost.

    Best-effort: a failure to write the cost row MUST NOT break the trading
    cycle. The DB write is awaited (not fire-and-forget) so the row is
    durable before we move on — typical write time is <10ms with SQLite + WAL.
    """
    if not usage:
        return

    input_t = int(usage.get("input_tokens", 0) or 0)
    output_t = int(usage.get("output_tokens", 0) or 0)
    model = str(usage.get("model") or settings.anthropic_model)
    cost = _compute_llm_cost_usd(input_t, output_t, model)

    try:
        async with AsyncSessionLocal() as session:
            session.add(
                LLMCostLog(
                    market=market,
                    symbol=symbol,
                    agent_label=agent_label,
                    model=model,
                    input_tokens=input_t,
                    output_tokens=output_t,
                    estimated_cost_usd=cost,
                )
            )
            await session.commit()
    except Exception as e:
        # Cost logging is observational — log and move on.
        log.warning(
            "orchestrator.cost_log_failed",
            agent=agent_label, symbol=symbol, err=str(e),
        )


# ---------------------------------------------------------------------------
# Macro context (shared across all symbols within a cycle)
# ---------------------------------------------------------------------------

async def _refresh_macro_context() -> dict[str, Any]:
    """Pull RSS news + FRED macro + DefiLlama on-chain in parallel.

    The news payload key is kept as `cryptopanic` for backward compatibility
    with the Sentiment Scanner prompt's <context> section — only the data
    SOURCE has pivoted (CryptoPanic's paid API → free RSS aggregator);
    the consumer shape is identical so the LLM sees no difference.
    """
    news_task = asyncio.create_task(
        _safe("rss_news", fetch_latest_news(limit=30))
    )
    fred_task = asyncio.create_task(_safe("fred", fetch_fred()))
    dl_task = asyncio.create_task(_safe("defillama", fetch_defillama()))
    news, fred_data, onchain = await asyncio.gather(news_task, fred_task, dl_task)
    return {"cryptopanic": news, "macro": fred_data, "onchain": onchain}


# ---------------------------------------------------------------------------
# Per-agent calls
# ---------------------------------------------------------------------------

async def _run_quant(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    ohlcv_by_interval: dict[str, list[dict]],
) -> dict[str, Any] | None:
    """Compute indicators with timeframe-aware lookbacks, then call the Quant Analyst.

    Each interval gets its own `lookbacks` dict from `lookbacks_for(iv)` —
    the SCALP 5m gets RSI(9)/MACD(8,21,5); 15m+ get RSI(14)/MACD(12,26,9).
    """
    indicators = {
        iv: compute_indicators(c, lookbacks=lookbacks_for(iv))
        for iv, c in ohlcv_by_interval.items()
        if c
    }
    if not indicators:
        log.warning("orchestrator.quant.no_indicators", symbol=symbol)
        return None

    primary_iv = PRIMARY_INTERVAL[term]
    primary_candles = ohlcv_by_interval.get(primary_iv) or []
    last_close = primary_candles[-1]["close"] if primary_candles else None

    payload = {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "primary_interval": primary_iv,
        "intervals": list(indicators.keys()),
        "indicators": indicators,
        "last_close": last_close,
    }

    parsed, usage = await call_agent(
        system_prompt=QUANT_SYSTEM_PROMPT,
        user_content=json.dumps(payload, default=str),
        max_tokens=2500,
        label=f"quant:{symbol}",
    )
    await _log_llm_cost(
        market=market, symbol=symbol, agent_label="quant", usage=usage,
    )
    return parsed


async def _run_sentiment(
    macro_context: dict[str, Any],
    *,
    market: MarketType,
    symbol: str,
) -> dict[str, Any] | None:
    """Sentiment Scanner — attributed to the symbol that triggered the call so
    LLM cost reports break down correctly per market/symbol (the scanner reads
    global macro data but we still pay per-symbol invocation costs)."""
    parsed, usage = await call_agent(
        system_prompt=SENTIMENT_SYSTEM_PROMPT,
        user_content=json.dumps(macro_context, default=str),
        max_tokens=2000,
        label=f"sentiment:{symbol}",
    )
    await _log_llm_cost(
        market=market, symbol=symbol, agent_label="sentiment", usage=usage,
    )
    return parsed


async def _fetch_microstructure(
    symbol: str, market: MarketType
) -> dict[str, Any]:
    """Fetch market-specific structural data the rulebooks depend on.

    Crypto : funding rate (8h cycle, contrarian at extremes per
             crypto rulebook §1) + open interest (base + USD).
    BIST   : USDTRY=X daily rate (BIST rulebook §2 — the entire TL macro
             overlay hinges on this; without it the LLM hallucinates).
    Other  : empty — macro feed already covers the relevant US macro vars.

    All sub-fetches are isolated by `_safe()`; a single failure (e.g. a
    listed-but-no-funding-history symbol) returns null for that key only.
    """
    out: dict[str, Any] = {}
    if market == MarketType.CRYPTO:
        funding = await _safe(
            f"funding:{symbol}", fetcher.fetch_funding_rate(symbol)
        )
        oi = await _safe(
            f"open_interest:{symbol}", fetcher.fetch_open_interest(symbol)
        )
        out["funding_rate"] = funding
        out["open_interest"] = oi
    elif market == MarketType.BIST:
        usdtry = await _safe("usdtry", fetcher.fetch_usdtry())
        out["usdtry"] = usdtry
    return out


async def _run_master_trader(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    primary_interval: str,
    quant: dict[str, Any],
    sentiment: dict[str, Any],
    chart_b64: str | None,
    execution_mode: ExecutionMode,
    pending_order: dict[str, Any] | None,
    microstructure: dict[str, Any],
) -> dict[str, Any] | None:
    """Compose the multi-modal payload (image + JSON, with optional pending
    order context + market-specific microstructure data) and call the brain.

    System prompt is built per-market via `build_master_trader_prompt(market)`
    so the correct strategy rulebook is injected. The function is cached
    (lru_cache 8) so the prompt-cache stays warm.
    """
    payload = {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "primary_interval": primary_interval,
        "execution_mode": execution_mode.value,
        "quant": quant,
        "sentiment": sentiment,
        "microstructure": microstructure,  # funding/OI for crypto, USDTRY for BIST
        "pending_order": pending_order,    # null when no order resting
        "risk_envelope": {
            "portfolio_notional_usd": settings.portfolio_notional,
            "max_risk_pct": settings.max_risk_pct,
            "max_risk_usd": settings.portfolio_notional * settings.max_risk_pct,
            "max_leverage": settings.default_leverage,
            "quote_asset": settings.quote_asset,
        },
    }

    user_content = build_vision_message(
        json_text=json.dumps(payload, default=str, indent=2),
        image_base64=chart_b64,
        image_caption=f"{symbol} {primary_interval} candles + EMA(20,50)",
    )

    parsed, usage = await call_agent(
        system_prompt=build_master_trader_prompt(market),
        user_content=user_content,
        max_tokens=3000,
        label=f"master:{symbol}",
    )
    await _log_llm_cost(
        market=market, symbol=symbol, agent_label="master", usage=usage,
    )
    return parsed


# ---------------------------------------------------------------------------
# Per-symbol cycle
# ---------------------------------------------------------------------------

async def run_symbol_cycle(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    execution_mode: ExecutionMode,
    macro_context: dict[str, Any],
) -> dict[str, Any] | None:
    """End-to-end pipeline for one symbol."""
    primary_iv = PRIMARY_INTERVAL[term]

    # 1. OHLCV bundle
    ohlcv = await _safe(
        f"fetch_term:{symbol}",
        fetcher.fetch_term(symbol, market, term),
    )
    if not ohlcv or not any(ohlcv.values()):
        log.warning(
            "orchestrator.no_ohlcv", symbol=symbol, market=market.value, term=term.value,
        )
        return None

    # 2. Quant + Sentiment in parallel
    quant_task = asyncio.create_task(
        _safe(f"quant:{symbol}", _run_quant(
            symbol=symbol, market=market, term=term, ohlcv_by_interval=ohlcv,
        ))
    )
    sent_task = asyncio.create_task(
        _safe(
            f"sentiment:{symbol}",
            _run_sentiment(macro_context, market=market, symbol=symbol),
        )
    )
    quant, sentiment = await asyncio.gather(quant_task, sent_task)

    if quant is None or sentiment is None:
        log.warning(
            "orchestrator.agent_missing",
            symbol=symbol, quant_ok=quant is not None, sentiment_ok=sentiment is not None,
        )
        return None

    # 3. Render the chart on the primary interval (off-thread; matplotlib blocks).
    primary_candles = ohlcv.get(primary_iv) or []
    chart_b64 = await asyncio.to_thread(
        render_chart_base64, primary_candles, symbol=symbol, interval=primary_iv,
    )

    # 4. Look up any UNFILLED limit order resting for this symbol.
    pending_order = await _safe(
        f"pending_lookup:{symbol}",
        get_pending_trade_for_symbol(symbol),
    )

    # 4b. Market-specific microstructure (closes the data blindspot the
    # rulebooks reference: funding/OI for crypto, USDTRY for BIST).
    microstructure = await _fetch_microstructure(symbol, market)

    # 5. Master Trader fuses everything (with pending order + microstructure).
    decision = await _safe(
        f"master:{symbol}",
        _run_master_trader(
            symbol=symbol,
            market=market,
            term=term,
            primary_interval=primary_iv,
            quant=quant,
            sentiment=sentiment,
            chart_b64=chart_b64,
            execution_mode=execution_mode,
            pending_order=pending_order,
            microstructure=microstructure,
        ),
    )
    if decision is None:
        return None
    decision = _normalize_decision(decision)

    # 6. Cache the full bundle for the UI (regardless of branch below)
    bundle = {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "execution_mode": execution_mode.value,
        "quant": quant,
        "sentiment": sentiment,
        "microstructure": microstructure,
        "pending_order": pending_order,
        "decision": decision,
        "produced_at": datetime.now(timezone.utc).isoformat(),
    }
    await cache.set_json(f"decision:{symbol}:latest", bundle, ttl=3600)

    action = decision.get("decision")

    # 7a. CANCEL_PENDING — Layer 2 of the staleness manager (AI-driven).
    if action == "CANCEL_PENDING":
        if not pending_order:
            log.warning(
                "orchestrator.cancel_pending_invalid_no_order", symbol=symbol,
            )
            # Treat as WAIT so we don't surprise downstream consumers
            return {
                "signal_id": None,
                "trade_id": None,
                "executed": False,
                "effective_mode": execution_mode,
                "reason": "cancel_pending_invalid",
            }
        canceled_n = await _safe(
            f"cancel_pending:{symbol}",
            cancel_pending_for_symbol(symbol, reason="ai_invalidated_thesis"),
        ) or 0
        log.info(
            "orchestrator.cancel_pending_done",
            symbol=symbol, trade_id=pending_order["trade_id"], canceled=canceled_n,
            justification=(decision.get("justification") or "")[:200],
        )
        return {
            "signal_id": None,
            "trade_id": pending_order["trade_id"],
            "executed": False,
            "effective_mode": execution_mode,
            "reason": "ai_canceled_pending",
            "canceled_count": canceled_n,
        }

    # 7b. Standard path — route LONG/SHORT/WAIT through the engine.
    result = await _safe(
        f"engine.route:{symbol}",
        engine.route(
            market=market,
            term=term,
            symbol=symbol,
            decision=decision,
            mode=execution_mode,
            quant_score=quant.get("quant_score"),
            sentiment_score=sentiment.get("sentiment_score"),
            fear_greed_index=sentiment.get("fear_greed_index"),
            macro_regime=sentiment.get("macro_regime"),
        ),
    )

    log.info(
        "orchestrator.symbol_done",
        symbol=symbol, market=market.value, decision=action,
        executed=bool(result and result.get("executed")),
        signal_id=(result or {}).get("signal_id"),
        had_pending_order=bool(pending_order),
    )
    return result


# ---------------------------------------------------------------------------
# Per-market cycle
# ---------------------------------------------------------------------------

async def _resolve_symbols(market_config: MarketConfig) -> list[str]:
    """Pick the universe of symbols for this cycle.

    If the dynamic screener is ON in Redis, ask the screener for the top-N
    "fırsat avcılığı" picks (top crypto by 24h volume / top equities by
    abs daily move). Otherwise use the configured `symbols_csv` list.
    """
    static_symbols = market_config.symbols
    screener_on = await _read_screener_flag(market_config.market)
    if not screener_on:
        return static_symbols

    picks = await _safe(
        f"screener:{market_config.market.value}",
        fetcher.get_dynamic_symbols(market_config.market, limit=5),
    )
    if not picks:
        log.warning(
            "orchestrator.screener_empty_fallback",
            market=market_config.market.value,
        )
        return static_symbols

    log.info(
        "orchestrator.screener_active",
        market=market_config.market.value,
        picks=picks,
        replaced_static=static_symbols,
    )
    return picks


async def run_market_cycle(market_config: MarketConfig) -> None:
    """Run a full cycle for one market, iterating its configured symbols."""
    if not market_config.enabled:
        log.info("orchestrator.market_disabled", market=market_config.market.value)
        return

    symbols = await _resolve_symbols(market_config)
    if not symbols:
        log.warning("orchestrator.no_symbols", market=market_config.market.value)
        return

    log.info(
        "orchestrator.cycle.start",
        market=market_config.market.value,
        term=market_config.term.value,
        execution_mode=market_config.execution_mode.value,
        symbols=symbols,
    )

    # Macro context: fetched ONCE, reused across all symbols this cycle.
    macro_context = await _refresh_macro_context()

    n_executed = 0
    n_signaled = 0
    n_canceled = 0
    for sym in symbols:
        try:
            result = await run_symbol_cycle(
                symbol=sym,
                market=market_config.market,
                term=market_config.term,
                execution_mode=market_config.execution_mode,
                macro_context=macro_context,
            )
            if result:
                n_signaled += 1
                if result.get("executed"):
                    n_executed += 1
                if result.get("reason") == "ai_canceled_pending":
                    n_canceled += 1
        except Exception as e:
            log.exception("orchestrator.symbol_failed", symbol=sym, err=str(e))

    # Stamp last_run_at
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(MarketConfig)
                .where(MarketConfig.market == market_config.market)
                .values(last_run_at=datetime.now(timezone.utc))
            )
            await session.commit()
    except Exception as e:
        log.warning("orchestrator.last_run_update_failed", err=str(e))

    log.info(
        "orchestrator.cycle.done",
        market=market_config.market.value,
        n_symbols=len(symbols),
        n_signaled=n_signaled,
        n_executed=n_executed,
        n_canceled_pending=n_canceled,
    )


__all__ = ["run_market_cycle", "run_symbol_cycle", "PRIMARY_INTERVAL"]
