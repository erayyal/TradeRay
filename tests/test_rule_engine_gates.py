"""Rule-engine pre-gate tests — VIX, FOMC, TCMB, earnings, USDTRY, funding.

These tests exercise `_evaluate_gates` and the funding-bias-flip path
hermetically: no network, no DB. We synthesize macro_lite payloads + monkey-
patch the calendar blackout helpers where date-sensitive behaviour matters.
"""
from __future__ import annotations

from agents.rule_engine import (
    BIST_PARAMS,
    CRYPTO_PARAMS,
    EQUITY_US_PARAMS,
    _evaluate_gates,
    _vol_targeted_multiplier,
)
from models import MarketType, Term


# ---------------------------------------------------------------------------
# VIX gate (US — Whaley 2000/2009)
# ---------------------------------------------------------------------------

def test_vix_calm_passes_full_size():
    allow, mult, _ = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={"vix": 15.0},
        next_earnings_iso=None,
    )
    assert allow is True and mult == 1.0


def test_vix_elevated_halves_size():
    allow, mult, reason = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={"vix": 27.0},
        next_earnings_iso=None,
    )
    assert allow is True and mult == 0.5
    assert "VIX" in reason


def test_vix_crisis_vetoes():
    allow, mult, _ = _evaluate_gates(
        market=MarketType.NASDAQ,
        macro_lite={"vix": 38.0},
        next_earnings_iso=None,
    )
    assert allow is False and mult == 0.0


def test_vix_threshold_inclusive():
    """≥35 must veto (not strictly >35) — Whaley uses inclusive bands."""
    allow, *_ = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={"vix": 35.0},
        next_earnings_iso=None,
    )
    assert allow is False


def test_vix_gate_skipped_for_crypto():
    """VIX is a US-equity-vol thing; doesn't gate crypto."""
    allow, mult, _ = _evaluate_gates(
        market=MarketType.CRYPTO,
        macro_lite={"vix": 99.0},  # would veto US — irrelevant here
        next_earnings_iso=None,
    )
    assert allow is True and mult == 1.0


def test_vix_missing_silent_passthrough():
    """When FRED is dark we shouldn't accidentally veto US entries."""
    allow, mult, _ = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={},
        next_earnings_iso=None,
    )
    assert allow is True and mult == 1.0


# ---------------------------------------------------------------------------
# Earnings blackout (US — Bernard-Thomas 1989 PEAD)
# ---------------------------------------------------------------------------

def test_earnings_blackout_vetoes(monkeypatch):
    """Within ±1d of an earnings date → veto."""
    monkeypatch.setattr(
        "agents.rule_engine.is_in_earnings_blackout",
        lambda iso: True,
    )
    allow, mult, reason = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={"vix": 15.0},
        next_earnings_iso="2026-05-17",
    )
    assert allow is False and mult == 0.0
    assert "earnings" in reason.lower()


def test_earnings_clear_passes(monkeypatch):
    monkeypatch.setattr(
        "agents.rule_engine.is_in_earnings_blackout",
        lambda iso: False,
    )
    allow, mult, _ = _evaluate_gates(
        market=MarketType.SP500,
        macro_lite={"vix": 15.0},
        next_earnings_iso="2026-12-01",
    )
    assert allow is True and mult == 1.0


# ---------------------------------------------------------------------------
# FOMC blackout (US — Lucca-Moench 2015)
# ---------------------------------------------------------------------------

def test_fomc_blackout_vetoes(monkeypatch):
    monkeypatch.setattr("agents.rule_engine.in_fomc_blackout", lambda: True)
    allow, mult, reason = _evaluate_gates(
        market=MarketType.NASDAQ,
        macro_lite={"vix": 15.0},
        next_earnings_iso=None,
    )
    assert allow is False and mult == 0.0
    assert "FOMC" in reason


# ---------------------------------------------------------------------------
# TCMB blackout (BIST)
# ---------------------------------------------------------------------------

def test_tcmb_blackout_vetoes(monkeypatch):
    monkeypatch.setattr("agents.rule_engine.in_tcmb_blackout", lambda: True)
    allow, mult, reason = _evaluate_gates(
        market=MarketType.BIST,
        macro_lite={},
        next_earnings_iso=None,
    )
    assert allow is False and mult == 0.0
    assert "TCMB" in reason


def test_usdtry_hard_move_halves(monkeypatch):
    """|Δ%|≥2% intra-day → soft halver, NOT a veto."""
    monkeypatch.setattr("agents.rule_engine.in_tcmb_blackout", lambda: False)
    allow, mult, reason = _evaluate_gates(
        market=MarketType.BIST,
        macro_lite={"usdtry": {"pct_change_1d": 0.025}},
        next_earnings_iso=None,
    )
    assert allow is True and mult == 0.5
    assert "USDTRY" in reason


def test_usdtry_normal_move_passes(monkeypatch):
    monkeypatch.setattr("agents.rule_engine.in_tcmb_blackout", lambda: False)
    allow, mult, _ = _evaluate_gates(
        market=MarketType.BIST,
        macro_lite={"usdtry": {"pct_change_1d": 0.005}},
        next_earnings_iso=None,
    )
    assert allow is True and mult == 1.0


# ---------------------------------------------------------------------------
# Vol-targeting multiplier (AQR/Harvey 2018)
# ---------------------------------------------------------------------------

def test_vol_target_disabled_returns_one():
    p = CRYPTO_PARAMS[Term.SCALP]
    mult, reason = _vol_targeted_multiplier(p, atr_pct=0.02)
    if p.vol_target_annual is None:
        assert mult == 1.0
        assert "disabled" in reason


def test_vol_target_high_vol_shrinks_size():
    """High realized vol vs target → size < 1 (clamped at 0.5)."""
    import dataclasses
    p_high_vol_asset = dataclasses.replace(
        CRYPTO_PARAMS[Term.MID_TERM],
        vol_target_annual=0.20,    # target 20% annualized
        periods_per_year=365,
    )
    # ATR_pct=0.05 daily → realized ≈ 0.05 * sqrt(365) ≈ 0.955 (95% ann.)
    mult, reason = _vol_targeted_multiplier(p_high_vol_asset, atr_pct=0.05)
    assert mult == 0.5
    assert "vol_target" in reason


def test_vol_target_low_vol_grows_size():
    """Low realized vol vs target → size > 1 (clamped at 1.5)."""
    import dataclasses
    p = dataclasses.replace(
        EQUITY_US_PARAMS[Term.MID_TERM],
        vol_target_annual=0.20,
        periods_per_year=252,
    )
    # ATR_pct=0.005 daily → realized ≈ 0.005 * sqrt(252) ≈ 0.079 (7.9% ann.)
    mult, reason = _vol_targeted_multiplier(p, atr_pct=0.005)
    assert mult == 1.5
    assert "vol_target" in reason


def test_vol_target_degenerate_atr_returns_one():
    import dataclasses
    p = dataclasses.replace(
        CRYPTO_PARAMS[Term.SCALP], vol_target_annual=0.25
    )
    mult, _ = _vol_targeted_multiplier(p, atr_pct=None)
    assert mult == 1.0
    mult, _ = _vol_targeted_multiplier(p, atr_pct=0.0)
    assert mult == 1.0


# ---------------------------------------------------------------------------
# Parameter matrix integrity
# ---------------------------------------------------------------------------

def test_every_market_term_has_params():
    """All 4 markets × 3 terms must be defined — orchestrator depends on it."""
    for market_params in (CRYPTO_PARAMS, EQUITY_US_PARAMS, BIST_PARAMS):
        for term in Term:
            assert term in market_params, f"missing {term} in {market_params}"


def test_bist_risk_pct_lower_than_crypto():
    """BIST 1.5% per trade vs CRYPTO 2% — gap-risk premium."""
    for term in Term:
        bist = BIST_PARAMS[term].risk_pct
        crypto = CRYPTO_PARAMS[term].risk_pct
        assert bist <= crypto
