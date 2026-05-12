from __future__ import annotations

from typing import Any

from config import settings
from core.logger import get_logger

log = get_logger(__name__)


class RiskRejection(Exception):
    """Raised when a decision violates the risk envelope."""


def validate_decision(decision: dict[str, Any]) -> None:
    """Last line of defense before sending any order to Binance.

    The Master Trader is already prompted with the risk cap, but we do not
    trust LLM output for money — re-check every numeric invariant here.
    """
    action = decision.get("decision")
    if action not in {"LONG", "SHORT", "WAIT"}:
        raise RiskRejection(f"invalid decision action: {action!r}")

    if action == "WAIT":
        return

    entry = decision.get("entry")
    tp = decision.get("take_profit")
    sl = decision.get("stop_loss")
    risk_usd = decision.get("risk_usd")
    rr = decision.get("reward_risk_ratio")
    leverage = decision.get("leverage", settings.default_leverage)

    if not all(isinstance(x, (int, float)) for x in (entry, tp, sl)):
        raise RiskRejection("entry/tp/sl must all be numeric for LONG/SHORT")

    if action == "LONG" and not (sl < entry < tp):
        raise RiskRejection(f"LONG ordering wrong: sl={sl} entry={entry} tp={tp}")
    if action == "SHORT" and not (tp < entry < sl):
        raise RiskRejection(f"SHORT ordering wrong: tp={tp} entry={entry} sl={sl}")

    max_risk = settings.portfolio_notional * settings.max_risk_pct
    if risk_usd is None or risk_usd <= 0:
        raise RiskRejection(f"risk_usd must be positive: {risk_usd}")
    if risk_usd > max_risk * 1.01:  # 1% tolerance for rounding
        raise RiskRejection(
            f"risk_usd {risk_usd:.2f} exceeds cap {max_risk:.2f}"
        )

    if rr is None or rr < 1.5:
        raise RiskRejection(f"reward:risk {rr} below minimum 1.5")

    if leverage > settings.default_leverage:
        raise RiskRejection(
            f"leverage {leverage} above cap {settings.default_leverage}"
        )

    log.info(
        "risk.validated",
        action=action,
        entry=entry,
        sl=sl,
        tp=tp,
        risk_usd=risk_usd,
        rr=rr,
    )
