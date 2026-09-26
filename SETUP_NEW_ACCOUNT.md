# Cryptone V4.5 — Setup Akun GitHub Baru

## 1. Buat repo

1. Login akun **baru** (khusus Cryptone)
2. New repository → nama bebas (contoh: `Cryptone-v45`)
3. **Private** recommended (secrets + minutes)
4. Jangan centang README/license (kita push full)

## 2. Push kode ini

Dari folder `cryptone_v45/` (isi repo):

```bash
git init
git add .
git commit -m "Cryptone V4.5 — initial (Fase A)"
git branch -M main
git remote add origin https://github.com/<USER_BARU>/<REPO>.git
git push -u origin main
```

Atau upload via GitHub web: file-file di folder ini.

### Isi wajib di root repo

```
cryptone_v45.py
cryptone_v45_test.py
requirements.txt
.env.example
.gitignore
README.md
SETUP_NEW_ACCOUNT.md
data/.gitkeep
.github/workflows/live.yml
.github/workflows/bootstrap.yml
.github/workflows/keepalive.yml
docs/CRYPTONE_V45_MASTER.md   # opsional tapi recommended
check_orphans.py              # opsional
```

## 3. Secrets (Settings → Secrets and variables → Actions)

| Secret name | Wajib? | Sumber |
|-------------|--------|--------|
| `GEMINI_API_KEY` | YA | Google AI Studio |
| `CRYPTONE_TELEGRAM_BOT_TOKEN` | YA | @BotFather |
| `CRYPTONE_OPERATOR_CHAT_ID` | YA | ID chat operator |
| `CRYPTONE_USER_CHAT_ID` | YA | ID chat user/channel |
| `FRED_API_KEY` | recommended | fred.stlouisfed.org |
| `ETHERSCAN_API_KEY` | optional | etherscan.io |
| `CRYPTONE_EXCHANGE_WALLETS` | optional | JSON wallet map |

Nama secret **harus exact** (case-sensitive) — sama dengan di `live.yml`.

## 4. Actions permissions

Repo → **Settings → Actions → General**:

- Actions permissions: **Allow all actions**
- Workflow permissions: **Read and write** (supaya artifact upload jalan)
- Save

## 5. Urutan run pertama

1. **Actions → Cryptone Bootstrap → Run workflow**  
   - days = `30`  
   - Tunggu hijau + Upload DB success  
   - Log: `meta ... fund=... atr=...`

2. **Actions → Cryptone Live → Run workflow** (sekali dulu, jangan spam)  
   - Banner: HL ✅ · FRED ✅ · Fear&Greed ✅ · Telegram ✅  
   - `📥 bootstrap · 30d · fund=...`  
   - CYCLE 1 + max 3 sinyal / cycle

3. Biarin cron Live (`0 */6 * * *`) jalan sendiri.

## 6. Hemat minutes (penting di free tier)

| Setting | Default | Saran testing |
|---------|---------|----------------|
| Live max runtime | 5.5 jam (19800s) | Test: turunin ke 900–1800 di yml dulu |
| Cycle | 300s | biarin |
| Jangan | parallel Live + Bootstrap | 1 job at a time |

Production boleh 5.5 jam lagi setelah stabil.

## 7. Akun lama

- Disable semua workflow di akun lama  
- Jangan biarin schedule masih nyala (nyedot minutes / payment flag)

## 8. Verifikasi sehat

Log Live yang bagus:

```
📡 SOURCES · Fear&Greed · 71 Greed
📥 bootstrap · 30d · fund=xxxxx
startup_recovery: active=N · recent_30m=M
📊 cycle · cand=.. · fund+/−/0=.. · sig=N (L?/S?) · max 3
```

Telegram dapat sinyal + CLOSE (TP/SL) otomatis.
