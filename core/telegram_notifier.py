"""Telegram Bot API notifier for TradeRay alerts.

Async, never-raising, MarkdownV2-aware. Configured via env:
    TELEGRAM_BOT_TOKEN  — from @BotFather
    TELEGRAM_CHAT_ID    — your chat (find via @userinfobot OR by sending a
                          message to the bot then GET /getUpdates)

If either env var is missing, every notify_* helper silently no-ops so the
backend runs identically without Telegram configured. A misbehaving Telegram
endpoint MUST NOT block trading or scheduler ticks: every send call is
bounded by a 10-second `aiohttp.ClientTimeout`, errors are caught and
logged, never re-raised.

MarkdownV2 escaping (per the official Bot API spec):
    Free text   : escape every  _ * [ ] ( ) ~ ` > # + - = | { } . ! \\
    Inside `…`  : escape only   ` \\
    Inside *…*  : escape every  *
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

import aiohttp

from core.logger import get_logger

log = get_logger(__name__)

_TG_API = "https://api.telegram.org"

# Reserved chars in MarkdownV2 free text — backslash-escape every match.
_MD2_RESERVED = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md2(text: str) -> str:
    """Escape every MarkdownV2-reserved character in `text`."""
    return _MD2_RESERVED.sub(r"\\\1", str(text))


def _code(value: Any) -> str:
    """Wrap `value` in a MarkdownV2 code span.

    Code spans only need ` and \\ escaped; numeric / financial content is
    safe as-is. We render the value with str() to keep call sites simple.
    """
    s = str(value).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{s}`"


# ---------------------------------------------------------------------------
# Configuration (read from env at import time)
# ---------------------------------------------------------------------------

_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def is_configured() -> bool:
    """True iff both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    return bool(_BOT_TOKEN and _CHAT_ID)


# ---------------------------------------------------------------------------
# Core sender (idempotent, never raises)
# ---------------------------------------------------------------------------

async def send_message(
    text: str,
    *,
    parse_mode: str = "MarkdownV2",
    disable_notification: bool = False,
) -> bool:
    """POST sendMessage. Returns True on 200 OK, False on any failure.

    Wall-clock cap: 10 seconds. Failures are logged at WARNING and swallowed.
    """
    if not is_configured():
        return False

    url = f"{_TG_API}/bot{_BOT_TOKEN}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": _CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if disable_notification:
        payload["disable_notification"] = True

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    log.warning(
                        "telegram.api_error",
                        status=resp.status, body=body,
                    )
                    return False
                return True
    except asyncio.TimeoutError:
        log.warning("telegram.timeout")
        return False
    except Exception as e:
        log.warning("telegram.send_failed", err=str(e))
        return False


def fire(coro) -> asyncio.Task | None:
    """Schedule a coroutine on the running loop without blocking the caller.

    Use from hot paths (order placement, tracker reconciliation) where we
    must NOT add ~10s of Telegram latency to a trading flow. Returns the
    Task on success, None when there's no running loop (drops silently).

    The notifier helpers themselves swallow all errors, so an orphan task
    cannot raise an unhandled exception in the loop.
    """
    if not is_configured():
        # Cleanly close the un-awaited coroutine so Python doesn't warn.
        coro.close()
        return None
    try:
        loop = asyncio.get_running_loop()
        return loop.create_task(coro)
    except RuntimeError:
        coro.close()
        return None


# ---------------------------------------------------------------------------
# Numeric / amount formatting
# ---------------------------------------------------------------------------

def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 10:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def _fmt_signed_usd(value: float) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


# ---------------------------------------------------------------------------
# Pre-formatted notifiers (one per event type)
# ---------------------------------------------------------------------------

async def notify_crypto_trade_placed(
    *, side: str, symbol: str, entry: float, risk_usd: float
) -> None:
    """🟢 [CRYPTO] AUTO-BOT: LONG BTCUSDT @ 64500 (Risk: $20)"""
    text = (
        f"🟢 *\\[CRYPTO\\] AUTO\\-BOT: {escape_md2(side)} {escape_md2(symbol)}*\n"
        f"Entry: {_code(_fmt_price(entry))}\n"
        f"Risk: {_code(_fmt_usd(risk_usd))}"
    )
    await send_message(text)


async def notify_signal_logged(
    *, market: str, side: str, symbol: str,
    entry: float | None, take_profit: float | None, stop_loss: float | None,
) -> None:
    """📡 [BIST] SIGNAL: LONG THYAO.IS @ 250 TL (Target: 260)"""
    lines = [
        f"📡 *\\[{escape_md2(market)}\\] SIGNAL: {escape_md2(side)} {escape_md2(symbol)}*",
        f"Entry: {_code(_fmt_price(entry))}",
    ]
    if take_profit is not None:
        lines.append(f"Target: {_code(_fmt_price(take_profit))}")
    if stop_loss is not None:
        lines.append(f"Stop: {_code(_fmt_price(stop_loss))}")
    await send_message("\n".join(lines))


async def notify_trade_closed(
    *, symbol: str, outcome: str, pnl_usd: float
) -> None:
    """🏁 [CRYPTO] CLOSED: BTCUSDT hit Take Profit! (Realized: +$45.50)"""
    label = "Take Profit ✅" if outcome == "TP" else "Stop Loss ❌"
    text = (
        f"🏁 *\\[CRYPTO\\] CLOSED: {escape_md2(symbol)} hit {escape_md2(label)}*\n"
        f"Realized: {_code(_fmt_signed_usd(pnl_usd))}"
    )
    await send_message(text)


_OUTCOME_LABELS: dict[str, str] = {
    "TP": "Take Profit ✅",
    "SL": "Stop Loss ❌",
    "BE": "Breakeven Stop ⚖️",
    "TIME": "Time Exit ⏱",
}


async def notify_signal_resolved(
    *, market: str, symbol: str, outcome: str, pnl_usd: float
) -> None:
    """🏁 [BIST] RESOLVED: THYAO.IS hit Stop Loss (Theoretical: -$15.00)"""
    label = _OUTCOME_LABELS.get(outcome, outcome)
    text = (
        f"🏁 *\\[{escape_md2(market)}\\] RESOLVED: {escape_md2(symbol)} hit {escape_md2(label)}*\n"
        f"Theoretical: {_code(_fmt_signed_usd(pnl_usd))}"
    )
    await send_message(text)


async def notify_order_canceled(*, symbol: str, reason: str) -> None:
    """⚠️ [CRYPTO] CANCELED: Pending BTCUSDT order invalidated."""
    text = (
        f"⚠️ *\\[CRYPTO\\] CANCELED: Pending {escape_md2(symbol)} order*\n"
        f"Reason: {escape_md2(reason)}"
    )
    await send_message(text)


async def notify_chandelier_tightened(
    *, symbol: str, side: str, old_sl: float, new_sl: float, atr: float,
) -> None:
    """🔧 [CRYPTO] TRAIL: BTCUSDT LONG SL 100.0 → 105.0 (ATR=2.1).

    Fires when the Chandelier scheduler job ratchets a stop closer to
    current price. Useful so the user knows their position is auto-locking
    profit without having to refresh the dashboard.
    """
    arrow = "↑" if side == "LONG" else "↓"
    text = (
        f"🔧 *\\[CRYPTO\\] TRAIL: {escape_md2(symbol)} {escape_md2(side)}*\n"
        f"SL {_code(f'{old_sl:.4f}')} {arrow} {_code(f'{new_sl:.4f}')} "
        f"\\(ATR\\={_code(f'{atr:.4f}')}\\)"
    )
    await send_message(text)


async def notify_cost_budget_alert(
    *, daily_usd: float, budget_usd: float, top_agent: str | None = None
) -> None:
    """💸 LLM cost budget exceeded. Sent at most once per day.

    Fires when the running daily LLM spend crosses the configured budget.
    The orchestrator-side guard ensures we don't spam — see
    `_check_cost_budget` in scheduler/jobs.py.
    """
    pct = (daily_usd / budget_usd * 100.0) if budget_usd else 0.0
    extra = f"\nTop agent today: {_code(escape_md2(top_agent))}" if top_agent else ""
    text = (
        f"💸 *LLM Cost Budget Exceeded*\n"
        f"Today: {_code(_fmt_signed_usd(daily_usd))} "
        f"\\(_{pct:.0f}% of {_code(_fmt_signed_usd(budget_usd))}_\\)"
        f"{extra}\n"
        f"_AI verification will keep running until you toggle markets off_"
    )
    await send_message(text)


async def notify_daily_digest(
    *,
    date_str: str,
    crypto_pnl: float,
    crypto_wins: int,
    crypto_losses: int,
    crypto_n_trades: int,
    signal_summary: dict[str, dict[str, Any]],
) -> None:
    """📊 Daily TradeRay Report — performance summary across all markets."""
    lines = [
        "📊 *Daily TradeRay Report*",
        f"_{escape_md2(date_str)} \\(UTC\\)_",
        "",
        "🪙 *Crypto Auto\\-Bot*",
        f"  PnL: {_code(_fmt_signed_usd(crypto_pnl))}",
        f"  Trades: {_code(crypto_n_trades)} \\({crypto_wins}W \\| {crypto_losses}L\\)",
        "",
    ]
    if signal_summary:
        lines.append("📡 *Signals \\(theoretical, resolved today\\)*")
        for market, stats in signal_summary.items():
            n = int(stats.get("n", 0) or 0)
            pnl = float(stats.get("pnl", 0.0) or 0.0)
            w = int(stats.get("wins", 0) or 0)
            l = int(stats.get("losses", 0) or 0)
            lines.append(
                f"  {escape_md2(market)}: {_code(n)} resolved, "
                f"{_code(_fmt_signed_usd(pnl))} \\({w}W \\| {l}L\\)"
            )
    else:
        lines.append("📡 _No signal resolutions today\\._")

    await send_message("\n".join(lines))


__all__ = [
    "send_message",
    "fire",
    "is_configured",
    "escape_md2",
    "notify_crypto_trade_placed",
    "notify_signal_logged",
    "notify_trade_closed",
    "notify_signal_resolved",
    "notify_order_canceled",
    "notify_daily_digest",
    "notify_cost_budget_alert",
    "notify_chandelier_tightened",
]
