# BIST Strategy Reference (Borsa İstanbul, .IS tickers)

> **Status: REFERENCE, NOT DOGMA.** BIST is a non-USD-denominated emerging
> market with structural inflation and FX overlays that 99% of US-centric
> playbooks do not account for. Harmonize these patterns with the Quant
> Analyst report and the chart you actually see — but never apply pure SMC
> or ICT templates without translating them to a TL-denominated, single-
> session, capital-controls-adjacent regime.

---

## 1. Market Microstructure

- **Single session:** ~10:00–18:00 Istanbul (TRT). Pre-open auction at 09:55.
  Post-close auction at 18:00. Overnight gap risk is **real and frequent** —
  any stop sized only on intraday ATR is incomplete.
- **No futures / options leverage** at the retail layer for most names; the
  pool is essentially long-only equity. Short squeezes are less common than
  in the US, but illiquid micro-caps can still gap dramatically.
- **Tick size and lot:** smaller than US equities; `quantityPrecision` from
  yfinance reflects whole shares — assume 1-share granularity.
- **Holiday calendar:** matches Turkish public holidays (Cumhuriyet Bayramı,
  Kurban Bayramı, Ramazan Bayramı) — extra gap risk around long weekends.

## 2. The TL Macro Overlay (the single most important factor)

Turkish equities trade on a **TL-denominated price** but their fundamentals
are partly USD-denominated. This creates a mechanical relationship that
overrides most technical signals on weekly+ timeframes:

- **TL weakening (USDTRY rising):** export-heavy industrials, refiners,
  airlines, banks with USD assets, and gold/mining names *benefit*. Prices
  often ramp purely on FX translation. Tickers to watch: **THYAO, TUPRS,
  FROTO, EREGL, KOZAL, KOZAA, PGSUS, ARCLK**.
- **TL strengthening (rare):** importers and pure-domestic plays benefit.
  Tickers: **BIMAS, MGROS, AEFES**.
- **TCMB rate decisions:** policy rate hikes are TL-supportive on
  announcement, banks (NIM expansion) often rally short-term but the
  broader index can sell off on growth fears. Cuts are TL-bearish and
  inflate the index nominally — distinguish between *real* gains and
  *nominal* TL-translation gains.

## 3. Inflation-Hedged Rallies (the structural bull driver)

In a high-CPI regime (Türkiye sustained 40–80% YoY in recent cycles), BIST
acts as an inflation hedge for domestic capital. This produces:

- **Persistent nominal uptrends** that look like bubbles on log charts but
  are partially explained by money supply expansion.
- **Mean reversion plays often fail** in the prevailing trend — RSI 70+ on
  daily can persist for months without a meaningful pullback.
- **Real (CPI-adjusted) returns are the truer signal.** A nominal +3% day
  in a 0.3% daily inflation regime is +2.7% real — still good, but not
  a free 3% alpha.

**Practical implication for Master Trader:**
- Treat overbought RSI on BIST with more tolerance than on US equities.
- Be wary of shorting strong nominal trends without a clear catalyst.

## 4. Sector Behavior

- **Banks (GARAN, AKBNK, ISCTR, YKBNK, HALKB, VAKBN):** trade on rate
  policy + asset quality + currency. Lead the index on policy days.
- **Holdings (KCHOL, SAHOL):** index-correlated, lower beta, TL-translated
  earnings.
- **Industrial / export (THYAO, TUPRS, FROTO, EREGL):** USD-revenue,
  benefit from TL weakness, hurt by global recession fears.
- **Telcos (TCELL):** defensive, dividend-yielding, low volatility.
- **REITs / Construction (EKGYO):** rate-sensitive, often a leveraged play
  on housing-policy decisions.

## 5. Localized Momentum Patterns

- **Pre-CBRT (TCMB) decision drift:** the index often drifts in the
  expected policy direction in the 2–3 days before a meeting. Fade only
  with a strong technical reason.
- **Earnings season ("bilanço dönemi"):** roughly Feb/Mar (FY), May (Q1),
  Aug (Q2/H1), Nov (Q3). Avoid swing trades within 2 days of a known
  release — surprises are common and gaps are large.
- **Weekly close above prior weekly high** in a high-CPI regime is a
  high-conviction continuation signal.
- **Friday afternoon weakness** is common (weekend FX risk-off).

## 6. SMC / ICT translation to BIST

Standard SMC concepts work but with caveats:

- **Order blocks:** valid at daily/weekly levels. Intraday OBs on 5m/15m
  are often noise due to lower turnover.
- **Liquidity sweeps:** less algo-driven than US equities or crypto, so
  textbook sweeps are rarer. When they do appear (usually on opening
  auction or on big-flow days), they are high quality.
- **BOS / CHoCH:** valid on 4h+. Don't trade structure breaks on 5m.

## 7. Volatility Regimes (asset-class adjusted)

Equities are less volatile than crypto. Use these `atr_pct` thresholds:

- **Low (<0.5%):** range / mean reversion preferred.
- **Normal (0.5–1.5%):** standard playbook.
- **Elevated (1.5–3%):** widen stops, reduce size. Often a TCMB / FX news
  day; wait for the dust to settle.
- **Extreme (>3%):** pure macro shock — almost always WAIT. The post-
  shock day-2 reaction is the more tradeable setup.

## 8. WAIT Triggers Specific to BIST

- Within 24h of a TCMB rate decision (and 2 hours after the announcement).
- Within 24h of a known CPI / IMF / S&P / Moody's announcement on Türkiye.
- Daily volume in the bottom 25% of the trailing 20-day average — thin
  tape, manipulation risk.
- Stock has gapped >3% on the open and the daily ATR is already exhausted
  — chasing the gap is a coin flip.

## 9. Position Construction (SIGNAL_ONLY only)

BIST is **strictly SIGNAL_ONLY** in TradeRay — no broker integration.
The Master Trader should still produce a precise plan that a human
operator could execute, with these adjustments:

- **Stop sizing:** add ~1 ATR cushion to account for overnight gaps.
- **Holding period:** prefer multi-day positions — TL volatility makes
  intraday scalping a tax/spread loss for retail. Default holding
  horizon for a SHORT_TERM signal: 3–7 sessions.
- **Take-profit:** prefer trailing stops behind weekly lows/highs over
  fixed limit targets — strong nominal trends overshoot textbook
  resistance regularly.
