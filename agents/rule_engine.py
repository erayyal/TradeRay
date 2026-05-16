"""Literature-grounded rule engine — market × term parameter matrix.

Replaces the prior single-ruleset approach (RSI<40 + MACD_hist>0 + EMA + multi-TF)
with three distinct decision biases driven by literature:

  - **TF** (Trend-Following)   — for MID_TERM equity/crypto positions where
                                  time-series momentum dominates
                                  (Moskowitz/Ooi/Pedersen 2012, Asness/Moskowitz/
                                  Pedersen 2013).
  - **MR** (Mean-Reversion)    — for SCALP where Connors RSI(2) extremes inside
                                  a higher-TF uptrend produce the most defensible
                                  short-term edge (Connors-Alvarez 2008, refined
                                  by 2010+ practitioner retro-tests).
  - **HYB** (Hybrid, ADX-gated)— for SHORT_TERM: ADX>25 → pull-back-in-trend
                                  (TF mode); ADX<20 → BB mean-reversion (MR mode);
                                  20≤ADX≤25 → WAIT (Wilder DMI/ADX 1978).

The parameter matrix per (market, term) follows RESEARCH_ALGORITHM.md §9.
Notable corrections vs the previous engine:

  - RSI thresholds are NO LONGER 40/60 — that had no empirical basis.
    SCALP uses RSI(2) at 10/90 (Connors); higher TFs use RSI(14) at 30/70 or 35/65.
  - Multi-TF alignment uses ONLY the signal interval + ONE confirmation interval
    (~4-6× longer). The prior "all available intervals must agree" rule was
    over-restrictive and likely curve-fit.
  - ATR multipliers and R:R targets vary by term and market — BIST gets wider
    stops (2.0×ATR) and 1.5% risk cap (gap risk + lower liquidity).
  - Volume confirmation gate (rel_volume ≥ threshold) reduces false signals.

EVERY non-WAIT decision still produces a complete entry/TP/SL plan that the
execution engine's strict TP/SL gate accepts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import settings
from core.logger import get_logger
from models import MarketType, Term

log = get_logger(__name__)


Bias = Literal["TF", "MR", "HYB"]


# ---------------------------------------------------------------------------
# Per-(market, term) parameter matrix.
# Source: RESEARCH_ALGORITHM.md §9.1 / §9.2 / §9.3.
# Thresholds are starting points — walk-forward backtest must validate before
# any production tuning (RESEARCH_ALGORITHM.md §7).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TermParams:
    """One row in the (market × term) policy table."""
    signal_interval: str            # the TF the decision is read on
    confirm_interval: str | None    # one higher TF for direction confirmation; None = standalone
    bias: Bias
    rsi_period: int                 # 2 for SCALP (Connors), 14 elsewhere (Wilder)
    rsi_long_max: float             # LONG triggers when RSI ≤ this on the signal TF
    rsi_short_min: float            # SHORT triggers when RSI ≥ this
    atr_sl_mult: float              # stop distance = mult × ATR
    rr_target: float                # take-profit = (rr_target × ATR_sl_distance) from entry
    leverage_cap: int
    risk_pct: float                 # % portfolio risked per trade
    rel_volume_min: float           # volume confirmation gate (1.0 = at average; >1 = above-avg)
    adx_min_for_trend: float = 25.0
    adx_max_for_range: float = 20.0


# CRYPTO — 24/7 perpetuals, leverage allowed
CRYPTO_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=10.0, rsi_short_min=90.0,   # Connors RSI(2)
        atr_sl_mult=1.0, rr_target=1.5,
        leverage_cap=3, risk_pct=0.02,
        rel_volume_min=1.2,
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=35.0, rsi_short_min=65.0,
        atr_sl_mult=1.5, rr_target=2.0,
        leverage_cap=3, risk_pct=0.02,
        rel_volume_min=1.2,
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,
        atr_sl_mult=2.0, rr_target=3.0,
        leverage_cap=2, risk_pct=0.02,
        rel_volume_min=1.0,
    ),
}

# US EQUITIES (SP500 / NASDAQ) — RTH only, no leverage in signal-only mode
EQUITY_US_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=5.0, rsi_short_min=95.0,    # Connors original
        atr_sl_mult=1.0, rr_target=1.5,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=1.5,                                    # equity intraday wants more confirmation
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=35.0, rsi_short_min=65.0,
        atr_sl_mult=1.5, rr_target=2.0,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=1.3,
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,
        atr_sl_mult=2.0, rr_target=3.0,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=1.0,
    ),
}

# BIST — wider stops (TR equity vol + gap risk), reduced per-trade risk
BIST_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=10.0, rsi_short_min=90.0,
        atr_sl_mult=2.0, rr_target=1.5,
        leverage_cap=1, risk_pct=0.015,                        # BIST risk_pct lower (gap risk)
        rel_volume_min=1.5,
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=35.0, rsi_short_min=65.0,
        atr_sl_mult=2.0, rr_target=2.0,
        leverage_cap=1, risk_pct=0.015,
        rel_volume_min=1.3,
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,
        atr_sl_mult=2.5, rr_target=3.0,                        # BIST mid-term widest stops
        leverage_cap=1, risk_pct=0.015,
        rel_volume_min=1.0,
    ),
}


_PARAM_TABLE: dict[MarketType, dict[Term, TermParams]] = {
    MarketType.CRYPTO: CRYPTO_PARAMS,
    MarketType.SP500: EQUITY_US_PARAMS,
    MarketType.NASDAQ: EQUITY_US_PARAMS,
    MarketType.BIST: BIST_PARAMS,
}


def params_for(market: MarketType, term: Term) -> TermParams:
    return _PARAM_TABLE[market][term]


# ---------------------------------------------------------------------------
# Decision helpers
# ---------------------------------------------------------------------------

WAIT_VALID_SECS: int = 1800
SETUP_VALID_SECS: int = 1800


def _empty_wait(symbol: str, market: MarketType, term: Term, p: TermParams, reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "decision": "WAIT",
        "confidence_level": 0,
        "entry_price": None,
        "entry": None,
        "take_profit": None,
        "stop_loss": None,
        "leverage": 1,
        "position_size_base": None,
        "position_notional_usd": None,
        "risk_usd": None,
        "reward_risk_ratio": None,
        "valid_until_seconds": WAIT_VALID_SECS,
        "vision_confirms_quant": None,
        "chart_observations": [
            "rule-engine: no setup",
            f"bias={p.bias}, signal={p.signal_interval}",
        ],
        "rulebook_references": ["rule_engine.v2"],
        "conflict_flags": [],
        "cancel_target_client_id": None,
        "source": "rule_engine",
        "justification": reason,
    }


def _confirm_direction(
    indicators: dict[str, dict],
    confirm_iv: str | None,
    direction: Literal["LONG", "SHORT"],
) -> tuple[bool, str]:
    """Higher-TF directional confirmation (2-TF alignment, NOT N-TF).

    Returns (ok, reason). When confirm_iv is None we treat as standalone TF.
    """
    if confirm_iv is None:
        return True, "no confirmation TF required"
    conf = indicators.get(confirm_iv) or {}
    if not conf or conf.get("error"):
        return False, f"confirmation interval {confirm_iv} has no usable data"
    above = conf.get("above_ema_slow")
    if above is None:
        return False, f"confirmation interval {confirm_iv} missing EMA position"
    if direction == "LONG" and not above:
        return False, f"confirmation interval {confirm_iv} below EMA_slow (long blocked)"
    if direction == "SHORT" and above:
        return False, f"confirmation interval {confirm_iv} above EMA_slow (short blocked)"
    return True, f"confirmed by {confirm_iv}"


def _build_setup(
    *, symbol: str, market: MarketType, term: Term,
    p: TermParams, direction: Literal["LONG", "SHORT"],
    entry: float, atr: float, primary: dict[str, Any],
    confidence: int, justification: str,
) -> dict[str, Any]:
    """Construct a full plan with ATR-based SL/TP and risk-derived sizing."""
    sl = entry - p.atr_sl_mult * atr if direction == "LONG" else entry + p.atr_sl_mult * atr
    tp = (entry + p.rr_target * p.atr_sl_mult * atr) if direction == "LONG" \
         else (entry - p.rr_target * p.atr_sl_mult * atr)

    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return _empty_wait(symbol, market, term, p, "degenerate ATR — zero risk_per_unit")

    max_risk_usd = settings.portfolio_notional * p.risk_pct
    size_base = max_risk_usd / risk_per_unit
    notional_usd = entry * size_base
    rr = abs(tp - entry) / risk_per_unit

    return {
        "symbol": symbol,
        "market": market.value,
        "term": term.value,
        "decision": direction,
        "confidence_level": confidence,
        "entry_price": entry,
        "entry": entry,
        "take_profit": tp,
        "stop_loss": sl,
        "leverage": p.leverage_cap,
        "position_size_base": size_base,
        "position_notional_usd": notional_usd,
        "risk_usd": max_risk_usd,
        "reward_risk_ratio": rr,
        "valid_until_seconds": SETUP_VALID_SECS,
        "vision_confirms_quant": None,
        "chart_observations": [
            f"rule-engine {direction} ({p.bias} mode)",
            f"RSI({p.rsi_period}) {primary.get('rsi_short' if p.rsi_period == 2 else 'rsi'):.1f}",
            f"ADX {primary.get('adx', 0):.1f} ({primary.get('adx_regime', '?')})",
        ],
        "rulebook_references": [f"rule_engine.v2:{market.value}:{term.value}:{p.bias}"],
        "conflict_flags": [],
        "cancel_target_client_id": None,
        "source": "rule_engine",
        "justification": justification,
    }


# ---------------------------------------------------------------------------
# Mode-specific evaluators
# ---------------------------------------------------------------------------

def _evaluate_mr(
    *, symbol: str, market: MarketType, term: Term, p: TermParams,
    indicators: dict[str, dict],
) -> dict[str, Any]:
    """Mean-reversion mode (SCALP). Connors RSI extremes + BB tail + trend filter."""
    primary = indicators[p.signal_interval]

    rsi_value = primary.get("rsi_short" if p.rsi_period == 2 else "rsi")
    bb_pos = primary.get("bb_position")
    atr = primary.get("atr")
    last = primary.get("last_close")
    rel_vol = primary.get("rel_volume")

    if rsi_value is None or atr is None or last is None:
        return _empty_wait(symbol, market, term, p, "missing core indicators (RSI/ATR/close)")

    # Volume confirmation
    if rel_vol is not None and rel_vol < p.rel_volume_min:
        return _empty_wait(
            symbol, market, term, p,
            f"volume confirmation fail: rel_vol={rel_vol:.2f} < {p.rel_volume_min}",
        )

    direction: Literal["LONG", "SHORT"] | None = None
    if rsi_value <= p.rsi_long_max:
        direction = "LONG"
    elif rsi_value >= p.rsi_short_min:
        direction = "SHORT"

    if direction is None:
        return _empty_wait(
            symbol, market, term, p,
            f"MR threshold not met: RSI({p.rsi_period})={rsi_value:.1f} "
            f"(needs ≤{p.rsi_long_max} or ≥{p.rsi_short_min})",
        )

    # BB confirmation (optional, strengthens MR signal)
    if bb_pos is not None:
        if direction == "LONG" and bb_pos > 0.2:
            return _empty_wait(
                symbol, market, term, p,
                f"LONG MR but price not near lower BB (bb_pos={bb_pos:.2f})",
            )
        if direction == "SHORT" and bb_pos < 0.8:
            return _empty_wait(
                symbol, market, term, p,
                f"SHORT MR but price not near upper BB (bb_pos={bb_pos:.2f})",
            )

    # Higher-TF trend filter — MR must align with the trend on the confirm TF
    ok, conf_reason = _confirm_direction(indicators, p.confirm_interval, direction)
    if not ok:
        return _empty_wait(symbol, market, term, p, f"trend filter: {conf_reason}")

    # Confidence: deeper extreme = higher confidence
    confidence = 60
    if p.rsi_period == 2:
        if (direction == "LONG" and rsi_value < 5) or (direction == "SHORT" and rsi_value > 95):
            confidence += 20
    else:
        if (direction == "LONG" and rsi_value < 25) or (direction == "SHORT" and rsi_value > 75):
            confidence += 15
    if bb_pos is not None and (
        (direction == "LONG" and bb_pos < 0.1) or (direction == "SHORT" and bb_pos > 0.9)
    ):
        confidence += 10
    confidence = min(95, confidence)

    just = (
        f"MR {direction}: RSI({p.rsi_period})={rsi_value:.1f}, "
        f"BB_pos={bb_pos:.2f}, {conf_reason}. "
        f"SL {p.atr_sl_mult}×ATR / TP {p.rr_target}R."
    )
    return _build_setup(
        symbol=symbol, market=market, term=term, p=p, direction=direction,
        entry=float(last), atr=float(atr), primary=primary,
        confidence=confidence, justification=just,
    )


def _evaluate_tf(
    *, symbol: str, market: MarketType, term: Term, p: TermParams,
    indicators: dict[str, dict],
) -> dict[str, Any]:
    """Trend-following mode (MID_TERM). Price > EMA_slow + positive momentum + ADX trending."""
    primary = indicators[p.signal_interval]

    last = primary.get("last_close")
    ema_slow = primary.get("ema_slow")
    above = primary.get("above_ema_slow")
    macd_hist = primary.get("macd_hist")
    adx = primary.get("adx")
    atr = primary.get("atr")
    rsi_value = primary.get("rsi")
    rel_vol = primary.get("rel_volume")

    if any(v is None for v in (last, ema_slow, atr, adx, macd_hist)):
        return _empty_wait(symbol, market, term, p, "missing TF core indicators (EMA/ATR/ADX/MACD)")

    # Volume gate
    if rel_vol is not None and rel_vol < p.rel_volume_min:
        return _empty_wait(
            symbol, market, term, p,
            f"volume confirmation fail: rel_vol={rel_vol:.2f} < {p.rel_volume_min}",
        )

    # Direction gate
    direction: Literal["LONG", "SHORT"] | None = None
    if above and macd_hist > 0 and adx >= p.adx_min_for_trend:
        direction = "LONG"
    elif (above is False) and macd_hist < 0 and adx >= p.adx_min_for_trend:
        direction = "SHORT"

    if direction is None:
        return _empty_wait(
            symbol, market, term, p,
            f"TF gate not met: above_EMA={above}, MACD_hist={macd_hist:+.4f}, "
            f"ADX={adx:.1f} (need ≥{p.adx_min_for_trend})",
        )

    # Optional pullback filter — don't enter at extreme momentum
    if rsi_value is not None:
        if direction == "LONG" and rsi_value > p.rsi_short_min:
            return _empty_wait(
                symbol, market, term, p,
                f"TF LONG but RSI={rsi_value:.1f} overbought ≥{p.rsi_short_min} — wait for pullback",
            )
        if direction == "SHORT" and rsi_value < p.rsi_long_max:
            return _empty_wait(
                symbol, market, term, p,
                f"TF SHORT but RSI={rsi_value:.1f} oversold ≤{p.rsi_long_max} — wait for bounce",
            )

    confidence = 65
    if adx > 30:
        confidence += 15
    if abs(macd_hist) > 0:
        confidence += 5
    confidence = min(95, confidence)

    just = (
        f"TF {direction}: price{'>' if direction=='LONG' else '<'}EMA_slow, "
        f"MACD_hist={macd_hist:+.4f}, ADX={adx:.1f} ({primary.get('adx_regime', '?')}). "
        f"SL {p.atr_sl_mult}×ATR / TP {p.rr_target}R."
    )
    return _build_setup(
        symbol=symbol, market=market, term=term, p=p, direction=direction,
        entry=float(last), atr=float(atr), primary=primary,
        confidence=confidence, justification=just,
    )


def _evaluate_hyb(
    *, symbol: str, market: MarketType, term: Term, p: TermParams,
    indicators: dict[str, dict],
) -> dict[str, Any]:
    """Hybrid mode (SHORT_TERM). ADX selects between TF and MR; transitional band → WAIT."""
    primary = indicators[p.signal_interval]
    adx = primary.get("adx")

    if adx is None:
        return _empty_wait(symbol, market, term, p, "ADX unavailable — can't choose hybrid mode")

    if adx >= p.adx_min_for_trend:
        # Trending regime → pull-back-in-trend
        # Re-use _evaluate_tf logic but allow RSI to be a touch oversold/overbought
        # (a pullback inside an uptrend is exactly what we want here).
        last = primary.get("last_close")
        ema_slow = primary.get("ema_slow")
        above = primary.get("above_ema_slow")
        atr = primary.get("atr")
        rsi_value = primary.get("rsi")
        macd_hist = primary.get("macd_hist")
        rel_vol = primary.get("rel_volume")

        if any(v is None for v in (last, ema_slow, atr, rsi_value, macd_hist)):
            return _empty_wait(symbol, market, term, p, "missing HYB-TF core indicators")
        if rel_vol is not None and rel_vol < p.rel_volume_min:
            return _empty_wait(
                symbol, market, term, p,
                f"volume confirmation fail: rel_vol={rel_vol:.2f} < {p.rel_volume_min}",
            )

        direction: Literal["LONG", "SHORT"] | None = None
        # In TRENDING regime we ALLOW oversold RSI (it's a pullback) — that's the whole point
        if above and rsi_value <= p.rsi_long_max:
            direction = "LONG"
        elif (above is False) and rsi_value >= p.rsi_short_min:
            direction = "SHORT"

        if direction is None:
            return _empty_wait(
                symbol, market, term, p,
                f"HYB-TF (ADX={adx:.1f} trending) but no pullback: above_EMA={above}, RSI={rsi_value:.1f}",
            )

        ok, conf_reason = _confirm_direction(indicators, p.confirm_interval, direction)
        if not ok:
            return _empty_wait(symbol, market, term, p, f"trend filter: {conf_reason}")

        confidence = 70 + (15 if adx > 30 else 0)
        confidence = min(95, confidence)
        just = (
            f"HYB-TF {direction}: ADX={adx:.1f} trending, price{'>' if direction=='LONG' else '<'}EMA, "
            f"RSI={rsi_value:.1f} pullback, {conf_reason}. SL {p.atr_sl_mult}×ATR / TP {p.rr_target}R."
        )
        return _build_setup(
            symbol=symbol, market=market, term=term, p=p, direction=direction,
            entry=float(last), atr=float(atr), primary=primary,
            confidence=confidence, justification=just,
        )

    if adx <= p.adx_max_for_range:
        # Ranging regime → BB mean-reversion (delegate to MR evaluator)
        return _evaluate_mr(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )

    # Transitional band — explicit WAIT (Wilder DMI/ADX practitioner consensus)
    return _empty_wait(
        symbol, market, term, p,
        f"HYB transitional: ADX={adx:.1f} between {p.adx_max_for_range}-{p.adx_min_for_trend}",
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def generate_rule_decision(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    primary_interval: str,             # kept for backwards-compat with orchestrator; we use params_for(market,term)
    indicators: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Dispatch to the (market × term) policy and run its bias-specific evaluator."""
    try:
        p = params_for(market, term)
    except KeyError:
        log.warning("rule_engine.no_params", market=market.value, term=term.value)
        return _empty_wait(
            symbol, market, term,
            TermParams(  # synthetic placeholder for the error path
                signal_interval=primary_interval, confirm_interval=None,
                bias="TF", rsi_period=14, rsi_long_max=40, rsi_short_min=60,
                atr_sl_mult=2.0, rr_target=2.0, leverage_cap=1, risk_pct=0.02,
                rel_volume_min=1.0,
            ),
            f"no parameter set defined for {market.value}/{term.value}",
        )

    # Signal interval must be present and computable
    primary = indicators.get(p.signal_interval)
    if not primary or primary.get("error"):
        return _empty_wait(
            symbol, market, term, p,
            f"signal interval {p.signal_interval} has insufficient indicator data",
        )

    if p.bias == "MR":
        decision = _evaluate_mr(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )
    elif p.bias == "TF":
        decision = _evaluate_tf(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )
    else:  # HYB
        decision = _evaluate_hyb(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )

    if decision["decision"] != "WAIT":
        log.info(
            "rule_engine.setup",
            symbol=symbol, market=market.value, term=term.value,
            bias=p.bias, direction=decision["decision"],
            confidence=decision["confidence_level"],
            rr=round(decision["reward_risk_ratio"], 2),
        )
    return decision


__all__ = ["generate_rule_decision", "params_for", "TermParams"]
