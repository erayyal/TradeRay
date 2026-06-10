"""Hermetic tests for orchestrator.apply_ai_guardrails — verifier discipline."""
from __future__ import annotations

import pytest

from agents.orchestrator import apply_ai_guardrails


def _rule(direction="LONG", risk=200.0):
    return {
        "decision": direction,
        "entry_price": 100.0,
        "take_profit": 110.0,
        "stop_loss": 95.0,
        "risk_usd": risk,
        "confidence_level": 75,
    }


def _master(direction="LONG", conf=80, risk=200.0):
    return {
        "decision": direction,
        "confidence_level": conf,
        "entry_price": 100.0,
        "take_profit": 110.0,
        "stop_loss": 95.0,
        "risk_usd": risk,
        "position_size_base": 40.0,
        "position_notional_usd": 4000.0,
        "leverage": 2,
        "justification": "looks good",
    }


class TestConfidenceFloor:
    def test_below_floor_becomes_wait(self):
        out = apply_ai_guardrails(_rule(), _master(conf=50), min_confidence=65)
        assert out["decision"] == "WAIT"
        assert out["entry_price"] is None
        assert out["risk_usd"] is None
        assert any("confidence" in f for f in out["conflict_flags"])

    def test_at_floor_passes(self):
        out = apply_ai_guardrails(_rule(), _master(conf=65), min_confidence=65)
        assert out["decision"] == "LONG"


class TestDirectionFlip:
    def test_opposite_direction_becomes_wait(self):
        out = apply_ai_guardrails(_rule("LONG"), _master("SHORT", conf=90))
        assert out["decision"] == "WAIT"
        assert any("direction_flip" in f for f in out["conflict_flags"])

    def test_same_direction_passes(self):
        out = apply_ai_guardrails(_rule("SHORT"), _master("SHORT", conf=90))
        assert out["decision"] == "SHORT"


class TestRiskClamp:
    def test_ai_cannot_increase_risk(self):
        out = apply_ai_guardrails(_rule(risk=200.0), _master(conf=90, risk=400.0))
        assert out["decision"] == "LONG"
        assert out["risk_usd"] == pytest.approx(200.0)
        # Sizing scaled by the same factor (0.5)
        assert out["position_size_base"] == pytest.approx(20.0)
        assert out["position_notional_usd"] == pytest.approx(2000.0)
        assert "guardrail_risk_clamped" in out["conflict_flags"]

    def test_ai_may_reduce_risk(self):
        out = apply_ai_guardrails(_rule(risk=200.0), _master(conf=90, risk=120.0))
        assert out["risk_usd"] == pytest.approx(120.0)
        assert "guardrail_risk_clamped" not in out.get("conflict_flags", [])


class TestPassthrough:
    def test_wait_untouched(self):
        m = {"decision": "WAIT", "confidence_level": 90, "justification": "no edge"}
        out = apply_ai_guardrails(_rule(), dict(m))
        assert out["decision"] == "WAIT"
        assert out["justification"] == "no edge"

    def test_cancel_pending_untouched(self):
        m = {"decision": "CANCEL_PENDING", "confidence_level": 30}
        out = apply_ai_guardrails(_rule(), dict(m))
        assert out["decision"] == "CANCEL_PENDING"
