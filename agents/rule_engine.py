"""Pure rule-based decision engine — zero LLM, zero token cost.

The orchestrator calls this BEFORE any LLM. Two modes:

  1. `use_ai = False`        : the rule engine's verdict IS the final decision.
                               Master Trader never runs. Cheapest mode.

  2. `use_ai = True`         : the rule engine acts as a GATE.
                               - WAIT  → orchestrator short-circuits, no LLM call,
                                         no token spend. This is the dominant case
                                         in quiet markets and is the source of
                                         most cost savings vs. the prior design.
                               - LONG/SHORT → LLM verifies/refines the setup.
                               The rule decision is passed in the user payload so
                               the model can agree, adjust the levels, or veto.

Decision logic (deliberately simple — interpretable + auditable):

  LONG triggers when ALL hold on the primary interval:
    - RSI < 40                    (pullback into oversold-ish zone)
    - MACD histogram > 0          (momentum has turned up)
    - last_close > EMA_slow       (uptrend filter)
    - All available intervals agree on EMA position (multi-TF alignment)

  SHORT mirrors:
    - RSI > 60, MACD hist < 0, last_close < EMA_slow, alignment bearish

  Otherwise WAIT.

Risk plan (mandatory — engine.route() rejects on missing TP/SL):
  - stop_loss  = entry ∓ 1.5 × ATR
  - take_profit = entry ± 3.0 × ATR
  - reward_risk_ratio = 2.0 (fixed)
  - position size: max_risk_usd / |entry - stop|

Confidence is a heuristic blend of RSI extremity, MACD strength, and TF
alignment. Useful for the orchestrator's "render chart only if conf ≥ 70"
token-saving rule.
"""
from __future__ import annotations

from typing import Any

from config import settings
from core.logger import get_logger
from models import MarketType, Term

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunables (kept in the module so the trader can iterate without touching
# orchestrator wiring)
# ---------------------------------------------------------------------------

ATR_SL_MULT: float = 1.5          # stop-loss distance in ATRs
ATR_TP_MULT: float = 3.0          # take-profit distance in ATRs (R:R = 2.0)
RSI_LONG_MAX: float = 40.0        # LONG only if RSI is at most this (sub-trend pullback)
RSI_SHORT_MIN: float = 60.0       # SHORT only if RSI is at least this
WAIT_VALID_SECS: int = 1800
SETUP_VALID_SECS: int = 1800


def _empty_wait(symbol: str, market: MarketType, term: Term, reason: str) -> dict[str, Any]:
    """Return a fully-populated WAIT decision (no setup found)."""
    return {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "decision": "WAIT",
        "confidence_level": 0,
        "entry_price": None,
        "entry": None,           # legacy alias
        "take_profit": None,
        "stop_loss": None,
        "leverage": 1,
        "position_size_base": None,
        "position_notional_usd": None,
        "risk_usd": None,
        "reward_risk_ratio": None,
        "valid_until_seconds": WAIT_VALID_SECS,
        "vision_confirms_quant": None,
        "chart_observations": ["rule-engine: no setup"],
        "rulebook_references": ["rule_engine.v1"],
        "conflict_flags": [],
        "cancel_target_client_id": None,
        "source": "rule_engine",
        "justification": reason,
    }


def generate_rule_decision(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    primary_interval: str,
    indicators: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Generate a LONG / SHORT / WAIT decision from TA-Lib indicators only.

    Returns a dict matching the Master Trader's output shape so the
    execution engine and persistence layer treat both code paths identically.
    """
    primary = indicators.get(primary_interval)
    if not primary or primary.get("error"):
        return _empty_wait(
            symbol, market, term,
            f"insufficient indicator data on {primary_interval}",
        )

    last = primary.get("last_close")
    rsi = primary.get("rsi")
    macd_hist = primary.get("macd_hist")
    ema_slow = primary.get("ema_slow")
    atr = primary.get("atr")

    if any(v is None for v in (last, rsi, macd_hist, ema_slow, atr)):
        return _empty_wait(
            symbol, market, term,
            "missing required indicators (RSI/MACD_hist/EMA_slow/ATR)",
        )

    # Multi-timeframe alignment — every interval that reports EMA position
    # must agree. If alignment is mixed, fall through to WAIT.
    ema_positions = [
        ind.get("above_ema_slow")
        for ind in indicators.values()
        if ind.get("above_ema_slow") is not None
    ]
    bull_aligned = bool(ema_positions) and all(ema_positions)
    bear_aligned = bool(ema_positions) and not any(ema_positions)

    # ----- Direction gate -----
    direction: str | None = None
    if rsi < RSI_LONG_MAX and macd_hist > 0 and last > ema_slow and bull_aligned:
        direction = "LONG"
    elif rsi > RSI_SHORT_MIN and macd_hist < 0 and last < ema_slow and bear_aligned:
        direction = "SHORT"

    if direction is None:
        return _empty_wait(
            symbol, market, term,
            (
                f"rules not satisfied: RSI={rsi:.1f}, MACD_hist={macd_hist:.4f}, "
                f"price{'>' if last > ema_slow else '<'}EMA_slow, "
                f"alignment={'bull' if bull_aligned else ('bear' if bear_aligned else 'mixed')}"
            ),
        )

    # ----- Risk plan (ATR-based, R:R = 2.0 by construction) -----
    entry = float(last)
    if direction == "LONG":
        sl = entry - ATR_SL_MULT * atr
        tp = entry + ATR_TP_MULT * atr
    else:
        sl = entry + ATR_SL_MULT * atr
        tp = entry - ATR_TP_MULT * atr

    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0 or tp == entry:
        return _empty_wait(
            symbol, market, term,
            "degenerate risk plan — ATR or entry produced zero distance",
        )

    max_risk_usd = settings.portfolio_notional * settings.max_risk_pct
    size_base = max_risk_usd / risk_per_unit
    notional_usd = entry * size_base
    rr = abs(tp - entry) / risk_per_unit

    # ----- Confidence heuristic -----
    confidence = 60  # baseline for a passing setup
    if (direction == "LONG" and rsi < 30) or (direction == "SHORT" and rsi > 70):
        confidence += 15  # deeper RSI extreme
    if abs(macd_hist) > 0:
        confidence += 5
    # alignment is already required, but reward the case
    confidence += 10
    confidence = min(95, confidence)

    decision = {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "decision": direction,
        "confidence_level": confidence,
        "entry_price": entry,
        "entry": entry,  # legacy alias used by binance_executor / risk_manager
        "take_profit": tp,
        "stop_loss": sl,
        "leverage": settings.default_leverage,
        "position_size_base": size_base,
        "position_notional_usd": notional_usd,
        "risk_usd": max_risk_usd,
        "reward_risk_ratio": rr,
        "valid_until_seconds": SETUP_VALID_SECS,
        "vision_confirms_quant": None,
        "chart_observations": [
            f"rule-engine {direction}",
            f"RSI {rsi:.1f}",
            f"MACD_hist {macd_hist:.4f}",
        ],
        "rulebook_references": ["rule_engine.v1"],
        "conflict_flags": [],
        "cancel_target_client_id": None,
        "source": "rule_engine",
        "justification": (
            f"Rule-engine {direction}: RSI={rsi:.1f} {'<' if direction=='LONG' else '>'} "
            f"{RSI_LONG_MAX if direction=='LONG' else RSI_SHORT_MIN}, "
            f"MACD hist {macd_hist:+.4f}, price {'above' if direction=='LONG' else 'below'} "
            f"EMA_slow ({ema_slow:.2f}). Plan: SL {ATR_SL_MULT}×ATR, TP {ATR_TP_MULT}×ATR, "
            f"R:R {rr:.2f}."
        ),
    }

    log.info(
        "rule_engine.setup",
        symbol=symbol, market=market.value, decision=direction,
        confidence=confidence, rr=round(rr, 2),
    )
    return decision


__all__ = ["generate_rule_decision"]
