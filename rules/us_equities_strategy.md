# US Equities Strategy Reference (S&P 500 + NASDAQ-100, ^GSPC, ^IXIC)

> **Status: REFERENCE, NOT DOGMA.** US equities are the most efficient,
> most algo-saturated public market on the planet. Textbook patterns are
> well-known and front-run; the edge is in *context* — earnings calendar,
> sector rotation, rates, macro prints — not in pure pattern recognition.
> Harmonize these references with the Quant Analyst output and what you
> see on the chart.

> **v2 update — literature-grounded baseline.** Same 3-mode rule engine.
> US-specific tweaks: SCALP uses **RSI(2) at 5/95** (Connors original
> thresholds, S&P empirically validated 2003-2010); **higher rel_volume
> gate (1.5×)** because intraday equity noise demands more confirmation;
> SHORT_TERM HYB respects PEAD (Bernard-Thomas 1989) — Master Trader
> should flag **±1 trading day around known earnings as veto context**
> (not yet enforced in code). VIX>25 should halve position size,
> VIX>35 should veto new entries entirely (Whaley fear gauge research) —
> currently this is a Master-Trader-prompt-only check.

---

## 1. Market Microstructure (RTH-only with significant pre/post-market)

- **Regular Trading Hours (RTH):** 09:30–16:00 ET (14:30–21:00 UTC during
  US daylight time; 14:30–21:00 UTC adjusts for DST). The signals you
  generate must be executable inside this window — pre-market and after-
  hours quotes from yfinance are unreliable and illiquid.
- **The opening 15 minutes** (09:30–09:45 ET) is the highest-volume
  reversal/trap zone. Avoid market orders on the open. Limit orders at
  prior-day VWAP or HOD/LOD are higher quality.
- **Lunch lull** (~12:00–13:30 ET): low volume, range-bound chop.
  Breakouts here are usually fakeouts.
- **Power hour** (15:00–16:00 ET): strongest directional moves of the day.
  Many institutional flows time their VWAP execution to this window.
- **MOC (market on close) imbalance** publishes 15:50 ET — visible flow
  signal for next-day continuation.

## 2. Gap Risk (the dominant overnight risk)

- US equities **gap on news, earnings, futures action overnight**. A stop
  sized only on intraday ATR is *under-protected* against gap risk — add
  ~1 ATR(14) of cushion for any swing position, OR use options to cap
  downside (out of scope for SIGNAL_ONLY).
- **Gap-and-go vs gap-and-fade:**
    - Gap > 2% with a fundamental catalyst (earnings beat/miss, M&A) →
      gap-and-go is more likely. Trade with the gap.
    - Gap > 2% without an obvious catalyst, opens near prior-day support/
      resistance → gap-and-fade is more likely. The first 15-min reversal
      candle is the classic entry.
    - Gap inside the prior day's range → noise. Wait for direction.

## 3. Earnings Volatility (the calendar overrides everything)

- **Avoid swing positions within 2 trading days of a known earnings
  release.** Implied move is typically 5–10% on big-cap names; that
  destroys risk-reward math.
- **Post-earnings drift (PEAD)** is real but only after the dust settles
  — wait for the day-2 close to confirm direction.
- **Earnings calendar reference:**
    - Q1 reports: mid-April through mid-May.
    - Q2 reports: mid-July through mid-August.
    - Q3 reports: mid-October through mid-November.
    - Q4 / Annual: late January through late February.
- The Master Trader should flag any signal whose horizon overlaps a known
  earnings date — even though we don't have a calendar feed, mention it
  in `chart_observations` if the chart shows a recent earnings gap.

## 4. Sector Rotation (rates regime drives this)

The single most important macro vector for US equities:

- **Falling rates / dovish Fed:** growth, tech, NASDAQ outperforms.
  Tickers: NVDA, AAPL, MSFT, GOOGL, META, AMZN, TSLA, AVGO, AMD.
- **Rising rates / hawkish Fed:** value, financials, energy, defensives.
  Tickers: JPM, BAC, XOM, CVX, JNJ, KO, PG, WMT.
- **Recession fears + falling rates:** defensives + bonds. Tickers:
  PG, KO, WMT, COST, JNJ, MRK, ABBV.
- **Risk-on (low VIX, steepening curve):** small caps, semis, cyclicals.
- **Risk-off (high VIX, inverted curve):** mega-cap tech (paradox: it
  becomes the new "defensive") + utilities + treasuries.

When the Sentiment Scanner reports `macro_regime`, weight it heavily for
US trades — sector positioning often matters more than the technical
setup on individual names.

## 5. SMC / ICT Concepts (well-suited to US algos)

This market is the native habitat of ICT-style frameworks; US equities
are the most algo-driven public market and exhibit textbook patterns
more reliably than other markets.

- **Liquidity grabs** at prior session highs/lows are routine in the
  first hour. The "Judas swing" (false move toward NY open) before the
  real move is a documented pattern on SPY/QQQ.
- **Fair Value Gaps (FVGs):** valid on 5m/15m intraday, 1h+ for swing.
  Price tends to revisit FVGs within 1–3 sessions.
- **Order blocks:** the last bullish/bearish candle before a strong
  impulse move. Valid on 1h+.
- **BOS / CHoCH:** structure breaks on 4h+ are reliable; intraday breaks
  on 1m/5m are often algo flushes.
- **Smart Money Concepts work best in liquid mega-caps** (top 30 by
  market cap, plus QQQ/SPY index ETFs). Mid-caps and below have less
  algorithmic structure and noisier candles.

## 6. Volatility Regimes (use VIX as the master gauge)

- **VIX < 15:** complacent regime. Breakouts work. Trend continuation.
  Mean reversion is short-lived and shallow.
- **VIX 15–20:** normal. Both playbooks work.
- **VIX 20–30:** elevated. Reduce size. Mean reversion edges improve.
  Skip overnight holds without strong R:R.
- **VIX > 30:** crisis regime. Almost always WAIT. The post-spike (VIX
  rolling lower from extremes) is the highest-Sharpe long-equity setup
  but timing is brutal.

## 7. Index vs Single Name

- **^GSPC / ^IXIC index signals** apply to the broad market. Tradeable
  via SPY / QQQ ETF positions (not in this terminal, but useful as a
  context filter).
- **Mega-cap signals (AAPL, MSFT, NVDA)** are partially correlated with
  the index — a "long NVDA" signal during a "short ^GSPC" regime is
  high-conviction-required (specific bullish driver needed).

## 8. Intraday vs Swing Bias by Term

- **SCALP (5m+15m):** US equities only — the playbook is opening-range
  break + first hour reversal. Avoid the 11:00–13:30 ET dead zone.
- **SHORT_TERM (1h+4h):** swing trades over 1–5 sessions. Earnings
  awareness mandatory.
- **MID_TERM (1d):** weekly+ position trades. Sector rotation +
  macro regime dominate. Single-stock idiosyncratic risk should be
  smaller than the macro thesis.

## 9. WAIT Triggers Specific to US Equities

- Within ±60 min of a Fed press conference / FOMC release.
- Within ±30 min of NFP / CPI / PPI release (08:30 ET prints).
- Within 2 sessions of a known earnings release for the symbol.
- Major US holiday eve (Thanksgiving, Christmas, July 4) — half-day
  sessions with thin volume.
- VIX > 30 and rising — defensive only.

## 10. Position Construction (SIGNAL_ONLY only)

US equity signals are **strictly SIGNAL_ONLY** in TradeRay. The
Master Trader should produce executable plans that account for:

- **Gap cushion:** stop = entry ∓ (1.5–2.0 × ATR + 1 ATR gap cushion).
- **Avoid pre-earnings entries:** if the chart shows a recent earnings
  gap, the next earnings is ~3 months away — usually safe.
- **Prefer limit entries** over market — US spreads are tight but slippage
  on the open / close is real.
- **Default holding horizon for SHORT_TERM:** 1–5 sessions.
- **R:R minimum 1.5; aim for 2.0+** as in crypto. Tighter R:R is
  acceptable in calm low-VIX regimes; wider required in high-VIX.
