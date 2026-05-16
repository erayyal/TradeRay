"""Backtest statistics: Sharpe + bootstrap p-value + Deflated Sharpe.

Why these three:

  - Sharpe (per-trade): annualized risk-adjusted return on the realized PnL
    stream. Standard but easy to fool.
  - Bootstrap p-value (Aronson 2006, "Evidence-Based Technical Analysis"):
    builds the null distribution by resampling PnL with replacement and
    computing how often the resampled Sharpe meets/exceeds the observed.
    A small p-value (< 0.05) is *necessary* but not sufficient.
  - Deflated Sharpe (López de Prado 2018, "Advances in Financial Machine
    Learning"): adjusts for multiple-testing. If you ran `n_trials` strategy
    variants, the expected max-Sharpe under the null grows roughly with
    sqrt(log(n_trials)) — DSR discounts the observed Sharpe accordingly and
    returns the probability that the *true* Sharpe is positive.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _skew(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = _mean(xs)
    sd = _stdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 3 for x in xs) / n


def _kurt(xs: Sequence[float]) -> float:
    """Excess kurtosis (Fisher: 0 = normal)."""
    n = len(xs)
    if n < 4:
        return 0.0
    m = _mean(xs)
    sd = _stdev(xs)
    if sd == 0:
        return 0.0
    return sum(((x - m) / sd) ** 4 for x in xs) / n - 3.0


def per_trade_sharpe(returns: Sequence[float]) -> float:
    """Per-trade Sharpe (no annualization). Use `annualize` separately.

    Returns 0.0 when the return series has effectively zero dispersion
    (constant returns or numerical noise). The epsilon guards against
    `stdev` being a tiny float-precision residual on identical inputs.
    """
    if len(returns) < 2:
        return 0.0
    mu = _mean(returns)
    sd = _stdev(returns)
    # Threshold ~1e-12 of typical return scale — any real strategy has SD
    # orders of magnitude larger than this. Anything below is FP noise.
    if sd < 1e-12 * max(abs(mu), 1.0):
        return 0.0
    return mu / sd


def bootstrap_pvalue(
    returns: Sequence[float], *, n_boot: int = 5000, seed: int = 7,
) -> float:
    """Permutation p-value for `Sharpe > 0` under the null of zero drift.

    Builds the null by sign-flipping each return iid — i.e. the distribution
    of Sharpe ratios *if the strategy had no edge*. Returns the fraction of
    bootstrapped null-Sharpes that exceed the observed Sharpe.

    Sign-flip > resample-with-replacement here because we want the null
    "expected return = 0, same volatility shape", which is exactly what
    flipping signs preserves.
    """
    if len(returns) < 5:
        return 1.0
    observed = per_trade_sharpe(returns)
    if observed <= 0:
        return 1.0
    rng = random.Random(seed)
    n = len(returns)
    n_extreme = 0
    for _ in range(n_boot):
        flipped = [
            r if rng.random() < 0.5 else -r
            for r in returns
        ]
        if per_trade_sharpe(flipped) >= observed:
            n_extreme += 1
    return n_extreme / n_boot


def deflated_sharpe(
    returns: Sequence[float],
    *,
    n_trials: int = 1,
) -> float:
    """López de Prado 2018 Deflated Sharpe Ratio (probability SR_true > 0).

    Inputs:
      returns  : per-trade returns (list[float])
      n_trials : number of strategy variants tried (incl. parameter combos)

    Returns: probability the *true* Sharpe is positive, after deflating for
    sample size, non-normality (skew/kurt), and multiple testing.

    Caveat: the SR0 (expected-max-under-null) formula uses the
    Bailey-López-de-Prado approximation that requires at least 2 trials —
    we floor n_trials at 2.
    """
    n = len(returns)
    if n < 4:
        return 0.0
    sr_hat = per_trade_sharpe(returns)
    sk = _skew(returns)
    ku = _kurt(returns)

    # Standard error of SR estimator (Mertens 2002):
    #   se(SR) = sqrt((1 + 0.5 SR^2 − sk·SR + (ku/4)·SR^2) / (n − 1))
    var_sr = (1 + 0.5 * sr_hat ** 2 - sk * sr_hat + (ku / 4.0) * sr_hat ** 2) / (n - 1)
    if var_sr <= 0:
        return 0.0
    se_sr = math.sqrt(var_sr)

    # Expected max SR under H0: SR_true = 0.
    # Bailey & López de Prado 2014 approximation using Euler-Mascheroni
    # constant and Z-quantile inversion.
    n_trials = max(2, int(n_trials))
    euler_gamma = 0.5772156649
    z = _inv_norm(1 - 1 / n_trials)
    z2 = _inv_norm(1 - 1 / (n_trials * math.e))
    sr0 = se_sr * ((1 - euler_gamma) * z + euler_gamma * z2)

    # Standardize and convert via the normal CDF.
    dsr = (sr_hat - sr0) / se_sr
    return _norm_cdf(dsr)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _inv_norm(p: float) -> float:
    """Beasley-Springer-Moro approximation for the inverse normal CDF.

    Accurate to ~7 decimals on (0, 1). Clamps inputs to avoid log(0) blowups
    when n_trials is very large.
    """
    p = min(max(p, 1e-12), 1 - 1e-12)
    a = [-3.969683028665376e+01,  2.209460984245205e+02,
         -2.759285104469687e+02,  1.383577518672690e+02,
         -3.066479806614716e+01,  2.506628277459239e+00]
    b = [-5.447609879822406e+01,  1.615858368580409e+02,
         -1.556989798598866e+02,  6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00,  2.938163982698783e+00]
    d = [ 7.784695709041462e-03,  3.224671290700398e-01,
          2.445134137142996e+00,  3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


@dataclass(frozen=True)
class Summary:
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_pnl: float
    total_pnl: float
    avg_r: float            # avg return in R-multiples
    sharpe_per_trade: float
    sharpe_annualized: float
    pvalue: float
    dsr: float              # probability SR_true > 0


def summarize(
    returns: Sequence[float],
    *,
    r_multiples: Sequence[float] | None = None,
    trades_per_year: int = 100,
    n_trials: int = 1,
) -> Summary:
    """Compress a list of per-trade returns into the headline report."""
    n = len(returns)
    if n == 0:
        return Summary(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    wins = sum(1 for r in returns if r > 0)
    losses = n - wins
    avg = _mean(returns)
    sr = per_trade_sharpe(returns)
    sr_ann = sr * math.sqrt(max(1, trades_per_year))
    pval = bootstrap_pvalue(returns)
    dsr = deflated_sharpe(returns, n_trials=n_trials)
    avg_r = _mean(r_multiples) if r_multiples else 0.0
    return Summary(
        n_trades=n,
        wins=wins,
        losses=losses,
        win_rate=wins / n,
        avg_pnl=avg,
        total_pnl=sum(returns),
        avg_r=avg_r,
        sharpe_per_trade=sr,
        sharpe_annualized=sr_ann,
        pvalue=pval,
        dsr=dsr,
    )


__all__ = [
    "Summary",
    "bootstrap_pvalue",
    "deflated_sharpe",
    "per_trade_sharpe",
    "summarize",
]
