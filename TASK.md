# TradeRay — Açık Görev / Hand-off

> Bu dosya: kullanıcı bir hafta sistem izleyecek; o süre sonunda agent
> (yani sen) bu dosyayı okuyup nereden devam edileceğini bilmek için.
> Bağlam tamamen bu dosyanın içinde. Önceki konuşmaları okumana gerek yok.

---

## 1. Durum (2026-05-16)

**Sürüm**: v2.6 — Phase 3.5 tamamlandı (commit `d6c3748`).

- Sunucu: `developer@135.181.93.25:/opt/traderay`
- 5 container healthy: `traderay-backend / ui / postgres / redis / cloudflared`
- 6 scheduler job aktif: `binance_orders / signal_resolution / stale_orders /
  chandelier / macro_refresh / cost_budget`
- 45/45 pytest pass
- Master switch: kullanıcı bugün UI'dan AÇACAK
- **Tüm marketler SIGNAL-only** (kullanıcının kararı): AUTO_BOT KAPALI

**Backtest bulguları** (`backtest/results/2026-05-16_smoke_test.md`):
- BTC/ETH/SOL MID_TERM (1d TF) negative Sharpe (-3 ile -12 arası), DSR ≈ 0
- BTCUSDT SHORT_TERM (4h HYB) → **0 setup** — gate'ler çok sıkı
- Bu yüzden AUTO_BOT açılmamalı; canlı SIGNAL gözlemiyle parametre sezgisi kazanılacak.

---

## 2. Kullanıcının görevi (bu hafta)

1. UI'a giriş: <https://traderay.forumarac.com> (Cloudflare Access — email-gated).
2. Master switch'i AÇ (sidebar'da "Sistem Aktif" toggle).
3. İstenen marketleri `enabled=True`, `execution_mode=SIGNAL_ONLY` bırak.
   - AI verification'ı (use_ai) açıp açmamak senin tercihin — açıksa LLM cost
     log dolacak, kapalıysa $0 LLM.
4. Bir hafta boyunca:
   - Sinyalleri Telegram'da takip et
   - UI Dashboard'ta DecisionAudit modal'larını oku (narrative format)
   - Trade History tab'inde resolution sonuçlarını gör (TP/SL hit)
5. Bir hafta sonra agent'a "TASK.md oku, durumu değerlendir" de.

---

## 3. Agent — bir hafta sonra (yani sen, okuyan) ne yapacaksın?

### 3.1 İlk önce sistem sağlığı

```bash
ssh developer@135.181.93.25 "docker ps --filter name=traderay --format 'table {{.Names}}\t{{.Status}}'"
```

Tümü `(healthy)` veya `Up` olmalı. Değilse `docker logs <container> --tail 100`
ile sebep ara, gerekirse rebuild et.

### 3.2 Hata sayımı (logs)

```bash
ssh developer@135.181.93.25 "docker logs traderay-backend --since 168h 2>&1 | grep -E 'error|exception|crashed|Traceback' | wc -l"
ssh developer@135.181.93.25 "docker logs traderay-backend --since 168h 2>&1 | grep -E 'warning' | wc -l"
```

Beklenti: error <20, warning <500 (yfinance flakiness yüzünden warning çok olur).
Outlier varsa logları detaylı oku, fix gerekiyorsa fix.

### 3.3 Sinyal istatistikleri (DB)

Aşağıdaki sorguları sırayla çalıştır:

```bash
ssh developer@135.181.93.25 "docker exec traderay-postgres psql -U traderay -d traderay -c \"
SELECT market, action, COUNT(*) AS n
FROM signals
WHERE created_at >= now() - interval '7 days'
GROUP BY market, action ORDER BY market, action;
\""
```

**Kontrol noktaları**:
- Her market'te en az birkaç LONG/SHORT olmalı. **Hiç yoksa** rule_engine
  o market için fazla kısıtlayıcı — eşikleri gevşetmek lazım.
- WAIT/non-WAIT oranı tipik olarak 95/5 dolayında.

```bash
ssh developer@135.181.93.25 "docker exec traderay-postgres psql -U traderay -d traderay -c \"
SELECT
  s.market, s.term, s.action,
  COUNT(*) AS n_signals,
  COUNT(*) FILTER (WHERE s.raw_payload->>'resolution' IS NOT NULL) AS n_resolved,
  COUNT(*) FILTER (WHERE s.raw_payload->'resolution'->>'outcome' = 'TP') AS wins,
  COUNT(*) FILTER (WHERE s.raw_payload->'resolution'->>'outcome' = 'SL') AS losses,
  ROUND(SUM(COALESCE((s.raw_payload->'resolution'->>'theoretical_pnl_usd')::numeric, 0))::numeric, 2) AS total_pnl_usd
FROM signals s
WHERE s.created_at >= now() - interval '7 days' AND s.action != 'WAIT'
GROUP BY s.market, s.term, s.action
ORDER BY s.market, s.term, s.action;
\""
```

**Bu hafta tüm rapor için tek-en-önemli sorgu.** Yorumla:
- **Win-rate > 40% + pozitif total_pnl** → strateji canlıda umut verici. Phase 4
  sweep'i daha agresif tune edebilir.
- **Win-rate < 30% veya negatif total_pnl** → backtest sonuçları doğrulanıyor.
  Phase 4'ün önceliği parametre sweep.
- **n_resolved çok düşük (örn. n_signals=50, n_resolved=5)** → ya pozisyonlar
  hâlâ açık ya da tracker.sync_theoretical_signals çalışmamış. `tracker:signal_resolution`
  job log'una bak.

### 3.4 LLM cost (use_ai aktifse)

```bash
ssh developer@135.181.93.25 "docker exec traderay-postgres psql -U traderay -d traderay -c \"
SELECT
  DATE(created_at) AS day,
  ROUND(SUM(estimated_cost_usd)::numeric, 3) AS daily_usd,
  COUNT(*) AS calls
FROM llm_cost_logs
WHERE created_at >= now() - interval '7 days'
GROUP BY day ORDER BY day DESC;
\""
```

Beklenti: günlük $1-5 arası (use_ai aktifse). $10+ ise bütçe alarmı tetiklenmiş
olmalı — `cost:alert_fired:*` Redis flag'ine bak, Telegram'a ulaşmış mı?

### 3.5 Trade history (eğer AUTO_BOT açıldıysa — şu an kapalı olmalı)

```bash
ssh developer@135.181.93.25 "docker exec traderay-postgres psql -U traderay -d traderay -c \"
SELECT status, COUNT(*), ROUND(SUM(COALESCE(realized_pnl_usd, 0))::numeric, 2) AS total_pnl
FROM trades
WHERE created_at >= now() - interval '7 days'
GROUP BY status;
\""
```

Şu an kapalıysa boş gelecek — beklenen.

### 3.6 DecisionAudit özet

```bash
ssh developer@135.181.93.25 "docker exec traderay-postgres psql -U traderay -d traderay -c \"
SELECT market, category, mode, outcome, COUNT(*)
FROM decision_audit
WHERE created_at >= now() - interval '7 days'
GROUP BY market, category, mode, outcome
ORDER BY COUNT(*) DESC LIMIT 20;
\""
```

`outcome=ERROR` çok ise log'a dön. `outcome=REJECTED` "rejected_missing_tp_sl"
sebepliyse rule_engine bir bug var.

---

## 4. Bulgulara göre karar matrisi

| Bulgu | Aksiyon |
|---|---|
| Sinyal yok / çok az (örn. <5/market) | rule_engine eşikleri gevşet: `rel_volume_min` 1.2→1.0, `adx_min_for_trend` 25→22 |
| Sinyal var ama tümü tek yönde (hep LONG / hep SHORT) | TF bias bir tarafa eğimli — RSI eşiklerini sym hale getir (rsi_long_max + rsi_short_min = 100) |
| Win-rate < 30% + negative total_pnl | Backtest doğrulandı. Phase 4 önceliği: parametre sweep. Bu turda **AUTO_BOT açma**. |
| Win-rate ~ 50% + total_pnl > 0 | Umut verici. SIGNAL-only bir hafta daha izle, sonra `BTCUSDT MID_TERM` için AUTO_BOT açmayı düşün. |
| LLM cost > $5/gün sürekli | use_ai'yi kapat veya `_VISION_CONFIDENCE_THRESHOLD`'u 70'ten 80'e çıkar. |
| Earnings/FOMC/TCMB blackout testi geldi mi? | `decision_audit` logic_trace'ine bak — pre-gate "rejected" var mı? Bu hafta FOMC yoktu, TCMB 2026-06-04. |
| Container'lar bir kez bile restart olmuş mu | `docker inspect` `RestartCount` > 0 ise sebebi bul; OOM ise compose memory limit gerekebilir. |

---

## 5. Phase 4 — bir hafta gözlem sonrası başlama planı

Bu hafta gözlemden sonuç ne olursa olsun, Phase 4 hazırlığı şudur:

1. **Parametre sweep harness** — `backtest/sweep.py` yaz:
   - Grid: `atr_sl_mult ∈ {1.5, 2.0, 2.5}`, `rr_target ∈ {1.5, 2.0, 2.5, 3.0}`,
     `adx_min_for_trend ∈ {20, 25, 30}`, `rel_volume_min ∈ {0.8, 1.0, 1.2}`
   - (3 sembol × 3 term × 108 kombinasyon = 972 backtest, paralel)
   - DSR'i `n_trials=972` ile değerlendir
   - En yüksek DSR'lı 5 parametre setini raporla
2. **MR-on-daily-crypto alternatifi** — `rule_engine.py:CRYPTO_PARAMS[Term.MID_TERM].bias`
   "TF"den "MR"a (Connors-stil) çevir, aynı sweep'i tekrar et.
3. **Regime-switching HMM** — `data_fetchers/regime.py` yaz, Hamilton 1989
   Markov 2-state EM. Bias'ı state'e göre auto-switch et.
4. **BIST earnings calendar** — KAP RSS entegrasyonu veya manuel data file.

---

## 6. Önemli dosyalar (hızlı referans)

- `ALGORITHM.md` — şu an çalışan algoritmanın tam dökümü (v2.6)
- `backtest/results/2026-05-16_smoke_test.md` — bu Phase'in backtest bulgusu
- `agents/rule_engine.py` — parametre matrisi + gate'ler
- `agents/orchestrator.py` — cycle akışı + thesis-broken cancel
- `execution/tracker.py` — Chandelier + reconcile + signal resolution
- `scheduler/jobs.py` — 6 tracker job tanımı
- `tests/` — 45 pytest (`docker exec traderay-backend python -m pytest tests/ -q`)

---

## 7. Bilinen "şimdi yapsak fena olmaz ama bloker değil" maddeler

Bu hafta gözlem ile öncelik sırası değişebilir, ama listede tut:

- [ ] Log seviyesi disiplini — `orchestrator.no_setup_wait` INFO'dan DEBUG'a
- [ ] Cycle correlation ID (uuid kısa) inject — bir cycle'ın 7-8 satır log'unu grep'lemek için
- [ ] APScheduler'ı structlog'a forward — "Adding job tentatively" satırları struct değil
- [ ] Log rotation — `logs/` klasöründe boyut/yaş limiti
- [ ] Rule rejection Telegram bildirimi — şu an "rule fired but rejected by risk_manager" sessiz
- [ ] Chandelier ratchet Telegram bildirimi — SL tightening şu an sessiz
- [ ] UI'da "günlük LLM cost ne kadar / bütçenin yüzde kaçındayız" widget'i

---

## 8. Hata durumunda

- Container down: `ssh developer@135.181.93.25 "cd /opt/traderay && docker compose up -d <service>"`
- Backend crash-loop: `docker logs traderay-backend --tail 200` → Traceback oku
- DB temizleme (gerekirse): **dikkat — tüm data silinir**.
  `cd /opt/traderay && docker compose down -v && docker compose up -d --build`
- Hızlı UI restart: `docker compose restart ui`
- Git pull + rebuild backend: `cd /opt/traderay && git pull && docker compose up -d --build backend`

---

> Son commit referansı: `git log -1 --pretty=format:'%h %s'` (sunucuda `/opt/traderay`).
> Bu dosya bağlam değiştikçe güncellenmeli. Mevcut versiyon: 2026-05-16.
