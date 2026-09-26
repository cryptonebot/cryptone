# CRYPTONE V4.5 — MASTER DOCUMENT (SOURCE OF TRUTH)

Market Screener Radar + Trader Assistant
Single-file merge dari 16 file. Urutan: 00 → 14.

---

## DAFTAR ISI

| # | File asal | Isi | Status di master |
|---|-----------|-----|------------------|
| 00 | README.md | Index + protokol kolaborasi | Ada |
| 01 | 00_PRODUCT.md | Definisi produk & 7 prinsip inti | Ada |
| 02 | 01_ARCHITECTURE.md | Arsitektur 8 layer | Ada |
| 03 | 02_PARAMETERS.md | Parameter framework 4 kategori | Ada |
| 04 | 03_DISCOVERY.md | Mekanisme discovery | Ada |
| 05 | 04_SETUPS.md | 3 setup trading | Ada |
| 06 | 05_DATA_SOURCES.md | 14 sumber data dengan peran unik | Ada |
| 07 | 06_MEMORY.md | Skema SQLite | Ada |
| 08 | 07_BLACKSWAN.md | Black swan + correlation | Ada |
| 09 | 08_VOLUME_SESSION.md | Volume & session | Ada |
| 10 | 09_WALLET.md | Wallet tracking 3 layer | Ada |
| 11 | 10_LLM.md | Integrasi Gemini | Ada |
| 12 | 11_TELEGRAM.md | Telegram Super App | Ada |
| 13 | 12_DEPLOY.md | Deploy & operasional | Ada |
| 14 | 13_ROADMAP.md | Roadmap implementasi | Ada |
| 15 | 14_ENGINEER_PROMPTS.md | Prompt AI engineer | Ada |
| 16 | 15_SIGNAL_LIFECYCLE.md | Signal lifecycle & expiry | Ada (baru) |
| 17 | 16_CLI.md | CLI interface | Ada (baru) |
| 18 | 17_ERROR_HANDLING.md | Error handling & recovery | Ada (baru) |

---


# BAGIAN 00 — README

## Cryptone V4.5 — Source of Truth

Market Screener Radar + Trader Assistant

### Apa Ini

Kumpulan dokumen arsitektur V4.5. Setiap bagian adalah satu sumber kebenaran tunggal. Kalau ada revisi, revisi di bagian yang sesuai — bukan di chat atau tempat lain.

### Status

Semua bagian: FINAL. Gap konsep nol. Siap implementasi.

### Protokol Kolaborasi

Dokumen ini bisa dikerjakan multi-AI/engineer, sesi terpisah. Protokol berikut pengganti ingatan bersama.

**Aturan #1 — Definisi Kanonik, Bukan Duplikat**
Kalau nambah field/fungsi/dataclass yang sudah ada, tambahkan ke definisi aslinya. Cari definisi pertama, edit di situ. Satu konsep = satu definisi.

**Aturan #2 — Riwayat Cross-Check Wajib**
Setiap bagian yang mengubah/memanggil/bergantung bagian lain, wajib tutup dengan tabel: Perubahan sumber | Titik yang ikut berubah | Kenapa.

**Aturan #3 — Status FINAL Bukan "Jangan Disentuh"**
FINAL berarti gap konsep nol saat ditulis. Kalau nemu bug, perbaiki di titik asalnya + catat di Riwayat Cross-Check.

**Aturan #4 — Jangan Menjawab Pertanyaan yang Sudah Dijawab**
Sebelum usul desain baru, cek dulu apakah pertanyaan itu sudah punya jawaban di bagian lain. Kalau ada tapi kurang lengkap, perluas.

**Aturan #5 — Setiap Parameter Wajib Punya Kategori**
Parameter baru wajib masuk salah satu dari 4 kategori (A/B/C/D, Bagian II). Nggak boleh ada parameter "ngambang".

**Aturan #6 — Single File Dulu, Modular Nanti**
Implementasi fase awal: single file `cryptone_v45.py` (~7k baris). Pecah ke modular cuma setelah semua fitur P0+P1 selesai + stabil minimal 30 hari. Jangan pecah terlalu dini.

**Aturan #7 — Simbol di Dokumen = Ilustrasi, Bukan Konfigurasi**
BTC, ETH, SOL (dan simbol lain) yang muncul di contoh output, pseudo-code, dan tabel cuma **simulasi tampilan**. Bot **tidak pernah** hardcode daftar coin. Universe selalu dinamis dari Layer 0 (HL meta → filter → 30-40 kandidat), dan simbol yang tampil di sinyal/radar/market adalah hasil seleksi runtime berdasarkan konteks saat itu. Satu-satunya "anchor" simbol yang dibolehkan adalah **market anchor** (lihat Bagian II §II.8), dan itu pun dipilih otomatis, bukan diketik manual.

### Sebelum Upload Versi Baru

- [ ] Field/fungsi baru yang dipanggil sudah didefinisikan
- [ ] Fungsi yang didefinisikan sudah dipanggil (bukan orphan)
- [ ] Parameter baru masuk kategori A/B/C/D
- [ ] Tidak ada simbol hardcode di kode (pakai universe dinamis / market anchor)
- [ ] Riwayat Cross-Check ditulis
- [ ] Index diupdate kalau nambah bagian
- [ ] `check_orphans.py` (§XIX.6) exit code 0 sebelum commit

### Reading Order

18+ bagian — engineer baru mulai dari sini, bukan baca linear dari atas.

**Minimum (≈2 jam) — cukup untuk paham alur & mulai baca kode:**
```
00 (README) → 01 (Produk) → I (Arsitektur 8 Layer) → II (Parameter)
  → XVI (Signal Lifecycle) → XVIII (Error Handling) → XIX (Contract)
```

**Lengkap (≈1 hari) — sebelum mulai implementasi serius:**
```
00 → 01 → I → II → III (Discovery) → IV (Setup) → V (Data Sources)
  → VI (Memory/Schema) → VII+VIII (Black Swan + Correlation)
  → IX (Volume/Session) → X (Wallet) → XI (LLM) → XII (Telegram)
  → XIII (Deploy) → XIV (Roadmap) → XV (Engineer Prompts)
  → XVI (Signal Lifecycle) → XVII (CLI) → XVIII (Error Handling)
  → XIX (Contract)
```

Kalau cuma mau ngerjain satu fitur spesifik: baca I + II dulu (semua bagian bergantung ke sini), lalu bagian yang relevan + Riwayat Cross-Check di ujung bagian itu untuk tahu titik lain yang mungkin perlu ikut berubah.

### Filosofi V4.5

Radar, bukan executor.
Math first, LLM second.
Parameter framework 4 kategori.
Fail-soft, tapi alert.
Beda peran, beda layer.
Context ≠ Vote.
Zero fee, no compromise.

Bot = organisme hidup. Nggak ada final. Selalu dirawat.

---


# BAGIAN 01 — 00_PRODUCT.md

## Bagian 0 — Definisi Produk & Prinsip Desain

Status: FINAL

## 0.1 Definisi Produk

Cryptone V4.5 = **Market Screener Radar + Trader Assistant.**
Bot scan universe Hyperliquid, nemu setup valid, kasih sinyal lengkap dengan reasoning, dan bantu trader dengan tools. Bukan executor.

### Yang Bot LAKUKAN
- Scan ~200 symbol HL setiap 15 menit
- Deteksi setup valid dari anomali market
- Klasifikasi horizon (scalping / intraday / swing)
- Push sinyal dengan full layer analysis report
- Deteksi black swan & warning operator
- Kasih tools: calc, alerts, journal, wallet tracking, backtest

### Yang Bot TIDAK LAKUKAN
- Eksekusi order (lo manual)
- Manage posisi (lo yang manage)
- Tau modal lo (nggak perlu)
- Prediksi black swan sebelum terjadi (mustahil)
- Kasih financial advice

## 0.2 Tujuh Prinsip Inti

| # | Prinsip | Konsekuensi Desain |
|---|---------|--------------------|
| 1 | Radar, bukan executor | Nggak ada risk management, position sizing, eksekusi order |
| 2 | Math first, LLM second | Semua keputusan pakai matematika. LLM cuma tag & konteks |
| 3 | Parameter framework 4 kategori | Setiap parameter punya kategori eksplisit (A/B/C/D) |
| 4 | Fail-soft, tapi alert | Source mati → lanjut. Critical source mati → alert |
| 5 | Beda peran, beda layer | Agent punya fungsi unik, bukan semua vote arah |
| 6 | Context ≠ Vote | Macro/Narrative/Social cuma modifier confidence |
| 7 | Zero fee, no compromise | Semua source gratis, LLM free tier |

## 0.3 Realita Operasional

Bot ini bukan mesin uang. Bot ini radar yang beneran membantu trader.

| Fase | Yang terjadi |
|------|--------------|
| Minggu 1-2 | Fix teknis (bot jalan 5 jam tanpa crash) |
| Minggu 3-4 | Observasi (kumpulin data, jangan tuning) |
| Minggu 5+ | Tuning bertahap (1 parameter per minggu) |
| Bulan 2-3 | Precision stabil 55-65% |

Bot = organisme hidup. Nggak ada final. Selalu dirawat.

## 0.4 Yang Lo Dapetin

| Aspek | Nilai |
|-------|-------|
| Coverage | 200 symbol HL → 30-40 tradeable |
| Frekuensi sinyal | 5-15 per hari (mix tier A/B/C) |
| Signal latency | 5-10 detik dari trigger |
| Reasoning | Full layer (TA + FA + makro + mikro + onchain) |
| Tools | Calc, alerts, journal, wallet, backtest |
| Warning | Black swan detection 5-15 menit setelah shock |
| Biaya | $0/bulan |

## 0.5 Perbandingan dengan V4

| Aspek | V4 | V4.5 |
|-------|----|------|
| Peran bot | Analyzer per symbol | Screener |
| Arsitektur | 7 agent vote arah | 8 layer pipeline |
| Parameter | ~70% dinamis | 40/30/20/10 framework |
| Setup | Trade semua setup | 3 setup spesifik |
| Horizon | Single | Multi (scalp/intraday/swing) |
| Black swan | ❌ | ✅ 7 indikator |
| Correlation | ❌ | ✅ Per-symbol context |
| Signal tier | ❌ | ✅ A/B/C |
| Assistant | ❌ | ✅ 6 fitur |
| Deploy | VPS | GitHub Actions |

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Reframe executor → radar | Bagian II (kategori A berubah) | Radar nggak manage modal |
| 7 prinsip inti | Semua bagian | Filter keputusan desain |

---

# BAGIAN I — ARSITEKTUR 8 LAYER (01_ARCHITECTURE.md)

Status: FINAL

## I.1 Peta Layer

```
L0  Universe Scanner      (15 menit)  → 30-40 candidate
L1  Trigger Detection     (5 menit)   → symbol dengan trigger
L2  Horizon Classifier                → scalp/intraday/swing
L3  Confirm (min 2/3)                 → verified setup
L4  Timing (Analyst)                  → entry/SL/TP
L5  Context Modifier                  → confidence multiplier
L6  Veto + Tier                       → A/B/C tier
L7  Delivery + Assistant              → signal + tools

PARALEL: Black Swan Detector
```

## I.2 Detail Tiap Layer

### Layer 0 — Universe Scanner

- Frekuensi: 15 menit
- Input: HL meta endpoint (REST)
- Output: 30-40 tradeable candidate

Proses:
1. Fetch ~200 symbol dari HL
2. Filter volume (per cap)
3. Filter spread
4. Filter ATR > 0.5%
5. Filter listing age > 7 hari
6. LLM tagging sector + narrative (tiap 4 jam)

### Layer 1 — Trigger Detection

- Frekuensi: Tier 1 (5 menit), Tier 2 (15 menit), Tier 3 (60 menit)
- Input: candidate list
- Output: symbol dengan trigger aktif

3 Agent:
- Funding Agent — funding > P95?
- Liquidation Agent — cascade > P90? (WS Binance)
- Volatility Agent — ATR compression < P20?

Kalau nggak ada trigger → skip cycle.

### Layer 2 — Horizon Classifier

- Input: trigger type + magnitude + TF
- Output: horizon (scalping / intraday / swing)

```python
if trigger == "cascade" and size > P90:
    return "SCALPING"      # 5m/15m, lifetime 2h
if trigger == "funding_extreme":
    return "INTRADAY"      # 1H/4H, lifetime 24h
if trigger == "volatility_compression":
    return "SWING"         # 4H/1D, lifetime 7d
```

Satu symbol bisa punya multiple sinyal di horizon berbeda.

### Layer 3 — Confirm (min 2 dari 3)

3 Agent:
- Microstructure — absorption / imbalance
- Flow — whale netflow berlawanan arah signal
- Cross-Exchange — Binance/Bybit sync

Min 2 dari 3. Kalau kurang → tunggu cycle berikutnya.

**Aturan saat agent mati (fail-soft, §V.6 & §X.2):** N = jumlah agent L3 yang aktif (bukan mati karena source down, mis. Flow Agent off kalau `CRYPTONE_EXCHANGE_WALLETS` kosong). Syarat confirm dihitung **relatif terhadap N**, bukan selalu "2 dari 3 nominal":

```
min_confirm = ceil(2 * N / 3)
```

| N aktif | min_confirm | Catatan |
|---------|--------------|---------|
| 3 | 2 | Normal (2/3) |
| 2 | 2 | Kedua agent yang tersisa **harus** confirm — bukan otomatis lolos dengan 1/2 |
| 1 | — | L3 **dianggap gagal** (fail-soft L3 = "Tidak", §I.3). Sinyal tidak jadi, apapun hasil agent tunggal itu. |
| 0 | — | L3 gagal total, cycle di-skip. |

Ini mencegah dua salah kaprah: (a) tetap mensyaratkan 2/3 nominal saat N=2 sehingga L3 jadi hampir mustahil lolos (harus dua-duanya fire, lebih ketat dari desain), dan (b) diam-diam melonggarkan jadi 1/2 saat satu agent mati. Log `active_agent_count` di `signal_history` (kolom diagnostik) setiap kali confirm dievaluasi dengan N < 3, supaya operator bisa audit kapan gate ini berjalan dalam mode terdegradasi.

### Layer 4 — Timing (Analyst Agent)

- Input: confirm pass
- Output: entry zone + SL + TP1 + TP2 + R:R + **base_confidence**

Metode:
- Swing high/low detection
- BOS/CHoCH identification
- Zona SMC (DBR/DBD/RBR/RBD)
- FVG (Fair Value Gap)
- ATR-based SL

**Formula `base_confidence` (dipakai di L6, §I.2 Layer 6):**

```
base_confidence = clamp(
    w1 * trigger_strength      # seberapa ekstrem trigger L1 (mis. funding percentile jarak dari P95, cascade size percentile)
  + w2 * (confirm_count / 3)   # 2/3 = 0.667, 3/3 = 1.0 -- makin banyak confirm L3, makin tinggi
  + w3 * rr_quality,           # R:R ternormalisasi, mis. min(realized_rr / 3.0, 1.0)
    0.0, 1.0
)
```

- `trigger_strength` ∈ [0, 1]: dihitung dari seberapa jauh nilai trigger L1 melampaui threshold-nya (percentile-based, konsisten dengan Aturan #7 zero-hardcode).
- `confirm_count`: jumlah agent L3 yang confirm (2 atau 3; kalau L3 gagal, sinyal tidak sampai L4 sama sekali).
- `rr_quality` ∈ [0, 1]: R:R dari L4 sendiri, ternormalisasi terhadap cap wajar (default 3.0R, Kategori D cold-start).
- `w1, w2, w3`: bobot Kategori C (semi-dynamic, §II.4), default `w1=0.4, w2=0.35, w3=0.25` (jumlah = 1.0), di-tune dari histori precision per kombinasi bobot.
- `base_confidence` inilah yang jadi input `confidence_final = L5.confidence_multiplier × base_confidence` di Layer 6 — bukan angka arbitrary, dan bukan R:R itu sendiri (R:R sudah masuk sebagai salah satu komponen lewat `rr_quality`).

### Layer 5 — Context Modifier

6 Agent (bukan vote, cuma modifier):

| Agent | Fungsi |
|-------|--------|
| Structure | Regime cocok? |
| Macro | Risk environment |
| Narrative | Theme relevan? (Gemini) |
| Social | Retail alignment (contrarian) |
| Correlation | BTC dominance impact |
| News Impact | Macro/sector/project classifier |

Output: confidence multiplier per symbol (0.5x - 1.2x).

### Layer 6 — Veto + Tier

**Dipakai duluan:** `confidence_final = L5.confidence_multiplier × base_confidence` (base_confidence dari L4/analyst) — dihitung sebelum tier assignment di bawah, dan `confidence_final` inilah yang dipakai untuk cek `confidence floor` (Kategori C, §II.4) dan ditampilkan ke user.

Risk Agent cek:
- Blackout window (<1 jam ke event)
- Anchor conflict
- Anchor flip rejected

Tier assignment:
- Tier A — R:R ≥2.5 + 3/3 confirms
- Tier B — R:R ≥2.0 + 2/3 confirms
- Tier C — sisanya

**Aturan confluence boost (§IV.7) vs syarat confirm:** Confluence multi-TF (3/4 TF aligned) **TIDAK PERNAH** menggantikan syarat confirm L3. Confluence hanya menaikkan R:R-effective yang dipakai untuk tier check, bukan jumlah confirm. Jadi:
- Setup dengan 2/3 confirm + R:R ≥2.0 + confluence 3/4 TF → tier B di-boost ke A **hanya kalau** R:R (asli atau confluence-adjusted) juga ≥2.5. Confluence tidak membuat 2/3 confirm dianggap setara 3/3.
- Definisi Tier A (§I.2 Layer 6) tetap: R:R ≥2.5 **DAN** 3/3 confirms. Kalau confirm cuma 2/3, tier maksimum yang bisa dicapai — dengan atau tanpa confluence boost — adalah **B**, bukan A.
- Confluence boost yang valid: tier C → B (kalau R:R ≥2.0 tercapai lewat confluence-adjusted score), bukan B → A jika confirm masih 2/3.

### Layer 7 — Delivery + Assistant

Signal format + chart + tools.

Commands: /radar /calc /alert /journal /wallet /stats /playbook /breadth

6 Fitur assistant:
1. Backtest on-demand
2. Playbook presets
3. Confluence score
4. Market breadth
5. RR visualizer
6. Setup performance

## I.3 Fail-Soft per Layer

| Layer | Bisa skip kalau gagal? |
|-------|------------------------|
| L0 Universe | Ya (pakai cache) |
| L1 Trigger | Ya (skip cycle) |
| L2 Horizon | Ya (default INTRADAY) |
| L3 Confirm | **Tidak** (min 2/3) |
| L4 Timing | Ya (pakai default entry) |
| L5 Context | Ya (multiplier 1.0) |
| L6 Veto | Ya (auto-approve) |
| L7 Delivery | Ya (retry queue) |

Critical exception: L3 nggak bisa di-skip — kalau <2 confirm, signal nggak jadi.

## I.4 Layer Dependencies

```
L0 → L1 → L2 → L3 → L4 → L5 → L6 → L7
                                  ↑
                            Black Swan
                            (paralel)
```

Setiap layer baca output layer sebelumnya. Layer 3 baca L1 + L2. Layer 6 baca L5.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 8 layer | Bagian II (parameter tiap layer) | Parameter harus match layer |
| L3 min 2/3 | Bagian IV (setup cek confirm) | Setup bergantung confirm rate |
| L4 Timing | Bagian III (Analyst Agent) | Analyst di kedua bagian |

---


# BAGIAN II — PARAMETER FRAMEWORK (02_PARAMETERS.md)

Status: FINAL

## II.1 Prinsip

> "Hardcode apa yang seharusnya jadi hukum. Dinamiskan apa yang seharusnya responsif."

Framework 4 kategori:

| Kategori | Definisi | Bisa tune? | Contoh |
|----------|----------|-----------|--------|
| A — Framework Default | Hukum sinyal (default) | ⚠️ Hanya via whitelist §II.6 | Lifetime, min R:R, cooldown |
| B — Dynamic | Respons market | ❌ Auto (per symbol) | Funding P95, cascade P90 |
| C — Semi-Dynamic | Boundary + flexibility | ⚠️ Manual override dalam boundary (§II.6) atau auto | Entry zone, confidence floor |
| D — Starting | Jembatan cold-start | ⚠️ Auto-replaced | Confidence default, reliability |

Ratio target: 40% A · 30% B · 20% C · 10% D

**Aturan #5 (diperjelas):** Kategori A = default framework yang boleh di-tune **hanya** lewat whitelist §II.6 (dalam range yang didefinisikan di sana). Kategori C = boundary hardcoded + nilai tengah dinamis by default, tapi boundary itu sendiri boleh di-override manual **dalam boundary-nya** lewat whitelist §II.6 juga. §II.7 adalah satu-satunya daftar parameter yang **tidak boleh disentuh dari jalur apapun** — beda dari A dan C yang masih boleh di-tune terbatas.

## II.2 Kategori A — Framework Default (tunable via §II.6 whitelist)

Hukum sinyal. Nilai default; sebagian boleh ditune lewat whitelist §II.6 (lihat tabel di sana untuk parameter mana yang boleh dan range-nya) — parameter yang benar-benar tidak boleh disentuh sama sekali ada di §II.7, bukan di sini.

### Signal Lifetime

| Parameter | Nilai | Alasan |
|-----------|-------|--------|
| Scalping | 2 jam | Sinyal scalping expired cepat |
| Intraday | 24 jam | Standar intraday |
| Swing | 7 hari | Swing butuh waktu |

### Min R:R

| Parameter | Nilai |
|-----------|-------|
| Scalping | 1:1.5 |
| Intraday | 1:2 |
| Swing | 1:3 |

### Confirm & Limits

| Parameter | Nilai |
|-----------|-------|
| Min confirmations | 2 dari 3 |
| Max concurrent signals | 10 |
| Signal cooldown per symbol | 30 menit |
| Event blackout window | 60 menit |

### Tier Threshold

| Tier | Min R:R | Min Confirms |
|------|---------|--------------|
| A | 1:2.5 | 3 |
| B | 1:2.0 | 2 |
| C | sisanya | sisanya |

### Black Swan

| Parameter | Nilai |
|-----------|-------|
| Trigger count | 3 dari 7 indikator |
| Duration minimal | 2 jam |
| Total indikator | 7 |

### Wallet & Intervals

| Parameter | Nilai |
|-----------|-------|
| Max user wallets | 5 |
| Universe scan interval | 15 menit |
| Trigger check (Tier 1) | 5 menit |
| Correlation update | 60 menit |
| Volume baseline update | 14 hari |

## II.3 Kategori B — Dynamic (Per Symbol)

Nilai dihitung runtime dari histori per symbol.

| Parameter | Basis | Min Sample | Lookback |
|-----------|-------|-----------|----------|
| Funding extreme | P95 | 30 | 30 hari |
| Cascade size | P90 | 30 | 30 hari |
| ATR compression | P20 | 30 | 30 hari |
| ATR spike | P99 | 30 | 30 hari |
| OI change | P80 | 30 | 30 hari |
| Orderbook imbalance | P80 | 30 | 30 hari |
| Volume spike | P80 | 30 | 30 hari |

Kalau sample < min: pakai default dari Kategori D.

## II.4 Kategori C — Semi-Dynamic

Boundary hardcoded, nilai tengah dinamis.

| Parameter | Boundary | Dinamis |
|-----------|----------|---------|
| Entry zone width | 0.3x - 2.0x ATR | Dari ATR sekarang |
| Confidence floor | 0.3 - 0.7 | Dari histori precision |
| Tier A count/hari | 3 - 10 | Dari market volatility |
| Tier B count/hari | 10 - 30 | Dari market volatility |
| Session spike | 1.3x - 3.0x | Dari baseline jam |

Boundary di atas (termasuk Tier A/B count) auto by default. Operator boleh override manual di dalam boundary itu lewat whitelist §II.6 — bukan kontradiksi, itu memang cara kerja Kategori C: "auto by default, manual override allowed in whitelist".

**Formula Confidence floor (gap sebelumnya: "dari histori precision" tanpa rumus):**

```
confidence_floor = clamp(
    0.3 + 0.4 * (precision_recent - precision_target) / precision_target,
    0.3, 0.7
)
```

- `precision_recent`: win rate realized (bobot PARTIAL 0.5, sama seperti §IV.6/§XVI) dari **50 sinyal terakhir** across semua symbol/setup.
- `precision_target`: target win rate rata-rata tertimbang dari 3 setup (§IV.6: 60%/55%/50%), dihitung ulang tiap kali proporsi setup yang jalan berubah signifikan.
- Kalau `precision_recent >= precision_target`: floor turun (lebih permisif, lebih banyak sinyal lolos) mendekati 0.3.
- Kalau `precision_recent < precision_target`: floor naik (lebih ketat) mendekati 0.7.
- Cold-start (Kategori D, §II.5): sebelum 50 sinyal terkumpul, `confidence_floor = 0.5` (titik tengah boundary), bukan hasil formula di atas.
- Formula ini menghasilkan angka di dalam boundary [0.3, 0.7] secara matematis (bukan arbitrary), dan tetap bisa di-override manual dalam boundary itu lewat whitelist §II.6.

## II.5 Kategori D — Starting (Cold-Start)

Cuma dipakai sebelum histori cukup.

| Parameter | Value | Diganti Setelah |
|-----------|-------|-----------------|
| Confidence cold-start | 0.5 | 20 outcome |
| Follow-through window | 24 jam | 10 outcome |
| Impulse ratio default | 0.015 | 15 zone sample |
| Zone tolerance default | 0.02 | 15 ATR sample |
| Reliability default | 0.5 | 10 samples |
| Volume baseline fallback | 1.0x | 14 hari histori |

## II.6 Whitelist Tuning via Telegram

Bukan semua parameter bisa di-tune. Cuma yang aman.

| Parameter | Range | Step | Preset |
|-----------|-------|------|--------|
| Funding P-value | 90-99 | 1 | 90, 95, 97, 99 |
| Cascade P-value | 85-99 | 1 | 85, 90, 95 |
| Min R:R Intraday | 1.5-3.0 | 0.25 | 1.5, 2, 2.5, 3 |
| Min R:R Swing | 2.0-4.0 | 0.5 | 2, 3, 4 |
| Signal Cooldown | 15-60 min | 15 | 15, 30, 60 |
| Tier A count/hari | 3-10 | 1 | 3, 5, 10 |
| Tier B count/hari | 10-30 | 5 | 10, 20, 30 |
| Stop-hunt Volume Z | 1.5-3.0 | 0.25 | 1.5, 2, 2.5 |
| Stop-hunt OI Z | 1.5-3.0 | 0.25 | 1.5, 2, 2.5 |
| Stop-hunt Price Change | 0.3-1.0% | 0.1 | 0.3, 0.5, 0.8 |
| Stop-hunt Cross-Exchange Div | 0.3-0.8% | 0.1 | 0.3, 0.5, 0.8 |

Setiap perubahan di-log ke `settings_history`. Rollback via `/settings rollback <timestamp>`.

## II.7 Parameter yang JANGAN Diubah

Ini **satu-satunya** daftar "locked sejati" (tidak boleh disentuh dari jalur apapun, termasuk whitelist §II.6). Parameter Kategori A lain yang tidak muncul di sini masih boleh ditune terbatas lewat §II.6.

| Parameter | Value | Alasan |
|-----------|-------|--------|
| Min confirmations | 2 dari 3 | Hukum setup valid |
| Black swan trigger | 3 dari 7 | Sensitivitas shock |
| Zero-fee principle | — | Filosofi proyek |
| Framework 4 kategori | — | Governance |
| Single-writer pattern | — | Konsistensi DB |

## II.8 Universe Dinamis & Market Anchor

**Prinsip:** nggak ada daftar simbol yang diketik manual. Semua simbol datang dari Layer 0 dan berubah tiap cycle sesuai kondisi market.

### Universe Dinamis

| Konsep | Definisi | Kategori |
|--------|----------|----------|
| `universe` | ~200 symbol dari HL `meta`, di-refresh tiap 15 menit | A (interval), B (filter dari histori) |
| `candidates` | Hasil filter volume/spread/ATR/listing age (30-40 symbol) | B — dinamis |
| `active_symbols` | Symbol yang lolos anomali & sedang dianalisis | B — dinamis |
| `pinned_symbols` (opsional) | Symbol yang operator MAU selalu ikut di-scan, tetap harus lolos filter | D — kosong by default |

`pinned_symbols` bukan whitelist. Symbol yang di-pin tetap wajib lolos filter likuiditas Layer 0. Kalau nggak lolos, di-skip dan di-log, bukan dipaksa masuk.

### Market Anchor

Beberapa logika butuh satu "wakil pasar" (regime black swan, korelasi makro). Wakil ini **dipilih otomatis**, bukan hardcode:

```python
def get_market_anchor(memory, universe_stats):
    """
    Pilih anchor = symbol dengan volume 24h tertinggi di universe HL
    (rank #1). Biasanya BTC, tapi kalau kondisi berubah, anchor ikut berubah.

    KONTRAK None: kalau `universe_stats` kosong (L0 gagal total) DAN
    `memory.get_last_anchor()` juga belum pernah ada (instalasi baru,
    belum ada history) -- fungsi ini me-return None. Caller WAJIB cek
    None secara eksplisit:
      - Di dalam cycle normal (L1-L6, Black Swan): anchor None -> SKIP
        CYCLE sepenuhnya (sama seperti fail-soft L1, §I.3), log ke
        `data_source_health`, JANGAN lanjut dengan anchor kosong/dummy.
      - Di startup/bootstrap: anchor None dianggap wajar (belum ada data),
        bukan error -- tunggu universe scan pertama berhasil.
    Tidak boleh ada downstream code yang mengasumsikan return value
    selalu string simbol valid.
    """
    ranked = sorted(universe_stats, key=lambda s: s.volume_24h, reverse=True)
    return ranked[0].symbol if ranked else memory.get_last_anchor()
```

| Parameter | Nilai | Kategori |
|-----------|-------|----------|
| Anchor selection | Volume 24h rank #1 | A |
| Anchor refresh | Tiap universe scan (15 menit) | A |
| Fallback kalau data gagal | Anchor terakhir yang tersimpan | D |

Anchor dipakai di: Black Swan auto-resume (§VII.4), Correlation modifier (§VIII.3), Correlation breakdown indikator #3.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Framework 4 kategori | Semua bagian | Parameter muncul di tiap layer |
| Universe dinamis + market anchor | Bagian VII §VII.4, Bagian VIII §VIII.3, Bagian XIII env | Hapus hardcode simbol |
| Whitelist tuning | Bagian XII (Telegram) | /settings pakai whitelist ini |
| Locked parameter | — | Nggak bisa diubah dari UI |

---


# BAGIAN III — DISCOVERY MECHANISM (03_DISCOVERY.md)

Status: FINAL

## III.1 Framework Inti

**Ide = Anomali dari baseline.**

Bot nggak berpikir kreatif. Bot scan anomali sistematis. Anomali = sinyal awal setup.

## III.2 Enam Jenis Anomali

| # | Anomali | Baseline | Setup yang Muncul |
|---|---------|----------|-------------------|
| 1 | Funding extreme | ±0.01%/8h | Funding Squeeze |
| 2 | Liquidation cascade | <$1M/5m | Cascade Scalp |
| 3 | Volatility compression | ATR normal | Compression Breakout |
| 4 | Struktur berubah | Ranging normal | BOS/CHoCH |
| 5 | Volume spike | Normal | Confirm only |
| 6 | Cross-exchange divergence | Sync | Confirm only |

## III.3 Lima Langkah Discovery

### Step 1 — Universe Scan
Fetch ~200 symbol dari HL meta endpoint (REST, zero-auth). Output: universe lengkap.

### Step 2 — Filter

| Filter | Threshold |
|--------|-----------|
| Volume 24h | Major $20M, Mid $5M, Low $2M |
| Spread | Major 0.03%, Mid 0.05%, Low 0.10% |
| ATR | 0.5% |
| Listing age | 7 hari |

Output: 30-40 candidate.

### Step 3 — Anomaly Detection
Cek 6 jenis anomali untuk tiap candidate. Output: 3-10 symbol dengan anomali aktif.

### Step 4 — Ranking

```
score = (trigger_strength × 0.3)
      + (confirmations × 0.25)
      + (symbol_trust × 0.2)
      + (rr_potential × 0.15)
      + (horizon_score × 0.1)
```

Output: top 5.

### Step 5 — Confirm + Timing
Top 5 masuk L3-L6 pipeline. Output: signal final.

## III.4 LLM Tagging — Bot Tau Symbol Apa

Masalah: HL meta cuma kasih nama symbol. Nggak ada deskripsi.

Solusi 3 tier:

| Tier | Source | Coverage |
|------|--------|----------|
| Primary | Gemini knowledge | 90% top symbol |
| Fallback | CoinGecko API (`/coins/list` + `/coins/{id}`, public tier, zero-auth) | 17k+ coin |
| Last resort | Manual mapping | Top 50 |

Prompt Gemini lengkap ada di Bagian XI (§XI.4). Frekuensi: tiap 4 jam. Beban: 6 call/hari. Cost: $0.

**Catatan Aturan "14 source" (§V.2):** CoinGecko di sini adalah fallback tier-2 untuk tagging saja (dipakai kalau Gemini gagal identifikasi symbol), bukan source data harga/OHLCV/OI yang jadi bagian pipeline utama L0-L6. Karena itu sengaja tidak masuk tabel 14 source di §V.2 (yang khusus data pipeline), sama seperti Gemini sendiri yang di §V.2 didaftar untuk perannya di Layer 0/5, bukan untuk tagging fallback ini. Kalau CoinGecko dipakai untuk sesuatu di luar tagging fallback, source itu wajib ditambahkan resmi ke §V.2.

## III.5 Symbol Trust Score

```
trust_score = win_rate × 0.7 + min(avg_rr / 2.5, 1.0) × 0.3
```

`win_rate` di sini = win rate berbobot dari Bagian XVI §XVI.3: `(PROFIT + 0.5×PARTIAL) / (PROFIT + PARTIAL + LOSS)`. NEUTRAL tidak masuk perhitungan.

| Trust Score | Tier | Scan Frekuensi |
|-------------|------|----------------|
| ≥ 0.7 | 1 | 5 menit |
| 0.4 - 0.7 | 2 | 15 menit |
| < 0.4 | 3 | 60 menit |
| Sample < 10 | provisional | 15 menit |

Rotasi tier: tiap 24 jam.

## III.6 Yang Bot BISA vs NGGAK BISA

| ✅ Bisa (Statistical) | ❌ Nggak Bisa (Fundamental) |
|----------------------|------------------------------|
| Funding ekstrem | Prediksi "AI season" |
| Cascade besar | Predict "project X announce" |
| ATR compression | Predict "regulator ban" |
| BOS/CHoCH | Predict "whale accumulate" |
| Divergence cross-exchange | React ke narasi baru |
| Multi-TF alignment | Anticipate news |

Bot bisa confirm setelah narasi muncul. Nggak bisa anticipate.

## III.7 Warning: LLM Halusinasi

Gemini bisa salah tagging (WIF dibilang "AI coin", RNDR dibilang "low quality").

Mitigasi:
1. Spot check manual 5-10 output pertama
2. Cache hasil (nggak re-run tiap cycle)
3. Fallback rule-based kalau LLM down
4. Low weight di ranking (bukan 100%)

LLM = advisor, bukan boss.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Discovery mechanism | Bagian V (Data Sources) | Setiap anomali butuh source |
| LLM tagging | Bagian XI (LLM) | Detail prompt & rate limit |
| Trust score | Bagian VI (Memory) | Table symbol_performance |

---


# BAGIAN IV — TIGA SETUP TRADING (04_SETUPS.md)

Status: FINAL

## IV.1 Overview

| Setup | Horizon | Trigger Utama | Lifetime | Min R:R | Expectancy |
|-------|---------|---------------|----------|---------|------------|
| Funding Squeeze | Intraday | Funding P95 + Cascade P90 | 24 jam | 1:2 | 2-4x/mgg · ~60% |
| Cascade Scalp | Scalping | Cascade P90/5m | 2 jam | 1:1.5 | 5-10x/mgg · ~55% |
| Compression | Swing | ATR P20 + BB squeeze | 7 hari | 1:3 | 1-2x/mgg · ~50% |

## IV.2 Setup 1 — Funding Squeeze Reversal (Intraday)

**Logika:** Crowded positions di-flush lewat cascade. Setelah flush, harga mean reversion ke baseline.

**Trigger**
- Funding rate > P95 (absolute) per symbol
- Liquidation cascade > P90 berlawanan arah funding
- Cascade size > $5M dalam 5 menit

**Confirm**
- Microstructure: absorption di harga post-cascade
- Flow: whale netflow berlawanan arah crowded positions

**Timing:** Zona SMC (DBR/RBR untuk LONG, DBD/RBD untuk SHORT) post-cascade. Entry zone 0.3x-2.0x ATR.

| Parameter | Nilai |
|-----------|-------|
| Horizon | Intraday (1H/4H) |
| Lifetime | 24 jam |
| Min R:R | 1:2 |
| SL | 1.5x ATR |
| TP1 | 1.5x ATR (50%) |
| TP2 | 2.5x ATR (50%) |

Contoh:
```
🔴 BTC SHORT · INTRADAY · Tier A
Entry: $85,600 - $85,700
SL   : $86,215 (0.67%)
TP1  : $84,850
TP2  : $84,200
R:R  : 1:2.5
```

## IV.3 Setup 2 — Liquidation Cascade Scalp (Scalping)

**Logika:** Panic selling/buying → dead cat bounce. Tekanan ekstrem sementara.

**Trigger**
- Cascade > P90 per symbol dalam 5 menit
- Bukan funding-driven (beda dari Setup 1)

**Confirm**
- Microstructure: fast absorption (bid/ask imbalance spike)
- Cross-exchange: Binance dan HL cascade synchronized

**Timing:** Breakout minor level setelah cascade. Entry cepat, exit cepat.

| Parameter | Nilai |
|-----------|-------|
| Horizon | Scalping (5m/15m) |
| Lifetime | 2 jam |
| Min R:R | 1:1.5 |
| SL | 0.8x ATR |
| TP1 | 1.0x ATR (60%) |
| TP2 | 1.5x ATR (40%) |

**Kenapa R:R rendah:** Scalping nggak butuh R:R tinggi. Volume tinggi + win rate 55% = expectancy positif.

## IV.4 Setup 3 — Volatility Compression Breakout (Swing)

**Logika:** Konsolidasi lama → breakout besar. Energy terakumulasi.

**Trigger**
- ATR sekarang < P20 historis 30 hari
- Bollinger Band squeeze (width < P15)
- Breakout dari range konsolidasi (>2% move)

**Confirm**
- Volume breakout > P80
- BOS/CHoCH di H4/D1 searah breakout
- Cross-exchange sync

**Timing:** Retest breakout level. Entry di pullback pertama.

| Parameter | Nilai |
|-----------|-------|
| Horizon | Swing (4H/1D) |
| Lifetime | 7 hari |
| Min R:R | 1:3 |
| SL | 2.0x ATR |
| TP1 | 3.0x ATR (40%) |
| TP2 | 5.0x ATR (60%) |

## IV.5 Horizon Classifier

```python
def classify_horizon(trigger_type, tf_dominant, atr_pct, cascade_size):
    if trigger_type == "cascade" and cascade_size > percentile(cascade_history, 90):
        return "SCALPING"
    if trigger_type == "funding_extreme":
        return "INTRADAY"
    if trigger_type == "volatility_compression":
        return "SWING"
    return None
```

Satu symbol bisa punya multiple sinyal di horizon berbeda:
- BTC: Funding Squeeze (intraday) + Compression (swing)
- ETH: Cascade Scalp (scalping)
- SOL: Funding Squeeze (intraday)

## IV.6 Setup Performance Tracker

Per setup type, track:

| Metrik | Target |
|--------|--------|
| Total signals | — |
| Win rate | Scalping >55%, Intraday >60%, Swing >50% (rumus: Bagian XVI §XVI.3, PARTIAL bobot 0.5) |
| Avg R:R realized | ≥ min R:R |
| Avg hold hours | Track |

Setup yang jelek di-disable otomatis (bukan dihapus).

Threshold pause = 5 poin persen di bawah target setup:

| Setup | Target win rate | Pause kalau |
|-------|-----------------|-------------|
| Funding Squeeze | 60% | <55% setelah n=100 |
| Cascade Scalp | 55% | <50% setelah n=100 |
| Compression | 50% | <45% setelah n=100 |

**Mekanisme "disable" di runtime (gap sebelumnya: istilah tanpa definisi):**

- Flag `setup_enabled[setup_type]` (default `True`) disimpan di tabel `runtime_settings` (§VI.5, Kelompok D — Mutable State), bukan hardcode di kode.
- **L1 Trigger Detection dicek flag ini duluan**, sebelum agent trigger jalan untuk setup itu: kalau `setup_enabled[setup_type] == False`, L1 skip pengecekan trigger untuk setup type tersebut sepenuhnya (bukan "sinyal dibuat tapi tidak di-push", dan bukan "masuk tier Z"). Tidak ada compute yang dibuang untuk setup yang sudah di-pause.
- Auto-flip ke `False`: dievaluasi tiap kali `setup_performance` di-update (§XVI.6) — kalau `win_rate < pause_threshold` DAN `total_signals >= 100` untuk setup itu, set `setup_enabled[setup_type] = False` dan kirim notifikasi operator (Telegram, priority normal) berisi setup mana yang di-pause dan win rate terakhir.
- Auto-resume: **manual saja** — operator re-enable lewat command `/setup_enable <setup_type>` (Telegram). Tidak ada auto-resume otomatis supaya operator sadar dan bisa investigasi kenapa setup itu underperform sebelum dinyalakan lagi.
- `setup_enabled` dicek juga di `run_preflight` (§XIII) startup supaya state pause bertahan lewat restart cron 6 jam (fail-soft: kalau `runtime_settings` kosong/corrupt, default semua setup `enabled=True`).

## IV.7 Confluence Score

Kalau setup valid di 2+ timeframe, tier bisa naik — **tapi boost tidak pernah menggantikan syarat confirm L3** (lihat §I.2 Layer 6, "Aturan confluence boost vs syarat confirm"). Confluence menaikkan R:R-effective yang dipakai untuk tier check; jumlah confirm tetap harus terpenuhi sendiri.

```
📊 Confluence Check
━━━━━━━━━━━━━━━━━━━
TF 15m : SHORT signal ✓
TF 1H  : SHORT signal ✓
TF 4H  : SHORT signal ✓
TF 1D  : RANGING (neutral)
Confirm L3: 3/3 ✓

Confluence: 3/4 TF aligned
→ R:R confluence-adjusted ≥2.5, confirm 3/3 terpenuhi
→ Tier boosted: B → A
```

Kalau confirm cuma 2/3, contoh di atas maksimum jadi Tier B meski confluence 3/4 TF aligned — confluence TIDAK BISA mengangkat tier ke A tanpa 3/3 confirm.

Value: Setup valid multi-TF = lebih kuat. Auto-detect, auto-boost — dibatasi syarat confirm yang tetap berlaku.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 3 setup | Bagian II (lifetime & R:R per horizon) | Setup butuh lifetime/R:R berbeda |
| Horizon classifier | Bagian I (Layer 2) | Classifier di layer 2 |
| Setup performance | Bagian VI (table setup_performance) | Track di DB |
| Confluence | Bagian XII (fitur assistant #3) | Display di signal |

---


# BAGIAN V — DATA SOURCES (05_DATA_SOURCES.md)

Status: FINAL

## V.1 Prinsip

Setiap source punya 1 peran unik. Zero overlap. Kalau ada yang overlap, merge atau buang salah satu.

Zero fee: semua source gratis. Kalau butuh bayar, cari alternatif.

## V.2 Tabel Lengkap 14 Source

| # | Source | Peran | Layer | Auth | Cost |
|---|--------|-------|-------|------|------|
| 1 | Hyperliquid REST | OHLCV, funding, OI, meta | 0,1,4 | Zero | $0 |
| 2 | Hyperliquid WS | Orderbook, trades | 3 | Zero | $0 |
| 3 | Binance REST | Cross-check harga | 3 | Zero | $0 |
| 4 | Binance WS !forceOrder@arr | Liquidation feed | 1 | Zero | $0 |
| 5 | Bybit REST | Funding cross-check | 3 | Zero | $0 |
| 6 | Etherscan | Wallet watchlist | 3 | API key (free) | $0 |
| 7 | DefiLlama | Stablecoin flow | 5 | Zero | $0 |
| 8 | RSS (4 feed) | Headline | 5 | Zero | $0 |
| 9 | CryptoPanic | Aggregated news | 5 | Zero | $0 |
| 10 | FRED | US10Y, DXY | 5 | API key (free) | $0 |
| 11 | Alternative.me | Fear & Greed | 5 | Zero | $0 |
| 12 | Forex Factory | Event calendar | 6 | Zero | $0 |
| 13 | StockTwits | Retail chatter | 5 | Zero | $0 |
| 14 | Gemini | Sector tagging, news classifier | 0,5 | API key (free) | $0 |

## V.3 Detail per Source

### 1. Hyperliquid REST
- Base: `https://api.hyperliquid.xyz/info`
- Endpoints:
  - `{"type": "meta"}` → list ~200 symbol
  - `{"type": "metaAndAssetCtxs"}` → funding, OI, mark price semua
  - `{"type": "allMids"}` → harga mid semua
  - `{"type": "candleSnapshot", "req": {...}}` → OHLCV
- Dipakai: Layer 0, 1, 4
- Rate limit: ~100 req/menit aman

### 2. Hyperliquid WS
- URL: `wss://api.hyperliquid.xyz/ws`
- Subscribe: l2Book, trades
- Dipakai: Layer 3

### 3. Binance REST
- Base: `https://fapi.binance.com`
- Endpoints: `/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/price`, `/fapi/v1/klines`
- Dipakai: Layer 3 (cross-check)

### 4. Binance WS — Liquidation Feed
- URL: `wss://fstream.binance.com/ws/!forceOrder@arr`
- Fungsi: Push tiap liquidation real-time
- Dipakai: Layer 1 (trigger cascade)
- Kenapa Binance: Likuiditas retail tertinggi

### 5. Bybit REST
- Base: `https://api.bybit.com`
- Endpoints: `/v5/market/instruments-info`, `/v5/market/tickers`
- Dipakai: Layer 3

### 6. Etherscan
- Base: `https://api.etherscan.io/api`
- Endpoint: `?module=account&action=txlist&address=...`
- Setup: Daftar di etherscan.io, get key di /myapikey
- Env: `ETHERSCAN_API_KEY`
- Dipakai: Layer 3 (flow)

### 7. DefiLlama
- Base: `https://stablecoins.llama.fi`
- Endpoint: `/stablecoincharts/all`
- Dipakai: Layer 5

### 8. RSS Feeds (4)
- CoinDesk: `https://www.coindesk.com/arc/outboundfeeds/rss/`
- CoinTelegraph: `https://cointelegraph.com/rss`
- The Block: `https://www.theblock.co/rss.xml`
- Decrypt: `https://decrypt.co/feed`
- Dipakai: Layer 5

### 9. CryptoPanic
- Base: `https://cryptopanic.com/api/v1`
- Auth: API key free
- Dipakai: Layer 5

### 10. FRED
- Base: `https://api.stlouisfed.org/fred`
- Series: DTWEXBGS (DXY), DGS10 (US10Y)
- Setup: fred.stlouisfed.org
- Env: `FRED_API_KEY`
- Dipakai: Layer 5

### 11. Alternative.me
- Base: `https://api.alternative.me/fng/`
- Fungsi: Fear & Greed Index
- Dipakai: Layer 5

### 12. Forex Factory
- Fungsi: Event calendar (FOMC, CPI, NFP)
- Setup manual via env:
  - `CRYPTONE_MACRO_EVENTS` (JSON)
  - `CRYPTONE_MINUTES_TO_NEXT_MACRO` (int override)
- Dipakai: Layer 6 (veto)

### 13. StockTwits
- Base: `https://api.stocktwits.com/api/2/streams/symbol/{SYMBOL}.X.json`
- Zero-auth, tapi nggak dijamin untuk otomasi skala besar
- Dipakai: Layer 5

### 14. Gemini
- Base: `https://generativelanguage.googleapis.com/v1beta`
- Model: gemini-2.5-flash
- Fungsi: Sector tagging, narrative detection, news classifier
- Setup: aistudio.google.com/apikey
- Rate limit: 15 req/menit, 1500 req/hari
- Dipakai: Layer 0, 5

## V.4 WS vs REST Decision

| Data | Source | Alasan |
|------|--------|--------|
| OHLCV history | REST | WS nggak ngasih history |
| Funding rate | REST | Update 8 jam |
| OI | REST | Update 1 menit |
| Universe list | REST | 1 call = 200 symbol |
| Orderbook snapshot | REST | Analisis |
| Liquidation feed | WS | Event real-time |
| Price trigger watch | WS | Alert presisi |
| Cross-exchange diff | REST | Polling paralel |

Rule: **WS untuk event-based, REST untuk state-based.**

## V.5 Pre-Filter Before LLM (WAJIB)

50 headline → 8 ke LLM. Rate limit aman.

Definisi kanonik `pre_filter_before_llm()` (Aturan #1) ada di **§XI.7**, bukan di sini — dulu dua bagian ini punya body identik-hampir-identik yang drift (keyword `"delisting"` cuma ada di satu tempat). Sekarang satu sumber kebenaran saja.

## V.6 Fail-Soft Per Source

Kalau source mati:
- Nggak hentikan sistem
- Log ke `data_source_health` table
- Kalau >2 critical source mati bersamaan → alert operator

Critical source: `hyperliquid_rest`, `binance_liq_ws`, `etherscan`.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 14 source | Bagian VI (data_source_health) | Track health |
| Pre-filter LLM | Bagian XI (LLM) | Rate limit mitigation |
| WS vs REST | Bagian I (layer I/O) | Layer butuh source |

---


# BAGIAN VI — MEMORY ENGINE / SQLITE SCHEMA (06_MEMORY.md)

Status: FINAL

## VI.1 Prinsip

1. Pisah tabel peristiwa (append-only) dari status (mutable)
2. Simpan nilai mentah, bukan cuma kesimpulan
3. Satu sumber kebenaran per konsep
4. Retensi eksplisit per tabel

**Zona waktu (gap sebelumnya: tidak dispesifikasi, resiko mixing dengan `now_wib()`):** **Semua kolom `TIMESTAMP` di seluruh schema (setiap tabel di §VI.2-§VI.6) disimpan dalam UTC**, ditulis lewat `utcnow()`/`now_utc()` (§II, whitelist stdlib). `now_wib()`/`to_wib_iso()` HANYA dipakai untuk **tampilan** (log console, pesan Telegram ke user, §XII) — tidak pernah untuk nilai yang ditulis ke DB. Alasan: ordering (`ORDER BY recorded_at`), perbandingan antar-tabel, dan expiry check (§XVI.5) semua bergantung pada satu zona waktu yang konsisten; campuran UTC/WIB di kolom yang sama akan merusak urutan dan bikin expiry check salah baca durasi.

## VI.2 Kelompok A — Time Series (Append-Only)

```sql
-- OHLCV
CREATE TABLE IF NOT EXISTS metric_ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    bar_time TIMESTAMP NOT NULL,
    is_simulated BOOLEAN DEFAULT 0,
    schema_version INTEGER DEFAULT 1,
    UNIQUE(symbol, timeframe, bar_time)
);
CREATE INDEX idx_ohlcv_symbol_tf_time
    ON metric_ohlcv(symbol, timeframe, bar_time);

-- Funding rate
CREATE TABLE IF NOT EXISTS metric_funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    rate REAL NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    is_simulated BOOLEAN DEFAULT 0,
    schema_version INTEGER DEFAULT 1
);
CREATE INDEX idx_funding_symbol_time
    ON metric_funding(symbol, recorded_at);

-- Open Interest
CREATE TABLE IF NOT EXISTS metric_oi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    value REAL NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    is_simulated BOOLEAN DEFAULT 0,
    schema_version INTEGER DEFAULT 1
);
CREATE INDEX idx_oi_symbol_time
    ON metric_oi(symbol, recorded_at);

-- ATR
CREATE TABLE IF NOT EXISTS metric_atr (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    value REAL NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    is_simulated BOOLEAN DEFAULT 0,
    schema_version INTEGER DEFAULT 1
);
CREATE INDEX idx_atr_symbol_tf_time
    ON metric_atr(symbol, timeframe, recorded_at);

-- Volume baseline per jam UTC
CREATE TABLE IF NOT EXISTS volume_baseline_hourly (
    symbol TEXT NOT NULL,
    hour_utc INTEGER NOT NULL,
    median_volume REAL,
    p80_volume REAL,
    p20_volume REAL,
    sample_count INTEGER,
    last_updated TIMESTAMP,
    schema_version INTEGER DEFAULT 1,
    PRIMARY KEY (symbol, hour_utc)
);

-- Correlation matrix
CREATE TABLE IF NOT EXISTS correlation_matrix (
    symbol_a TEXT NOT NULL,
    symbol_b TEXT NOT NULL,
    correlation REAL NOT NULL,
    lookback_hours INTEGER,
    updated_at TIMESTAMP NOT NULL,
    schema_version INTEGER DEFAULT 1,
    PRIMARY KEY (symbol_a, symbol_b, lookback_hours)
);

-- News spike volume per bucket 5 menit (dipakai §VII.1 indikator #7 news spike)
CREATE TABLE IF NOT EXISTS news_volume_5m (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_start TIMESTAMP NOT NULL,
    headline_count INTEGER NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    schema_version INTEGER DEFAULT 1
);
CREATE INDEX idx_news_volume_bucket
    ON news_volume_5m(bucket_start);
```

## VI.3 Kelompok B — Event Log (Append-Only)

```sql
-- Signal history
-- Kolom lifecycle (setup_type, tp1_hit, tp1_hit_at, last_checked_at, realized_rr)
-- sudah include di CREATE langsung (konsisten migration strategy §VI.9: tabel baru
-- start dari schema_version terkini, bukan nunggu di-ALTER). Riwayat: sebelumnya
-- kolom-kolom ini ditambah lewat ALTER di §XVI.8 -- untuk instalasi existing yang
-- sudah punya tabel versi lama, ALTER di §XVI.8 tetap jadi migration path-nya.
CREATE TABLE IF NOT EXISTS signal_history (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    horizon TEXT NOT NULL,
    tier TEXT NOT NULL,
    entry_zone_low REAL,
    entry_zone_high REAL,
    stop_loss REAL,
    tp1 REAL,
    tp2 REAL,
    rr REAL,
    confidence REAL,
    trigger_type TEXT,
    reasoning TEXT,
    setup_type TEXT,
    tp1_hit BOOLEAN DEFAULT 0,
    tp1_hit_at TIMESTAMP,
    last_checked_at TIMESTAMP,
    realized_rr REAL,
    created_at TIMESTAMP NOT NULL,
    valid_until TIMESTAMP NOT NULL,
    outcome TEXT,
    resolved_at TIMESTAMP,
    is_simulated BOOLEAN DEFAULT 0,
    schema_version INTEGER DEFAULT 2
);
CREATE INDEX idx_signal_symbol_time
    ON signal_history(symbol, created_at DESC);
CREATE INDEX idx_signal_tier_time
    ON signal_history(tier, created_at DESC);

-- Liquidation events
CREATE TABLE IF NOT EXISTS liquidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    exchange TEXT NOT NULL,
    recorded_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_liq_symbol_time
    ON liquidation_log(symbol, recorded_at DESC);

-- Black swan events
CREATE TABLE IF NOT EXISTS blackswan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_indicators TEXT NOT NULL,
    trigger_count INTEGER,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    was_resumed BOOLEAN
);

-- Settings history (audit trail)
CREATE TABLE IF NOT EXISTS settings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT,
    changed_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_settings_key_time
    ON settings_history(key, changed_at DESC);

-- Data source health
CREATE TABLE IF NOT EXISTS data_source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    success BOOLEAN NOT NULL,
    latency_ms REAL,
    error_type TEXT,
    recorded_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_source_health_name_time
    ON data_source_health(source_name, recorded_at DESC);
```

## VI.4 Kelompok C — Master Data (Mutable)

```sql
-- Symbol metadata (dari LLM tagging)
CREATE TABLE IF NOT EXISTS symbol_metadata (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    sector TEXT,
    narrative TEXT,
    quality_flag TEXT,
    news_relevance REAL,
    updated_at TIMESTAMP
);

-- Symbol performance tracker
CREATE TABLE IF NOT EXISTS symbol_performance (
    symbol TEXT PRIMARY KEY,
    total_signals INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    partials INTEGER DEFAULT 0,
    neutrals INTEGER DEFAULT 0,
    avg_rr REAL DEFAULT 0,
    trust_score REAL DEFAULT 0.5,
    last_updated TIMESTAMP
);

-- Setup performance tracker
-- CATATAN: kolom `partials` ditambahkan di Bagian XVI §XVI.8 untuk instalasi lama.
-- TIDAK ADA kolom `neutrals` di sini (beda dengan symbol_performance) --
-- lihat §XVI.6: NEUTRAL sengaja tidak diupdate di setup_performance
-- karena bukan trade yang terbukti menang/kalah per-setup.
CREATE TABLE IF NOT EXISTS setup_performance (
    setup_type TEXT PRIMARY KEY,
    total_signals INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    avg_rr REAL,
    avg_hold_hours REAL,
    last_updated TIMESTAMP
);
```

## VI.5 Kelompok D — Mutable State

```sql
-- Active signals (expiry check)
CREATE TABLE IF NOT EXISTS active_signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_active_signal_expires
    ON active_signals(expires_at);

-- Runtime settings (override default)
CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

-- Wallet watch list
CREATE TABLE IF NOT EXISTS wallet_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    label TEXT,
    type TEXT DEFAULT 'user',
    added_by TEXT,
    added_at TIMESTAMP NOT NULL,
    UNIQUE(address)
);

-- Wallet transactions
CREATE TABLE IF NOT EXISTS wallet_txlog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    to_label TEXT,
    tx_hash TEXT,
    recorded_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_wallet_tx_address_time
    ON wallet_txlog(address, recorded_at DESC);

-- Pesan yang gagal dikirim & menunggu retry (skenario #5, §XVIII.2 — token
-- Telegram dicabut/expired). Dipilih tabel DB, bukan file log, karena file
-- log GH Actions hilang saat run selesai (§XVIII.2).
CREATE TABLE IF NOT EXISTS pending_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,        -- serialized TelegramMessage
    attempts INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL,
    schema_version INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_pending_delivery_time
    ON pending_delivery(created_at);

-- Bootstrap marker (dipakai §XIII.3 deteksi "belum pernah bootstrap")
-- Single row, id selalu 1. Terpisah dari PRAGMA user_version (§VI.9,
-- dipakai khusus schema migration) -- jangan overload satu mekanisme
-- untuk dua tujuan berbeda.
CREATE TABLE IF NOT EXISTS bootstrap_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bootstrapped_at TIMESTAMP,
    bootstrap_days INTEGER
);
```

## VI.6 Kelompok E — LLM

```sql
-- Cache hasil LLM
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    function_name TEXT,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_llm_cache_expires
    ON llm_cache(expires_at);

-- LLM call log
CREATE TABLE IF NOT EXISTS llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    function_name TEXT NOT NULL,
    latency_ms REAL,
    success BOOLEAN,
    error_type TEXT,
    recorded_at TIMESTAMP NOT NULL
);
```

## VI.7 Retention Policy

| Table | Retention |
|-------|-----------|
| metric_ohlcv | 7 hari raw, 90 hari hourly |
| metric_funding | 90 hari |
| metric_oi | 90 hari |
| metric_atr | 90 hari |
| signal_history | 90 hari |
| liquidation_log | 30 hari |
| blackswan_log | Permanen |
| settings_history | Permanen |
| wallet_txlog | 30 hari |
| data_source_health | 7 hari |
| correlation_matrix | Refresh tiap 1 jam |
| llm_cache | Auto-purge by expires_at |
| pending_delivery | 7 hari (auto-purge setelah terkirim / kedaluwarsa) |
| news_volume_5m | 30 hari |
| bootstrap_meta | Permanen (single row) |

## VI.8 Single-Writer Pattern

Semua write lewat queue tunggal. Concurrent write dari banyak agent → batch di akhir cycle.

```python
class MemoryEngine:
    def __init__(self, db_path):
        self._write_queue = asyncio.Queue()
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    async def enqueue_write(self, table, params):
        await self._write_queue.put((table, params))

    async def flush_writes(self):
        # Batch commit
        ...
```

## VI.9 Migration Strategy

Tiap tabel punya kolom `schema_version`. Kalau schema berubah:
1. Bump version
2. Tulis migration script
3. Auto-apply saat startup

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 5 kelompok tabel | Semua bagian yang baca/tulis DB | Setiap layer butuh table spesifik |
| Retention | Bagian XIII (retention job) | Job butuh jadwal |
| Single-writer | Bagian I (concurrency) | Konsistensi |

---


# BAGIAN VII & VIII — BLACK SWAN DETECTOR + CORRELATION ENGINE (07_BLACKSWAN.md)

Status: FINAL

## VII.1 Black Swan — Tujuh Indikator

| # | Indikator | Threshold | Cara Deteksi |
|---|-----------|-----------|--------------|
| 1 | Volatility spike | ATR > P99 | Percentile compare |
| 2 | Volume cascade | Volume 5m > P99 | Percentile compare |
| 3 | Multi-asset correlation break | **Market anchor itu sendiri** DAN top-2 symbol paling berkorelasi (dari `correlation_matrix`, §VIII) drop >3% serentak | Threshold 3% fixed (Kategori A), daftar simbol dinamis via `get_market_anchor()` (§II.8) — bukan hardcode (Aturan #7). Anchor sendiri dicek dulu (fix: sebelumnya cuma top-2 correlated symbol yang dicek, anchor crash langsung bisa terlewat) |
| 4 | Funding flip | Sign flip >0.1% | Compare vs 1h ago |
| 5 | Liquidation mega-cascade | $50M / 5 menit | Sum dari WS Binance |
| 6 | Cross-exchange divergence | HL vs Binance >1% | Price compare |
| 7 | News volume spike | Headline count / 5 menit ≥ 5x baseline | Rolling count dari 4 RSS feed (§V.3 #8), lihat spesifikasi lengkap di bawah |

**Indikator #3 detail:** kondisi trigger = `anchor_drop_pct > 3%` **ATAU** (`top2_correlated_avg_drop_pct > 3%` DAN serentak dengan pergerakan anchor searah). Anchor drop dicek independen supaya crash pada anchor sendiri tidak butuh symbol lain untuk ikut jatuh dulu.

**Indikator #7 — data path (gap sebelumnya: tidak ada spec):**
- Tabel `news_volume_5m` (tambahan di §VI.2, Kelompok A — Time Series): kolom `bucket_start TIMESTAMP, headline_count INTEGER, recorded_at TIMESTAMP`. Satu row per bucket 5 menit.
- Job polling: RSS (4 feed, §V.3 #8) di-poll tiap 5 menit (sinkron dengan L1 trigger tier tercepat), bukan tiap kali cycle jalan.
- Yang dihitung: **headline baru saja** dalam window 5 menit terakhir (dedup by URL/guid terhadap window sebelumnya) — bukan total kumulatif termasuk headline lama yang masih nampang di feed.
- Baseline "5x volume": median `headline_count` dari 12 bucket terakhir (1 jam rolling, Kategori C semi-dynamic, §II.4), default cold-start (Kategori D) = 3 headline/5menit sampai ≥12 bucket terkumpul.
- Trigger: `headline_count(bucket sekarang) >= 5 * median(12 bucket sebelumnya)`.
- Tanpa job polling + tabel ini, indikator #7 tidak punya data untuk dievaluasi dan akan selalu diam (fail-soft, tidak pernah nge-trigger) — implementasi job ini wajib, bukan opsional.

## VII.2 Trigger

**3 dari 7 indikator aktif bersamaan → BLACK SWAN MODE.**

Kenapa 3?
- 1 indikator = noise
- 2 indikator = warning
- 3 indikator = confirmed shock

Bisa dikalibrasi (locked di Bagian II).

## VII.3 Aksi Saat Trigger

```python
async def enter_black_swan_mode(memory, config, queue):
    # 1. Freeze sinyal baru (V4.5 tidak punya status FORMING terpisah
    #    seperti "thesis" di V4 -- gate ini menahan pembentukan sinyal
    #    baru selama black_swan_mode aktif)
    memory.set_flag("black_swan_mode", True)

    # 2. Push alert critical ke operator
    await queue.enqueue(TelegramMessage(
        chat_id=config.operator_chat_id,
        text="🚨 BLACK SWAN MODE ...",
        priority="critical",
    ))

    # 3. Extend lifetime semua sinyal ACTIVE (beri ruang napas sebelum
    #    dianggap invalid akibat volatilitas black swan, bukan sinyal asli)
    for sig in memory.get_active_signals():
        memory.extend_signal_validity(sig.signal_id, hours=6)

    # 4. Tidak ada langkah cancel terpisah di sini -- semua sinyal ACTIVE
    #    di V4.5 sudah masuk get_active_signals() di atas (V4.5 tidak
    #    punya status FORMING/CONFIRMED terpisah seperti thesis V4).
    #    Sinyal yang memang harus gugur karena regime flip tetap lewat
    #    jalur normal check_signal_expiry() (§XVI.5) / resolve_signal()
    #    (§XVI.6) pada cycle berikutnya.

    # 5. Log event
    memory.insert_blackswan_log(
        triggered_indicators=json.dumps(active_indicators),
        trigger_count=len(active_indicators),
        started_at=now_wib(),
    )
```

## VII.4 Auto-Resume Condition

Setelah 2 jam minimum, cek:

| Kondisi | Aksi |
|---------|------|
| ATR turun < P90 AND volume normal | Exit black swan mode |
| ATR masih > P95 | Extend 2 jam lagi |
| ATR turun tapi volatile (P90-P95) | "Elevated mode" — Tier A only |

```python
async def check_resume_condition(memory, universe_stats):
    anchor = get_market_anchor(memory, universe_stats)   # dinamis, bukan hardcode (§II.8)
    atr = get_current_atr(anchor)
    # get_atr_history(symbol, lookback_days) -- parameter kedua adalah
    # JUMLAH HARI (bukan jam/bar); 30 = ATR harian selama 30 hari terakhir,
    # dipakai sebagai basis percentile P90/P95. Konsisten dengan method
    # kontrak MemoryEngine.get_atr_history(symbol, lookback_days) di §VI.
    atr_p95 = percentile(get_atr_history(anchor, lookback_days=30), 95)
    atr_p90 = percentile(get_atr_history(anchor, lookback_days=30), 90)

    if atr < atr_p90:
        return "RESUME"        # exit black swan
    elif atr > atr_p95:
        return "EXTEND"        # masih ekstrem
    else:
        return "ELEVATED"      # tier A only
```

## VII.5 Alert Format

### Saat Trigger
```
🚨 BLACK SWAN MODE — 11:23 WIB
━━━━━━━━━━━━━━━━━━━
Active indicators (3/7):
  ✓ ATR spike P99 (4x baseline)
  ✓ Volume cascade P99
  ✓ Liquidation $47M/5m

Action:
  • New signals SUSPENDED
  • Sinyal ACTIVE diperpanjang +6h

Duration: min 2 hours
Monitor: /blackswan
```

### Saat Update
```
🚨 UPDATE — 11:35 WIB
BTC -5.8% dari open
Cascade total $89M
Cross-exchange spread normal

Status: masih black swan mode
```

### Saat Recovery
```
✅ RECOVERY — 13:40 WIB
ATR turun ke P85 (normal)
Volume kembali normal
BTC stabil di $81,200

→ EXIT BLACK SWAN MODE
Resume normal analysis
```

### Post-Shock Analysis
```
📊 POST-SHOCK — 13:45 WIB
Setup baru detected: Oversold Bounce
BTC/ETH/SOL di P95 oversold
Analyst detect strong demand zone

→ Tier A signal incoming...
```

## VII.6 Schema

```sql
CREATE TABLE IF NOT EXISTS blackswan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_indicators TEXT NOT NULL,
    trigger_count INTEGER,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    was_resumed BOOLEAN
);
CREATE INDEX idx_blackswan_time
    ON blackswan_log(started_at DESC);
```

> **Catatan contoh:** semua simbol di contoh output Bagian VIII (`/correlation`, `/news`, `/divergence` — BTC/ETH/SOL/XRP dst.) adalah **simulasi tampilan**, bukan daftar tetap. Di runtime, simbol yang muncul dipilih otomatis dari `correlation_matrix` dan `active_symbols` saat itu (lihat Bagian II §II.8, Aturan #7).

## VIII.1 Correlation Engine — Matrix

Update tiap 1 jam. Hitung korelasi return **168 jam (7 hari)** antar semua symbol dengan candle 1H (168 data point per symbol).

**Kenapa 168h, bukan 24h:** Pearson correlation dengan n=24 (1 hari candle 1H) terlalu noisy — sampel kecil menghasilkan koefisien korelasi yang tidak stabil antar-update dan rentan false positive (dua symbol kelihatan "berkorelasi tinggi" cuma karena kebetulan gerak bareng dalam 24 jam). n=168 (7 hari) memberi sampel yang cukup untuk korelasi yang lebih representatif tanpa kehilangan sensitivitas terhadap regime yang berubah relatif cepat (masih di bawah 30 hari).

```python
async def compute_correlation_matrix(symbols, lookback_hours=168):
    returns = {}
    for symbol in symbols:
        bars = get_ohlcv(symbol, "1H", lookback_hours)
        returns[symbol] = [
            (bars[i].close / bars[i-1].close) - 1
            for i in range(1, len(bars))
        ]

    matrix = {}
    for s1 in symbols:
        for s2 in symbols:
            if s1 == s2:
                matrix[(s1, s2)] = 1.0
            else:
                matrix[(s1, s2)] = statistics.correlation(
                    returns[s1], returns[s2]
                )

    return matrix
```

Simpan ke DB (lihat schema `correlation_matrix` di Bagian VI):

```sql
CREATE TABLE IF NOT EXISTS correlation_matrix (
    symbol_a TEXT NOT NULL,
    symbol_b TEXT NOT NULL,
    correlation REAL NOT NULL,
    lookback_hours INTEGER,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol_a, symbol_b, lookback_hours)
);
```

## VIII.2 News Impact Classifier

| Tier | Keyword | Impact Scope | Confidence Drop |
|------|---------|--------------|-----------------|
| MACRO | fed, fomc, recession, war, tariff | Semua crypto | Proporsional korelasi |
| SECTOR | sec, etf, regulation, hack | Sector-specific | -0.20 |
| PROJECT | listing, delisting, partnership | Symbol-specific | -0.30 |

```python
NEWS_IMPACT_KEYWORDS = {
    "MACRO": ["fed", "fomc", "recession", "war", "tariff", "interest rate"],
    "SECTOR": ["sec", "etf", "regulation", "crypto ban", "halving"],
    "PROJECT": ["hack", "exploit", "listing", "delisting", "partnership"],
}

def classify_news_impact(headline):
    headline_lower = headline.lower()
    for tier, keywords in NEWS_IMPACT_KEYWORDS.items():
        if any(kw in headline_lower for kw in keywords):
            return tier
    return "PROJECT"
```

## VIII.3 Symbol-Specific Context Modifier

```python
async def get_context_modifier(symbol, news_tier, correlation, anchor,
                                affected_symbols=None, affected_sector=None):
    multiplier = 1.0

    if news_tier == "MACRO":
        # anchor = market anchor dinamis (§II.8), bukan simbol hardcode
        anchor_corr = correlation.get((anchor, symbol), 0.5)
        multiplier -= 0.15 * anchor_corr

    elif news_tier == "SECTOR":
        if symbol in (affected_sector or []):
            multiplier -= 0.20

    elif news_tier == "PROJECT":
        if symbol in (affected_symbols or []):
            multiplier -= 0.30

    return clamp(multiplier, 0.5, 1.2)
```

Hasil: Confidence modifier per symbol, bukan market-wide.

## VIII.4 Command /correlation

```
📊 Correlation Matrix (24h)
━━━━━━━━━━━━━━━━━━━
         BTC    ETH    SOL    XRP
BTC      1.00   0.85   0.72   0.31
ETH      0.85   1.00   0.68   0.28
SOL      0.72   0.68   1.00   0.25
XRP      0.31   0.28   0.25   1.00
━━━━━━━━━━━━━━━━━━━
📌 BTC-ETH: 0.85 (sangat correlated)
📌 BTC-XRP: 0.31 (independen)
```

## VIII.5 Command /news

```
📰 Berita Terbaru (5 menit)
━━━━━━━━━━━━━━━━━━━
🔴 MACRO (impact: semua)
   "Fed naikkan rate 25bp"
   → Impact: -2% s/d -3% semua symbol

🟡 SECTOR (impact: L1)
   "ETH upgrade Cancun berhasil"
   → Impact: +1% untuk L1

⚪ PROJECT (impact: SOL)
   "Solana network downtime 30 menit"
   → Impact: -1% untuk SOL
```

## VIII.6 Command /divergence

```
📊 Divergence Monitor
━━━━━━━━━━━━━━━━━━━
BTC +5% · ETH +2% · SOL -1%
⚠️ BTC vs SOL divergence: -6%

Kemungkinan:
  • SOL independent weakness
  • BTC dominance rally
  • Uang pindah dari altcoin ke BTC

📌 Sinyal potensial: SHORT SOL
   (jika ada trigger di SOL)
```

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Black swan 7 indikator | Bagian II (locked: 3 dari 7) | Trigger count harus lock |
| Correlation matrix | Bagian VI (table correlation_matrix) | Update tiap 1 jam |
| News classifier | Bagian XI (Gemini prompt) | Klasifikasi via LLM |
| Symbol context | Bagian III (confidence modifier) | Per-symbol logic |

---


# BAGIAN IX — VOLUME & SESSION FRAMEWORK (08_VOLUME_SESSION.md)

Status: FINAL

## IX.1 Prinsip

Volume bukan filter tunggal. Ada 3 layer filter yang harus lolos semua:
1. Absolute floor (liquidity minimum)
2. Relative spike (ada gerakan)
3. OI change (leading indicator)

Plus: session-aware — baseline per jam UTC, bukan asumsi "US session = jam X".

## IX.2 Filter 1 — Absolute Floor (Liquidity)

| Cap | Volume 24h Min | Spread Max |
|-----|----------------|------------|
| Major (rank volume 1-10) | $20M | 0.03% |
| Mid-cap (rank 11-50) | $5M | 0.05% |
| Low-cap (rank >50) | $2M | 0.10% |

Mutlak. Di bawah ini, slippage parah, nggak worth.

```python
def classify_cap(symbol):
    rank = get_volume_rank(symbol)
    if rank <= 10:  return "MAJOR"
    if rank <= 50:  return "MID"
    return "LOW"
```

## IX.3 Filter 2 — Relative Spike

Bukan volume absolut, tapi relatif ke baseline symbol itu.

```python
def get_volume_spike(symbol, lookback_days=7):
    vol_1h = get_current_volume_1h(symbol)
    median_1h = get_median_volume_1h(symbol, lookback_days)

    if median_1h == 0:
        return None

    return vol_1h / median_1h
```

| Cap | Min Spike |
|-----|-----------|
| Major | 1.5x |
| Mid/Low | 2.5x |

Kenapa beda? Mid-low cap lebih noisy. Butuh spike lebih besar untuk dianggap "meaningful".

## IX.4 Filter 3 — OI Change (Leading Indicator)

Insight: Volume naik tapi OI nggak naik = fake pump (retail entry, no position). Volume naik + OI naik = posisi baru beneran.

```python
def get_oi_change(symbol, lookback_hours=1):
    oi_now = get_current_oi(symbol)
    oi_past = get_oi_at(symbol, hours_ago=lookback_hours)

    if oi_past == 0:
        return None

    return abs(oi_now - oi_past) / oi_past
```

Threshold: P80 per symbol historis.

```python
oi_threshold = percentile(
    get_historical_oi_deltas(symbol, lookback_days=30),
    80
)
# Syarat: oi_change > oi_threshold
```

## IX.5 Kombinasi: Early Entry Sebelum Pump

Kunci "masuk sebelum pump/dump":
1. Volume spike mulai (early signal)
2. OI change positif (posisi baru masuk)
3. Harga masih di zona akumulasi (belum breakout)

Kalau masuk di fase ini = early entry. Tapi risiko: fake signal (stop hunt trap).

## IX.6 Session-Aware Volume (Auto-Detect)

Bukan asumsi "jam 14:00 UTC = US session". Tapi baseline per jam UTC per symbol.

Schema kanonik: lihat Bagian VI §VI.2 (`volume_baseline_hourly`, termasuk kolom `p20_volume`).

Update tiap 14 hari.

## IX.7 Cara Hitung Baseline

```python
async def update_volume_baseline(symbol, lookback_days=14):
    bars = get_ohlcv(symbol, "1H", hours=lookback_days * 24)

    by_hour = {h: [] for h in range(24)}
    for bar in bars:
        hour_utc = bar.bar_time.astimezone(timezone.utc).hour
        by_hour[hour_utc].append(bar.volume)

    for hour, volumes in by_hour.items():
        if len(volumes) < 10:
            continue

        save_baseline(
            symbol=symbol,
            hour_utc=hour,
            median_volume=statistics.median(volumes),
            p80_volume=percentile(volumes, 80),
            p20_volume=percentile(volumes, 20),
            sample_count=len(volumes),
        )
```

## IX.8 Deteksi Session Spike

```python
def get_session_spike(symbol, current_volume):
    hour_now = now_utc().hour
    baseline = get_baseline(symbol, hour_now)

    if not baseline or baseline.sample_count < 20:
        return None

    ratio = current_volume / baseline.median_volume

    if ratio > 1.5:
        return {"session": "spike", "ratio": ratio}
    if ratio < 0.7:
        return {"session": "quiet", "ratio": ratio}
    return {"session": "normal", "ratio": ratio}
```

Boundary (Kategori C):
- Min spike: 1.3x
- Max spike: 3.0x (di atas ini = possible manipulation)

## IX.9 Integrasi ke Setup

| Setup | Volume Requirement |
|-------|--------------------|
| Funding Squeeze | OK quiet session |
| Cascade Scalp | spike atau normal |
| Compression Breakout | session_spike > 1.5 |

## IX.10 Stop Hunt Mitigation

Stop hunt = market maker sengaja gerak harga ke SL retail, terus balik.

```python
def is_potential_stop_hunt(symbol, memory, params):
    """
    Deteksi stop-hunt sebelum sinyal terbentuk.
    Semua threshold kategori C (boundary hardcoded, nilai default D di cold-start).
    """
    volume_z       = get_volume_z_score(symbol)
    price_change   = get_price_change_pct(symbol, hours=1)
    oi_z           = get_oi_z_score(symbol)
    funding        = get_funding_rate(symbol)
    hl_price       = get_hl_price(symbol)
    binance_price  = get_binance_price(symbol)

    # Check 1: Volume naik tapi harga stuck
    if volume_z > params.stop_hunt_volume_z and abs(price_change) < params.stop_hunt_price_change:
        return True

    # Check 2: OI spike tapi funding ekstrem (p95 per symbol, kategori B)
    if oi_z > params.stop_hunt_oi_z and abs(funding) > get_funding_p95(symbol):
        return True

    # Check 3: Cross-exchange divergence
    if abs(hl_price - binance_price) / binance_price > params.stop_hunt_xdiv:
        return True

    return False
```

| Parameter | Kategori | Default | Range | Alasan |
|-----------|----------|---------|-------|--------|
| `stop_hunt_volume_z` | C | 2.0 | 1.5 – 3.0 | Z-score vol untuk "volume naik tidak wajar" |
| `stop_hunt_price_change` | C | 0.5% | 0.3 – 1.0% | "harga stuck" relatif ke volatilitas |
| `stop_hunt_oi_z` | C | 2.0 | 1.5 – 3.0 | Z-score OI |
| `stop_hunt_xdiv` | C | 0.5% | 0.3 – 0.8% | Divergence HL vs Binance |

Kalau True → jangan bentuk sinyal. Tunggu konfirmasi tambahan.

## IX.11 Warning: False Positive

Session baseline bisa keliru kalau:
- Symbol baru listing (< 14 hari)
- Market regime shift (misal halving)
- Data corrupt

Mitigasi:
- Skip kalau sample_count < 20
- Re-compute tiap 14 hari
- Kalau ada 3 hari berturut-turut deviasi > 3x, alert operator

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 3 filter volume | Bagian IV (setup butuh volume) | Setup butuh filter |
| Session-aware | Bagian VI (volume_baseline_hourly) | Storage baseline |
| Stop hunt mitigation | Bagian I (Layer 4 timing) | Timing cek stop hunt |

---


# BAGIAN X — WALLET TRACKING, 3 LAYER (09_WALLET.md)

Status: FINAL

## X.1 Tiga Layer

| Layer | Scope | Frekuensi | Muncul di UI |
|-------|-------|-----------|--------------|
| Analytical | 20-30 exchange wallet | Tiap cycle | ❌ Internal |
| User-Defined | Max 5 wallet | User-defined | ✅ /wallet |
| Smart Money | Wallet profitable | Nanti | ✅ /wallet smart |

## X.2 Layer 1 — Analytical Watch

Fungsi: Dipakai Flow Agent buat deteksi netflow exchange.

Wallet yang dipantau **dibaca dari env `CRYPTONE_EXCHANGE_WALLETS`** (JSON atau file path, lihat §XIII.4). Daftar di bawah cuma contoh default — dikirim ke env saat setup, bukan ditulis hardcode di kode:

```json
{
  "binance_8":   "0xF977814e90dA44bFA03b6295A0616a897441aceC",
  "binance_14":  "0x28C6c06298d514Db089934071355E5743bf21d60",
  "binance_15":  "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549",
  "binance_16":  "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d",
  "coinbase":    "0x71660c4005BAe913Eae4147586F7FacbC99A7c5E",
  "coinbase_2":  "0x503828976D22510aad0201ac7EC88293211D23Da",
  "kraken":      "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2",
  "kraken_2":    "0x0A869d79a7052C7f1b55a8EbAbbEa3420F0D1E13",
  "okx":         "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
  "bybit":       "0x1Db92e2EeBC8E0c075a02BeA49a2935BcD2dFCF4",
  "bitfinex":    "0x1151314c646Ce4E0eFD76d1aF4760aE66a9Fe30F",
  "gemini":      "0xd24400ae8BfEBb18cA49Be86258a3C749cf46853"
}
```

Kalau env kosong: **Flow Agent (wallet-based) tidak jalan**, log warning ke `data_source_health`. Bukan fallback ke hardcoded list di kode — itu bertentangan langsung dengan Aturan #7. Konsekuensi: Layer 3 confirm berkurang 1 agent; sistem tetap jalan lewat fail-soft yang sudah ada (§V.6), bukan blocker.

Nggak ada UI. Background service.

Rate limit: 5 req/detik Etherscan (free tier). Cache TTL 5 menit — aman.

## X.3 Layer 2 — User-Defined Watch

Max 5 wallet. User commands:

**`/wallet add <address> <label>`**
```
/wallet add 0xabcdef... whale_1
✅ Wallet whale_1 ditambahkan
   Address: 0xabcdef...
   Type: user
```

**`/wallet list`**
```
💼 Wallet Tracker (3/5 slots)
━━━━━━━━━━━━━━━━━━━
1. whale_1   · 0xabcdef...  · $12M/24h
2. fund_A    · 0x123456...  · $340k/24h
3. dex_X     · 0x789abc...  · $5.2M/24h
━━━━━━━━━━━━━━━━━━━
[➕ Add] [❌ Remove] [🔔 Alert]
```

**`/wallet <label>`**
```
💰 whale_1 · 0xabcdef...
━━━━━━━━━━━━━━━━━━━
Incoming : $5.2M (3 tx, 24h)
Outgoing : $7.1M (2 tx, 24h)
Net      : -$1.9M (outflow → bullish)
Last tx  : 12 min ago → Binance
━━━━━━━━━━━━━━━━━━━
Recent tx:
  • 12m ago: -$2.3M → Binance
  • 3h ago:  +$4.1M ← unknown
  • 8h ago:  -$4.8M → Coinbase
━━━━━━━━━━━━━━━━━━━
[📊 Chart] [🔔 Alert] [❌ Remove]
```

**`/wallet tag <label> analytical`** — Masukkan wallet user ke analisis (jadi bagian dari Flow Agent).

**`/wallet remove <label>`** — Hapus dari watch list.

## X.4 Layer 3 — Smart Money (Nanti)

Fungsi: Wallet yang historically profitable.

Kenapa belum? Butuh analisis on-chain kompleks:
- Track P&L wallet (butuh full tx history)
- Tentukan win rate on-chain
- Klasifikasi strategi

Plan: Phase 2, setelah Phase 1 stabil.

## X.5 Integrasi ke Layer 3 (Confirm)

```python
def wallet_flow_confirm(symbol, direction):  # rename dari flow_confirm -- disambiguasi dari "flow" sebagai konsep umum (P6)
    netflow = get_wallet_netflow(symbol, lookback_hours=6)
    netflow_p80 = percentile(get_netflow_history(symbol, 30), 80)
    netflow_p20 = percentile(get_netflow_history(symbol, 30), 20)

    if direction == "SHORT" and netflow > netflow_p80:
        return True  # inflow tinggi = bearish confirm
    if direction == "LONG" and netflow < netflow_p20:
        return True  # outflow tinggi = bullish confirm

    return False
```

Wallet user-defined cuma masuk analisis kalau di-tag analytical.

## X.6 Schema

Lihat Bagian VI §VI.5 (`wallet_watch`, `wallet_txlog`) — definisi kanonik ada di sana.

```sql
CREATE TABLE IF NOT EXISTS wallet_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    label TEXT,
    type TEXT DEFAULT 'user',
    added_by TEXT,
    added_at TIMESTAMP NOT NULL,
    UNIQUE(address)
);

CREATE TABLE IF NOT EXISTS wallet_txlog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    to_label TEXT,
    tx_hash TEXT,
    recorded_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_wallet_tx_address_time
    ON wallet_txlog(address, recorded_at DESC);
```

## X.7 Known Limitation

Netflow dari Etherscan watchlist bersifat **market-wide (ETH), BUKAN per-symbol**.

- Untuk crypto majors (BTC/ETH/SOL), korelasi masih kuat
- Untuk altcoin kecil, netflow ETH nggak relevan
- Filter per-asset = butuh token transfer API + contract map → defer Phase 2

## X.8 Rate Limit Strategy

Etherscan free tier: 5 req/detik.
- 30 wallet × 1 req per cycle (5 min) = 30 req / 5 min = 0.1 req/detik
- Aman.
- Cache TTL: 5 menit

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 3 layer wallet | Bagian VI (wallet_watch, wallet_txlog) | Schema storage |
| Flow confirm | Bagian I (Layer 3) | Confirm agent |
| Max 5 wallet | Bagian II (locked) | Limit param |

---


# BAGIAN XI — LLM INTEGRATION (10_LLM.md)

Status: FINAL

## XI.1 Prinsip

**LLM = tagger & context provider. BUKAN decision maker.**

Semua keputusan arah, entry, SL, TP = matematika. LLM cuma:
- Tag sector symbol
- Deteksi narasi berita
- Klasifikasi dampak news

## XI.2 Provider

Gemini — model `gemini-2.5-flash`.

| Aspek | Nilai |
|-------|-------|
| Free tier req/menit | 15 |
| Free tier req/hari | 1500 |
| Latency rata-rata | 1-3 detik |
| Auth | API key (gratis) |
| Setup | aistudio.google.com/apikey |

Kenapa Gemini?
- Lo udah familiar
- Free tier cukup
- Multi-modal (kalau nanti butuh chart analysis)

## XI.3 Penggunaan

| Fungsi | Frekuensi | Beban |
|--------|-----------|-------|
| Sector tagging | Tiap 4 jam | 6 call/hari |
| Narrative detection | Tiap 1 jam | 24 call/hari |
| News classifier | On-demand (pre-filtered) | 20-50 call/hari |
| Counter-argument LLM (opsional) | Per signal penting | 5-10 call/hari |

Total: 55-90 call/hari. Jauh di bawah limit 1500.

## XI.4 Prompt Template — Sector Tagging

```
Lo crypto analyst. Untuk setiap symbol Hyperliquid berikut,
kasih kategorisasi berdasarkan NAMA SYMBOL + pengetahuan lo.

SYMBOL LIST:
{symbol_list}

TASK: Return JSON array dengan format:
{
  "symbol": "WIF",
  "sector": "MEME",
  "narrative": "Solana memecoin",
  "news_relevance": 0.0-1.0,
  "quality_flag": "high|medium|low|rug_risk",
  "confidence": 0.0-1.0,
  "reasoning": "1 kalimat"
}

Rules:
- sector: L1|L2|DEFI|MEME|AI|DEPIN|GAMING|RWA|INFRA|OTHER
- Kalau nggak yakin symbol ini apa, set sector="OTHER", confidence=0.3
- Jangan halusinasi. Kalau nggak tau, bilang nggak tau.
- Return HANYA JSON array, jangan ada teks lain.
```

Output parsing: pakai `safe_json_loads` yang handle fence, trailing comma, dll.

## XI.5 Prompt Template — Narrative Detection

```
Lo crypto analyst. Baca berita terkini dan identifikasi
narasi yang lagi dominan.

BERITA TERKINI (5-10 headline):
{headlines}

TASK: Return JSON:
{
  "top_narratives": [
    {"name": "AI/GPU tokens", "strength": 0.85, "reasoning": "..."},
    {"name": "DePIN", "strength": 0.72, "reasoning": "..."}
  ],
  "affected_sectors": ["AI", "DEPIN"],
  "summary": "1-2 kalimat ringkasan"
}

Rules:
- Maksimal 3 narasi
- Strength: 0.0-1.0
- Return HANYA JSON, jangan ada teks lain.
```

## XI.6 Prompt Template — News Classifier

```
Lo crypto analyst. Klasifikasi berita berikut.

HEADLINE: "{headline}"

TASK: Return JSON:
{
  "impact_tier": "MACRO|SECTOR|PROJECT",
  "affected_symbols": ["BTC", "ETH"],
  "affected_sectors": ["L1"],
  "severity": 0.0-1.0,
  "reasoning": "1 kalimat"
}

Rules:
- MACRO: dampak ke seluruh pasar (Fed, perang, tarif)
- SECTOR: dampak ke sektor tertentu (SEC, ETF, regulation)
- PROJECT: dampak ke symbol spesifik (hack, listing)
- Return HANYA JSON.
```

## XI.7 Pre-Filter (WAJIB)

Sebelum kirim ke Gemini, filter dulu:

```python
def pre_filter_before_llm(headlines):
    scored = []
    for h in headlines:
        score = 0
        h_lower = h.lower()

        if any(k in h_lower for k in ["fed", "fomc", "recession", "war", "tariff"]):
            score += 10
        if any(k in h_lower for k in ["sec", "etf", "ban", "regulation"]):
            score += 5
        if any(k in h_lower for k in ["hack", "exploit", "listing", "delisting"]):
            score += 3

        scored.append((h, score))

    # Top 8
    return [h for h, s in sorted(scored, key=lambda x: -x[1])[:8]]
```

50 headline → 8 ke LLM.

## XI.8 Caching Strategy

```python
CACHE_TTL = {
    "sector_tagging": 4 * 3600,      # 4 jam
    "narrative_detection": 3600,     # 1 jam
    "news_classifier": 3600,         # 1 jam
}
```

Cache hit rate ekspektasi: 40-60% (crypto news sering duplikat).
Key: `hash(prompt)`. Value: `(response, timestamp)`.

## XI.9 Fallback Kalau Gemini Down

| Priority | Fallback |
|----------|----------|
| 1 | Cache hasil terakhir (kalau <4 jam) |
| 2 | Rule-based keyword classifier |
| 3 | Sector "OTHER", narrative "unknown" |

```python
KEYWORD_SECTOR_MAP = {
    "AI": ["ai", "gpu", "render", "fetch"],
    "MEME": ["doge", "shib", "pepe", "wif"],
    "L1": ["solana", "ethereum", "avalanche"],
    "DEFI": ["uniswap", "aave", "curve", "maker"],
    "DEPIN": ["helium", "render", "filecoin"],
}
```

## XI.10 Rate Limit Handling

```python
async def call_gemini(prompt, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = await gemini_client.generate(prompt)
            return response
        except RateLimitError:
            wait = 2 ** attempt  # exponential backoff
            await asyncio.sleep(wait)
        except TimeoutError:
            await asyncio.sleep(2)

    raise LLMError("Gemini failed after retries")
```

`LLMError` didefinisikan di §XIX (Contract) sebagai custom exception, konsisten dengan `DataUnavailable`/`RateLimitError`/`TelegramRateLimitError` — bukan builtin `Exception` generik.

## XI.11 Cost Tracking

Walau free tier, tetap track:

```python
class LLMCostTracker:
    def __init__(self):
        self.daily_calls = 0
        self.monthly_calls = 0

    def record_call(self, function_name):
        self.daily_calls += 1
        if self.daily_calls > 1000:
            logger.warning(f"LLM calls approaching limit: {self.daily_calls}")
```

Warning threshold: 1000/hari (dari 1500 limit).

## XI.12 Schema

Definisi kanonik di Bagian VI §VI.6.

```sql
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key TEXT PRIMARY KEY,
    response TEXT NOT NULL,
    function_name TEXT,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_llm_cache_expires ON llm_cache(expires_at);

CREATE TABLE IF NOT EXISTS llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    function_name TEXT NOT NULL,
    latency_ms REAL,
    success BOOLEAN,
    error_type TEXT,
    recorded_at TIMESTAMP NOT NULL
);
CREATE INDEX idx_llm_call_time ON llm_call_log(recorded_at DESC);
```

## XI.13 Warning: Halusinasi Gemini

Gemini bisa:
- Salah tag WIF sebagai "AI coin" (padahal memecoin)
- Bilang RNDR "low quality" (padahal legit)
- Ngarang reasoning tanpa dasar

Mitigasi:
1. Spot check 5-10 output pertama secara manual
2. Pakai confidence field — kalau <0.5, jangan percaya
3. Cache hasil (nggak re-run tiap cycle)
4. Low weight di ranking (bukan 100%)
5. Fallback rule-based kalau LLM jelek

LLM = advisor, bukan boss.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Gemini provider | Bagian V (source #14) | Konsisten |
| Pre-filter | Bagian III (news pipeline) | Wajib pre-filter |
| Cache | Bagian VI (llm_cache, llm_call_log) | Storage |

---


# BAGIAN XII — TELEGRAM SUPER APP (11_TELEGRAM.md)

Status: FINAL

> **Catatan contoh:** semua simbol, harga, dan angka di contoh output Bagian XII (BTC/ETH/SOL, $85,644, dst.) adalah **simulasi tampilan**. Di runtime, simbol yang muncul di `/radar`, `/market`, sinyal, breadth, dan correlation dipilih otomatis dari `active_symbols` (top hasil ranking Layer 0-6 saat itu), dan berubah tiap cycle sesuai konteks market. `/market` menampilkan market anchor + top mover dinamis, bukan daftar tetap.

## XII.1 Menu Utama /start

```
🤖 Cryptone V4.5 — Market Radar
━━━━━━━━━━━━━━━━━━━

📡 Analisis
   [📊 Radar Sekarang]
   [🌍 Market Snapshot]
   [🚨 Black Swan Status]

🛠️ Tools
   [💰 Position Calculator]
   [🔔 Custom Alerts]
   [📓 Trade Journal]
   [💼 Wallet Tracking]

⚙️ Settings
   [🎯 Mode & Filter]
   [⏰ Quiet Hours]
   [🔧 Threshold Tuning]

📈 Stats
   [📊 Sinyal Performance]
   [🎯 Accuracy Tracker]
   [🏆 Best/Worst Setup]
```

## XII.2 Signal Format

```
🔴 BTC SHORT · INTRADAY · Tier A
━━━━━━━━━━━━━━━━━━━
Entry  : $85,600 - $85,700
Stop   : $86,215 (0.67%)
TP1    : $84,850 (50%)
TP2    : $84,200 (50%)
R:R    : 1:2.5
Valid  : 24 jam
⏱️ Trigger 47 detik lalu
━━━━━━━━━━━━━━━━━━━
📋 Funding Squeeze Reversal
⚡ Funding +0.18% (P95) + Cascade $7M
✅ Confirm 3/3
⚠️ Macro risk-off (-15% conf)
━━━━━━━━━━━━━━━━━━━
[📊 Chart] [🧠 Reasoning]
[💰 Calc] [❌ Skip]
```

Variasi tier:
- Tier A: full report
- Tier B: medium
- Tier C: minimal

## XII.3 Command List

### Analisis

| Command | Fungsi |
|---------|--------|
| /radar | Screener aktif, sort by tier |
| /market | Snapshot market global |
| /blackswan | Status black swan |
| /correlation | Correlation matrix |
| /news | Berita terbaru dengan impact |
| /breadth | Market breadth |
| /divergence | Divergence monitor |

### Tools

| Command | Fungsi |
|---------|--------|
| /calc SYM ENTRY SL TP [RISK] | Position calculator |
| /alert SYM PRICE | Custom alert |
| /journal | Trade journal |
| /stats | Personal stats |
| /wallet | Wallet tracking |
| /playbook | Preset alerts |

### Settings

| Command | Fungsi |
|---------|--------|
| /mode [active\|passive\|hybrid] | Ganti mode |
| /tier [A\|AB\|all] | Filter tier |
| /horizon [scalp\|intra\|swing\|all] | Filter horizon |
| /schedule quiet HH:MM HH:MM | Quiet hours |
| /settings | Threshold tuning |
| /settings rollback <ts> | Rollback setting |

### Stats

| Command | Fungsi |
|---------|--------|
| /performance | Setup performance |
| /accuracy | Precision/recall tracker |

## XII.4 Contoh Output

### /radar
```
📡 RADAR · 2026-09-24 11:45 WIB
━━━━━━━━━━━━━━━━━━━
🟢 BTC SHORT · Intraday · Tier A
   R:R 1:2.5 · 55% · 23h left

🟡 ETH LONG · Scalping · Tier B
   R:R 1:1.8 · 48% · 1h left

⚪ SOL SHORT · Swing · Tier C
   R:R 1:3.2 · 42% · 6d left
━━━━━━━━━━━━━━━━━━━
Aktif: 3 · Cooldown: 5 symbol
[Refresh] [Filter Tier A]
```

### /calc BTC 85644 86215 84200 100
```
📍 BTC SHORT
━━━━━━━━━━━━━━━━━━━
Entry    : $85,644
Stop     : $86,215 (+0.67%)
Target   : $84,200 (-1.68%)
R:R      : 1:2.5

💰 Position Size (risk $100)
   Size  : 0.172 BTC
   Value : $14,730
   Stop loss: -$100 ✓

💸 Fee Estimate (HL taker 0.035%)
   Entry: $5.16 · Exit: $5.06 · Total: $10.22
━━━━━━━━━━━━━━━━━━━
```

### /market
Menampilkan: market anchor (§II.8) + top N symbol berdasarkan ranking saat itu (bukan daftar tetap).
```
🌍 MARKET SNAPSHOT · 11:45 WIB
━━━━━━━━━━━━━━━━━━━
BTC  $85,644  · ATR $1,240 · RANGING
ETH  $3,120   · ATR $89    · TRENDING_DN
SOL  $142     · ATR $4.2   · RANGING

Funding (8h):
  BTC : +0.018% (P92)
  ETH : -0.005% (P40)
  SOL : +0.012% (P75)

Fear & Greed: 42 (Fear)
DXY: 105.2 (+0.3%)
US10Y: 4.32% (-0.05%)

Active signals: 3
Black swan: NO
━━━━━━━━━━━━━━━━━━━
```

## XII.5 Enam Fitur Assistant

### 1. Backtest On-Demand — tombol 🔬 Test Setup Ini
```
🔬 Backtest — Funding Squeeze (BTC)
━━━━━━━━━━━━━━━━━━━
Sinyal serupa 30 hari: 8
   ✓ Win  : 5 (62.5%)
   ✗ Loss : 2 (25%)
   ⚪ Flat: 1 (12.5%)

Avg R:R : 1:2.3 · Avg hold: 8.4 jam
Best    : 13 Sep (+3.2x ATR)
Worst   : 19 Sep (-1.0x ATR)
```

### 2. Playbook (Preset Alerts)
```
📋 Playbook
━━━━━━━━━━━━━━━━━━━
🎯 Entry Presets
   • BTC break $90k → alert
   • ETH funding >0.15% → alert
   • Cascade >$20M → alert

🛡️ Exit Presets
   • BTC -5% in 1h → alert
   • ATR spike P95 → alert
   • Black swan trigger → alert

📊 Watch Presets
   • BTC dominance >55% → alert
   • Stablecoin outflow >$500M → alert
   • RSS headline volume 5x → alert
```

### 3. Confluence Score (otomatis di signal)
```
📊 Confluence Check
━━━━━━━━━━━━━━━━━━━
TF 15m : SHORT signal ✓
TF 1H  : SHORT signal ✓
TF 4H  : SHORT signal ✓
TF 1D  : RANGING (neutral)

Confluence: 3/4 TF aligned
→ Tier boosted: B → A
```

### 4. Market Breadth
```
📊 Market Breadth — 11:45
━━━━━━━━━━━━━━━━━━━
Universe: 30 symbol
Bullish  : 8  ████░░░░░░ 27%
Neutral  : 15 ████████░░ 50%
Bearish  : 7  ███░░░░░░░ 23%

Dominance:
   BTC : 52.3% (↑ 0.8% 24h)
   ETH : 18.1% (↓ 0.2% 24h)
   Alt : 29.6% (↓ 0.6% 24h)
```

### 5. RR Visualizer
```
[CHART: garis putih (entry), merah (SL), hijau (TP1/TP2), zona kuning (current)]

━━━━━━━━━━━━━━━━━━━
Current : $85,820 (Entry -0.03%)
To Stop : $395 (0.46%)
To TP1  : $970 (1.13%)
To TP2  : $1,620 (1.89%)
━━━━━━━━━━━━━━━━━━━
Progress: [███░░░░░░░] 12% → TP1
```

### 6. Setup Performance
```
📊 Performance — 30 hari
━━━━━━━━━━━━━━━━━━━
🎯 Funding Squeeze
   Signals : 42 · Win rate: 62%
   Avg R:R : 1:2.3

⚡ Cascade Scalp
   Signals : 87 · Win rate: 54%
   Avg R:R : 1:1.7

📈 Compression
   Signals : 12 · Win rate: 50%
   Avg R:R : 1:3.1
```

## XII.6 Settings Manager

```python
class SettingsManager:
    TUNABLE = {
        "funding_extreme_pct": {"min": 90, "max": 99, "presets": [90,95,97,99]},
        "cascade_size_pct": {"min": 85, "max": 99, "presets": [85,90,95]},
        "min_rr_intraday": {"min": 1.5, "max": 3.0, "presets": [1.5,2,2.5,3]},
        "min_rr_swing": {"min": 2.0, "max": 4.0, "presets": [2,3,4]},
        "signal_cooldown_min": {"min": 15, "max": 60, "presets": [15,30,60]},
        "tier_a_count_max": {"min": 3, "max": 10, "presets": [3,5,10]},
        "tier_b_count_max": {"min": 10, "max": 30, "presets": [10,20,30]},
    }
```

Setiap perubahan di-log ke `settings_history`. Rollback via `/settings rollback <timestamp>`.

## XII.7 Mode Delivery

| Mode | Behavior |
|------|----------|
| Active | Semua tier push real-time |
| Passive | Silent, dashboard only |
| Hybrid | Tier A push, B/C pasif (default) |

## XII.8 Quiet Hours

```
/schedule quiet 23:00 07:00
✅ Quiet hours: 23:00 - 07:00
   Tier A tetap dikirim
   Tier B/C ditahan → dikirim jam 07:00
```

## XII.9 Delivery Queue

```python
class TelegramDeliveryQueue:
    def __init__(self):
        self._queue = asyncio.Queue()

    async def enqueue(self, message):
        await self._queue.put(message)

    async def run_sender_loop(self, bot_client, min_interval=1.1):
        while True:
            msg = await self._queue.get()
            try:
                await bot_client.send(msg)
            except TelegramRateLimitError as e:
                await asyncio.sleep(e.retry_after)
                await self._queue.put(msg)
            await asyncio.sleep(min_interval)
```

## XII.10 Chart Rendering

- Line chart — signal card, /status
- Candlestick — war room, signal detail (dengan TP1/TP2/SL + volume)
- Area chart — regime summary

Style: background dark (#0d1117), font Inter/Roboto Mono, watermark "Cryptone" (alpha 0.06).

Dependency: mplfinance, matplotlib, pandas.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Signal format | Bagian IV (R:R, lifetime) | Format match setup |
| Settings manager | Bagian II (whitelist) | Konsisten |
| 6 fitur assistant | Bagian XIV (prioritas) | Roadmap fitur |

---

# BAGIAN XIII — DEPLOY & OPERASIONAL (12_DEPLOY.md)

Status: FINAL

## XIII.1 GitHub Actions Infinite Pool

Konsep: job jalan 5.5 jam, exit, trigger berikutnya di cron.

`.github/workflows/live.yml`

```yaml
name: Cryptone Live
on:
  schedule:
    - cron: '0 */6 * * *'   # tiap 6 jam (job jalan 5.5 jam, buffer 30 menit)
  workflow_dispatch:

concurrency:
  group: cryptone-live
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 330

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install deps
        run: pip install -r requirements.txt

      - name: Download DB
        uses: dawidd6/action-download-artifact@v3
        continue-on-error: true
        with:
          name: cryptone-db
          path: ./data
          if_no_artifact_found: warn

      - name: Mkdir data
        run: mkdir -p ./data

      - name: Run bot 5.5 jam
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          ETHERSCAN_API_KEY: ${{ secrets.ETHERSCAN_API_KEY }}
          CRYPTONE_TELEGRAM_BOT_TOKEN: ${{ secrets.CRYPTONE_TELEGRAM_BOT_TOKEN }}
          CRYPTONE_OPERATOR_CHAT_ID: ${{ secrets.CRYPTONE_OPERATOR_CHAT_ID }}
          CRYPTONE_USER_CHAT_ID: ${{ secrets.CRYPTONE_USER_CHAT_ID }}
          CRYPTONE_PINNED_SYMBOLS: ""   # opsional; kosong = universe 100% dinamis (§II.8)
          CRYPTONE_DB_PATH: ./data/cryptone_v45.db
          CRYPTONE_CYCLE_INTERVAL: 300
        run: |
          # --max-runtime 19800 = 5.5 jam; exit 0 saat timeout terencana (Bagian XVII §XVII.7)
          python cryptone_v45.py --live --max-runtime 19800

      - name: Upload DB
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: cryptone-db
          path: ./data/cryptone_v45.db
          retention-days: 7
```

## XIII.2 Keep-Alive Commit

Cegah auto-pause 60 hari tanpa commit.

`.github/workflows/keepalive.yml`

```yaml
name: Keep-Alive
on:
  schedule:
    - cron: '0 0 1,15 * *'
  workflow_dispatch:

jobs:
  keepalive:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Keep-alive commit
        run: |
          git config user.email "bot@cryptone.local"
          git config user.name "Cryptone Bot"
          echo "$(date)" > .keepalive
          git add .keepalive
          git commit -m "keepalive $(date +%Y-%m-%d)" || true
          git push || true
```

## XIII.3 Backtest Bootstrap

Wajib sebelum live.

```bash
python cryptone_v45.py --bootstrap --days 60
```

Proses:
1. Fetch candle historis 60 hari dari HL
2. Replay setup detection
3. Simulasi outcome tiap sinyal
4. Isi metric_* tables
5. Kalibrasi reliability tracker

Durasi: ~30 menit sekali. Bukan recurring.

**Signature kontrak** (implementasi detail di `cryptone_v45.py`):

**Deteksi "belum pernah bootstrap":** tabel `bootstrap_meta` (Kelompok D — Mutable State, §VI.5), single row: `id INTEGER PRIMARY KEY CHECK (id = 1), bootstrapped_at TIMESTAMP, bootstrap_days INTEGER`. Startup sequence (§XIII.12 langkah 5) cek `SELECT bootstrapped_at FROM bootstrap_meta WHERE id=1` — kalau row tidak ada / NULL, jalankan `run_bootstrap()` lalu tulis row ini di akhir. Kalau row sudah ada, skip. Ini lebih eksplisit daripada `PRAGMA user_version` (yang dipakai untuk schema migration di §VI.9, bukan untuk marker bootstrap — jangan overload satu mekanisme untuk dua tujuan berbeda).

```python
async def run_bootstrap(days: int = 60) -> None:
    """Entry point --bootstrap. Langkah 1-5 di atas. Dipanggil dari §XVII.2/§XVIII.5.
    Menulis row bootstrap_meta(id=1, bootstrapped_at=now(), bootstrap_days=days)
    di akhir setelah semua langkah sukses -- ini yang jadi marker "sudah pernah"
    dicek startup sequence §XIII.12."""
    ...

async def run_preflight() -> bool:
    """Entry point --check. Validasi env + koneksi ke semua data source, lalu keluar. Return True kalau semua lolos. Dipanggil dari §XVII.2."""
    ...

async def run_live_service(dry_run: bool = False) -> None:
    """
    Entry point --live / --dry-run (§XVII.4).
    dry_run=True: jalan seperti live (semua engine, semua scan), TAPI
    push Telegram di-skip (log 'would send' saja) dan tidak menulis
    outcome ke signal_history -- state DB tidak berubah.
    Dipakai buat validasi end-to-end tanpa risiko spam/tulis data palsu.
    """
    ...

def restore_from_previous_artifact() -> bool:
    """Coba pulihkan state.db dari GitHub Actions artifact run sebelumnya. Return True kalau berhasil. Dipanggil dari §XVIII.5."""
    ...

def create_fresh_db() -> None:
    """Inisialisasi schema kosong (semua CREATE TABLE Bagian VI) saat restore gagal/tidak ada artifact. Dipanggil dari §XVIII.5."""
    ...

async def flush_and_exit(memory: MemoryEngine, queue: WriteQueue, timeout: int = 20) -> None:
    """Handler SIGTERM: flush write queue (checkpoint WAL) dalam batas `timeout` detik, lalu exit bersih. Dipanggil dari §XVIII.6."""
    ...

async def startup_db_check(db_path: str) -> bool:
    """Integrity check DB saat startup (PRAGMA integrity_check). Return True kalau sehat; False memicu restore_from_previous_artifact()/create_fresh_db() di jalur §XVIII.5. Dipanggil dari §XVIII.5."""
    ...
```

Semua fungsi di atas wajib ada dengan signature ini di `.py` — kalau hilang, itu blocker sebelum go-live (lihat script orphan-check di Lampiran).

## XIII.4 Environment Variables

### Wajib (mode `--live`)
```env
GEMINI_API_KEY=
CRYPTONE_TELEGRAM_BOT_TOKEN=
CRYPTONE_OPERATOR_CHAT_ID=
CRYPTONE_USER_CHAT_ID=
```

### Wajib (mode `--dry-run`, `--bootstrap`, `--test`)
```env
GEMINI_API_KEY=
```

`CRYPTONE_TELEGRAM_BOT_TOKEN`, `CRYPTONE_OPERATOR_CHAT_ID`, `CRYPTONE_USER_CHAT_ID` **TIDAK wajib** untuk `--dry-run`/`--bootstrap`/`--test` — mode-mode ini tidak pernah push Telegram (dry-run: "push di-skip, log 'would send' saja", §XIII.3), jadi validasi env di `run_preflight()` (§XIII) skip ketiga variabel ini kalau mode aktif bukan `--live`. Ini yang memungkinkan dry-run jalan lokal tanpa secrets Telegram. Kalau salah satu di atas memang di-set saat dry-run (mis. karena env global sama dengan live), boleh — cuma tidak divalidasi sebagai syarat wajib.

### Recommended
```env
ETHERSCAN_API_KEY=
CRYPTONE_EXCHANGE_WALLETS=
CRYPTONE_DB_PATH=./data/cryptone_v45.db
```

### Optional
```env
# FRED_API_KEY=
# CRYPTONE_MACRO_EVENTS=
# CRYPTONE_MINUTES_TO_NEXT_MACRO=
# CRYPTONE_LOG_LEVEL=INFO
# CRYPTONE_PINNED_SYMBOLS=        # opsional, koma-separated; tetap wajib lolos filter Layer 0
```

## XIII.5 Operasional Realistis

Timeline pra-live & pasca-live ada di §XIV.2 dan §XIV.5 — sengaja tidak diduplikasi di sini supaya konsisten (Aturan #1). Lihat juga §XIV.4 untuk checklist sebelum live.

## XIII.6 Metrik Monitoring

| Metrik | Target | Track di |
|--------|--------|----------|
| Precision | 55% | setup_performance |
| Recall | 60% | (derived) |
| Tier A accuracy | 70% | signal_history |
| False positive | <40% | signal_history |
| Uptime | 95% | GitHub Actions log |

## XIII.7 Alert ke Operator

| Trigger | Alert |
|---------|-------|
| 2 critical source mati | 🚨 source health |
| Black swan mode | 🚨 black swan |
| Signal Tier A | 🟢 (ini memang di-push) |
| Error rate > 10% | ⚠️ agent error |
| Daily recap | 📊 jam 07:00 WIB |

Nggak ada spam. Push cuma kalau perlu. Aturan dedup & klasifikasi: Bagian XVIII §XVIII.7.

## XIII.8 Retention Job

Jalan tiap 24 jam:

```python
async def run_retention_job(memory):
    await downsample_ohlcv(memory, raw_days=7, hourly_days=90)
    await purge_old_signals(memory, days=90)
    await purge_old_liquidations(memory, days=30)
    await purge_old_health_logs(memory, days=7)

    if days_since_last_baseline_update() >= 14:
        await update_all_volume_baselines(memory)

    memory.checkpoint_wal()
```

## XIII.9 Zero Fee Confirmation

| Komponen | Biaya |
|----------|-------|
| Data sources (14) | $0 |
| Gemini (free tier) | $0 |
| Telegram Bot | $0 |
| GitHub Actions (public repo) | $0 |
| SQLite | $0 |
| **Total** | **$0/bulan** |

## XIII.10 Recovery dari Crash

```python
async def startup_recovery(memory):
    # 1. Cek active_signals yang expired
    expired = memory.get_expired_signals()
    for signal in expired:
        memory.mark_signal_expired(signal)

    # 2. Tutup sinyal yang expired/kena TP-SL selama bot mati (Bagian XVI §XVI.9 edge 4-5)
    await check_signal_expiry(memory, market_data, now_wib())

    # 3. Resume dari state terakhir
    last_cycle = memory.get_last_cycle()
    logger.info(f"Resume from cycle {last_cycle}")
```

## XIII.11 File Structure — Fase A (Single File)

Keputusan: tahap awal single file. Pecah ke modular setelah semua P0+P1 selesai + stabil 30 hari.

```
cryptone_v45/
├── cryptone_v45.py              # SEMUA logic (~7k baris)
│   ├── §1   Header + Constants
│   ├── §2   Parameter Framework
│   ├── §3   Dataclasses
│   ├── §4   MemoryEngine
│   ├── §5   Data Sources
│   ├── §6   Agents
│   ├── §7   Pipeline 8 layer
│   ├── §8   BlackSwan Detector
│   ├── §9   Correlation Engine
│   ├── §10  LLM Client (Gemini)
│   ├── §11  Bootstrap / Backtest
│   ├── §12  Telegram UI
│   ├── §13  Delivery Queue
│   ├── §14  run_live_service
│   └── §15  CLI entry
│
├── cryptone_v45_test.py         # Test suite
├── requirements.txt
├── .github/workflows/
│   ├── live.yml
│   └── keepalive.yml
└── data/
    └── cryptone_v45.db
```

Struktur Fase B (nanti): pecah jadi `cryptone_v45.py` (core), `cryptone_v45_telegram.py` (UI), `cryptone_v45_test.py` (test). Jangan pecah terlalu dini.

## XIII.12 Startup Sequence

```
1. setup_logging()
2. ensure_thread_pool()
3. MemoryEngine.start()          → schema migration
4. startup_recovery(memory)      → fix orphan state
5. Bootstrap backtest            → kalau belum pernah (deteksi via `bootstrap_meta`, lihat §XIII.3)
6. TelegramDeliveryQueue.start() → sender loop
7. run_live_service()            → main loop
   ├── Universe scan (tiap 15 min) → active_symbols dinamis
   ├── Signal expiry check (tiap cycle, SEBELUM trigger — Bagian XVI §XVI.5)
   ├── Trigger detection (tiap 5 min)
   ├── Black swan detector (paralel)
   ├── Correlation update (tiap 1 jam)
   └── Retention job (tiap 24 jam)
```

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Deploy GitHub Actions | Bagian VI (artifact DB) | Persistence |
| Retention job | Bagian VI (retention policy) | Konsisten |
| Bootstrap | Bagian XIV (fase 6) | Timeline |
| Single file Fase A | Bagian XV (commit format) | Section-based |
| Startup sequence | Bagian I (8 layer init order) | Konsisten |

---


# BAGIAN XIV — ROADMAP IMPLEMENTASI (13_ROADMAP.md)

Status: FINAL

## XIV.1 File Structure — Fase A: Single File

Keputusan: tahap awal single file. Pecah ke modular setelah semua fitur P0 + P1 selesai dan stabil minimal 30 hari.

Alasan:
- Single file = 1 titik truth, gampang track progress
- Pecah terlalu dini = overhead di tengah iterasi
- Modularitas = reward setelah selesai, bukan requirement di awal

Struktur Fase A identik dengan §XIII.11.

Kapan pecah (Fase B):
- ✅ Semua fitur P0 selesai
- ✅ Running stabil 30 hari
- ✅ Udah ngerti bagian mana yang sering diubah
- ✅ Baru pecah berdasarkan pengalaman nyata

Jangan pecah kalau:
- ❌ Masih iterasi fitur
- ❌ Masih sering debug
- ❌ Belum tau hot spot

## XIV.2 Timeline 7 Minggu — PRA-LIVE

(Fase development, berakhir di live pertama)

| Fase | Deliverable | Estimasi |
|------|-------------|----------|
| Minggu 1 | §1-4: Constants + Framework + Dataclasses + MemoryEngine | 7 hari |
| Minggu 2 | §5-7: Data Sources + Agents + Pipeline | 7 hari |
| Minggu 3 | §8-11: BlackSwan + Correlation + LLM + Bootstrap | 7 hari |
| Minggu 4 | §12-15: Telegram UI + Delivery + main loop | 7 hari |
| Minggu 5 | Test suite lengkap + bug fix | 7 hari |
| Minggu 6 | Bootstrap backtest 60 hari + tuning | 7 hari |
| Minggu 7 | Deploy + Live + observasi awal | 7 hari |

Total: ~7 minggu sampai live pertama.

## XIV.3 Prioritas Fitur

| P0 (Wajib) | P1 (Penting) | P2 (Nice) |
|------------|--------------|-----------|
| Universe Scanner | Settings via Telegram | Wallet tracking user |
| Trigger Detection | Playbook | Smart Money wallet |
| Confirm Layer | Backtest on-demand | Market breadth |
| Timing (Analyst) | Confluence score | RR visualizer |
| Context Modifier | Setup performance | — |
| Black Swan | Volume session-aware | — |
| Signal Delivery | — | — |
| Correlation | — | — |

Rules: P0 dulu semua. Kalau P0 selesai, baru P1. P2 cuma kalau ada waktu.

Fase A (single file) target: P0 + P1 selesai. Fase B (modular) dilakukan setelah semua P0+P1 stabil.

## XIV.4 Sebelum Live — Checklist

- [ ] Bootstrap backtest 60 hari selesai
- [ ] Bot jalan 5 jam tanpa crash
- [ ] Semua source terhubung (health check)
- [ ] Telegram bot tested end-to-end
- [ ] Signal format sesuai spec
- [ ] Settings via Telegram bekerja
- [ ] Rollback tested
- [ ] GitHub Actions workflow stabil
- [ ] Retention job tested
- [ ] Black swan trigger tested
- [ ] Single file masih maintainable (>7k baris = tanda siap pecah)

## XIV.5 Realita Post-Launch

| Minggu | Yang akan terjadi | Yang harus dilakukan |
|--------|-------------------|----------------------|
| 1-2 | Bug di mana-mana | Fix cepat, jangan tambah fitur |
| 3-4 | Signal mulai masuk | Observasi, jangan tuning |
| 5-6 | Kualitas signal bervariasi | Tuning 1 parameter/minggu |
| 7+ | Precision mulai stabil | Iterasi based on data |
| 30+ | Stabil, ngerti hot spot | Baru pertimbangkan pecah ke modular |

Jangan tuning 5 parameter sekaligus. Bingung, nggak tau mana yang ngefek.

## XIV.6 Yang Perlu Disiapkan

| Item | Waktu | Prioritas |
|------|-------|-----------|
| GitHub repo public | 5 menit | P0 |
| Telegram bot (@BotFather) | 5 menit | P0 |
| Gemini API key | 5 menit | P0 |
| Etherscan API key | 5 menit | P1 |
| FRED API key | 5 menit | P1 |
| Bootstrap backtest | 30 menit | P0 |

## XIV.7 Handoff Protocol (Multi-Engineer)

### Format Commit Message

```
[SECTION] Component — short description

File: cryptone_v45.py §12
Related: 11_TELEGRAM.md §XII.3

What changed:
- Added X
- Fixed Y

Testing:
- Unit test: PASS
- Integration: PASS

Notes:
- Known issue: Z
```

Catatan: [SECTION] = §1-§15 (section di single file). Kalau nanti udah modular, ganti jadi [LAYER] + file path.

### Format PR Description

```markdown
## Section
§6 (Agents — Analyst)

## File Changed
- cryptone_v45.py §6
- cryptone_v45_test.py §X1

## Reference
- 04_SETUPS.md §IV.2
- 02_PARAMETERS.md §II.2

## What
Implement Analyst Agent untuk setup Funding Squeeze.

## Test
- Unit: PASS (5/5)
- Integration: PASS (2/2)
- Edge case: PASS (3/3)

## Cross-check
- [x] Definisi kanonik (nggak duplikat)
- [x] Parameter masuk kategori A/B/C/D
- [x] Nggak ada orphan function
- [x] Riwayat Cross-Check diupdate
```

### Handoff Note

```markdown
## Handoff — 2026-09-24

### Current state
- §1-3 (Constants, Framework, Dataclasses): DONE ✅
- §4 (MemoryEngine): DONE ✅
- §5 (Data Sources): IN PROGRESS (60%)
- §6-15: TODO

### What's working
- Universe scanner fetch 200 symbol
- Trigger detection for funding
- Horizon classifier

### What's broken
- Liquidation agent: rate limit di WS Binance
- Test suite: 3 test gagal

### Next step
- Fix WS reconnection
- Add cascade detection
- Then lanjut §6 (Agents — Timing)

### Notes
- Parameter funding_extreme_pct udah di-tune ke 97
- Jangan lupa baca 07_BLACKSWAN.md
- Single file masih <2000 baris, belum perlu pecah
```

## XIV.8 Change Protocol

### Yang Boleh Diubah (Low Risk)
- Nambah parameter (wajib kategori A/B/C/D)
- Nambah command Telegram
- Nambah fitur assistant
- Nambah data source (kalau zero-fee)
- Nambah test case
- Rename variable internal

### Yang Butuh Konfirmasi (Medium Risk)
- Ubah threshold default di kategori A
- Ubah formula ranking
- Ubah signal format
- Nambah layer
- Ubah pipeline order
- Nambah LLM provider

### Yang JANGAN Diubah (High Risk)
- Filosofi radar (bukan executor)
- Framework 4 kategori
- Min confirmations (2/3)
- Black swan trigger (3/7)
- Single-writer pattern
- Zero-fee principle
- Universe dinamis (nggak boleh hardcode daftar simbol — Aturan #7)
- 4 outcome sinyal & bobot PARTIAL 0.5 (Bagian XVI)

### Prosedur Ubah
1. Diskusi dulu — jelaskan alasan, dampak, alternatif
2. Update dokumen — bukan langsung kode
3. Update Riwayat Cross-Check
4. Bump version — schema_version + PROTOCOL_VERSION
5. Update test suite
6. Baru kode

Rules: **Dokumen dulu, kode kemudian. Jangan pernah kebalik.**

### Versioning

```
V4.5.0     — initial (single file, sekarang)
V4.5.1     — bug fix, minor tuning
V4.5.2     — fitur tambahan (P1)
V4.5.3     — single file stabil
V4.5.4     — pecah ke modular (milestone)
V4.6.0     — perubahan arsitektur
V5.0.0     — milestone besar
```

Catatan: pecah ke modular = V4.5.4, bukan V4.6. Karena isinya tetap V4.5, cuma reorganisasi file.

## XIV.9 Test Suite Requirement

### Struktur Test (Single File)

```
cryptone_v45_test.py
├── §X1  Unit Tests
│   ├── test_constants
│   ├── test_parameter_framework
│   ├── test_dataclasses
│   ├── test_memory_engine
│   ├── test_data_sources
│   └── test_agents
│
├── §X2  Integration Tests
│   ├── test_pipeline_end_to_end
│   ├── test_telegram_commands
│   └── test_settings_update
│
├── §X2b Lifecycle & Error Tests
│   ├── test_signal_lifecycle_4_outcomes      (Bagian XVI)
│   ├── test_tp_sl_same_candle_pessimistic    (Bagian XVI §XVI.4)
│   ├── test_tp1_hit_persists_across_restart  (Bagian XVI §XVI.8)
│   ├── test_cli_mutually_exclusive_modes     (Bagian XVII)
│   ├── test_no_hardcoded_symbols             (Aturan #7)
│   └── test_retry_and_circuit_breaker        (Bagian XVIII §XVIII.3)
│
├── §X3  Backtest Tests (Minggu 6)
│   ├── test_bootstrap_run
│   └── test_signal_history_filled
│
└── §X4  Mock Data
    ├── mock_ohlcv
    ├── mock_funding
    └── mock_candles
```

### Coverage Target
- Core (cryptone_v45.py): >70%
- Critical path (signal generation): >90%

### Test Runner

```bash
python cryptone_v45_test.py --all
python cryptone_v45_test.py --section 6
python cryptone_v45_test.py --unit
python cryptone_v45_test.py --integration
```

Setiap section (§1-§15) wajib punya minimal 1 unit test. Kalau nggak ada test = nggak dianggap selesai.

## XIV.10 Migrasi Single File → Modular (Fase B)

Trigger untuk migrasi:
- Semua P0+P1 selesai
- Running stabil minimal 30 hari
- File >7k baris
- Udah tau bagian mana yang sering diubah (hot spot)

Cara migrasi (jangan sekali jalan):

```
Step 1: Bikin copy cryptone_v45.py → cryptone_v45_v2.py
Step 2: Extract section satu per satu ke file baru
Step 3: Setiap extract, run test suite — pastikan pass
Step 4: Commit setiap extract (jangan batch)
Step 5: Setelah semua extract, hapus cryptone_v45.py lama
Step 6: Rename cryptone_v45_v2.py → cryptone_v45.py
```

Kalau ada 1 section yang gagal dipisah: balik, jangan paksa.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Single file dulu | XIV.1, XIV.7, XIV.8, XIV.9 | Pecah modular jadi Fase B |
| Section numbering | XIV.7 commit format | Ganti [LAYER] jadi [SECTION] |
| Versioning | XIV.8 | Tambah V4.5.4 (milestone pecah) |
| Test structure | XIV.9 | Section-based, bukan file-based |
| Migrasi | XIV.10 | Cara aman pindah single → modular |

---


# BAGIAN XV — ENGINEER PROMPTS (14_ENGINEER_PROMPTS.md)

Tujuan: pandu AI engineer biar efisien, minimize residual error, dan konsisten dengan V4.5.

Karakter AI yang perlu di-handle:
- **Claude:** terlalu teliti, buang sesi buat hal yang nggak esensial. Tendency: over-engineer, nambah komentar panjang, "improve" kode yang udah OK.
- **Grok:** terlalu casual, kadang skip detail, suka improvisasi di luar spec. Tendency: hardcode, asumsi ngawur, nggak baca spec.
- **GPT:** mix. Kadang panjang, kadang skip. Suka halusinasi field yang nggak ada.

---

## PROMPT UNIVERSAL (Dipakai Semua AI)

Copy-paste ini di awal sesi:

```
Saya bekerja di proyek Cryptone V4.5 — Market Screener Radar + Trader Assistant.

SEBELUM KERJA, WAJIB:
1. Baca semua file di folder cryptone_v45_docs/
2. Pahami 7 prinsip inti (00_PRODUCT.md §0.2)
3. Pahami arsitektur 8 layer (01_ARCHITECTURE.md)
4. Pahami parameter framework 4 kategori (02_PARAMETERS.md)

ATURAN MAIN:
- Definisi kanonik: satu konsep = satu definisi. Jangan duplikat.
- Parameter baru WAJIB masuk kategori A/B/C/D.
- Nggak boleh ada orphan function (didefinisi tapi nggak dipanggil).
- JANGAN hardcode simbol (BTC/ETH/SOL dsb.). Simbol dari universe dinamis / market anchor (Aturan #7).
- Setiap perubahan catat di Riwayat Cross-Check.
- Test suite WAJIB update kalau ubah logic.
- Dokumen dulu, kode kemudian.
- Single file dulu (`cryptone_v45.py`), modular nanti.

KALAU RAGU:
- Jangan improvisasi. Tanya dulu.
- Jangan asumsi. Baca spec.
- Jangan "improve" kode yang nggak diminta.

SCOPE TASK:
[LO TULIS DI SINI: section mana, deliverable apa]

OUTPUT YANG DIHARAPKAN:
[LO TULIS: kode? test? review? bug fix?]
```

---

## PROMPT KHUSUS CLAUDE

Masalah karakter Claude: Terlalu teliti. Buang sesi buat:
- Nambah komentar panjang yang nggak diminta
- Refactor kode yang udah OK
- "Improve" sesuatu di luar scope
- Tulis 500 kata penjelasan buat perubahan 5 baris
- Tanya balik 10 kali sebelum kerja

Strategi:
1. Batesi output length. Minta output konkret, bukan essay.
2. Larang refactor di luar scope.
3. Force decision. Kalau ragu, kasih pilihan A/B/C.
4. Skip explanation. Cuma butuh kode, bukan penjelasan.

```
KONTEKS: Cryptone V4.5, section [X]. Baca docs sesuai PROMPT UNIVERSAL.

TASK: [spesifik, 1-2 kalimat]

KONSTRAIN:
- Output kode dulu, penjelasan max 3 kalimat.
- JANGAN refactor kode di luar scope.
- JANGAN nambah komentar panjang (>2 baris per blok).
- JANGAN tanya balik kecuali ada ambiguity KRITIS.
- Kalau ragu, kasih 2-3 opsi A/B/C, pilih default sendiri.
- Test suite wajib update.

DELIVERABLE:
1. Kode (utuh atau patch)
2. Test (unit + edge case)
3. Commit message (format handoff)

JANGAN:
- Tulis essay penjelasan
- Suggest "improvements" di luar scope
- Nambah fitur yang nggak diminta
- Pindahin kode ke file baru tanpa alasan kuat

Batas waktu sesi: 1 task = 1 response. Jangan multi-round.
```

Expected output Claude: kode + test + commit message. < 200 baris total.

---

## PROMPT KHUSUS GROK

Masalah karakter Grok: Terlalu casual. Cenderung:
- Skip baca spec, langsung kode
- Hardcode nilai yang seharusnya dinamis
- Asumsi ngawur (misal ngarang field yang nggak ada)
- Improvisasi di luar spec
- Nggak test edge case

Strategi:
1. Force baca docs. Wajib quote prinsip yang relevan.
2. Force kategori parameter. Tiap parameter, jelasin kategori A/B/C/D.
3. Force edge case test. Minimal 3 edge case.
4. Larang hardcode. Kalau ada angka, sebutin kategori.

```
KONTEKS: Cryptone V4.5. WAJIB baca docs sebelum kode:
- 00_PRODUCT.md §0.2 (7 prinsip)
- 01_ARCHITECTURE.md (8 layer)
- 02_PARAMETERS.md (kategori A/B/C/D)
- 04_SETUPS.md (kalau related setup)

TASK: [spesifik]

ATURAN KETAT:
1. Setiap parameter yang lo tulis, jelasin:
   - Kategori: A / B / C / D
   - Alasan: kenapa kategori itu

2. Setiap function, jelasin:
   - Input
   - Output
   - Edge case yang di-handle

3. JANGAN hardcode angka. Kalau angka muncul, harus dari kategori A/B/C/D.

4. WAJIB tulis test dengan minimal 3 edge case:
   - Empty input
   - Invalid input
   - Boundary case

5. JANGAN asumsi field/table yang nggak ada. Cek dulu di docs.

6. Kalau nggak tau, BILANG "nggak tau". Jangan ngarang.

DELIVERABLE:
1. Parameter dengan kategori eksplisit
2. Kode
3. Test dengan edge case
4. Commit message

QUOTE 1 prinsip V4.5 yang paling relevan dengan task ini di awal response.
```

Expected output Grok: kode + test + kategori parameter. Kualitas lebih tinggi karena forced discipline.

---

## PROMPT KHUSUS GPT

Masalah GPT: Mix. Kadang panjang (kayak Claude), kadang skip (kayak Grok). Suka ngarang field yang nggak ada di spec.

Strategi:
1. Force cek docs. Sebelum ngoding, sebutin file + section yang dibaca.
2. Force struktur output.
3. Force skip halusinasi.

```
KONTEKS: Cryptone V4.5.

SEBELUM KODE, output ini:
1. File docs yang lo baca: [list]
2. Section yang relevan: [quote]
3. Prinsip yang applicable: [list]
4. Field/table yang lo pakai: [list — dan konfirmasi ada di docs]

BARU KODE.

ATURAN:
- Jangan ngarang field. Kalau pakai `signal.magic_field`, harus ada di docs.
- Jangan skip edge case.
- Jangan over-explain.

TASK: [spesifik]
```

---

## PROMPT HANDOFF (Kalau Ganti AI/Engineer)

```
Kamu nerima handoff dari engineer sebelumnya.

BACA DULU:
1. Semua docs di cryptone_v45_docs/
2. .handoff/last_state.md (state terakhir)
3. git log -20 (commit terakhir)

STATE SEKARANG:
- §1-3 (Constants, Framework, Dataclasses): DONE ✅
- §4 (MemoryEngine): DONE ✅
- §5 (Data Sources): IN PROGRESS (60%)
- §6-15: TODO

TASK LO:
1. Fix 3 test yang gagal
2. Lanjut §5 sampai DONE
3. Update .handoff/last_state.md

ATURAN:
- Jangan ubah apapun di §1-4 tanpa alasan kuat
- Kalau ada bug di section lama, fix + catat di Riwayat Cross-Check
- Setiap commit, format sesuai handoff protocol (13_ROADMAP.md §XIV.7)
```

---

## PROMPT REVIEW CODE (Buat Validasi)

```
Review code berikut terhadap spec V4.5.

CODE:
[tempel kode]

CHECKLIST:
- [ ] Definisi kanonik (nggak duplikat)?
- [ ] Parameter ada kategori A/B/C/D?
- [ ] Ada orphan function?
- [ ] Test coverage cukup?
- [ ] Edge case di-handle?
- [ ] Riwayat Cross-Check diupdate?
- [ ] Konsisten dengan prinsip V4.5?
- [ ] Nggak hardcode yang seharusnya dinamis?
- [ ] Nggak dinamis yang seharusnya hardcode?

OUTPUT:
- Pelanggaran: [list]
- Saran fix: [list]
- Priority: P0/P1/P2
```

---

## PROMPT REFACTOR (Kalau Perlu)

```
Refactor kode berikut. TAPI:

ATURAN REFACTOR:
- Jangan ubah behavior. Cuma struktur.
- Jangan ubah interface (function signature).
- Jangan ubah nama public function/class.
- Test lama WAJIB masih pass.
- Kalau perlu test baru, tulis + jelasin kenapa.

SCOPE:
- Cuma refactor [section ini / function ini]
- JANGAN refactor section lain.

CODE:
[tempel]
```

---

## PROMPT BUG FIX

```
Bug: [deskripsi]

BACA DULU:
- docs terkait
- Kode related
- Test yang gagal

ROOT CAUSE analysis:
1. Apa yang seharusnya terjadi
2. Apa yang sebenarnya terjadi
3. Kenapa

FIX:
1. Perbaiki di titik akarnya (bukan gejala)
2. Update test (tambah test untuk bug ini)
3. Catat di Riwayat Cross-Check

JANGAN:
- Fix gejala doang
- Tambah workaround tanpa jelasin
- Ubah kode lain yang nggak related
```

---

## Rules Universal untuk Semua AI

### Wajib
1. Baca docs dulu. Nggak ada pengecualian.
2. Test wajib. Kode tanpa test = nggak selesai.
3. Kategori parameter. Setiap angka, sebutin kategorinya.
4. Commit format. Ikutin handoff protocol (§XIV.7).
5. Cross-check. Update Riwayat Cross-Check tiap ada perubahan.

### Dilarang
1. Improvisasi. Kalau nggak ada di spec, tanya.
2. Hardcode. Angka harus dari kategori.
3. Refactor di luar scope. Fokus task yang dikasih.
4. Halusinasi field. Cek docs dulu.
5. Skip edge case. Minimal 3 test case.

### Kalau Ragu
- Nggak tau? Bilang nggak tau.
- Ambigu? Kasih opsi A/B/C, pilih default.
- Spec kurang jelas? Tanya dulu, jangan asumsi.

### Time Budget
- 1 task = 1 response. Jangan multi-round.
- Output max 500 baris. Lebih dari itu, pecah.
- Kompleks? Kerjakan bertahap, commit tiap sub-task.

---

## Contoh Good Response

```
§6 — Confirm Layer

Parameter baru (kategori):
- cascade_size_threshold_pct: [B] Dynamic
  Alasan: per symbol, dari histori
- min_confirmations: [A] Hardcoded (locked = 2)
  Alasan: hukum sinyal

Kode:
[~80 baris]

Test:
- Empty candidate list → skip
- Only 1 confirm → reject
- Boundary cascade → edge value

Commit:
[SECTION 6] Add cascade detection for confirm layer

File: cryptone_v45.py §6
Related: 01_ARCHITECTURE.md §I.3

What changed:
- Added FundingAgent, LiquidationAgent, VolatilityAgent
- Fixed edge case for empty candidate list

Testing:
- Unit test: PASS (5/5)
- Integration: PASS (2/2)
```

Response bersih. Fokus. Ada test. Ada kategori.

## Contoh Bad Response

**Claude over-teliti:**
```
Berdasarkan analisis mendalam saya terhadap arsitektur V4.5,
saya menemukan bahwa pendekatan yang lebih baik mungkin adalah
refactor seluruh §6. Setelah mempertimbangkan 15 edge case,
saya propose...

[Screenshot: 800 kata essay, kode baru 20 baris]
```
Problem: Buang sesi. Nggak fokus.

**Grok under-teliti:**
```
Ya gitu aja bro. Ini kodenya:

[hardcode "BTC"] if price > 85000: signal = SHORT   # SALAH: simbol & harga hardcode
```
Problem: Hardcode, no test, no kategori.

## Version Control untuk Prompt

```
v1.0 (2026-09-24) — Initial
v1.1 — Tambah section untuk GPT
v1.2 — Perbaiki Claude prompt (kurang tegas)
```

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| Engineer prompts | Bagian XIV (handoff protocol) | Konsisten format |
| Rules universal | Semua bagian | Governance |
| Time budget | Bagian XIV (test suite) | 1 task = 1 response |

---


# BAGIAN XVI — SIGNAL LIFECYCLE & EXPIRY (15_SIGNAL_LIFECYCLE.md)

Status: FINAL

## XVI.1 Prinsip

Setiap sinyal punya siklus hidup tertutup: **ACTIVE → salah satu dari 4 outcome.** Nggak ada sinyal yang "lupa ditutup", dan nggak ada outcome `UNKNOWN`.

Tiga aturan keras:
1. **Resolusi pakai high/low candle, bukan snapshot harga.** Wick yang nyentuh TP/SL di antara dua cycle tetap kehitung.
2. **Threshold hit berbasis ATR, bukan persen tetap.** Persen tetap rusak di horizon scalping (SL bisa lebih kecil dari buffer).
3. **State hit disimpan di DB, bukan di memori.** Bot restart tiap ~6 jam (GitHub Actions); flag di RAM akan hilang.

## XVI.2 Diagram Status

```
                        ┌─► TP2 hit ──────────────► RESOLVED · PROFIT
                        │
ACTIVE ─── tiap cycle ──┼─► SL hit ───────────────► RESOLVED · LOSS
                        │
                        └─► valid_until lewat ─┬──► EXPIRED · PARTIAL  (TP1 hit, TP2 belum)
                                               └──► EXPIRED · NEUTRAL  (TP1 belum hit)
```

## XVI.3 Empat Outcome

| Outcome | Kondisi | Kontribusi ke win rate | Kontribusi ke trust/setup |
|---------|---------|------------------------|---------------------------|
| PROFIT | TP2 hit sebelum SL & sebelum expiry | 1.0 win | Penuh |
| PARTIAL | TP1 hit, lalu expired sebelum TP2/SL | 0.5 win (bobot) | Setengah |
| LOSS | SL hit (sebelum TP2) | 1.0 loss | Penuh |
| NEUTRAL | Expired tanpa TP1, tanpa SL | Tidak dihitung | Tidak dihitung |

**Kenapa PARTIAL bobot 0.5, bukan win penuh:** kalau PARTIAL dihitung 1 win penuh, win rate menggelembung (terutama swing, di mana TP1 cuma 40% posisi). Bobot 0.5 menjaga metrik jujur. Bobot ini Kategori A (hukum sinyal).

```
win_rate = (PROFIT + 0.5 × PARTIAL) / (PROFIT + PARTIAL + LOSS)
```

NEUTRAL sengaja dikeluarkan dari penyebut: itu bukan trade yang terbukti benar atau salah, jadi nggak boleh mengencerkan atau menaikkan win rate.

### Kasus TP1 hit lalu balik kena SL

Kalau TP1 sudah hit, lalu harga balik dan kena SL sebelum TP2: outcome = **PARTIAL**, bukan LOSS. Alasan: TP1 sudah merealisasikan sebagian profit (50-60% posisi per parameter setup). Ini konsisten dengan asumsi trader mengamankan TP1.

## XVI.4 Deteksi Hit (Berbasis Candle + ATR)

### Sumber data

Tiap cycle, ambil candle 1m/5m sejak `last_checked_at` sinyal (bukan cuma harga terakhir). Pakai `high` dan `low` tiap candle.

### Definisi hit

```python
def price_touched(level, direction, candles, atr, tolerance_atr):
    """
    level      : harga TP/SL
    direction  : arah sinyal (LONG/SHORT) untuk menentukan sisi sentuhan
    candles    : candle sejak last_checked_at
    tolerance  : toleransi dalam satuan ATR (Kategori C)
    """
    buffer = tolerance_atr * atr
    for c in candles:
        if direction == "LONG":
            # TP di atas entry, SL di bawah entry
            ...
    return hit_time or None
```

Dua fungsi turunan yang dipakai lifecycle:

| Fungsi | LONG | SHORT |
|--------|------|-------|
| `hit_tp(level)` | candle.high ≥ level − buffer | candle.low ≤ level + buffer |
| `hit_sl(level)` | candle.low ≤ level + buffer | candle.high ≥ level − buffer |

Buffer dipakai untuk menerima "hampir nyentuh" (spread/slippage), bukan menyaring wick.

### Kalau TP dan SL kena di candle yang sama

Nggak bisa tahu urutannya dari satu candle. Aturan pesimis: **SL dianggap kena duluan.** Kalau granularitas lebih halus tersedia (candle 1m), pakai itu untuk menentukan urutan sebelum jatuh ke aturan pesimis.

## XVI.5 Expiry Check Job

Jalan tiap cycle, **sebelum** trigger detection (supaya slot `max concurrent signals` kebebasan dihitung benar).

```python
async def check_signal_expiry(memory, market_data, now):
    """
    Iterasi semua sinyal ACTIVE. Aman dipanggil ulang (idempotent).
    Semua state (tp1_hit, last_checked_at) dibaca/ditulis dari DB.
    """
    for sig in memory.get_active_signals():
        try:
            candles = await market_data.get_candles_since(
                sig.symbol, since=sig.last_checked_at, tf="1m"
            )
        except DataUnavailable:
            # Edge: data gagal. Jangan resolve tanpa data. Coba cycle depan.
            memory.record_skip(sig.signal_id, reason="NO_DATA")
            continue

        atr = memory.get_atr(sig.symbol, sig.horizon_tf)
        tol = params.hit_tolerance_atr            # Kategori C

        # Urutan cek: SL dulu (pesimis), lalu TP2, lalu TP1
        if hit_sl(sig, candles, atr, tol):
            outcome = "PARTIAL" if sig.tp1_hit else "LOSS"
            await resolve_signal(sig, outcome, memory, now)
            continue

        if hit_tp(sig, sig.tp2, candles, atr, tol):
            await resolve_signal(sig, "PROFIT", memory, now)
            continue

        if not sig.tp1_hit and hit_tp(sig, sig.tp1, candles, atr, tol):
            memory.mark_tp1_hit(sig.signal_id, at=now)   # persisten di DB
            sig.tp1_hit = True

        memory.update_last_checked(sig.signal_id, now)

        if now >= sig.valid_until:
            outcome = "PARTIAL" if sig.tp1_hit else "NEUTRAL"
            await resolve_signal(sig, outcome, memory, now)
```

Catatan urutan: SL dicek sebelum TP2 karena aturan pesimis. Tapi kalau `tp1_hit` sudah true saat SL kena, hasilnya PARTIAL (lihat §XVI.3).

## XVI.6 Resolve Signal

`resolve_signal` adalah **satu-satunya** tempat outcome ditulis. Ini menjaga kanonik (Aturan #1).

```python
async def resolve_signal(sig, outcome, memory, now):
    assert outcome in ("PROFIT", "PARTIAL", "LOSS", "NEUTRAL")

    # 1. Tulis ke signal_history (append-only event log)
    memory.set_signal_outcome(sig.signal_id, outcome, resolved_at=now)

    # 2. Keluarkan dari active_signals (slot bebas)
    memory.remove_active_signal(sig.signal_id)

    # 3. Update statistik (single-writer queue, Bagian VI §VI.8)
    await memory.enqueue_write("performance_update", {
        "symbol": sig.symbol,
        "setup_type": sig.setup_type,
        "outcome": outcome,
        "realized_rr": compute_realized_rr(sig, outcome),
        "hold_hours": (now - sig.created_at).total_seconds() / 3600,
    })

    # 4. Notifikasi ringkas ke user (kalau Tier A/B dan notifikasi aktif)
    await maybe_notify_resolution(sig, outcome)
```

**Signature kontrak tambahan** (dipakai VII.3 Black Swan, implementasi di `.py`):

```python
def extend_signal_validity(signal_id: str, hours: int) -> None:
    """Tambah `valid_until` sinyal ACTIVE sebanyak `hours` jam. Dipanggil saat black swan mode aktif (§VII.3) supaya sinyal tidak invalid akibat volatilitas non-organik.

    KONTRAK DUAL-WRITE (wajib): `signal_history.valid_until` DAN
    `active_signals.expires_at` adalah salinan satu sama lain (lihat
    "Kontrak signal_history <-> active_signals" di atas -- keduanya
    ditulis dalam satu operasi saat sinyal dibuat). extend_signal_validity
    HARUS meng-update KEDUA kolom itu dalam SATU transaksi SQLite lewat
    enqueue_write() (single-writer, §VI.8), bukan cuma signal_history.
    Kalau cuma signal_history.valid_until yang di-extend sementara
    active_signals.expires_at tetap, expiry check yang baca
    active_signals (jalur baca paling sering dipakai runtime) akan salah
    menganggap sinyal sudah expired padahal belum.

    Implementasi wajib setara dengan:
        UPDATE signal_history  SET valid_until = valid_until + hours
            WHERE signal_id = ? AND outcome IS NULL;
        UPDATE active_signals  SET expires_at  = expires_at  + hours
            WHERE signal_id = ?;
    dieksekusi sebagai satu payload enqueue_write (bukan dua enqueue_write
    terpisah, supaya tidak ada window di mana salah satu ter-update duluan).
    """
    ...
```

### Aturan update statistik

| Outcome | symbol_performance | setup_performance |
|---------|--------------------|-------------------|
| PROFIT | wins += 1 | wins += 1 |
| PARTIAL | partials += 1 | partials += 1 |
| LOSS | losses += 1 | losses += 1 |
| NEUTRAL | neutrals += 1 | tidak diupdate (bukan trade terbukti) |

`avg_rr` hanya dihitung dari PROFIT/PARTIAL/LOSS. NEUTRAL nggak punya R:R realized.

### Realized R:R per outcome

| Outcome | Realized R:R |
|---------|--------------|
| PROFIT | Rata-rata tertimbang TP1/TP2 sesuai split setup (mis. 50/50) |
| PARTIAL | Porsi TP1 saja, sisanya dianggap keluar di entry (0R) |
| LOSS | −1.0R |
| NEUTRAL | Tidak dihitung |

## XVI.7 Parameter

| Parameter | Kategori | Nilai | Alasan |
|-----------|----------|-------|--------|
| Check interval | A | Tiap cycle (5 menit) | Sinkron dengan trigger check |
| Sumber deteksi | A | High/low candle 1m | Wick nggak boleh terlewat |
| Aturan tie TP+SL sama candle | A | SL duluan | Pesimis, hindari false positive |
| Bobot PARTIAL | A | 0.5 | Jaga win rate jujur |
| Outcome valid | A | 4 nilai, nggak ada UNKNOWN | Semua sinyal harus resolve |
| `hit_tolerance_atr` | C | 0.0 - 0.15 ATR | Dinamis per horizon; default D = 0.05 |
| Default tolerance (cold-start) | D | 0.05 ATR | Sampai ≥ 20 outcome |

Kenapa ATR, bukan persen: sinyal scalping SL = 0.8x ATR bisa hanya ~0.3% dari entry. Buffer tetap 0.5% akan lebih besar dari jarak SL, jadi SL nggak pernah "kena". Buffer ATR ikut skala volatilitas tiap symbol.

## XVI.8 Schema

Definisi kanonik. Tabel ini menggantikan/melengkapi §VI.3 (`signal_history`) dan §VI.5 (`active_signals`).

### Kolom tambahan di `signal_history` (migration path — instalasi existing)

Kolom ini sudah ada langsung di `CREATE TABLE` §VI.3 untuk instalasi baru. Blok `ALTER` di bawah cuma migration path untuk DB yang sudah jalan dengan schema_version 1 (dibuat sebelum patch ini):

```sql
-- Bump schema_version ke 2 untuk tabel ini
ALTER TABLE signal_history ADD COLUMN setup_type TEXT;
ALTER TABLE signal_history ADD COLUMN tp1_hit BOOLEAN DEFAULT 0;
ALTER TABLE signal_history ADD COLUMN tp1_hit_at TIMESTAMP;
ALTER TABLE signal_history ADD COLUMN last_checked_at TIMESTAMP;
ALTER TABLE signal_history ADD COLUMN realized_rr REAL;
ALTER TABLE signal_history ADD COLUMN schema_version INTEGER DEFAULT 2;
```

`outcome` (sudah ada) hanya boleh berisi: `NULL` (masih ACTIVE), `PROFIT`, `PARTIAL`, `LOSS`, `NEUTRAL`.

### Kolom tambahan di tabel performa (migration path — instalasi existing)

Kolom ini sudah ada langsung di `CREATE TABLE` §VI.4 untuk instalasi baru. Blok `ALTER` di bawah cuma migration path untuk DB lama.

```sql
ALTER TABLE symbol_performance ADD COLUMN partials INTEGER DEFAULT 0;
ALTER TABLE setup_performance  ADD COLUMN partials INTEGER DEFAULT 0;
```

**Tidak ada** `ALTER TABLE setup_performance ADD COLUMN neutrals` — kolom ini sengaja dihapus dari spec. Sesuai tabel "Aturan update statistik" di atas, NEUTRAL tidak diupdate di `setup_performance` sama sekali, jadi kolom `neutrals` di tabel itu akan selalu bernilai 0 (dead column). `setup_performance` cuma punya: `wins`, `losses`, `partials`, `avg_rr`, `avg_hold_hours`. `neutrals` HANYA ada di `symbol_performance`.

### `active_signals`

`active_signals.expires_at` adalah **salinan** `signal_history.valid_until` untuk query cepat. Sumber kebenaran tetap `signal_history.valid_until`. Saat sinyal dibuat, keduanya ditulis dalam satu operasi.

## XVI.9 Edge Case

| # | Kasus | Perlakuan |
|---|-------|-----------|
| 1 | TP1 dan SL kena di candle sama | SL duluan (pesimis) kecuali candle 1m menunjukkan urutan lain |
| 2 | Black swan mode aktif | Sinyal lama TETAP dimonitor dan di-resolve normal. Black swan hanya membekukan sinyal baru |
| 3 | Data candle gagal diambil | Skip sinyal ini di cycle ini, coba lagi cycle depan. Jangan resolve tanpa data |
| 4 | Bot mati beberapa jam lalu restart | `last_checked_at` menentukan jendela candle yang dicek ulang. Wick selama bot mati tetap terdeteksi |
| 5 | Sinyal expired saat bot mati | Saat startup recovery (§XIII.10), jalankan `check_signal_expiry` sekali untuk menutup yang lewat |
| 6 | Harga gap melewati SL | Hit SL tetap tercatat; realized R:R bisa < −1.0R (slippage). Catat apa adanya |
| 7 | Sinyal di-Skip user via tombol | Status tetap ACTIVE untuk tracking statistik radar; user-skip hanya menyembunyikan notifikasi |
| 8 | Symbol ter-delist selagi ACTIVE | Resolve sebagai NEUTRAL dengan reason `SYMBOL_DELISTED`, keluarkan dari statistik |

## XVI.10 Integrasi /journal

```
📓 Signal Journal
━━━━━━━━━━━━━━━━━━━
Active (2):
   🔴 {symbol} SHORT · 12h left · TP1 ✅ · TP2 ⏳
   🟢 {symbol} LONG  · 4h left  · TP1 ⏳

Resolved (7 hari):
   ✓ PROFIT  : 3
   ~ PARTIAL : 2
   ✗ LOSS    : 1
   ⚪ NEUTRAL: 1

Win rate: 67%  ((3 + 0.5×2) / (3+2+1))
```

`{symbol}` diisi dari sinyal aktif saat itu, bukan daftar tetap.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 4 outcome + bobot PARTIAL | Bagian IV §IV.6 (win rate), Bagian III §III.5 (trust score) | Rumus win rate berubah |
| Kolom `tp1_hit`, `last_checked_at`, `realized_rr` | Bagian VI §VI.3 (signal_history) | Schema kanonik |
| Kolom `partials`/`neutrals` | Bagian VI §VI.4 (performance tables) | Track outcome baru |
| Expiry job sebelum trigger | Bagian XIII §XIII.12 (startup sequence) | Urutan main loop |
| Recovery expiry saat startup | Bagian XIII §XIII.10 | Tutup sinyal yang lewat saat bot mati |
| `hit_tolerance_atr` | Bagian II (Kategori C/D) | Parameter baru wajib kategori |

---

# BAGIAN XVII — CLI INTERFACE (16_CLI.md)

Status: FINAL

## XVII.1 Prinsip

Satu entry point: `cryptone_v45.py`. Satu mode per proses. CLI tipis: cuma parsing flag lalu memanggil fungsi yang sudah didefinisikan di bagian lain (nggak ada logika bisnis di CLI).

## XVII.2 Mode

Tepat satu mode harus dipilih (mutually exclusive):

| Flag | Fungsi | Memanggil |
|------|--------|-----------|
| `--live` | Main loop produksi | `run_live_service()` (§XIII.12) |
| `--bootstrap` | Backtest historis & isi DB | `run_bootstrap()` (§XIII.3) |
| `--test` | Jalankan test suite | test runner (§XIV.9) |
| `--dry-run` | Sama seperti live, tapi tanpa Telegram push. **Tidak butuh** `CRYPTONE_TELEGRAM_BOT_TOKEN`/`CRYPTONE_OPERATOR_CHAT_ID`/`CRYPTONE_USER_CHAT_ID` (§XIII.4) | `run_live_service(dry_run=True)` |
| `--check` | Validasi env + koneksi source lalu keluar | `run_preflight()` |
| `--recover` | Jalankan startup recovery saja lalu keluar | `startup_recovery()` (§XIII.10) |

## XVII.3 Opsi

| Opsi | Berlaku untuk | Default | Fungsi |
|------|---------------|---------|--------|
| `--days N` | `--bootstrap` | 60 | Panjang histori backtest |
| `--pin SYM[,SYM]` | live, dry-run, bootstrap | kosong | Symbol yang selalu ikut di-scan (tetap wajib lolos filter Layer 0, §II.8) |
| `--db PATH` | semua | `./data/cryptone_v45.db` | Lokasi database |
| `--log-level LEVEL` | semua | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `--cycle-interval SEC` | live, dry-run | 300 | Interval cycle |
| `--max-runtime SEC` | live, dry-run | 19800 | Batas runtime (5.5 jam, sinkron GH Actions) |
| `--section N` | `--test` | semua | Jalankan test section tertentu |
| `--unit` / `--integration` | `--test` | semua | Filter jenis test |

### Catatan penting: `--pin`, bukan `--symbols`

Versi awal menyebut `--symbols BTC,ETH`. Itu diganti `--pin` karena `--symbols` menyiratkan **daftar tetap**, padahal universe harus dinamis (Aturan #7). `--pin` hanya menambahkan symbol ke kandidat, dan symbol itu tetap wajib lolos filter likuiditas. Tanpa `--pin`, bot jalan dengan universe 100% dinamis.

## XVII.4 Implementasi

```python
def build_parser():
    p = argparse.ArgumentParser(prog="cryptone_v45.py")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--bootstrap", action="store_true")
    mode.add_argument("--test", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--recover", action="store_true")

    p.add_argument("--days", type=int, default=60)
    p.add_argument("--pin", type=parse_symbol_list, default=[])
    p.add_argument("--db", default=None)          # None = ambil dari env
    p.add_argument("--log-level", default=None)
    p.add_argument("--cycle-interval", type=int, default=None)
    p.add_argument("--max-runtime", type=int, default=None)
    p.add_argument("--section", type=int, default=None)
    p.add_argument("--unit", action="store_true")
    p.add_argument("--integration", action="store_true")
    return p
```

## XVII.5 Prioritas Konfigurasi

Urutan (tinggi menang):

```
1. Flag CLI
2. Environment variable (CRYPTONE_*)
3. runtime_settings di DB (hanya untuk parameter whitelist tuning, §II.6)
4. Default di kode
```

Flag CLI menimpa env; env menimpa default. `runtime_settings` hanya menyentuh parameter tuning, bukan path/token.

**Mapping eksplisit CLI → env name → default (gap sebelumnya: cuma `--db`/`--log-level` yang jelas None-fallback-nya):**

| Flag CLI | Env var | Default kode |
|----------|---------|---------------|
| `--db` | `CRYPTONE_DB_PATH` | `./data/cryptone_v45.db` |
| `--log-level` | `CRYPTONE_LOG_LEVEL` | `INFO` |
| `--cycle-interval` | `CRYPTONE_CYCLE_INTERVAL_SEC` | `300` |
| `--max-runtime` | `CRYPTONE_MAX_RUNTIME_SEC` | `19800` |
| `--pin` | `CRYPTONE_PINNED_SYMBOLS` | kosong (tidak ada env fallback untuk `--days`/`--section`/`--unit`/`--integration` — flag-only, `default` di argparse langsung final) |

Resolusi tiap parameter yang punya `default=None` di argparse (§XVII.4): kalau flag CLI tidak diberikan (`None`), baca env var terkait; kalau env juga tidak ada, pakai kolom "Default kode" di atas.

## XVII.6 Validasi Argumen

| Kondisi | Perilaku |
|---------|----------|
| `--days` di luar 7-365 | Error, exit code 2 |
| `--pin` berisi simbol yang tidak ada di HL meta | Peringatan + lanjut tanpa simbol itu (bukan crash) |
| `--pin` berisi simbol dengan listing age <7 hari (gagal filter §I.2 Layer 0 langkah 5) | **Peringatan** (bukan skip diam) + lanjut tanpa simbol itu di cycle sekarang. Log ke console/`CRYPTONE_LOG_LEVEL=WARNING`: `f"--pin {symbol} ditolak: listing age {age}d < 7d minimum"`. `--pin` TIDAK bisa membypass filter listing age Layer 0 — itu proteksi likuiditas, bukan preferensi yang bisa dioverride operator. |
| `--test` + `--pin` | Error, exit code 2 (argumen tidak valid — `--pin` tidak relevan untuk mode test) |
| Kombinasi mode lebih dari satu | argparse menolak otomatis |
| Env wajib kosong di `--live` | Error jelas, exit code 3 (lihat §XVII.7) |

## XVII.7 Exit Code

| Code | Arti |
|------|------|
| 0 | Selesai normal (termasuk timeout terencana `--max-runtime`) |
| 1 | Error tak terduga |
| 2 | Argumen tidak valid |
| 3 | Konfigurasi/env wajib hilang |
| 4 | Preflight gagal (source critical tidak terjangkau) |
| 5 | Database korup / tidak bisa dibuka |

Workflow GitHub Actions membedakan exit 0 (normal) dari yang lain; exit 0 karena `--max-runtime` bukan error.

## XVII.8 Contoh Pemakaian

```bash
# Produksi (GitHub Actions): universe 100% dinamis
python cryptone_v45.py --live

# Latihan tanpa kirim Telegram
python cryptone_v45.py --dry-run --log-level DEBUG

# Backtest 60 hari, tambahkan symbol pilihan sebagai kandidat (tetap kena filter)
python cryptone_v45.py --bootstrap --days 60 --pin SOL,HYPE

# Cek env & koneksi sebelum deploy
python cryptone_v45.py --check

# Test satu section
python cryptone_v45.py --test --section 6
```

Simbol pada contoh di atas hanya ilustrasi sintaks.

## XVII.9 Parameter

| Parameter | Kategori | Nilai |
|-----------|----------|-------|
| Jumlah mode | A | 6, mutually exclusive |
| Default `--days` | D | 60 |
| Default `--max-runtime` | A | 19800 detik (sinkron §XIII.1) |
| Default `--cycle-interval` | A | 300 detik (sinkron §II.2) |
| Prioritas konfigurasi | A | CLI > env > DB > default |

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| `--pin` menggantikan `--symbols` | Bagian II §II.8, Bagian XIII env | Universe dinamis |
| `--max-runtime 19800` | Bagian XIII §XIII.1 workflow | Sinkron timeout GH Actions |
| `--dry-run` | Bagian XII §XII.9 (delivery queue) | Queue di-stub saat dry-run |
| Exit code | Bagian XIII §XIII.1 workflow | Bedakan timeout normal vs error |
| `--recover` | Bagian XIII §XIII.10 | Jalankan recovery terpisah |
| `--test --section` | Bagian XIV §XIV.9 | Runner test |

---

# BAGIAN XVIII — ERROR HANDLING & RECOVERY (17_ERROR_HANDLING.md)

Status: FINAL

## XVIII.1 Prinsip

Melengkapi Prinsip #4 (**Fail-soft, tapi alert**). Bagian ini berisi skenario yang **pasti** terjadi pada arsitektur ini (SQLite lewat artifact, GitHub Actions restart, free-tier API). Skenario spekulatif sengaja tidak ditulis; tambahkan berdasarkan error nyata setelah 2-3 minggu live (lihat §XVIII.9).

Tiga kelas kegagalan, tiga perlakuan:

| Kelas | Contoh | Perlakuan |
|-------|--------|-----------|
| **Transient** | Timeout, rate limit sesaat | Retry + backoff, tanpa alert |
| **Degraded** | Satu source mati berjam-jam | Fallback + log, alert kalau melewati ambang |
| **Fatal** | DB korup, semua source mati | Berhenti aman + alert operator |

## XVIII.2 Matriks Skenario

| # | Skenario | Kelas | Deteksi | Tindakan otomatis | Alert |
|---|----------|-------|---------|-------------------|-------|
| 1 | Satu source non-critical mati | Degraded | `data_source_health` gagal ≥ 3x beruntun | Layer terkait jalan fail-soft (§I.3) | Tidak (log) |
| 2 | ≥ 2 source **critical** mati bersamaan | Degraded | Health check | Lanjut dengan cache; tahan sinyal baru kalau L3 tak bisa jalan | Ya (🚨) |
| 3 | Gemini rate limit / down > 1 jam | Degraded | Retry habis (§XI.10) | Fallback rule-based (§XI.9) | Ya, sekali per kejadian |
| 4 | Telegram API gagal | Degraded | Send error | Retry queue eksponensial; simpan pesan | Tidak (log) |
| 5 | Bot token Telegram dicabut | Fatal (delivery) | 401/403 berulang | Lanjut analisis, tulis sinyal ke tabel `pending_delivery` di DB (bukan file log — file log GH Actions hilang saat run selesai). Ada retry periodik begitu token valid terpasang lagi (detail retry mengikuti pola backoff §XVIII.3). | Tandai di `data_source_health` |
| 6 | Memory Engine gagal tulis | Fatal | Exception di flush | Lihat §XVIII.4 | Ya (🚨) |
| 7 | DB korup saat startup | Fatal | Integrity check | Lihat §XVIII.5 | Ya (🚨) |
| 8 | GH Actions timeout di tengah cycle | Transient | Restart run berikutnya | Lihat §XVIII.6 | Tidak |
| 9 | Artifact DB gagal di-download | Degraded | Step download gagal | Mulai DB baru + bootstrap ringan | Ya |
| 10 | Artifact DB gagal di-upload | Degraded | Step upload gagal | State run ini hilang; run berikut mulai dari artifact lama | Ya |
| 11 | WS Binance terputus | Transient | Heartbeat hilang | Reconnect eksponensial; REST fallback untuk cascade | Tidak |
| 12 | HL REST mengembalikan data kosong/anomali | Transient | Validasi respons | Buang cycle ini, pakai cache Layer 0 | Tidak |

## XVIII.3 Retry & Backoff (Standar)

Satu kebijakan retry dipakai di semua panggilan eksternal supaya konsisten:

```python
RETRY_POLICY = {
    "max_attempts": 3,
    "base_delay_s": 1.0,
    "factor": 2.0,          # 1s, 2s, 4s
    "jitter": 0.25,         # ±25% agar tidak serentak
    "retry_on": (Timeout, ConnectionError, RateLimitError, ServerError5xx),
    "never_retry_on": (AuthError, BadRequest4xx),   # gagal cepat
}
```

Circuit breaker per source: setelah 5 kegagalan beruntun, source ditandai `OPEN` selama 5 menit (nggak dipanggil), lalu 1 percobaan `HALF_OPEN`. Ini mencegah cycle 5 menit terhenti menunggu source yang jelas mati.

| Parameter | Kategori | Nilai |
|-----------|----------|-------|
| Max attempts | A | 3 |
| Base delay / factor | A | 1s / 2x |
| Circuit breaker threshold | A | 5 gagal beruntun |
| Circuit open duration | C | 2 - 15 menit (default D: 5) |

## XVIII.4 Memory Engine Gagal Tulis

Kondisi: `flush_writes()` melempar error (disk penuh, lock, I/O).

Tindakan:
1. Rollback transaksi batch saat ini.
2. Simpan batch yang gagal ke buffer in-memory (maks 1000 item), coba flush lagi di cycle berikutnya.
3. Kalau gagal 3 cycle beruntun → **stop sinyal baru** (jangan hasilkan sinyal yang nggak bisa dicatat), alert operator, lanjut monitoring sinyal aktif hanya-baca.
4. Jangan pernah membuang batch diam-diam.

Alasan langkah 3: sinyal yang tidak tercatat tidak bisa di-resolve nanti (§XVI), jadi statistik dan trust score rusak. Lebih aman berhenti menghasilkan sinyal daripada menghasilkan yang tak terlacak.

## XVIII.5 DB Korup Saat Startup

```python
async def startup_db_check(db_path):
    ok = run_integrity_check(db_path)          # PRAGMA integrity_check
    if ok:
        return "OK"
    # 1. Coba artifact sebelumnya (retention 7 hari)
    if restore_from_previous_artifact():
        return "RESTORED"
    # 2. Mulai DB kosong + bootstrap ringan
    create_fresh_db()
    await run_bootstrap(days=14)               # cukup untuk baseline percentile
    return "REBUILT"
```

| Hasil | Konsekuensi | Alert |
|-------|-------------|-------|
| OK | Normal | Tidak |
| RESTORED | Kehilangan data sejak artifact itu | Ya |
| REBUILT | Statistik/trust score reset; sinyal aktif hilang | Ya, + tandai sinyal terputus |

Selama REBUILT, parameter Kategori B jatuh ke default Kategori D (§II.3) sampai sample cukup. Ini sudah didesain, bukan perlakuan khusus.

## XVIII.6 GH Actions Timeout di Tengah Cycle

Ini bukan error; ini **mode operasi normal** (bot dibunuh tiap ~5.5 jam). Pastikan restart aman:

| Yang harus konsisten saat dibunuh | Mekanisme |
|-----------------------------------|-----------|
| Sinyal aktif | Ada di `active_signals` + `signal_history` (ditulis atomik saat sinyal dibuat) |
| Status `tp1_hit` | Disimpan di DB (§XVI.8), bukan RAM |
| Jendela candle yang belum dicek | `last_checked_at` per sinyal; dicek ulang saat startup |
| Write queue yang belum di-flush | Flush saat SIGTERM (graceful) bila sempat; sisanya hilang (ditoleransi) |
| Sinyal ACTIVE orphan (crash sebelum resolve) | `startup_recovery` (§XIII.10) menutupnya |

Handler sinyal:

```python
def install_shutdown_handler(memory, queue):
    def _graceful(signum, frame):
        # Beri waktu ≤ 20 detik untuk flush, lalu keluar dengan kode 0
        asyncio.get_event_loop().create_task(flush_and_exit(memory, queue, timeout=20))
    signal.signal(signal.SIGTERM, _graceful)
```

Data yang hilang maksimal = satu cycle (5 menit) write queue. Itu batas toleransi yang diterima (Kategori A).

## XVIII.7 Alert Operator (Anti-Spam)

Aturan supaya alert tetap berarti:

| Aturan | Nilai |
|--------|-------|
| Dedup: alert identik dalam jendela | 60 menit |
| Alert kelas Fatal | Selalu dikirim, tanpa dedup pertama kali |
| Ringkasan degraded | Digabung ke daily recap 07:00 WIB kalau bukan kritis |
| Recovery | Kirim "✅ pulih" sekali saat kondisi kembali normal |

Format:

```
🚨 [FATAL] Memory Engine gagal tulis 3 cycle
━━━━━━━━━━━━━━━━━━━
Tindakan: sinyal baru DIHENTIKAN
Monitoring sinyal aktif: lanjut (read-only)
Buffer tertahan: 214 item
Butuh: cek disk / artifact
```

## XVIII.8 Parameter

| Parameter | Kategori | Nilai |
|-----------|----------|-------|
| Retry max attempts | A | 3 |
| Circuit breaker | A | 5 gagal → open |
| Gagal flush sebelum stop sinyal | A | 3 cycle |
| Buffer tulis maks | A | 1000 item |
| Grace flush SIGTERM | A | 20 detik |
| Dedup alert | C | 30 - 120 menit (default D: 60) |
| Rebuild bootstrap saat DB korup | D | 14 hari |

## XVIII.9 Cara Menambah Skenario Baru

Playbook ini sengaja singkat. Setelah 2-3 minggu live:

1. Tarik error nyata dari `data_source_health`, `llm_call_log`, dan log run GH Actions.
2. Kalau error muncul ≥ 3 kali dan belum ada di matriks §XVIII.2, tambahkan baris (kelas, deteksi, tindakan, alert).
3. Jangan menulis skenario yang belum pernah terjadi dan tidak ada di daftar "pasti terjadi" di atas.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|-------------------------|--------|
| 3 kelas kegagalan | Bagian I §I.3 (fail-soft per layer) | Klasifikasi konsisten |
| Retry policy standar | Bagian XI §XI.10 (Gemini retry) | Satu kebijakan untuk semua panggilan |
| Stop sinyal baru saat DB gagal | Bagian XVI (lifecycle) | Sinyal tak tercatat tak bisa di-resolve |
| DB korup → bootstrap 14 hari | Bagian XIII §XIII.3 | Bootstrap parsial |
| Graceful SIGTERM | Bagian XIII §XIII.1 workflow | Timeout adalah mode normal |
| Alert anti-spam | Bagian XIII §XIII.7 | Aturan alert operator |
| Exit code | Bagian XVII §XVII.7 | Bedakan timeout normal vs error |

---

# PENUTUP

**Status: Design LOCKED. Siap implementasi.**

Filosofi akhir:

Radar, bukan executor.
Math first, LLM second.
Parameter framework 4 kategori.
Fail-soft, tapi alert.
Beda peran, beda layer.
Context ≠ Vote.
Zero fee, no compromise.

Bot = organisme hidup. Nggak ada final. Selalu dirawat.

**Source of Truth V4.5 selesai. Sekarang eksekusi. 🚀**

---

# LOG PATCH (sudah diterapkan)

## Putaran 1
| # | Masalah | Perbaikan |
|---|---------|-----------|
| A1/A2 | Bagian 01 & XII placeholder | Diisi |
| 1 | Jumlah source 13 vs 14 | Diseragamkan ke 14 |
| 2 | `p20_volume` tidak di schema VI | Ditambah; duplikat di §IX.6 dihapus |
| 4 | Pseudo-code `require` §IX.4 | Jadi komentar Python |
| 5 | Pause rule seragam | Per-setup (target − 5 poin) |
| 6 | Timeline pra/pasca-live | Diberi label |
| 7 | Cron 5 jam vs job 5,5 jam | Jadi tiap 6 jam |
| 8 | `main.py` vs `cryptone_v45.py` | Diseragamkan |

## Putaran 2
| # | Masalah | Perbaikan |
|---|---------|-----------|
| H1 | `CRYPTONE_SYMBOLS=BTC,ETH,SOL` di workflow & env bertentangan dengan universe dinamis | Diganti `CRYPTONE_PINNED_SYMBOLS` (opsional, tetap kena filter Layer 0) |
| H2 | Black swan resume hardcode `"BTC"` (§VII.4) | Pakai `get_market_anchor()` dinamis (§II.8) |
| H3 | Correlation modifier hardcode `("BTC", symbol)` (§VIII.3) | Pakai parameter `anchor` dinamis |
| H4 | Tabel cap sebut "BTC/ETH/SOL" (§IX.2) | Diganti definisi rank volume |
| H5 | Contoh output Telegram terlihat seperti daftar tetap | Ditambah disclaimer + Aturan #7 |
| C2 | Signal lifecycle belum ada | Bagian XVI. Draft awal dikoreksi (lihat di bawah) |
| C1 | CLI belum ada | Bagian XVII. `--symbols` diganti `--pin` |
| C3 | Error handling belum ada | Bagian XVIII (hanya skenario yang pasti terjadi) |

### Koreksi terhadap draft C2 awal
1. Buffer hit **0.5% tetap** diganti **toleransi berbasis ATR** (0.5% > jarak SL scalping, SL nggak akan pernah kena).
2. Deteksi hit **snapshot harga** diganti **high/low candle 1m** (wick antar cycle nggak terlewat).
3. Flag `tp1_hit` **RAM** diganti **kolom DB** (bot restart tiap ~6 jam).
4. PARTIAL dihitung **bobot 0.5**, bukan win penuh (mencegah win rate menggelembung); NEUTRAL dikeluarkan dari penyebut.
5. TP1 hit lalu kena SL = **PARTIAL**, bukan LOSS.
6. Ditambah kolom `partials`/`neutrals` yang sebelumnya tidak ada di tabel performa.

# BAGIAN XIX — CONTRACT: DATACLASS & EXCEPTION TERPUSAT (18_CONTRACT.md)

Status: FINAL

## XIX.1 Kenapa Bagian Ini Ada

Sebelumnya signature kontrak `.py` tersebar di banyak bagian (§XIII.3, §XVI.6, §VII.3, §XI.10) tanpa satu tempat yang mendaftar semuanya. `check_orphans.py` (lampiran §XIII.3 — lihat §XIX.4) menemukan beberapa nama dipakai tanpa pernah didefinisikan di doc manapun (`TelegramMessage`, `DataUnavailable`, `RateLimitError`, `TelegramRateLimitError`). Bagian ini adalah satu sumber kebenaran untuk semua dataclass dan exception yang jadi kontrak antara dokumen ini dan implementasi `.py`.

## XIX.2 Dataclasses

```python
@dataclass
class TelegramMessage:
    """Payload satu pesan Telegram, dipakai TelegramDeliveryQueue.enqueue()
    (§XII.9). Dibentuk oleh caller mana pun yang mau kirim pesan
    (§VII.3 Black Swan alert, §XII signal delivery, dll)."""
    chat_id: str
    text: str
    priority: str  # "critical" | "normal" | "low" -- menentukan urutan di queue
    parse_mode: str = "Markdown"
    chart_bytes: bytes | None = None  # opsional, lampiran chart (§XII.10)
```

Dataclass lain yang sudah didefinisikan di bagian masing-masing (referensi, bukan didefinisikan ulang di sini): `AnalystReading` (§III, Analyst Agent), signal record fields (§VI.3 `signal_history`, §XVI.8).

## XIX.3 Custom Exceptions

```python
class DataUnavailable(Exception):
    """Source data tidak bisa diambil (timeout, rate limit habis, response
    invalid) setelah retry wajar. Ditangkap di level cycle (§XVI.5) untuk
    fail-soft: memory.record_skip(...) lalu lanjut ke symbol berikutnya,
    bukan crash seluruh cycle."""

class RateLimitError(Exception):
    """Rate limit provider (LLM/exchange) tercapai. Beda dari
    DataUnavailable: retry_after biasanya diketahui, jadi backoff
    eksplisit (lihat call_gemini, §XI.10) alih-alih skip langsung."""
    retry_after: float = 1.0

class TelegramRateLimitError(RateLimitError):
    """Rate limit spesifik Telegram Bot API (429). Dipakai di
    TelegramDeliveryQueue.run_sender_loop (§XII.9) untuk re-queue pesan
    dengan delay retry_after, bukan drop."""

class LLMError(Exception):
    """Kegagalan LLM call setelah retry habis (§XI.10) -- termasuk
    Gemini gagal merespons valid setelah max_retries. Dipakai alih-alih
    Exception generik supaya penanganan di level atas bisa membedakan
    error LLM dari error lain."""
```

Semua exception custom di atas adalah `Exception` biasa (bukan builtin) — jangan `raise Exception(...)` generik di kode manapun; selalu pakai salah satu di atas atau tambahkan exception baru ke bagian ini kalau memang perlu jenis baru.

## XIX.4 Entry Points

| Fungsi | Dipanggil dari | Detail |
|--------|------------------|--------|
| `main()` | CLI / GH Actions workflow | §XVII (CLI Interface) |
| `run_live_service()` | `main()` | §XIII.3 |
| `run_bootstrap()` | `main()`, sekali per instalasi baru | §XIII |
| `build_parser()` | `main()` | §XVII.4 |

## XIX.5 Method MemoryEngine Lintas-Bagian

Daftar lengkap ada di §VI (schema) dan §XVI.6 (signal lifecycle write path) — bagian ini cuma pointer supaya engineer baru tahu ke mana harus cari signature method `MemoryEngine` (`enqueue_write`, `get_active_signals`, `extend_signal_validity`, dll) alih-alih menduga-duga.

## XIX.6 Lampiran: check_orphans.py

Script verifikasi otomatis untuk bagian ini — cek semua nama yang dipakai di blok python dokumen tapi tidak pernah didefinisikan (top-level call, method call, exception class di `except`), plus `--check-dead-defs` (def tanpa pemanggil di doc) dan `--audit-whitelist` (entry whitelist yang tidak terpakai). Ini menutup dead link yang sebelumnya direferensi di §XIII.3 tanpa file aktual di master.

### Riwayat Cross-Check

| Perubahan sumber | Titik yang ikut berubah | Kenapa |
|------------------|--------------------------|--------|
| TelegramMessage, custom exceptions | §VII.3, §XI.10, §XII.9, §XVI.6 (semua pemanggil) | Kontrak terpusat, jangan drift |
| Entry points | §XVII (CLI), §XIII (Deploy) | Titik masuk proses |

---


# GAP YANG MASIH TERBUKA

| # | Gap | Prioritas |
|---|-----|-----------|
| C4 | Multi-user support (saat ini implied single-user; `/mode`, `/schedule`, filter tier masih global) | Nanti |
| C5 | Monitoring/observability (health endpoint, export CSV/JSON) | Nanti |
| — | Skenario error nyata di §XVIII | Isi setelah 2-3 minggu live |
