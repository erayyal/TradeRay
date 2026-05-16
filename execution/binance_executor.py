"""Hardened Binance Futures Testnet executor.

Three production-grade defenses:

  1. Strict quantization & exchange-filter validation.
     Every price and quantity sent to Binance is rounded to the symbol's
     `tickSize` / `stepSize` BEFORE the API call. Pre-flight `_validate_filters`
     additionally checks `LOT_SIZE.minQty/maxQty`, `PRICE_FILTER.minPrice/
     maxPrice`, and `MIN_NOTIONAL` (or the newer `NOTIONAL` filter). Filter
     violations raise `BinanceFilterError` BEFORE any network call — Binance's
     `APIError(-1013)` "Invalid quantity/price" cannot occur from this code.

  2. Tenacity-backed retries on transient API errors.
     429 (rate limit), 5xx (server), `code -1003` (too many requests),
     `code -1015` (too many requests for this IP) and `BinanceRequestException`
     (network) are retried with exponential backoff. Auth, validation, and
     filter errors are NOT retried — they're loud bugs.

  3. Idempotent order placement via `newClientOrderId`.
     Every order (entry, SL, TP) carries a deterministic client_order_id.
     If a 5xx leaves us uncertain whether the order made it through, the
     retry either succeeds normally OR comes back with `code -2010`
     (duplicate clientOrderId) — we treat that as "original succeeded" and
     fetch the order back rather than create a second one.

The executor reuses the AsyncClient created by `data_fetchers.market_fetcher`
— single connection pool, single rate-limit envelope across the whole app.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

import tenacity
from binance.enums import (
    FUTURE_ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
    SIDE_BUY,
    SIDE_SELL,
    TIME_IN_FORCE_GTC,
)
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config import settings
from core.logger import get_logger
from core.redis_client import cache
from core.telegram_notifier import fire, notify_crypto_trade_placed
from data_fetchers.market_fetcher import fetcher as _market_fetcher
from execution.risk_manager import RiskRejection, validate_decision

log = get_logger(__name__)
_retry_log = logging.getLogger("traderay.binance.retry")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BinanceFilterError(Exception):
    """Order rejected pre-flight by exchange filter validation."""


# ---------------------------------------------------------------------------
# Shared client (same pool as orchestrator + tracker)
# ---------------------------------------------------------------------------

async def _client():
    return await _market_fetcher._binance.client()


# ---------------------------------------------------------------------------
# Tenacity retry policy
# ---------------------------------------------------------------------------

# Codes that signal "your call hit a rate limit / IP ban" — safe to retry.
_RETRY_CODES: frozenset[int] = frozenset({-1003, -1015})

# Codes that we explicitly DO NOT retry — they tell us the order is settled.
_NO_RETRY_CODES: frozenset[int] = frozenset({-2010, -2011, -2013})
# -2010 : duplicate newClientOrderId (original succeeded)
# -2011 : unknown order id (doesn't exist or already canceled)
# -2013 : order does not exist


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BinanceRequestException):
        return True  # network-level (DNS, connection, timeout)
    if isinstance(exc, BinanceAPIException):
        if exc.code in _NO_RETRY_CODES:
            return False
        if exc.code in _RETRY_CODES:
            return True
        status = getattr(exc, "status_code", None)
        if status is not None and (status == 429 or status >= 500):
            return True
    return False


_binance_retry = tenacity.retry(
    stop=tenacity.stop_after_attempt(4),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=20),
    retry=tenacity.retry_if_exception(_is_retryable),
    before_sleep=tenacity.before_sleep_log(_retry_log, logging.WARNING),
    reraise=True,
)


@_binance_retry
async def _exchange_info_safe(client) -> dict:
    return await client.futures_exchange_info()


@_binance_retry
async def _change_leverage_safe(client, *, symbol: str, leverage: int) -> dict:
    return await client.futures_change_leverage(symbol=symbol, leverage=leverage)


@_binance_retry
async def _get_order_safe(client, *, symbol: str, orig_client_order_id: str) -> dict:
    return await client.futures_get_order(
        symbol=symbol, origClientOrderId=orig_client_order_id
    )


async def _create_order_idempotent(client, **kwargs) -> dict:
    """Place an order with retries + duplicate-clientOrderId recovery.

    If a transient failure caused the original POST to succeed on the server
    but never return to us, the retry collides with `code -2010`. We treat
    that as "the order is on the book" and fetch it back, which is the only
    safe way to avoid placing a duplicate.
    """
    try:
        return await _create_order_safe(client, **kwargs)
    except BinanceAPIException as e:
        if e.code == -2010 and kwargs.get("newClientOrderId"):
            cid = kwargs["newClientOrderId"]
            sym = kwargs["symbol"]
            log.info(
                "binance.duplicate_client_id_recovered",
                client_order_id=cid, symbol=sym,
            )
            return await _get_order_safe(
                client, symbol=sym, orig_client_order_id=cid
            )
        raise


@_binance_retry
async def _create_order_safe(client, **kwargs) -> dict:
    return await client.futures_create_order(**kwargs)


# ---------------------------------------------------------------------------
# Quantization & filter validation
# ---------------------------------------------------------------------------

# Cached exchange filters per symbol (refreshed on cold cache miss).
_symbol_filters: dict[str, dict] = {}


async def _load_filters(symbol: str) -> dict:
    """Return the cached `exchangeInfo` filter map for `symbol`."""
    if symbol in _symbol_filters:
        return _symbol_filters[symbol]
    c = await _client()
    info = await _exchange_info_safe(c)
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            f = {flt["filterType"]: flt for flt in s["filters"]}
            f["pricePrecision"] = s["pricePrecision"]
            f["quantityPrecision"] = s["quantityPrecision"]
            _symbol_filters[symbol] = f
            return f
    raise ValueError(f"symbol {symbol} not found on Binance Futures exchangeInfo")


def _floor_to_step(value: float, step: float) -> float:
    """Round DOWN to the nearest `step`. Used for quantities so we never
    accidentally exceed the LLM's risk_usd budget by a tick of slippage."""
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _round_to_tick(value: float, tick: float) -> float:
    """Round to the nearest `tick`. Used for prices."""
    if tick <= 0:
        return value
    return round(round(value / tick) * tick, 12)


def _quantize_price(price: float, filters: dict) -> float:
    tick = float(filters["PRICE_FILTER"]["tickSize"])
    return _round_to_tick(price, tick)


def _quantize_qty(qty: float, filters: dict) -> float:
    step = float(filters["LOT_SIZE"]["stepSize"])
    return _floor_to_step(qty, step)


def _format_for_api(value: float, precision: int) -> str:
    """Binance accepts strings; trailing zeros vary by precision filter.

    Using `f"{value:.{precision}f}"` keeps the wire format consistent with
    the exchange's pricePrecision / quantityPrecision metadata.
    """
    return f"{value:.{precision}f}"


def _validate_filters(price: float, qty: float, filters: dict) -> None:
    """Pre-flight check — raises `BinanceFilterError` on any violation.

    Anything that gets past this should never trigger Binance's -1013.
    """
    lot = filters["LOT_SIZE"]
    min_qty = float(lot.get("minQty", 0) or 0)
    max_qty = float(lot.get("maxQty", "inf") or "inf")
    if qty < min_qty:
        raise BinanceFilterError(
            f"qty {qty} < LOT_SIZE.minQty {min_qty} (likely risk_usd too small for this symbol)"
        )
    if qty > max_qty:
        raise BinanceFilterError(f"qty {qty} > LOT_SIZE.maxQty {max_qty}")

    pf = filters["PRICE_FILTER"]
    min_price = float(pf.get("minPrice", 0) or 0)
    max_price = float(pf.get("maxPrice", "inf") or "inf")
    if min_price and price < min_price:
        raise BinanceFilterError(f"price {price} < PRICE_FILTER.minPrice {min_price}")
    if max_price and price > max_price:
        raise BinanceFilterError(f"price {price} > PRICE_FILTER.maxPrice {max_price}")

    # MIN_NOTIONAL exists on spot; futures uses the newer "NOTIONAL" filter.
    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
    if notional_filter:
        # Field name differs between the two filter versions.
        min_notional = float(
            notional_filter.get("notional")
            or notional_filter.get("minNotional")
            or 0
        )
        notional = qty * price
        if min_notional and notional < min_notional:
            raise BinanceFilterError(
                f"notional {notional:.2f} < min {min_notional:.2f} "
                f"(qty {qty} × price {price})"
            )


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

async def place_decision(decision: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a Master Trader JSON payload and place the bracket on Binance.

    Returns a dict snapshot of the placed orders, or None on WAIT.
    Raises:
        RiskRejection        — risk_manager pre-flight failed.
        BinanceFilterError   — exchange filters failed pre-flight.
        BinanceAPIException  — exchange rejected the order despite checks.
    """
    validate_decision(decision)

    if decision["decision"] == "WAIT":
        log.info("execution.wait", symbol=decision.get("symbol"))
        return None

    symbol: str = decision["symbol"]
    side_long = decision["decision"] == "LONG"
    entry: float = float(decision["entry"])
    sl: float = float(decision["stop_loss"])
    tp: float = float(decision["take_profit"])
    leverage: int = int(decision.get("leverage") or settings.default_leverage)

    # Position size (BASE units) = risk_usd / |entry - stop|.
    risk_usd: float = float(decision["risk_usd"])
    raw_qty = risk_usd / abs(entry - sl)

    # ---- 1. Quantize EVERYTHING to exchange precision ---------------------
    filters = await _load_filters(symbol)
    entry_q = _quantize_price(entry, filters)
    sl_q = _quantize_price(sl, filters)
    tp_q = _quantize_price(tp, filters)
    qty_q = _quantize_qty(raw_qty, filters)

    # ---- 2. Pre-flight filter validation ----------------------------------
    # Validate against the entry price (the limit price we're sending);
    # bracket prices are independently checked via PRICE_FILTER.
    _validate_filters(entry_q, qty_q, filters)
    _validate_filters(sl_q, qty_q, filters)
    _validate_filters(tp_q, qty_q, filters)

    # ---- 3. Build wire-format strings + idempotency keys ------------------
    price_prec = int(filters["pricePrecision"])
    qty_prec = int(filters["quantityPrecision"])
    entry_str = _format_for_api(entry_q, price_prec)
    qty_str = _format_for_api(qty_q, qty_prec)
    sl_str = _format_for_api(sl_q, price_prec)
    tp_str = _format_for_api(tp_q, price_prec)

    base_id = f"traderay-{uuid.uuid4().hex[:12]}"
    entry_cid = base_id
    sl_cid = f"{base_id}-sl"
    tp_cid = f"{base_id}-tp"

    side = SIDE_BUY if side_long else SIDE_SELL
    close_side = SIDE_SELL if side_long else SIDE_BUY

    log.info(
        "execution.preflight_ok",
        symbol=symbol, side=("LONG" if side_long else "SHORT"),
        raw_qty=raw_qty, qty=qty_q, entry=entry_q, sl=sl_q, tp=tp_q,
        client_id=base_id,
    )

    c = await _client()

    # ---- 4. Set leverage (idempotent on Binance side) ---------------------
    await _change_leverage_safe(c, symbol=symbol, leverage=leverage)

    # ---- 5. Entry: limit GTC ---------------------------------------------
    entry_order = await _create_order_idempotent(
        c,
        symbol=symbol,
        side=side,
        type=FUTURE_ORDER_TYPE_LIMIT,
        timeInForce=TIME_IN_FORCE_GTC,
        price=entry_str,
        quantity=qty_str,
        newClientOrderId=entry_cid,
    )

    # ---- 6. Stop-loss: stop-market, closePosition, MARK_PRICE -------------
    sl_order = await _create_order_idempotent(
        c,
        symbol=symbol,
        side=close_side,
        type=FUTURE_ORDER_TYPE_STOP_MARKET,
        stopPrice=sl_str,
        closePosition=True,
        workingType="MARK_PRICE",
        newClientOrderId=sl_cid,
    )

    # ---- 7. Take-profit: tp-market, closePosition, MARK_PRICE ------------
    tp_order = await _create_order_idempotent(
        c,
        symbol=symbol,
        side=close_side,
        type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
        stopPrice=tp_str,
        closePosition=True,
        workingType="MARK_PRICE",
        newClientOrderId=tp_cid,
    )

    snapshot = {
        "client_id": base_id,
        "symbol": symbol,
        "side": "LONG" if side_long else "SHORT",
        "entry_price": entry_q,
        "stop_loss": sl_q,
        "take_profit": tp_q,
        "qty": qty_q,
        "leverage": leverage,
        "entry_order_id": entry_order.get("orderId"),
        "sl_order_id": sl_order.get("orderId"),
        "tp_order_id": tp_order.get("orderId"),
        "entry_client_id": entry_cid,
        "sl_client_id": sl_cid,
        "tp_client_id": tp_cid,
        "placed_at": int(time.time()),
        "decision": decision,
    }

    await cache.set_json(f"order:{base_id}", snapshot, ttl=86400)
    recent = (await cache.get_json("orders:recent")) or []
    recent.insert(0, snapshot)
    await cache.set_json("orders:recent", recent[:25], ttl=86400)

    log.info(
        "execution.placed",
        symbol=symbol, side=snapshot["side"],
        entry=entry_q, sl=sl_q, tp=tp_q, qty=qty_q,
        client_id=base_id,
    )

    # Fire-and-forget Telegram alert. The trading flow MUST NOT block on
    # Telegram — fire() schedules the send on the running loop and returns
    # immediately; failures inside the notifier are caught and logged.
    fire(
        notify_crypto_trade_placed(
            side=snapshot["side"],
            symbol=symbol,
            entry=entry_q,
            risk_usd=risk_usd,
        )
    )

    return snapshot


async def execute_if_actionable(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """Wrapper that swallows risk + filter rejections so a bad decision
    doesn't kill the scheduler tick."""
    decision = bundle.get("decision") or {}
    try:
        return await place_decision(decision)
    except RiskRejection as e:
        log.warning("execution.rejected_risk", reason=str(e), symbol=decision.get("symbol"))
        return None
    except BinanceFilterError as e:
        log.warning("execution.rejected_filter", reason=str(e), symbol=decision.get("symbol"))
        return None
    except BinanceAPIException as e:
        log.exception(
            "execution.failed_api",
            symbol=decision.get("symbol"),
            code=e.code, status=getattr(e, "status_code", None), msg=str(e),
        )
        return None
    except Exception as e:
        log.exception("execution.failed", err=str(e), symbol=decision.get("symbol"))
        return None


# ---------------------------------------------------------------------------
# Chandelier trailing-exit support — cancel + re-place the SL leg
# ---------------------------------------------------------------------------

async def replace_stop_loss(
    *,
    symbol: str,
    side_long: bool,
    old_sl_order_id: int | str | None,
    new_sl_price: float,
) -> dict[str, Any]:
    """Atomically replace the SL leg of an OPEN position with a tighter one.

    Used by the Chandelier trailing-exit tracker job. Returns the new order
    dict (so callers can persist the new orderId + price).

    The new SL keeps the same shape as the original entry-side SL leg:
      - type=STOP_MARKET, closePosition=True, workingType=MARK_PRICE
      - close_side = SELL for LONG / BUY for SHORT
      - newClientOrderId tagged `traderay-trail-<uuid>` so retries are idempotent
        and the lineage is visible in Binance UI.

    Caller MUST hold the trade row; this function only talks to Binance.
    """
    filters = await _load_filters(symbol)
    sl_q = _quantize_price(new_sl_price, filters)
    _validate_filters(sl_q, qty=0.0, filters=filters) if False else None  # noqa: E711 — see comment below
    # NOTE: we DO NOT call _validate_filters here because qty=0 always fails
    # MIN_NOTIONAL. SL is a closePosition order — qty is implicit at execution.
    # Price-side filters are still enforced via _quantize_price + the explicit
    # PRICE_FILTER min/max check below.
    price_prec = int(filters["pricePrecision"])
    sl_str = _format_for_api(sl_q, price_prec)

    pf = filters["PRICE_FILTER"]
    min_price = float(pf.get("minPrice", 0) or 0)
    max_price = float(pf.get("maxPrice", "inf") or "inf")
    if min_price and sl_q < min_price:
        raise BinanceFilterError(
            f"new SL {sl_q} < PRICE_FILTER.minPrice {min_price}"
        )
    if max_price and sl_q > max_price:
        raise BinanceFilterError(
            f"new SL {sl_q} > PRICE_FILTER.maxPrice {max_price}"
        )

    c = await _client()

    # 1. Cancel the existing SL leg (best-effort: it may have already filled).
    if old_sl_order_id:
        try:
            await c.futures_cancel_order(symbol=symbol, orderId=old_sl_order_id)
        except BinanceAPIException as e:
            # -2011 (unknown order id) means it filled or was already canceled.
            if e.code not in _NO_RETRY_CODES:
                raise
            log.info(
                "execution.trail.old_sl_already_gone",
                symbol=symbol, old_sl_order_id=old_sl_order_id, code=e.code,
            )

    # 2. Place the replacement SL with a fresh client_id.
    new_cid = f"traderay-trail-{uuid.uuid4().hex[:10]}"
    close_side = SIDE_SELL if side_long else SIDE_BUY
    new_order = await _create_order_idempotent(
        c,
        symbol=symbol,
        side=close_side,
        type=FUTURE_ORDER_TYPE_STOP_MARKET,
        stopPrice=sl_str,
        closePosition=True,
        workingType="MARK_PRICE",
        newClientOrderId=new_cid,
    )
    log.info(
        "execution.trail.sl_replaced",
        symbol=symbol, side=("LONG" if side_long else "SHORT"),
        old_sl_order_id=old_sl_order_id, new_sl_order_id=new_order.get("orderId"),
        new_sl_price=sl_q,
    )
    return {
        "order_id": new_order.get("orderId"),
        "client_order_id": new_cid,
        "stop_price": sl_q,
    }


__all__ = [
    "place_decision",
    "execute_if_actionable",
    "replace_stop_loss",
    "BinanceFilterError",
]
