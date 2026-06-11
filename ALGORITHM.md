  # TradeRay — Karar Algoritması (v3.0 — exit mühendisliği + HMM rejim + drift bekçisi)

  > **Bu dosya: bot şu anda trade ve sinyali NEYE göre üretir?**
  >
  > - v1: tek-evrensel-kural (deprecate).
  > - v2 (Mayıs 2026): market × term parameter matrisi + 3 bias (TF/MR/HYB) + 2-TF
  >   alignment + ADX rejim filtresi + volume gate + Connors RSI(2).
  > - v2.5: Phase 3 — VIX/FOMC/TCMB/Earnings/Funding gate'leri + vol-targeting
  >   kod yolu + Chandelier trailing + walk-forward backtest harness.
  > - v2.6 — Phase 3.5: vol-targeting AKTİF (Carver-style defaults), rule-only
  >   thesis-broken cancel, LLM daily cost budget alarm, pytest suite.
  > - v2.7: gözlem-kalitesi eşik gevşetme (SIGNAL-only veri akışı için).
  > - v2.8: portfolio risk overlay (günlük zarar limiti + SL cooldown +
  >   concurrency cap), AI verifier guardrail'leri (confidence floor, yön
  >   flip→WAIT, risk clamp), per-agent model routing (Haiku/Opus),
  >   sentiment cache, parametre sweep harness.
  > - **v2.9 (bu sürüm) — Phase 4-a/4-b tamamlandı:** CRYPTO SHORT_TERM ve
  >   MID_TERM parametreleri 432-kombo walk-forward sweep'ten DSR>0.5 ile
  >   seçildi (`backtest/results/2026-06-11_phase4_sweep.md`). MR-on-daily
  >   alternatifi test edildi ve REDDEDİLDİ (en iyi MR DSR 0.053).
  >
  > - **v3.0 (bu sürüm):** (1) **Exit mühendisliği** — breakeven-at-R +
  >   zaman bariyeri (López de Prado triple-barrier; Davey 567k-backtest)
  >   harness'te sweep'lenebilir, tracker canlı mirror'lı, gerçek trade'de
  >   Chandelier'a entegre BE tabanı. (2) **HMM rejim filtresi** (Hamilton
  >   1989, saf numpy Baum-Welch, FILTERED olasılık — lookahead yok).
  >   (3) **Fear&Greed** (alternative.me, ücretsiz) enstrümantasyonu +
  >   **BIST earnings blackout**. (4) **Aylık otomatik drift re-sweep**
  >   (Lo 2004 AMH) — Telegram raporu, otomatik uygulama yok.
  >
  > **Üretim notu:** DSR>0.5 şartı sağlandı (§11.5'in 1/3 şartı). AUTO_BOT
  > için kalan şartlar: ≥2 hafta pozitif SIGNAL-only canlı izleme + cost
  > budget uyumu. Canlı veri 2026-06-10 sıfırlamasından itibaren temiz
  > birikmekte. Exit/rejim politikaları yalnızca sweep'te baseline'ı
  > geçtikleri hücrede aktif (sonuçlar: backtest/results/).

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
  | **SCALP** | 15m / 1h | **MR** | RSI(2): ≤15 LONG, ≥85 SHORT | 1.0 | 1.5 | 2% | 3× | 1.0× | Connors-Alvarez 2008 🛠 |
  | **SHORT_TERM** | 4h / 1d | **HYB** | RSI(14): ≤40 / ≥60, ADX≥20/≤18 | **2.0** | **1.5** | 2% | 3× | **0.8×** | **v2.9 sweep: DSR 0.774, 241 trade, p≈0.000** |
  | **MID_TERM** | 1d / — | **TF** | RSI(14): ≤45 / ≥55 + ADX≥20 | **1.5** | **2.0** | 2% | 2× | **0.8×** | **v2.9 sweep: DSR 0.545, 110 trade, p=0.002** |

  > v2.9 parametre seçimi tamamen ampirik: 432 kombo × 3 sembol walk-forward,
  > DSR sıralaması (çoklu-test cezalı). Her iki kazanan set geniş pozitif
  > plato üzerinde (komşu atr×rr kombolarının 11-12/12'si pozitif) ve her
  > sembol tek tek pozitif — tek şanslı nokta değil.

  ### 2.2 SP500 & NASDAQ

  | Term | Sinyal TF / Onay | Bias | RSI eşikleri | ATR SL × | R:R | Risk | Lev | Vol gate |
  |---|---|---|---|---|---|---|---|---|
  | **SCALP** | 15m / 1h | **MR** | RSI(2): ≤10 / ≥90 | 1.0 | 1.5 | 2% | 1× | 1.2× |
  | **SHORT_TERM** | 4h / 1d | **HYB** | RSI(14): ≤40 / ≥60 | 1.5 | 2.0 | 2% | 1× | 1.0× |
  | **MID_TERM** | 1d / — | **MR** | RSI(14): ≤30 / ≥70 | **2.0** | **3.0** | 2% | 1× | **1.2×** |

  > **v2.9 sweep bulgusu (US):** 432 kombo × 10 mega-cap'te istatistiksel
  > anlamlı edge YOK (en iyi DSR 0.019). MID_TERM MR seti observation-grade
  > olarak çalışır (signal-only); 4h flat/negatif olduğundan US marketleri
  > MID_TERM'e alındı. Canlı veri evrende kalma kararını verecek.

  ### 2.3 BIST (.IS)

  | Term | Sinyal TF / Onay | Bias | RSI eşikleri | **ATR SL ×** | R:R | **Risk** | Lev | Vol gate |
  |---|---|---|---|---|---|---|---|---|
  | **SCALP** | 15m / 1h | MR | RSI(2): ≤15 / ≥85 | **2.0** | 1.5 | **1.5%** | 1× | 1.2× |
  | **SHORT_TERM** | 4h / 1d | HYB | RSI(14): ≤40 / ≥60 | **2.0** | 2.0 | **1.5%** | 1× | 1.0× |
  | **MID_TERM** | 1d / — | **MR** | **RSI(14): ≤30 / ≥70** | **1.5** | 3.0 | **1.5%** | 1× | **0.8×** |

  > **v2.9 sweep bulgusu (BIST):** günlük mean-reversion ÇOK güçlü —
  > **DSR 0.979, 317 trade, avg_R +0.51, +163R** (10 mega-cap, 2 yıl).
  > EM kısa-vadeli overreaction literatürüyle tutarlı (De Bondt-Thaler 1985).
  > BIST market'i bu yüzden MID_TERM'de çalışır.

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
  - ❌ **BIST earnings calendar** — yfinance `.IS` için earnings güvenilmez;
    KAP RSS / Foreks API entegrasyonu Phase 4.
  - ❌ **TCMB MPC takvimi otomatik güncelleme** — şu an `config/calendars.py`
    elle bakımlı. Yılda bir TCMB calendar PDF'inden cron'la sync ideal.

  ---

  ## 8B. Phase 3.5 eklemeleri (v2.6 — production gap'leri kapandı)

  ### 8B.1 Vol-targeting AKTİF (Carver 2015 §11 + AQR/Harvey 2018)

  Phase 3'te kod yolu vardı ama `vol_target_annual=None` ile uyuyordu. v2.6'da
  per-(market, term) hedef vol set edildi:

  | Market | Annualized hedef vol | Mantık |
  |---|---|---|
  | CRYPTO | 25% | Coin'lerin realized vol ~60-100%; 25% target → asset DOWN-size |
  | SP500 / NASDAQ | 15% | S&P long-run realized vol ~16-18%; mild down-tilt |
  | BIST | 20% | TR equity vol G7'den yüksek (TL macro + gap risk) |

  `periods_per_year` per signal interval: crypto 15m → 35040, 4h → 2190, 1d → 365;
  US/BIST equity 15m → 6552 (252×26 bars/session), 4h → 504, 1d → 252.

  Çarpan formülü: `mult = clamp(target_vol / realized_vol, 0.5, 1.5)`.
  Volatil sembollerde size yarıya iner, sakin sembollerde 1.5× büyür — daha
  doğru risk-parity sizing.

  ### 8B.2 Rule-only thesis-broken cancel — `agents/orchestrator.py`

  Eski boşluk: AI aktifken Master Trader `CANCEL_PENDING` döndürebiliyordu,
  ama rule-only mode'da pending order'lar 24h TTL'ne kadar bekliyordu.

  v2.6: her cycle'da PENDING entry varsa, rule engine ŞU AN ne diyor diye
  bakılır. Engine WAIT VEYA ZIT yön diyorsa order anında iptal edilir
  (`reason="rule_thesis_broken"`). Layer 3 of staleness manager.

  ### 8B.3 LLM daily cost budget alarm — `core/telegram_notifier.py`

  Settings: `LLM_DAILY_BUDGET_USD` (default $5/day, 0 = disabled).
  Scheduler 30dk'da bir bugünkü `LLMCostLog` toplamını kontrol eder;
  bütçe aşılırsa Telegram alarm + günlük Redis flag ile debounce.

  **Bilinçli tercih**: soft alarm. Bütçe aşımı bot'u durdurmaz — kullanıcı
  market toggle'larıyla yönetir. Çünkü mid-setup AI kesintisi daha kötü.

  ### 8B.4 Pytest test suite — `tests/`

  45 hermetic test (network/DB yok, ~3sn):
  - `test_rule_engine_gates.py` — VIX/FOMC/TCMB/earnings/USDTRY/funding eşikleri,
    vol-targeting matematiği, parametre matris bütünlüğü (4×3=12 hücre var mı?)
  - `test_calendars.py` — TCMB + FOMC pencere aritmetiği, Lucca-Moench
    next-day drift
  - `test_chandelier.py` — ratchet-only, last-close clamp
  - `test_backtest_stats.py` — Sharpe yön/sıfır-varyans, bootstrap p-value
    duyarlılığı, DSR multi-trial penalty

  CI/CD entegrasyonu yok; `docker exec traderay-backend python -m pytest tests/`.

  ### 8B.5 Backtest smoke test sonuçları

  `python -m backtest <SYMBOL> CRYPTO MID_TERM 2024-01-01 2026-05-15 --n-trials 12`

  Detaylı sonuç: [`backtest/results/2026-05-16_smoke_test.md`](backtest/results/2026-05-16_smoke_test.md)

  ---

  ## 8C. v2.8 eklemeleri (2026-06-11)

  ### 8C.1 Portfolio-level risk overlay — `execution/portfolio_guard.py`

  Sinyal-seviyesi rule engine'in göremediği üç klasik hata moduna karşı,
  `engine.route()` içinde signal persist edilmeden ÖNCE çalışan gate'ler
  (Carver 2015 §9 system risk overlays; Tharp 2008 heat caps):

  | Gate | Tetik | Default | Settings |
  |---|---|---|---|
  | **Daily loss kill-switch** | Bugünkü realized + theoretical PnL ≤ −(notional × pct) | 3% | `DAILY_LOSS_LIMIT_PCT` |
  | **SL cooldown** | Aynı (symbol, term, yön) son SL'den sonra pencere içinde | SCALP 4h / SHORT 24h / MID 3d | `SL_COOLDOWN_ENABLED` |
  | **Concurrency cap** | Açık exposure (unresolved sinyal + canlı trade) | 3/market, 8 toplam | `MAX_OPEN_PER_MARKET`, `MAX_OPEN_TOTAL` |

  Gate'ler hem SIGNAL_ONLY hem AUTO_BOT yolunda çalışır (revenge re-entry ve
  korele pile-on sinyal akışını da kirletir). Infrastructure hatasında
  fail-open + loud log. Saf karar mantığı `evaluate_portfolio_gates()` —
  DB'siz unit-test edilir (`tests/test_portfolio_guard.py`).

  ### 8C.2 AI Verification Layer v2 — "verifier, not generator"

  **Sorun:** Master Trader rule engine'in planını hiç GÖRMÜYORDU — sıfırdan
  plan türetiyordu; düşük convictionla "işlem atmış olmak için" trade
  üretebiliyordu. v2.8 değişiklikleri:

  1. **`rule_proposal` payload'da** — AI artık denetlediği planı görür;
     prompt "audit the proposal; default WAIT; reject = success" çerçevesine
     güncellendi.
  2. **Kod-seviyesi guardrails** (`orchestrator.apply_ai_guardrails`, prompt'a
     güvenilmez):
     - Confidence floor: LONG/SHORT + conf < `AI_MIN_CONFIDENCE` (65) → WAIT.
     - Direction flip yasak: AI rule'un tersini derse → WAIT
       (ensemble-disagreement = edge yok).
     - Risk clamp: AI risk_usd'yi sadece DÜŞÜREBİLİR; artış rule planına
       ölçeklenir.

  ### 8C.3 Token ekonomisi — model routing + sentiment cache

  | Ajan | Eski | Yeni | Maliyet |
  |---|---|---|---|
  | Quant | global model (sonnet) | **Haiku 4.5** (`ANTHROPIC_MODEL_QUANT`) | $1/$5 per MTok |
  | Sentiment | global model, **sembol başına çağrı** | **Haiku 4.5** + **30dk Redis cache** (`SENTIMENT_CACHE_SECONDS`) | cycle başına ≤1 çağrı |
  | Master | global model | **Opus 4.8** (`ANTHROPIC_MODEL_MASTER`) | $5/$25 per MTok |

  - LLM_PRICING tablosu düzeltildi (Opus 4.7 $15/$75 değil $5/$25'ti — cost
    loglar 3× şişkindi).
  - Haiku adaptive thinking desteklemez → `llm_client` thinking parametresini
    model-bazlı geçirir.

  ### 8C.4 Parametre sweep harness — `backtest/sweep.py` (Phase 4-a/4-b)

  ```
  python -m backtest.sweep BTCUSDT,ETHUSDT,SOLUSDT CRYPTO MID_TERM \
      2024-01-01 2026-06-01 --biases TF,MR --json results.json
  ```

  - Grid: `atr_sl_mult×rr_target×adx_min×rel_volume_min` (108) × bias; MR
    için 3 RSI eşik çifti (toplam 432 combo).
  - DSR `n_trials = full grid` ile hesaplanır (Bailey-LdP 2014 multiple-testing
    cezası kazanana değil tüm denemelere uygulanır).
  - Mum verisi sembol başına 1 kez çekilir, tüm combolarda paylaşılır.
  - Çıktı kuralı: **DSR > 0.5 ve ≥20 trade yoksa AUTO_BOT açılmaz** (§11.5).

  ### 8C.5 Walk-forward bug fix — SHORT_TERM "0 setup"un asıl nedeni

  Eski harness `confirm_interval`'i temizlenmiş parametreleri hazırlıyor ama
  `generate_rule_decision`'a GEÇİRMİYORDU; engine production parametrelerini
  (confirm TF'li) kullanıyor, tek-TF replay'de onay verisi olmadığından tüm
  MR/HYB sinyalleri "confirmation interval has no usable data" ile WAIT'e
  düşüyordu. v2.6 smoke testindeki "SHORT_TERM 0 setup" bulgusunun başlıca
  nedeni buydu → eski SHORT_TERM backtest sonuçları geçersiz, sweep ile
  yeniden değerlendirilmeli. Fix: `params_override` parametresi.

  ### 8C.6 Faber trend filtresi — zaten mevcut (kod değişikliği yok)

  `_evaluate_tf` LONG için `price > EMA_slow` ister ve 1d interval'de
  `ema_slow=200` (`INDICATOR_LOOKBACKS`) — Faber 2007'nin 10-aylık SMA
  filtresi ile aynı işlev. Ek filtre eklemek redundant olurdu.

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

  ## 11. Üretim rehberi — v2.6 (Phase 3.5 backtest sonrası)

  ### 11.1 Backtest sonuçları (2026-05-16 smoke test)

  Walk-forward replay, 2024-01-01 → 2026-05-15, n_trials=12.

  | Sembol | Term | Setups | Closed | Win % | Avg R | Total R | Sharpe (ann) | DSR |
  |---|---|---|---|---|---|---|---|---|
  | BTCUSDT | MID_TERM | 31 | 30 | 10.0% | -0.60 | -18.0 | -7.80 | 0.0005 |
  | ETHUSDT | MID_TERM | 18 | 17 | 5.9% | -0.76 | -13.0 | -12.51 | 0.001 |
  | SOLUSDT | MID_TERM | 24 | 23 | 17.4% | -0.30 | -7.0 | -3.12 | 0.007 |
  | BTCUSDT | SHORT_TERM | **0** | 0 | — | — | — | — | — |

  **Bootstrap p-value = 1.0 / DSR ≈ 0** üç negatif run için → bu sonuçlar şans
  değil, parametre kaynaklı.

  ### 11.2 Bulguların yorumu

  1. **MID_TERM 1d TF crypto, mevcut parametrelerle: kayıp.** Win-rate 10-17%
     × R:R=3 → matematik tutarlı (avg_R ≈ -0.6 = (0.1×3) − (0.9×1)). Tek-coin
     trend-following günlük TF'de ATR stop'lara yeniyor.
  2. **SHORT_TERM 4h: 0 setup.** RSI + volume + ADX kombinasyonu 4h'da çok
     kısıtlayıcı — HYB bias hiç tetiklenmiyor.
  3. **Harness işini yaptı.** Bu sonuçlar olmadan AUTO_BOT canlı parayla
     açılsaydı doğrudan kanama başlardı. Backtest = ücretsiz uyarı.

  ### 11.3 Önerilen rollout

  | Aşama | Aksiyon | Süre |
  |---|---|---|
  | **Şu an** | `system_enabled=ON`, **tüm marketler SIGNAL-only**, AUTO_BOT kapalı | 2-4 hafta |
  | Gözlem | Canlı sinyalleri DecisionAudit + theoretical PnL ile izle | 2-4 hafta |
  | Phase 4-a | Parametre sweep (`atr_sl_mult`, `rr_target`, `adx_min`, `rel_volume_min`) walk-forward + DSR'e göre — `n_trials` doğru girilmeli | 1 hafta |
  | Phase 4-b | MR-on-MID_TERM-crypto alternatifi test (Connors-stil daily) | 1 hafta |
  | Phase 4-c | Regime-switching HMM (Hamilton 1989) — vol rejimine göre bias auto-switch | 2-3 hafta |
  | Production | Sweep'ten geçen parametrelerle CRYPTO MID_TERM AUTO_BOT açılır | — |

  ### 11.4 Şu anda güvenle çalışan kısımlar

  - **Sinyal üretimi her piyasada güvenli** — yanlış sinyal ≠ para kaybı,
    sadece "bu setup'a uymadık" verisi. DecisionAudit + Telegram'la izlenir.
  - **AI Verification Layer** açıkken Master Trader rule_decision'ı eleyebilir;
    kayıplı rule setup'ı LLM'in WAIT'e çevirme şansı var. Token harcaması var
    ama "akıllı veto" değeri var.
  - **Makro gate'leri** (VIX/FOMC/TCMB/earnings) — bunlar parametre değil,
    politika; backtest'te dahil değiller ama canlıda **otomatik koruma** sağlar.
  - **Chandelier trailing** — açık trade'de zarar limitleyici (sadece OPEN
    trade'lerde anlamlı, şu an hiç trade yok).
  - **Cost budget alarm** — AI aktifse $5/gün üzerine çıkarsa Telegram uyarır.

  ### 11.5 Ne ZAMAN AUTO_BOT açılmalı?

  Şu üç eşiğin hepsi geçilmeden açma:
  1. Walk-forward + DSR sweep'iyle **DSR > 0.5** olan parametre seti.
  2. En az 2 hafta paper/SIGNAL-only canlı izleme — gerçek sinyaller
     resolution'da pozitif total-R üretmiş olmalı.
  3. Cost budget testten geçmiş — AI aktif modda bir hafta `≤ $3/gün` kalmış olmalı.

  ---

  > **Canlı belge.** Her parameter tweak'i sonrası burayı güncelle. Commit hash
  > referansı: `git log -1 --pretty=format:'%h %s'`.
