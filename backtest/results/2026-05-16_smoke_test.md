# Phase 3.5 Backtest Smoke Test — 2026-05-16

First walk-forward results against real Binance data using the harness in
`backtest/`. **The harness works; the results are the message.**

## Method

`python -m backtest <symbol> CRYPTO <term> 2024-01-01 2026-05-15 --n-trials 12`

- Walk-forward replay, no look-ahead (Pardo 1992).
- Single TF (`confirm_interval=None` for backtest) — production engine adds
  a 2-TF check on top.
- TP/SL touch resolved on next bar; same-bar tie → SL wins (conservative).
- n_trials=12 baked into DSR to account for the 4 markets × 3 terms search.

## Results (current v2.5 parameters, untuned)

| Symbol  | Term       | Setups | Closed | Win %  | Avg R  | Total R | Sharpe (ann) | DSR    |
|---------|------------|--------|--------|--------|--------|---------|--------------|--------|
| BTCUSDT | MID_TERM   | 31     | 30     | 10.0%  | -0.60  | -18.0   | -7.80        | 0.0005 |
| ETHUSDT | MID_TERM   | 18     | 17     | 5.9%   | -0.76  | -13.0   | -12.51       | 0.001  |
| SOLUSDT | MID_TERM   | 24     | 23     | 17.4%  | -0.30  | -7.0    | -3.12        | 0.007  |
| BTCUSDT | SHORT_TERM | 0      | 0      | —      | —      | —       | —            | —      |

**Bootstrap p-value = 1.0 on all three negative runs** — there is essentially
no chance the observed negative Sharpe arose by luck under the null. The
strategy is genuinely losing money on this sample.

## What this tells us

1. **MID_TERM 1d TF on crypto, as currently parameterized, is unprofitable.**
   The math is internally consistent — 10–17% win rate at R:R=3 gives expected
   R ≈ -0.6, which matches `avg_R` directly. Single-coin trend-following on
   noisy daily data gets chopped up by ATR-based stops.

2. **SHORT_TERM 4h produced ZERO setups in 1489 bars.** The combined
   `RSI(14) ≤ 35 / ≥ 65` + `rel_volume ≥ 1.2` + `ADX ≥ 25` filter is too
   strict on the 4h timeframe. Relaxing one of these (probably the volume
   gate to 1.0) would let the HYB bias actually fire.

3. **The harness DID its job.** Without this, we'd have wired the live bot
   to MID_TERM crypto on real money and started bleeding R-multiples on
   day one. Backtest exposed it for free.

## Implications for production rollout

- **Do NOT enable AUTO_BOT on crypto MID_TERM at current parameters.**
- **SIGNAL-only** mode is fine — losing-strategy signals are still useful
  reference data while we tune. The user reviews each signal, doesn't follow
  it.
- Phase 4 priority becomes: **parameter sweep + walk-forward selection**
  using the `n_trials` arm of DSR to penalize multi-testing. Realistic
  parameter ranges to sweep:
  - `atr_sl_mult`: 1.5 → 3.0 step 0.25
  - `rr_target`: 1.5 → 4.0 step 0.25
  - `adx_min_for_trend`: 20 → 30 step 2
  - `rel_volume_min`: 0.8 → 1.5 step 0.1
- Also worth testing: **MR bias on MID_TERM crypto** instead of TF.
  Mean-reversion on daily Bitcoin (Connors-style) has more historical
  evidence than pure trend-following on individual coins.

## Caveats (what this test does NOT prove)

- Two-year window is short for a daily-TF strategy.
- Confirm-TF stripped — production engine adds a 2nd timeframe filter
  that should reduce false signals (untested here).
- Macro gates (VIX/FOMC/TCMB/earnings) not applied in backtest — they
  would have prevented some of these entries.
- Funding-rate bias-flip not applied — same reason.

So the production engine should perform *somewhat* better than these numbers
suggest, but not dramatically — the gates are sparse triggers, not a
sign-flipper. Realistically expect maybe +10-20% Sharpe improvement from
gates alone. Not enough to fix this.
