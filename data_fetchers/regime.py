"""2-state Gaussian HMM volatility-regime detector (Hamilton 1989).

Pure numpy Baum-Welch — no extra dependency. Two states on log returns:
  state 0 = low-volatility  (calm / trending-friendly)
  state 1 = high-volatility (panic / mean-reversion-friendly)

Lookahead discipline (the part most naive implementations get wrong):
  - Parameters are re-fit on an EXPANDING window every `refit_every` bars,
    using only data up to the refit point.
  - Per-bar probabilities are FILTERED (forward algorithm only) — the
    smoothed (forward-backward) gamma uses future observations and would
    leak information into a walk-forward backtest.

`annotate_regime(candles)` is the one public entrypoint: it returns a list
aligned 1:1 with `candles` of P(high-vol state | data up to that bar),
None where there isn't enough history yet.
"""
from __future__ import annotations

import math

import numpy as np

from core.logger import get_logger

log = get_logger(__name__)

# Minimum observations before the first fit — below this the EM estimates
# are noise. ~6 months of daily bars / ~5 weeks of 4h bars.
_MIN_OBS: int = 120
_REFIT_EVERY: int = 20
_EM_ITERS: int = 40
_VAR_FLOOR: float = 1e-12


def _gaussian_logpdf(x: np.ndarray, mean: float, var: float) -> np.ndarray:
    var = max(var, _VAR_FLOOR)
    return -0.5 * (np.log(2 * np.pi * var) + (x - mean) ** 2 / var)


def fit_hmm(
    returns: np.ndarray,
    *,
    n_iter: int = _EM_ITERS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Baum-Welch for a 2-state Gaussian HMM on `returns`.

    Returns (pi, A, means, vars_) — initial distribution, transition matrix,
    per-state means and variances. State 1 is forced to be the HIGH-variance
    state on return (swap if needed) so callers can rely on the convention.
    """
    x = np.asarray(returns, dtype=np.float64)
    T = len(x)

    # Init: split by |return| median → calm vs turbulent halves.
    abs_med = np.median(np.abs(x))
    hi = np.abs(x) > abs_med
    means = np.array([x[~hi].mean() if (~hi).any() else 0.0,
                      x[hi].mean() if hi.any() else 0.0])
    vars_ = np.array([
        max(x[~hi].var(), _VAR_FLOOR) if (~hi).any() else _VAR_FLOOR,
        max(x[hi].var(), _VAR_FLOOR) if hi.any() else 4 * _VAR_FLOOR,
    ])
    A = np.array([[0.95, 0.05], [0.05, 0.95]])   # sticky regimes (Hamilton)
    pi = np.array([0.5, 0.5])

    for _ in range(n_iter):
        # E-step (log-space forward-backward with scaling)
        logB = np.stack([
            _gaussian_logpdf(x, means[0], vars_[0]),
            _gaussian_logpdf(x, means[1], vars_[1]),
        ], axis=1)                                # (T, 2)
        B = np.exp(logB - logB.max(axis=1, keepdims=True))

        alpha = np.zeros((T, 2))
        scale = np.zeros(T)
        alpha[0] = pi * B[0]
        scale[0] = alpha[0].sum() or 1e-300
        alpha[0] /= scale[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            scale[t] = alpha[t].sum() or 1e-300
            alpha[t] /= scale[t]

        beta = np.zeros((T, 2))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / scale[t + 1]

        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True)

        xi_num = np.zeros((2, 2))
        for t in range(T - 1):
            num = (alpha[t][:, None] * A) * (B[t + 1] * beta[t + 1])[None, :]
            s = num.sum() or 1e-300
            xi_num += num / s

        # M-step
        pi = gamma[0]
        A = xi_num / (xi_num.sum(axis=1, keepdims=True) + 1e-300)
        for k in (0, 1):
            w = gamma[:, k]
            wsum = w.sum() or 1e-300
            means[k] = (w * x).sum() / wsum
            vars_[k] = max(((w * (x - means[k]) ** 2).sum() / wsum), _VAR_FLOOR)

    # Convention: index 1 = high-vol state.
    if vars_[0] > vars_[1]:
        means = means[::-1].copy()
        vars_ = vars_[::-1].copy()
        A = A[::-1, ::-1].copy()
        pi = pi[::-1].copy()

    return pi, A, means, vars_


def filtered_p_high(
    returns: np.ndarray,
    pi: np.ndarray, A: np.ndarray, means: np.ndarray, vars_: np.ndarray,
) -> np.ndarray:
    """Forward-only filtered P(state=high-vol | r_1..r_t) per bar."""
    x = np.asarray(returns, dtype=np.float64)
    T = len(x)
    out = np.zeros(T)
    logB = np.stack([
        _gaussian_logpdf(x, means[0], vars_[0]),
        _gaussian_logpdf(x, means[1], vars_[1]),
    ], axis=1)
    B = np.exp(logB - logB.max(axis=1, keepdims=True))
    alpha = pi * B[0]
    alpha /= alpha.sum() or 1e-300
    out[0] = alpha[1]
    for t in range(1, T):
        alpha = (alpha @ A) * B[t]
        alpha /= alpha.sum() or 1e-300
        out[t] = alpha[1]
    return out


def _filter_step(
    alpha: np.ndarray, r: float,
    A: np.ndarray, means: np.ndarray, vars_: np.ndarray,
) -> np.ndarray:
    """One incremental forward-filter step on a single new return."""
    logb = np.array([
        _gaussian_logpdf(np.array([r]), means[0], vars_[0])[0],
        _gaussian_logpdf(np.array([r]), means[1], vars_[1])[0],
    ])
    b = np.exp(logb - logb.max())
    nxt = (alpha @ A) * b
    s = nxt.sum() or 1e-300
    return nxt / s


def _forward_filter_full(
    rets: np.ndarray,
    pi: np.ndarray, A: np.ndarray, means: np.ndarray, vars_: np.ndarray,
) -> np.ndarray:
    """Full forward pass; returns the final alpha (state posterior)."""
    logB = np.stack([
        _gaussian_logpdf(rets, means[0], vars_[0]),
        _gaussian_logpdf(rets, means[1], vars_[1]),
    ], axis=1)
    B = np.exp(logB - logB.max(axis=1, keepdims=True))
    alpha = pi * B[0]
    alpha /= alpha.sum() or 1e-300
    for t in range(1, len(rets)):
        alpha = (alpha @ A) * B[t]
        alpha /= alpha.sum() or 1e-300
    return alpha


def annotate_regime(
    candles: list[dict],
    *,
    min_obs: int = _MIN_OBS,
    refit_every: int = _REFIT_EVERY,
) -> list[float | None]:
    """Per-bar filtered P(high-vol) aligned with `candles`. None pre-warmup.

    Expanding-window refits every `refit_every` bars; BETWEEN refits the
    forward filter advances incrementally on frozen parameters (one
    O(1) step per bar instead of a full O(t) re-pass) — strictly causal
    and ~T/refit_every× faster than the naive formulation.
    """
    closes = np.array([float(c["close"]) for c in candles], dtype=np.float64)
    if len(closes) < min_obs + 1:
        return [None] * len(candles)

    rets = np.diff(np.log(np.maximum(closes, 1e-12)))   # rets[i] = bar i+1's return
    out: list[float | None] = [None] * len(candles)

    params: tuple | None = None
    alpha: np.ndarray | None = None
    next_refit = min_obs
    for t in range(min_obs, len(rets) + 1):
        if params is None or t >= next_refit:
            try:
                params = fit_hmm(rets[:t])
                alpha = _forward_filter_full(rets[:t], *params)
            except Exception as e:   # EM blowup on degenerate data → keep old
                log.debug("regime.fit_failed", err=str(e), t=t)
                if params is None:
                    return out
            next_refit = t + refit_every
        elif alpha is not None:
            _pi, A, means, vars_ = params
            alpha = _filter_step(alpha, float(rets[t - 1]), A, means, vars_)
        if alpha is not None:
            out[t] = float(alpha[1])   # candle index t ↔ return index t-1
    return out


def latest_p_high(candles: list[dict]) -> float | None:
    """Single filtered P(high-vol) for the LAST closed bar — production path.

    One fit on the trailing window + one forward pass; cheap enough to run
    once per symbol per cycle.
    """
    closes = np.array([float(c["close"]) for c in candles], dtype=np.float64)
    if len(closes) < _MIN_OBS + 1:
        return None
    rets = np.diff(np.log(np.maximum(closes, 1e-12)))[-500:]
    try:
        params = fit_hmm(rets)
        return float(filtered_p_high(rets, *params)[-1])
    except Exception as e:
        log.warning("regime.latest_failed", err=str(e))
        return None


__all__ = ["annotate_regime", "latest_p_high", "fit_hmm", "filtered_p_high"]
