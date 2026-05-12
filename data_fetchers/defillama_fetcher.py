from __future__ import annotations

import aiohttp

from core.logger import get_logger
from core.redis_client import cache

log = get_logger(__name__)

BASE = "https://api.llama.fi"


async def fetch_defillama() -> dict:
    """Pull global TVL + top chain breakdown."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE}/v2/chains", timeout=15) as r:
            r.raise_for_status()
            chains = await r.json()

    chains_sorted = sorted(
        chains, key=lambda c: c.get("tvl", 0) or 0, reverse=True
    )[:10]

    total_tvl = sum((c.get("tvl") or 0) for c in chains)
    eth = next((c for c in chains if c.get("name") == "Ethereum"), {})
    eth_tvl = eth.get("tvl", 0)
    eth_dom = (eth_tvl / total_tvl) if total_tvl else 0.0

    payload = {
        "total_tvl_usd": total_tvl,
        "eth_tvl_usd": eth_tvl,
        "eth_dominance": eth_dom,
        "top_chains": [
            {"name": c["name"], "tvl_usd": c.get("tvl", 0)}
            for c in chains_sorted
        ],
    }
    await cache.set_json("onchain:defillama", payload, ttl=3600)
    log.info("defillama.refresh", total_tvl=total_tvl)
    return payload
