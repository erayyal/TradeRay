"""Hermetic tests for execution/portfolio_guard.py — pure gate logic only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from execution.portfolio_guard import (
    PortfolioSnapshot,
    evaluate_portfolio_gates,
)
from models import MarketType, Term

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _gate(snapshot: PortfolioSnapshot, **kw):
    defaults = dict(
        market=MarketType.CRYPTO, term=Term.SHORT_TERM,
        direction="LONG", now=NOW,
    )
    defaults.update(kw)
    return evaluate_portfolio_gates(snapshot, **defaults)


class TestDailyLossLimit:
    def test_clean_day_allows(self):
        allow, reason = _gate(PortfolioSnapshot())
        assert allow, reason

    def test_breach_blocks(self):
        limit = settings.portfolio_notional * settings.daily_loss_limit_pct
        snap = PortfolioSnapshot(today_realized_pnl_usd=-(limit + 1))
        allow, reason = _gate(snap)
        assert not allow
        assert "daily_loss_limit" in reason

    def test_combined_realized_and_theoretical(self):
        limit = settings.portfolio_notional * settings.daily_loss_limit_pct
        snap = PortfolioSnapshot(
            today_realized_pnl_usd=-(limit * 0.6),
            today_theoretical_pnl_usd=-(limit * 0.6),
        )
        allow, _ = _gate(snap)
        assert not allow

    def test_just_under_limit_allows(self):
        limit = settings.portfolio_notional * settings.daily_loss_limit_pct
        snap = PortfolioSnapshot(today_realized_pnl_usd=-(limit * 0.9))
        allow, _ = _gate(snap)
        assert allow


class TestSlCooldown:
    def test_recent_same_direction_sl_blocks(self):
        snap = PortfolioSnapshot(last_sl=(NOW - timedelta(hours=2), "LONG"))
        allow, reason = _gate(snap, term=Term.SHORT_TERM, direction="LONG")
        assert not allow
        assert "sl_cooldown" in reason

    def test_opposite_direction_allowed(self):
        snap = PortfolioSnapshot(last_sl=(NOW - timedelta(hours=2), "SHORT"))
        allow, _ = _gate(snap, direction="LONG")
        assert allow

    def test_expired_cooldown_allows(self):
        snap = PortfolioSnapshot(last_sl=(NOW - timedelta(hours=30), "LONG"))
        allow, _ = _gate(snap, term=Term.SHORT_TERM, direction="LONG")
        assert allow

    def test_scalp_window_shorter_than_midterm(self):
        sl_at = NOW - timedelta(hours=6)
        scalp = _gate(
            PortfolioSnapshot(last_sl=(sl_at, "LONG")),
            term=Term.SCALP, direction="LONG",
        )
        mid = _gate(
            PortfolioSnapshot(last_sl=(sl_at, "LONG")),
            term=Term.MID_TERM, direction="LONG",
        )
        assert scalp[0] is True       # 4h window expired
        assert mid[0] is False        # 3d window still active


class TestConcurrencyCaps:
    def test_market_cap_blocks(self):
        snap = PortfolioSnapshot(
            open_signals_by_market={"CRYPTO": settings.max_open_per_market},
        )
        allow, reason = _gate(snap, market=MarketType.CRYPTO)
        assert not allow
        assert "max_open_per_market" in reason

    def test_other_market_not_affected(self):
        snap = PortfolioSnapshot(
            open_signals_by_market={"CRYPTO": settings.max_open_per_market},
        )
        allow, _ = _gate(snap, market=MarketType.SP500)
        assert allow

    def test_trades_count_toward_cap(self):
        snap = PortfolioSnapshot(
            open_signals_by_market={"CRYPTO": settings.max_open_per_market - 1},
            open_trades_by_market={"CRYPTO": 1},
        )
        allow, _ = _gate(snap, market=MarketType.CRYPTO)
        assert not allow

    def test_global_cap_blocks(self):
        per = settings.max_open_per_market
        markets = ["CRYPTO", "SP500", "NASDAQ", "BIST"]
        counts = {}
        remaining = settings.max_open_total
        for m in markets:
            take = min(per - 1, remaining)
            counts[m] = take
            remaining -= take
        snap = PortfolioSnapshot(open_signals_by_market=counts)
        if sum(counts.values()) >= settings.max_open_total:
            allow, reason = _gate(snap, market=MarketType.CRYPTO)
            assert not allow
            assert "max_open_total" in reason
        else:
            pytest.skip("config can't reach global cap with per-market caps")
