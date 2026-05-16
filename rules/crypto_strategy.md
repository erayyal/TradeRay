# Crypto Strategy Reference (BTCUSDT, ETHUSDT, USDT-perp universe)

> **Status: REFERENCE, NOT DOGMA.** Harmonize these patterns with the
> quantitative evidence in the Quant Analyst report and what you actually
> see on the candle chart. If the chart contradicts a textbook setup,
> trust your eyes — a "perfect" Wyckoff schematic that the price has
> already invalidated is not a setup, it's confirmation bias.

> **v2 update — literature-grounded baseline.** TradeRay's rule engine now
> uses a market × term parameter matrix (see `RESEARCH_ALGORITHM.md` §9).
> For Crypto the bias is:
> - **SCALP (15m + 1h confirm)**: Connors-style **mean-reversion** —
>   RSI(2)≤10 LONG / ≥90 SHORT, near BB tail, higher-TF trend filter.
> - **SHORT_TERM (4h + 1d confirm)**: ADX-gated **hybrid** — ADX>25 trades
>   pullbacks-in-trend; ADX<20 trades BB mean-reversion; transitional band → WAIT.
> - **MID_TERM (1d)**: **trend-following** (price>EMA200, MACD positive,
>   ADX>25). R:R target 3.0; Chandelier exit recommended (Phase 3).
> Stops: 1.0×ATR (SCALP) / 1.5×ATR (SHORT) / 2.0×ATR (MID). Risk per trade 2%.

---

## 1. Market Microstructure (24/7, perpetual futures)

- **No closing bell.** No gap risk between sessions. Entry/exit can fire
  any minute of the week. This means stops sized purely on intraday
  ATR are *not* underestimated — but it also means liquidity has
  micro-cycles you should respect:
    - **Asia open (~00:00–04:00 UTC):** thin liquidity, prone to wicks,
      good for stop-runs, bad for breakouts.
    - **EU/London (~07:00–11:00 UTC):** first real volume, common
      reversal zone after Asia drift.
    - **NY (~13:00–21:00 UTC):** maximum participation. Most trend
      moves originate or accelerate here.
    - **Weekend (Sat/Sun):** lower volume, manipulation-prone, treat
      breakouts skeptically.
- **Funding rate** (every 8h on Binance perps). Take this as a
  contrarian indicator at extremes:
    - Funding > +0.05% sustained → crowded longs, potential squeeze short.
    - Funding < −0.03% sustained → crowded shorts, potential squeeze long.
- **Open interest (OI) divergence.** Price up + OI up = real buyers.
  Price up + OI down = short covering (weaker). Price down + OI up =
  fresh shorts (continuation more likely).

## 2. Liquidity Sweeps (ICT / SMC framing)

The dominant intraday pattern on crypto perps. Use this as a chart-
reading lens, not a trading recipe.

- **Equal highs / equal lows** become magnets — algos hunt the stop
  pools sitting just beyond. A clean wick *through* the equal level
  followed by a strong rejection candle on the next bar is the
  high-probability sweep.
- **Liquidity sweep ≠ breakout.** A sweep that fails to close beyond
  the level is a reversal signal. A sweep that closes *and consolidates*
  beyond the level is a real breakout.
- **Asia high / Asia low** are sweep targets in EU and NY sessions.
- **Daily / weekly highs and lows** are higher-conviction sweep zones —
  a sweep of the prior day's high in the NY session that fails to
  hold is a textbook short setup at session resistance.

## 3. Wyckoff Schematics (at swing levels only)

Useful at major support/resistance, not for intraday noise.

- **Accumulation:** Spring (sweep of range low → recovery) → Test →
  Sign of Strength → markup. Long entries on the post-spring test
  with stop below the spring low.
- **Distribution:** Upthrust (sweep of range high → rejection) → Test
  → Sign of Weakness → markdown. Mirror for shorts.
- The timeframe matters — Wyckoff schematics on 5m are often noise.
  Reserve the framing for 4h+ structures.

## 4. Volatility Regimes (use Quant's `volatility_state`)

- **Low (atr_pct < 1%):** prefer breakout setups; range trades will be
  small wins with high stop-out risk.
- **Normal (1–3%):** all playbooks usable. Best regime for textbook
  trend trades.
- **Elevated (3–6%):** widen stops to 2–2.5× ATR; reduce size; favor
  trend-with-structure trades over countertrend.
- **Extreme (>6%):** prefer WAIT. If trading, halve size. The post-
  Bollinger-blowout mean reversion is a known pattern but the entry
  timing is brutal — wait for the second test of the extreme.

## 5. Mean Reversion vs Trend

Crypto runs both modes; the regime determines which works.

- **Mean reversion** (Bollinger snapback, RSI extreme + reversal candle):
  works when 4h ADX low, when price is at a multi-day extreme without
  a fundamental catalyst, when funding is at an extreme.
- **Trend continuation** (pullback to EMA20/50 in trend, breakout retest):
  works when 4h ADX rising, when there is a fundamental driver (Fed,
  ETF flows, regulation), when funding has reset to neutral mid-trend.

## 6. BTC Dominance & Alt Behavior

- BTC.D rising with BTC up → alts underperform. Trade BTC, not alts.
- BTC.D falling with BTC up → alt season; rotation favors high-beta
  pairs. Favor SOL/AVAX/ETH-paired trades.
- BTC.D flat with BTC down → alt capitulation; large alts often bottom
  before BTC's final low.

## 7. Risk Sizing (Crypto AUTO_BOT only)

- **2% portfolio risk hard cap.** Never relax for "high conviction".
- Stops sized at 1.5–2.0× ATR(14) on the primary timeframe.
- R:R minimum 1.5; aim for 2.0+.
- Do not stack a new position on the same symbol if a prior position
  is still open — let bracket orders work.
- Avoid news windows: ±15 minutes around Fed / CPI / ETF announcements.

## 8. WAIT Triggers (in addition to global conflict gates)

- Price wedged inside the prior 4h candle range with declining volume
  and inside Bollinger middle band — pure noise.
- Major US holiday + weekend combination (Christmas, Thanksgiving) —
  crypto still trades but participation collapses.
- Within 6 hours of a known macro print (FOMC, NFP, CPI).
- Funding extreme but price has not yet shown the reversal candle.
