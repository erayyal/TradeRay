from __future__ import annotations

from typing import Any

from agents.llm_client import call_agent, extract_json
from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)

SYSTEM_PROMPT = """You are the QUANT ANALYST agent of the TradeRay trading desk.

Your job: read OHLCV + TA-Lib indicators from Redis snapshots and produce a
structured, mathematical assessment of the market for a single symbol.

Strict rules:
- You DO NOT decide trades. You describe the market.
- You DO NOT speculate beyond what the indicators show.
- Cite numerical values (RSI, MACD hist, BB position, ATR) in your analysis.
- Output a single JSON object, nothing else. No prose around it.

Output schema:
{
  "symbol": "<SYMBOL>",
  "trend": "bullish" | "bearish" | "ranging",
  "trend_strength": 0.0-1.0,
  "momentum": "accelerating_up" | "accelerating_down" | "decelerating" | "flat",
  "volatility_regime": "low" | "normal" | "elevated" | "extreme",
  "key_levels": {"support": <float>, "resistance": <float>},
  "indicator_signals": {
    "rsi_state": "overbought" | "oversold" | "neutral",
    "macd_state": "bullish_cross" | "bearish_cross" | "bullish" | "bearish" | "flat",
    "bollinger_state": "upper_band" | "lower_band" | "mid_band" | "expanding" | "contracting",
    "trend_filter": "above_200ema" | "below_200ema" | "no_data"
  },
  "atr_pct": <float>,                  // ATR / price
  "summary": "<one sentence>"
}
"""


async def run_quant_analyst(symbol: str) -> dict[str, Any]:
    """Pull indicator snapshots across timeframes and ask the Quant Analyst
    for a structured assessment."""

    snapshot = {
        "symbol": symbol,
        "indicators_1m": await cache.get_json(f"indicators:{symbol}:1m"),
        "indicators_5m": await cache.get_json(f"indicators:{symbol}:5m"),
        "indicators_15m": await cache.get_json(f"indicators:{symbol}:15m"),
        "last_price": await cache.get_price(symbol),
    }

    raw = await call_agent(
        system_prompt=SYSTEM_PROMPT,
        user_payload=snapshot,
        max_tokens=1500,
    )
    result = extract_json(raw)
    log.info("quant.done", symbol=symbol, trend=result.get("trend"))
    return result
