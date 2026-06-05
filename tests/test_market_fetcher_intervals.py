"""Market fetch bundles must cover the rule engine's required intervals."""
from __future__ import annotations

from agents.rule_engine import params_for
from data_fetchers.market_fetcher import intervals_for
from models import MarketType, Term


def test_fetch_term_includes_rule_engine_signal_and_confirmation_intervals():
    """Every live cycle must fetch both the primary and confirmation TF.

    A missing confirmation interval makes `_confirm_direction` return WAIT,
    which can suppress otherwise-valid setups for weeks.
    """
    for market in MarketType:
        for term in Term:
            params = params_for(market, term)
            fetched = intervals_for(term)

            assert params.signal_interval in fetched
            if params.confirm_interval is not None:
                assert params.confirm_interval in fetched
