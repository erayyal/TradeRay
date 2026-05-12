from __future__ import annotations

from typing import Any

from agents.llm_client import call_agent, extract_json
from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)

SYSTEM_PROMPT = """You are the SENTIMENT SCANNER agent of the TradeRay trading desk.

Your job: synthesize crypto news (CryptoPanic), on-chain stats (DefiLlama),
and macro indicators (FRED) into a short, structured read on market mood
and macroeconomic risk.

Strict rules:
- Do NOT recommend trades. You report mood and risk only.
- Be explicit about whether macro is supportive (risk-on) or hostile (risk-off).
- High VIX, inverted yield curve (T10Y2Y < 0), strong DXY = risk-off context.
- Output a single JSON object, nothing else.

Output schema:
{
  "fear_greed": "extreme_fear" | "fear" | "neutral" | "greed" | "extreme_greed",
  "fear_greed_score": -1.0 to 1.0,
  "macro_regime": "risk_on" | "neutral" | "risk_off",
  "macro_drivers": ["<short bullet>", ...],   // 1-4 items
  "news_themes": ["<short theme>", ...],      // 1-5 items
  "onchain_health": "healthy" | "neutral" | "degrading",
  "summary": "<one sentence>"
}
"""


async def run_sentiment_scanner() -> dict[str, Any]:
    payload = {
        "cryptopanic": await cache.get_json("sentiment:cryptopanic"),
        "macro": await cache.get_json("macro:fred"),
        "onchain": await cache.get_json("onchain:defillama"),
    }

    raw = await call_agent(
        system_prompt=SYSTEM_PROMPT,
        user_payload=payload,
        max_tokens=1200,
    )
    result = extract_json(raw)
    log.info(
        "sentiment.done",
        fear_greed=result.get("fear_greed"),
        macro=result.get("macro_regime"),
    )
    return result
