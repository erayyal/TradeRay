"""Render OHLCV candles to a Base64 PNG chart for AI vision input.

The Master Trader prompt instructs Claude Opus 4.7 to physically inspect this
chart for support/resistance, trendlines, breakouts, and classical patterns
(Head & Shoulders, double tops/bottoms, flags, wedges). The image is shipped
alongside the JSON payload as a multi-content block — see
`build_vision_message()` for the exact shape Anthropic's Messages API expects.

Matplotlib / mplfinance are blocking — call `render_chart_base64()` from an
async context via `asyncio.to_thread(...)` to avoid stalling the event loop.
"""
from __future__ import annotations

import base64
import io
from typing import Any, Iterable

import matplotlib

# Headless backend — never try to open a window from a server process
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

from core.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------

# Anthropic accepts PNG/JPEG/WEBP/GIF; PNG keeps candle wicks crisp.
_IMAGE_FORMAT = "png"
_IMAGE_MEDIA_TYPE = "image/png"

_DEFAULT_STYLE = mpf.make_mpf_style(
    base_mpf_style="charles",
    marketcolors=mpf.make_marketcolors(
        up="#26a69a", down="#ef5350",
        edge="inherit", wick="inherit", volume="in",
    ),
    facecolor="#0e1117",
    figcolor="#0e1117",
    edgecolor="#444",
    gridcolor="#222",
    rc={"axes.labelcolor": "#ccc", "xtick.color": "#ccc", "ytick.color": "#ccc"},
)


def _candles_to_dataframe(candles: Iterable[dict]) -> pd.DataFrame:
    df = pd.DataFrame(list(candles))
    if df.empty:
        return df
    df["dt"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close", "volume"]]
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    return df


def render_chart_base64(
    candles: list[dict],
    *,
    symbol: str,
    interval: str,
    last_n: int = 120,
    show_ema: tuple[int, ...] = (20, 50),
    title_suffix: str | None = None,
) -> str | None:
    """Render the last `last_n` candles to a Base64-encoded PNG string.

    Returns None when the input is too short to render meaningfully — callers
    should treat None as "skip the vision block, send JSON only".
    """
    if not candles or len(candles) < 30:
        log.warning("vision.skip", reason="insufficient_candles", n=len(candles or []))
        return None

    df = _candles_to_dataframe(candles[-last_n:])
    if df.empty:
        return None

    title = f"{symbol}  ·  {interval}"
    if title_suffix:
        title = f"{title}  ·  {title_suffix}"

    addplots = []
    # EMA overlays — only include if we have enough bars to compute them
    for span in show_ema:
        if len(df) > span:
            ema = df["Close"].ewm(span=span, adjust=False).mean()
            addplots.append(
                mpf.make_addplot(ema, width=0.9, color=("#f5b041" if span <= 20 else "#5dade2"))
            )

    buf = io.BytesIO()
    try:
        mpf.plot(
            df,
            type="candle",
            volume=True,
            style=_DEFAULT_STYLE,
            title=title,
            ylabel="Price",
            ylabel_lower="Vol",
            figsize=(11, 6),
            tight_layout=True,
            addplot=addplots if addplots else None,
            savefig=dict(
                fname=buf,
                format=_IMAGE_FORMAT,
                dpi=120,
                bbox_inches="tight",
                facecolor=_DEFAULT_STYLE["facecolor"],
            ),
        )
    except Exception as e:
        log.exception("vision.render_failed", symbol=symbol, interval=interval, err=str(e))
        return None
    finally:
        plt.close("all")

    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("ascii")
    log.info(
        "vision.rendered",
        symbol=symbol, interval=interval, bars=len(df), bytes_b64=len(encoded),
    )
    return encoded


# ---------------------------------------------------------------------------
# Anthropic message-content helpers
# ---------------------------------------------------------------------------

def build_vision_message(
    *,
    json_text: str,
    image_base64: str | None,
    image_caption: str | None = None,
) -> list[dict[str, Any]]:
    """Compose the multi-block `content` array for the Master Trader user turn.

    Order matters: image first, then text — Claude attends to images at the
    position they appear, so putting the chart before the JSON nudges the
    model to anchor its reasoning visually before reading numbers.
    """
    blocks: list[dict[str, Any]] = []

    if image_base64:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _IMAGE_MEDIA_TYPE,
                    "data": image_base64,
                },
            }
        )
        if image_caption:
            blocks.append({"type": "text", "text": f"[chart] {image_caption}"})

    blocks.append({"type": "text", "text": json_text})
    return blocks


__all__ = [
    "render_chart_base64",
    "build_vision_message",
]
