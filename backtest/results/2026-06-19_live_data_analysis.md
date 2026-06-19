# Canlı Veri Analizi — 8 günlük gözlem (2026-06-11 → 06-19)

> Sistem 2026-06-19 11:38 UTC'de durduruldu (master switch OFF). Bu rapor
> sıfırlama sonrası biriken TEMİZ veriyi analiz eder. AUTO_BOT hiç açılmadı
> (0 gerçek trade), AI hiç çağrılmadı (0 LLM maliyeti).

## 1. Veri envanteri

| Tablo | Adet | Not |
|---|---|---|
| signals | 62 | sadece non-WAIT |
| decision_audits | 3.794 | her cycle bir satır (WAIT dahil, dedup'lu) |
| trades | 0 | AUTO_BOT kapalı — beklenen |
| llm_cost_logs | 0 | use_ai kapalı — $0 maliyet |

Pencere: 8 gün. Teorik toplam PnL: **−609.53 USD** (10.000 USD nominal portföy üzerinden, hepsi SCALP'ten).

## 2. EN ÖNEMLİ BULGU: aktivitenin tamamı doğrulanmamış SCALP'te

Sinyal dağılımı:

| Market | Term | n | Çözülen | TP | SL | PnL (USD) |
|---|---|---|---|---|---|---|
| **CRYPTO** | **SCALP** | **54** | **54** | **20** | **34** | **−509.53** |
| NASDAQ | MID_TERM | 3 | 1 | 0 | 1 | −100.00 |
| BIST | MID_TERM | 3 | 0 | — | — | 0 (hepsi açık) |
| SP500 | MID_TERM | 2 | 0 | — | — | 0 (hepsi açık) |

**Ne oldu:** CRYPTO 11-12 Haziran'da doğrulanmış SHORT_TERM (4h HYB, DSR
0.785) ile **2 günde 0 sinyal** üretti. 12-13 Haziran'da CRYPTO **SCALP'e
alındı** (config son güncelleme izinde görülüyor) — çünkü kullanıcı sinyal
akışı istiyordu. SCALP (15m RSI(2) MR) çok sık tetiklenir ama **hiç sweep'le
doğrulanmadı** ve kaybetti.

SCALP matematiği:
- 20 TP × +1.5R, 34 SL × −1.0R = net **−4R**
- Kazanma oranı %37 < rr 1.5'in başabaş eşiği %40 → yapısal kayıp
- Kazanan/kaybeden tamamen sembole göre saçılmış (ETH +266, SPCX +350 vs
  ZEC −400, BTC −376) → **edge yok, gürültü**

## 3. ALTIN BULGU: canlı SCALP verisi iki ücretsiz, güçlü filtre ortaya çıkardı

Sistemi kötü gösteren SCALP verisi, aslında nasıl düzeltileceğinin reçetesini içeriyor.

### 3.1 Güven skoru sonucu mükemmel ayrıştırıyor

| Güven | n | TP | Kazanma % | PnL (USD) |
|---|---|---|---|---|
| **≥80** | 27 | 12 | **44%** | **+300.80** |
| **<75** | 27 | 8 | 30% | **−810.33** |

Kural motorunun KENDİ güven skoru, sonucu güçlü öngörüyor. Sadece güven ≥80
alınsaydı −609 USD → **+300 USD** olurdu. Bu, tüm hücrelerde taranması
gereken birinci sınıf bir filtre.

### 3.2 HMM rejim filtresi de ayrıştırıyor (mean-reversion mantığıyla)

| Rejim (girişte) | n | TP | SL | avg_R | total_R |
|---|---|---|---|---|---|
| **high_vol** (P≥0.5) | 16 | 7 | 9 | +0.09 | **+1.5R** |
| **low_vol** (P<0.5) | 38 | 13 | 25 | −0.15 | **−5.5R** |

RSI(2) mean-reversion **oynak/dalgalı rejimde** çalışır, sakin trend
rejiminde başarısız olur (RSI extreme'leri trendde daha da extreme'e gider).
Backtest sweep'i rejim gate'inin SHORT/MID_TERM'de zarar verdiğini bulmuştu —
ama SCALP hiç rejimle taranmadı, canlı veri tersini söylüyor. (n=16 küçük,
sweep'le doğrulanmalı.)

> **Not:** İki filtre muhtemelen örtüşüyor (yüksek güven ≈ derin RSI extreme ≈
> oynak rejim). Birleşik etki ayrıca ölçülmeli.

## 4. Doğrulanmış hücreler hakkında: henüz hüküm yok

- **BIST 1d MR** (en güçlü iddia, DSR 0.991): 3 sinyal, **hepsi açık**.
  Günlük TF hedefleri haftalar sürer; 8 gün yetersiz. BE=1.0 politikası
  doğru eklenmiş (exit_policy'de görülüyor).
- **Equity MID_TERM**: 8 sinyal, 1 çözüldü (NASDAQ AMAT SHORT → SL).
- **CRYPTO 4h HYB**: 8 günde 0 sinyal — fazla kısıtlayıcı (tarihsel sorun).

**Sonuç:** Doğrulanmış yavaş hücreler 8 günde anlamlı veri üretemez. Ya daha
çok sembol/daha düşük TF gerekiyor, ya da çok daha uzun gözlem.

## 5. Sembol kalitesi sorunu

CRYPTO screener'ın seçtikleri: ETHUSDT(12), HYPEUSDT(11), SPCXUSDT(9),
BTCUSDT(8), SOLUSDT(8), ZECUSDT(4), HUSDT(2). HYPE/SPCX/H/ZEC = küçük/yeni/
meme coin'ler — 24h hacme/harekete göre top-5 seçimi **pompalanmış çöpü**
seçiyor. SCALP mean-reversion için en tehlikeli semboller.

## 6. Diğer enstrümantasyon

- **Rejim hesaplama**: CRYPTO 2793/2803 audit'te `regime_p_high` mevcut,
  değerler mantıklı (CRYPTO avg 0.21 sakin, NASDAQ 0.87 oynak). Modül sağlam.
- **FNG**: tüm pencere "Extreme Fear" (12-23 arası, avg 18.5). Varyasyon yok →
  korelasyon çıkarılamaz, birikmeye devam etmeli.
- **Bar-hizalı cron + WAIT dedup**: 3.794 audit, "xx:35 artifact" yok, sağlıklı.
- **0 traceback/exception** 8 gün boyunca. Altyapı stabil.

## 7. Tek cümlelik özet

> Sistem teknik olarak kusursuz çalıştı; tek hata, doğrulanmış yavaş hücreler
> sinyal üretmeyince **doğrulanmamış SCALP'in açık bırakılması** oldu. Ama bu
> "hata" bize iki güçlü ücretsiz filtre (güven ≥80, high_vol rejim) ve net bir
> sembol-kalitesi dersi hediye etti. Kayıp −609 USD teoriktir (gerçek para
> riske girmedi) ve öğrenme değeri maliyetinden kat kat fazla.
