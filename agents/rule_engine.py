"""Literature-grounded rule engine — market × term parameter matrix
(v2.5 — adds VIX / TCMB / earnings / funding gates + vol-targeting sizing).

Three decision biases:
  - **TF** (Trend-Following) — Moskowitz/Ooi/Pedersen 2012, Asness/Moskowitz/Pedersen 2013
  - **MR** (Mean-Reversion)  — Connors-Alvarez 2008 (RSI(2) extremes)
  - **HYB** (Hybrid, ADX-gated) — Wilder DMI/ADX 1978

Macro / calendar / microstructure gates (Phase 3):
  - **VIX gate** (US): Whaley 2000/2009 — VIX>25 halves size, VIX>35 vetoes.
  - **FOMC blackout** (US): Lucca-Moench 2015 NY Fed drift study + Bernard-Thomas spirit.
  - **TCMB MPC blackout** (BIST): announcement window 13:00-17:00 TR — entry veto.
  - **Earnings blackout** (US): Bernard-Thomas 1989 PEAD — ±1 trading day.
  - **Funding rate extreme** (Crypto): Glassnode/Coinglass — abs(annualized funding)>50%
    flips the engine to mean-reversion bias even in HYB regime.

Vol-targeting position sizing (AQR/Harvey 2018):
  - Default sizing uses fixed risk_pct of portfolio. When `vol_target` is provided
    AND realized vol (from ATR_pct) is computable, sizing is scaled by
    (vol_target / asset_realized_vol), clamped [0.5, 1.5].

EVERY non-WAIT decision produces a complete entry/TP/SL plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from config import settings
from config.calendars import in_fomc_blackout, in_tcmb_blackout
from core.logger import get_logger
from data_fetchers.earnings_fetcher import is_in_earnings_blackout
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
    # Vol-targeting (AQR/Harvey 2018). When set, position size scales by
    # (vol_target_annual / asset_realized_vol_annual), clamped [0.5, 1.5].
    # Asset realized vol is approximated from ATR_pct × sqrt(periods_per_year).
    vol_target_annual: float | None = None
    # Periods per year for the signal interval — used by vol annualization.
    # 15m bar: 365×24×4 = ~35040 for crypto / ~252×6.5×4 for US equity = 6552.
    # We use crypto convention since it's the more aggressive sizer; for equities
    # the multiplier is conservative (over-sizes slightly), which is the safer
    # error direction in vol-targeting context.
    periods_per_year: int = 35040


# Vol-targeting starting defaults (Carver 2015 §11; AQR/Harvey 2018).
# These are the asset-class annualized vol *targets*. The engine compares this
# to the asset's CURRENT realized vol (estimated from ATR_pct) and scales
# position size by (target / realized), clamped to [0.5, 1.5]. The clamp
# ensures vol-targeting can't whipsaw size by more than 50% in either direction.
#
# Reasoning:
#   - Crypto perps run hot — 25% target leaves headroom for the asset's
#     natural ~60-100% realized vol, so most coins get DOWN-sized (which
#     is what we want for risk parity vs equity portfolios).
#   - US equities: S&P 500 long-run realized vol ~16-18%, so 15% target
#     gives a mild down-tilt on noisy weeks and slight up-tilt on calm ones.
#   - BIST equities: TR equity realized vol structurally higher than G7
#     (TL macro overlay, gap risk), so 20% target — wider than US.
_VOL_TARGET_CRYPTO = 0.25
_VOL_TARGET_US_EQUITY = 0.15
_VOL_TARGET_BIST = 0.20

# Periods-per-year for each signal interval. Crypto 24/7, US equity RTH (6.5h).
# BIST RTH is 8h but we use the same equity convention for simplicity.
_PPY_CRYPTO = {
    "15m": 365 * 24 * 4,
    "4h": 365 * 6,
    "1d": 365,
}
_PPY_EQUITY = {
    "15m": 252 * 26,           # ~26 15-min bars per US RTH session
    "4h": 252 * 2,             # ~2 4-hour bars per session (approx.)
    "1d": 252,
}


# Thresholds below were LOOSENED in v2.7 (2026-05-21) — backtest smoke test
# showed pre-v2.7 SHORT_TERM 4h produced 0 setups in 1500 bars, and SIGNAL_ONLY
# observation mode would never collect data. v2.7 is "observation-grade": more
# permissive thresholds so we get a real signal stream; AUTO_BOT still gated
# by §11 (paper-trade + walk-forward sweep).
#
# Changes vs v2.6 (annotated inline as `# v2.7`).


# CRYPTO — 24/7 perpetuals, leverage allowed
CRYPTO_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=15.0, rsi_short_min=85.0,   # v2.7: 10/90 → 15/85
        atr_sl_mult=1.0, rr_target=1.5,
        leverage_cap=3, risk_pct=0.02,
        rel_volume_min=1.0,                                    # v2.7: 1.2 → 1.0
        vol_target_annual=_VOL_TARGET_CRYPTO,
        periods_per_year=_PPY_CRYPTO["15m"],
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,  # v2.7: 35/65 → 40/60
        atr_sl_mult=1.5, rr_target=2.0,
        leverage_cap=3, risk_pct=0.02,
        rel_volume_min=1.0,                                    # v2.7: 1.2 → 1.0
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        adx_max_for_range=18.0,                                # v2.7: 20 → 18 (wider trans. band)
        vol_target_annual=_VOL_TARGET_CRYPTO,
        periods_per_year=_PPY_CRYPTO["4h"],
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=45.0, rsi_short_min=55.0,  # v2.7: 40/60 → 45/55
        atr_sl_mult=2.0, rr_target=3.0,
        leverage_cap=2, risk_pct=0.02,
        rel_volume_min=0.9,                                    # v2.7: 1.0 → 0.9
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        vol_target_annual=_VOL_TARGET_CRYPTO,
        periods_per_year=_PPY_CRYPTO["1d"],
    ),
}

# US EQUITIES (SP500 / NASDAQ) — RTH only, no leverage in signal-only mode
EQUITY_US_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=10.0, rsi_short_min=90.0,   # v2.7: 5/95 → 10/90 (less strict than Connors)
        atr_sl_mult=1.0, rr_target=1.5,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=1.2,                                    # v2.7: 1.5 → 1.2
        vol_target_annual=_VOL_TARGET_US_EQUITY,
        periods_per_year=_PPY_EQUITY["15m"],
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,  # v2.7: 35/65 → 40/60
        atr_sl_mult=1.5, rr_target=2.0,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=1.0,                                    # v2.7: 1.3 → 1.0
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        adx_max_for_range=18.0,                                # v2.7: 20 → 18
        vol_target_annual=_VOL_TARGET_US_EQUITY,
        periods_per_year=_PPY_EQUITY["4h"],
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=45.0, rsi_short_min=55.0,  # v2.7: 40/60 → 45/55
        atr_sl_mult=2.0, rr_target=3.0,
        leverage_cap=1, risk_pct=0.02,
        rel_volume_min=0.9,                                    # v2.7: 1.0 → 0.9
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        vol_target_annual=_VOL_TARGET_US_EQUITY,
        periods_per_year=_PPY_EQUITY["1d"],
    ),
}

# BIST — wider stops (TR equity vol + gap risk), reduced per-trade risk
BIST_PARAMS: dict[Term, TermParams] = {
    Term.SCALP: TermParams(
        signal_interval="15m", confirm_interval="1h",
        bias="MR",
        rsi_period=2, rsi_long_max=15.0, rsi_short_min=85.0,   # v2.7: 10/90 → 15/85
        atr_sl_mult=2.0, rr_target=1.5,
        leverage_cap=1, risk_pct=0.015,
        rel_volume_min=1.2,                                    # v2.7: 1.5 → 1.2
        vol_target_annual=_VOL_TARGET_BIST,
        periods_per_year=_PPY_EQUITY["15m"],
    ),
    Term.SHORT_TERM: TermParams(
        signal_interval="4h", confirm_interval="1d",
        bias="HYB",
        rsi_period=14, rsi_long_max=40.0, rsi_short_min=60.0,  # v2.7: 35/65 → 40/60
        atr_sl_mult=2.0, rr_target=2.0,
        leverage_cap=1, risk_pct=0.015,
        rel_volume_min=1.0,                                    # v2.7: 1.3 → 1.0
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        adx_max_for_range=18.0,                                # v2.7: 20 → 18
        vol_target_annual=_VOL_TARGET_BIST,
        periods_per_year=_PPY_EQUITY["4h"],
    ),
    Term.MID_TERM: TermParams(
        signal_interval="1d", confirm_interval=None,
        bias="TF",
        rsi_period=14, rsi_long_max=45.0, rsi_short_min=55.0,  # v2.7: 40/60 → 45/55
        atr_sl_mult=2.5, rr_target=3.0,                        # BIST mid-term widest stops
        leverage_cap=1, risk_pct=0.015,
        rel_volume_min=0.9,                                    # v2.7: 1.0 → 0.9
        adx_min_for_trend=22.0,                                # v2.7: 25 → 22
        vol_target_annual=_VOL_TARGET_BIST,
        periods_per_year=_PPY_EQUITY["1d"],
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

def _evaluate_gates(
    *,
    market: MarketType,
    macro_lite: dict[str, Any] | None,
    next_earnings_iso: str | None,
) -> tuple[bool, float, str]:
    """Macro/calendar/microstructure pre-gate.

    Returns (allow_entry, size_multiplier, reason).
      - allow_entry=False blocks the cycle outright (returns WAIT).
      - size_multiplier scales the eventual position size (1.0 = no change,
        0.5 = halve size in elevated-but-not-veto regimes).
    """
    macro_lite = macro_lite or {}

    # VIX gate (US only) — Whaley 2000/2009.
    if market in (MarketType.SP500, MarketType.NASDAQ):
        vix = macro_lite.get("vix")
        if vix is not None:
            if vix >= 35:
                return False, 0.0, f"VIX {vix:.1f}≥35 — vol crisis, no new entries"
            if vix >= 25:
                return True, 0.5, f"VIX {vix:.1f} elevated — size halved"

    # FOMC blackout (US only) — Lucca-Moench 2015 NY Fed drift study.
    if market in (MarketType.SP500, MarketType.NASDAQ):
        if in_fomc_blackout():
            return False, 0.0, "FOMC blackout window active"
        if is_in_earnings_blackout(next_earnings_iso):
            return False, 0.0, (
                f"earnings blackout active "
                f"(next earnings: {next_earnings_iso}) — PEAD vol veto"
            )

    # TCMB blackout (BIST only).
    if market == MarketType.BIST:
        if in_tcmb_blackout():
            return False, 0.0, "TCMB PPK blackout window (13:00-17:00 TR) active"
        # Huge daily USDTRY move flag (informational; soft size cut).
        usdtry = macro_lite.get("usdtry") or {}
        pct = usdtry.get("pct_change_1d")
        if pct is not None and abs(pct) >= 0.02:
            return True, 0.5, (
                f"USDTRY |Δ%|={abs(pct):.2%}≥2% — TR macro vol elevated, size halved"
            )

    # Crypto funding rate extremes — Glassnode/Coinglass.
    # |annualized funding| > 50% indicates excessively leveraged positioning
    # (long-squeeze risk on positive, short-squeeze on negative). In that regime
    # we down-weight trend-following and prefer mean-reversion. The bias FLIP
    # is handled in generate_rule_decision; here we just signal it via reason.
    # (The funding-flag-to-MR-bias flip happens BEFORE this function returns,
    # so this gate stays "allow with full size" for the crypto path.)

    return True, 1.0, "all gates clear"


def _vol_targeted_multiplier(
    p: TermParams, atr_pct: float | None
) -> tuple[float, str]:
    """Compute the vol-targeting size multiplier from realized vol estimate.

    Returns (multiplier, reason). Falls back to 1.0 when target/atr_pct unset.

    Realized vol estimate: ATR_pct × sqrt(periods_per_year) — a coarse
    annualized vol proxy widely used by AQR / Two Sigma. Slightly over-estimates
    in regimes with high gap risk, slightly under-estimates in fully-continuous
    markets — both biases are conservative for position sizing.
    """
    if p.vol_target_annual is None or atr_pct is None or atr_pct <= 0:
        return 1.0, "vol-target disabled"
    realized_vol = atr_pct * (p.periods_per_year ** 0.5)
    if realized_vol <= 0:
        return 1.0, "vol estimate degenerate"
    raw = p.vol_target_annual / realized_vol
    clamped = max(0.5, min(1.5, raw))
    return clamped, (
        f"vol_target={p.vol_target_annual:.0%}, realized≈{realized_vol:.0%}, "
        f"raw={raw:.2f}, clamped={clamped:.2f}"
    )


def generate_rule_decision(
    *,
    symbol: str,
    market: MarketType,
    term: Term,
    primary_interval: str,             # kept for backwards-compat; uses params_for(market, term)
    indicators: dict[str, dict[str, Any]],
    # Macro/microstructure context (None ⇒ gates skipped — backwards compat).
    macro_lite: dict[str, Any] | None = None,
    next_earnings_iso: str | None = None,
) -> dict[str, Any]:
    """Dispatch to the (market × term) policy and run its bias-specific evaluator.

    New (v2.5): pre-gate evaluates macro/calendar/microstructure context
    BEFORE the bias logic. A failed gate returns WAIT immediately; a soft
    gate (size halver) flows through and scales position size at construction.
    """
    try:
        p = params_for(market, term)
    except KeyError:
        log.warning("rule_engine.no_params", market=market.value, term=term.value)
        return _empty_wait(
            symbol, market, term,
            TermParams(
                signal_interval=primary_interval, confirm_interval=None,
                bias="TF", rsi_period=14, rsi_long_max=40, rsi_short_min=60,
                atr_sl_mult=2.0, rr_target=2.0, leverage_cap=1, risk_pct=0.02,
                rel_volume_min=1.0,
            ),
            f"no parameter set defined for {market.value}/{term.value}",
        )

    # ---- Pre-gate: macro / calendar / microstructure ---------------------
    allow, size_mult, gate_reason = _evaluate_gates(
        market=market,
        macro_lite=macro_lite,
        next_earnings_iso=next_earnings_iso,
    )
    if not allow:
        return _empty_wait(symbol, market, term, p, f"pre-gate: {gate_reason}")

    # ---- Crypto funding bias flip (Glassnode/Coinglass) ------------------
    # If funding is extreme in the SAME direction as a would-be trend trade,
    # we flip to mean-reversion bias (countertrend the crowded book).
    effective_bias: Bias = p.bias
    if market == MarketType.CRYPTO and macro_lite:
        funding = (macro_lite.get("funding_rate") or {})
        ann_pct = funding.get("annualized_pct")
        if ann_pct is not None and abs(ann_pct) > 50.0:
            if effective_bias == "TF":
                # Crowded book in trend direction → fade with MR
                effective_bias = "MR"
                log.info(
                    "rule_engine.funding_flip_to_mr",
                    symbol=symbol, annualized_pct=ann_pct,
                )

    # Signal interval must exist
    primary = indicators.get(p.signal_interval)
    if not primary or primary.get("error"):
        return _empty_wait(
            symbol, market, term, p,
            f"signal interval {p.signal_interval} has insufficient indicator data",
        )

    if effective_bias == "MR":
        decision = _evaluate_mr(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )
    elif effective_bias == "TF":
        decision = _evaluate_tf(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )
    else:  # HYB
        decision = _evaluate_hyb(
            symbol=symbol, market=market, term=term, p=p, indicators=indicators,
        )

    # ---- Apply pre-gate size multiplier + vol-targeting ------------------
    if decision["decision"] in ("LONG", "SHORT"):
        atr_pct = primary.get("atr_pct")
        vol_mult, vol_reason = _vol_targeted_multiplier(p, atr_pct)
        final_mult = size_mult * vol_mult
        if abs(final_mult - 1.0) > 1e-6:
            # Scale the sizing fields; keep entry/SL/TP/RR identical (risk dollars change).
            for k in ("position_size_base", "position_notional_usd", "risk_usd"):
                v = decision.get(k)
                if v is not None:
                    decision[k] = v * final_mult
            decision.setdefault("chart_observations", []).append(
                f"size×{final_mult:.2f} (gate={gate_reason}; {vol_reason})"
            )
        decision["sizing_multiplier"] = final_mult
        decision["gate_reason"] = gate_reason

        log.info(
            "rule_engine.setup",
            symbol=symbol, market=market.value, term=term.value,
            bias=effective_bias, direction=decision["decision"],
            confidence=decision["confidence_level"],
            rr=round(decision["reward_risk_ratio"], 2),
            size_mult=round(final_mult, 2),
        )
    return decision


__all__ = ["generate_rule_decision", "params_for", "TermParams"]
