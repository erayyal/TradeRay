# Geliştirme Planı — canlı veri analizinden çıkan yol haritası

> Dayanak: `2026-06-19_live_data_analysis.md`. Tüm adımlar ücretsiz kaynaklarla
> (mevcut Binance/yfinance/alternative.me + yerel hesaplama). Geliştirme EN SON
> yapılacak — bu dosya önce NE yapılacağını ve NEDEN'ini netleştirir.

## Çekirdek içgörü: elimizde bir "meta-labeling" fırsatı var

López de Prado (Advances in Financial ML, 2018) meta-labeling: birincil model
yön tahmin eder (yüksek recall), **ikincil model** her sinyalin alınıp
alınmayacağına karar verir (precision artırır → F1 ve Sharpe yükselir). Canlı
veride kural motoru = birincil model (çok sinyal), **güven skoru + HMM rejimi =
hazır ikincil-model özellikleri**. Veri bunların işe yaradığını gösterdi:

- Güven ≥80 filtresi: −609 USD → +300 USD
- high_vol rejim filtresi: −5.5R → +1.5R

Plan, bu içgörüyü tüm sisteme disiplinli biçimde uygulamak üzerine kurulu.

---

## FAZA A — Sembol evreni kalitesi (temel, en yüksek öncelik)

**Sorun:** Screener 24h hacme/harekete göre top-5 seçiyor → HYPE/SPCX/H/ZEC
gibi pompalanmış meme/yeni coin'leri topluyor. SCALP mean-reversion için
zehir.

**Çözüm (ücretsiz, Binance API):**
1. CRYPTO için statik kalite havuzu: yalnızca yerleşik, yüksek-likidite
   USDT-M perpetual'ler (BTC, ETH, SOL, BNB, XRP, ADA, AVAX, LINK, DOGE, LTC,
   DOT, ... ~20-30 isim). Screener bu havuz İÇİNDEN top-N seçer.
2. Likidite tabanı: 24h quote-volume eşiği (örn. ≥ $50M) + listelenme yaşı
   filtresi (Binance `onboardDate` ≥ 90 gün) — yeni/ince coin'leri ele.
3. Volatilite tavanı: atr_pct çok aşırı semboller (örn. >%15 günlük) SCALP'ten
   düşülür — bunlar gap riski taşır.

**Doğrulama:** Bu filtre tarihsel sweep'lerde zaten örtük vardı (BTC/ETH/SOL
ile yapıldı). Canlıda screener'ı bu havuza bağlamak, backtest ile canlı
arasındaki sembol uçurumunu kapatır.

---

## FAZA B — Güveni birinci sınıf, taranabilir filtreye dönüştür (meta-labeling)

**Sorun:** Güven skoru sonucu güçlü öngörüyor ama şu an yalnızca AI katmanında
bir eşik (ai_min_confidence=65) olarak kullanılıyor; kural-only modda hiç
filtre değil.

**Çözüm:**
1. `TermParams`'a `min_confidence: int = 0` alanı ekle. Kural motoru, setup'ın
   confidence'ı bu eşiğin altındaysa WAIT döndürür.
2. `sweep.py`'a confidence eksenini ekle (örn. {0, 60, 70, 80}) — tıpkı
   atr/rr gibi taranır, DSR ile seçilir.
3. Her (market, term) hücresi kendi optimal confidence tabanını kazanır.

**Beklenti:** SCALP'te min_confidence≈80, yavaş hücrelerde belki 0 (zaten
seçici). Bu, meta-labeling'i kural-only mimariye gömer — ekstra model yok,
mevcut skoru kapı bekçisi yapar.

---

## FAZA C — SCALP'i düzgün yeniden doğrula (ya geçer ya kapanır)

**Sorun:** SCALP tek aktivite kaynağı ama doğrulanmamış ve kaybediyor.

**Çözüm — karar ağacı:**
1. SCALP'i tam sweep'e sok: atr × rr × rel_volume × **min_confidence ×
   regime_filter** (yeni eksenler), kalite havuzundaki sembollerle, 15m veri.
2. **Eğer** bir kombo DSR>0.5 + ≥50 trade veriyorsa → o parametrelerle SCALP
   aktif kalır (canlı veri zaten umut veriyor: güven≥80 + high_vol).
3. **Eğer** hiçbir kombo geçmiyorsa → SCALP `enabled=false`, kapatılır.
   Doğrulanmamış bir term'i sırf "akış olsun" diye açık tutmak v2.6'daki
   negatif-Sharpe hatasının aynısı.

**Not:** SCALP 15m verisi Binance'tan bol (1500 bar ≈ 15 gün; daha uzun için
sayfalama gerekebilir — harness zaten destekliyor). Walk-forward için en az
6-12 ay 15m çekilmeli.

---

## FAZA D — Yavaş hücrelerin "frekans" sorunu

**Sorun:** Doğrulanmış 4h/1d hücreleri 8 günde neredeyse hiç tetiklenmedi
(CRYPTO 4h: 0 sinyal). Bunlar en güvenilir edge'ler ama veri üretmiyor.

**Çözüm seçenekleri (sweep'le karşılaştır):**
1. **Sembol evrenini genişlet**: 4h/1d hücreler için 3 değil 10-15 sembol →
   daha çok eşzamanlı fırsat, aynı edge. (Portfolio guard concurrency cap'i
   zaten koruyor.)
2. **Frekansı kabul et**: BIST 1d MR DSR 0.991 — nadir ama çok kaliteli.
   Belki doğru olan, az ama öz sinyal. Bu durumda beklenti yönetimi mesele;
   değerlendirme penceresi 1 hafta değil 1-2 ay olmalı.
3. CRYPTO 4h HYB eşiklerini GEVŞETME (v2.7 dersini tekrarlama) — bunun yerine
   evren genişletmesi tercih edilir.

---

## FAZA E — Rejim gate'ini canlı veriyle yeniden değerlendir

**Sorun:** Backtest sweep'i rejim gate'inin SHORT/MID_TERM'de DSR'ı düşürdüğünü
bulmuştu; ama canlı SCALP verisi high_vol gate'inin yardım ettiğini gösteriyor
(farklı TF/bias). Çelişki değil — farklı stratejiler.

**Çözüm:**
1. Faza C'deki SCALP sweep'ine regime_filter eksenini dahil et (zaten planlı).
2. Audit'te `regime_p_high × outcome` korelasyonunu her hücre için periyodik
   incele (aylık re-sweep raporuna ek).
3. FNG yeterince varyasyon gösterdiğinde (şu an hep Extreme Fear) onu da
   ikincil-model özelliği olarak sweep'e ekle.

---

## Uygulama sırası (geliştirme fazı — EN SON)

```
A (sembol kalitesi)  ─┐
B (confidence ekseni) ─┼─→ C (SCALP re-validation, A+B+E'yi kullanır)
E (regime ekseni)    ─┘        │
                               ├─→ D (frekans: evren genişletme sweep'i)
                               └─→ deploy v3.1 + yeni 1-2 aylık gözlem
```

Önce A+B+E altyapısı (TermParams alanları + sweep eksenleri), sonra tek büyük
sweep kampanyası tüm hücreleri yeni eksenlerle yeniden değerlendirir, kazananlar
uygulanır, sistem yeni gözleme alınır.

## Başarı kriterleri (v3.1 için)

- SCALP: ya DSR>0.5 doğrulanmış parametre seti, ya kapalı.
- Her aktif hücrede min_confidence sweep'le seçilmiş.
- CRYPTO screener kalite havuzuna bağlı; meme/yeni coin yok.
- Yeni gözlem penceresi ≥ 1 ay (yavaş hücrelerin çözülmesi için).
- AUTO_BOT hâlâ kapalı; §11.5 şartları yeni veriyle yeniden değerlendirilir.
