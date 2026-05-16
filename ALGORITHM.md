# TradeRay — Karar Algoritması (v2, literatür-temelli)

> **Bu dosya: bot şu anda trade ve sinyali NEYE göre üretir?**
> 14 Mayıs 2026'da `RESEARCH_ALGORITHM.md` araştırması yapıldı; bu dosya o
> araştırmanın uygulanmış halidir. Önceki sürüm (`v1`, tek-evrensel-kural)
> deprecate edildi.

> 🎓 = peer-reviewed academic | 🏛 = kurumsal araştırma | 🛠 = uygulayıcı

---

## 0. Mimari özet

**Dual-core** + **market × term parameter matrisi**:

1. **Rule Engine** (`agents/rule_engine.py` v2) — deterministik, free.
   - Her (market × term) kombinasyonunun **kendi bias'ı + eşikleri + ATR çarpanları + risk %'si** var.
   - 3 mod: **TF** (trend-following), **MR** (mean-reversion, Connors-stil), **HYB** (ADX-gated).
2. **AI Verification Layer** — Anthropic Opus 4.7 (opsiyonel, per-market toggle).
   - Sadece Rule Engine LONG/SHORT döndürürse çağrılır. WAIT'te 0 token.
   - Master Trader rule_decision'ı **doğrular / değiştirir / reddeder**; market-spesifik rulebook prompt'a enjekte edilir.

---

## 1. Veri akışı (cycle başı)

```
1. Symbol seç (Dynamic Screener — top 5 by 24h volume / daily move)
2. OHLCV çek (Binance / yfinance)
3. TA-Lib indikatörleri hesapla (RSI/RSI(2)/MACD/BB/ATR/ADX/EMA/Vol-MA)
4. Rule Engine — market × term policy table → LONG / SHORT / WAIT
5. (Opsiyonel, use_ai=True ve setup varsa) AI Layer → final decision
6. Engine route → strict TP/SL gate → DB / borsa
7. DecisionAudit'e tam düşünce zinciri yaz
```

---

## 2. Rule Engine v2 — market × term parameter matrisi

Konum: `agents/rule_engine.py:PARAMS_TABLE` (her hücre `TermParams` dataclass'ı).

### 2.1 CRYPTO (Binance USDT-M perpetuals)

| Term | Sinyal TF / Onay | Bias | RSI(period) eşikleri | ATR SL × | R:R | Risk | Lev | Vol gate | Atıf |
|---|---|---|---|---|---|---|---|---|---|
| **SCALP** | 15m / 1h | **MR** | RSI(2): ≤10 LONG, ≥90 SHORT | 1.0 | 1.5 | 2% | 3× | 1.2× | Connors-Alvarez 2008 🛠 |
| **SHORT_TERM** | 4h / 1d | **HYB** | RSI(14): ≤35 / ≥65 | 1.5 | 2.0 | 2% | 3× | 1.2× | Wilder DMI 🛠 + MOP 2012 🎓 |
| **MID_TERM** | 1d / — | **TF** | RSI(14): ≤40 / ≥60 + ADX>25 | 2.0 | 3.0 | 2% | 2× | 1.0× | MOP 2012, AMP 2013 🎓 |

### 2.2 SP500 & NASDAQ

| Term | Sinyal TF / Onay | Bias | RSI eşikleri | ATR SL × | R:R | Risk | Lev | Vol gate |
|---|---|---|---|---|---|---|---|---|
| **SCALP** | 15m / 1h | **MR** | RSI(2): ≤5 / ≥95 (Connors original) | 1.0 | 1.5 | 2% | 1× | **1.5×** |
| **SHORT_TERM** | 4h / 1d | **HYB** | RSI(14): ≤35 / ≥65 | 1.5 | 2.0 | 2% | 1× | 1.3× |
| **MID_TERM** | 1d / — | **TF** | RSI(14): ≤40 / ≥60 + ADX>25 | 2.0 | 3.0 | 2% | 1× | 1.0× |

### 2.3 BIST (.IS)

| Term | Sinyal TF / Onay | Bias | RSI eşikleri | **ATR SL ×** | R:R | **Risk** | Lev | Vol gate |
|---|---|---|---|---|---|---|---|---|
| **SCALP** | 15m / 1h | MR | RSI(2): ≤10 / ≥90 | **2.0** | 1.5 | **1.5%** | 1× | 1.5× |
| **SHORT_TERM** | 4h / 1d | HYB | RSI(14): ≤35 / ≥65 | **2.0** | 2.0 | **1.5%** | 1× | 1.3× |
| **MID_TERM** | 1d / — | TF | RSI(14): ≤40 / ≥60 + ADX>25 | **2.5** | 3.0 | **1.5%** | 1× | 1.0× |

> **Neden BIST'te daha geniş stop + düşük risk?** TR equity'leri gap riski + mid/small-cap likidite + TCMB/USDTRY makro vol → AQR-style vol-targeting mantığı, IMF EM coupling literatürü.

---

## 3. Karar mantığı — bias'a göre

### 3.1 MR (Mean-Reversion) mode — SCALP'in tek modu, HYB'in ranging dalı

Tüm koşullar primary interval'da:

```
LONG:
  RSI(rsi_period) ≤ rsi_long_max          # Connors RSI(2)<10 ya da Wilder RSI(14)<30
  AND  BB_position ≤ 0.2                  # fiyat alt banda yakın
  AND  rel_volume ≥ rel_volume_min        # hacim onayı
  AND  confirm_TF (1 üst TF) EMA üstünde  # daha yüksek TF trendde
```
SHORT mirror.

### 3.2 TF (Trend-Following) mode — MID_TERM

```
LONG:
  price > EMA_slow                        # BLL 1992 trend filtresi
  AND  MACD_hist > 0                      # momentum onayı
  AND  ADX ≥ adx_min_for_trend (=25)      # rejim trend (Wilder DMI)
  AND  rel_volume ≥ rel_volume_min
  AND  RSI NOT overbought (anti-FOMO)     # extreme RSI = pullback bekle
```
SHORT mirror (price<EMA_slow, MACD_hist<0).

### 3.3 HYB (Hybrid, ADX-gated) — SHORT_TERM

```
if ADX ≥ 25:                              # TRENDING regime
    pull-back-in-trend (RSI pullback + EMA filter + confirm TF)
elif ADX ≤ 20:                            # RANGING regime
    BB mean-reversion (MR evaluator)
else:                                     # TRANSITIONAL (20<ADX<25)
    WAIT                                  # Wilder folklor + practitioner consensus
```

---

## 4. Risk planı (her bias için)

```
risk_per_unit  = atr_sl_mult × ATR(14)
max_risk_usd   = PORTFOLIO_NOTIONAL × risk_pct  (CRYPTO/US=2%, BIST=1.5%)
size_base      = max_risk_usd / risk_per_unit
notional_usd   = entry × size_base
TP             = entry ± rr_target × risk_per_unit
```

`engine.route()` **strict TP/SL gate**'i — entry/TP/SL'den biri eksikse decision **DB'ye yazılmaz**, borsaya gitmez.

---

## 5. Multi-timeframe alignment — KISITLI

Eski v1: "tüm interval'ler EMA tarafında aynı olmalı" → likely curve-fit.

**v2:** sadece **2 timeframe** (sinyal TF + onay TF), oran ~4-6×.

| Term | Sinyal | Onay |
|---|---|---|
| SCALP | 15m | 1h |
| SHORT_TERM | 4h | 1d |
| MID_TERM | 1d | (standalone) |

Onay TF: yalnızca EMA-slow pozisyonu kontrol edilir (yön süzgeci). Daha karmaşık alignment kuralları PHASE 3'e bırakıldı (overfitting riski).

---

## 6. AI Verification Layer (use_ai=True iken)

Rule Engine LONG/SHORT döndüğünde devreye girer. 3 paralel/sıralı LLM çağrısı:

| Ajan | Görev | Input |
|---|---|---|
| **Quant Analyst** | Salt matematiksel TA değerlendirmesi | Tüm interval'lerin indikatör snapshot'ları |
| **Sentiment Scanner** | News + macro + on-chain sentez | RSS news, FRED macro, DefiLlama |
| **Master Trader** | Karar: doğrula / değiştir / reddet | quant + sentiment + microstructure + chart (conf≥70) + **market-spesifik rulebook** |

**Master Trader yetkileri:**
- Rule setup'ı **WAIT'e çevirebilir** (chart contradicts numerical narrative).
- Entry/TP/SL'yi **kendi planına göre değiştirebilir**.
- Pending limit order'ı **iptal edebilir** (`CANCEL_PENDING`).

**Token ekonomisi:** chart sadece `rule.confidence ≥ 70` ise gönderilir (~3K ekstra input token). Rule WAIT iken **LLM hiç çağrılmaz**.

---

## 7. v1 → v2 farkları

| Konu | v1 | v2 (literatür-temelli) |
|---|---|---|
| Tek ruleset her markete | ✓ | ❌ Market × term matrisi |
| RSI eşiği | 40 / 60 (ampirik dayanak yok) | SCALP: RSI(2) 10/90 (Connors); SHORT/MID: RSI(14) 30-35/65-70 (Wilder) |
| Multi-TF alignment | Tüm interval'ler agree | Sinyal TF + 1 onay TF |
| Mod ayrımı | Yok (universal rule) | TF / MR / HYB — ADX rejim filtresi |
| Volume confirmation | ❌ Yok | ✓ `rel_volume ≥ threshold` |
| ATR SL/TP | 1.5×/3.0× sabit (R:R=2) | Term'a göre 1.0–2.5×, R:R 1.5–3.0 |
| BIST risk farklı mı | ❌ Hayır | ✓ %1.5/trade, daha geniş ATR |
| ADX rejim filtresi | ❌ Yok | ✓ TRENDING/RANGING/TRANSITIONAL |
| Connors RSI(2) | ❌ | ✓ SCALP için |

---

## 8. Hâlâ eksikler (Phase 3 — sonraki büyük adım)

- ❌ **Backtest framework** — `RESEARCH_ALGORITHM.md` §7'in tüm önerileri: walk-forward (Pardo), bootstrap permutation (Aronson), Deflated Sharpe (LdP 2014). Bu yapılmadan parametre değişiklikleri canlıda valide edilemez.
- ❌ **Earnings calendar guard** (US) — Bernard-Thomas PEAD. Şu an Master Trader prompt'unda nüans olarak var, kodda gate yok.
- ❌ **TCMB MPC takvimi** (BIST) — 13:00–17:00 PPK günü filtresi yok.
- ❌ **VIX rejim gate** (US) — VIX>25 size halve, >35 veto. Şu an prompt seviyesinde.
- ❌ **Crypto funding rate ekstrem gate** — payload'da var (microstructure), Master Trader görür, ama Rule Engine'de değil.
- ❌ **Chandelier trailing exit** (MID_TERM) — sabit R:R yerine trailing.
- ❌ **Vol-targeting position sizing** — sabit %2 yerine, asset'in 30-gün realized vol'una ters orantılı.
- ❌ **Regime-switching model** (Markov 2-state) — yüksek-vol vs düşük-vol mode geçişleri.

---

## 9. Konfigürasyon — geliştirici için harita

| Dosya | Parametre |
|---|---|
| `agents/rule_engine.py` | `CRYPTO_PARAMS`, `EQUITY_US_PARAMS`, `BIST_PARAMS` — her hücre `TermParams` dataclass'ı |
| `data_fetchers/technicals.py` | `DEFAULT_LOOKBACKS` (RSI/MACD/BB/ATR/ADX/EMA/volume_ma periyotları) |
| `data_fetchers/market_fetcher.py` | `INDICATOR_LOOKBACKS` (interval başına override) |
| `execution/risk_manager.py` | TP/SL ordering, R:R min (1.5), leverage cap |
| `config/settings.py` | `MAX_RISK_PCT`, `DEFAULT_LEVERAGE`, `PORTFOLIO_NOTIONAL` |
| `agents/prompts.py` | LLM system prompt'ları |
| `rules/*.md` | Market-spesifik kural kitabı (Master Trader prompt'una enjekte) |

---

## 10. Akademik atıflar (kısa liste — tam liste `RESEARCH_ALGORITHM.md` §11'de)

🎓 **Peer-reviewed:**
- Jegadeesh & Titman (1993) *JoF* — cross-sectional momentum
- Asness, Moskowitz & Pedersen (2013) *JoF* — value & momentum everywhere
- Moskowitz, Ooi & Pedersen (2012) *JFE* — time-series momentum
- Brock, Lakonishok & LeBaron (1992) *JoF* — MA crossover validation
- Sullivan, Timmermann & White (1999) *JoF* — data-snooping correction
- Lo, Mamaysky & Wang (2000) *JoF* — TA foundations
- Bernard & Thomas (1989) *J Acct Res* — PEAD
- Whaley (2000, 2009) *JPM* — VIX
- Lo (2004) *JPM* — Adaptive Markets Hypothesis
- López de Prado (2018) — backtest pitfalls / Deflated Sharpe

🛠 **Practitioner classics:**
- Wilder (1978) — RSI, ATR, ADX/DMI orijinal kaynak
- Connors & Alvarez (2008) — RSI(2) mean-reversion
- Carver (2015) — *Systematic Trading*
- Tharp (2008) — position sizing
- Bollinger (2001) — Bollinger Bands

---

> **Canlı belge.** Her parameter tweak'i sonrası burayı + `RESEARCH_ALGORITHM.md`'yi
> güncelle. Commit hash referansı: `git log -1 --pretty=format:'%h %s'`.
