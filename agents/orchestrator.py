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
from agents.rule_engine import generate_rule_decision
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
    AuditCategory,
    AuditMode,
    AuditOutcome,
    ExecutionMode,
    LLMCostLog,
    MarketConfig,
    MarketType,
    Term,
    log_decision_audit,
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
        max_tokens=3500,
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


# Token-economy gate: only render + send the chart when the rule-engine's
# conviction is high enough that vision-confirmation is worth ~3K extra
# input tokens. Sub-threshold setups get text-only Master Trader calls.
_VISION_CONFIDENCE_THRESHOLD: int = 70


# ---------------------------------------------------------------------------
# Audit trail — full transparency log of every cycle's reasoning
# ---------------------------------------------------------------------------

def _extract_indicator_snapshot(
    indicators: dict[str, dict], primary_iv: str
) -> dict[str, Any]:
    """Pick the headline numbers from the indicator bundle for the audit row."""
    primary = indicators.get(primary_iv) or {}
    return {
        "primary_interval": primary_iv,
        "intervals_used": list(indicators.keys()),
        "rsi": primary.get("rsi"),
        "macd": primary.get("macd"),
        "macd_hist": primary.get("macd_hist"),
        "macd_signal": primary.get("macd_signal"),
        "ema_fast": primary.get("ema_fast"),
        "ema_slow": primary.get("ema_slow"),
        "atr": primary.get("atr"),
        "atr_pct": primary.get("atr_pct"),
        "last_close": primary.get("last_close"),
        "above_ema_slow": primary.get("above_ema_slow"),
        "bb_position": primary.get("bb_position"),
    }


def _classify_audit(
    *,
    decision: dict[str, Any],
    result: dict[str, Any] | None,
    use_ai: bool,
    execution_mode: ExecutionMode,
) -> tuple[AuditCategory, AuditMode, AuditOutcome, str]:
    """Map the run_symbol_cycle output to a clean audit record."""
    mode_enum = AuditMode.AI_ENABLED if use_ai else AuditMode.RULE_BASED_ONLY

    # Category — BOT iff a real exchange order actually went out.
    # Failed attempts (rejected/error) on the AUTO_BOT path stay as SIGNAL.
    if result and result.get("executed"):
        category = AuditCategory.BOT
    elif execution_mode == ExecutionMode.AUTO_BOT and result and result.get("executed"):
        category = AuditCategory.BOT
    else:
        category = AuditCategory.SIGNAL

    action = (decision or {}).get("decision", "WAIT")

    if result is None:
        return category, mode_enum, AuditOutcome.ERROR, "no result from cycle (early exit)"

    if result.get("executed"):
        return (
            AuditCategory.BOT, mode_enum, AuditOutcome.EXECUTED,
            f"Order placed: trade_id={result.get('trade_id')}",
        )

    reason = (result.get("reason") or "").lower()
    just = (decision or {}).get("justification") or ""

    if action == "WAIT" or reason == "decision_wait":
        return category, mode_enum, AuditOutcome.WAITED, just[:240] or "no setup"

    if "signal_only" in reason:
        return category, mode_enum, AuditOutcome.SIGNAL_SENT, just[:240] or "signal logged"

    if "rejected" in reason or "rejected_missing_tp_sl" in reason or "risk_rejected" in reason:
        return category, mode_enum, AuditOutcome.REJECTED, result.get("reason") or "rejected"

    if "ai_canceled_pending" in reason:
        return category, mode_enum, AuditOutcome.REJECTED, "AI invalidated pending order"

    if "error" in reason or "executor_returned_none" in reason:
        return category, mode_enum, AuditOutcome.ERROR, result.get("reason") or "error"

    # Defensive default — unknown route() outcome
    return category, mode_enum, AuditOutcome.WAITED, result.get("reason") or "unknown"


async def _persist_audit(
    *,
    market: MarketType,
    symbol: str,
    use_ai: bool,
    execution_mode: ExecutionMode,
    trace: dict[str, Any],
    decision: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> None:
    """Build the audit row from the in-flight trace + final result, then write it."""
    category, mode_enum, outcome, reason = _classify_audit(
        decision=decision or {},
        result=result,
        use_ai=use_ai,
        execution_mode=execution_mode,
    )
    await log_decision_audit(
        category=category,
        market=market,
        symbol=symbol,
        mode=mode_enum,
        outcome=outcome,
        logic_trace=trace,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Per-symbol cycle
# ---------------------------------------------------------------------------

async def run_symbol_cycle(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    execution_mode: ExecutionMode,
    use_ai: bool,
    macro_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Dual-core symbol cycle: rule engine first, LLM only when there's a setup
    AND the user has explicitly opted in to AI verification.

    Flow:
      1. Fetch OHLCV (free).
      2. Compute indicators (free).
      3. Rule engine → LONG / SHORT / WAIT (free, deterministic).
      4. If WAIT → route as-is, zero LLM cost, zero tokens.
      5. If setup found AND use_ai=False → route the rule decision as-is.
      6. If setup found AND use_ai=True → call Quant + Sentiment + Master to
         verify/refine. Chart is rendered only when the rule engine's
         confidence ≥ 70 (token-economy gate).

    Every exit path writes ONE DecisionAudit row capturing the full trace.
    """
    primary_iv = PRIMARY_INTERVAL[term]

    # Audit trace accumulator — every branch populates what's relevant.
    trace: dict[str, Any] = {
        "indicators": {},
        "rule_engine": {},
        "ai_analysis": None,
        "validation": {},
        "execution": {},
    }

    # 1. OHLCV bundle
    ohlcv = await _safe(
        f"fetch_term:{symbol}",
        fetcher.fetch_term(symbol, market, term),
    )
    if not ohlcv or not any(ohlcv.values()):
        log.warning(
            "orchestrator.no_ohlcv", symbol=symbol, market=market.value, term=term.value,
        )
        trace["execution"]["early_exit"] = "no_ohlcv"
        await _persist_audit(
            market=market, symbol=symbol, use_ai=use_ai,
            execution_mode=execution_mode, trace=trace,
            decision=None,
            result={"executed": False, "reason": "error_no_ohlcv"},
        )
        return None

    # 2. Compute indicators per interval with the right lookbacks
    indicators = {
        iv: compute_indicators(c, lookbacks=lookbacks_for(iv))
        for iv, c in ohlcv.items()
        if c
    }
    if not indicators:
        log.warning("orchestrator.no_indicators", symbol=symbol)
        trace["execution"]["early_exit"] = "no_indicators"
        await _persist_audit(
            market=market, symbol=symbol, use_ai=use_ai,
            execution_mode=execution_mode, trace=trace,
            decision=None,
            result={"executed": False, "reason": "error_no_indicators"},
        )
        return None

    # Snapshot indicators for the audit row
    trace["indicators"] = _extract_indicator_snapshot(indicators, primary_iv)

    # 3. Pending-order lookup (need it for cache + master prompt)
    pending_order = await _safe(
        f"pending_lookup:{symbol}",
        get_pending_trade_for_symbol(symbol),
    )

    # 4. Rule engine — deterministic, free
    rule_decision = generate_rule_decision(
        symbol=symbol, market=market, term=term,
        primary_interval=primary_iv, indicators=indicators,
    )
    trace["rule_engine"] = {
        "decision": rule_decision["decision"],
        "confidence": rule_decision["confidence_level"],
        "justification": rule_decision["justification"],
        "entry": rule_decision.get("entry_price"),
        "tp": rule_decision.get("take_profit"),
        "sl": rule_decision.get("stop_loss"),
        "rr": rule_decision.get("reward_risk_ratio"),
    }

    # Track the path we took for cost-attribution and dashboard observability
    decision_source = "rule_engine"
    quant: dict[str, Any] | None = None
    sentiment: dict[str, Any] | None = None
    microstructure: dict[str, Any] = {}
    chart_b64: str | None = None

    if rule_decision["decision"] == "WAIT":
        # No setup — short-circuit. ZERO LLM cost in this branch (dominant case).
        log.info(
            "orchestrator.no_setup_wait", symbol=symbol, market=market.value,
            use_ai=use_ai,
        )
        final_decision = rule_decision

    elif not use_ai:
        # Setup found, but AI is OFF for this market — rule decision is final.
        # Pure-algorithm mode: still ZERO LLM cost.
        log.info(
            "orchestrator.rule_only_setup", symbol=symbol, market=market.value,
            direction=rule_decision["decision"],
            confidence=rule_decision["confidence_level"],
        )
        final_decision = rule_decision

    else:
        # Setup + use_ai=True — invoke the LLM verification pipeline.
        decision_source = "ai_verified"

        # Quant + Sentiment in parallel
        quant_task = asyncio.create_task(
            _safe(f"quant:{symbol}", _run_quant(
                symbol=symbol, market=market, term=term,
                ohlcv_by_interval=ohlcv,
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
                "orchestrator.agent_missing_fallback_to_rule",
                symbol=symbol, quant_ok=quant is not None,
                sentiment_ok=sentiment is not None,
            )
            # Fail safe to rule decision rather than burning more tokens on retry
            final_decision = rule_decision
        else:
            # Chart only on high-confidence setups (token-economy gate)
            primary_candles = ohlcv.get(primary_iv) or []
            if rule_decision["confidence_level"] >= _VISION_CONFIDENCE_THRESHOLD:
                chart_b64 = await asyncio.to_thread(
                    render_chart_base64,
                    primary_candles, symbol=symbol, interval=primary_iv,
                )

            microstructure = await _fetch_microstructure(symbol, market)

            master_decision = await _safe(
                f"master:{symbol}",
                _run_master_trader(
                    symbol=symbol, market=market, term=term,
                    primary_interval=primary_iv,
                    quant=quant, sentiment=sentiment,
                    chart_b64=chart_b64,
                    execution_mode=execution_mode,
                    pending_order=pending_order,
                    microstructure=microstructure,
                ),
            )

            # Audit: record the FULL raw outputs of each LLM agent (untruncated).
            # `master_decision` is the JSON Master Trader emitted — including
            # its `justification` field which is the AI's nihai karar cümlesi.
            trace["ai_analysis"] = {
                "vision_chart_attached": chart_b64 is not None,
                "vision_threshold": _VISION_CONFIDENCE_THRESHOLD,
                "quant_report": quant,
                "sentiment_report": sentiment,
                "microstructure": microstructure,
                "master_decision": master_decision,
            }

            if master_decision is None:
                # Master failed — fall back to the rule engine's setup
                log.warning(
                    "orchestrator.master_failed_fallback_to_rule", symbol=symbol,
                )
                final_decision = rule_decision
            else:
                final_decision = _normalize_decision(master_decision)

    # 5. Tag and persist the final decision for the UI
    final_decision["source"] = decision_source

    bundle = {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "execution_mode": execution_mode.value,
        "use_ai": use_ai,
        "rule_decision": rule_decision,  # always present
        "quant": quant,                  # null when use_ai=False
        "sentiment": sentiment,          # null when use_ai=False
        "microstructure": microstructure,
        "pending_order": pending_order,
        "decision": final_decision,
        "produced_at": datetime.now(timezone.utc).isoformat(),
    }
    await cache.set_json(f"decision:{symbol}:latest", bundle, ttl=3600)

    decision = final_decision  # downstream branches use this name

    action = decision.get("decision")

    # Pre-route trace: validation snapshot derived from the final decision.
    trace["validation"] = {
        "decision": action,
        "entry": decision.get("entry") or decision.get("entry_price"),
        "take_profit": decision.get("take_profit"),
        "stop_loss": decision.get("stop_loss"),
        "reward_risk_ratio": decision.get("reward_risk_ratio"),
        "risk_usd": decision.get("risk_usd"),
        "leverage": decision.get("leverage"),
        "confidence_level": decision.get("confidence_level"),
        "has_complete_plan": all([
            (decision.get("entry") or decision.get("entry_price")) is not None,
            decision.get("take_profit") is not None,
            decision.get("stop_loss") is not None,
        ]),
    }

    result: dict[str, Any] | None

    # 7a. CANCEL_PENDING — Layer 2 of the staleness manager (AI-driven).
    if action == "CANCEL_PENDING":
        if not pending_order:
            log.warning(
                "orchestrator.cancel_pending_invalid_no_order", symbol=symbol,
            )
            result = {
                "signal_id": None,
                "trade_id": None,
                "executed": False,
                "effective_mode": execution_mode,
                "reason": "cancel_pending_invalid",
            }
        else:
            canceled_n = await _safe(
                f"cancel_pending:{symbol}",
                cancel_pending_for_symbol(symbol, reason="ai_invalidated_thesis"),
            ) or 0
            log.info(
                "orchestrator.cancel_pending_done",
                symbol=symbol, trade_id=pending_order["trade_id"], canceled=canceled_n,
                justification=(decision.get("justification") or "")[:200],
            )
            result = {
                "signal_id": None,
                "trade_id": pending_order["trade_id"],
                "executed": False,
                "effective_mode": execution_mode,
                "reason": "ai_canceled_pending",
                "canceled_count": canceled_n,
            }
    else:
        # 7b. Standard path — route LONG/SHORT/WAIT through the engine.
        result = await _safe(
            f"engine.route:{symbol}",
            engine.route(
                market=market,
                term=term,
                symbol=symbol,
                decision=decision,
                mode=execution_mode,
                quant_score=(quant or {}).get("quant_score"),
                sentiment_score=(sentiment or {}).get("sentiment_score"),
                fear_greed_index=(sentiment or {}).get("fear_greed_index"),
                macro_regime=(sentiment or {}).get("macro_regime"),
            ),
        )

    # Finalize trace.execution with what actually happened
    trace["execution"] = {
        "mode_requested": execution_mode.value,
        "mode_applied": (
            result.get("effective_mode").value
            if result and result.get("effective_mode")
            else None
        ),
        "executed": bool(result and result.get("executed")),
        "signal_id": (result or {}).get("signal_id"),
        "trade_id": (result or {}).get("trade_id"),
        "route_reason": (result or {}).get("reason"),
        "had_pending_order": bool(pending_order),
        "decision_source": decision_source,
    }

    # Persist the audit row — every exit path lands here.
    await _persist_audit(
        market=market, symbol=symbol, use_ai=use_ai,
        execution_mode=execution_mode, trace=trace,
        decision=decision, result=result,
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


async def _is_system_enabled() -> bool:
    """Master switch — `config:system_enabled` Redis flag. Default OFF.

    Gated INSIDE `run_market_cycle` so every entry point respects it
    (scheduler ticks, the boot-time kickstart, future admin tools, tests).
    """
    try:
        val = await cache.client.get("config:system_enabled")
        return val == "1"
    except Exception as e:
        log.warning("orchestrator.system_flag_read_failed", err=str(e))
        return False  # Fail-safe: stay paused if Redis is unreachable.


async def run_market_cycle(market_config: MarketConfig) -> None:
    """Run a full cycle for one market, iterating its configured symbols."""
    # Master switch — covers BOTH scheduler ticks AND the boot kickstart.
    if not await _is_system_enabled():
        log.info(
            "orchestrator.system_paused", market=market_config.market.value
        )
        return

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

    # Macro context — only fetched when this market actually uses AI.
    # In rule-only mode (use_ai=False) we save the HTTP calls to RSS/FRED/
    # DefiLlama AND prevent the Sentiment LLM from running entirely.
    macro_context: dict[str, Any] = {}
    if market_config.use_ai:
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
                use_ai=market_config.use_ai,
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
