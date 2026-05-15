# TradeRay — Karar Algoritmaları (Algorithm Reference)

> Bu dosya: bot **şu anda** trade veya sinyali neye göre üretiyor?
> Hem AI kapalı (saf rule engine) hem AI açık (rule engine + LLM verification)
> modlarının davranışı, formülleri ve market başına farklılıkları aşağıda.

> Amaç: Gemini ile geliştirme planlamak için **mevcut durumun net referansı**.

---

## 0. Sistem mimarisi tek paragrafta

TradeRay **dual-core** bir karar motoru kullanır:

1. **Rule Engine** (saf TA-Lib, deterministik, ücretsiz, hızlı).
2. **AI Verification Layer** (Anthropic Claude — opsiyonel, market başına aç/kapat).

Her sembol için her cycle:
- Önce Rule Engine çalışır.
- WAIT döndürürse → durur. **LLM hiç çağrılmaz.**
- LONG/SHORT döndürürse:
  - `use_ai = False` → Rule Engine kararı **direkt** karardır.
  - `use_ai = True` → Master Trader LLM doğrular/reddeder/değiştirir.

Bu yüzden her cycle'ın maliyeti şudur:

| Senaryo | Maliyet |
|---|---|
| Rule WAIT, AI off | 0 token, ~1-2 sn |
| Rule WAIT, AI on | 0 token (LLM çağrılmaz) |
| Rule setup, AI off | 0 token |
| Rule setup, AI on | 3 LLM çağrısı (Quant + Sentiment + Master) |

---

## 1. Veri akışı (her cycle başı)

Her sembol için sıralı şekilde çalışır:

```
1. Symbol seç (Dynamic Screener — top 5 by 24h volume / daily move)
2. OHLCV çek (Binance / yfinance)
3. TA-Lib indikatörleri hesapla
4. Rule Engine çalıştır → LONG / SHORT / WAIT
5. (Opsiyonel) AI Layer → LONG / SHORT / WAIT / CANCEL_PENDING
6. Engine route → DB'ye Signal yaz / borsaya emir gönder
7. DecisionAudit'e tam düşünce zinciri kaydet
```

### 1.1. Term → Primary Interval

Karar verici timeframe (Rule Engine ve LLM'in odaklandığı):

| Term | Primary Interval | Yardımcı Interval'lar |
|---|---|---|
| SCALP | **15m** | 5m + 15m |
| SHORT_TERM | **4h** | 1h + 4h |
| MID_TERM | **1d** | 1d |

`SCALP` 5m'i texture/timing için kullanır, `SHORT_TERM` 1h'i alignment için.

### 1.2. Dynamic TA-Lib Lookback'leri

Interval'e göre indikatör periyotları **otomatik** değişir
(`data_fetchers/market_fetcher.py:INDICATOR_LOOKBACKS`):

| Interval | RSI | MACD | BBands | ATR | EMA fast | EMA slow |
|---|---|---|---|---|---|---|
| 5m | **9** | (8, 21, 5) | 20 | 14 | 21 | 100 |
| 15m | 14 | (12, 26, 9) | 20 | 14 | 50 | 200 |
| 1h | 14 | (12, 26, 9) | 20 | 14 | 50 | 200 |
| 4h | 14 | (12, 26, 9) | 20 | 14 | 50 | 200 |
| 1d | 14 | (12, 26, 9) | 20 | 14 | 50 | 200 |

> 5m hariç hepsinde klasik textbook ayar.

---

## 2. Rule Engine — kesin kararlar

Konum: `agents/rule_engine.py:generate_rule_decision`

### 2.1. Direction gate (kesin kurallar)

Primary interval üzerinde:

**LONG koşulu — HEPSİ aynı anda doğru olmalı:**
- `RSI < 40`
- `MACD histogram > 0`
- `last_close > EMA_slow` (50 veya 200, interval'e göre)
- **Multi-TF alignment bullish**: cycle'daki TÜM interval'lerin `above_ema_slow` değeri True

**SHORT koşulu — mirror:**
- `RSI > 60`
- `MACD histogram < 0`
- `last_close < EMA_slow`
- Multi-TF alignment bearish (hepsinde `above_ema_slow = False`)

**Aksi halde:** `WAIT` — neden WAIT döndüğü `justification`'a yazılır.

### 2.2. Risk planı (TP/SL/sizing)

Setup bulunduğunda **mutlaka** üretilir (yoksa engine reject eder):

```
ATR_SL_MULT  = 1.5     # Stop-loss distance in ATRs
ATR_TP_MULT  = 3.0     # Take-profit distance in ATRs
R:R = ATR_TP_MULT / ATR_SL_MULT = 2.0    (fixed)

LONG:
  entry  = last_close
  stop_loss   = entry − 1.5 × ATR
  take_profit = entry + 3.0 × ATR

SHORT: mirror

risk_per_unit  = |entry − stop_loss|              ( = 1.5 × ATR )
max_risk_usd   = PORTFOLIO_NOTIONAL × MAX_RISK_PCT ( default $10000 × 2% = $200 )
position_size  = max_risk_usd / risk_per_unit
position_notional = entry × position_size
```

Yani **her trade $200 risk taşır**, R:R sabit **2:1**.

### 2.3. Confidence heuristic

```
confidence = 60                                  # baseline
+ 15  if (LONG ∧ RSI < 30)   or   (SHORT ∧ RSI > 70)   # deeper extreme
+ 5   if |MACD_hist| > 0
+ 10  always (alignment gate'i geçtiği için)
cap at 95
```

`confidence ≥ 70` ise AI moduyla chart (vision) gönderilir; altındaysa text-only.

### 2.4. Strict TP/SL gate (zero-tolerance)

`execution/engine.py:route()` içinde — Rule Engine veya AI ne dönerse dönsün:

```python
if action in ("LONG", "SHORT"):
    if take_profit is None or stop_loss is None or entry is None:
        return REJECTED  # DB'ye yazmaz, emir göndermez
```

---

## 3. AI Verification Layer (use_ai=True iken)

Rule Engine `LONG/SHORT` döndürdüğünde devreye girer. 3 ajan paralel/sıralı çalışır:

### 3.1. Quant Analyst

- **Input**: tüm interval'lerin indikatör snapshot'ları (RSI/MACD/BB/ATR/EMA + seri tail'leri), last_close.
- **Çıktı (zorunlu JSON)**:
  - `trend_bias`: bullish / bearish / neutral
  - `trend_strength`: 0.0–1.0
  - `momentum_state`: accelerating_up / decelerating_up / flat / decelerating_down / accelerating_down
  - `key_levels`: { support, resistance }
  - `volatility_state`: low / normal / elevated / extreme
  - `atr_pct`: ATR / last_close
  - `timeframe_alignment`: aligned_bullish / aligned_bearish / mixed / neutral
  - `quant_score`: −1.0 to +1.0
- **Tabu**: haber, sentiment, makro hakkında konuşamaz — sadece matematik.

### 3.2. Sentiment Scanner

- **Input**:
  - News (RSS feed aggregator — CoinDesk, Cointelegraph, Decrypt, The Block, CryptoSlate, Bitcoin Magazine, CryptoPanic RSS)
  - FRED makro (Fed Funds rate, US 10Y, T10Y2Y yield curve, VIX, DXY)
  - DefiLlama on-chain (total TVL, ETH dominance)
- **Mapping kuralları** (prompt'ta zorunlu):
  - VIX > 20 ↑ → **risk_off**
  - T10Y2Y < 0 (inverted curve) → **risk_off lean**
  - DXY ↑ → **risk_off** (özellikle crypto/EM için)
  - Fed cut cycle → **risk_on**
- **Çıktı**:
  - `fear_greed_index`: 0–100
  - `macro_regime`: risk_on / neutral / risk_off
  - `news_catalysts`: max 5 item, impact_tier'a göre sıralı
  - `sentiment_score`: −1.0 to +1.0
- **Tabu**: fiyat tahmini yapamaz, oy saymaz (headline impact > headline count).

### 3.3. Master Trader (Beyin)

Diğer iki ajanın çıktısını + microstructure + chart + market-spesifik rulebook'u birleştirir.

**Input** (multi-block):
- 🖼 IMAGE: candle + volume + EMA(20,50) chart (~120 mum, sadece `rule.confidence ≥ 70` ise)
- 📋 JSON payload:
  - `quant`, `sentiment` raporları
  - `microstructure`: market'e göre
    - **CRYPTO**: funding_rate (8h, annualized), open_interest (base + USD)
    - **BIST**: USDTRY=X daily rate + günlük % değişim
    - **SP500/NASDAQ**: boş (FRED zaten US makroyu veriyor)
  - `pending_order`: bu sembolde unfilled limit varsa
  - `risk_envelope`: portfolio_notional, max_risk_pct, max_leverage
- 📚 SYSTEM PROMPT (market'e göre dinamik):
  - `rules/crypto_strategy.md` ya da `rules/bist_strategy.md` ya da `rules/us_equities_strategy.md` enjekte edilir.

**8 adımlı zorunlu CoT** (prompt'ta):
1. Visual read (chart)
2. Tape fusion (quant + sentiment + microstructure)
3. Vision vs. numbers cross-check
4. Rulebook harmonization
5. Conflict gate
6. Volatility & market-class gate
7. Trade construction (entry/SL/TP)
8. R:R verification (≥ 1.5)
9. Confidence + sanity checks

**Çıktı (strict JSON)**:
- `decision`: LONG / SHORT / WAIT / **CANCEL_PENDING**
- `confidence_level`: 0–100
- `entry_price`, `take_profit`, `stop_loss`, `leverage`
- `position_size_base`, `position_notional_usd`, `risk_usd`, `reward_risk_ratio`
- `vision_confirms_quant`: bool
- `chart_observations`, `rulebook_references`, `conflict_flags`
- `justification`: 2-3 cümle — Rule Engine setup'ına ne yaptığını açıklar

**Master Trader'ın yetkisi:**
- Rule Engine'in LONG/SHORT setup'ını **reddedebilir** (WAIT'e çevirir)
- TP/SL/Entry seviyelerini **değiştirebilir** (kendi planını üretir)
- Pending limit order'ı **iptal edebilir** (`CANCEL_PENDING`)
- Yeni LONG/SHORT karar verebilir (Rule farklı dese bile)

---

## 4. Market başına farklılıklar

### 4.1. Rule Engine — market spesifik DEĞİL ❌

Şu an **aynı 4 kural** tüm marketlerde çalışıyor:
- LONG/SHORT thresholds: RSI 40/60, MACD hist sign, EMA filter, multi-TF alignment
- ATR-based TP/SL
- Crypto/BIST/SP500/NASDAQ farkı yok

**Bilinen eksiklikler:**
- US SCALP'te RTH (09:30-16:00 ET) kontrolü yok
- BIST'te USDTRY makro overlay yok
- Earnings calendar guard yok
- VIX guard yok
- Crypto funding extremes filtresi yok

### 4.2. AI Verification — market spesifik EVET ✅

`build_master_trader_prompt(market)` her market için **farklı rulebook** enjekte ediyor:

| Market | Rulebook | Vurgular |
|---|---|---|
| **CRYPTO** | `rules/crypto_strategy.md` | 24/7 microstructure, funding rate, OI divergence, BTC dominance, liquidity sweeps (ICT-style), Wyckoff schematics, Asia/EU/NY session davranışı, volatility regimes |
| **BIST** | `rules/bist_strategy.md` | Tek seans + gap risk, **TL macro overlay** (USDTRY), inflation-hedged rallies, TCMB politika günleri, bilanço dönemi guard, BIST sektör davranışı (banks/holdings/industrial) |
| **SP500 / NASDAQ** | `rules/us_equities_strategy.md` | RTH only, overnight gap risk, earnings calendar awareness (±2 session avoid), sector rotation by rates regime, **VIX master gauge**, ICT/SMC patterns (FVG, OB, BOS/CHoCH on 4h+) |

**Yani:**
- Crypto + AI off → universal rule engine
- Crypto + AI on → Master Trader, crypto rulebook'la harmonize
- BIST + AI on → Master Trader, BIST rulebook + USDTRY context

---

## 5. CANCEL_PENDING — pending order invalidation

**Sadece AI mode** — Master Trader'ın özel yetkisi.

Rule Engine bunu üretemez (çünkü pending order context yok).

Master Trader bir sembolde unfilled limit order görürse:
- Yapısal invalidation var mı? (Support break, regime flip, eski emir > 12h)
- Varsa → `decision = "CANCEL_PENDING"` döner.
- Orchestrator `tracker.cancel_pending_for_symbol()` çağırır.
- Binance'da emir iptal edilir, Trade row `CANCELED`'a düşer.

**Layer 1 fallback**: 24 saat unfilled kalan limit otomatik iptal (zaman bazlı, AI'dan bağımsız).

---

## 6. Execution & risk gate'leri

`execution/engine.py:route()` katmanları:

1. **Mode coercion**: BIST/SP500/NASDAQ için `AUTO_BOT` → `SIGNAL_ONLY`'e zorla düşürülür. **Sadece CRYPTO** gerçek emir gönderebilir.
2. **Strict TP/SL gate**: LONG/SHORT decision'ında entry/TP/SL eksikse → REJECTED (DB'ye yazılmaz).
3. **Risk Manager** (`execution/risk_manager.py`):
   - LONG ordering: `sl < entry < tp`
   - SHORT ordering: `tp < entry < sl`
   - `risk_usd ≤ portfolio × max_risk_pct` (= %2)
   - `reward_risk_ratio ≥ 1.5`
   - `leverage ≤ DEFAULT_LEVERAGE` (= 3x)
4. **Binance executor** (yalnız Crypto AUTO_BOT):
   - Strict quantization (tickSize/stepSize), MIN_NOTIONAL kontrolü
   - Tenacity retry (429, 5xx, network)
   - Idempotent client_order_id (duplicate → recovery via `futures_get_order`)
   - Bracket: Limit entry (GTC) + Stop-Market SL + Take-Profit-Market TP, hepsi `closePosition=True`

---

## 7. Karar akışı diyagramı

```
                        ┌─────────────────────────────┐
                        │  Scheduler tick (SCALP=5dk, │
                        │  SHORT=1sa, MID=1gün)       │
                        └──────────────┬──────────────┘
                                       ▼
                          ┌────────────────────────┐
                          │ Master switch (Redis)  │
                          │ system_enabled = "1"?  │
                          └──────────┬─────────────┘
                                     │ no → STOP
                                     ▼ yes
                          ┌────────────────────────┐
                          │ MarketConfig.enabled?  │
                          └──────────┬─────────────┘
                                     │ no → SKIP MARKET
                                     ▼ yes
                          ┌────────────────────────┐
                          │ Symbols: screener vs   │
                          │ static symbols_csv     │
                          └──────────┬─────────────┘
                                     ▼
                          ┌────────────────────────┐
                          │ Fetch OHLCV + compute  │
                          │ indicators (free)      │
                          └──────────┬─────────────┘
                                     ▼
                          ┌────────────────────────┐
                          │ Rule Engine            │
                          │ (RSI/MACD/EMA gates)   │
                          └──────────┬─────────────┘
                            ┌────────┴────────┐
                            ▼                 ▼
                        WAIT                LONG/SHORT
                            │                 │
                ┌───────────┘                 ▼
                ▼                  ┌─────────────────────┐
        ┌────────────────┐         │  use_ai?            │
        │ engine.route() │         └──┬──────────────────┘
        │  → WAITED      │            │ no → engine.route() → SIGNAL_SENT or REJECTED
        │  audit log     │            │
        └────────────────┘            ▼ yes
                                ┌─────────────────────────────┐
                                │ Quant + Sentiment (parallel)│
                                │ + microstructure            │
                                │ + chart (if conf ≥ 70)      │
                                └──────────┬──────────────────┘
                                           ▼
                                ┌─────────────────────────┐
                                │ Master Trader           │
                                │ → final LONG/SHORT/WAIT │
                                │   /CANCEL_PENDING       │
                                └──────────┬──────────────┘
                                           ▼
                                  ┌────────────────┐
                                  │ engine.route() │
                                  │ → EXECUTED /   │
                                  │   SIGNAL_SENT/ │
                                  │   REJECTED     │
                                  └────────┬───────┘
                                           ▼
                                  ┌────────────────┐
                                  │ DecisionAudit  │
                                  │ (full trace)   │
                                  └────────────────┘
```

---

## 8. Mevcut sınırlamalar & geliştirme alanları

### Rule Engine eksiklikleri
- ❌ **Volume confirmation yok**: hacim filtresi olmadan breakout/breakdown güvenilmez.
- ❌ **RTH check yok** (US/BIST için): tek-seans market'lerde saat dışı tetiklenmemeli.
- ❌ **Earnings/Fed/CPI calendar guard yok**: makro print öncesi 30-60dk WAIT olmalı.
- ❌ **Crypto funding rate threshold yok**: extreme funding'de kontrast (squeeze) sinyali alınmalı.
- ❌ **Trailing stop yok**: pozisyon kar'a geçince SL breakeven'a çekilmiyor.
- ❌ **Pyramiding kuralı yok**: aynı sembolde 1 trade üst sınırı zorlanmıyor.
- ❌ **Equal-weight gate**: confidence < 70 ile confidence = 95 aynı sizing alıyor (max 2% her ikisi için).

### AI Verification eksiklikleri
- ⚠️ **Prompt cache hit rate ölçülmüyor**: log'da var ama optimize edilmedi.
- ⚠️ **Master Trader veto oranı bilinmiyor**: kaç defa rule setup'ı reddediyor?
- ⚠️ **Sentiment Scanner over-budgets**: max_tokens=3500, ortalama daha az kullanılsa daha hızlı.
- ⚠️ **Vision şart mı?**: gerçekten confidence ≥ 70'te değer katıyor mu, A/B testi yok.

### Market-spesifik eksiklikler
- **BIST**: TCMB karar günü guard yok, USDTRY threshold (örn. %2 günlük hareket) Rule Engine'e bağlanmadı.
- **US Equities**: VIX threshold (>25 ise WAIT lean), sektör rotation real-time signal yok.
- **Crypto**: BTC dominance trend Rule Engine'e bağlı değil, sadece prompt'ta referans.

### Backtest yok
- Hiçbir kural canlı veriden ÖNCE backtest ile validate edilmedi.
- Rule Engine'in 12 aylık geçmiş üzerindeki Sharpe / max DD bilinmiyor.

### Time-of-day, day-of-week filters yok
- Crypto 24/7 ama Asia-thin saatleri / hafta sonu davranışı modellenmedi.
- BIST'te Cuma kapanış / Pazartesi açılış idiosyncrasies yok.

---

## 9. Konfigürasyon dosyaları — geliştirici için yol haritası

| Dosya | Değiştirilebilir parametreler |
|---|---|
| `agents/rule_engine.py` | `ATR_SL_MULT`, `ATR_TP_MULT`, `RSI_LONG_MAX`, `RSI_SHORT_MIN` |
| `agents/orchestrator.py` | `_VISION_CONFIDENCE_THRESHOLD` (chart eşiği), `LLM_PRICING` |
| `data_fetchers/market_fetcher.py` | `INDICATOR_LOOKBACKS` (interval başına TA-Lib periyotları), `TERM_INTERVALS`, screener pool'ları |
| `config/settings.py` | `MAX_RISK_PCT` (default 0.02), `DEFAULT_LEVERAGE` (default 3) |
| `execution/risk_manager.py` | TP/SL ordering, R:R minimum (1.5), leverage cap |
| `agents/prompts.py` | LLM system prompt'ları (XML-tagged) |
| `rules/*.md` | Market-spesifik harmonize edilen "kitap" — Master Trader prompt'una enjekte edilir |

---

## 10. Doğru sorulması gereken sorular (Gemini'ye sorarken)

1. **Volume**: Rule Engine'e volume filtresi (örn. SMA(20) volume × 1.5) eklemek win-rate'i artırır mı?
2. **Market regime detection**: ADX gibi trend strength indicator'u eklemek, range vs trending markette farklı kurallar uygulamak mantıklı mı?
3. **Mean reversion vs trend continuation**: tek bir setup template her ikisinde de iyi çalışmaz — ikisini ayırmak gerekir mi?
4. **Time-based gating**: günün hangi saatlerinde Rule Engine en yüksek başarıyı veriyor? (Audit verisi biriksin → analyze.)
5. **Stop placement**: ATR yerine swing low/high mantıklı mı?
6. **Position sizing**: %2 sabit risk yerine confidence-weighted (60% → 1%, 95% → 3%) test edilmeli mi?
7. **AI agreement metric**: Master Trader rule decision'ı kaç % oranında onaylıyor? Bu metriği track edip Rule Engine kurallarını yeniden ayarlamak için kullanabilir miyiz?
8. **Backtest framework**: Mevcut audit data'sı backtest replay için yeterli mi? (Cevap: hayır — entry/exit time'ları evet ama tarihsel mum verisi cache'lenmiyor.)

---

> **Bu dosya canlı belge**. Geliştirme yaptıkça güncelle. Mevcut commit:
> `git log -1 --pretty=format:'%h %s'`
