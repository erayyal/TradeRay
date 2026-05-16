"""TCMB + FOMC blackout window tests.

These cover the time-zone arithmetic — easy to break, hard to spot.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from config.calendars import (
    FOMC_2026,
    TCMB_MPC_2026,
    in_fomc_blackout,
    in_tcmb_blackout,
)

TR = ZoneInfo("Europe/Istanbul")
ET = ZoneInfo("America/New_York")


def _utc_at(tz_name: str, y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    tz = ZoneInfo(tz_name)
    return datetime(y, m, d, hh, mm, tzinfo=tz).astimezone(timezone.utc)


# --- TCMB ------------------------------------------------------------------

def test_tcmb_inside_window_blocked():
    # 2026-01-22 is a TCMB MPC day. 14:00 TR = announcement.
    now = _utc_at("Europe/Istanbul", 2026, 1, 22, 14, 0)
    assert in_tcmb_blackout(now_utc=now) is True


def test_tcmb_before_window_clear():
    # 12:00 TR = before 13:00 pre-window
    now = _utc_at("Europe/Istanbul", 2026, 1, 22, 12, 30)
    assert in_tcmb_blackout(now_utc=now) is False


def test_tcmb_after_window_clear():
    # 17:30 TR = post-window
    now = _utc_at("Europe/Istanbul", 2026, 1, 22, 17, 30)
    assert in_tcmb_blackout(now_utc=now) is False


def test_tcmb_non_mpc_day_always_clear():
    now = _utc_at("Europe/Istanbul", 2026, 1, 21, 14, 0)
    assert in_tcmb_blackout(now_utc=now) is False


def test_tcmb_2026_calendar_complete():
    """Quick sanity check that someone didn't accidentally delete dates."""
    assert len(TCMB_MPC_2026) >= 8


# --- FOMC ------------------------------------------------------------------

def test_fomc_announcement_window_blocked():
    # 2026-01-28 is a FOMC day. 14:30 ET = press conference.
    now = _utc_at("America/New_York", 2026, 1, 28, 14, 30)
    assert in_fomc_blackout(now_utc=now) is True


def test_fomc_morning_clear():
    """Before 14:00 ET on FOMC day — pre-announcement drift period."""
    now = _utc_at("America/New_York", 2026, 1, 28, 10, 0)
    assert in_fomc_blackout(now_utc=now) is False


def test_fomc_next_day_drift_window_blocked():
    """Lucca-Moench: drift extends to ~16:00 ET next day."""
    now = _utc_at("America/New_York", 2026, 1, 29, 15, 0)
    assert in_fomc_blackout(now_utc=now) is True


def test_fomc_next_day_after_close_clear():
    """Past 16:00 ET next day — drift has cleared."""
    now = _utc_at("America/New_York", 2026, 1, 29, 17, 0)
    assert in_fomc_blackout(now_utc=now) is False


def test_fomc_2026_calendar_complete():
    assert len(FOMC_2026) == 8
