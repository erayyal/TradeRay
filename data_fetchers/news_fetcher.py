"""TradeRay RSS news fetcher — drop-in replacement for the deprecated
CryptoPanic Developer API.

CryptoPanic discontinued its free Developer API tier in April 2026. We
refuse to pay for what plain RSS gives us for free. This module aggregates
a curated set of high-quality public RSS feeds — including CryptoPanic's
OWN free RSS endpoint (/news/rss/) which remains available.

Output schema is identical in shape to the legacy `fetch_cryptopanic()`
return so the orchestrator wiring and Sentiment Scanner prompt work
unchanged:

    {
      "posts": [
        {
          "title":         str,
          "url":           str,
          "source":        str,         # e.g. "CoinDesk"
          "published_at":  str,         # ISO 8601 (UTC), "" if missing
          "currencies":    list[str],   # empty — RSS doesn't tag tickers
        },
        ...
      ],
      "score":          0.0,            # RSS has no community votes;
      "bullish_votes":  0,              #   Sentiment Scanner is instructed
      "bearish_votes":  0,              #   NOT to count headlines as votes
      "available":      bool,           #   anyway — tone is judged from
                                        #   title text directly.
      "feeds_used":     list[str],      # which feeds returned data
      "feeds_failed":   list[str],      # which timed out / errored
    }

The aggregator is FAULT-TOLERANT by design. Any individual feed timing
out, returning 4xx/5xx, or serving malformed XML reduces the corpus but
does not abort the call. The Sentiment Scanner is happy with any non-empty
`posts` list, and if all feeds fail the orchestrator's `_safe()` shield
treats `available=False` as "no news this cycle".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import aiohttp
import feedparser

from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Curated feed list — free, public, editorially weighty.
# Tuple shape: (display_source, rss_url).
#
# Order influences dedupe: when two feeds publish the same story, the FIRST
# occurrence in this list wins. We put CoinDesk first because it has the
# largest US-institutional reach; CryptoPanic last because it's a curator
# of stories from these same upstream sources (so dedupe naturally keeps
# the original source attribution).
# ---------------------------------------------------------------------------

FEEDS: list[tuple[str, str]] = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
    ("The Block",     "https://www.theblock.co/rss.xml"),
    ("CryptoSlate",   "https://cryptoslate.com/feed/"),
    ("Bitcoin Mag",   "https://bitcoinmagazine.com/.rss/full/"),
    ("CryptoPanic",   "https://cryptopanic.com/news/rss/"),
]

# Per-feed network ceiling. 10s × 7 feeds run concurrently, so total
# wall-clock for the fetch step is ~10s worst case.
_HTTP_TIMEOUT_SEC: int = 10

# How many entries to keep PER feed before merge. Keeps the merge cheap
# and prevents one chatty feed from dominating the aggregated set.
_PER_FEED_LIMIT: int = 15

# Some publishers (Cloudflare-fronted ones especially) reject Python's
# default UA. A boring browser-like UA gets us through.
_USER_AGENT: str = (
    "Mozilla/5.0 (compatible; TradeRay/1.0; "
    "+https://github.com/anthropics/traderay)"
)


# ---------------------------------------------------------------------------
# Stage 1 — async network fetch (one task per feed)
# ---------------------------------------------------------------------------

async def _fetch_feed_xml(
    session: aiohttp.ClientSession, source: str, url: str
) -> tuple[str, str | None]:
    """Return (source, xml_text_or_None). Never raises."""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SEC),
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": (
                    "application/rss+xml, application/atom+xml, "
                    "application/xml;q=0.9, text/xml;q=0.8"
                ),
            },
        ) as resp:
            if resp.status != 200:
                log.warning(
                    "news.feed_http_error", source=source, status=resp.status
                )
                return source, None
            return source, await resp.text()
    except asyncio.TimeoutError:
        log.warning("news.feed_timeout", source=source)
        return source, None
    except Exception as e:
        log.warning("news.feed_error", source=source, err=str(e))
        return source, None


# ---------------------------------------------------------------------------
# Stage 2 — off-thread XML parse (feedparser is sync + CPU-bound)
# ---------------------------------------------------------------------------

def _parse_feed(source: str, xml: str) -> list[dict[str, Any]]:
    """Parse RSS XML into our internal post dicts. Runs in a thread pool."""
    try:
        parsed = feedparser.parse(xml)
    except Exception as e:
        log.warning("news.feed_parse_error", source=source, err=str(e))
        return []

    posts: list[dict[str, Any]] = []
    for entry in parsed.entries[:_PER_FEED_LIMIT]:
        # Prefer `published_parsed`, fall back to `updated_parsed` for feeds
        # that don't expose original publish time (some Atom feeds do this).
        pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        pub_iso, pub_ms = "", 0
        if pub_struct:
            try:
                dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
                pub_iso = dt.isoformat()
                pub_ms = int(dt.timestamp() * 1000)
            except (TypeError, ValueError):
                pass

        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue

        posts.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "published_at": pub_iso,
                "_published_ms": pub_ms,  # internal sort key; stripped pre-return
                "currencies": [],  # RSS doesn't carry reliable ticker tags
            }
        )
    return posts


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def fetch_latest_news(limit: int = 20) -> dict[str, Any]:
    """Fan out, parse, merge, dedupe, sort, cap. Return the news payload.

    Args:
        limit: Maximum number of merged headlines to return (newest first).

    Returns:
        A dict matching the legacy CryptoPanic payload shape — see module
        docstring. Always returns; never raises. `available=False` means
        every feed failed this cycle.
    """
    # Stage 1: parallel HTTP fetches
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_fetch_feed_xml(session, src, url) for src, url in FEEDS),
            return_exceptions=False,
        )

    # Stage 2: parallel off-thread XML parses (only for feeds that gave us bytes)
    parse_tasks = [
        asyncio.to_thread(_parse_feed, source, xml)
        for source, xml in results
        if xml
    ]
    parsed_lists: list[list[dict[str, Any]]] = (
        await asyncio.gather(*parse_tasks) if parse_tasks else []
    )

    # Track feed health for observability — surfaces in Redis + logs
    feeds_used: list[str] = []
    feeds_failed: list[str] = []
    for source, xml in results:
        (feeds_used if xml else feeds_failed).append(source)

    # Stage 3: merge with URL-based dedupe (first-feed-wins per the FEEDS order)
    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []
    for posts in parsed_lists:
        for p in posts:
            if p["url"] in seen_urls:
                continue
            seen_urls.add(p["url"])
            merged.append(p)

    # Stage 4: sort newest first, cap, strip internal sort key
    merged.sort(key=lambda p: p.get("_published_ms", 0), reverse=True)
    top = merged[:limit]
    for p in top:
        p.pop("_published_ms", None)

    payload: dict[str, Any] = {
        "posts": top,
        # Score + vote fields are kept at 0 to preserve the legacy schema
        # without smuggling synthetic data. The Sentiment Scanner prompt
        # explicitly instructs the LLM to weight news by *impact* rather
        # than by count or score — title text is the load-bearing signal.
        "score": 0.0,
        "bullish_votes": 0,
        "bearish_votes": 0,
        "available": bool(top),
        "feeds_used": feeds_used,
        "feeds_failed": feeds_failed,
    }

    # Cache for the dashboard / ops debugging. TTL > scheduler cycle so the
    # most recent successful fetch is always inspectable even between ticks.
    try:
        await cache.set_json("news:latest", payload, ttl=900)
    except Exception as e:
        # Cache write failure must not break the news pipeline.
        log.warning("news.cache_failed", err=str(e))

    log.info(
        "news.refresh",
        n=len(top),
        n_feeds_ok=len(feeds_used),
        n_feeds_failed=len(feeds_failed),
        feeds_failed=feeds_failed,
    )
    return payload


__all__ = ["fetch_latest_news", "FEEDS"]
