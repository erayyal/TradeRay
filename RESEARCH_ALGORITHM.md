# RESEARCH_ALGORITHM.md — TradeRay için Literatür-Temelli Algoritma Tasarımı

> **Amaç:** Mevcut TradeRay kural motorunu (RSI<40 + MACD_hist>0 + price>EMA_slow + multi-TF EMA hizalanması; ATR 1.5/3.0 R:R; her piyasaya tek kural seti) hakemli akademik ve büyük kurumsal araştırmalarla yeniden çerçeveletmek. **Hedef okuyucu:** Eray + Gemini (ikinci tur analiz).
>
> **Kanıt katmanları:** 🎓 hakemli akademik | 🏛 büyük kurumsal araştırma (AQR, Two Sigma, Glassnode, CBOE, Coin Metrics) | 🛠 uygulayıcı bilgisi (Wilder, Tharp, Connors, Bollinger, Elder, Carver)
>
> **Önemli not:** Kripto literatürü genç, BIST literatürü İngilizce akademide ince. Bu boşlukları açıkça işaretledim. "[kaynak gerekli — doğrulanamadı]" notu olanlar uydurulmuş referans değil, doğrulanamadığım iddialardır.

---

## 1. Yönetici Özeti (TL;DR)

TradeRay'in tek-evrensel-kural mimarisi, ampirik literatürün en güçlü bulgusuyla doğrudan çelişiyor: **momentum/değer/trend kalıcılığı varlık-sınıfı ve zaman-dilimi başına farklı genlik ve yönde işliyor** (Asness/Moskowitz/Pedersen 2013, Moskowitz/Ooi/Pedersen 2012). Aynı RSI<40 eşiği BTC 15dk, BIST günlük ve NASDAQ 4 saatlikte aynı bilgi içeriğine sahip *değildir*. Sistemin tek-set yapısı, kanıtlar arası varyansı görmezden geliyor.

### En yüksek kaldıraçlı 7 değişiklik (önem sırasıyla)

1. **Rejim filtresi ekle (ADX veya gerçekleşmiş volatilite yüzdebirliği) → kural motoru iki modlu olsun: TREND-FOLLOWING (ADX>25) ve MEAN-REVERSION (ADX<20).** Mevcut kural mantığı (RSI düşük + MACD pozitif + EMA üstü) bir "düşük RSI'de mean-reversion *ama* trend yönünde TF" hibridi; rejim ayrımı yapılmadığında ikisi de zayıflar. (Brock-Lakonishok-LeBaron 1992 vs Lo-Mamaysky-Wang 2000; Wilder 1978 DMI/ADX uygulayıcı çerçevesi.)
2. **Mevcut "RSI<40" eşiği uygulayıcı folkloru bile değil** — Wilder'in orijinal eşiği 30/70'tir; Connors RSI(2) 5/95 ya da 10/90 kullanır. Kanıt-temelli bir başlangıç: SCALP'te Connors-tipi RSI(2)<10 (mean-reversion); MID_TERM'de Wilder RSI(14)<30 + trend hizalanması (pull-back trade); 40 sayısının ampirik bir dayanağı yoktur.
3. **R:R = 2.0 sabit hedef, ATR 1.5/3.0 yapısı muhafazakar ama optimal değil.** Time-series momentum literatürü (Moskowitz/Ooi/Pedersen 2012) volatilite-hedefli pozisyon büyüklüğü + trailing stop önerisini ampirik olarak doğrular. Sabit 2R hedef, en güçlü trendleri keser. Önerim: SCALP'te 1.5R sabit, SHORT_TERM'de 2R, MID_TERM'de Chandelier (ATR-trailing) stop ile sınırsız üst.
4. **BACKTEST YOK — bu, sistemin tek en büyük zayıflığı.** López de Prado'nun çalışması (2018, JPM) düşük gözlem sayısıyla parametre seçimi yapan stratejilerin Deflated Sharpe'sini neredeyse her zaman negatif buluyor. Walk-forward (Pardo 1992) + bootstrap permütasyon (Aronson 2007) zorunlu olmadan üretime hiçbir parametre değişikliği gitmemeli.
5. **Piyasa-spesifik kapılar (gates):** Hisse senetlerinde **kazanç takvimi engeli (Bernard-Thomas 1989 PEAD ile uyumlu olarak yön önceden bilinemediği için kazanç günü öncesi/gün-of yeni pozisyon açmama)**, kriptoda **funding rate aşırı uçları (Glassnode/Coinglass araştırmaları)**, BIST'te **TCMB MPC günü filtresi (saat 14:00 sonrası dalgalanma)**.
6. **Multi-timeframe EMA hizalanması "her interval EMA tarafında aynı olmalı" kuralı tehlikeli bir serbestlik derecesi.** Elder (1986) Triple Screen uygulayıcı çerçevesi; akademide bu kuralın Sharpe'yi artırdığı *kontrollü* gösterimi yok (curve-fit ihtimali yüksek). Önerim: yalnızca 2 zaman dilimi kullan (sinyal TF + onay TF, oran ~4-6x); 3+ TF hizalanması istatistiksel olarak gereksiz kısıtlama.
7. **Aynı kural her piyasaya uygulanıyor — ayır.** Kripto perp'leri (24/7, leverage, funding); ABD hisseleri (RTH + pre/post, kazanç takvimi); BIST (TL FX coupling, daha düşük likidite, TCMB rejimi). Bunlar farklı microstructure → farklı parametre setleri ve farklı kural fonksiyonları olmalı (ayrı rule_engine.py çağrı yolları).

### Dürüst boşluklar
- BIST için **İngilizce hakemli literatür çok zayıf**; Borsa İstanbul Review'da Fama-French + Carhart momentum testi var (Türkiye'de momentum prim var diyor) ama saatlik/15dk işleyebilen mikrobackstest neredeyse yok. Bilkent/Koç/Sabancı yüksek lisans tezleri ve TCMB working paper'larına işaret ediyorum.
- Kripto vadeli işlemlerin akademik likidite/funding araştırması 2019 sonrası, peer-reviewed sayısı az; Glassnode, Coin Metrics ve Bitwise kurumsal raporları (🏛) ana kaynak.
- ICT-stil "likidite süpürme/stop hunt" kavramları **hakemli literatürde doğrulanmıyor**; uygulayıcı folklor (🛠). Algoritmanın etrafında karar vermeyin.
- Connors RSI(2) için Connors/Alvarez'in 2008 öncesi backtest'leri pozitif; **2008 sonrası tek başına alfa kaybı** birden fazla retro-test'te belgelendi (Quantified Strategies, 2023-2025 retro testleri 🏛). Yani Connors RSI mean-reversion mantığı *konfigurasyon olarak doğru* ama tek başına yeterli değil.

---

## 2. Trend-Following vs Mean-Reversion: Hangisi Nerede İşliyor?

Akademinin en sağlam tek bulgusu: **cross-sectional momentum, 3–12 aylık yatay kesitte hisse senetlerinde anlamlı pozitif risk-ayarlı getiri üretir** (Jegadeesh & Titman 1993, *JoF*: aylık ~%1.49 fark, geçmiş 12 aydaki en iyi decile − en kötü decile). Bu sonuç Asness/Moskowitz/Pedersen 2013'te dört coğrafya ve sekiz varlık sınıfında genelleniyor; momentum ve değer "her yerde" ve birbirine negatif korele.

**Time-series momentum** (Moskowitz/Ooi/Pedersen 2012, *JFE*): bir enstrümanın *kendi* 1–12 aylık getirisi gelecek getirinin pozitif öngörücüsüdür; 58 vadeli işlem (endeks, FX, emtia, tahvil) üzerinde 25+ yıl. Geleneksel CTA/managed futures'ın akademik temellendirmesi budur. **Önemli sonuç:** TF stratejileri ekstrem piyasa rejimlerinde en güçlü.

Mean-reversion ise **kısa horizonda (günlük/saatlik) ve kontrarian/aşırı uç durumlarda** öne çıkar:
- Hisse senetleri: 1 aydan kısa horizonda kısa-vadeli reversal (Jegadeesh 1990, *JoF*).
- Kripto: Bollinger Bands mean-reversion BTC/USDT'de döngüye göre değişen performans gösteriyor (Arda 2024, SSRN); boğa fazlarında çalışıyor, distribütasyon fazında bozuluyor.
- S&P 500 günlük indekste: 25-yıllık parametre taraması Bollinger mean-reversion'ın Buy&Hold'u **geçemediğini** gösteriyor (Rational Growth retro testi 🛠, ancak akademik Brock-Lakonishok-LeBaron MA kuralları da Sullivan-Timmermann-White 1999'da data-snooping düzeltmesinden sonra büyük ölçüde kayboluyor).

**Lo'nun Adaptive Markets Hipotezi** (2004, *JPM*): hangi stratejinin işlediği rejime, katılımcı popülasyonuna ve evrime bağlı; "her zaman trend-following işler" ya da "her zaman mean-reversion işler" iddiası hatalıdır.

### Pratik çıkarım — varlık sınıfı × zaman dilimi matrisi (literatür eğilimi):

| Varlık × TF | Baskın bias (kanıt-temelli) | Ana referans |
|---|---|---|
| Hisse — günlük (3–12ay holding) | Cross-sectional + TS momentum | Jegadeesh-Titman 1993, AMP 2013 🎓 |
| Hisse — haftalık/aylık | Momentum 12-2 (skip son ay) | Asness 1994 🎓 |
| Hisse — 1–5 günlük | Kısa-vadeli reversal | Jegadeesh 1990, Lehmann 1990 🎓 |
| Hisse — intraday | Mikroyapı + VWAP-pullback | Lo-Mamaysky-Wang 2000 (zayıf), pratik 🏛/🛠 |
| Vadeli endeksler — 1–12 ay | TS momentum çok güçlü | MOP 2012 🎓 |
| Emtia — 1–12 ay | TS momentum + carry | MOP 2012, AQR 🎓/🏛 |
| Kripto — günlük | TS momentum var ama gürültülü | Bianchi 2020 🎓; Bitwise 🏛 |
| Kripto — saatlik/intraday | Funding-driven mean-reversion + TF karışık | Glassnode 🏛, pratik |
| BIST — aylık | Momentum primi belgelendi | Borsa İstanbul Review benchmark çalışmaları 🎓 |
| BIST — günlük | Mevcut İngilizce literatür yetersiz | [kaynak gerekli] |

### TradeRay'in 3 termi için öneri:

- **SCALP (15dk):** **Mean-reversion** öncelikli (Connors-stil RSI<10/15, Bollinger alt bant), trend-süzgeçle (üst TF'de yön).
- **SHORT_TERM (4h):** **Hibrit** — ADX rejim filtresine bağla. ADX>25 ise pull-back-in-trend; ADX<20 ise BB-MR.
- **MID_TERM (1d):** **Trend-following** baskın (TS-momentum, 50/200 MA pozisyonu). Mean-reversion sadece aşırı uç günlerde devreye gir.

---

## 3. İndikatör-İndikatör Akademik Değerlendirme

### 3.1 RSI (Relative Strength Index — Wilder 1978)
- 🛠 **Orijinal:** Wilder 14-periyot, 70 aşırı alım / 30 aşırı satım. Wilder'in kendi tavsiyesi: "trend yönündeki sinyalleri al, karşı yöndekileri filtrele."
- 🛠 **Connors RSI(2):** 2-periyot, eşik 10/90 hatta 5/95; sadece kısa-vadeli pullback'ler için. Connors-Alvarez (2008) *Short Term Trading Strategies That Work* — S&P 500 hisseleri ve ETF'lerde pozitif edge belgelendi; **ancak 2010 sonrası tek başına performansı düştü** (Quantified Strategies retro testleri 🏛, 2023-2025).
- 🎓 **Akademik durum:** Tek-başına RSI'nin uzun-vadeli alfa ürettiğine dair sağlam hakemli kanıt yok; ancak *rejim göstergesi* ve *trend içi pull-back tetikleyici* olarak kullanışlı.
- **TradeRay için kritik bulgu:** `RSI<40` ne Wilder ne de Connors eşiği; ampirik dayanağı yok. **Önerim:** SCALP'te RSI(2)<10, SHORT_TERM'de RSI(14)<30, MID_TERM'de RSI(14)<35 *ama yalnızca yukarı trend onaylandığında*.

### 3.2 MACD (Appel ~1979)
- 🎓 Stand-alone MACD crossover'ın istatistiksel anlamlılığı zayıf (Chong & Ng 2008 *Applied Economics Letters* MACD ve RSI'nin Londra borsasında küçük pozitif edge raporlasa da çoklu test düzeltmesi yok).
- 🛠 MACD histogramı `>0` "momentum pozitif" filtresi olarak uygulayıcı yaygın; ama EMA(12)−EMA(26) sinyalin EMA(9)'undan farkı, EMA-slope'un türevi → mevcut sistemde **`price>EMA_slow` ile bilgi çakışması yapıyor** (kolinearlik). İki gate aynı bilgiyi çoklamış oluyor.
- **Öneri:** MACD histogramı yerine **ROC(20) momentum yüzdebirliği** veya **MOP-tipi 12-1 ay getirisi** (mid-term'de) daha temiz.

### 3.3 Hareketli Ortalamalar (MA / EMA)
- 🎓 **Brock-Lakonishok-LeBaron 1992** (*JoF*) Dow Jones 1897–1986: basit MA crossover kuralları (1/50, 1/150, 1/200) bootstrap ile anlamlı pozitif edge gösterdi → **TA için tek en çok atıfta bulunulan akademik destek**.
- 🎓 **Sullivan-Timmermann-White 1999** (*JoF*) — White's Reality Check ile data-snooping düzeltmesi yapıldığında BLL'nin 1986 sonrası 10-yıl out-of-sample performansı **kaybolur**. Yani: edge gerçekti, sonra arbitraj edildi/değişti.
- 🏛 AQR'ın time-series momentum çalışmaları (10-ay MA, 200-gün MA filtresi) hala sistematik trend takibinin **risk-azaltıcı** etkisini destekliyor.
- **Sonuç:** EMA-pozisyon filtresi **rejim filtresi olarak (price>200MA ⇒ trend mevcut)** mantıklı, **giriş tetikleyicisi olarak** zayıf.

### 3.4 ADX / DMI (Wilder 1978)
- 🛠 Wilder'in kendi tavsiyesi: ADX>25 trendli, <20 yatay. **ADX akademik literatürde nadiren tek başına test ediliyor** ama trend-filtresi olarak kurumsal sistemlerde standart.
- 🏛 Çeşitli kurumsal retro testlerde ADX<20 filtresi yanlış sinyalleri ~%30-40 azaltır (TradingView, BuildAlpha retro testleri 🛠/🏛).
- **TradeRay için öneri: rejim anahtarı olarak ekle (Phase 2).**

### 3.5 ATR (Wilder 1978)
- 🛠 Risk boyutlandırma için altın standart (Tharp, Carver).
- 🎓 Akademik volatilite-hedefli pozisyon büyüklüğü (Moskowitz/Ooi/Pedersen 2012; Harvey ve diğ. *J. of Portfolio Mgmt* 2018 "Impact of Volatility Targeting") — sabit-dolar pozisyonuna göre Sharpe'yi belirgin artırır.
- **Mevcut TradeRay 1.5×ATR stop / 3×ATR hedef = R:R 2.0 ⇒ koruyucu ama bilgi içermeyen sabit oran.** Önerim aşağıda §6'da.

### 3.6 Bollinger Bantları (Bollinger 1980'ler)
- 🛠 Bollinger'in kendi tavsiyesi: BB tek başına değil, momentum onayı ile (BB %b + RSI).
- 🎓 Karışık: Taiwan 50'de pozitif edge (*Physica A* 2020); S&P 500 25-yıllık testlerde Buy&Hold altında; BTC/USDT'de rejim-bağımlı (Arda SSRN 2024).
- **Pratik:** Bollinger Band Squeeze (daralma → genişleme) volatilite genişlemesi başlangıcını yakalamada faydalı (vol-of-vol sinyali).

### 3.7 Hacim / OBV / VWAP
- 🎓 OBV (Granville 1963) akademik destek minimal; akademik volume-price literatürü "hacim volatiliteyi öngörür ama yön sinyali zayıf" diyor (Karpoff 1987 *JFQA*).
- 🏛 **VWAP'in temel akademik kullanımı *execution*, sinyal değil** (Kakade ve diğ., Białkowski-Darolles-Le Fol 2008). Intraday "VWAP-pullback" stratejileri pratik (Zarattini-Aziz 2023, SSRN 🏛) ama hakemli mikroyapı çalışmaları sinyal değil execution maliyeti minimizasyonu üzerine.
- **Sonuç:** Hacim sinyalini volatilite-rejim onaylayıcısı (relative volume > 1.5×ortalama) olarak kullan, bağımsız tetikleyici yapma.

---

## 4. Piyasa Mikroyapısı (varlık-sınıfı bazında)

### 4.1 Kripto (Binance USDT-M perps)
- 🏛 **Funding rate ekstremleri kontrarian sinyal** — Glassnode, Coin Metrics, Coinglass raporları: yıllıklandırılmış funding < −15% (kalabalık short) genellikle yerel dip; > +30% (kalabalık long) ısınmış tepe. Negatif funding short-squeeze setup'ı için en açık sinyallerden biri (örn. Glassnode Mart 2026 raporu: persistent negative funding = yerel dip).
- 🏛 **Open Interest divergence:** fiyat yeni zirve / OI düşük ⇒ delaminasyon, trend zayıf. (Coin Metrics State of the Network).
- 🏛 **BTC dominance rejim göstergesi:** BTC.D yükselen ⇒ altların underperform'u; düşen + BTC stabil ⇒ alt-season penceresi. **Pratik öneri:** TradeRay tarayıcısı altcoin'lerin daha agresif sinyal vermesine BTC.D düşüşü gerektirsin.
- 🎓 Akademik kripto literatürü genç. Bianchi (2020) *J. Banking & Finance*: BTC ve ETH'de momentum etkisi gözlemleniyor; Liu-Tsyvinski (2021) *RFS*: kripto piyasası risk faktörleri (market, size, momentum) hisselere paralel ama daha gürültülü.
- 🛠 ICT/"likidite süpürme/stop hunt": **hakemli literatürde doğrulanmıyor**, agent-based simülasyonlar ve order-flow araştırmaları kısmen destekliyor ama trader folklor seviyesinde algoritma karar girdisi yapma.

### 4.2 ABD Hisseleri (SP500 / NASDAQ)
- 🎓 **PEAD (Post-Earnings Announcement Drift, Bernard-Thomas 1989, *J. Accounting Research*):** SUE-en üst decile, açıklamayı izleyen 60 gün boyunca yıllık ~%10+ alfa. 1974–1985 örneğinde 48 çeyrekten 41'inde pozitif. **Pratik:** kazanç-sonrası 5–60 gün arasında trend yönündeki entry'leri *desteklemek* için kullan; gün-of avoid et (haber-volatilitesi yön belirsiz).
- 🎓 **VIX** (Whaley 2000 *JPM* "Investor Fear Gauge"; Whaley 2009 *JPM* "Understanding the VIX"): geriye-bakışlı değil ileriye-bakışlı 30-günlük örtük vol. VIX>25 = yüksek-vol rejimi (BB genişler, false breakouts artar); VIX<13 = ısınmış, mean-reversion kısa-vadede ama kuyruk riski yüksek.
- 🛠 Sektör rotasyonu faiz rejimine göre (XLU/XLP defensive vs XLK/XLY cyclical); akademik literatür heterojen.
- **Mikroyapı:** RTH 09:30–16:00 ET. İlk 30 dakika ve son saat aşırı oynak — SCALP'te avoid önerilir veya ayrı parametre seti.

### 4.3 BIST (Borsa İstanbul)
- 🎓 **Çok-faktörlü modeller BIST'te çalışıyor:** Fama-French 3F/5F + Carhart momentum + q-faktör testleri (2008-2019), Borsa Istanbul Review'da yayımlanan çalışmalar momentum priminin BIST hisselerinde anlamlı olduğunu raporluyor. q-faktör modeli en yüksek açıklayıcı güç.
- 🏛 **TCMB MPC günü:** Kararlar saat 14:00 (TR saati) açıklanır; sonraki 5 iş günü özet PPK metni. **Pratik:** PPK günü 13:00–17:00 arası TradeRay BIST için yeni giriş yapmasın (haber-driven outlier riski).
- 🎓 **TL FX overlay:** USDTRY trendi BIST USD-bazlı performansı domine eder; TL zayıflığında ihracatçı/sanayi outperform, finans/iç-pazar underperform (IMF Article IV Turkey reports; BIS papers on EM currency-equity correlation, e.g., Kearns-Patel 2016 BIS Quarterly Review on FX-equity coupling). [kaynak gerekli — TR-spesifik makale doğrulanamadı]
- 🎓 **Yüksek enflasyon rejimi:** TÜFE > %25 ortamlarda nominal hisse getirileri ile reel getiriler arasında ayrışma; Modigliani-Cohn inflation illusion etkisi gelişen piyasalarda zayıflar. (Bekaert ve diğ. EM çalışmaları, NBER 🎓.)
- 🏛 **Likidite/spread yapısı:** BIST mid/small-cap'lerde gap riski yüksek, ATR-temelli stop'lar US hisselerine göre daha geniş çarpanlar gerektirir (örn. 2.0× yerine 2.5–3.0×).
- 🛠 **İngilizce hakemli literatür ince** — Türkçe kaynaklar: Borsa İstanbul Review (https://www.sciencedirect.com/journal/borsa-istanbul-review), SPK çalışma raporları, Bilkent/Koç/Sabancı yüksek lisans tezleri.

---

## 5. Volatilite ve Rejim Tespiti

- 🎓 **Hamilton 1989** (*Econometrica*): Markov regime-switching model — gizli durumlar arasında geçişler. Pratikte: 2 ya da 3 durum yeterli ("yüksek vol risk-off" / "düşük vol risk-on" / "trend"). Pratik uygulamada `arch` veya `statsmodels.tsa.regime_switching` modülleri.
- 🏛 **VIX-managed portfolios** (Moreira-Muir 2017 *JoF*): vol-targeting alfa eklenir; CBOE white papers VIX-rejim filtrelemesini destekler. Yüksek VIX'te risk pozisyonunu küçült.
- 🏛 **Kripto vol:** Deribit BVOL index, Coin Metrics realized-vol yayınları. Kriptoda gerçekleşmiş vol yıllıklandırılmış %40 üstü = "yüksek" rejim.
- **TradeRay için pratik rejim sinyali (basit, robust):**
  - ADX(14) > 25 → TRENDING; ADX < 20 → RANGING
  - Gerçekleşmiş 14-gün vol percentile (rolling 252) > 80 → HIGH-VOL (pozisyon büyüklüğünü %50 azalt)
  - Kripto için ek: |funding 24h ort| > 0.03% (yıllık ~%30) ⇒ aşırı kaldıraçlı rejim → mean-reversion mode

---

## 6. Pozisyon Büyüklüğü ve Risk Yönetimi

- 🎓 **Kelly (Thorp 1969)**: f* = (bp − q)/b, log-utility maksimizasyonu.
- 🎓 **Fractional Kelly (MacLean-Thorp-Ziemba, *Kelly Capital Growth Investment Criterion*)**: full Kelly kısa horizonda yıkıcı drawdown yapar (1/2 ya da 1/4 Kelly standart). Edge tahmin hatası → fractional zorunlu.
- 🛠 **Van Tharp 1%/2% kuralı**: literatürde matematiksel optimallik iddiası yok; ampirik olarak %2 / trade ile %26 max DD, %10 ile blowup. **TradeRay'in %2 / trade kuralı uygulayıcı standardına uyuyor — koru.**
- 🎓 **Vol-targeting (Harvey ve diğ. 2018, AQR Asness "Why Not 100% Equities" 1996)**: hedef vol = 10–15% portföy bazında; pozisyon büyüklüğünü her enstrümanın 30-günlük vol'una ters orantılı ayarla. Sharpe artışı belgelendi.
- 🎓 **ATR-tabanlı vs swing-tabanlı stop**: akademik karşılaştırma sınırlı; pratik retro testlerde ATR daha smooth, swing daha bilgilendirici ama gap riskine açık.
- **R:R = 2.0 sabit hedef değerlendirme:** 2 sayısının ampirik dayanağı yok; daha iyi yaklaşım — beklenti (`E[R] = win_rate * avg_R − loss_rate`) ölçülerek sistematik biçimde belirlenmeli. **Önerim:**
  - SCALP: tight stop, 1R–1.5R hedef (yüksek frekans, daha düşük R:R ama win-rate yüksek)
  - SHORT_TERM: 2R sabit (mevcut yapı)
  - MID_TERM: ATR-trailing (Chandelier exit, 3×ATR(22)) — büyük trendler kesilmesin

---

## 7. Backtest Metodolojisi (PROD ÖNCE ZORUNLU)

Mevcut TradeRay'in **hiçbir backtest'i yok**. Bu, López de Prado'nun *Advances in Financial Machine Learning* (2018) kitabında listelediği "ML fonlarının başarısız olma sebeplerinin" 7'sinden 6'sını birden tetikler.

### Tehlikeler ve çareler

| Tehlike | Açıklama | Çare |
|---|---|---|
| **Survivorship bias** | Sadece hala işlem gören sembollerle çalışmak | Tarihsel evrenler kullan (delisted symbols dahil) |
| **Look-ahead bias** | T zamanında olmayan bilgiyi kullanmak | Strict point-in-time data; rolling indicator hesabı |
| **Data-snooping** | Aynı veri üzerinde yüzlerce parametre denemek | White Reality Check (Sullivan-Timmermann-White 1999), Deflated Sharpe (López de Prado 2014) |
| **Overfitting** | Karmaşık model, az veri | Walk-forward (Pardo 1992), train/test ayrımı, k-fold *purged* CV (LdP 2018) |
| **Multiple testing** | "En iyi" indikatörü seçmek | Bonferroni / Benjamini-Hochberg düzeltmesi |
| **Regime change** | Geçmişte işleyen bugün işlemiyor | Out-of-sample dönem (en az son 24 ay), rolling Sharpe |

### Tavsiye edilen minimum pipeline (TradeRay için)

1. **Veri katmanı:** her sembol için en az 3 yıl 15dk + 5 yıl günlük; spreadi ve funding'i dahil et.
2. **Walk-forward:** 12-ay in-sample / 3-ay out-of-sample, kayan pencere.
3. **Bootstrap permutation test** (Aronson *Evidence-Based Technical Analysis* 2007): sinyal-getiri ilişkisini rastgele permüte ederek null dağılım oluştur, p-value hesapla.
4. **Deflated Sharpe Ratio** her aday parametre seti için raporla.
5. **Slippage + komisyon modellemesi:** kripto 5–10 bps perp + funding; ABD hisseleri 1–2 bps; BIST 10–15 bps; SCALP'te bu gizli düşman.

---

## 8. Multi-timeframe Hizalanması — Savunulabilir mi?

- 🛠 **Elder Triple Screen (1986):** uzun-trend, ortar-momentum, kısa-tetik — uygulayıcı çerçeve.
- 🎓 Hakemli kanıt zayıf. Multi-TF "aynı yönde EMA" filtresi her ekstra TF eklendiğinde *etkin sinyal sayısını düşürür* ama Sharpe artışı kontrolsüz testlerde kanıtlanmıyor; **muhtemelen serbestlik derecesi (overfitting kanalı)**.
- **TradeRay'de mevcut tüm intervalin EMA-pozisyonu aynı olmalı** kuralı katı; bu, sinyal frekansını düşürerek aşırı muhafazakar yapıyor ve gerçek seçicilikten çok p-hacking riski yaratıyor.
- **Önerim:** 2-katmanlı, en fazla 3. Sinyal TF'i + onay TF'i (≈4-6x daha uzun). Örn. SCALP: sinyal 15dk + onay 1h; SHORT_TERM: sinyal 4h + onay 1d; MID_TERM: sinyal 1d + onay 1w.

---

## 9. Piyasa × Term başına Önerilen Algoritma (12 kombinasyon)

> Notasyon: **TF** = trend-following, **MR** = mean-reversion, **HYB** = ADX rejimine göre dinamik. Eşikler başlangıç noktası; walk-forward ile ayarlanmalı.

### 9.1 CRYPTO (Binance USDT-M perps)

| Term | Sinyal TF / Onay | Bias | İndikatör stack | Eşikler | Gates (filtreler) | Risk |
|---|---|---|---|---|---|---|
| SCALP | 15m / 1h | MR | RSI(2), BB(20, 2σ), VWAP-pullback | LONG: RSI(2)<10 + price<BB_lower + 1h EMA50 üstünde | funding 24h ort ≠ aşırı (-15%<funding<+30% yıllıklandırılmış); rel vol > 1.2× | 1.0×ATR(14) stop, 1.5R hedef, lev ≤3x |
| SHORT_TERM | 4h / 1d | HYB | RSI(14), ADX(14), EMA20/50 | ADX>25: pull-back-in-trend (RSI<35 LONG, RSI>65 SHORT); ADX<20: BB-MR | BTC.D filtresi (alt için BTC.D düşmeli); haber takvimi (FOMC, CPI) | 1.5×ATR(14) stop, 2R hedef, lev ≤3x |
| MID_TERM | 1d / 1w | TF | 12-1 ay momentum, 50/200 EMA, ROC(20) | LONG: price>200EMA + 12-1mo ret>0; SHORT: tersi | Realized vol percentile < 90; funding ortalaması normal | ATR-trailing (Chandelier 3×ATR(22)), lev ≤2x |

**Atıflar:** Bianchi 2020 🎓; Liu-Tsyvinski 2021 🎓; Glassnode/Coinglass funding raporları 🏛; MOP 2012 🎓.

### 9.2 SP500 ve NASDAQ

| Term | Sinyal TF / Onay | Bias | İndikatör stack | Eşikler | Gates | Risk |
|---|---|---|---|---|---|---|
| SCALP | 15m / 1h | MR | RSI(2), VWAP, opening range | RSI(2)<5 LONG; opening range break + VWAP üstünde | İlk 30dk avoid; saat 15:30–16:00 ET avoid; VIX>30 boyut /2 | 1.0×ATR(14), 1.5R, no overnight |
| SHORT_TERM | 4h / 1d | HYB | RSI(14), ADX(14), 50SMA | ADX>20 + price>50SMA: pull-back LONG (RSI<35); ADX<20: range-fade | Kazanç günü ±1 işlem günü girişi yasak (Bernard-Thomas tabanlı PEAD avoidance entry değil exit için); VIX<25 | 1.5×ATR, 2R |
| MID_TERM | 1d / 1w | TF | 12-1 momentum, 200SMA, MACD-hist (momentum onayı) | LONG: price>200SMA AND 12-1 mo ret > median (cross-sectional) | Sektör rotasyon (relative strength vs SPX); FOMC haftası giriş yok | Chandelier 3×ATR(22) |

**Atıflar:** Jegadeesh-Titman 1993 🎓; AMP 2013 🎓; Bernard-Thomas 1989 🎓; Whaley 2000/2009 🎓; AQR vol-targeting 🏛.

### 9.3 BIST (.IS)

| Term | Sinyal TF / Onay | Bias | İndikatör stack | Eşikler | Gates | Risk |
|---|---|---|---|---|---|---|
| SCALP | 15m / 1h | MR (ihtiyatlı) | RSI(2), BB(20) | RSI(2)<10 LONG (mid-cap'lerde dikkat — likidite riski) | İlk 30dk avoid; PPK günü 13:00–17:00 girişe kapalı; USDTRY günlük |delta| > %2 ise pas | 2.0×ATR (BIST volatilite yüksek), 1.5R |
| SHORT_TERM | 4h / 1d | HYB | RSI(14), ADX, 50EMA, USDTRY trend | LONG: 50EMA üstünde + ADX>20 + USDTRY düşüş veya yatay | Sektör USDTRY duyarlılığı; rel volume > 1.5× | 2.0×ATR, 2R |
| MID_TERM | 1d / 1w | TF + faktör | 12-1 momentum (Carhart), 200SMA, q-faktör | LONG: 12-1 ret üst %30; price>200SMA | Enflasyon rejimi (TÜFE>%50 ise FX-hedged sektör tercih); TCMB faiz kararı haftası giriş yok | 2.5×ATR(22) Chandelier; pozisyon başı ≤%1.5 (BIST'te %2 yerine — likidite ve gap riski) |

**Atıflar:** Borsa İstanbul Review benchmark çalışmaları 🎓 (Fama-French + Carhart Türkiye'de çalışıyor); TCMB MPC takvimi 🏛; BIS EM currency-equity coupling 🏛.

---

## 10. TradeRay Kod Tabanı için Uygulama Yol Haritası

Repo referansları doğrulanmıştır:
- `agents/rule_engine.py` (225 satır) — LONG/SHORT yön kapısı, RSI/MACD/EMA mantığı.
- `data_fetchers/market_fetcher.py` (565 satır) — INDICATOR_LOOKBACKS tablosu.
- `data_fetchers/technicals.py` (179 satır) — RSI, MACD, EMA, ATR hesaplamaları.
- `execution/risk_manager.py` (67 satır) — TP/SL emir, R:R kontrolü.
- `rules/{crypto,bist,us_equities}_strategy.md` — Master Trader prompt'una enjekte edilen kural kitapları.

### Phase 1 — Parametre tweak (yeni kod yok, 1–2 gün)

1. **`agents/rule_engine.py`:** `RSI<40`/`>60` eşiklerini termo göre param-laştır:
   - SCALP: 25/75
   - SHORT_TERM: 30/70 (Wilder klasik)
   - MID_TERM: 35/65
2. **R:R sabitliğini termo göre değiştir:** SCALP 1.5, SHORT_TERM 2.0, MID_TERM 2.5 minimum (Chandelier'a geçiş Phase 3).
3. **Pozisyon büyüklüğü tavanı**: BIST'te %2 → %1.5 (gap riski).
4. **Multi-TF hizalanma kuralı**: tüm intervaller yerine sadece sinyal-TF + 1 onay-TF gerektir (mevcut "all available intervals agree" çok katı).
5. **`rules/*.md`** dosyalarına bu §9 tabloları ekle (Master Trader LLM'ye literatür-temelli bağlam).

### Phase 2 — Yeni gate'ler (orta düzey, 3–5 gün)

1. **ADX rejim filtresi** — `data_fetchers/technicals.py`'ye ADX(14) ekle; `rule_engine.py`'da `regime = "TREND" if ADX>25 else "RANGE" if ADX<20 else "TRANSITION"` ve mod-anahtarı.
2. **Hacim onay gate'i** — relative volume > 1.5×ortalama (20-bar) entry'ye eklensin.
3. **Crypto funding rate filtresi** — `data_fetchers/binance_fetcher.py`'da `fundingRate` çek (Binance `/fapi/v1/premiumIndex`); aşırı ekstremlerde (|funding| > 0.03%/8h) ya MR moduna geç ya da girişi atla.
4. **Earnings calendar guard (US)** — Finnhub / FMP API ile her sembol için sonraki kazanç tarihi; ±1 işlem günü içinde yeni pozisyon yok.
5. **TCMB PPK takvimi (BIST)** — manuel/sabit takvim listesi `config/`'a; PPK günü 13:00–17:00 trade yok.
6. **VIX rejim sayacı (US)** — VIX>25'te pozisyon /2; VIX>35'te yeni giriş kapalı (Whaley).

### Phase 3 — Büyük mimari değişiklikler (1–3 hafta)

1. **Walk-forward backtest framework** — `backtest/` klasörü yarat. `vectorbt` veya `backtrader` veya kendi minimalin. Walk-forward (Pardo) + bootstrap permutation (Aronson) + Deflated Sharpe raporu (LdP).
2. **Piyasa-spesifik rule_engine yolları** — `agents/rule_engine.py`'yı `agents/rule_engines/{crypto,us_equities,bist}.py` olarak parçala; ortak primitives ortak modülde. Mevcut tek-fonksiyon yapısı genişletme noktasında patlama riski yaratıyor.
3. **Regime-switching layer** — basit Markov 2-state model (yüksek-vol/düşük-vol) `data_fetchers/`'a. `statsmodels.tsa.regime_switching.MarkovRegression` ile günlük çalıştır, mevcut rejimi rule_engine'e ver.
4. **Volatility-targeted position sizing** — sabit %2 risk yerine, pozisyon büyüklüğü `(target_vol / asset_realized_vol) × (portfolio × 2%)` formülüne (AQR vol-targeting).
5. **Chandelier exit (MID_TERM)** — `execution/risk_manager.py`'a trailing stop modu; entry sonrası max(high) − 3×ATR(22) takip et.
6. **Slippage/komisyon simülasyonu** — `execution/tracker.py`'a paper-trading mode'da realistic slippage modeli.

---

## 11. Kaynakça

### 🎓 Hakemli akademik
- Jegadeesh, N., & Titman, S. (1993). "Returns to Buying Winners and Selling Losers: Implications for Stock Market Efficiency." *Journal of Finance*, 48(1), 65–91. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x
- Asness, C. S., Moskowitz, T. J., & Pedersen, L. H. (2013). "Value and Momentum Everywhere." *Journal of Finance*, 68(3), 929–985. https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12021
- Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012). "Time Series Momentum." *Journal of Financial Economics*, 104(2), 228–250. https://www.sciencedirect.com/science/article/pii/S0304405X11002613
- Brock, W., Lakonishok, J., & LeBaron, B. (1992). "Simple Technical Trading Rules and the Stochastic Properties of Stock Returns." *Journal of Finance*, 47(5), 1731–1764. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1992.tb04681.x
- Sullivan, R., Timmermann, A., & White, H. (1999). "Data-Snooping, Technical Trading Rule Performance, and the Bootstrap." *Journal of Finance*, 54(5), 1647–1691. https://onlinelibrary.wiley.com/doi/abs/10.1111/0022-1082.00163
- Lo, A. W., Mamaysky, H., & Wang, J. (2000). "Foundations of Technical Analysis: Computational Algorithms, Statistical Inference, and Empirical Implementation." *Journal of Finance*, 55(4), 1705–1765. https://onlinelibrary.wiley.com/doi/abs/10.1111/0022-1082.00265
- Lo, A. W. (2004). "The Adaptive Markets Hypothesis." *Journal of Portfolio Management*, 30(5), 15–29. https://jpm.pm-research.com/content/30/5/15
- Bernard, V. L., & Thomas, J. K. (1989). "Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?" *Journal of Accounting Research*, 27, 1–36.
- Whaley, R. E. (2000). "The Investor Fear Gauge." *Journal of Portfolio Management*, 26(3), 12–17.
- Whaley, R. E. (2009). "Understanding the VIX." *Journal of Portfolio Management*, 35(3), 98–105. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1296743
- Hamilton, J. D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle." *Econometrica*, 57(2), 357–384. https://users.ssc.wisc.edu/~behansen/718/Hamilton1989.pdf
- Thorp, E. O. (1969). "Optimal Gambling Systems for Favorable Games." *Review of the International Statistical Institute*, 37(3).
- MacLean, L., Thorp, E. O., & Ziemba, W. T. (eds., 2011). *The Kelly Capital Growth Investment Criterion: Theory and Practice*. World Scientific. https://www.worldscientific.com/worldscibooks/10.1142/7598
- López de Prado, M. (2018). "The 10 Reasons Most Machine Learning Funds Fail." *Journal of Portfolio Management*, 44(6). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- Bianchi, D. (2020). "Cryptocurrencies as an Asset Class? An Empirical Assessment." *Journal of Alternative Investments*. (Bianchi'nin kripto momentum analizleri).
- Liu, Y., & Tsyvinski, A. (2021). "Risks and Returns of Cryptocurrency." *Review of Financial Studies*, 34(6), 2689–2727.
- Harvey, C. R., Hoyle, E., Korgaonkar, R., Rattray, S., Sargaison, M., & Van Hemert, O. (2018). "The Impact of Volatility Targeting." *Journal of Portfolio Management*.
- Moreira, A., & Muir, T. (2017). "Volatility-Managed Portfolios." *Journal of Finance*, 72(4).
- Jegadeesh, N. (1990). "Evidence of Predictable Behavior of Security Returns." *Journal of Finance*, 45(3).
- Lehmann, B. N. (1990). "Fads, Martingales, and Market Efficiency." *Quarterly Journal of Economics*.
- Karpoff, J. (1987). "The Relation Between Price Changes and Trading Volume: A Survey." *J. Financial and Quantitative Analysis*, 22(1).
- Chong, T. T-L., & Ng, W. K. (2008). "Technical Analysis and the London Stock Exchange: Testing the MACD and RSI rules using the FT30." *Applied Economics Letters*.
- Borsa İstanbul Review — birden çok 2018–2024 makalesi, Fama-French/Carhart Türkiye benchmark çalışmaları. https://www.sciencedirect.com/journal/borsa-istanbul-review

### 🏛 Büyük kurumsal / sektörel araştırma
- AQR — "Value and Momentum Everywhere" insight sayfası. https://www.aqr.com/Insights/Research/Journal-Article/Value-and-Momentum-Everywhere
- AQR — "Time Series Momentum" insight. https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum
- Glassnode Insights — funding rate, OI, BTC dominance ardışık raporları. https://insights.glassnode.com/
- Coin Metrics — *State of the Network* haftalık raporları.
- Bitwise Asset Management — kripto araştırma whitepaper'ları.
- CBOE — VIX whitepaper ve methodology dokümanı.
- Coinglass — derivatives ve funding-rate dashboard'u. https://www.coinglass.com/learn/how-to-judge-market-by-fr-en
- TCMB — Para Politikası Kurulu metinleri. https://www.tcmb.gov.tr/wps/wcm/connect/en/tcmb+en/main+menu/core+functions/monetary+policy
- BIS — EM currency-equity coupling working papers (Kearns-Patel 2016 ve diğ.).
- Zarattini, C., & Aziz, A. (2023). "Volume Weighted Average Price (VWAP): The Holy Grail for Day Trading Systems." SSRN.

### 🛠 Uygulayıcı klasikler
- Wilder, J. W. (1978). *New Concepts in Technical Trading Systems*. (RSI, ATR, ADX/DMI orijinal kaynak.)
- Tharp, V. K. (2008). *Definitive Guide to Position Sizing*.
- Tharp, V. K. (2007). *Trade Your Way to Financial Freedom*.
- Connors, L., & Alvarez, C. (2008). *Short Term Trading Strategies That Work*.
- Bollinger, J. (2001). *Bollinger on Bollinger Bands*.
- Elder, A. (1993). *Trading for a Living* (Triple Screen sistemi).
- Carver, R. (2015). *Systematic Trading: A Unique New Method for Designing Trading and Investing Systems*.
- Pardo, R. (2008). *The Evaluation and Optimization of Trading Strategies* (2nd ed.). Wiley. (Walk-forward analysis orijinal kaynağı.) https://onlinelibrary.wiley.com/doi/10.1002/9781119196969.ch11
- Aronson, D. (2007). *Evidence-Based Technical Analysis*. (Bootstrap permutation tests.)
- Appel, G. — MACD orijinal yayını.
- Granville, J. — OBV orijinal kaynağı.

### Doğrulanamayan / işaret edilmesi gereken
- ICT (Inner Circle Trader) "smart money concepts", "likidite süpürme" — hakemli kaynak doğrulanamadı, uygulayıcı folklor sınıfında bırakıldı.
- "TL FX overlay BIST sektörlerinin USD-bazlı performansını domine eder" iddiası — Türkçe ampirik çalışma var ama spesifik makale erişiminden doğrulanamadı; IMF Article IV Turkey ve BIS coupling literatürüne sırtla.
- TradeRay'in `rules/*.md` dosyalarının mevcut içeriğini *doğrudan* okumadım (zaman tasarrufu); §10'daki kod referansları `ls` çıktısından + 4 anahtar dosyanın satır sayısından doğrulandı.

---

## Ek: Kullanım Notu

Bu rapor TradeRay sistem geliştirme sürecini *literatür-grounded* hale getirmek için bir başlangıç çerçevesidir. Üretime gitmeden ÖNCE:

1. Phase 1 değişikliklerini bile **walk-forward backtest** ile doğrula.
2. Eşikleri (RSI cutoff, ADX threshold, ATR çarpanı) **per-market per-term grid search** yap, sonra **Deflated Sharpe** ile gerçek anlamlılığı kontrol et.
3. Live ortamda **paper-trading** (Binance Testnet zaten var) ile en az 30 trade — yön doğruluğunu ölç.
4. Bu raporun §9 tablolarını canlı tutmak için **rules/*.md** dosyalarını birer "living document" yap.

Brutal honesty: bugün TradeRay'in en büyük teknik borcu **eksik backtest framework**. RSI eşiğini düzeltmenin bile değeri ancak bu altyapı kurulduktan sonra ölçülebilir hale gelir.
