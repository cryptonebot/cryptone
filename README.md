# HL Terminal Bot v4.0

Bot Telegram untuk analisis dan sinyal trading Hyperliquid perpetual futures.

## Fitur Utama
- SMC (Smart Money Concept) multi-timeframe
- Entry signal dengan SL/TP dinamis
- Squeeze detection (long/short squeeze)
- Warroom analysis
- Wallet tracker & copytrade
- Auto-sniper mode (AGGRO/INSANE)
- Learning engine (Bandit UCB1)

## Quick Start dengan Docker

```bash
# 1. Edit .env, isi TOKEN bot Telegram kamu
nano .env

# 2. Build & run
docker-compose up -d

# 3. Cek log
docker-compose logs -f

# 4. Stop bot
docker-compose down 
