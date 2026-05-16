"""Chandelier trailing-exit ratchet logic — pure unit tests.

We can't unit-test the full `_trail_one_trade` (it hits Binance + DB), but we
CAN test the ratchet decision: given an anchor + ATR + side + current SL,
does the function correctly (a) compute the chandelier level, (b) refuse to
loosen the stop, (c) clip against last_close so we don't get instant-stopped.

For that we lift the ratchet math out as a pure helper that mirrors what
`_trail_one_trade` does internally.
"""
from __future__ import annotations

CHAND_MULT = 3.0


def chandelier_level(side_long: bool, anchor: float, atr: float) -> float:
    return (anchor - CHAND_MULT * atr) if side_long else (anchor + CHAND_MULT * atr)


def should_tighten(
    side_long: bool, current_sl: float, anchor: float, atr: float, last_close: float,
) -> tuple[bool, float | None]:
    """Returns (should_replace, new_sl_or_None). Mirrors `_trail_one_trade`."""
    new_sl = chandelier_level(side_long, anchor, atr)
    if side_long:
        new_sl = min(new_sl, last_close * 0.999)
        if new_sl <= current_sl:
            return False, None
    else:
        new_sl = max(new_sl, last_close * 1.001)
        if new_sl >= current_sl:
            return False, None
    return True, new_sl


# --- Ratchet semantics -----------------------------------------------------

def test_long_tightens_on_higher_anchor():
    # Entry 100, SL 95, price moved to 120 → chandelier = 120 - 3*1 = 117
    ok, new_sl = should_tighten(
        side_long=True, current_sl=95.0, anchor=120.0, atr=1.0, last_close=119.5,
    )
    assert ok is True and 95 < new_sl <= 119.5


def test_long_refuses_to_loosen():
    # Anchor still 105, ATR 2.0 → chandelier = 99. Current SL is 100, better.
    ok, new_sl = should_tighten(
        side_long=True, current_sl=100.0, anchor=105.0, atr=2.0, last_close=104.0,
    )
    assert ok is False and new_sl is None


def test_short_tightens_on_lower_anchor():
    # Short entry 100, SL 105, price moved down to 80 → chandelier = 80 + 3 = 83
    ok, new_sl = should_tighten(
        side_long=False, current_sl=105.0, anchor=80.0, atr=1.0, last_close=80.5,
    )
    assert ok is True and 80.5 < new_sl < 105


def test_short_refuses_to_loosen():
    ok, _ = should_tighten(
        side_long=False, current_sl=95.0, anchor=90.0, atr=2.0, last_close=91.0,
    )
    assert ok is False


# --- Last-close clamp (never set SL where it instant-stops) ----------------

def test_long_chandelier_clipped_below_current_price():
    """If chandelier math would put SL above current price, we clip to 0.999×close."""
    # anchor 100, atr 0.1 → raw chandelier = 99.7. Last close 99 → 99 * 0.999 = 98.901
    ok, new_sl = should_tighten(
        side_long=True, current_sl=90.0, anchor=100.0, atr=0.1, last_close=99.0,
    )
    assert ok is True
    assert new_sl <= 99.0 * 0.999 + 1e-9


def test_short_chandelier_clipped_above_current_price():
    ok, new_sl = should_tighten(
        side_long=False, current_sl=110.0, anchor=100.0, atr=0.1, last_close=101.0,
    )
    assert ok is True
    assert new_sl >= 101.0 * 1.001 - 1e-9
