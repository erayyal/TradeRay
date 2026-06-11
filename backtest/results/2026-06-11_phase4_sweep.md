# Phase 4 parameter sweep — 2026-06-11

```
python -m backtest.sweep BTCUSDT,ETHUSDT,SOLUSDT CRYPTO MID_TERM  2024-01-01 2026-06-10 --biases TF,MR   --n-bars 1500
python -m backtest.sweep BTCUSDT,ETHUSDT,SOLUSDT CRYPTO SHORT_TERM 2025-06-01 2026-06-10 --biases HYB,MR --n-bars 1500
```

432 combos per term (grid: atr_sl_mult × rr_target × adx_min × rel_volume_min
× bias [× MR-RSI]); DSR computed with `n_trials=432` (full multiple-testing
penalty, Bailey-LdP 2014). Walk-forward replay, SL-wins-ties, single-TF.
Pooled R-multiples across the 3 symbols.

Not modeled: makro/takvim gate'leri, vol-targeting, portfolio guard, fees/
slippage. Fees ~0.05%/side taker Binance perp ≈ 0.04R @1.5×ATR(1d) stop —
avg_R +0.31..+0.45 sonuçları bunu fazlasıyla karşılıyor.

## MID_TERM (1d, 2024-01 → 2026-06, ~891 bar)

| rank | bias | atr | rr | adx | rvol | rsi | n | win% | avgR | totR | Sharpe | p | DSR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | TF | 1.5 | 2.0 | 20 | 0.8 | 45/55 | 110 | 48.2% | +0.45 | +49.0 | +2.96 | 0.002 | **0.545** |
| 2 | TF | 2.5 | 1.5 | 20 | 1.0 | 45/55 | 62 | 61.3% | +0.53 | +33.0 | +4.34 | 0.001 | 0.531 |
| 3 | TF | 2.5 | 2.0 | 20 | 1.0 | 45/55 | 62 | 53.2% | +0.60 | +37.0 | +3.95 | 0.003 | 0.503 |

- Seçilen: **rank 1** — per-symbol: BTC +14R / ETH +10R / SOL +25R (hepsi pozitif).
- Plato: aynı (bias, adx, rvol, rsi) ile 12/12 atr×rr komşusu pozitif.
- Bias dağılımı: TF 80/108 pozitif; MR 60/324, en iyi MR DSR 0.053 → daily'de MR reddedildi (Phase 4-b sorusu kapandı).
- v2.6 smoke testindeki kayıp (win 10% × rr 3.0) → rr 3.0 + sıkı gate kombinasyonuydu; sweep rr 2.0 + adx 20 + rvol 0.8'i seçti.

## SHORT_TERM (4h, 2025-06 → 2026-06, ~1494 bar)

| rank | bias | atr | rr | adx | rvol | rsi | n | win% | avgR | totR | Sharpe | p | DSR |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HYB | 2.0 | 1.5 | 20 | 0.8 | 40/60 | 241 | 52.3% | +0.31 | +74.0 | +2.45 | 0.000 | **0.774** |
| 2 | HYB | 1.5 | 2.5 | 20 | 0.8 | 40/60 | 241 | 39.0% | +0.37 | +88.0 | +2.13 | 0.001 | 0.679 |
| 3 | HYB | 2.0 | 2.0 | 20 | 0.8 | 40/60 | 241 | 43.6% | +0.31 | +74.0 | +2.06 | 0.001 | 0.606 |

- Seçilen: **rank 1** — per-symbol: BTC +11.5R / ETH +26R / SOL +36.5R.
- Plato: 11/12 komşu pozitif; HYB 69/108 pozitif. MR 4h'da ölü (3/324).

## Uygulanan değişiklik (rule_engine.py v2.9, CRYPTO)

| Param | SHORT_TERM eski→yeni | MID_TERM eski→yeni |
|---|---|---|
| atr_sl_mult | 1.5 → **2.0** | 2.0 → **1.5** |
| rr_target | 2.0 → **1.5** | 3.0 → **2.0** |
| adx_min_for_trend | 22 → **20** | 22 → **20** |
| rel_volume_min | 1.0 → **0.8** | 0.9 → **0.8** |

US/BIST parametrelerine dokunulmadı (sweep crypto verisiyle yapıldı; equity
sweep'i yfinance 4h kısıtları nedeniyle ayrı çalışma ister).

## AUTO_BOT kararı (§11.5)

DSR>0.5 şartı artık sağlanıyor (1/3). Kalan şartlar: ≥2 hafta SIGNAL-only
canlı izlemede pozitif total-R + cost budget uyumu. **Şimdilik SIGNAL-only
devam** — canlı veri 2026-06-10 sıfırlamasından itibaren birikmekte.

Ham sonuçlar: sunucuda `traderay-backend:/tmp/sweep_mid.json` + `/tmp/sweep_short.json`.
