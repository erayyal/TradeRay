"""Static market calendars: TCMB MPC (BIST), FOMC (US), CPI prints.

These dates anchor the rule-engine gate that vetoes entries inside high-
impact macro-announcement windows. The schedule is published a year in
advance by the issuing institution, so a hand-maintained static table is
the right tradeoff (no extra API dependency, no live failure mode).

Update annually:
  - TCMB PPK: https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB+EN/main+menu/announcements/press+releases/calendar
  - FOMC:     https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

Times stored in UTC for unambiguous comparison.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TR_TZ = ZoneInfo("Europe/Istanbul")
US_EAST = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# TCMB Para Politikası Kurulu (PPK) — official 2026 schedule.
# Decisions are announced at 14:00 TR time on the listed day.
# The rule engine vetoes BIST entries inside [13:00, 17:00] TR around each
# date (announcement run-up + immediate volatility absorption).
# ---------------------------------------------------------------------------

TCMB_MPC_2026: list[str] = [
    "2026-01-22",
    "2026-02-26",
    "2026-04-09",
    "2026-04-30",
    "2026-06-04",
    "2026-07-23",
    "2026-09-10",
    "2026-10-29",
    "2026-12-10",
]

# ---------------------------------------------------------------------------
# FOMC (Federal Open Market Committee) — official 2026 schedule.
# Decision announcement: 14:00 ET (Wednesday); press conference 14:30 ET.
# Rule engine vetoes US entries from FOMC day open through next day open.
# ---------------------------------------------------------------------------

FOMC_2026: list[str] = [
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
]


def _str_to_date_tr(date_str: str) -> datetime:
    """Parse YYYY-MM-DD as a Europe/Istanbul midnight."""
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d, tzinfo=TR_TZ)


def _str_to_date_us(date_str: str) -> datetime:
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d, tzinfo=US_EAST)


def in_tcmb_blackout(now_utc: datetime | None = None) -> bool:
    """True iff `now_utc` falls inside a TCMB PPK decision window
    (13:00 TR → 17:00 TR on a listed date).

    Pre-window: 13:00 TR (1h before the 14:00 announcement) — market
    starts front-running.
    Post-window: 17:00 TR (3h after) — immediate vol typically absorbed.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    now_tr = now_utc.astimezone(TR_TZ)
    today_str = now_tr.strftime("%Y-%m-%d")
    if today_str not in TCMB_MPC_2026:
        return False
    return time(13, 0) <= now_tr.time() < time(17, 0)


def in_fomc_blackout(now_utc: datetime | None = None) -> bool:
    """True iff `now_utc` falls inside a FOMC window.

    Window: 14:00 ET on FOMC day → 16:00 ET next trading day.
    Captures the announcement, press conference, AND post-FOMC drift the
    NY Fed's own working papers (Lucca-Moench 2015) documented.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    now_et = now_utc.astimezone(US_EAST)
    # Check today
    today_str = now_et.strftime("%Y-%m-%d")
    if today_str in FOMC_2026 and now_et.time() >= time(14, 0):
        return True
    # Check yesterday — drift window through 16:00 ET next day
    yest = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
    if yest in FOMC_2026 and now_et.time() < time(16, 0):
        return True
    return False


__all__ = [
    "TCMB_MPC_2026",
    "FOMC_2026",
    "in_tcmb_blackout",
    "in_fomc_blackout",
]
