# TradeRay — Karar Algoritması (v2.5, literatür-temelli — Phase 3 tamamlandı)

> **Bu dosya: bot şu anda trade ve sinyali NEYE göre üretir?**
>
> - v1: tek-evrensel-kural (deprecate).
> - v2 (Mayıs 2026): market × term parameter matrisi + 3 bias (TF/MR/HYB) + 2-TF
>   alignment + ADX rejim filtresi + volume gate + Connors RSI(2).
> - **v2.5 (bu sürüm):** Phase 3 eklendi — VIX/FOMC/TCMB/Earnings/Funding
>   gate'leri + vol-targeting position sizing + Chandelier trailing exit
>   + walk-forward backtest harness.

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
4. Macro-lite snapshot: VIX/DXY/curve (US), USDTRY (BIST), funding (CRYPTO)
5. Earnings calendar lookup (US — yfinance + 24h Redis cache)
6. Rule Engine pre-gate (VIX/FOMC/TCMB/Earnings/USDTRY) → veto VEYA size mult
7. Rule Engine bias dispatch — market × term policy → LONG / SHORT / WAIT
8. Vol-targeting size mult uygula (gate mult × vol mult, [0.5, 1.5] clamp)
9. (Opsiyonel, use_ai=True ve setup varsa) AI Layer → final decision
10. Engine route → strict TP/SL gate → DB / borsa
11. (OPEN trade'ler için) Chandelier trailing — 30dk'da bir SL ratchet-up
12. DecisionAudit'e tam düşünce zinciri yaz
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

## 8. Phase 3 eklemeleri (v2.5 — tamamlandı)

### 8.1 Makro / takvim gate'leri — `agents/rule_engine.py:_evaluate_gates`

Bias logic'ten **önce** çalışır. Sonuç: `(allow: bool, size_mult: float, reason: str)`.

| Gate | Tetik | Sonuç | Atıf |
|---|---|---|---|
| **VIX hard** (US) | VIX ≥ 35 | veto | Whaley 2000/2009 🎓 |
| **VIX soft** (US) | VIX ≥ 25 | size ×0.5 | Whaley 2000/2009 🎓 |
| **FOMC blackout** (US) | 14:00 ET FOMC günü → 16:00 ET ertesi gün | veto | Lucca-Moench 2015 🏛 (NY Fed) |
| **Earnings blackout** (US) | ±1 trading day | veto | Bernard-Thomas 1989 🎓 (PEAD) |
| **TCMB PPK** (BIST) | 13:00–17:00 TR — listelenen tarih | veto | TCMB resmi 2026 takvimi |
| **USDTRY hard move** (BIST) | \|Δ%\| ≥ 2% (günlük) | size ×0.5 | IMF EM coupling lit. |

Takvim verisi: `config/calendars.py` — TCMB_MPC_2026 + FOMC_2026 elle bakımlı sabit
listeler (yılda bir güncellenir, kaynak: TCMB / Federal Reserve resmi sayfaları).

Earnings: `data_fetchers/earnings_fetcher.py` — yfinance `Ticker.calendar`,
sembol başına 24h Redis cache, ağ/parse hatasında "no blackout" (fail-open).

### 8.2 Crypto funding rate ekstrem gate

`generate_rule_decision` içinde, bias dispatch'ten önce:

```
if market == CRYPTO and |annualized_funding| > 50%:
    if effective_bias == "TF":
        effective_bias = "MR"     # crowded book → fade
```

Atıf: Glassnode / Coinglass funding-rate aşırılıkları → long-squeeze /
short-squeeze gözlemleri 🛠. Sezgisel olarak da: pozisyon yoğunluğu zaten
trend yönündeyse trend takipçisi geç katılır, daha çok zarar görür.

### 8.3 Vol-targeting position sizing — `_vol_targeted_multiplier`

`TermParams.vol_target_annual` set edilirse:

```
realized_vol ≈ ATR_pct × √periods_per_year       # coarse annualization
mult         = clamp(vol_target / realized_vol, 0.5, 1.5)
```

Atıf: AQR / Harvey & Hoyle & Korgaonkar & Rattray & Van Hemert 2018 🎓.
Default'ta `vol_target_annual=None` (vol-targeting devre dışı). Phase 4'te
walk-forward'tan sonra per-(market, term) hedef vol açılacak.

**Kombine sizing:** `final_mult = gate_size_mult × vol_mult`. `position_size_base`,
`position_notional_usd`, `risk_usd` bu çarpanla ölçeklenir; entry/TP/SL/R:R
aynı kalır (risk dolar miktarı değişir, fiyat hedefleri değil).

### 8.4 Chandelier trailing exit — `execution/tracker.py:update_chandelier_stops`

`signals.term ∈ {SHORT_TERM, MID_TERM}` olan OPEN crypto trade'ler için
30 dakikada bir çalışır:

```
LONG : new_sl = HighestHigh(post_entry) − 3 × ATR(22)
SHORT: new_sl = LowestLow(post_entry)   + 3 × ATR(22)
```

**Sadece ratchet-up** (LONG için sadece yükselt; SHORT için sadece düşür) —
genişletilemez. Eski SL Binance'da iptal edilir, yeni `STOP_MARKET` +
`closePosition=True` yerleştirilir; idempotent `client_order_id` ile.

Interval: MID_TERM → 1d, SHORT_TERM → 4h (rule engine'in onay TF'siyle aynı
seviye). Atıf: Chuck LeBeau "Chandelier Exit" — `Bulletin: Trading Stops`
1990s, Carver 2015 §10 🛠 ("vol-adjusted trailing exits").

### 8.5 Walk-forward backtest harness — `backtest/`

```
python -m backtest BTCUSDT CRYPTO MID_TERM 2024-01-01 2026-01-01 [--n-trials N]
```

Üç katmanlı güvenlik (kod referansları `backtest/walk_forward.py` + `stats.py`):

1. **Walk-forward replay** (Pardo 1992 🛠) — bar `t`'de yalnızca `≤ t` görür,
   TP/SL `t+1`'in high/low'unda çözülür, **aynı bar belirsizliğinde SL kazanır**
   (konservatif).
2. **Bootstrap permutation** (Aronson 2006 🛠) — return'lerin sign-flip
   resampling'i ile null distribution, p-value döndürür.
3. **Deflated Sharpe Ratio** (López de Prado 2018 🎓 / Bailey-LdP 2014) —
   `n_trials` parametresi multi-testing düzeltmesi yapar, `P[SR_true > 0]` döndürür.

CLI output (tek satır): trades / win-rate / avg-R / Sharpe(ann) / p-value / DSR.
İsteğe bağlı `--csv` ile per-trade rows.

### 8.6 Hâlâ açık (Phase 4 — sonraki büyük adım)

- ❌ **Regime-switching model** (Hamilton 1989 🎓 Markov 2-state) — yüksek-vol vs
  düşük-vol mode geçişleri. Şu an ADX kaba bir trend/range proxy'si; HMM ile
  rejim olasılığı bayesyen olarak güncellenir.
- ❌ **Walk-forward sonucu parametre tuning** — backtest var, sweep yok. Phase
  4'te `n_trials >> 1` ile parametre grid'i + DSR seçimi.
- ❌ **BIST earnings calendar** — yfinance `.IS` için earnings güvenilmez;
  KAP RSS / Foreks API entegrasyonu Phase 4.
- ❌ **TCMB MPC takvimi otomatik güncelleme** — şu an `config/calendars.py`
  elle bakımlı. Yılda bir TCMB calendar PDF'inden cron'la sync ideal.

---

## 9. Konfigürasyon — geliştirici için harita

| Dosya | Parametre |
|---|---|
| `agents/rule_engine.py` | `CRYPTO_PARAMS`, `EQUITY_US_PARAMS`, `BIST_PARAMS` — her hücre `TermParams` dataclass'ı; `_evaluate_gates`, `_vol_targeted_multiplier` |
| `config/calendars.py` | `TCMB_MPC_2026`, `FOMC_2026` — elle bakımlı yıllık takvimler + blackout fonksiyonları |
| `data_fetchers/earnings_fetcher.py` | yfinance earnings + Redis 24h cache, ±1d PEAD blackout |
| `data_fetchers/technicals.py` | `DEFAULT_LOOKBACKS` (RSI/MACD/BB/ATR/ADX/EMA/volume_ma periyotları) |
| `data_fetchers/market_fetcher.py` | `INDICATOR_LOOKBACKS` (interval başına override) + VIX/DXY/USDTRY fetcher |
| `execution/risk_manager.py` | TP/SL ordering, R:R min (1.5), leverage cap |
| `execution/binance_executor.py` | `replace_stop_loss` — Chandelier ratchet için SL cancel + re-place |
| `execution/tracker.py` | `update_chandelier_stops` (30dk job) |
| `scheduler/jobs.py` | `CHANDELIER_INTERVAL_SECONDS=1800`, market-cycle cadence, daily digest |
| `backtest/` | Walk-forward replay + bootstrap + DSR; `python -m backtest …` |
| `config/settings.py` | `MAX_RISK_PCT`, `DEFAULT_LEVERAGE`, `PORTFOLIO_NOTIONAL` |
| `agents/prompts.py` | LLM system prompt'ları |
| `rules/*.md` | Market-spesifik kural kitabı (Master Trader prompt'una enjekte) |

---

## 10. Akademik atıflar

🎓 **Peer-reviewed:**
- Jegadeesh & Titman (1993) *JoF* — cross-sectional momentum
- Asness, Moskowitz & Pedersen (2013) *JoF* — value & momentum everywhere
- Moskowitz, Ooi & Pedersen (2012) *JFE* — time-series momentum
- Brock, Lakonishok & LeBaron (1992) *JoF* — MA crossover validation
- Sullivan, Timmermann & White (1999) *JoF* — data-snooping correction
- Lo, Mamaysky & Wang (2000) *JoF* — TA foundations
- Bernard & Thomas (1989) *J Acct Res* — PEAD (earnings ±1d blackout)
- Whaley (2000, 2009) *JPM* — VIX yapısı + signal use cases
- Hamilton (1989) *Econometrica* — Markov regime-switching
- Lo (2004) *JPM* — Adaptive Markets Hypothesis
- Harvey, Hoyle, Korgaonkar, Rattray, Van Hemert (2018) *JPM* — vol targeting in TF
- Bailey & López de Prado (2014) *Journal of Risk* — Deflated Sharpe
- López de Prado (2018) — *Advances in Financial Machine Learning* (backtest pitfalls)

🏛 **Institutional / official:**
- Lucca & Moench (2015) NY Fed — FOMC pre-announcement drift
- TCMB — PPK 2026 resmi takvimi
- Federal Reserve — FOMC 2026 resmi takvimi

🛠 **Practitioner classics:**
- Wilder (1978) — RSI, ATR, ADX/DMI orijinal kaynak
- Connors & Alvarez (2008) — RSI(2) mean-reversion
- LeBeau (1990s) — Chandelier Exit (trailing stop)
- Carver (2015) — *Systematic Trading*
- Tharp (2008) — position sizing
- Bollinger (2001) — Bollinger Bands
- Aronson (2006) — *Evidence-Based Technical Analysis* (bootstrap)
- Pardo (1992) — *The Evaluation and Optimization of Trading Strategies* (walk-forward)
- Glassnode / Coinglass — funding-rate ekstrem gözlemleri

---

> **Canlı belge.** Her parameter tweak'i sonrası burayı güncelle. Commit hash
> referansı: `git log -1 --pretty=format:'%h %s'`.
