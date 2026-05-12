from __future__ import annotations

from typing import Any

from agents.llm_client import call_agent, extract_json
from config import settings
from core.logger import get_logger

log = get_logger(__name__)

SYSTEM_PROMPT = f"""You are the MASTER TRADER agent of the TradeRay trading desk.

You receive:
1. A QUANT report describing the market structure for one symbol.
2. A SENTIMENT report describing news + macro + on-chain mood.
3. A RISK envelope (max risk per trade, portfolio notional, leverage cap).

Your job: decide LONG, SHORT, or WAIT and — if taking a position — calculate
strict Entry, Take-Profit, and Stop-Loss levels.

Hard rules (non-negotiable):
- Risk per trade MUST NOT exceed {settings.max_risk_pct * 100:.2f}% of portfolio_notional.
- Stop-Loss distance from Entry should be sized using ATR (typically 1.5x-2x ATR).
- Take-Profit must give at least a 1.5:1 reward:risk ratio.
- If Quant trend conflicts with Sentiment macro_regime, prefer WAIT unless
  one is overwhelmingly strong.
- If volatility_regime is "extreme", reduce position size or WAIT.
- Output STRICT JSON only — no prose, no markdown, no commentary.

Output schema:
{{
  "symbol": "<SYMBOL>",
  "decision": "LONG" | "SHORT" | "WAIT",
  "confidence": 0.0-1.0,
  "rationale": "<1-2 sentences>",
  "entry": <float | null>,
  "take_profit": <float | null>,
  "stop_loss": <float | null>,
  "leverage": <int>,
  "position_size_usd": <float | null>,    // notional, post-leverage
  "risk_usd": <float | null>,              // (entry - stop) * size_in_base
  "reward_risk_ratio": <float | null>,
  "valid_until_seconds": <int>             // how long this decision is fresh
}}

If decision is WAIT, set entry/tp/sl/size/risk fields to null.
"""


async def run_master_trader(
    *,
    symbol: str,
    quant_report: dict[str, Any],
    sentiment_report: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "quant": quant_report,
        "sentiment": sentiment_report,
        "risk_envelope": {
            "portfolio_notional_usd": settings.portfolio_notional,
            "max_risk_pct": settings.max_risk_pct,
            "max_risk_usd": settings.portfolio_notional * settings.max_risk_pct,
            "max_leverage": settings.default_leverage,
            "quote_asset": settings.quote_asset,
        },
    }

    raw = await call_agent(
        system_prompt=SYSTEM_PROMPT,
        user_payload=payload,
        max_tokens=1500,
    )
    decision = extract_json(raw)

    # Defensive: never let an LLM drift past the configured risk cap
    max_risk = settings.portfolio_notional * settings.max_risk_pct
    if decision.get("risk_usd") and decision["risk_usd"] > max_risk * 1.05:
        log.warning(
            "master.risk_breach_blocked",
            risk_usd=decision["risk_usd"],
            cap=max_risk,
        )
        decision["decision"] = "WAIT"
        decision["rationale"] = (
            f"BLOCKED: requested risk {decision['risk_usd']:.2f} > cap {max_risk:.2f}"
        )

    decision["symbol"] = symbol
    log.info(
        "master.done",
        symbol=symbol,
        decision=decision.get("decision"),
        confidence=decision.get("confidence"),
    )
    return decision
