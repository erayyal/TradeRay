"""Monthly parameter-drift re-sweep (Adaptive Markets Hypothesis hygiene).

Lo (2004): edges decay as markets adapt. Once a month we re-run the entry
sweep for every VALIDATED (market, term) cell in an isolated subprocess
(the EM/TA loops are CPU-bound — running them in-process would stall the
event loop and every scheduler job with it), then Telegram a drift verdict:

  - best DSR now vs. the DSR the production params earned at validation;
  - whether the production parameter set still ranks in the top decile.

NOTHING is auto-applied. A human (or an agent session with the TASK.md
context) reviews the report and re-runs the apply step deliberately —
silent parameter swaps are how systematic shops blow up quietly.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from core.logger import get_logger
from core.telegram_notifier import escape_md2, is_configured, send_message

log = get_logger(__name__)

_SWEEP_TIMEOUT_S = 3600


@dataclass(frozen=True)
class SweepSpec:
    label: str
    symbols: str
    market: str
    term: str
    start: str
    biases: str
    # The DSR the CURRENT production params earned when they were validated —
    # update when params change (see backtest/results/*.md).
    baseline_dsr: float
    # The production combo's identifying fields (subset-matched against the
    # ranked results to find its current rank).
    prod_combo: dict


# Validated cells as of v3.0 (2026-06-11). Keep in sync with rule_engine.py.
SWEEP_SPECS: list[SweepSpec] = [
    SweepSpec(
        label="CRYPTO 4h (HYB)",
        symbols="BTCUSDT,ETHUSDT,SOLUSDT", market="CRYPTO", term="SHORT_TERM",
        start="2025-06-01", biases="HYB,MR", baseline_dsr=0.774,
        prod_combo={"bias": "HYB", "atr_sl_mult": 2.0, "rr_target": 1.5,
                    "adx_min_for_trend": 20.0, "rel_volume_min": 0.8},
    ),
    SweepSpec(
        label="CRYPTO 1d (TF)",
        symbols="BTCUSDT,ETHUSDT,SOLUSDT", market="CRYPTO", term="MID_TERM",
        start="2024-01-01", biases="TF,MR", baseline_dsr=0.545,
        prod_combo={"bias": "TF", "atr_sl_mult": 1.5, "rr_target": 2.0,
                    "adx_min_for_trend": 20.0, "rel_volume_min": 0.8},
    ),
    SweepSpec(
        label="BIST 1d (MR)",
        symbols=("THYAO.IS,ASELS.IS,GARAN.IS,AKBNK.IS,ISCTR.IS,"
                 "TUPRS.IS,KCHOL.IS,SISE.IS,EREGL.IS,BIMAS.IS"),
        market="BIST", term="MID_TERM",
        start="2024-01-01", biases="TF,MR", baseline_dsr=0.979,
        prod_combo={"bias": "MR", "atr_sl_mult": 1.5, "rr_target": 3.0,
                    "rel_volume_min": 0.8, "rsi_long_max": 30.0},
    ),
]


def _combo_matches(combo: dict, prod: dict) -> bool:
    return all(combo.get(k) == v for k, v in prod.items())


async def _run_one(spec: SweepSpec, end_date: str) -> dict | None:
    """Run one sweep subprocess; return parsed summary or None on failure."""
    out_path = f"/tmp/resweep_{spec.market}_{spec.term}.json"
    cmd = [
        sys.executable, "-m", "backtest.sweep",
        spec.symbols, spec.market, spec.term, spec.start, end_date,
        "--biases", spec.biases, "--json", out_path, "--top", "3",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=_SWEEP_TIMEOUT_S)
        if proc.returncode != 0:
            log.warning("resweep.subprocess_failed", label=spec.label,
                        rc=proc.returncode)
            return None
        with open(out_path) as f:
            rows = json.load(f)
    except Exception as e:
        log.exception("resweep.run_failed", label=spec.label, err=str(e))
        return None

    if not rows:
        return None
    best = rows[0]
    prod_rank = next(
        (i + 1 for i, r in enumerate(rows) if _combo_matches(r["combo"], spec.prod_combo)),
        None,
    )
    return {
        "label": spec.label,
        "baseline_dsr": spec.baseline_dsr,
        "best_dsr": best["dsr"],
        "best_total_r": best["total_r"],
        "best_combo": best["combo"],
        "prod_rank": prod_rank,
        "n_combos": len(rows),
    }


async def run_monthly_resweep() -> None:
    """Job body — sequential sweeps, then one Telegram drift report."""
    from datetime import datetime, timezone
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    for spec in SWEEP_SPECS:
        log.info("resweep.start", label=spec.label)
        r = await _run_one(spec, end_date)
        if r:
            results.append(r)
            log.info("resweep.done", **{k: v for k, v in r.items() if k != "best_combo"})

    if not results:
        log.warning("resweep.no_results")
        return
    if not is_configured():
        return

    lines = ["🔁 *Aylık parametre drift raporu*", ""]
    for r in results:
        decile = max(1, r["n_combos"] // 10)
        if r["prod_rank"] is not None and r["prod_rank"] <= decile \
                and r["best_dsr"] >= 0.5 * r["baseline_dsr"]:
            verdict = "✅ sağlıklı"
        elif r["prod_rank"] is None or r["best_dsr"] < 0.25 * r["baseline_dsr"]:
            verdict = "🚨 re\\-tune gerekli"
        else:
            verdict = "⚠️ izle"
        rank_s = str(r["prod_rank"]) if r["prod_rank"] else ">listelenmedi"
        best_s = "{:.3f}".format(r["best_dsr"])
        base_s = "{:.3f}".format(r["baseline_dsr"])
        lines.append(
            f"{escape_md2(r['label'])}: {verdict}\n"
            f"  en iyi DSR {escape_md2(best_s)} "
            f"\\(validasyon: {escape_md2(base_s)}\\), "
            f"prod set sırası: {escape_md2(rank_s)}"
        )
    lines.append("")
    lines.append(escape_md2("Otomatik uygulama yok — değişiklik kararı insan/agent incelemesiyle."))
    await send_message("\n".join(lines))


__all__ = ["run_monthly_resweep", "SWEEP_SPECS"]
