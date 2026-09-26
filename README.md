# Cryptone V4.5 — Market Screener Radar + Trader Assistant

**Fase A single-file · production-ready core**

Setup akun baru / migrasi → baca **[SETUP_NEW_ACCOUNT.md](SETUP_NEW_ACCOUNT.md)** dulu.

---

## Filosofi

- Radar, bukan executor
- Math first, LLM second
- Parameter framework 4 kategori (A/B/C/D)
- Fail-soft, tapi alert
- Beda peran, beda layer
- Context ≠ Vote
- Zero fee, no compromise

## Source of Truth

Semua spesifikasi ada di satu file:

```
docs/CRYPTONE_V45_MASTER.md
```

Baca urutan minimum (≈2 jam):

```
00 (README) → 01 (Produk) → I (Arsitektur 8 Layer) → II (Parameter)
  → XVI (Signal Lifecycle) → XVIII (Error Handling) → XIX (Contract)
```

## Struktur Fase A (Single File)

```
cryptone_v45/
├── cryptone_v45.py              # SEMUA logic (~7k baris target)
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
├── cryptone_v45_test.py         # Test suite
├── check_orphans.py             # Verifikasi kontrak doc ↔ kode
├── requirements.txt
├── .env.example
├── .github/workflows/
│   ├── live.yml                 # Job 5.5 jam, cron tiap 6 jam
│   └── keepalive.yml
├── docs/
│   └── CRYPTONE_V45_MASTER.md
└── data/
    └── cryptone_v45.db          # runtime (gitignored)
```

Pecah ke modular **hanya** setelah semua fitur P0+P1 selesai + stabil minimal 30 hari (Aturan #6).

## Quick Start

```bash
# 1. Clone & deps
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Env
cp .env.example .env
# isi GEMINI_API_KEY (wajib untuk bootstrap/dry-run)
# isi Telegram secrets (wajib untuk --live)

# 3. Preflight
python cryptone_v45.py --check

# 4. Bootstrap 60 hari (sekali)
python cryptone_v45.py --bootstrap --days 60

# 5. Dry-run (tanpa push Telegram)
python cryptone_v45.py --dry-run --log-level DEBUG

# 6. Live
python cryptone_v45.py --live --max-runtime 19800
```

## CLI Modes (mutually exclusive)

| Flag | Fungsi |
|------|--------|
| `--live` | Main loop produksi |
| `--bootstrap` | Backtest historis & isi DB |
| `--dry-run` | Live tanpa Telegram push |
| `--check` | Validasi env + koneksi source |
| `--recover` | Startup recovery saja |
| `--test` | Test suite |

Lihat `docs/CRYPTONE_V45_MASTER.md` Bagian XVII untuk opsi lengkap.

## Orphan Check

Sebelum commit:

```bash
python check_orphans.py docs/CRYPTONE_V45_MASTER.md
# exit 0 = bersih
```

## Roadmap

| Fase | Deliverable | Estimasi |
|------|-------------|----------|
| Minggu 1 | §1–4: Constants + Framework + Dataclasses + MemoryEngine | 7 hari |
| Minggu 2 | §5–7: Data Sources + Agents + Pipeline | 7 hari |
| Minggu 3 | §8–11: BlackSwan + Correlation + LLM + Bootstrap | 7 hari |
| Minggu 4 | §12–15: Telegram UI + Delivery + main loop | 7 hari |
| Minggu 5 | Test suite lengkap + bug fix | 7 hari |
| Minggu 6 | Bootstrap 60 hari + tuning | 7 hari |
| Minggu 7 | Deploy + Live + observasi | 7 hari |

Detail: Bagian XIV di master.

## License / Cost

Zero fee. Semua source gratis. Gemini free tier. GitHub Actions public repo. SQLite.
