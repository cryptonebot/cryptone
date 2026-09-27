#!/usr/bin/env python3
"""
Cryptone V4.5 — Market Screener Radar + Trader Assistant
Source of Truth: docs/CRYPTONE_V45_MASTER.md

Fase A: single file. Section markers §1–§15 sesuai §XIII.11 / §XIV.1.
Jangan pecah modular sebelum P0+P1 stabil ≥30 hari (Aturan #6).

Prinsip:
  Radar, bukan executor.
  Math first, LLM second.
  Parameter framework 4 kategori.
  Fail-soft, tapi alert.
  Beda peran, beda layer.
  Context ≠ Vote.
  Zero fee, no compromise.
"""

from __future__ import annotations

# =============================================================================
# §1  HEADER + CONSTANTS
# =============================================================================

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import sqlite3
import sys
import time
import uuid
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = "4.5.0"
SCHEMA_VERSION = 4

# WIB = UTC+7 (hanya untuk tampilan; semua TIMESTAMP di DB = UTC)
WIB = timezone(timedelta(hours=7))
UTC = timezone.utc

logger = logging.getLogger("cryptone")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def now_wib() -> datetime:
    return datetime.now(tz=WIB)


def session_allows(horizon: str, tier: str, now: datetime | None = None) -> tuple[bool, str]:
    """
    Gate jam likuid (London–NY overlap, WIB).
    - Di dalam session → semua tier OK
    - Di luar session → hanya Tier A (atau SWING)
    Fail-soft: SESSION_FILTER_ENABLED=False → always allow
    """
    if not SESSION_FILTER_ENABLED:
        return True, "session_off"
    # env override (tests / ops): CRYPTONE_SESSION_FILTER=0
    if os.environ.get("CRYPTONE_SESSION_FILTER", "1").strip() in ("0", "false", "False", "no"):
        return True, "session_env_off"
    t = now or now_wib()
    if t.tzinfo is None:
        t = t.replace(tzinfo=WIB)
    else:
        t = t.astimezone(WIB)
    h = t.hour
    in_session = SESSION_START_HOUR_WIB <= h < SESSION_END_HOUR_WIB
    if in_session:
        return True, f"session_ok_{h:02d}wib"
    if horizon == "SWING":
        return True, f"session_offhours_swing_{h:02d}wib"
    if tier == "A":
        return True, f"session_offhours_tierA_{h:02d}wib"
    return False, f"session_reject_{h:02d}wib"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def percentile(data: list[float], p: float) -> float:
    """Percentile 0–100. Empty → 0.0 (caller wajib handle cold-start)."""
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


# =============================================================================
# §2  PARAMETER FRAMEWORK (4 kategori A/B/C/D)
# =============================================================================
# Sumber kanonik: Bagian II. Nilai di sini = default kode.
# Override: CLI > env > runtime_settings (whitelist §II.6) > default.

# --- Kategori A — Framework Default (tunable terbatas via whitelist) ---
SIGNAL_LIFETIME_HOURS = {
    "SCALPING": 2,
    "INTRADAY": 24,
    "SWING": 168,  # 7 hari
}
TIME_STOP_HOURS = {
    "INTRADAY": 6,
    "SWING": 48,
    "SCALPING": 1,
}
# Session filter (WIB): overlap London open → NY afternoon
# Di luar jam ini: INTRADAY/SCALPING hanya Tier A; SWING bebas
SESSION_FILTER_ENABLED = True
SESSION_START_HOUR_WIB = 13   # 13:00 WIB ≈ London open
SESSION_END_HOUR_WIB = 21     # 21:00 WIB ≈ NY midday
MIN_RR = {
    "SCALPING": 1.5,
    "INTRADAY": 2.0,
    "SWING": 3.0,
}
MIN_CONFIRMATIONS = 2  # locked §II.7 — 2 dari 3
MAX_CONCURRENT_SIGNALS = 10
MAX_SIGNALS_PER_CYCLE = 3  # cap spam saat funding ekstrem berkepanjangan
MAX_CORRELATED_SIGNALS = 5
CORR_CLUSTER_THRESHOLD = 0.75
SIGNAL_COOLDOWN_MIN = 30
POST_LOSS_COOLDOWN_MIN = 240          # 4 jam penalty box setelah LOSS (§trader)
SETUP_LOSS_TRIGGER = 3                # circuit breaker per setup
SETUP_LOSS_PAUSE_H = 2
GLOBAL_LOSS_TRIGGER = 5               # circuit breaker global
GLOBAL_LOSS_PAUSE_H = 6
EVENT_BLACKOUT_MIN = 60
ENTRY_ZONE_ATR_WIDTH = 0.40           # max lebar zone = 0.4×ATR (bukan 1.0×)
TIER_A_MIN_RR = 2.5
TIER_A_MIN_CONFIRMS = 3
TIER_B_MIN_RR = 2.0
TIER_B_MIN_CONFIRMS = 2
BLACK_SWAN_TRIGGER_COUNT = 3  # locked — 3 dari 7
BLACK_SWAN_MIN_DURATION_H = 2
PARTIAL_WEIGHT = 0.5  # locked — bobot PARTIAL di win_rate
DEFAULT_CYCLE_INTERVAL_SEC = 300
DEFAULT_MAX_RUNTIME_SEC = 19800  # 5.5 jam

# --- Kategori B — Dynamic (per symbol, dari histori) ---
# Threshold dihitung runtime: Funding P95, Cascade P90, ATR P20, dll.
# Min sample = 30, lookback 30 hari. Cold-start → Kategori D.

# --- Kategori C — Semi-Dynamic (boundary + nilai tengah dinamis) ---
ENTRY_ZONE_ATR_MIN = 0.3
ENTRY_ZONE_ATR_MAX = 2.0

# --- Structure detector (SMC — §P1) ---
# Fractal swing: N bar kiri+kanan lebih rendah/tinggi dari pivot (5 = standar 2-2).
STRUCT_SWING_LOOKBACK = 2
STRUCT_MIN_CANDLES = 20            # minimal bar buat swing+BOS/CHoCH valid
STRUCT_TIMEFRAME = "15m"           # timeframe kerja structure detector
STRUCT_LOOKBACK_BARS = 96          # ~24 jam di 15m
STRUCT_SWEEP_WICK_MIN_ATR = 0.15   # wick minimal (dalam ATR) buat disebut sweep
STRUCT_SWEEP_CLOSE_BACK_MAX_ATR = 0.35  # close harus balik dalam X×ATR dari level

# --- Kategori D — Starting (cold-start) ---
HIT_TOLERANCE_ATR_DEFAULT = 0.05
CONFIDENCE_FLOOR_COLD = 0.5

# Whitelist tuning via Telegram (§II.6)
TUNABLE_WHITELIST: dict[str, dict[str, Any]] = {
    "funding_extreme_pct": {"min": 90, "max": 99, "presets": [90, 95, 97, 99], "default": 95},
    "cascade_size_pct": {"min": 85, "max": 99, "presets": [85, 90, 95], "default": 90},
    "min_rr_intraday": {"min": 1.5, "max": 3.0, "presets": [1.5, 2, 2.5, 3], "default": 2.0},
    "min_rr_swing": {"min": 2.0, "max": 4.0, "presets": [2, 3, 4], "default": 3.0},
    "signal_cooldown_min": {"min": 15, "max": 60, "presets": [15, 30, 60], "default": 30},
    "tier_a_count_max": {"min": 3, "max": 10, "presets": [3, 5, 10], "default": 5},
    "tier_b_count_max": {"min": 10, "max": 30, "presets": [10, 20, 30], "default": 20},
    "stop_hunt_volume_z": {"min": 1.5, "max": 3.0, "presets": [1.5, 2, 2.5], "default": 2.0},
    "stop_hunt_oi_z": {"min": 1.5, "max": 3.0, "presets": [1.5, 2, 2.5], "default": 2.0},
    "stop_hunt_price_change": {"min": 0.003, "max": 0.01, "presets": [0.003, 0.005, 0.008], "default": 0.005},
    "stop_hunt_xdiv": {"min": 0.003, "max": 0.008, "presets": [0.003, 0.005, 0.008], "default": 0.005},
}


# =============================================================================
# §3  DATACLASSES + EXCEPTIONS (kontrak §XIX)
# =============================================================================

class DataUnavailable(Exception):
    """Source data tidak bisa diambil setelah retry. Fail-soft di level cycle."""


class RateLimitError(Exception):
    """Rate limit provider. retry_after diketahui → backoff eksplisit."""
    def __init__(self, message: str = "", retry_after: float = 1.0):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramRateLimitError(RateLimitError):
    """Rate limit Telegram Bot API (429)."""


class LLMError(Exception):
    """Kegagalan LLM call setelah retry habis."""


@dataclass
class TelegramMessage:
    """Payload satu pesan Telegram (§XII.9, §XIX.2)."""
    chat_id: str
    text: str
    priority: str = "normal"  # critical | normal | low
    parse_mode: str | None = None  # plain by default (emoji-safe)
    chart_bytes: bytes | None = None
    reply_markup: dict | None = None  # InlineKeyboardMarkup JSON


# =============================================================================
# §4  MEMORY ENGINE (SQLite, single-writer)
# =============================================================================
# Schema kanonik: Bagian VI. Semua TIMESTAMP = UTC.

class MemoryEngine:
    """Single-writer SQLite engine. Semua write lewat queue (§VI.8)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._write_queue: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None

    def start(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()
        self._write_queue = asyncio.Queue()
        logger.info("MemoryEngine started: %s", self.db_path)

    def _apply_schema(self) -> None:
        assert self._conn is not None
        # --- Kelompok A: Time Series ---
        self._conn.executescript("""
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
        CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf_time
            ON metric_ohlcv(symbol, timeframe, bar_time);

        CREATE TABLE IF NOT EXISTS metric_funding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            rate REAL NOT NULL,
            recorded_at TIMESTAMP NOT NULL,
            is_simulated BOOLEAN DEFAULT 0,
            schema_version INTEGER DEFAULT 1,
            UNIQUE(symbol, recorded_at)
        );
        CREATE INDEX IF NOT EXISTS idx_funding_symbol_time
            ON metric_funding(symbol, recorded_at);

        CREATE TABLE IF NOT EXISTS metric_oi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            value REAL NOT NULL,
            recorded_at TIMESTAMP NOT NULL,
            is_simulated BOOLEAN DEFAULT 0,
            schema_version INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_oi_symbol_time
            ON metric_oi(symbol, recorded_at);

        CREATE TABLE IF NOT EXISTS metric_atr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            value REAL NOT NULL,
            recorded_at TIMESTAMP NOT NULL,
            is_simulated BOOLEAN DEFAULT 0,
            schema_version INTEGER DEFAULT 1,
            UNIQUE(symbol, timeframe, recorded_at)
        );
        CREATE INDEX IF NOT EXISTS idx_atr_symbol_tf_time
            ON metric_atr(symbol, timeframe, recorded_at);

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

        CREATE TABLE IF NOT EXISTS correlation_matrix (
            symbol_a TEXT NOT NULL,
            symbol_b TEXT NOT NULL,
            correlation REAL NOT NULL,
            lookback_hours INTEGER,
            updated_at TIMESTAMP NOT NULL,
            schema_version INTEGER DEFAULT 1,
            PRIMARY KEY (symbol_a, symbol_b, lookback_hours)
        );

        CREATE TABLE IF NOT EXISTS news_volume_5m (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_start TIMESTAMP NOT NULL,
            headline_count INTEGER NOT NULL,
            recorded_at TIMESTAMP NOT NULL,
            schema_version INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_news_volume_bucket
            ON news_volume_5m(bucket_start);
        """)
        # --- Kelompok B: Event Log ---
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS signal_history (
            signal_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            horizon TEXT NOT NULL,
            tier TEXT NOT NULL,
            entry_zone_low REAL,
            entry_zone_high REAL,
            stop_loss REAL,
            original_stop REAL,
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
            state TEXT NOT NULL DEFAULT 'PENDING',
            armed_at TIMESTAMP,
            resolved_at TIMESTAMP,
            is_simulated BOOLEAN DEFAULT 0,
            schema_version INTEGER DEFAULT 4
        );
        CREATE INDEX IF NOT EXISTS idx_signal_symbol_time
            ON signal_history(symbol, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_tier_time
            ON signal_history(tier, created_at DESC);

        CREATE TABLE IF NOT EXISTS liquidation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            exchange TEXT NOT NULL,
            recorded_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_liq_symbol_time
            ON liquidation_log(symbol, recorded_at DESC);

        CREATE TABLE IF NOT EXISTS blackswan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_indicators TEXT NOT NULL,
            trigger_count INTEGER,
            started_at TIMESTAMP NOT NULL,
            ended_at TIMESTAMP,
            was_resumed BOOLEAN
        );

        CREATE TABLE IF NOT EXISTS settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT,
            changed_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_settings_key_time
            ON settings_history(key, changed_at DESC);

        CREATE TABLE IF NOT EXISTS data_source_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            latency_ms REAL,
            error_type TEXT,
            recorded_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_source_health_name_time
            ON data_source_health(source_name, recorded_at DESC);
        """)
        # --- Kelompok C: Master Data ---
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS symbol_metadata (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            sector TEXT,
            narrative TEXT,
            quality_flag TEXT,
            news_relevance REAL,
            updated_at TIMESTAMP
        );

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

        CREATE TABLE IF NOT EXISTS setup_performance (
            setup_type TEXT PRIMARY KEY,
            total_signals INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            partials INTEGER DEFAULT 0,
            avg_rr REAL,
            avg_hold_hours REAL,
            last_updated TIMESTAMP
        );
        """)
        # --- Kelompok D: Mutable State ---
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS active_signals (
            signal_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_active_signal_expires
            ON active_signals(expires_at);

        CREATE TABLE IF NOT EXISTS runtime_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        );

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
        CREATE INDEX IF NOT EXISTS idx_wallet_tx_address_time
            ON wallet_txlog(address, recorded_at DESC);

        CREATE TABLE IF NOT EXISTS pending_delivery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP NOT NULL,
            schema_version INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_pending_delivery_time
            ON pending_delivery(created_at);

        CREATE TABLE IF NOT EXISTS bootstrap_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            bootstrapped_at TIMESTAMP,
            bootstrap_days INTEGER
        );
        """)
        # --- Kelompok E: LLM ---
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            function_name TEXT,
            created_at TIMESTAMP NOT NULL,
            expires_at TIMESTAMP NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_llm_cache_expires
            ON llm_cache(expires_at);

        CREATE TABLE IF NOT EXISTS llm_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            function_name TEXT NOT NULL,
            latency_ms REAL,
            success BOOLEAN,
            error_type TEXT,
            recorded_at TIMESTAMP NOT NULL
        );
        """)
        # Migrate pre-v4 databases without dropping signal history.
        cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(signal_history)").fetchall()
        }
        if "state" not in cols:
            self._conn.execute(
                "ALTER TABLE signal_history "
                "ADD COLUMN state TEXT NOT NULL DEFAULT 'PENDING'"
            )
        if "original_stop" not in cols:
            self._conn.execute(
                "ALTER TABLE signal_history ADD COLUMN original_stop REAL"
            )
        if "armed_at" not in cols:
            self._conn.execute(
                "ALTER TABLE signal_history ADD COLUMN armed_at TIMESTAMP"
            )
        # Repair legacy metric tables: remove historical duplicates first,
        # then enforce idempotency for future bootstrap/live writes.
        self._conn.execute(
            "DELETE FROM metric_funding WHERE id NOT IN "
            "(SELECT MIN(id) FROM metric_funding GROUP BY symbol, recorded_at)"
        )
        self._conn.execute(
            "DELETE FROM metric_atr WHERE id NOT IN "
            "(SELECT MIN(id) FROM metric_atr "
            "GROUP BY symbol, timeframe, recorded_at)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_funding_symbol_time "
            "ON metric_funding(symbol, recorded_at)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_atr_symbol_tf_time "
            "ON metric_atr(symbol, timeframe, recorded_at)"
        )
        self._conn.execute(
            "UPDATE signal_history SET schema_version = ? WHERE schema_version < ?",
            (SCHEMA_VERSION, SCHEMA_VERSION),
        )
        self._conn.commit()

    async def enqueue_write(self, table: str, params: dict) -> None:
        if self._write_queue is None:
            raise RuntimeError("MemoryEngine not started")
        await self._write_queue.put((table, params))

    async def flush_writes(self) -> None:
        """Batch commit pending writes. Dipanggil akhir cycle / SIGTERM."""
        if self._write_queue is None or self._conn is None:
            return
        batch: list[tuple[str, dict]] = []
        while not self._write_queue.empty():
            try:
                batch.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        for table, params in batch:
            self._dispatch_write(table, params)
        self._conn.commit()
        logger.debug("flushed %d writes", len(batch))

    def _dispatch_write(self, table: str, params: dict) -> None:
        """Single-writer dispatch. Tambah tabel di sini saat dipakai."""
        assert self._conn is not None
        if table == "data_source_health":
            self._conn.execute(
                "INSERT INTO data_source_health "
                "(source_name, success, latency_ms, error_type, recorded_at) "
                "VALUES (:source_name, :success, :latency_ms, :error_type, :recorded_at)",
                params,
            )
        elif table == "metric_funding":
            self._conn.execute(
                "INSERT OR IGNORE INTO metric_funding "
                "(symbol, rate, recorded_at, is_simulated, schema_version) "
                "VALUES (:symbol, :rate, :recorded_at, :is_simulated, 1)",
                {**params, "is_simulated": params.get("is_simulated", 0)},
            )
        elif table == "metric_atr":
            self._conn.execute(
                "INSERT OR IGNORE INTO metric_atr "
                "(symbol, timeframe, value, recorded_at, is_simulated, schema_version) "
                "VALUES (:symbol, :timeframe, :value, :recorded_at, :is_simulated, 1)",
                {**params, "is_simulated": params.get("is_simulated", 0)},
            )
        elif table == "liquidation_log":
            self._conn.execute(
                "INSERT INTO liquidation_log "
                "(symbol, side, amount_usd, exchange, recorded_at) "
                "VALUES (:symbol, :side, :amount_usd, :exchange, :recorded_at)",
                params,
            )
        elif table == "runtime_settings":
            self._conn.execute(
                "INSERT INTO runtime_settings (key, value, updated_at) "
                "VALUES (:key, :value, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                params,
            )
        elif table == "signal_history":
            # INSERT OR IGNORE — jangan REPLACE (wipe outcome/tp1_hit/resolved_at)
            self._conn.execute(
                "INSERT OR IGNORE INTO signal_history ("
                "signal_id, symbol, direction, horizon, tier, "
                "entry_zone_low, entry_zone_high, stop_loss, original_stop, "
                "tp1, tp2, rr, "
                "confidence, trigger_type, reasoning, setup_type, "
                "tp1_hit, last_checked_at, created_at, valid_until, "
                "state, armed_at, is_simulated, schema_version"
                ") VALUES ("
                ":signal_id, :symbol, :direction, :horizon, :tier, "
                ":entry_zone_low, :entry_zone_high, :stop_loss, :original_stop, "
                ":tp1, :tp2, :rr, "
                ":confidence, :trigger_type, :reasoning, :setup_type, "
                "0, :last_checked_at, :created_at, :valid_until, "
                ":state, :armed_at, :is_simulated, :schema_version)",
                {
                    **params,
                    "original_stop": params.get("original_stop"),
                    "armed_at": params.get("armed_at"),
                    "state": params.get("state", "PENDING"),
                    "schema_version": SCHEMA_VERSION,
                },
            )
        elif table == "active_signals":
            self._conn.execute(
                "INSERT OR REPLACE INTO active_signals (signal_id, symbol, expires_at) "
                "VALUES (:signal_id, :symbol, :expires_at)",
                params,
            )
        elif table == "news_volume_5m":
            self._conn.execute(
                "INSERT INTO news_volume_5m "
                "(bucket_start, headline_count, recorded_at, schema_version) "
                "VALUES (:bucket_start, :headline_count, :recorded_at, 1)",
                params,
            )
        elif table == "llm_call_log":
            self._conn.execute(
                "INSERT INTO llm_call_log "
                "(function_name, latency_ms, success, error_type, recorded_at) "
                "VALUES (:function_name, :latency_ms, :success, :error_type, :recorded_at)",
                params,
            )
        else:
            logger.warning("flush_writes: unknown table %s (skipped)", table)

    def get_funding_history(self, symbol: str, lookback_days: int = 30) -> list[float]:
        if not self._conn:
            return []
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT rate FROM metric_funding "
            "WHERE symbol = ? AND recorded_at >= ? ORDER BY recorded_at",
            (symbol, cutoff),
        ).fetchall()
        return [float(r["rate"]) for r in rows]

    def get_funding_near(
        self, symbol: str, target: datetime, tolerance_min: int = 90,
    ) -> float | None:
        """Rate closest to target time within ±tolerance (for funding_flip 1h)."""
        if not self._conn:
            return None
        lo = (target - timedelta(minutes=tolerance_min)).isoformat()
        hi = (target + timedelta(minutes=tolerance_min)).isoformat()
        rows = self._conn.execute(
            "SELECT rate, recorded_at FROM metric_funding "
            "WHERE symbol = ? AND recorded_at >= ? AND recorded_at <= ? "
            "ORDER BY recorded_at",
            (symbol, lo, hi),
        ).fetchall()
        if not rows:
            return None
        best = None
        best_dt = None
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r["recorded_at"]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            if best is None or abs((dt - target).total_seconds()) < abs((best_dt - target).total_seconds()):
                best = float(r["rate"])
                best_dt = dt
        return best

    def get_atr_history(self, symbol: str, timeframe: str = "1h", lookback_days: int = 30) -> list[float]:
        if not self._conn:
            return []
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT value FROM metric_atr "
            "WHERE symbol = ? AND timeframe = ? AND recorded_at >= ? "
            "ORDER BY recorded_at",
            (symbol, timeframe, cutoff),
        ).fetchall()
        return [float(r["value"]) for r in rows]

    def get_cascade_history(self, symbol: str, lookback_days: int = 30) -> list[float]:
        """
        Agregat USD cascade per bucket 5 menit (untuk P90).
        Bukan individual liquidation — threshold cascade = total 5m (§IV.2).
        """
        if not self._conn:
            return []
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT amount_usd, recorded_at FROM liquidation_log "
            "WHERE symbol = ? AND recorded_at >= ? ORDER BY recorded_at",
            (symbol, cutoff),
        ).fetchall()
        buckets: dict[str, float] = {}
        for r in rows:
            try:
                dt = datetime.fromisoformat(str(r["recorded_at"]).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            # floor ke 5 menit
            minute = (dt.minute // 5) * 5
            key = dt.replace(minute=minute, second=0, microsecond=0).isoformat()
            buckets[key] = buckets.get(key, 0.0) + float(r["amount_usd"] or 0)
        return list(buckets.values())

    def set_runtime(self, key: str, value: str) -> None:
        if not self._conn:
            return
        self._conn.execute(
            "INSERT INTO runtime_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now_utc().isoformat()),
        )
        self._conn.commit()

    def get_runtime(self, key: str, default: str | None = None) -> str | None:
        if not self._conn:
            return default
        row = self._conn.execute(
            "SELECT value FROM runtime_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def count_active_signals(self) -> int:
        if not self._conn:
            return 0
        row = self._conn.execute("SELECT COUNT(*) AS n FROM active_signals").fetchone()
        return int(row["n"]) if row else 0

    def count_active_in_corr_cluster(
        self,
        corr_map: dict[str, float],
        threshold: float,
    ) -> int:
        """Count active signals whose symbols are in the correlation cluster."""
        if not self._conn or not corr_map:
            return 0
        cluster_symbols = {
            symbol
            for symbol, correlation in corr_map.items()
            if abs(float(correlation)) >= threshold
        }
        if not cluster_symbols:
            return 0
        placeholders = ",".join("?" for _ in cluster_symbols)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM active_signals "
            f"WHERE symbol IN ({placeholders})",
            tuple(cluster_symbols),
        ).fetchone()
        return int(row["n"]) if row else 0

    def has_active_symbol(self, symbol: str) -> bool:
        """True kalau symbol masih punya sinyal aktif (belum expired)."""
        if not self._conn:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM active_signals WHERE symbol = ? LIMIT 1", (symbol,)
        ).fetchone()
        return row is not None

    def get_bootstrap_info(self) -> dict[str, Any] | None:
        """Baca bootstrap_meta — None kalau belum pernah bootstrap."""
        if not self._conn:
            return None
        n_fund = int(
            self._conn.execute("SELECT COUNT(*) AS n FROM metric_funding").fetchone()["n"]
        )
        n_atr = int(
            self._conn.execute("SELECT COUNT(*) AS n FROM metric_atr").fetchone()["n"]
        )
        row = self._conn.execute(
            "SELECT bootstrapped_at, bootstrap_days FROM bootstrap_meta WHERE id = 1"
        ).fetchone()
        # Repair: ada sample tapi meta hilang (artifact partial) → tulis meta
        if not row and (n_fund >= 500 or n_atr >= 100):
            self._conn.execute(
                "INSERT OR REPLACE INTO bootstrap_meta "
                "(id, bootstrapped_at, bootstrap_days) VALUES (1, ?, ?)",
                (now_utc().isoformat(), 30),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT bootstrapped_at, bootstrap_days FROM bootstrap_meta WHERE id = 1"
            ).fetchone()
            logger.info(
                "📥 bootstrap · repaired meta from samples fund=%d atr=%d",
                n_fund, n_atr,
            )
        if not row:
            return None
        return {
            "bootstrapped_at": row["bootstrapped_at"],
            "bootstrap_days": row["bootstrap_days"],
            "funding_samples": n_fund,
            "atr_samples": n_atr,
        }

    def symbol_on_cooldown(self, symbol: str, cooldown_min: int = SIGNAL_COOLDOWN_MIN) -> bool:
        """True kalau signal dibuat/ditutup < cooldown_min yang lalu."""
        if not self._conn:
            return False
        cutoff = (now_utc() - timedelta(minutes=cooldown_min)).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM signal_history "
            "WHERE symbol = ? AND COALESCE(resolved_at, created_at) >= ? LIMIT 1",
            (symbol, cutoff),
        ).fetchone()
        return row is not None

    def symbol_post_loss_blocked(self, symbol: str) -> bool:
        """
        True kalau symbol baru LOSS dalam POST_LOSS_COOLDOWN_MIN.
        Cegah revenge re-entry (TNSR/2Z loop di sample).
        """
        if not self._conn:
            return False
        cutoff = (now_utc() - timedelta(minutes=POST_LOSS_COOLDOWN_MIN)).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM signal_history "
            "WHERE symbol = ? AND outcome = 'LOSS' AND resolved_at >= ? "
            "LIMIT 1",
            (symbol, cutoff),
        ).fetchone()
        return row is not None

    def consecutive_losses(
        self,
        n: int = GLOBAL_LOSS_TRIGGER,
        setup_type: str | None = None,
    ) -> int:
        """Jumlah LOSS beruntun, opsional difilter ke satu setup."""
        if not self._conn:
            return 0
        if setup_type:
            rows = self._conn.execute(
                "SELECT outcome FROM signal_history "
                "WHERE outcome IS NOT NULL AND is_simulated = 0 "
                "AND setup_type = ? "
                "ORDER BY resolved_at DESC LIMIT ?",
                (setup_type, n),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT outcome FROM signal_history "
                "WHERE outcome IS NOT NULL AND is_simulated = 0 "
                "ORDER BY resolved_at DESC LIMIT ?",
                (n,),
            ).fetchall()
        c = 0
        for r in rows:
            if r["outcome"] == "LOSS":
                c += 1
            else:
                break
        return c

    def setup_signals_paused(self, setup_type: str | None) -> bool:
        """True while a setup-specific circuit-breaker pause is active."""
        if not self._conn or not setup_type:
            return False
        until = self.get_runtime(f"setup_paused_until:{setup_type}")
        if not until:
            return False
        try:
            t = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            return now_utc() < t
        except (ValueError, TypeError):
            return False

    def signals_paused(self) -> bool:
        until = self.get_runtime("signals_paused_until")
        if not until:
            return False
        try:
            t = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=UTC)
            return now_utc() < t
        except (ValueError, TypeError):
            return False

    def get_setup_enabled(self) -> dict[str, bool]:
        raw = self.get_runtime("setup_enabled_json")
        default = {
            "FUNDING_SQUEEZE": True,
            "CASCADE_SCALP": True,
            "COMPRESSION": True,
        }
        if not raw:
            return default
        try:
            data = json.loads(raw)
            return {**default, **{k: bool(v) for k, v in data.items()}}
        except (json.JSONDecodeError, TypeError):
            return default

    def mark_signal_expired(self, signal_id: str, outcome: str = "NEUTRAL") -> None:
        if not self._conn:
            return
        ts = now_utc().isoformat()
        self._conn.execute(
            "UPDATE signal_history SET outcome = ?, resolved_at = ? "
            "WHERE signal_id = ? AND outcome IS NULL",
            (outcome, ts, signal_id),
        )
        self._conn.execute(
            "DELETE FROM active_signals WHERE signal_id = ?", (signal_id,)
        )
        self._conn.commit()

    def touch_signal_check(self, signal_id: str, *, tp1_hit: bool | None = None) -> None:
        """Update last_checked_at (+ optional tp1_hit) tanpa resolve."""
        if not self._conn:
            return
        ts = now_utc().isoformat()
        if tp1_hit is True:
            self._conn.execute(
                "UPDATE signal_history SET last_checked_at = ?, tp1_hit = 1, "
                "tp1_hit_at = COALESCE(tp1_hit_at, ?) "
                "WHERE signal_id = ? AND outcome IS NULL",
                (ts, ts, signal_id),
            )
        else:
            self._conn.execute(
                "UPDATE signal_history SET last_checked_at = ? "
                "WHERE signal_id = ? AND outcome IS NULL",
                (ts, signal_id),
            )
        self._conn.commit()

    def arm_signal(self, signal_id: str) -> None:
        """Mark a pending signal ARMED after its entry zone has been reached."""
        if not self._conn:
            return
        ts = now_utc().isoformat()
        self._conn.execute(
            "UPDATE signal_history SET state = 'ARMED', last_checked_at = ?, "
            "armed_at = COALESCE(armed_at, ?) "
            "WHERE signal_id = ? AND outcome IS NULL",
            (ts, ts, signal_id),
        )
        self._conn.commit()

    def move_stop_to_breakeven(self, signal_id: str) -> float | None:
        """
        Move an active signal's stop to its entry-zone midpoint after TP1.
        The stop only moves in the protective direction:
        LONG uses max(old_stop, entry_mid), SHORT uses min(old_stop, entry_mid).
        """
        if not self._conn:
            return None
        row = self._conn.execute(
            "SELECT direction, entry_zone_low, entry_zone_high, stop_loss "
            "FROM signal_history WHERE signal_id = ? AND outcome IS NULL",
            (signal_id,),
        ).fetchone()
        if row is None:
            return None
        entry_mid = (
            float(row["entry_zone_low"]) + float(row["entry_zone_high"])
        ) / 2.0
        old_stop = float(row["stop_loss"])
        direction = str(row["direction"]).upper()
        if direction == "LONG":
            new_stop = max(old_stop, entry_mid)
        elif direction == "SHORT":
            new_stop = min(old_stop, entry_mid)
        else:
            new_stop = entry_mid
        self._conn.execute(
            "UPDATE signal_history SET stop_loss = ? "
            "WHERE signal_id = ? AND outcome IS NULL",
            (new_stop, signal_id),
        )
        self._conn.commit()
        return new_stop

    def resolve_signal(
        self,
        signal_id: str,
        outcome: str,
        *,
        realized_rr: float | None = None,
        tp1_hit: bool | None = None,
    ) -> None:
        """
        Tutup sinyal: outcome + realized_rr, hapus active_signals,
        update symbol_performance + setup_performance (§XVI.6).
        """
        if not self._conn:
            return
        ts = now_utc().isoformat()
        row = self._conn.execute(
            "SELECT symbol, setup_type, outcome, is_simulated FROM signal_history "
            "WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        if row is None:
            return
        if row["outcome"] is not None:
            # already resolved
            self._conn.execute(
                "DELETE FROM active_signals WHERE signal_id = ?", (signal_id,)
            )
            self._conn.commit()
            return
        if tp1_hit is True:
            self._conn.execute(
                "UPDATE signal_history SET outcome = ?, resolved_at = ?, "
                "realized_rr = ?, tp1_hit = 1, "
                "tp1_hit_at = COALESCE(tp1_hit_at, ?), last_checked_at = ? "
                "WHERE signal_id = ? AND outcome IS NULL",
                (outcome, ts, realized_rr, ts, ts, signal_id),
            )
        else:
            self._conn.execute(
                "UPDATE signal_history SET outcome = ?, resolved_at = ?, "
                "realized_rr = ?, last_checked_at = ? "
                "WHERE signal_id = ? AND outcome IS NULL",
                (outcome, ts, realized_rr, ts, signal_id),
            )
        self._conn.execute(
            "DELETE FROM active_signals WHERE signal_id = ?", (signal_id,)
        )
        # Stats: skip simulated so dry-run / test tidak racuni trust_score
        if not row["is_simulated"]:
            self._apply_performance_update(
                symbol=str(row["symbol"]),
                setup_type=str(row["setup_type"] or "UNKNOWN"),
                outcome=outcome,
                realized_rr=realized_rr,
                ts=ts,
            )
            # Circuit breaker dua level:
            # 1) 3 LOSS beruntun pada setup yang sama → pause setup 2 jam
            # 2) 5 LOSS beruntun lintas setup → pause semua setup 6 jam
            if outcome == "LOSS":
                setup_type = str(row["setup_type"] or "UNKNOWN")
                setup_losses = self.consecutive_losses(
                    SETUP_LOSS_TRIGGER,
                    setup_type=setup_type,
                )
                if setup_losses >= SETUP_LOSS_TRIGGER:
                    until = (
                        now_utc() + timedelta(hours=SETUP_LOSS_PAUSE_H)
                    ).isoformat()
                    self.set_runtime(
                        f"setup_paused_until:{setup_type}", until,
                    )
                    logger.warning(
                        "⏸ setup-loss pause · %s · %d LOSS · %dh until %s",
                        setup_type, setup_losses, SETUP_LOSS_PAUSE_H, until[:19],
                    )
                global_losses = self.consecutive_losses(GLOBAL_LOSS_TRIGGER)
                if global_losses >= GLOBAL_LOSS_TRIGGER:
                    until = (
                        now_utc() + timedelta(hours=GLOBAL_LOSS_PAUSE_H)
                    ).isoformat()
                    self.set_runtime("signals_paused_until", until)
                    logger.warning(
                        "⏸ global-loss pause · %d LOSS · %dh until %s",
                        global_losses, GLOBAL_LOSS_PAUSE_H, until[:19],
                    )
        self._conn.commit()

    def _apply_performance_update(
        self,
        symbol: str,
        setup_type: str,
        outcome: str,
        realized_rr: float | None,
        ts: str,
    ) -> None:
        """Increment symbol_performance + setup_performance (§XVI.6 / §IV.6)."""
        assert self._conn is not None
        # symbol
        self._conn.execute(
            "INSERT INTO symbol_performance "
            "(symbol, total_signals, wins, losses, partials, neutrals, avg_rr, "
            "trust_score, last_updated) "
            "VALUES (?, 0, 0, 0, 0, 0, 0, 0.5, ?) "
            "ON CONFLICT(symbol) DO NOTHING",
            (symbol, ts),
        )
        # setup
        self._conn.execute(
            "INSERT INTO setup_performance "
            "(setup_type, total_signals, wins, losses, partials, avg_rr, "
            "avg_hold_hours, last_updated) "
            "VALUES (?, 0, 0, 0, 0, 0, 0, ?) "
            "ON CONFLICT(setup_type) DO NOTHING",
            (setup_type, ts),
        )
        win = 1 if outcome == "PROFIT" else 0
        loss = 1 if outcome == "LOSS" else 0
        partial = 1 if outcome == "PARTIAL" else 0
        neutral = 1 if outcome == "NEUTRAL" else 0
        rr = float(realized_rr) if realized_rr is not None else 0.0

        self._conn.execute(
            """
            UPDATE symbol_performance SET
                total_signals = total_signals + 1,
                wins = wins + ?,
                losses = losses + ?,
                partials = partials + ?,
                neutrals = neutrals + ?,
                avg_rr = CASE
                    WHEN total_signals = 0 THEN ?
                    ELSE (avg_rr * total_signals + ?) / (total_signals + 1)
                END,
                trust_score = CASE
                    WHEN (total_signals + 1) <= 0 THEN 0.5
                    ELSE MIN(1.0, MAX(0.0,
                        (wins + ? + partials * 0.5 + ?)
                        / (total_signals + 1.0)
                    ))
                END,
                last_updated = ?
            WHERE symbol = ?
            """,
            (win, loss, partial, neutral, rr, rr, win, partial, ts, symbol),
        )
        # trust_score formula used wins/partials BEFORE increment in CASE — fix with post-read
        # Simpler recompute:
        sp = self._conn.execute(
            "SELECT total_signals, wins, losses, partials, neutrals, avg_rr "
            "FROM symbol_performance WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if sp:
            # Denominator = hanya trade yang benar-benar resolved win/loss.
            # NEUTRAL dikeluarkan sesuai §XVI.3.
            resolved = sp["wins"] + sp["losses"] + sp["partials"]
            if resolved > 0:
                tw = sp["wins"] + PARTIAL_WEIGHT * sp["partials"]
                trust = clamp(tw / resolved, 0.0, 1.0)
                self._conn.execute(
                    "UPDATE symbol_performance SET trust_score = ? WHERE symbol = ?",
                    (trust, symbol),
                )

        # setup_performance: hanya trade yang benar-benar resolusi arah.
        # NEUTRAL (SL/TP tidak tersentuh) bukan trade terbukti (§XVI.6),
        # jadi tidak boleh ikut menghitung total_signals di sini — kalau
        # ikut, setup legit dengan banyak NEUTRAL bisa ke-pause salah.
        if outcome != "NEUTRAL":
            self._conn.execute(
                """
                UPDATE setup_performance SET
                    total_signals = total_signals + 1,
                    wins = wins + ?,
                    losses = losses + ?,
                    partials = partials + ?,
                    avg_rr = CASE
                        WHEN total_signals = 0 THEN ?
                        ELSE (COALESCE(avg_rr, 0) * total_signals + ?) / (total_signals + 1)
                    END,
                    last_updated = ?
                WHERE setup_type = ?
                """,
                (win, loss, partial, rr, rr, ts, setup_type),
            )
            # Auto-pause setup if enough sample + low win rate (§IV.6)
            st = self._conn.execute(
                "SELECT total_signals, wins, partials FROM setup_performance "
                "WHERE setup_type = ?",
                (setup_type,),
            ).fetchone()
            if st and st["total_signals"] >= 100:
                wr = (st["wins"] + PARTIAL_WEIGHT * st["partials"]) / st["total_signals"]
                if wr < 0.40:
                    enabled = self.get_setup_enabled()
                    if enabled.get(setup_type, True):
                        enabled[setup_type] = False
                        self.set_runtime("setup_enabled_json", json.dumps(enabled))
                        logger.warning(
                            "⏸ setup auto-pause · %s · wr=%.2f · n=%d",
                            setup_type, wr, st["total_signals"],
                        )

    def extend_signal_validity(self, extra_hours: float = 6.0) -> int:
        """
        Black swan §VII.3 / §XVI.6 — extend valid_until + expires_at semua aktif.
        Return jumlah sinyal yang di-extend.
        """
        if not self._conn:
            return 0
        rows = self._conn.execute(
            "SELECT a.signal_id, a.expires_at, s.valid_until "
            "FROM active_signals a "
            "JOIN signal_history s ON s.signal_id = a.signal_id "
            "WHERE s.outcome IS NULL"
        ).fetchall()
        n = 0
        delta = timedelta(hours=extra_hours)
        for r in rows:
            for col, table, key in (
                (r["expires_at"], "active_signals", "expires_at"),
                (r["valid_until"], "signal_history", "valid_until"),
            ):
                exp = None
                try:
                    if col:
                        exp = datetime.fromisoformat(str(col).replace("Z", "+00:00"))
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    exp = now_utc()
                if exp is None:
                    exp = now_utc()
                new_exp = (exp + delta).isoformat()
                if table == "active_signals":
                    self._conn.execute(
                        "UPDATE active_signals SET expires_at = ? WHERE signal_id = ?",
                        (new_exp, r["signal_id"]),
                    )
                else:
                    self._conn.execute(
                        "UPDATE signal_history SET valid_until = ? "
                        "WHERE signal_id = ? AND outcome IS NULL",
                        (new_exp, r["signal_id"]),
                    )
            n += 1
        if n:
            self._conn.commit()
            logger.info("⏳ black swan · extend +%.0fh · %d active signals", extra_hours, n)
        return n

    def checkpoint_wal(self) -> None:
        if self._conn:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def get_active_signals(self) -> list[dict]:
        if not self._conn:
            return []
        rows = self._conn.execute(
            "SELECT a.signal_id, a.symbol, a.expires_at, s.* "
            "FROM active_signals a "
            "JOIN signal_history s ON s.signal_id = a.signal_id "
            "WHERE s.outcome IS NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_anchor(self) -> str | None:
        # Placeholder: simpan di runtime_settings key=last_anchor
        if not self._conn:
            return None
        row = self._conn.execute(
            "SELECT value FROM runtime_settings WHERE key = ?", ("last_anchor",)
        ).fetchone()
        return row["value"] if row else None

    def close(self) -> None:
        if self._conn:
            self.checkpoint_wal()
            self._conn.close()
            self._conn = None


# =============================================================================
# §5  DATA SOURCES
# =============================================================================
# 14 source, zero-fee. Lihat Bagian V.
# Rule: WS = event-based, REST = state-based.
# Critical: hyperliquid_rest, binance_liq_ws, etherscan (§V.6).

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"

# Retry policy §XVIII.3 (Kategori A)
RETRY_POLICY = {
    "max_attempts": 3,
    "base_delay_s": 1.0,
    "factor": 2.0,
    "jitter": 0.25,
}

# Circuit breaker state per source name
_circuit: dict[str, dict[str, Any]] = defaultdict(
    lambda: {"failures": 0, "open_until": 0.0}
)
CIRCUIT_THRESHOLD = 5          # Kategori A
CIRCUIT_OPEN_SEC = 300.0       # default D = 5 menit


@dataclass
class AssetCtx:
    """Satu baris dari metaAndAssetCtxs (universe + ctx sejajar index)."""
    symbol: str
    funding: float
    open_interest: float
    mark_px: float
    mid_px: float
    day_ntl_vlm: float          # notional volume 24h USD
    prev_day_px: float
    oracle_px: float = 0.0


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_time: datetime          # UTC


def _circuit_allow(source: str) -> bool:
    st = _circuit[source]
    if st["open_until"] and time.monotonic() < st["open_until"]:
        return False
    if st["open_until"] and time.monotonic() >= st["open_until"]:
        st["failures"] = 0
        st["open_until"] = 0.0
    return True


def _circuit_success(source: str) -> None:
    _circuit[source]["failures"] = 0
    _circuit[source]["open_until"] = 0.0


def _circuit_failure(source: str) -> None:
    st = _circuit[source]
    was_open = bool(st.get("open_until") and time.monotonic() < st["open_until"])
    st["failures"] += 1
    if st["failures"] >= CIRCUIT_THRESHOLD:
        st["open_until"] = time.monotonic() + CIRCUIT_OPEN_SEC
        if not was_open:
            logger.warning(
                "⚡ circuit OPEN · %s · %ss",
                source, int(CIRCUIT_OPEN_SEC),
            )


def _http_post_json(url: str, payload: dict, timeout: float = 15.0) -> Any:
    """Sync POST JSON. Dipanggil via to_thread."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "cryptone-v45"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "cryptone-v45"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def http_post_json(url: str, payload: dict, timeout: float = 15.0) -> Any:
    return await asyncio.to_thread(_http_post_json, url, payload, timeout)


async def http_get_json(url: str, timeout: float = 15.0) -> Any:
    return await asyncio.to_thread(_http_get_json, url, timeout)


async def with_retry(
    source: str,
    coro_factory: Callable[[], Any],
    memory: "MemoryEngine | None" = None,
) -> Any:
    """
    Retry + circuit breaker + health log (§XVIII.3, §V.6).
    coro_factory: zero-arg async callable (supaya tiap attempt fresh).
    """
    if not _circuit_allow(source):
        raise DataUnavailable(f"circuit open: {source}")

    last_err: Exception | None = None
    attempts = RETRY_POLICY["max_attempts"]
    base = RETRY_POLICY["base_delay_s"]
    factor = RETRY_POLICY["factor"]
    jitter = RETRY_POLICY["jitter"]

    for attempt in range(attempts):
        t0 = time.monotonic()
        try:
            result = await coro_factory()
            latency = (time.monotonic() - t0) * 1000
            _circuit_success(source)
            if memory is not None:
                await memory.enqueue_write("data_source_health", {
                    "source_name": source,
                    "success": True,
                    "latency_ms": latency,
                    "error_type": None,
                    "recorded_at": now_utc().isoformat(),
                })
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, json.JSONDecodeError, OSError) as e:
            last_err = e
            latency = (time.monotonic() - t0) * 1000
            _circuit_failure(source)
            if memory is not None:
                await memory.enqueue_write("data_source_health", {
                    "source_name": source,
                    "success": False,
                    "latency_ms": latency,
                    "error_type": type(e).__name__,
                    "recorded_at": now_utc().isoformat(),
                })
            if attempt + 1 >= attempts:
                break
            delay = base * (factor ** attempt)
            delay *= 1.0 + random.uniform(-jitter, jitter)
            await asyncio.sleep(max(0.1, delay))
        except Exception as e:
            # Auth / 4xx style — jangan retry
            last_err = e
            _circuit_failure(source)
            break

    raise DataUnavailable(f"{source} failed after retries: {last_err}")


# ----- Hyperliquid REST (critical, L0/L1/L4) -----

async def hl_meta_and_asset_ctxs(
    memory: "MemoryEngine | None" = None,
) -> list[AssetCtx]:
    """
    Fetch universe + funding/OI/mark/volume semua symbol.
    Response: [ {universe: [...]}, [ctx, ctx, ...] ] — index sejajar.
    """
    async def _call():
        return await http_post_json(HL_INFO_URL, {"type": "metaAndAssetCtxs"})

    raw = await with_retry("hyperliquid_rest", _call, memory)
    if not isinstance(raw, list) or len(raw) < 2:
        raise DataUnavailable("hyperliquid_rest: unexpected metaAndAssetCtxs shape")

    meta, ctxs = raw[0], raw[1]
    universe = meta.get("universe") or []
    out: list[AssetCtx] = []
    for i, u in enumerate(universe):
        if i >= len(ctxs):
            break
        name = u.get("name")
        if not name or u.get("isDelisted"):
            continue
        c = ctxs[i] or {}
        try:
            out.append(AssetCtx(
                symbol=str(name),
                funding=float(c.get("funding") or 0),
                open_interest=float(c.get("openInterest") or 0),
                mark_px=float(c.get("markPx") or 0),
                mid_px=float(c.get("midPx") or c.get("markPx") or 0),
                day_ntl_vlm=float(c.get("dayNtlVlm") or 0),
                prev_day_px=float(c.get("prevDayPx") or 0),
                oracle_px=float(c.get("oraclePx") or 0),
            ))
        except (TypeError, ValueError):
            continue
    return out


async def hl_all_mids(memory: "MemoryEngine | None" = None) -> dict[str, float]:
    async def _call():
        return await http_post_json(HL_INFO_URL, {"type": "allMids"})

    raw = await with_retry("hyperliquid_rest", _call, memory)
    if not isinstance(raw, dict):
        raise DataUnavailable("hyperliquid_rest: allMids not dict")
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def hl_candle_snapshot(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    memory: "MemoryEngine | None" = None,
) -> list[Candle]:
    """
    OHLCV history. interval: 1m|5m|15m|1h|4h|1d …
    Max ~5000 candles per call; paginate dengan startTime terakhir.
    """
    async def _call():
        return await http_post_json(HL_INFO_URL, {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
            },
        })

    raw = await with_retry("hyperliquid_rest", _call, memory)
    if not isinstance(raw, list):
        raise DataUnavailable("hyperliquid_rest: candleSnapshot not list")
    out: list[Candle] = []
    for row in raw:
        try:
            t_ms = int(row["t"])
            out.append(Candle(
                symbol=coin,
                timeframe=interval,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row["v"]),
                bar_time=datetime.fromtimestamp(t_ms / 1000.0, tz=UTC),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ----- Binance REST (cross-check L3) -----

async def binance_premium_price(
    symbol: str,
    memory: "MemoryEngine | None" = None,
) -> float | None:
    """Harga mark futures Binance. symbol contoh BTCUSDT — caller yang mapping."""
    if not _circuit_allow("binance_rest"):
        return None
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/price?symbol={symbol}"

    async def _call():
        return await http_get_json(url)

    try:
        raw = await with_retry("binance_rest", _call, memory)
        return float(raw["price"])
    except (DataUnavailable, KeyError, TypeError, ValueError):
        return None


async def bybit_ticker_price(
    symbol: str,
    memory: "MemoryEngine | None" = None,
) -> float | None:
    """Harga mark/last linear Bybit — fallback cross-check."""
    if not _circuit_allow("bybit_rest"):
        return None
    url = f"{BYBIT_API}/v5/market/tickers?category=linear&symbol={symbol}"

    async def _call():
        return await http_get_json(url)

    try:
        raw = await with_retry("bybit_rest", _call, memory)
        lst = (raw.get("result") or {}).get("list") or []
        if not lst:
            return None
        row = lst[0]
        px = row.get("markPrice") or row.get("lastPrice")
        return float(px) if px is not None else None
    except (DataUnavailable, KeyError, TypeError, ValueError, IndexError):
        return None


async def cross_exchange_price(
    hl_symbol: str,
    memory: "MemoryEngine | None" = None,
) -> tuple[float | None, str | None]:
    """Binance dulu, Bybit fallback. Skip source kalau circuit OPEN."""
    pair = f"{hl_symbol}USDT"
    px = await binance_premium_price(pair, memory)
    if px is not None:
        return px, "binance"
    px = await bybit_ticker_price(pair, memory)
    if px is not None:
        return px, "bybit"
    return None, None


async def binance_klines(
    symbol: str,
    interval: str,
    limit: int = 100,
    memory: "MemoryEngine | None" = None,
) -> list[Candle]:
    url = (
        f"{BINANCE_FAPI}/fapi/v1/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )

    async def _call():
        return await http_get_json(url)

    raw = await with_retry("binance_rest", _call, memory)
    out: list[Candle] = []
    for row in raw:
        try:
            out.append(Candle(
                symbol=symbol,
                timeframe=interval,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                bar_time=datetime.fromtimestamp(int(row[0]) / 1000.0, tz=UTC),
            ))
        except (IndexError, TypeError, ValueError):
            continue
    return out


# ----- Universe helpers (L0, Aturan #7 — no hardcode) -----

# Volume floor by rank (§IX.2) — Kategori A thresholds
VOL_FLOOR_MAJOR = 20_000_000.0   # rank 1–10
VOL_FLOOR_MID = 5_000_000.0      # rank 11–50
VOL_FLOOR_LOW = 2_000_000.0      # rank >50


def filter_universe_candidates(
    assets: list[AssetCtx],
    pinned: list[str] | None = None,
) -> list[AssetCtx]:
    """
    Layer 0 filter: volume floor by rank, drop zero price.
    pinned tetap harus lolos filter (bukan whitelist bypass).
    """
    ranked = sorted(assets, key=lambda a: a.day_ntl_vlm, reverse=True)
    out: list[AssetCtx] = []
    pinned_set = {p.upper() for p in (pinned or [])}

    for i, a in enumerate(ranked):
        rank = i + 1
        if a.mark_px <= 0 and a.mid_px <= 0:
            continue
        if rank <= 10:
            floor = VOL_FLOOR_MAJOR
        elif rank <= 50:
            floor = VOL_FLOOR_MID
        else:
            floor = VOL_FLOOR_LOW
        if a.day_ntl_vlm < floor:
            if a.symbol.upper() in pinned_set:
                logger.warning(
                    "--pin %s ditolak: volume 24h $%.0f < floor $%.0f (rank %d)",
                    a.symbol, a.day_ntl_vlm, floor, rank,
                )
            continue
        out.append(a)
    return out


def get_market_anchor(
    memory: "MemoryEngine | None",
    universe_stats: list[AssetCtx],
) -> str | None:
    """
    Anchor = volume 24h rank #1 (§II.8).
    KONTRAK None: universe kosong + belum pernah anchor → None.
    Caller wajib cek None (skip cycle / bootstrap wait).
    """
    ranked = sorted(universe_stats, key=lambda s: s.day_ntl_vlm, reverse=True)
    if ranked:
        return ranked[0].symbol
    if memory is not None:
        return memory.get_last_anchor()
    return None


def compute_atr(candles: list[Candle], period: int = 14) -> float | None:
    """ATR klasik dari list Candle (urutan waktu naik)."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    if not window:
        return None
    return sum(window) / len(window)



# ----- Binance Force Order WS → Cascade Buffer (§V.3 #5, CASCADE_SCALP) -----
# Stream: wss://fstream.binance.com/ws/!forceOrder@arr
# LONG liquidated → exchange SELL → cascade pressure down → bias SHORT
# SHORT liquidated → exchange BUY  → cascade pressure up   → bias LONG

BINANCE_FORCE_ORDER_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"
CASCADE_WINDOW_SEC = 300.0


def _binance_sym_to_hl(sym: str) -> str:
    """BTCUSDT → BTC, 1000PEPEUSDT → 1000PEPE (HL naming; best-effort strip)."""
    s = (sym or "").upper().strip()
    if s.endswith("USDT"):
        s = s[:-4]
    elif s.endswith("USD"):
        s = s[:-3]
    return s


class CascadeBuffer:
    """
    Rolling 5m liquidation aggregator.
    Single asyncio loop safe (WS task writer + cycle reader).
    """

    def __init__(self, window_sec: float = CASCADE_WINDOW_SEC):
        self.window_sec = window_sec
        # (monotonic_ts, symbol_hl, side_bias LONG|SHORT, usd)
        self._events: list[tuple[float, str, str, float]] = []
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_msg_mono: float = 0.0
        self.total_events = 0
        self.last_error: str = ""
        self._memory: "MemoryEngine | None" = None

    def bind_memory(self, memory: "MemoryEngine | None") -> None:
        self._memory = memory

    def _prune(self, now_m: float | None = None) -> None:
        now_m = now_m if now_m is not None else time.monotonic()
        cut = now_m - self.window_sec
        if self._events and self._events[0][0] < cut:
            self._events = [e for e in self._events if e[0] >= cut]

    def add_event(self, symbol_hl: str, side_bias: str, usd: float) -> None:
        if not symbol_hl or usd <= 0:
            return
        if side_bias not in ("LONG", "SHORT"):
            return
        now_m = time.monotonic()
        self._events.append((now_m, symbol_hl, side_bias, float(usd)))
        self.total_events += 1
        self.last_msg_mono = now_m
        self._prune(now_m)
        mem = self._memory
        if mem is not None and mem._write_queue is not None:
            try:
                mem._write_queue.put_nowait((
                    "liquidation_log",
                    {
                        "symbol": symbol_hl,
                        "side": side_bias,
                        "amount_usd": float(usd),
                        "exchange": "binance",
                        "recorded_at": now_utc().isoformat(),
                    },
                ))
            except Exception:
                pass

    def snapshot_usd(self) -> dict[str, float]:
        """symbol → total USD liquidated in window."""
        self._prune()
        out: dict[str, float] = defaultdict(float)
        for _, sym, _, usd in self._events:
            out[sym] += usd
        return dict(out)

    def snapshot_side(self) -> dict[str, str]:
        """symbol → dominant cascade bias by USD weight."""
        self._prune()
        long_usd: dict[str, float] = defaultdict(float)
        short_usd: dict[str, float] = defaultdict(float)
        for _, sym, side, usd in self._events:
            if side == "LONG":
                long_usd[sym] += usd
            else:
                short_usd[sym] += usd
        out: dict[str, str] = {}
        for sym in set(long_usd) | set(short_usd):
            out[sym] = "SHORT" if short_usd[sym] >= long_usd[sym] else "LONG"
        return out

    def status_note(self) -> str:
        self._prune()
        n_sym = len({e[1] for e in self._events})
        total = sum(e[3] for e in self._events)
        age = (
            f"{int(time.monotonic() - self.last_msg_mono)}s ago"
            if self.last_msg_mono else "no msg"
        )
        if self.connected:
            return f"live · {len(self._events)} evt · {n_sym} sym · ${total/1e6:.1f}M/5m · {age}"
        if self.last_error:
            return f"down · {self.last_error[:40]}"
        return "starting"

    def ingest_force_order(self, msg: dict | list) -> None:
        """Parse one Binance forceOrder payload (or list)."""
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self.ingest_force_order(item)
            return
        if not isinstance(msg, dict):
            return
        o = msg.get("o")
        if not isinstance(o, dict):
            return
        raw_sym = str(o.get("s") or "")
        side = str(o.get("S") or "").upper()
        try:
            qty = float(o.get("q") or o.get("z") or 0)
            px = float(o.get("ap") or o.get("p") or 0)
        except (TypeError, ValueError):
            return
        usd = qty * px
        if usd <= 0:
            return
        if side == "SELL":
            bias = "SHORT"
        elif side == "BUY":
            bias = "LONG"
        else:
            return
        self.add_event(_binance_sym_to_hl(raw_sym), bias, usd)

    async def _ws_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            self.last_error = "websockets not installed"
            logger.error("cascade WS · %s", self.last_error)
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    BINANCE_FORCE_ORDER_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2_000_000,
                ) as ws:
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    logger.info("🌊 cascade WS · connected Binance forceOrder")
                    async for raw in ws:
                        try:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        self.ingest_force_order(msg)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"[:80]
                logger.warning(
                    "🌊 cascade WS · disconnect · %s · retry %.0fs",
                    self.last_error, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._ws_loop(), name="cascade_ws")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        self.connected = False


_cascade_buffer = CascadeBuffer()


def get_cascade_buffer() -> CascadeBuffer:
    return _cascade_buffer


# -----------------------------------------------------------------------------
# HL L2 order book buffer — wires real `microstructure_confirm` (Tier A/B path)
# -----------------------------------------------------------------------------
# Stream: wss://api.hyperliquid.xyz/ws  · subscription {"type": "l2Book", "coin": SYM}
# Tanpa ini, microstructure selalu fallback ke funding_proxy (is_fallback=True)
# → real_count tidak pernah naik dari agent ini → Tier A/B mustahil tercapai
# semata dari microstructure (§ catatan "Tier C itu benar, bukan bug").

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_BOOK_DEPTH_LEVELS = 10       # jumlah level teratas yang dihitung notional-nya
HL_BOOK_MAX_AGE_SEC = 30.0      # snapshot lebih tua dari ini dianggap stale → None
HL_BOOK_SUB_BATCH = 25          # subscribe/unsubscribe per reconcile tick (hindari burst)
HL_BOOK_RECONCILE_SEC = 3.0     # interval cek desired vs subscribed


class HLBookBuffer:
    """
    Subscribe l2Book Hyperliquid untuk symbol yang sedang jadi kandidat aktif
    (bukan seluruh universe — 178 subscription sekaligus tidak perlu dan boros).
    Expose imbalance top-N level: (bid_usd - ask_usd) / (bid_usd + ask_usd).
    Single asyncio loop safe (WS task writer + cycle reader), fail-soft penuh:
    kalau WS mati / belum connect, get_imbalance() balik None → caller fallback
    ke funding_proxy seperti sebelum ini di-wire.
    """

    def __init__(self, depth: int = HL_BOOK_DEPTH_LEVELS, max_age_sec: float = HL_BOOK_MAX_AGE_SEC):
        self.depth = depth
        self.max_age_sec = max_age_sec
        # symbol_hl → (bid_usd, ask_usd, monotonic_ts)
        self._book: dict[str, tuple[float, float, float]] = {}
        self._desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_msg_mono: float = 0.0
        self.total_msgs = 0
        self.last_error: str = ""

    def set_symbols(self, symbols: list[str]) -> None:
        """Dipanggil tiap cycle dari run_pipeline_cycle dengan kandidat lolos L0.
        Sinkron, non-blocking — hanya update set; kirim subscribe/unsubscribe
        aktual dilakukan oleh _ws_loop lewat _reconcile_subscriptions()."""
        self._desired = {s for s in symbols if s}

    def get_imbalance(self, symbol_hl: str) -> float | None:
        entry = self._book.get(symbol_hl)
        if entry is None:
            return None
        bid_usd, ask_usd, ts = entry
        if time.monotonic() - ts > self.max_age_sec:
            return None
        total = bid_usd + ask_usd
        if total <= 0:
            return None
        return clamp((bid_usd - ask_usd) / total, -1.0, 1.0)

    def status_note(self) -> str:
        age = (
            f"{int(time.monotonic() - self.last_msg_mono)}s ago"
            if self.last_msg_mono else "no msg"
        )
        if self.connected:
            return f"live · sub={len(self._subscribed)} · {self.total_msgs} msg · {age}"
        if self.last_error:
            return f"down · {self.last_error[:40]}"
        return "starting"

    def ingest(self, msg: dict) -> None:
        """Parse satu l2Book WS payload dari Hyperliquid."""
        if not isinstance(msg, dict) or msg.get("channel") != "l2Book":
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        coin = str(data.get("coin") or "")
        levels = data.get("levels")
        if not coin or not isinstance(levels, list) or len(levels) < 2:
            return
        try:
            bids, asks = levels[0], levels[1]
            bid_usd = sum(
                float(lv["px"]) * float(lv["sz"])
                for lv in (bids or [])[: self.depth]
                if isinstance(lv, dict)
            )
            ask_usd = sum(
                float(lv["px"]) * float(lv["sz"])
                for lv in (asks or [])[: self.depth]
                if isinstance(lv, dict)
            )
        except (TypeError, ValueError, KeyError):
            return
        now_m = time.monotonic()
        self._book[coin] = (bid_usd, ask_usd, now_m)
        self.total_msgs += 1
        self.last_msg_mono = now_m

    async def _reconcile_subscriptions(self, ws) -> None:
        """Samakan subscription aktif dengan kandidat cycle terakhir.
        Dibatasi per-batch supaya tidak flood WS saat universe berganti besar."""
        to_add = list(self._desired - self._subscribed)[:HL_BOOK_SUB_BATCH]
        to_remove = list(self._subscribed - self._desired)[:HL_BOOK_SUB_BATCH]
        for sym in to_add:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": sym},
            }))
            self._subscribed.add(sym)
        for sym in to_remove:
            await ws.send(json.dumps({
                "method": "unsubscribe",
                "subscription": {"type": "l2Book", "coin": sym},
            }))
            self._subscribed.discard(sym)
            self._book.pop(sym, None)

    async def _ws_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            self.last_error = "websockets not installed"
            logger.error("HL book WS · %s", self.last_error)
            return

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    HL_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=5_000_000,
                ) as ws:
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    self._subscribed = set()  # fresh connect → re-subscribe semua desired
                    self._book.clear()  # buang snapshot sesi lama (age-guard sudah cover, ini extra bersih)
                    logger.info("📖 HL book WS · connected")
                    await self._reconcile_subscriptions(ws)
                    last_reconcile = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            raw = None
                        # Reconcile pada wall-clock cadence, independen dari
                        # traffic recv() — kalau tidak, begitu 25 symbol
                        # pertama mulai kirim l2Book terus-menerus, recv()
                        # tidak pernah timeout lagi dan sisa _desired tidak
                        # pernah ke-subscribe (bug #2).
                        now_m = time.monotonic()
                        if now_m - last_reconcile >= HL_BOOK_RECONCILE_SEC:
                            await self._reconcile_subscriptions(ws)
                            last_reconcile = now_m
                        if raw is None:
                            continue
                        try:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            msg = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        self.ingest(msg)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"[:80]
                logger.warning(
                    "📖 HL book WS · disconnect · %s · retry %.0fs",
                    self.last_error, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._ws_loop(), name="hl_book_ws")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        self.connected = False


_hl_book_buffer = HLBookBuffer()


def get_hl_book_buffer() -> HLBookBuffer:
    return _hl_book_buffer


# =============================================================================
# §6  AGENTS
# =============================================================================
# L1 Trigger: Funding / Liquidation / Volatility
# L2 Horizon classifier
# L3 Confirm: Microstructure / Flow / Cross-Exchange (min_confirm relatif N)
# L4 Analyst (Timing) — entry/SL/TP + base_confidence
# L5 Context modifiers — di §7 pipeline (Structure/Macro/…)

# --- Kategori B defaults (cold-start, sample < MIN_SAMPLE) ---
MIN_SAMPLE_DYNAMIC = 30          # §II.3
FUNDING_P_DEFAULT = 95           # whitelist default
CASCADE_P_DEFAULT = 90
ATR_COMPRESSION_P_DEFAULT = 20
CASCADE_ABS_FLOOR_USD = 5_000_000  # §IV.2 cascade size > $5M / 5m
ATR_PCT_MIN_FILTER = 0.005       # Layer 0: ATR > 0.5%
BASE_CONF_W1, BASE_CONF_W2, BASE_CONF_W3 = 0.4, 0.35, 0.25  # Kategori C


@dataclass
class TriggerHit:
    """Output satu agent L1 yang fire."""
    symbol: str
    trigger_type: str          # funding_extreme | cascade | volatility_compression
    strength: float            # 0–1 seberapa jauh di atas threshold
    direction_bias: str        # LONG | SHORT | NEUTRAL
    raw_value: float
    threshold: float
    setup_type: str            # FUNDING_SQUEEZE | CASCADE_SCALP | COMPRESSION


@dataclass
class ConfirmResult:
    agent: str
    confirmed: bool
    detail: str = ""
    is_fallback: bool = False   # True = echo/proxy, bukan source independen


@dataclass
class TimingResult:
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    base_confidence: float
    atr: float
    sl_basis: str = "atr"       # "atr" | "structure" — audit trail, non-breaking
    entry_basis: str = "atr"    # "atr" | "structure"


def _p_threshold(history: list[float], p: float, cold_default_abs: float | None = None) -> float:
    """Percentile threshold; cold-start pakai abs default kalau sample kurang."""
    if len(history) < MIN_SAMPLE_DYNAMIC:
        if cold_default_abs is not None:
            return cold_default_abs
        return percentile(history, p) if history else 0.0
    return percentile(history, p)


def funding_direction_bias(funding_rate: float) -> str:
    """Funding positif = crowded long → bias SHORT (squeeze). Negatif → LONG."""
    if funding_rate > 0:
        return "SHORT"
    if funding_rate < 0:
        return "LONG"
    return "NEUTRAL"


# ----- L1: Funding Agent -----

def funding_agent(
    asset: AssetCtx,
    memory: "MemoryEngine | None",
    p_value: float = FUNDING_P_DEFAULT,
) -> TriggerHit | None:
    """
    Fire kalau abs(funding) > P{p} historis per symbol (§II.3 Kategori B).
    Cold-start: threshold abs 0.03% per 8h (≈0.0003) sebagai jembatan D.
    """
    hist = memory.get_funding_history(asset.symbol) if memory else []
    # funding HL sudah dalam bentuk rate (mis. 0.0001 = 0.01%)
    cold = 0.0003
    thr = _p_threshold([abs(x) for x in hist], p_value, cold_default_abs=cold)
    val = abs(asset.funding)
    if val <= thr:
        return None
    # strength: seberapa jauh di atas threshold, cap 1.0
    strength = clamp((val - thr) / thr if thr > 0 else 1.0, 0.0, 1.0)
    if memory:
        consistency = memory.get_funding_history(asset.symbol, lookback_days=1)
        if len(consistency) >= 3:
            last3 = consistency[-3:]
            same_sign = (
                all(x > 0 for x in last3)
                if asset.funding > 0
                else all(x < 0 for x in last3)
            )
            if not same_sign:
                strength *= 0.5
    return TriggerHit(
        symbol=asset.symbol,
        trigger_type="funding_extreme",
        strength=strength,
        direction_bias=funding_direction_bias(asset.funding),
        raw_value=asset.funding,
        threshold=thr if asset.funding >= 0 else -thr,
        setup_type="FUNDING_SQUEEZE",
    )


# ----- L1: Liquidation / Cascade Agent -----

def cascade_agent(
    symbol: str,
    cascade_usd_5m: float,
    memory: "MemoryEngine | None",
    p_value: float = CASCADE_P_DEFAULT,
) -> TriggerHit | None:
    """
    Fire kalau cascade 5m > max(P90 histori, $5M floor) (§IV.2 / §IV.3).
    cascade_usd_5m diisi dari WS buffer / aggregator di pipeline.
    """
    hist = memory.get_cascade_history(symbol) if memory else []
    thr_p = _p_threshold(hist, p_value, cold_default_abs=CASCADE_ABS_FLOOR_USD)
    thr = max(thr_p, CASCADE_ABS_FLOOR_USD)
    if cascade_usd_5m <= thr:
        return None
    strength = clamp(
        (cascade_usd_5m - thr) / thr if thr > 0 else 1.0, 0.0, 1.0
    )
    return TriggerHit(
        symbol=symbol,
        trigger_type="cascade",
        strength=strength,
        direction_bias="NEUTRAL",  # arah dari side cascade di pipeline
        raw_value=cascade_usd_5m,
        threshold=thr,
        setup_type="CASCADE_SCALP",
    )


# ----- L1: Volatility / Compression Agent -----

def volatility_agent(
    symbol: str,
    atr_now: float,
    price: float,
    memory: "MemoryEngine | None",
    p_value: float = ATR_COMPRESSION_P_DEFAULT,
) -> TriggerHit | None:
    """
    Fire kalau ATR sekarang < P20 historis DAN ATR% > 0.5% filter L0 (§IV.4).
    """
    if price <= 0 or atr_now <= 0:
        return None
    atr_pct = atr_now / price
    if atr_pct < ATR_PCT_MIN_FILTER:
        return None  # terlalu mati, bukan kandidat
    hist = memory.get_atr_history(symbol) if memory else []
    # cold-start: anggap compression kalau atr_pct < 1% (jembatan D)
    thr = _p_threshold(hist, p_value, cold_default_abs=price * 0.01)
    if atr_now >= thr:
        return None
    # strength: semakin kecil ATR vs thr, semakin kuat compression
    strength = clamp((thr - atr_now) / thr if thr > 0 else 0.0, 0.0, 1.0)
    return TriggerHit(
        symbol=symbol,
        trigger_type="volatility_compression",
        strength=strength,
        direction_bias="NEUTRAL",  # arah dari breakout di L4
        raw_value=atr_now,
        threshold=thr,
        setup_type="COMPRESSION",
    )


# ----- L2: Horizon classifier -----

def classify_horizon(trigger: TriggerHit) -> str | None:
    """§IV.5 / §I.2 Layer 2."""
    if trigger.trigger_type == "cascade":
        return "SCALPING"
    if trigger.trigger_type == "funding_extreme":
        return "INTRADAY"
    if trigger.trigger_type == "volatility_compression":
        return "SWING"
    return None


# ----- L3: Confirm agents -----

def min_confirm_required(n_active: int) -> int | None:
    """
    min_confirm = ceil(2 * N / 3). N=1 atau 0 → L3 gagal (§I.2).
    """
    if n_active <= 1:
        return None  # fail
    return math.ceil(2 * n_active / 3)


def microstructure_confirm(
    direction: str,
    imbalance: float | None,
    absorption: bool | None = None,
) -> ConfirmResult:
    """
    Confirm kalau orderbook imbalance searah signal atau ada absorption.
    imbalance: +1 bid-heavy (bullish), -1 ask-heavy. None = agent off.
    """
    if imbalance is None and absorption is None:
        return ConfirmResult("microstructure", False, "no data")
    ok = False
    detail = []
    if absorption:
        ok = True
        detail.append("absorption")
    if imbalance is not None:
        if direction == "LONG" and imbalance > 0.2:
            ok = True
            detail.append(f"imbalance={imbalance:.2f}")
        elif direction == "SHORT" and imbalance < -0.2:
            ok = True
            detail.append(f"imbalance={imbalance:.2f}")
    return ConfirmResult("microstructure", ok, ",".join(detail) or "no confirm")


def flow_confirm(
    direction: str,
    netflow: float | None,
    netflow_p80: float | None = None,
    netflow_p20: float | None = None,
) -> ConfirmResult:
    """
    §X.5: SHORT + inflow tinggi (p80) = bearish confirm;
    LONG + outflow (p20) = bullish confirm.
    netflow None → agent off (wallet env kosong).
    """
    if netflow is None:
        return ConfirmResult("flow", False, "agent off")
    p80 = netflow_p80 if netflow_p80 is not None else 0.0
    p20 = netflow_p20 if netflow_p20 is not None else 0.0
    if direction == "SHORT" and netflow > p80:
        return ConfirmResult("flow", True, f"inflow={netflow:.0f}>p80")
    if direction == "LONG" and netflow < p20:
        return ConfirmResult("flow", True, f"outflow={netflow:.0f}<p20")
    return ConfirmResult("flow", False, "no confirm")


def cross_exchange_confirm(
    hl_price: float,
    ref_price: float | None,
    max_div: float = 0.01,
    source: str | None = None,
) -> ConfirmResult:
    """Sync HL vs Binance/Bybit: |div| < max_div → sync OK (confirm)."""
    if ref_price is None or ref_price <= 0 or hl_price <= 0:
        return ConfirmResult("cross_exchange", False, "no xref data")
    div = abs(hl_price - ref_price) / ref_price
    tag = source or "xref"
    if div <= max_div:
        return ConfirmResult("cross_exchange", True, f"{tag}_div={div:.4f}")
    return ConfirmResult("cross_exchange", False, f"{tag}_div={div:.4f}>max")


def oi_structure_confirm(
    direction: str,
    funding: float,
    open_interest: float,
) -> ConfirmResult:
    """
    Confirm sekunder saat cross-exchange mati (GH Actions IP block dsb).
    Proxy: OI > 0 + funding searah bias = struktur crowded masih hidup.
    Tetap ACTIVE (bukan 'no data') supaya N tidak jatuh ke 1.
    """
    if open_interest <= 0:
        return ConfirmResult("oi_structure", False, "no oi")
    ok = (
        (direction == "SHORT" and funding > 0)
        or (direction == "LONG" and funding < 0)
    )
    return ConfirmResult(
        "oi_structure",
        ok,
        f"oi={open_interest:.2f}_fund={funding:.6g}",
        is_fallback=True,  # funding+OI echo, bukan source independen
    )


def evaluate_confirms(
    results: list[ConfirmResult],
) -> tuple[bool, int, int, int]:
    """
    Returns (passed, total_confirm_count, real_confirm_count, n_active).
    - Agent off / no data → tidak dihitung
    - Fallback (funding_proxy, oi echo) → boleh isi N, tapi
      minimal 1 confirm harus dari source NON-fallback (independen).
    """
    inactive = {
        "agent off", "no data", "no binance data", "no xref data", "no oi",
    }
    active = [r for r in results if r.detail not in inactive]
    n = len(active)
    need = min_confirm_required(n)
    if need is None:
        return False, 0, 0, n
    count = sum(1 for r in active if r.confirmed)
    real_count = sum(1 for r in active if r.confirmed and not r.is_fallback)
    if count < need:
        return False, count, real_count, n
    # Kalau ada agent non-fallback di pool, wajib ≥1 confirm real
    has_real_agent = any(not r.is_fallback for r in active)
    if has_real_agent:
        if real_count < 1:
            return False, count, real_count, n
    # Pure-fallback env (REST block): lolos need, tapi quality lemah
    return True, count, real_count, n


# ----- L4: Analyst / Timing -----

def analyst_timing(
    direction: str,
    price: float,
    atr: float,
    horizon: str,
    trigger_strength: float,
    confirm_count: int,
) -> TimingResult | None:
    """
    Entry zone + SL/TP dari ATR & parameter setup (§IV).
    base_confidence formula §I.2 Layer 4.
    """
    if price <= 0 or atr <= 0:
        return None

    # SL/TP mult §IV. INTRADAY TP2=3.0×ATR → R:R natural 2.0 (= MIN_RR).
    if horizon == "SCALPING":
        sl_mult, tp1_mult, tp2_mult = 0.8, 1.0, 1.5
        min_rr = MIN_RR["SCALPING"]
    elif horizon == "SWING":
        sl_mult, tp1_mult, tp2_mult = 2.0, 3.0, 5.0
        min_rr = MIN_RR["SWING"]
    else:  # INTRADAY
        sl_mult, tp1_mult, tp2_mult = 1.5, 1.5, 3.0
        min_rr = MIN_RR["INTRADAY"]

    # Zone sempit (0.4×ATR) — bukan 1.0×ATR yang hampir selebar SL
    zone_w = clamp(ENTRY_ZONE_ATR_WIDTH, ENTRY_ZONE_ATR_MIN, ENTRY_ZONE_ATR_MAX) * atr
    half = zone_w / 2.0
    entry_low = price - half
    entry_high = price + half

    # R:R dari mid (price) — konsisten dengan mult ATR, bukan worst-entry.
    if direction == "LONG":
        stop = price - sl_mult * atr
        tp1 = price + tp1_mult * atr
        tp2 = price + tp2_mult * atr
        risk = price - stop
        reward = tp2 - price
    elif direction == "SHORT":
        stop = price + sl_mult * atr
        tp1 = price - tp1_mult * atr
        tp2 = price - tp2_mult * atr
        risk = stop - price
        reward = price - tp2
    else:
        return None

    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr - 1e-9:
        return None

    rr_quality = min(rr / 3.0, 1.0)
    base_conf = clamp(
        BASE_CONF_W1 * trigger_strength
        + BASE_CONF_W2 * (confirm_count / 3.0)
        + BASE_CONF_W3 * rr_quality,
        0.0,
        1.0,
    )
    return TimingResult(
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        rr=rr,
        base_confidence=base_conf,
        atr=atr,
        sl_basis="atr",
        entry_basis="atr",
    )


# --- Structural SL/TP + entry-zone buffer (§P1) ---
STRUCT_SL_BUFFER_ATR = 0.15         # buffer di luar swing/sweep level, cegah wick-hunt persis
STRUCT_ENTRY_ZONE_ATR_WIDTH = 0.30  # zona entry di sekitar sweep, lebih sempit dari ATR default


def analyst_timing_structural(
    direction: str,
    price: float,
    atr: float,
    horizon: str,
    trigger_strength: float,
    confirm_count: int,
    structure: "StructureContext",
) -> TimingResult | None:
    """
    Versi struktural §P1: SL di luar swing/sweep terdekat (bukan ATR mult),
    entry zone di sekitar level sweep kalau ada sweep searah & masih relevan.
    TP tetap R-multiple dari risk struktural (RR tetap terjaga, min_rr sama).
    Fail-soft KETAT: kalau tidak ada swing relevan searah → return None,
    caller WAJIB fallback ke analyst_timing (ATR) — tidak ada silent guess.
    """
    if price <= 0 or atr <= 0:
        return None
    if horizon == "SCALPING":
        tp1_mult, tp2_mult = 1.0, 1.5
        min_rr = MIN_RR["SCALPING"]
    elif horizon == "SWING":
        tp1_mult, tp2_mult = 3.0, 5.0
        min_rr = MIN_RR["SWING"]
    else:
        tp1_mult, tp2_mult = 1.5, 3.0
        min_rr = MIN_RR["INTRADAY"]

    buf = atr * STRUCT_SL_BUFFER_ATR
    sweep = structure.sweep
    entry_basis = "structure"

    if direction == "LONG":
        # SL: di bawah wick sweep (kalau ada sweep bullish relevan) atau
        # di bawah swing LOW terakhir kalau tidak ada sweep.
        candidate_lows = [s.price for s in structure.swings if s.kind == "LOW"]
        if sweep is not None and sweep.direction == "LONG":
            struct_low = sweep.wick_price
            entry_mid = sweep.close_price
        elif candidate_lows:
            struct_low = candidate_lows[-1]
            entry_mid = price
        else:
            return None
        if entry_mid < price - 0.5 * atr:
            return None
        stop = struct_low - buf
        if stop >= entry_mid:
            return None
        entry_basis = "structure"
        # Prefer FVG bullish dekat harga sebagai entry zone (lebih presisi)
        fvg = nearest_aligned_fvg(
            [structure.nearest_fvg] if structure.nearest_fvg else [],
            "LONG", price, atr,
        )
        # juga scan: nearest_fvg only stores one; rebuild from none — use stored if align
        fvg = nearest_aligned_fvg(structure.fvg_zones or [], "LONG", price, atr)
        if fvg is not None and fvg.high > stop:
            entry_mid = (fvg.low + fvg.high) / 2.0
            entry_low = fvg.low
            entry_high = fvg.high
            entry_basis = "fvg"
        if entry_basis != "fvg":
            risk = entry_mid - stop
            half = clamp(STRUCT_ENTRY_ZONE_ATR_WIDTH, ENTRY_ZONE_ATR_MIN, ENTRY_ZONE_ATR_MAX) * atr / 2.0
            entry_low = entry_mid - half
            entry_high = entry_mid + half
        else:
            risk = entry_mid - stop
            if risk <= 0:
                return None
        tp1 = entry_mid + tp1_mult * atr
        tp2 = entry_mid + tp2_mult * atr
        reward = tp2 - entry_mid
    elif direction == "SHORT":
        candidate_highs = [s.price for s in structure.swings if s.kind == "HIGH"]
        if sweep is not None and sweep.direction == "SHORT":
            struct_high = sweep.wick_price
            entry_mid = sweep.close_price
        elif candidate_highs:
            struct_high = candidate_highs[-1]
            entry_mid = price
        else:
            return None
        if entry_mid > price + 0.5 * atr:
            return None
        stop = struct_high + buf
        if stop <= entry_mid:
            return None
        entry_basis = "structure"
        fvg = nearest_aligned_fvg(structure.fvg_zones or [], "SHORT", price, atr)
        if fvg is not None and fvg.low < stop:
            entry_mid = (fvg.low + fvg.high) / 2.0
            entry_low = fvg.low
            entry_high = fvg.high
            entry_basis = "fvg"
        if entry_basis != "fvg":
            risk = stop - entry_mid
            half = clamp(STRUCT_ENTRY_ZONE_ATR_WIDTH, ENTRY_ZONE_ATR_MIN, ENTRY_ZONE_ATR_MAX) * atr / 2.0
            entry_low = entry_mid - half
            entry_high = entry_mid + half
        else:
            risk = stop - entry_mid
            if risk <= 0:
                return None
        tp1 = entry_mid - tp1_mult * atr
        tp2 = entry_mid - tp2_mult * atr
        reward = entry_mid - tp2
    else:
        return None

    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr - 1e-9:
        return None

    rr_quality = min(rr / 3.0, 1.0)
    base_conf = clamp(
        BASE_CONF_W1 * trigger_strength
        + BASE_CONF_W2 * (confirm_count / 3.0)
        + BASE_CONF_W3 * rr_quality,
        0.0,
        1.0,
    )
    return TimingResult(
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop,
        tp1=tp1,
        tp2=tp2,
        rr=rr,
        base_confidence=base_conf,
        atr=atr,
        sl_basis="structure",
        entry_basis=entry_basis,
    )


def assign_tier(rr: float, confirm_count: int) -> str:
    """
    §I.2 Layer 6 tier.
    A: RR≥2.5 + 3 confirms | B: RR≥2.0 + 2 confirms | C: sisanya.
    Epsilon 1e-9: hindari float miss (RR desain 3.0/1.5 = 2.0 tepat).
    Catatan: confidence ≠ tier. Conf tinggi + tier C = trigger kuat tapi
    confirm/RR belum tembus gerbang B (harusnya jarang untuk INTRADAY).

    CATATAN (keputusan trading policy, bukan bug): di env dengan
    cross-exchange terblok (mis. GH Actions), real_count praktis hanya
    dari microstructure book (max=1) → tier C. Kalau Tier B perlu muncul
    di env tersebut, tambahkan source real kedua (mis. trade-flow dari HL
    'trades' WS) atau longgarkan TIER_B_MIN_CONFIRMS. Tidak diubah di sini.
    """
    if rr + 1e-9 >= TIER_A_MIN_RR and confirm_count >= TIER_A_MIN_CONFIRMS:
        return "A"
    if rr + 1e-9 >= TIER_B_MIN_RR and confirm_count >= TIER_B_MIN_CONFIRMS:
        return "B"
    return "C"


# ----- L1 scan helper (semua trigger per asset) -----

def scan_triggers_for_asset(
    asset: AssetCtx,
    atr_now: float | None,
    cascade_usd_5m: float,
    memory: "MemoryEngine | None",
    setup_enabled: dict[str, bool] | None = None,
) -> list[TriggerHit]:
    """
    Jalankan 3 agent L1. setup_enabled gate (§IV.6) — skip setup yang di-pause.
    """
    enabled = setup_enabled or {
        "FUNDING_SQUEEZE": True,
        "CASCADE_SCALP": True,
        "COMPRESSION": True,
    }
    hits: list[TriggerHit] = []

    if enabled.get("FUNDING_SQUEEZE", True):
        h = funding_agent(asset, memory)
        if h:
            hits.append(h)

    if enabled.get("CASCADE_SCALP", True) and cascade_usd_5m > 0:
        h = cascade_agent(asset.symbol, cascade_usd_5m, memory)
        if h:
            hits.append(h)

    if enabled.get("COMPRESSION", True) and atr_now is not None:
        px = asset.mid_px or asset.mark_px
        h = volatility_agent(asset.symbol, atr_now, px, memory)
        if h:
            hits.append(h)

    return hits


# =============================================================================
# §7  PIPELINE 8 LAYER
# =============================================================================
# L0 Universe → L1 Trigger → L2 Horizon → L3 Confirm → L4 Timing
# → L5 Context → L6 Veto+Tier → L7 Delivery (queue; Telegram di §12–13)


@dataclass
class PipelineSignal:
    """Sinyal lolos L6, siap delivery."""
    signal_id: str
    symbol: str
    direction: str
    horizon: str
    tier: str
    setup_type: str
    trigger_type: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    confidence: float
    base_confidence: float
    context_mult: float
    confirm_count: int
    active_agent_count: int
    reasoning: str
    created_at: datetime
    valid_until: datetime
    sl_basis: str = "atr"
    entry_basis: str = "atr"
    state: str = "PENDING"


def get_symbol_sector(memory: "MemoryEngine | None", symbol: str) -> str | None:
    """Read the sector tag written by the optional Gemini sector scan."""
    if memory is None or memory._conn is None:
        return None
    row = memory._conn.execute(
        "SELECT sector FROM symbol_metadata WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None:
        return None
    sector = row["sector"]
    return str(sector) if sector else None


async def context_modifier(
    symbol: str,
    direction: str,
    anchor: str | None,
    *,
    us10y: float | None = None,
    fear_greed: float | None = None,
    anchor_corr: float | None = None,
    news_bias: str | None = None,
    sector: str | None = None,
    sector_bias: str | None = None,
) -> float:
    """
    L5 Context — multiplier 0.5–1.2 (§I.2).
    Fail-soft: data kosong → 1.0 (netral).
    - US10Y / Fear&Greed / correlation vs anchor / thin news bias / sector bias
    """
    mult = 1.0
    _ = (symbol, anchor)
    if us10y is not None:
        if us10y >= 4.5:
            mult *= 0.92 if direction == "LONG" else 1.05
        elif us10y <= 3.5:
            mult *= 1.05 if direction == "LONG" else 0.95
    if fear_greed is not None:
        if fear_greed <= 25:
            mult *= 1.08 if direction == "LONG" else 0.94
        elif fear_greed <= 40:
            mult *= 1.03 if direction == "LONG" else 0.97
        elif fear_greed >= 75:
            mult *= 0.94 if direction == "LONG" else 1.08
        elif fear_greed >= 60:
            mult *= 0.97 if direction == "LONG" else 1.03
    # §VIII.3 — high corr to anchor: slightly dampen counter-trend vs market
    if anchor_corr is not None:
        if anchor_corr >= 0.75:
            mult *= 0.97  # crowded with market — slight caution
        elif anchor_corr <= 0.2:
            mult *= 1.03  # decoupled — slight edge for idiosyncratic
    if news_bias in ("BULLISH", "BEARISH"):
        if news_bias == "BULLISH":
            mult *= 1.04 if direction == "LONG" else 0.96
        else:
            mult *= 1.04 if direction == "SHORT" else 0.96
    # B2 fix: sector narrative alignment. Only fires when Gemini sector_tag
    # has written a tag AND the RSS poll shows a directional tilt in that
    # sector's headlines. Otherwise no-op (do not silently degrade).
    if sector and sector_bias in ("BULLISH", "BEARISH"):
        if sector_bias == "BULLISH":
            mult *= 1.03 if direction == "LONG" else 0.97
        else:
            mult *= 1.03 if direction == "SHORT" else 0.97
    return clamp(mult, 0.5, 1.2)


def veto_checks(
    memory: "MemoryEngine | None",
    symbol: str,
    confidence_final: float,
    confidence_floor: float = CONFIDENCE_FLOOR_COLD,
    setup_type: str | None = None,
) -> tuple[bool, str]:
    """
    L6 Veto. Return (ok, reason).
    - concurrent limit
    - symbol cooldown
    - confidence floor
    Blackout event (Forex Factory) → fail-soft skip kalau env kosong.
    """
    if confidence_final < confidence_floor:
        return False, f"confidence {confidence_final:.2f} < floor {confidence_floor:.2f}"
    if memory is None:
        return True, "ok"
    if memory.count_active_signals() >= MAX_CONCURRENT_SIGNALS:
        return False, "max concurrent signals"
    if memory.has_active_symbol(symbol):
        return False, "symbol still active"
    if memory.symbol_post_loss_blocked(symbol):
        return False, "post-loss penalty box"
    if memory.symbol_on_cooldown(symbol):
        return False, "symbol cooldown"
    if memory.setup_signals_paused(setup_type):
        return False, "setup-loss pause"
    if memory.signals_paused():
        return False, "global-loss pause"
    # blackout: CRYPTONE_MINUTES_TO_NEXT_MACRO override
    raw = os.environ.get("CRYPTONE_MINUTES_TO_NEXT_MACRO", "").strip()
    if raw.isdigit() and int(raw) < EVENT_BLACKOUT_MIN:
        return False, f"macro blackout {raw}m"
    return True, "ok"


async def estimate_atr(
    symbol: str,
    price: float,
    memory: "MemoryEngine | None",
) -> float | None:
    """ATR 1h dari candle HL; fallback histori DB; terakhir 1% price (D)."""
    end_ms = int(now_utc().timestamp() * 1000)
    start_ms = end_ms - 48 * 3600 * 1000  # 48 jam
    try:
        candles = await hl_candle_snapshot(symbol, "1h", start_ms, end_ms, memory)
        atr = compute_atr(candles, period=14)
        if atr and atr > 0:
            if memory is not None:
                # Floor ke bucket 5 menit supaya refetch dalam cycle yang
                # sama tidak menumpuk baris baru (UNIQUE keyed by timestamp
                # mikrodetik, jadi harus di-bucket eksplisit).
                ts = now_utc()
                bucket = ts.replace(second=0, microsecond=0) - timedelta(
                    minutes=ts.minute % 5
                )
                await memory.enqueue_write("metric_atr", {
                    "symbol": symbol,
                    "timeframe": "1h",
                    "value": atr,
                    "recorded_at": bucket.isoformat(),
                })
            return atr
    except DataUnavailable as e:
        logger.debug("ATR fetch %s fail-soft: %s", symbol, e)
    if memory is not None:
        hist = memory.get_atr_history(symbol, "1h")
        if hist:
            return hist[-1]
    if price > 0:
        return price * 0.01  # cold-start D
    return None



@dataclass
class SwingPoint:
    idx: int              # index di list candles yang dipakai
    price: float
    kind: str             # "HIGH" | "LOW"
    bar_time: datetime


@dataclass
class StructureEvent:
    kind: str              # "BOS" | "CHoCH"
    direction: str         # "UP" | "DOWN" — arah struktur baru setelah event
    level: float           # level swing yang ditembus
    at: datetime


@dataclass
class SweepEvent:
    direction: str         # "LONG" | "SHORT" — bias yang divalidasi sweep ini
    swept_level: float
    wick_price: float      # extreme (low utk sweep-low, high utk sweep-high)
    close_price: float
    at: datetime


@dataclass
class FVGZone:
    """Fair Value Gap 3-candle (ICT-style, simplified)."""
    direction: str         # LONG = bullish gap (support), SHORT = bearish gap
    low: float
    high: float
    at: datetime
    bar_idx: int


@dataclass
class StructureContext:
    """
    Output structure detector: dipakai analyst_timing_structural buat SL/TP/entry.
    swings sudah urut ascending by idx (chronological).
    """
    swings: list[SwingPoint]
    events: list[StructureEvent]
    last_trend: str            # "UP" | "DOWN" | "RANGE"
    sweep: SweepEvent | None   # sweep paling baru yang belum expired (kalau ada)
    atr: float
    nearest_fvg: FVGZone | None = None
    fvg_zones: list | None = None


def find_swing_points(
    candles: list[Candle],
    lookback: int = STRUCT_SWING_LOOKBACK,
) -> list[SwingPoint]:
    """
    Fractal swing high/low murni: pivot[i] adalah HIGH kalau high[i] adalah
    max ketat di window [i-lookback, i+lookback], demikian juga LOW.
    Tidak pakai repaint-look-ahead di luar window tetap (standar fractal),
    sengaja O(n×lookback) simpel — n kecil (≤ beberapa ratus bar).
    """
    n = len(candles)
    out: list[SwingPoint] = []
    if n < (2 * lookback + 1):
        return out
    for i in range(lookback, n - lookback):
        window = candles[i - lookback: i + lookback + 1]
        hi = candles[i].high
        lo = candles[i].low
        if hi == max(c.high for c in window) and hi > max(
            c.high for c in window if c is not candles[i]
        ):
            out.append(SwingPoint(idx=i, price=hi, kind="HIGH", bar_time=candles[i].bar_time))
        elif lo == min(c.low for c in window) and lo < min(
            c.low for c in window if c is not candles[i]
        ):
            out.append(SwingPoint(idx=i, price=lo, kind="LOW", bar_time=candles[i].bar_time))
    return out


def detect_structure_events(
    candles: list[Candle],
    swings: list[SwingPoint],
) -> tuple[list[StructureEvent], str]:
    """
    BOS = close menembus swing terakhir SEARAH trend berjalan (kontinuasi).
    CHoCH = close menembus swing terakhir BERLAWANAN trend berjalan (reversal).
    Trend disimpulkan dari urutan HH/HL (UP), LH/LL (DOWN), campur (RANGE).
    Fail-soft: swing < 2 → return ([], "RANGE").
    """
    if len(swings) < 2:
        return [], "RANGE"

    events: list[StructureEvent] = []
    trend = "RANGE"
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None

    for sw in swings:
        if sw.kind == "HIGH":
            if last_high is not None:
                broke_close = None
                # Cari candle setelah swing high ini yang close-nya menembus level
                for c in candles[sw.idx + 1:]:
                    if c.close > sw.price:
                        broke_close = c
                        break
                if broke_close is not None:
                    kind = "BOS" if trend == "UP" else "CHoCH"
                    events.append(StructureEvent(
                        kind=kind, direction="UP",
                        level=sw.price, at=broke_close.bar_time,
                    ))
                    trend = "UP"
            last_high = sw
        else:  # LOW
            if last_low is not None:
                broke_close = None
                for c in candles[sw.idx + 1:]:
                    if c.close < sw.price:
                        broke_close = c
                        break
                if broke_close is not None:
                    kind = "BOS" if trend == "DOWN" else "CHoCH"
                    events.append(StructureEvent(
                        kind=kind, direction="DOWN",
                        level=sw.price, at=broke_close.bar_time,
                    ))
                    trend = "DOWN"
            last_low = sw

    return events, trend


def detect_liquidity_sweep(
    candles: list[Candle],
    swings: list[SwingPoint],
    atr: float,
    max_age_bars: int = 5,
) -> SweepEvent | None:
    """
    Sweep = wick menembus swing level lalu close balik ke sisi lain (rejection).
    Sweep-low (bullish, validasi LONG): low candle < swing LOW terakhir,
    tapi close kembali di atas level minus toleransi close-back.
    Sweep-high (bearish, validasi SHORT): mirror.
    Hanya sweep pada `max_age_bars` terakhir yang dianggap masih relevan
    (fail-soft: sweep lama tidak dipakai buat entry sekarang).
    """
    if not candles or atr <= 0:
        return None
    lows = [s for s in swings if s.kind == "LOW"]
    highs = [s for s in swings if s.kind == "HIGH"]
    n = len(candles)
    recent = candles[-max_age_bars:]
    start_idx = n - len(recent)

    best: SweepEvent | None = None
    for offset, c in enumerate(recent):
        idx = start_idx + offset
        # sweep-low: cari swing low sebelum candle ini
        prior_lows = [s for s in lows if s.idx < idx]
        if prior_lows:
            level = prior_lows[-1].price
            wick = c.low
            if wick < level:
                dip = level - wick
                if dip >= atr * STRUCT_SWEEP_WICK_MIN_ATR and c.close >= level - atr * STRUCT_SWEEP_CLOSE_BACK_MAX_ATR:
                    best = SweepEvent(
                        direction="LONG", swept_level=level,
                        wick_price=wick, close_price=c.close, at=c.bar_time,
                    )
        # sweep-high: cari swing high sebelum candle ini
        prior_highs = [s for s in highs if s.idx < idx]
        if prior_highs:
            level = prior_highs[-1].price
            wick = c.high
            if wick > level:
                spike = wick - level
                if spike >= atr * STRUCT_SWEEP_WICK_MIN_ATR and c.close <= level + atr * STRUCT_SWEEP_CLOSE_BACK_MAX_ATR:
                    best = SweepEvent(
                        direction="SHORT", swept_level=level,
                        wick_price=wick, close_price=c.close, at=c.bar_time,
                    )
    return best


def detect_fvg_zones(
    candles: list[Candle],
    atr: float,
    max_age_bars: int = 24,
) -> list[FVGZone]:
    """
    FVG 3-candle:
    - Bullish (LONG): candle[i-2].high < candle[i].low  → gap di tengah
    - Bearish (SHORT): candle[i-2].low > candle[i].high
    Hanya FVG dengan lebar ≥ 0.1×ATR dan ≤ 1.5×ATR, di max_age_bars terakhir.
    """
    if atr <= 0 or len(candles) < 3:
        return []
    out: list[FVGZone] = []
    start = max(2, len(candles) - max_age_bars)
    for i in range(start, len(candles)):
        c0, c2 = candles[i - 2], candles[i]
        # bullish gap
        if c0.high < c2.low:
            gap_lo, gap_hi = c0.high, c2.low
            w = gap_hi - gap_lo
            if 0.1 * atr <= w <= 1.5 * atr:
                out.append(FVGZone(
                    direction="LONG", low=gap_lo, high=gap_hi,
                    at=c2.bar_time, bar_idx=i,
                ))
        # bearish gap
        if c0.low > c2.high:
            gap_lo, gap_hi = c2.high, c0.low
            w = gap_hi - gap_lo
            if 0.1 * atr <= w <= 1.5 * atr:
                out.append(FVGZone(
                    direction="SHORT", low=gap_lo, high=gap_hi,
                    at=c2.bar_time, bar_idx=i,
                ))
    return out


def nearest_aligned_fvg(
    zones: list[FVGZone],
    direction: str,
    price: float,
    atr: float,
    max_dist_atr: float = 2.0,
) -> FVGZone | None:
    """FVG searah terdekat ke harga, dalam radius max_dist_atr."""
    aligned = [z for z in zones if z.direction == direction]
    if not aligned:
        return None
    best = None
    best_d = None
    for z in aligned:
        mid = (z.low + z.high) / 2.0
        d = abs(mid - price)
        if d > max_dist_atr * atr:
            continue
        if best is None or d < best_d:
            best, best_d = z, d
    return best


def build_structure_context(
    candles: list[Candle],
    atr: float,
) -> StructureContext | None:
    """
    Entry point tunggal structure detector. Return None kalau data kurang
    (fail-soft — caller fallback ke jalur ATR murni, TIDAK block sinyal).
    """
    if atr <= 0 or len(candles) < STRUCT_MIN_CANDLES:
        return None
    swings = find_swing_points(candles)
    if len(swings) < 2:
        return None
    events, trend = detect_structure_events(candles, swings)
    sweep = detect_liquidity_sweep(candles, swings, atr)
    fvgs = detect_fvg_zones(candles, atr)
    nearest = fvgs[-1] if fvgs else None
    return StructureContext(
        swings=swings, events=events, last_trend=trend,
        sweep=sweep, atr=atr, nearest_fvg=nearest, fvg_zones=fvgs or None,
    )


async def get_structure_context(
    symbol: str,
    atr: float,
    memory: "MemoryEngine | None" = None,
) -> StructureContext | None:
    """
    Fetch candle 15m + build structure context. Fail-soft penuh: exception
    apa pun (network/data) → None, caller lanjut pakai jalur ATR lama.
    """
    end_ms = int(now_utc().timestamp() * 1000)
    bar_sec = 15 * 60
    start_ms = end_ms - STRUCT_LOOKBACK_BARS * bar_sec * 1000
    try:
        candles = await hl_candle_snapshot(
            symbol, STRUCT_TIMEFRAME, start_ms, end_ms, memory,
        )
    except Exception as e:
        logger.debug("%s structure fetch fail-soft: %s", symbol, e)
        return None
    return build_structure_context(candles, atr)


async def htf_bias_gate(
    symbol: str,
    direction: str,
    memory: "MemoryEngine | None",
) -> tuple[bool, str]:
    """
    HTF 4H bias gate (P0 trading).
    LONG ditolak kalau 4H jelas DOWN (lower highs + lower lows).
    SHORT ditolak kalau 4H jelas UP.
    Fail-soft: data kurang → allow (True).
    """
    end_ms = int(now_utc().timestamp() * 1000)
    start_ms = end_ms - 30 * 4 * 3600 * 1000  # 30 bar 4H
    candles: list[Candle] = []
    pair = symbol if symbol.upper().endswith("USDT") else f"{symbol}USDT"
    # Binance REST is the faster HTF source; Hyperliquid remains the
    # fail-soft fallback for symbols not listed there or a transient outage.
    try:
        candles = await binance_klines(pair, "4h", limit=30, memory=memory)
    except Exception:
        candles = []
    if len(candles) < 30:
        try:
            candles = await hl_candle_snapshot(symbol, "4h", start_ms, end_ms, memory)
        except Exception:
            return True, "htf_na"
    if len(candles) < 30:
        return True, "htf_short"
    recent = candles[-30:]
    closes = [c.close for c in recent]
    highs = [c.high for c in recent]
    lows = [c.low for c in recent]
    h1, h2, h3 = max(highs[:10]), max(highs[10:20]), max(highs[20:30])
    l1, l2, l3 = min(lows[:10]), min(lows[10:20]), min(lows[20:30])
    c0, c2 = closes[0], closes[-1]
    trend = "RANGE"
    if h3 > h2 > h1 and l3 > l2 > l1 and c2 > c0:
        trend = "UP"
    elif h3 < h2 < h1 and l3 < l2 < l1 and c2 < c0:
        trend = "DOWN"
    if direction == "LONG" and trend == "DOWN":
        return False, f"htf_4h_down"
    if direction == "SHORT" and trend == "UP":
        return False, f"htf_4h_up"
    return True, f"htf_4h_{trend.lower()}"


async def process_trigger(
    asset: AssetCtx,
    trigger: TriggerHit,
    memory: "MemoryEngine | None",
    anchor: str | None,
    cascade_side: str | None = None,
    corr_map: dict[str, float] | None = None,
    hl_book: "HLBookBuffer | None" = None,
    atr_hint: float | None = None,
) -> PipelineSignal | None:
    """
    Satu trigger → L2→L6. Return PipelineSignal atau None (gagal gate).
    cascade_side: LONG/SHORT dari side liquidasi (BUY liq = short cascade pressure).
    hl_book: buffer L2 book HL (opsional). Kalau ada data segar → microstructure
    jadi real confirm (is_fallback=False), buka jalur real_count untuk Tier A/B.
    None / stale / no data → tetap fallback ke funding_proxy seperti semula.
    """
    horizon = classify_horizon(trigger)
    if not horizon:
        return None

    direction = trigger.direction_bias
    if direction == "NEUTRAL":
        if cascade_side in ("LONG", "SHORT"):
            direction = cascade_side
        else:
            # compression tanpa arah: skip sampai breakout confirm (L4 butuh direction)
            return None

    # HTF 4H bias gate — cascade adalah event-driven exception (§B2).
    if trigger.trigger_type != "cascade":
        htf_ok, htf_note = await htf_bias_gate(asset.symbol, direction, memory)
        if not htf_ok:
            logger.debug("%s HTF reject %s %s", asset.symbol, direction, htf_note)
            return None

    # L3 confirms
    # Dengan L2 book ter-wire: imbalance real dari HLBookBuffer (real confirm).
    # Tanpa data segar (buffer off/stale/symbol belum subscribed): fallback ke
    # funding-proxy (tetap ACTIVE, bukan "no data") supaya N≥2 bersama
    # cross_exchange. Flow off kalau wallets kosong (§X.2).
    hl_px = asset.mid_px or asset.mark_px
    ref_px, ref_src = await cross_exchange_price(asset.symbol, memory)

    imbalance = hl_book.get_imbalance(asset.symbol) if hl_book is not None else None
    micro = microstructure_confirm(direction, imbalance=imbalance, absorption=None)
    if micro.detail == "no data":
        fund_ok = (
            (direction == "SHORT" and asset.funding > 0)
            or (direction == "LONG" and asset.funding < 0)
        )
        micro = ConfirmResult(
            "microstructure",
            fund_ok,
            "funding_proxy" if fund_ok else "funding_proxy_no_align",
            is_fallback=True,  # echo funding, bukan book real
        )

    xref = cross_exchange_confirm(hl_px, ref_px, source=ref_src)
    confirms = [
        micro,
        flow_confirm(direction, netflow=None),
        xref,
    ]
    # Kalau semua xref mati (GH Actions block Binance/Bybit): OI structure
    # sebagai agent aktif ke-2 supaya L3 tidak stuck N=1.
    if xref.detail == "no xref data":
        confirms.append(oi_structure_confirm(direction, asset.funding, asset.open_interest))

    passed, conf_count, real_count, n_active = evaluate_confirms(confirms)
    if not passed:
        logger.debug(
            "%s L3 fail confirms=%d real=%d n=%d %s",
            asset.symbol, conf_count, real_count, n_active,
            [f"{r.agent}:{r.confirmed}" for r in confirms],
        )
        return None

    if memory is not None and corr_map:
        n_corr_active = memory.count_active_in_corr_cluster(
            corr_map, CORR_CLUSTER_THRESHOLD,
        )
        if n_corr_active >= MAX_CORRELATED_SIGNALS:
            logger.debug(
                "%s correlation_cluster_reject active=%d threshold=%.2f",
                asset.symbol, n_corr_active, CORR_CLUSTER_THRESHOLD,
            )
            return None

    atr = atr_hint if atr_hint is not None else await estimate_atr(
        asset.symbol, hl_px, memory,
    )
    if atr is None:
        return None

    # Fetch structure once and use it for both the fresh-sweep veto (#10)
    # and the broader trend/sweep consultation gate (#6).
    structure_quick = await get_structure_context(asset.symbol, atr, memory)
    if (
        structure_quick is not None
        and structure_quick.sweep is not None
        and trigger.trigger_type != "cascade"
        and structure_quick.sweep.direction != direction
    ):
        logger.debug(
            "%s sweep_oppose %s vs %s",
            asset.symbol, structure_quick.sweep.direction, direction,
        )
        return None

    if structure_quick is not None:
        event_direction = "UP" if direction == "LONG" else "DOWN"
        has_event = any(
            event.kind in {"BOS", "CHoCH"}
            and event.direction == event_direction
            for event in structure_quick.events
        )
        sweep_aligned = (
            structure_quick.sweep is not None
            and structure_quick.sweep.direction == direction
        )
        if horizon == "SCALPING" and not has_event:
            logger.debug(
                "%s structure_event_missing %s direction=%s horizon=%s",
                asset.symbol, event_direction, direction, horizon,
            )
            return None
        if horizon == "INTRADAY" and not (has_event or sweep_aligned):
            logger.debug(
                "%s structure_event_or_sweep_missing %s direction=%s",
                asset.symbol, event_direction, direction,
            )
            return None
        trend_opposes = (
            (direction == "LONG" and structure_quick.last_trend == "DOWN")
            or (direction == "SHORT" and structure_quick.last_trend == "UP")
        )
        if (
            trigger.trigger_type != "cascade"
            and trend_opposes
            and not sweep_aligned
        ):
            logger.debug("%s trend_oppose %s vs %s", asset.symbol, structure_quick.last_trend, direction)
            return None

    # §P1 — coba timing struktural dulu (SL di swing/sweep, entry di zona sweep).
    # Fail-soft: structure context tidak cukup / tidak ada swing searah →
    # fallback penuh ke analyst_timing ATR lama (tidak ada jalur silent-guess).
    timing = None
    structure = structure_quick
    if structure is not None:
        timing = analyst_timing_structural(
            direction, hl_px, atr, horizon, trigger.strength, conf_count, structure,
        )
    if timing is None:
        timing = analyst_timing(
            direction, hl_px, atr, horizon, trigger.strength, conf_count,
        )
    if timing is None:
        return None

    # L5 — macro FRED US10Y + Fear&Greed (fail-soft None)
    us10y = await fred_latest("DGS10") if _env_ok("FRED_API_KEY") else None
    fng = await fear_greed_latest()
    if corr_map is None and anchor:
        try:
            corr_map = await correlation_vs_anchor([asset.symbol], anchor, memory)
        except Exception as e:
            logger.debug("%s correlation fail-soft: %s", asset.symbol, type(e).__name__)
            corr_map = {}
    anchor_corr = (corr_map or {}).get(asset.symbol)
    sector = get_symbol_sector(memory, asset.symbol)
    ctx_mult = await context_modifier(
        asset.symbol, direction, anchor,
        us10y=us10y, fear_greed=fng, anchor_corr=anchor_corr,
        news_bias=rss_news_bias(asset.symbol),
        sector=sector,
        sector_bias=sector_news_bias(sector),
    )
    conf_final = clamp(timing.base_confidence * ctx_mult, 0.0, 1.0)

    # L6 veto + tier
    ok, reason = veto_checks(
        memory, asset.symbol, conf_final, setup_type=trigger.setup_type,
    )
    if not ok:
        logger.debug("%s L6 veto: %s", asset.symbol, reason)
        return None

    tier = assign_tier(timing.rr, real_count)
    sess_ok, sess_note = session_allows(horizon, tier)
    if not sess_ok:
        logger.debug("%s session reject · %s · tier %s", asset.symbol, sess_note, tier)
        return None
    lifetime_h = SIGNAL_LIFETIME_HOURS.get(horizon, 24)
    created = now_utc()
    valid_until = created + timedelta(hours=lifetime_h)
    sid = str(uuid.uuid4())

    reasoning = (
        f"{trigger.setup_type} | {trigger.trigger_type} "
        f"raw={trigger.raw_value:.6g} thr={trigger.threshold:.6g} "
        f"str={trigger.strength:.2f} | confirm {conf_count}/{n_active} "
        f"(real {real_count}) | "
        f"ctx×{ctx_mult:.2f} | sl={timing.sl_basis} entry={timing.entry_basis}"
    )

    return PipelineSignal(
        signal_id=sid,
        symbol=asset.symbol,
        direction=direction,
        horizon=horizon,
        tier=tier,
        setup_type=trigger.setup_type,
        trigger_type=trigger.trigger_type,
        entry_low=timing.entry_low,
        entry_high=timing.entry_high,
        stop_loss=timing.stop_loss,
        tp1=timing.tp1,
        tp2=timing.tp2,
        rr=timing.rr,
        confidence=conf_final,
        base_confidence=timing.base_confidence,
        context_mult=ctx_mult,
        confirm_count=conf_count,
        active_agent_count=n_active,
        reasoning=reasoning,
        created_at=created,
        valid_until=valid_until,
        sl_basis=timing.sl_basis,
        entry_basis=timing.entry_basis,
        state="PENDING",
    )


async def persist_signal(
    memory: MemoryEngine,
    sig: PipelineSignal,
    simulated: bool = False,
) -> None:
    """Dual-write signal_history + active_signals (satu flush)."""
    ts = sig.created_at.isoformat()
    exp = sig.valid_until.isoformat()
    await memory.enqueue_write("signal_history", {
        "signal_id": sig.signal_id,
        "symbol": sig.symbol,
        "direction": sig.direction,
        "horizon": sig.horizon,
        "tier": sig.tier,
        "entry_zone_low": sig.entry_low,
        "entry_zone_high": sig.entry_high,
        "stop_loss": sig.stop_loss,
        "original_stop": sig.stop_loss,
        "tp1": sig.tp1,
        "tp2": sig.tp2,
        "rr": sig.rr,
        "confidence": sig.confidence,
        "trigger_type": sig.trigger_type,
        "reasoning": sig.reasoning,
        "setup_type": sig.setup_type,
        "last_checked_at": ts,
        "created_at": ts,
        "valid_until": exp,
        "state": "PENDING",
        "is_simulated": 1 if simulated else 0,
    })
    await memory.enqueue_write("active_signals", {
        "signal_id": sig.signal_id,
        "symbol": sig.symbol,
        "expires_at": exp,
    })
    await memory.flush_writes()


async def run_universe_scan(
    memory: "MemoryEngine | None" = None,
    pinned: list[str] | None = None,
) -> tuple[list[AssetCtx], str | None]:
    """
    L0: fetch meta, filter candidates, pilih market anchor.
    Returns (candidates, anchor_symbol).
    """
    assets = await hl_meta_and_asset_ctxs(memory)
    candidates = filter_universe_candidates(assets, pinned=pinned)
    anchor = get_market_anchor(memory, assets)
    if memory is not None and anchor:
        memory.set_runtime("last_anchor", anchor)
    if memory is not None and candidates:
        # Sector tagging is optional enrichment, never part of the bootstrap
        # critical path. Keep it off unless explicitly enabled.
        existing: set[str] = set()
        if memory._conn:
            placeholders = ",".join("?" for _ in candidates)
            rows = memory._conn.execute(
                f"SELECT symbol FROM symbol_metadata WHERE symbol IN ({placeholders})",
                [a.symbol for a in candidates],
            ).fetchall()
            existing = {str(r["symbol"]) for r in rows}
        enable_sector_tag = (
            os.environ.get("CRYPTONE_ENABLE_SECTOR_TAG", "").strip() == "1"
        )
        if enable_sector_tag:
            gemini = GeminiClient(memory)
            if gemini.available():
                consecutive_sector_failures = 0
                for asset in candidates:
                    if asset.symbol in existing:
                        continue
                    if time.monotonic() < gemini._skip_until:
                        logger.warning(
                            "sector scan stopped · gemini skip window active"
                        )
                        break
                    try:
                        sector = await gemini.sector_tag(asset.symbol)
                    except Exception as e:
                        logger.debug(
                            "sector tag fail-soft %s: %s",
                            asset.symbol, type(e).__name__,
                        )
                        sector = None
                    if sector is None:
                        consecutive_sector_failures += 1
                        if consecutive_sector_failures >= 3:
                            logger.warning(
                                "gemini sector scan stopped · %d consecutive failures",
                                consecutive_sector_failures,
                            )
                            break
                    else:
                        consecutive_sector_failures = 0
                    if sector and memory._conn:
                        memory._conn.execute(
                            "INSERT INTO symbol_metadata (symbol, sector, updated_at) "
                            "VALUES (?, ?, ?) "
                            "ON CONFLICT(symbol) DO UPDATE SET sector=excluded.sector, "
                            "updated_at=excluded.updated_at",
                            (asset.symbol, sector, now_utc().isoformat()),
                        )
                if memory._conn:
                    memory._conn.commit()
    if memory is not None:
        # Bucket per 5 menit — konsisten dengan pola metric_atr supaya
        # run berulang / cycle rapat tidak inflate metric_funding.
        now_ts = now_utc()
        bucket_ts = now_ts.replace(second=0, microsecond=0) - timedelta(
            minutes=now_ts.minute % 5
        )
        ts = bucket_ts.isoformat()
        for a in candidates:
            await memory.enqueue_write("metric_funding", {
                "symbol": a.symbol,
                "rate": a.funding,
                "recorded_at": ts,
            })
        await memory.flush_writes()
    return candidates, anchor


async def run_pipeline_cycle(
    memory: MemoryEngine,
    pinned: list[str] | None = None,
    cascade_map: dict[str, float] | None = None,
    cascade_side_map: dict[str, str] | None = None,
    dry_run: bool = False,
) -> list[PipelineSignal]:
    """
    Satu cycle penuh L0–L6.
    cascade_map: symbol → USD cascade 5m (dari WS buffer; kosong = no cascade trigger).
    cascade_side_map: symbol → LONG|SHORT dominant liquidation bias.
    """
    cascade_map = cascade_map or {}
    cascade_side_map = cascade_side_map or {}
    try:
        candidates, anchor = await run_universe_scan(memory, pinned=pinned)
    except DataUnavailable as e:
        logger.error("L0 fail-soft skip cycle: %s", e)
        return []

    if not candidates:
        logger.info("L0: no candidates")
        return []

    # Keep HL book subscriptions in sync with this cycle's candidates only —
    # 178 universe symbols tidak perlu semua di-subscribe, cukup yang lolos L0.
    get_hl_book_buffer().set_symbols([a.symbol for a in candidates])

    # Compute once per cycle; process_trigger and black-swan share this map.
    corr_map: dict[str, float] = {}
    if anchor:
        try:
            corr_map = await correlation_vs_anchor(
                [a.symbol for a in candidates], anchor, memory,
            )
        except Exception as e:
            logger.debug("cycle correlation fail-soft: %s", type(e).__name__)

    # Black swan gate: freeze sinyal baru (§VII.3)
    bs = await check_black_swan(
        memory, anchor, candidates, cascade_map, corr_map=corr_map,
    )
    if bs.active:
        logger.warning("cycle skip new signals — BLACK SWAN %s", bs.indicators)
        return []
    elevated_tier_a_only = (bs.mode == "ELEVATED") or (
        (memory.get_runtime("black_swan_mode") or "") == "ELEVATED"
    )
    if elevated_tier_a_only:
        logger.info("elevated risk · Tier A only this cycle")

    setup_enabled = memory.get_setup_enabled()
    produced: list[PipelineSignal] = []

    # Market funding polarity (observability — bukan filter)
    n_fund_pos = sum(1 for a in candidates if a.funding > 0)
    n_fund_neg = sum(1 for a in candidates if a.funding < 0)
    n_fund_zero = len(candidates) - n_fund_pos - n_fund_neg

    # Rank by |funding| ekstrem dulu (hemat API ATR) — top slice
    # Multi-factor soft rank: |funding| + cascade boost (doc §III.4)
    def _rank_key(a: AssetCtx) -> float:
        fund_s = abs(a.funding) * 1e4
        cas_s = float((cascade_map or {}).get(a.symbol, 0.0)) / 1e6
        return fund_s + cas_s * 2.0
    ranked = sorted(candidates, key=_rank_key, reverse=True)

    # Diagnostic counters (L1 → L6)
    n_trig_long = 0
    n_trig_short = 0
    n_trig_neutral = 0
    n_gate_fail = 0
    n_cooldown = 0

    for asset in ranked:
        if len(produced) >= MAX_SIGNALS_PER_CYCLE:
            break
        if memory.count_active_signals() + len(produced) >= MAX_CONCURRENT_SIGNALS:
            break
        if memory.symbol_on_cooldown(asset.symbol):
            n_cooldown += 1
            continue
        if memory.has_active_symbol(asset.symbol):
            n_cooldown += 1  # still active → treat as blocked
            continue

        cas = cascade_map.get(asset.symbol, 0.0)
        # ATR opsional di L1 compression; estimasi cepat dari cold bila belum
        atr_guess = None
        hits = scan_triggers_for_asset(
            asset, atr_guess, cas, memory, setup_enabled=setup_enabled,
        )
        # Compression butuh ATR — fetch sekali kalau funding tidak fire
        if not hits:
            atr_guess = await estimate_atr(
                asset.symbol, asset.mid_px or asset.mark_px, memory,
            )
            hits = scan_triggers_for_asset(
                asset, atr_guess, cas, memory, setup_enabled=setup_enabled,
            )
        if not hits:
            continue

        for trigger in hits:
            bias = trigger.direction_bias
            if bias == "LONG":
                n_trig_long += 1
            elif bias == "SHORT":
                n_trig_short += 1
            else:
                n_trig_neutral += 1

            c_side = None
            if trigger.trigger_type == "cascade" or trigger.setup_type == "CASCADE_SCALP":
                c_side = (cascade_side_map or {}).get(asset.symbol)
            sig = await process_trigger(
                asset, trigger, memory, anchor, cascade_side=c_side,
                corr_map=corr_map, hl_book=get_hl_book_buffer(),
                atr_hint=atr_guess,   # ← hindari fetch ulang
            )
            if sig is None:
                n_gate_fail += 1
                continue
            if elevated_tier_a_only and sig.tier != "A":
                n_gate_fail += 1
                logger.debug("%s elevated skip tier %s", asset.symbol, sig.tier)
                continue
            if not dry_run:
                await persist_signal(memory, sig, simulated=False)
            # dry_run: NO DB write (§XIII.3) — sinyal tetap di-return untuk log
            produced.append(sig)
            break  # satu sinyal per symbol per cycle

    n_sig_long = sum(1 for s in produced if s.direction == "LONG")
    n_sig_short = sum(1 for s in produced if s.direction == "SHORT")

    # Circuit snapshot (transparan)
    circ = []
    for src in ("hyperliquid_rest", "binance_rest", "bybit_rest"):
        st = _circuit[src]
        if st.get("open_until") and time.monotonic() < st["open_until"]:
            circ.append(f"{src}=OPEN")
        else:
            circ.append(f"{src}=ok")
    logger.info(
        "📊 cycle · cand=%d · fund+/−/0=%d/%d/%d · "
        "trig L/S/N=%d/%d/%d · gate_fail=%d · cd=%d · "
        "sig=%d (L%d/S%d) · anchor=%s · %s",
        len(candidates), n_fund_pos, n_fund_neg, n_fund_zero,
        n_trig_long, n_trig_short, n_trig_neutral,
        n_gate_fail, n_cooldown,
        len(produced), n_sig_long, n_sig_short,
        anchor, " · ".join(circ),
    )
    return produced


# =============================================================================
# §8  BLACK SWAN DETECTOR
# =============================================================================
# 7 indikator, trigger 3/7 (§VII). Fail-soft: indikator tanpa data = diam.

@dataclass
class BlackSwanState:
    active: bool
    indicators: list[str]
    count: int
    mode: str  # NORMAL | BLACK_SWAN | ELEVATED
    started_at: datetime | None = None


def _ind_atr_spike(atr_now: float | None, atr_hist: list[float]) -> bool:
    """#1 ATR > P99."""
    if atr_now is None or len(atr_hist) < MIN_SAMPLE_DYNAMIC:
        return False
    return atr_now > percentile(atr_hist, 99)


def _ind_volume_cascade(vol_5m: float | None, vol_hist: list[float]) -> bool:
    """#2 Volume 5m > P99."""
    if vol_5m is None or len(vol_hist) < MIN_SAMPLE_DYNAMIC:
        return False
    return vol_5m > percentile(vol_hist, 99)


def _ind_correlation_break(
    anchor_drop_pct: float | None,
    top2_avg_drop_pct: float | None,
    *,
    threshold: float = 0.03,
) -> bool:
    """#3 Anchor OR top2 correlated avg *1h velocity* > 3% in same direction.

    B7 fix: caller passes 1h velocity, not 24h change. 24h ±3% is routine
    noise for crypto majors and was triggering false-positive BS states.
    """
    if anchor_drop_pct is not None and abs(anchor_drop_pct) > threshold:
        return True
    if (
        top2_avg_drop_pct is not None
        and abs(top2_avg_drop_pct) > threshold
        and anchor_drop_pct is not None
        and (anchor_drop_pct * top2_avg_drop_pct) > 0
    ):
        return True
    return False


def _ind_funding_flip(funding_now: float | None, funding_1h_ago: float | None) -> bool:
    """#4 Sign flip >0.1% (0.001 absolute rate)."""
    if funding_now is None or funding_1h_ago is None:
        return False
    if funding_now == 0 or funding_1h_ago == 0:
        return False
    if (funding_now > 0) == (funding_1h_ago > 0):
        return False
    return abs(funding_now - funding_1h_ago) > 0.001


def _ind_mega_cascade(cascade_usd_5m: float | None) -> bool:
    """#5 Liquidation ≥ $50M / 5m."""
    if cascade_usd_5m is None:
        return False
    return cascade_usd_5m >= 50_000_000


def _ind_cross_exchange_div(hl_px: float | None, xref_px: float | None) -> bool:
    """#6 HL vs ref >1%."""
    if hl_px is None or xref_px is None or xref_px <= 0:
        return False
    return abs(hl_px - xref_px) / xref_px > 0.01


def _ind_news_spike(headline_count: int | None, baseline_median: float | None) -> bool:
    """#7 Headline count ≥ 5× baseline (12 bucket)."""
    if headline_count is None or baseline_median is None or baseline_median <= 0:
        return False
    return headline_count >= 5 * baseline_median


def evaluate_black_swan_indicators(
    *,
    atr_now: float | None = None,
    atr_hist: list[float] | None = None,
    vol_5m: float | None = None,
    vol_hist: list[float] | None = None,
    anchor_drop_pct: float | None = None,
    top2_avg_drop_pct: float | None = None,
    funding_now: float | None = None,
    funding_1h_ago: float | None = None,
    cascade_usd_5m: float | None = None,
    hl_px: float | None = None,
    xref_px: float | None = None,
    headline_count: int | None = None,
    news_baseline: float | None = None,
) -> BlackSwanState:
    """
    Evaluasi 7 indikator. Tanpa data → indikator diam (fail-soft).
    Trigger: count >= BLACK_SWAN_TRIGGER_COUNT (3).
    """
    flags: list[tuple[str, bool]] = [
        ("atr_spike", _ind_atr_spike(atr_now, atr_hist or [])),
        ("volume_cascade", _ind_volume_cascade(vol_5m, vol_hist or [])),
        ("correlation_break", _ind_correlation_break(anchor_drop_pct, top2_avg_drop_pct)),
        ("funding_flip", _ind_funding_flip(funding_now, funding_1h_ago)),
        ("mega_cascade", _ind_mega_cascade(cascade_usd_5m)),
        ("cross_exchange_div", _ind_cross_exchange_div(hl_px, xref_px)),
        ("news_spike", _ind_news_spike(headline_count, news_baseline)),
    ]
    active_names = [name for name, on in flags if on]
    count = len(active_names)
    if count >= BLACK_SWAN_TRIGGER_COUNT:
        mode = "BLACK_SWAN"
        active = True
    elif count >= 2:
        mode = "ELEVATED"
        active = False
    else:
        mode = "NORMAL"
        active = False
    return BlackSwanState(
        active=active,
        indicators=active_names,
        count=count,
        mode=mode,
        started_at=now_utc() if active else None,
    )


async def check_black_swan(
    memory: MemoryEngine,
    anchor: str | None,
    candidates: list[AssetCtx],
    cascade_map: dict[str, float] | None = None,
    corr_map: dict[str, float] | None = None,
) -> BlackSwanState:
    """
    Satu pass detektor dari data yang sudah ada di cycle.
    Cascade/news penuh menyusul WS + RSS job.
    """
    cascade_map = cascade_map or {}
    atr_now = None
    atr_hist: list[float] = []
    funding_now = None
    hl_px = None
    xref_px = None
    cascade_total = sum(cascade_map.values()) if cascade_map else None

    funding_1h = None
    drops: dict[str, float] = {}
    if anchor:
        for a in candidates:
            if a.symbol == anchor:
                funding_now = a.funding
                hl_px = a.mid_px or a.mark_px
                break
        atr_hist = memory.get_atr_history(anchor, "1h")
        if atr_hist:
            atr_now = atr_hist[-1]
        xref_px, _ = await cross_exchange_price(anchor, memory)
        # funding ~1h ago from DB
        funding_1h = memory.get_funding_near(
            anchor, now_utc() - timedelta(hours=1), tolerance_min=90,
        )
        # correlation ranking still uses 24h drops (cheap, already have prev_day_px).
        for a in candidates[:15]:
            if a.symbol == anchor:
                continue
            px = a.mid_px or a.mark_px
            if a.prev_day_px and a.prev_day_px > 0 and px:
                drops[a.symbol] = (px - a.prev_day_px) / a.prev_day_px
        if corr_map is None:
            try:
                corr_map = await correlation_vs_anchor(
                    list(drops), anchor, memory,
                )
            except Exception as e:
                logger.debug("black-swan correlation fail-soft: %s", type(e).__name__)
                corr_map = {}

    # Volume 5m from 1m candles; also reused for anchor 1h velocity (B7 fix).
    vol_5m: float | None = None
    vol_hist: list[float] = []
    anchor_drop_1h: float | None = None
    if anchor:
        try:
            end_ms = int(now_utc().timestamp() * 1000)
            start_ms = end_ms - 4 * 3600 * 1000
            bars = await hl_candle_snapshot(anchor, "1m", start_ms, end_ms, memory)
            if bars:
                vol_5m = sum(bar.volume for bar in bars[-5:])
                for i in range(5, len(bars)):
                    vol_hist.append(sum(bar.volume for bar in bars[i - 5:i]))
                if len(bars) >= 60 and bars[-60].close > 0:
                    anchor_drop_1h = (
                        (bars[-1].close - bars[-60].close) / bars[-60].close
                    )
        except Exception as e:
            logger.debug("bs volume/velocity fail-soft: %s", type(e).__name__)

    # B7 fix: top-2 correlated 1h velocity (2 extra calls per cycle).
    top2_avg_1h: float | None = None
    if anchor and corr_map:
        ranked_c = sorted(corr_map.items(), key=lambda t: abs(t[1]), reverse=True)[:2]
        if len(ranked_c) >= 2:
            end_ms = int(now_utc().timestamp() * 1000)
            start_ms = end_ms - 3 * 3600 * 1000
            vel: list[float] = []
            for sym, _ in ranked_c:
                try:
                    sb = await hl_candle_snapshot(sym, "1h", start_ms, end_ms, memory)
                except Exception:
                    continue
                if len(sb) >= 2 and sb[-2].close > 0:
                    vel.append((sb[-1].close - sb[-2].close) / sb[-2].close)
            if len(vel) >= 2:
                top2_avg_1h = sum(vel) / len(vel)

    # news volume from DB (RSS job fills news_volume_5m)
    headline_count = None
    news_baseline = None
    if memory._conn:
        try:
            row = memory._conn.execute(
                "SELECT headline_count FROM news_volume_5m "
                "ORDER BY bucket_start DESC LIMIT 1"
            ).fetchone()
            if row:
                headline_count = int(row["headline_count"])
            rows = memory._conn.execute(
                "SELECT headline_count FROM news_volume_5m "
                "ORDER BY bucket_start DESC LIMIT 30"
            ).fetchall()
            if len(rows) >= 5:
                vals = sorted(float(r["headline_count"]) for r in rows)
                news_baseline = vals[len(vals) // 2]
        except Exception:
            pass

    state = evaluate_black_swan_indicators(
        atr_now=atr_now,
        atr_hist=atr_hist,
        vol_5m=vol_5m,
        vol_hist=vol_hist,
        cascade_usd_5m=cascade_total,
        funding_now=funding_now,
        funding_1h_ago=funding_1h,
        hl_px=hl_px,
        xref_px=xref_px,
        anchor_drop_pct=anchor_drop_1h,
        top2_avg_drop_pct=top2_avg_1h,
        headline_count=headline_count,
        news_baseline=news_baseline,
    )

    prev_mode = memory.get_runtime("black_swan_mode", "0") or "0"
    started_at = memory.get_runtime("black_swan_started_at")

    if state.active:
        logger.warning(
            "BLACK SWAN MODE indicators=%s count=%d",
            state.indicators, state.count,
        )
        memory.set_runtime("black_swan_mode", "1")
        if prev_mode != "1":
            memory.set_runtime("black_swan_started_at", now_utc().isoformat())
            memory.extend_signal_validity(extra_hours=6.0)
            # Flag untuk live loop kirim Telegram alert (hindari circular queue)
            memory.set_runtime(
                "black_swan_alert_pending",
                json.dumps({"indicators": state.indicators, "count": state.count}),
            )
            if memory._conn:
                memory._conn.execute(
                    "INSERT INTO blackswan_log "
                    "(triggered_indicators, trigger_count, started_at) "
                    "VALUES (?, ?, ?)",
                    (json.dumps(state.indicators), state.count, now_utc().isoformat()),
                )
                memory._conn.commit()
    elif state.mode == "ELEVATED":
        memory.set_runtime("black_swan_mode", "ELEVATED")
        logger.info("elevated risk · indicators=%s — Tier A only", state.indicators)
    else:
        # Auto-resume §VII.4: min 2h in BS before clear
        if prev_mode == "1" and started_at:
            try:
                st = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if st.tzinfo is None:
                    st = st.replace(tzinfo=UTC)
                hours = (now_utc() - st).total_seconds() / 3600.0
            except (ValueError, TypeError):
                hours = 99.0
            if hours < BLACK_SWAN_MIN_DURATION_H:
                # stay in BS until min duration even if indicators cooled
                memory.set_runtime("black_swan_mode", "1")
                state = BlackSwanState(
                    active=True,
                    mode="BLACK_SWAN",
                    indicators=state.indicators,
                    count=state.count,
                )
                logger.info(
                    "BLACK SWAN hold · %.1fh < min %dh",
                    hours, BLACK_SWAN_MIN_DURATION_H,
                )
            else:
                memory.set_runtime("black_swan_mode", "0")
                memory.set_runtime("black_swan_started_at", "")
                if memory._conn:
                    memory._conn.execute(
                        "UPDATE blackswan_log SET ended_at = ?, was_resumed = 1 "
                        "WHERE ended_at IS NULL",
                        (now_utc().isoformat(),),
                    )
                    memory._conn.commit()
                logger.info("BLACK SWAN resume · held %.1fh · indicators clear", hours)
                memory.set_runtime(
                    "black_swan_alert_pending",
                    json.dumps({"resume": True, "hours": round(hours, 1)}),
                )
        else:
            memory.set_runtime("black_swan_mode", "0")

    return state


# =============================================================================
# §9  CORRELATION ENGINE
# =============================================================================
# Pairwise return correlation vs market anchor. Dipakai BS #3 + L5 context.

def returns_from_prices(prices: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0:
            out.append((prices[i] - prices[i - 1]) / prices[i - 1])
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy)


async def fetch_close_series(
    symbol: str,
    interval: str = "1h",
    hours: int = 72,
    memory: "MemoryEngine | None" = None,
) -> list[float]:
    end_ms = int(now_utc().timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    try:
        candles = await hl_candle_snapshot(symbol, interval, start_ms, end_ms, memory)
        return [c.close for c in candles]
    except DataUnavailable:
        return []


async def correlation_vs_anchor(
    symbols: list[str],
    anchor: str,
    memory: "MemoryEngine | None" = None,
    top_n: int = 5,
) -> dict[str, float]:
    """
    Return dict symbol → corr vs anchor (sorted abs desc, top_n).
    Fail-soft: symbol tanpa data di-skip.
    """
    if not anchor or not symbols:
        return {}
    anchor_px = await fetch_close_series(anchor, memory=memory)
    anchor_ret = returns_from_prices(anchor_px)
    if len(anchor_ret) < 10:
        return {}

    scored: list[tuple[str, float]] = []
    for sym in symbols:
        if sym == anchor:
            continue
        px = await fetch_close_series(sym, memory=memory)
        ret = returns_from_prices(px)
        r = pearson(anchor_ret, ret)
        if r is not None:
            scored.append((sym, r))
    scored.sort(key=lambda t: abs(t[1]), reverse=True)
    return {s: r for s, r in scored[:top_n]}


def top2_correlated_drop(
    corr_map: dict[str, float],
    drops: dict[str, float],
) -> float | None:
    """Rata-rata drop 2 symbol paling berkorelasi (untuk BS #3)."""
    if not corr_map:
        return None
    ranked = sorted(corr_map.items(), key=lambda t: abs(t[1]), reverse=True)[:2]
    vals = [drops[s] for s, _ in ranked if s in drops]
    if len(vals) < 2:
        return None
    return sum(vals) / len(vals)


# =============================================================================
# §10  LLM CLIENT (Gemini)
# =============================================================================
# Math first, LLM second. Rate limit 15/min. Cache di llm_cache.

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_MODEL_CANDIDATES = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
)
GEMINI_MODEL_DISCOVERY_TTL_SEC = 3600
GEMINI_INTERACTIONS_MODEL = "models/gemini-3-flash-preview"

# Sector taxonomy — sumber tunggal, dipakai oleh parser dan prompt.
_ALLOWED_SECTORS: frozenset[str] = frozenset({
    "L1", "L2", "DEFI", "MEME", "AI",
    "GAMEFI", "INFRA", "STABLE", "RWA", "OTHER",
})


def _parse_sector_tag(text: str) -> str | None:
    """Ambil tag valid pertama dari response LLM. Return None kalau
    tidak ada token yang cocok dengan whitelist — mencegah 'The' / 'Layer'
    / potongan penjelasan tersimpan sebagai sector."""
    if not text:
        return None
    for token in text.replace(",", " ").replace(".", " ").split():
        t = token.strip("\"'`;:()[]!?*").upper()
        if t in _ALLOWED_SECTORS:
            return t
    return None


@dataclass
class LLMResult:
    text: str
    function_name: str
    latency_ms: float
    cached: bool = False
    success: bool = True
    error: str | None = None


def _llm_cache_key(function_name: str, prompt: str) -> str:
    h = hashlib.sha256(f"{function_name}:{prompt}".encode()).hexdigest()
    return f"{function_name}:{h[:32]}"


class GeminiClient:
    """Thin Gemini REST client — fail-soft, cached."""

    def __init__(self, memory: "MemoryEngine | None" = None):
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.memory = memory
        self._calls_this_minute = 0
        self._minute_start = time.monotonic()
        self._consecutive_failures = 0
        self._skip_until = 0.0
        self._model_name: str | None = None
        self._model_checked_at = 0.0
        self._sdk_client: Any | None = None
        self._sdk_import_failed = False

    def available(self) -> bool:
        return bool(self.api_key)

    def _get_interactions_client(self) -> Any | None:
        """Lazily load the official google-genai SDK when installed."""
        if self._sdk_import_failed:
            return None
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            from google import genai
            self._sdk_client = genai.Client(api_key=self.api_key)
        except (ImportError, ModuleNotFoundError):
            self._sdk_import_failed = True
            return None
        except Exception as e:
            self._sdk_import_failed = True
            logger.warning(
                "▸ gemini interactions client unavailable · %s",
                type(e).__name__,
            )
            return None
        return self._sdk_client

    @staticmethod
    def _interaction_text(interaction: Any) -> str:
        """Extract final text across google-genai SDK response shapes."""
        steps = (
            interaction.get("steps")
            if isinstance(interaction, dict)
            else getattr(interaction, "steps", None)
        ) or []
        for step in reversed(steps):
            values = []
            if isinstance(step, dict):
                values.extend([
                    step.get("text"),
                    step.get("output_text"),
                    step.get("content"),
                    step.get("output"),
                ])
            else:
                values.extend([
                    getattr(step, "text", None),
                    getattr(step, "output_text", None),
                    getattr(step, "content", None),
                    getattr(step, "output", None),
                ])
            for value in values:
                if isinstance(value, str) and value.strip():
                    return value.strip()
                nested = getattr(value, "text", None)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    async def _generate_interactions(
        self,
        function_name: str,
        prompt: str,
        *,
        temperature: float,
        max_tokens: int,
        use_search: bool = False,
        thinking_level: str = "low",
    ) -> LLMResult | None:
        """Use google-genai Interactions API; None means use REST fallback.

        use_search/thinking_level default hemat quota (no browsing, low
        reasoning) — cocok untuk task klasifikasi singkat seperti
        sector_tag. Set use_search=True hanya untuk task yang memang
        butuh konteks live (mis. narrative/news detection nanti)."""
        client = self._get_interactions_client()
        if client is None:
            return None
        t0 = time.monotonic()
        try:
            def _create():
                kwargs: dict = {
                    "model": GEMINI_INTERACTIONS_MODEL,
                    "input": prompt,
                    "generation_config": {
                        "temperature": temperature,
                        "max_output_tokens": max_tokens,
                        "top_p": 0.95,
                        "thinking_level": thinking_level,
                    },
                }
                if use_search:
                    kwargs["tools"] = [{"type": "google_search"}]
                return client.interactions.create(**kwargs)

            interaction = await asyncio.to_thread(_create)
            text = self._interaction_text(interaction)
            ms = (time.monotonic() - t0) * 1000
            if not text:
                return LLMResult(
                    "", function_name, ms,
                    success=False, error="empty interactions response",
                )
            return LLMResult(text, function_name, ms, success=True)
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            err_name = type(e).__name__
            logger.warning(
                "▸ gemini interactions fail-soft · %s · %s",
                function_name, err_name,
            )
            # Any failure counts — network blips, auth errors, rate limits.
            # 3+ consecutive → skip window, consistent dengan REST path.
            self._consecutive_failures += 1
            is_rate_limit = (
                "RateLimit" in err_name
                or "429" in str(e)
                or "TooManyRequests" in err_name
            )
            if is_rate_limit or self._consecutive_failures >= 3:
                self._skip_until = max(
                    self._skip_until, time.monotonic() + 120.0,
                )
            return LLMResult(
                "", function_name, ms,
                success=False, error=err_name,
            )

    async def _discover_model(
        self,
        *,
        force: bool = False,
        exclude: set[str] | None = None,
    ) -> str:
        """Pick a generateContent model available to this API key."""
        now = time.monotonic()
        excluded = exclude or set()
        if (
            not force
            and self._model_name
            and now - self._model_checked_at < GEMINI_MODEL_DISCOVERY_TTL_SEC
        ):
            return self._model_name

        selected: str | None = None
        try:
            raw = await http_get_json(
                f"{GEMINI_BASE}/models?key={self.api_key}",
                timeout=15.0,
            )
            available: set[str] = set()
            for item in raw.get("models") or []:
                name = str(item.get("name") or "").removeprefix("models/")
                methods = {
                    str(method) for method in (
                        item.get("supportedGenerationMethods") or []
                    )
                }
                if name and "generateContent" in methods:
                    available.add(name)

            for candidate in GEMINI_MODEL_CANDIDATES:
                if candidate in available and candidate not in excluded:
                    selected = candidate
                    break

            if selected is None:
                generic = sorted(
                    name for name in available
                    if "flash" in name.lower()
                    and "image" not in name.lower()
                    and name not in excluded
                )
                selected = generic[0] if generic else None
        except Exception as e:
            logger.warning("▸ gemini model discovery failed · %s", type(e).__name__)

        self._model_name = selected or GEMINI_MODEL
        self._model_checked_at = now
        logger.info("▸ gemini model selected · %s", self._model_name)
        return self._model_name

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        if now - self._minute_start >= 60:
            self._minute_start = now
            self._calls_this_minute = 0
        if now < self._skip_until:
            return False
        return self._calls_this_minute < 14  # margin di bawah 15

    def _cache_get(self, key: str) -> str | None:
        if not self.memory or not self.memory._conn:
            return None
        row = self.memory._conn.execute(
            "SELECT response, expires_at FROM llm_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        try:
            exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if now_utc() > exp:
                return None
        except ValueError:
            return None
        return row["response"]

    def _cache_put(self, key: str, function_name: str, response: str, ttl_h: float = 4.0) -> None:
        if not self.memory or not self.memory._conn:
            return
        exp = (now_utc() + timedelta(hours=ttl_h)).isoformat()
        self.memory._conn.execute(
            "INSERT OR REPLACE INTO llm_cache "
            "(cache_key, response, function_name, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, response, function_name, now_utc().isoformat(), exp),
        )
        self.memory._conn.commit()

    async def generate(
        self,
        function_name: str,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 512,
        use_cache: bool = True,
        cache_ttl_h: float = 4.0,
        use_search: bool = False,
        thinking_level: str = "low",
    ) -> LLMResult:
        if not self.available():
            return LLMResult("", function_name, 0, success=False, error="no api key")

        key = _llm_cache_key(function_name, prompt)
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                return LLMResult(cached, function_name, 0, cached=True)

        if not self._rate_ok():
            return LLMResult("", function_name, 0, success=False, error="rate limited")

        # Count every outbound model request before it is made, including SDK calls.
        self._calls_this_minute += 1
        sdk_result = await self._generate_interactions(
            function_name,
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            use_search=use_search,
            thinking_level=thinking_level,
        )
        if sdk_result is not None:
            if sdk_result.success and sdk_result.text:
                if use_cache:
                    self._cache_put(
                        key, function_name, sdk_result.text, ttl_h=cache_ttl_h,
                    )
                self._consecutive_failures = 0
                return sdk_result
            # REST uses the same key and will hit the same quota after SDK 429.
            if sdk_result.error and "RateLimit" in sdk_result.error:
                return sdk_result
            logger.warning(
                "▸ gemini REST fallback · %s",
                sdk_result.error or "interactions failed",
            )

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        t0 = time.monotonic()
        try:
            model = await self._discover_model()
            raw = None
            for attempt in range(2):
                url = (
                    f"{GEMINI_BASE}/models/{model}:generateContent"
                    f"?key={self.api_key}"
                )
                self._calls_this_minute += 1
                try:
                    raw = await http_post_json(url, body, timeout=30.0)
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 404 or attempt > 0:
                        raise
                    old_model = model
                    model = await self._discover_model(
                        force=True, exclude={old_model},
                    )
                    if model == old_model:
                        raise
                    logger.warning(
                        "▸ gemini model fallback · %s → %s",
                        old_model, model,
                    )
            assert raw is not None
            ms = (time.monotonic() - t0) * 1000
            text = ""
            for cand in raw.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    text += part.get("text") or ""
            if not text:
                return LLMResult("", function_name, ms, success=False, error="empty response")
            if use_cache:
                self._cache_put(key, function_name, text, ttl_h=cache_ttl_h)
            self._consecutive_failures = 0
            if self.memory is not None:
                await self.memory.enqueue_write("llm_call_log", {
                    "function_name": function_name,
                    "latency_ms": ms,
                    "success": 1,
                    "error_type": None,
                    "recorded_at": now_utc().isoformat(),
                })
            return LLMResult(text.strip(), function_name, ms, success=True)
        except urllib.error.HTTPError as e:
            ms = (time.monotonic() - t0) * 1000
            body_text = e.read().decode(errors="ignore")[:200]
            logger.warning(
                "▸ gemini http %s · %s · %s",
                e.code, function_name, body_text,
            )
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._skip_until = time.monotonic() + 60
                logger.warning(
                    "▸ gemini failure pause · %d consecutive failures · 60s",
                    self._consecutive_failures,
                )
            return LLMResult("", function_name, ms, success=False, error=str(e)[:80])
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            logger.warning("▸ gemini fail-soft · %s · %s", function_name, type(e).__name__)
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._skip_until = time.monotonic() + 60
                logger.warning(
                    "▸ gemini failure pause · %d consecutive failures · 60s",
                    self._consecutive_failures,
                )
            return LLMResult("", function_name, ms, success=False, error=str(e)[:80])

    async def sector_tag(self, symbol: str) -> str | None:
        """Tag sector kasar untuk universe (cache 24h).

        Response yang tidak cocok dengan whitelist DIBUANG dari cache
        supaya symbol bisa dicoba ulang di cycle berikutnya — bukan
        'teracuni 24 jam' oleh jawaban seperti 'The' atau 'Layer'."""
        prompt = (
            f"Classify the Hyperliquid perpetual '{symbol}' into exactly "
            f"ONE of these sector tags: L1, L2, DeFi, Meme, AI, GameFi, "
            f"Infra, Stable, RWA, Other.\n"
            f"Reply with ONLY the tag in uppercase, no other words."
        )
        r = await self.generate(
            "sector_tag", prompt, max_tokens=8, cache_ttl_h=24.0,
        )
        if not r.success or not r.text:
            return None
        tag = _parse_sector_tag(r.text)
        if tag is None:
            # Invalid response: drop cache entry so next call retries
            # instead of serving the same garbage for 24h.
            if self.memory and self.memory._conn:
                key = _llm_cache_key("sector_tag", prompt)
                self.memory._conn.execute(
                    "DELETE FROM llm_cache WHERE cache_key = ?", (key,),
                )
                self.memory._conn.commit()
            return None
        return tag


# =============================================================================
# §11  BOOTSTRAP / BACKTEST
# =============================================================================

async def hl_funding_history(
    coin: str,
    start_ms: int,
    end_ms: int | None = None,
    memory: "MemoryEngine | None" = None,
) -> list[tuple[datetime, float]]:
    """Historical funding rates. Return list (time_utc, rate)."""
    payload: dict[str, Any] = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_ms,
    }
    if end_ms is not None:
        payload["endTime"] = end_ms

    async def _call():
        return await http_post_json(HL_INFO_URL, payload)

    try:
        raw = await with_retry("hyperliquid_rest", _call, memory)
    except DataUnavailable:
        return []
    out: list[tuple[datetime, float]] = []
    if not isinstance(raw, list):
        return out
    for row in raw:
        try:
            t_ms = int(row["time"])
            rate = float(row.get("fundingRate") or row.get("funding") or 0)
            out.append((datetime.fromtimestamp(t_ms / 1000.0, tz=UTC), rate))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def run_bootstrap(days: int = 60) -> None:
    """
    Entry point --bootstrap (§XIII.3).
    Isi metric_funding + metric_atr top candidates → threshold P95/P20 real.
    """
    days = max(7, min(int(days), 90))
    db_path = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    logger.info("══════════════════════════════════════")
    logger.info("📥 BOOTSTRAP · %d days · db %s", days, db_path)
    logger.info("══════════════════════════════════════")

    memory = MemoryEngine(db_path)
    memory.start()

    try:
        candidates, anchor = await run_universe_scan(memory, pinned=None)
    except DataUnavailable as e:
        logger.error("Bootstrap abort · L0 fail: %s", e)
        memory.close()
        return

    if not candidates:
        logger.error("Bootstrap abort · no candidates")
        memory.close()
        return

    ranked = sorted(candidates, key=lambda a: a.day_ntl_vlm, reverse=True)
    targets: list[str] = []
    if anchor:
        targets.append(anchor)
    for a in ranked:
        if a.symbol not in targets:
            targets.append(a.symbol)
        if len(targets) >= 25:
            break

    end_ms = int(now_utc().timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    funding_rows = 0
    atr_rows = 0

    for i, sym in enumerate(targets, 1):
        logger.info("  ├─ [%d/%d] %s", i, len(targets), sym)
        hist = await hl_funding_history(sym, start_ms, end_ms, memory)
        for ts, rate in hist:
            await memory.enqueue_write("metric_funding", {
                "symbol": sym,
                "rate": rate,
                "recorded_at": ts.isoformat(),
                "is_simulated": 0,
            })
            funding_rows += 1
        try:
            candles = await hl_candle_snapshot(sym, "1h", start_ms, end_ms, memory)
            if len(candles) >= 15:
                for j in range(14, len(candles), 6):
                    window = candles[j - 14: j + 1]
                    atr = compute_atr(window, period=14)
                    if atr and atr > 0:
                        await memory.enqueue_write("metric_atr", {
                            "symbol": sym,
                            "timeframe": "1h",
                            "value": atr,
                            "recorded_at": candles[j].bar_time.isoformat(),
                            "is_simulated": 0,
                        })
                        atr_rows += 1
        except DataUnavailable as e:
            logger.warning("  │  ⚠ candles %s skip: %s", sym, e)
        await memory.flush_writes()
        await asyncio.sleep(0.35)

    if memory._conn:
        memory._conn.execute(
            "INSERT OR REPLACE INTO bootstrap_meta "
            "(id, bootstrapped_at, bootstrap_days) VALUES (1, ?, ?)",
            (now_utc().isoformat(), days),
        )
        memory._conn.commit()

    # Report dari DB, bukan counter lokal — INSERT OR IGNORE membuat
    # counter lokal menyesatkan saat run ulang di DB yang sama.
    n_fund = n_atr = 0
    if memory._conn:
        n_fund = int(memory._conn.execute(
            "SELECT COUNT(*) FROM metric_funding"
        ).fetchone()[0])
        n_atr = int(memory._conn.execute(
            "SELECT COUNT(*) FROM metric_atr"
        ).fetchone()[0])
    logger.info(
        "📥 BOOTSTRAP DONE · symbols=%d · funding_total=%d (+%d attempted) · "
        "atr_total=%d (+%d attempted) · anchor=%s",
        len(targets), n_fund, funding_rows, n_atr, atr_rows, anchor,
    )
    memory.close()


def _env_ok(key: str) -> bool:
    v = os.environ.get(key, "").strip()
    return bool(v)


FRED_BASE = "https://api.stlouisfed.org/fred"


async def probe_fred() -> tuple[bool, str]:
    """Hit FRED series DGS10 (US10Y) — zero cost, validate key."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        return False, "no key"
    url = (
        f"{FRED_BASE}/series/observations"
        f"?series_id=DGS10&api_key={key}&file_type=json&sort_order=desc&limit=1"
    )
    try:
        raw = await http_get_json(url, timeout=12.0)
        obs = (raw.get("observations") or [])
        if not obs:
            return False, "empty observations"
        v = obs[0].get("value", ".")
        if v in (".", "", None):
            return True, "key ok (stale point)"
        return True, f"US10Y={v}"
    except Exception as e:
        return False, type(e).__name__


async def fred_latest(series_id: str) -> float | None:
    """Ambil observasi terakhir numeric. Fail-soft → None."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        return None
    url = (
        f"{FRED_BASE}/series/observations"
        f"?series_id={series_id}&api_key={key}&file_type=json"
        f"&sort_order=desc&limit=5"
    )
    try:
        raw = await http_get_json(url, timeout=12.0)
        for obs in raw.get("observations") or []:
            v = obs.get("value")
            if v not in (".", "", None):
                return float(v)
    except Exception:
        return None
    return None


# Fear & Greed — alternative.me (free, no key). TTL cache 15m.
_FNG_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}
_FNG_TTL_SEC = 900.0
FNG_URL = "https://api.alternative.me/fng/?limit=1"



# §V.3 #8 — thin RSS headline volume (BS#7 + L5 news bias input)
RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
)
_RSS_LAST_POLL = 0.0
_RSS_LAST_COUNT = 0
_RSS_CACHE_BIAS: str | None = None

_RSS_BULL_PATTERN = re.compile(
    r"\b(surge[sd]?|rally|rallies|rallied|soar(?:s|ed)?|jump(?:s|ed)?|"
    r"etf|approval|approved|all[- ]?time high|ath|bull|bullish|breakout|"
    r"inflow|inflows|adoption|partnership|listing|launch|upgrade|"
    r"milestone|record high)\b",
    re.IGNORECASE,
)
_RSS_BEAR_PATTERN = re.compile(
    r"\b(hack(?:ed|s)?|exploit(?:ed|s)?|breach|sec|securities|lawsuit|"
    r"sues|sued|ban(?:ned|s)?|crash(?:es|ed)?|plunge[sd]?|dump(?:s|ed)?|"
    r"bear|bearish|outflow|outflows|delist(?:ed)?|fraud|rug(?:ged)?|scam|"
    r"liquidat(?:ed|ion|ions)?|halt(?:s|ed)?|freeze|frozen)\b",
    re.IGNORECASE,
)

_RECENT_HEADLINES: list[tuple[float, str]] = []   # (mono_ts, title)
_RSS_HEADLINE_TTL_SEC = 3600.0
_RSS_SECTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI":     ("ai", "artificial intelligence", "llm", "agent", "agi"),
    "MEME":   ("meme", "doge", "shib", "pepe", "bonk", "wif"),
    "DEFI":   ("defi", "dex", "lending", "yield", "amm", "uniswap", "aave"),
    "L1":     ("layer 1", "layer-1", "l1", "ethereum", "solana", "avalanche"),
    "L2":     ("layer 2", "layer-2", "l2", "rollup", "optimism", "arbitrum", "base"),
    "GAMEFI": ("gamefi", "gaming", "play-to-earn", "p2e", "metaverse"),
    "RWA":    ("rwa", "real world asset", "tokenized", "tokenization"),
    "INFRA":  ("infra", "oracle", "chainlink", "indexing"),
    "STABLE": ("stablecoin", "usdc", "usdt", "dai"),
}
_SECTOR_BIAS_CACHE: dict[str, str] = {}


def _score_headline(title: str) -> int:
    """+1 bullish, -1 bearish, 0 neutral/ambiguous. Word-boundary matched."""
    if not title:
        return 0
    bull = len(_RSS_BULL_PATTERN.findall(title))
    bear = len(_RSS_BEAR_PATTERN.findall(title))
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def _symbol_mentioned(title: str, symbol: str) -> bool:
    """Whole-word match, case-insensitive. Strips HL 1000x prefix."""
    if not symbol or not title:
        return False
    ticker = symbol.upper()
    if ticker.startswith("1000") and len(ticker) > 4:
        ticker = ticker[4:]   # 1000PEPE → PEPE
    return bool(re.search(rf"\b{re.escape(ticker)}\b", title, re.IGNORECASE))


def _recompute_sector_bias() -> None:
    """Aggregate recent headlines per sector into _SECTOR_BIAS_CACHE."""
    global _SECTOR_BIAS_CACHE
    agg: dict[str, list[int]] = defaultdict(list)
    for _, title in _RECENT_HEADLINES:
        score = _score_headline(title)
        if score == 0:
            continue
        tl = title.lower()
        for sector, kws in _RSS_SECTOR_KEYWORDS.items():
            if any(kw in tl for kw in kws):
                agg[sector].append(score)
    out: dict[str, str] = {}
    for sector, scores in agg.items():
        total = sum(scores)
        if total >= 2:
            out[sector] = "BULLISH"
        elif total <= -2:
            out[sector] = "BEARISH"
    _SECTOR_BIAS_CACHE = out


async def poll_rss_headline_count(memory: "MemoryEngine | None" = None) -> int:
    """
    Poll 4 RSS feeds, count entries newer than ~30m.
    Fail-soft: network error → return last count / 0.
    Rate: max 1 poll / 5 menit.
    """
    global _RSS_LAST_POLL, _RSS_LAST_COUNT, _RSS_CACHE_BIAS, _RECENT_HEADLINES
    now_m = time.monotonic()
    if now_m - _RSS_LAST_POLL < 300 and _RSS_LAST_POLL > 0:
        return _RSS_LAST_COUNT

    def _sync_poll() -> tuple[int, str | None, list[tuple[float, str]]]:
        try:
            import feedparser  # type: ignore
            import calendar
        except ImportError:
            return 0, None, []
        now_epoch = time.time()
        cutoff = now_epoch - 1800  # 30 menit
        total = bull = bear = 0
        titles: list[tuple[float, str]] = []
        seen: set[str] = set()
        for url in RSS_FEEDS:
            try:
                feed = feedparser.parse(url)
            except Exception:
                continue
            for e in getattr(feed, "entries", [])[:40]:
                title = (getattr(e, "title", "") or "").strip()
                if not title:
                    continue
                # dedup by guid/link/title
                uid = (
                    getattr(e, "id", None)
                    or getattr(e, "link", None)
                    or title.lower()
                )
                uid = str(uid).strip().lower()
                if uid in seen:
                    continue
                seen.add(uid)
                # published age filter
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    st = getattr(e, key, None)
                    if st:
                        try:
                            published = calendar.timegm(st)
                        except (TypeError, ValueError, OverflowError):
                            published = None
                        if published:
                            break
                if published is not None and published < cutoff:
                    continue
                # no published → count as recent (fail-soft include)
                ts = float(published) if published else now_epoch
                total += 1
                titles.append((ts, title))
                s = _score_headline(title)
                if s > 0:
                    bull += 1
                elif s < 0:
                    bear += 1
        bias = None
        if bull > bear + 2:
            bias = "BULLISH"
        elif bear > bull + 2:
            bias = "BEARISH"
        return total, bias, titles

    try:
        count, bias, titles = await asyncio.to_thread(_sync_poll)
    except Exception as e:
        logger.debug("RSS poll fail-soft: %s", type(e).__name__)
        return _RSS_LAST_COUNT

    _RSS_LAST_POLL = now_m
    _RSS_LAST_COUNT = count
    _RSS_CACHE_BIAS = bias
    # Rolling merge: keep prior headlines still inside TTL
    merged: list[tuple[float, str]] = []
    seen_t: set[str] = set()
    for ts, title in list(titles) + list(_RECENT_HEADLINES):
        if now_m - ts > _RSS_HEADLINE_TTL_SEC and ts < now_m:
            # ts is epoch; now_m is monotonic — compare carefully
            pass
        key = title.lower()
        if key in seen_t:
            continue
        seen_t.add(key)
        merged.append((ts if ts > 1e9 else now_m, title))
    # Prefer epoch titles from this poll; drop stale by wall clock via count cap
    _RECENT_HEADLINES = merged[:200]
    _recompute_sector_bias()

    if memory is not None:
        bucket = now_utc().replace(second=0, microsecond=0)
        bucket = bucket - timedelta(minutes=bucket.minute % 5)
        await memory.enqueue_write("news_volume_5m", {
            "bucket_start": bucket.isoformat(),
            "headline_count": int(count),
            "recorded_at": now_utc().isoformat(),
        })
        await memory.flush_writes()
    return count


def rss_news_bias(symbol: str | None = None) -> str | None:
    """
    Sentiment bias from recent crypto headlines.

    symbol provided → only headlines mentioning that symbol (whole-word).
    No symbol-specific headlines found → falls back to global market bias.
    No symbol → market-wide bias (legacy behavior).
    """
    if symbol:
        now_m = time.monotonic()
        scores: list[int] = []
        for ts, title in _RECENT_HEADLINES:
            if now_m - ts > _RSS_HEADLINE_TTL_SEC:
                continue
            if _symbol_mentioned(title, symbol):
                scores.append(_score_headline(title))
        if scores:
            total = sum(scores)
            if total >= 2:
                return "BULLISH"
            if total <= -2:
                return "BEARISH"
            # Ambiguous symbol-level signal → fall through to global.
    return _RSS_CACHE_BIAS


def sector_news_bias(sector: str | None) -> str | None:
    """Sector-level sentiment from recent headlines (populated per RSS poll)."""
    if not sector:
        return None
    return _SECTOR_BIAS_CACHE.get(sector)


async def fear_greed_latest() -> float | None:
    """
    Crypto Fear & Greed Index 0–100. Fail-soft → None.
    Cache 15 menit biar 1 cycle tidak spam API per symbol.
    """
    now = time.monotonic()
    if _FNG_CACHE["value"] is not None and (now - _FNG_CACHE["ts"]) < _FNG_TTL_SEC:
        return _FNG_CACHE["value"]
    try:
        raw = await http_get_json(FNG_URL, timeout=10.0)
        data = (raw.get("data") or [])
        if not data:
            return None
        val = float(data[0].get("value"))
        _FNG_CACHE["value"] = val
        _FNG_CACHE["ts"] = now
        return val
    except Exception:
        return _FNG_CACHE["value"]  # stale ok


def _src_icon(ok: bool | None) -> str:
    if ok is True:
        return "✅"
    if ok is False:
        return "❌"
    return "⚪"


async def probe_sources() -> dict[str, dict[str, Any]]:
    """
    Probe semua source yang relevan. Transparent status untuk log.
    ok: True/False/None (None = not configured / not probed).
    """
    status: dict[str, dict[str, Any]] = {}

    # --- Env / keys (no network) ---
    if _env_ok("GEMINI_API_KEY"):
        status["gemini"] = {"ok": True, "note": f"key set · model={GEMINI_MODEL}"}
    else:
        status["gemini"] = {"ok": False, "note": "GEMINI_API_KEY missing"}
    has_token = _env_ok("CRYPTONE_TELEGRAM_BOT_TOKEN")
    has_user = _env_ok("CRYPTONE_USER_CHAT_ID")
    has_op = _env_ok("CRYPTONE_OPERATOR_CHAT_ID")
    if has_token and has_user and has_op:
        tg_ok, tg_note = True, "token+user+operator"
    elif has_token and (has_user or has_op):
        tg_ok, tg_note = True, "token+1 chat (live prefers both)"
    elif has_token:
        tg_ok, tg_note = False, "token set · chat missing"
    else:
        tg_ok, tg_note = False, "token missing"
    status["telegram"] = {"ok": tg_ok, "note": tg_note}
    status["etherscan"] = {
        "ok": True if _env_ok("ETHERSCAN_API_KEY") else None,
        "note": "key set" if _env_ok("ETHERSCAN_API_KEY") else "optional, no key",
    }
    if _env_ok("FRED_API_KEY"):
        try:
            t0 = time.monotonic()
            fred_ok, fred_note = await probe_fred()
            ms = (time.monotonic() - t0) * 1000
            status["fred"] = {"ok": fred_ok, "note": f"{fred_note} · {ms:.0f}ms"}
        except Exception as e:
            status["fred"] = {"ok": False, "note": str(e)[:50]}
    else:
        status["fred"] = {
            "ok": None,
            "note": "no FRED_API_KEY in env (add to live.yml)",
        }
    status["wallets"] = {
        "ok": True if _env_ok("CRYPTONE_EXCHANGE_WALLETS") else None,
        "note": "set" if _env_ok("CRYPTONE_EXCHANGE_WALLETS") else "flow agent off",
    }

    # --- Hyperliquid (critical) ---
    t0 = time.monotonic()
    try:
        assets = await hl_meta_and_asset_ctxs(memory=None)
        ms = (time.monotonic() - t0) * 1000
        status["hyperliquid"] = {
            "ok": bool(assets),
            "note": f"universe={len(assets)} {ms:.0f}ms" if assets else "empty",
            "assets": assets,
        }
    except Exception as e:
        status["hyperliquid"] = {"ok": False, "note": str(e)[:80], "assets": []}

    # --- Binance / Bybit (cross-check) ---
    t0 = time.monotonic()
    try:
        if not _circuit_allow("binance_rest"):
            status["binance"] = {"ok": False, "note": "circuit OPEN"}
        else:
            px = await binance_premium_price("BTCUSDT", memory=None)
            ms = (time.monotonic() - t0) * 1000
            status["binance"] = {
                "ok": px is not None,
                "note": f"BTC={px:.1f} {ms:.0f}ms" if px else f"unreachable {ms:.0f}ms",
            }
    except Exception as e:
        status["binance"] = {"ok": False, "note": str(e)[:60]}

    t0 = time.monotonic()
    try:
        if not _circuit_allow("bybit_rest"):
            status["bybit"] = {"ok": False, "note": "circuit OPEN"}
        else:
            px = await bybit_ticker_price("BTCUSDT", memory=None)
            ms = (time.monotonic() - t0) * 1000
            status["bybit"] = {
                "ok": px is not None,
                "note": f"BTC={px:.1f} {ms:.0f}ms" if px else f"unreachable {ms:.0f}ms",
            }
    except Exception as e:
        status["bybit"] = {"ok": False, "note": str(e)[:60]}

    # --- Fear & Greed (free, no key) ---
    t0 = time.monotonic()
    try:
        fng = await fear_greed_latest()
        ms = (time.monotonic() - t0) * 1000
        if fng is not None:
            label = (
                "Extreme Fear" if fng <= 25 else
                "Fear" if fng <= 45 else
                "Neutral" if fng <= 55 else
                "Greed" if fng <= 75 else
                "Extreme Greed"
            )
            status["fear_greed"] = {
                "ok": True,
                "note": f"{fng:.0f} {label} · {ms:.0f}ms",
            }
        else:
            status["fear_greed"] = {"ok": False, "note": f"empty · {ms:.0f}ms"}
    except Exception as e:
        status["fear_greed"] = {"ok": False, "note": str(e)[:50]}

    # --- Cascade WS (status; connects on --live) ---
    buf = get_cascade_buffer()
    if buf.connected:
        status["binance_ws_liq"] = {"ok": True, "note": buf.status_note()}
    elif buf.last_error:
        status["binance_ws_liq"] = {"ok": False, "note": buf.status_note()}
    else:
        status["binance_ws_liq"] = {
            "ok": None,
            "note": "buffer ready · connects on --live",
        }

    # --- HL L2 book WS (status; connects on --live) ---
    hl_buf = get_hl_book_buffer()
    if hl_buf.connected:
        status["hl_ws"] = {"ok": True, "note": hl_buf.status_note()}
    elif hl_buf.last_error:
        status["hl_ws"] = {"ok": False, "note": hl_buf.status_note()}
    else:
        status["hl_ws"] = {
            "ok": None,
            "note": "buffer ready · connects on --live",
        }

    # --- Not yet wired (honest) ---
    if _RSS_LAST_POLL > 0:
        status["rss_news"] = {
            "ok": True,
            "note": f"{_RSS_LAST_COUNT} headlines · bias={_RSS_CACHE_BIAS or 'n/a'}",
        }
    else:
        status["rss_news"] = {"ok": None, "note": "polls on --live"}
    status["cryptopanic"] = {"ok": None, "note": "not wired"}
    status["defillama"] = {"ok": None, "note": "not wired"}

    return status


def log_source_banner(status: dict[str, dict[str, Any]]) -> None:
    """
    Tree log modern — branch per grup source.
    ✅ hidup · ❌ gagal · ⚪ skip/belum wired
    """
    def note(name: str) -> str:
        st = status.get(name) or {}
        return str(st.get("note") or "")[:42]

    def leaf(name: str, label: str, last: bool = False, pipe: str = "│") -> str:
        st = status.get(name) or {}
        icon = _src_icon(st.get("ok"))
        br = "└─" if last else "├─"
        return f"  {pipe}  {br} {icon} {label} · {note(name)}"

    n_ok = sum(1 for s in status.values() if s.get("ok") is True)
    n_fail = sum(1 for s in status.values() if s.get("ok") is False)
    n_skip = sum(1 for s in status.values() if s.get("ok") is None)

    lines = [
        "📡 SOURCES",
        "  ├─ 📈 MARKET",
        leaf("hyperliquid", "Hyperliquid"),
        leaf("binance", "Binance"),
        leaf("bybit", "Bybit"),
        leaf("binance_ws_liq", "Binance WS liq"),
        leaf("hl_ws", "HL WS book", last=True),
        "  ├─ 🧠 AI / MACRO",
        leaf("gemini", "Gemini"),
        leaf("fred", "FRED"),
        leaf("fear_greed", "Fear&Greed"),
        leaf("etherscan", "Etherscan"),
        leaf("wallets", "Wallets", last=True),
        "  ├─ 📨 DELIVERY",
        leaf("telegram", "Telegram", last=True),
        "  └─ 📰 NEWS (soon)",
        leaf("rss_news", "RSS", pipe=" "),
        leaf("cryptopanic", "CryptoPanic", pipe=" "),
        leaf("defillama", "DefiLlama", last=True, pipe=" "),
        f"  ── {n_ok} live · {n_fail} down · {n_skip} skip",
    ]
    for ln in lines:
        logger.info(ln)



async def run_preflight() -> bool:
    """Entry point --check. Validasi env + koneksi critical source (§XIII.3)."""
    logger.info("Preflight start")
    status = await probe_sources()
    log_source_banner(status)

    missing = []
    if not status.get("gemini", {}).get("ok"):
        missing.append("GEMINI_API_KEY")
    if missing:
        logger.error("Missing required env: %s", ", ".join(missing))
        return False

    hl = status.get("hyperliquid", {})
    if not hl.get("ok"):
        logger.error("Critical source hyperliquid FAIL: %s", hl.get("note"))
        return False

    assets = hl.get("assets") or []
    candidates = filter_universe_candidates(assets)
    anchor = get_market_anchor(None, assets)
    logger.info(
        "Preflight OK · universe=%d · candidates=%d · anchor=%s",
        len(assets), len(candidates), anchor,
    )
    xref_ok = status.get("binance", {}).get("ok") or status.get("bybit", {}).get("ok")
    if not xref_ok:
        logger.warning(
            "Cross-exchange FAIL (Binance+Bybit) — L3 pakai oi_structure fallback"
        )
    return True


# =============================================================================
# §12  TELEGRAM UI — Super App (tombol, bukan command text)
# =============================================================================
# Master §XII.1 Menu Utama pakai tombol. User ketuk → callback_query.
# Text /start /menu tetap dibuka sebagai entry, isinya keyboard.

TELEGRAM_API = "https://api.telegram.org"

# Offset getUpdates (module-level, single process)
_TG_UPDATE_OFFSET = 0


def _kb_main_menu() -> dict:
    """§XII.1 Menu Utama — InlineKeyboard."""
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Radar", "callback_data": "menu:radar"},
                {"text": "🌍 Market", "callback_data": "menu:market"},
            ],
            [
                {"text": "🚨 Black Swan", "callback_data": "menu:blackswan"},
                {"text": "📈 Breadth", "callback_data": "menu:breadth"},
            ],
            [
                {"text": "📓 Journal", "callback_data": "menu:journal"},
                {"text": "📊 Performance", "callback_data": "menu:performance"},
            ],
            [
                {"text": "🎯 Accuracy", "callback_data": "menu:accuracy"},
                {"text": "⚙️ Settings", "callback_data": "menu:settings"},
            ],
            [
                {"text": "🏠 Menu", "callback_data": "menu:home"},
            ],
        ]
    }


def _kb_signal(signal_id: str) -> dict:
    """§XII.2 tombol di kartu sinyal."""
    sid = (signal_id or "")[:36]
    return {
        "inline_keyboard": [
            [
                {"text": "🧠 Reasoning", "callback_data": f"sig:reason:{sid}"},
                {"text": "📓 Journal", "callback_data": "menu:journal"},
            ],
            [
                {"text": "📊 Radar", "callback_data": "menu:radar"},
                {"text": "🏠 Menu", "callback_data": "menu:home"},
            ],
        ]
    }


def _kb_back() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "⬅️ Kembali", "callback_data": "menu:home"}],
        ]
    }


def format_signal_message(s: "PipelineSignal") -> str:
    """Pesan sinyal Telegram §XII.2 — bersih, mobile-friendly."""
    arrow = "🟢" if s.direction == "LONG" else "🔴"
    state = getattr(s, "state", "PENDING") or "PENDING"
    sl_b = getattr(s, "sl_basis", "atr") or "atr"
    en_b = getattr(s, "entry_basis", "atr") or "atr"
    plain = (
        f"{arrow} {s.symbol}  {s.direction}\n"
        f"{s.horizon} · Tier {s.tier} · {s.setup_type}\n"
        f"status {state} · SL {sl_b} · entry {en_b}\n"
        f"\n"
        f"Entry  {s.entry_low:.6g} – {s.entry_high:.6g}\n"
        f"SL     {s.stop_loss:.6g}\n"
        f"TP1    {s.tp1:.6g}\n"
        f"TP2    {s.tp2:.6g}\n"
        f"\n"
        f"R:R 1:{s.rr:.2f}  ·  conf {s.confidence:.0%}\n"
        f"confirms {s.confirm_count}/{s.active_agent_count}"
        f"  ·  {s.trigger_type}"
    )
    if abs(s.context_mult - 1.0) >= 0.01:
        plain += f"  ·  ctx×{s.context_mult:.2f}"
    return plain


def format_main_menu_text() -> str:
    return (
        "🤖 Cryptone V4.5 — Market Radar\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "Ketuk tombol di bawah.\n"
        "Sinyal real-time tetap push otomatis."
    )


def _fmt_radar(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    if not rows:
        return (
            "📊 Radar — aktif\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "Tidak ada sinyal aktif.\n"
            "Push otomatis saat setup lolos gate."
        )
    # sort Tier A first
    order = {"A": 0, "B": 1, "C": 2}
    rows = sorted(rows, key=lambda r: order.get(str(r.get("tier") or "C"), 9))
    lines = [
        "📊 Radar — sinyal aktif",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    for r in rows[:12]:
        arrow = "🟢" if str(r.get("direction")).upper() == "LONG" else "🔴"
        st = str(r.get("state") or "PENDING")
        lines.append(
            f"{arrow} {r.get('symbol')} {r.get('direction')} · "
            f"Tier {r.get('tier')} · {st}\n"
            f"   {r.get('setup_type')} · R:R 1:{float(r.get('rr') or 0):.2f}"
        )
    lines.append(f"\nTotal aktif: {len(rows)}")
    return "\n".join(lines)


def _fmt_market(memory: "MemoryEngine") -> str:
    anchor = memory.get_last_anchor() or "—"
    bs = memory.get_runtime("black_swan_mode", "0") or "0"
    bs_label = {
        "1": "🚨 BLACK_SWAN",
        "ELEVATED": "⚠️ ELEVATED",
    }.get(bs, "✅ NORMAL")
    n_act = memory.count_active_signals()
    fund_n = 0
    atr_n = 0
    if memory._conn:
        try:
            fund_n = int(memory._conn.execute(
                "SELECT COUNT(*) AS n FROM metric_funding"
            ).fetchone()["n"])
            atr_n = int(memory._conn.execute(
                "SELECT COUNT(*) AS n FROM metric_atr"
            ).fetchone()["n"])
        except Exception:
            pass
    fng = None
    try:
        # cache module may hold last F&G
        from __main__ import fear_greed_latest  # noqa — avoid; use runtime
    except Exception:
        pass
    return (
        "🌍 Market Snapshot\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"Anchor     : {anchor}\n"
        f"Black swan : {bs_label}\n"
        f"Active sig : {n_act}\n"
        f"Funding samples : {fund_n}\n"
        f"ATR samples     : {atr_n}\n"
        f"\n"
        f"Bias RSS  : {rss_news_bias() or 'n/a'}"
    )


def _fmt_blackswan(memory: "MemoryEngine") -> str:
    mode = memory.get_runtime("black_swan_mode", "0") or "0"
    started = memory.get_runtime("black_swan_started_at") or "—"
    if mode == "1":
        label = "🚨 ACTIVE — BLACK SWAN"
    elif mode == "ELEVATED":
        label = "⚠️ ELEVATED (Tier A only)"
    else:
        label = "✅ NORMAL"
    lines = [
        "🚨 Black Swan Status",
        "━━━━━━━━━━━━━━━━━━━",
        f"Mode    : {label}",
        f"Since   : {started[:19] if started and started != '—' else '—'}",
    ]
    if memory._conn:
        try:
            row = memory._conn.execute(
                "SELECT triggered_indicators, trigger_count, started_at "
                "FROM blackswan_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                inds = row["triggered_indicators"]
                lines.append(f"Last    : {inds}")
                lines.append(f"Count   : {row['trigger_count']}")
        except Exception:
            pass
    return "\n".join(lines)


def _fmt_journal(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    lines = [
        "📓 Trade Journal — active",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if not rows:
        lines.append("(kosong)")
    for r in rows[:10]:
        arrow = "🟢" if str(r.get("direction")).upper() == "LONG" else "🔴"
        lines.append(
            f"{arrow} {r.get('symbol')} {r.get('direction')} · "
            f"{r.get('state')} · Tier {r.get('tier')}\n"
            f"   Entry {r.get('entry_zone_low')}–{r.get('entry_zone_high')} · "
            f"SL {r.get('stop_loss')}"
        )
    # recent resolved
    if memory._conn:
        try:
            recent = memory._conn.execute(
                "SELECT symbol, direction, outcome, realized_rr, resolved_at "
                "FROM signal_history WHERE outcome IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT 5"
            ).fetchall()
            if recent:
                lines.append("\n— Recent closed —")
                for r in recent:
                    rr = r["realized_rr"]
                    rr_s = f" R:R {rr:.2f}" if rr is not None else ""
                    lines.append(
                        f"{r['outcome']} {r['symbol']} {r['direction']}{rr_s}"
                    )
        except Exception:
            pass
    return "\n".join(lines)


def _fmt_performance(memory: "MemoryEngine") -> str:
    lines = [
        "📊 Performance — setup",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if not memory._conn:
        lines.append("(no db)")
        return "\n".join(lines)
    try:
        rows = memory._conn.execute(
            "SELECT setup_type, total_signals, wins, losses, partials, avg_rr "
            "FROM setup_performance ORDER BY total_signals DESC"
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        lines.append("Belum ada sample resolved.")
        return "\n".join(lines)
    for r in rows:
        n = int(r["total_signals"] or 0)
        w = int(r["wins"] or 0)
        p = int(r["partials"] or 0)
        wr = ((w + PARTIAL_WEIGHT * p) / n * 100) if n else 0
        avg = r["avg_rr"]
        avg_s = f"{avg:.2f}" if avg is not None else "—"
        lines.append(
            f"🎯 {r['setup_type']}\n"
            f"   n={n} · WR {wr:.0f}% · avg R:R {avg_s}"
        )
    return "\n".join(lines)


def _fmt_accuracy(memory: "MemoryEngine") -> str:
    lines = [
        "🎯 Accuracy Tracker",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if not memory._conn:
        return "\n".join(lines) + "\n(no db)"
    try:
        row = memory._conn.execute(
            "SELECT "
            "SUM(CASE WHEN outcome='PROFIT' THEN 1 ELSE 0 END) AS w, "
            "SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS l, "
            "SUM(CASE WHEN outcome='PARTIAL' THEN 1 ELSE 0 END) AS p, "
            "SUM(CASE WHEN outcome='NEUTRAL' THEN 1 ELSE 0 END) AS n, "
            "COUNT(*) AS t, "
            "AVG(realized_rr) AS avg_rr "
            "FROM signal_history WHERE outcome IS NOT NULL AND is_simulated=0"
        ).fetchone()
    except Exception:
        row = None
    if not row or not row["t"]:
        lines.append("Belum ada outcome.")
        return "\n".join(lines)
    t = int(row["t"])
    w, l, p, n = int(row["w"] or 0), int(row["l"] or 0), int(row["p"] or 0), int(row["n"] or 0)
    wr = (w + PARTIAL_WEIGHT * p) / t * 100
    avg = row["avg_rr"]
    avg_s = f"{avg:.2f}" if avg is not None else "—"
    lines.extend([
        f"Total resolved : {t}",
        f"PROFIT {w} · LOSS {l} · PARTIAL {p} · NEUTRAL {n}",
        f"Win rate (w+0.5p) : {wr:.1f}%",
        f"Avg realized R:R  : {avg_s}",
    ])
    return "\n".join(lines)


def _fmt_settings_view(memory: "MemoryEngine") -> str:
    lines = [
        "⚙️ Settings (read-only view)",
        "━━━━━━━━━━━━━━━━━━━",
        f"Cooldown cooldown : {SIGNAL_COOLDOWN_MIN}m",
        f"Post-loss box  : {POST_LOSS_COOLDOWN_MIN}m",
        f"Max concurrent : {MAX_CONCURRENT_SIGNALS}",
        f"Max / cycle    : {MAX_SIGNALS_PER_CYCLE}",
        f"Session WIB    : {SESSION_START_HOUR_WIB}:00–{SESSION_END_HOUR_WIB}:00",
        f"Entry zone ATR : {ENTRY_ZONE_ATR_WIDTH}×",
        "",
        "Tuning via whitelist §II.6 — Fase B tombol preset.",
    ]
    paused = memory.get_runtime("signals_paused_until")
    if paused:
        lines.append(f"⏸ signals paused until {paused[:19]}")
    return "\n".join(lines)


def _fmt_breadth(memory: "MemoryEngine") -> str:
    """Rough breadth from last funding snapshot signs in DB."""
    lines = [
        "📈 Market Breadth (funding)",
        "━━━━━━━━━━━━━━━━━━━",
    ]
    if not memory._conn:
        return "\n".join(lines) + "\n(no db)"
    try:
        # latest funding per symbol (approximate via max recorded_at group)
        rows = memory._conn.execute(
            """
            SELECT symbol, rate FROM metric_funding
            WHERE recorded_at = (
                SELECT MAX(recorded_at) FROM metric_funding f2
                WHERE f2.symbol = metric_funding.symbol
            )
            """
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        lines.append("Belum ada sample funding.")
        return "\n".join(lines)
    pos = sum(1 for r in rows if float(r["rate"]) > 0)
    neg = sum(1 for r in rows if float(r["rate"]) < 0)
    zero = len(rows) - pos - neg
    lines.extend([
        f"Symbols  : {len(rows)}",
        f"Funding+ : {pos}  (crowded long → short bias)",
        f"Funding− : {neg}  (crowded short → long bias)",
        f"Flat     : {zero}",
    ])
    return "\n".join(lines)


def _fmt_reasoning(memory: "MemoryEngine", signal_id: str) -> str:
    if not memory._conn or not signal_id:
        return "Reasoning tidak tersedia."
    row = memory._conn.execute(
        "SELECT symbol, direction, reasoning, setup_type, tier "
        "FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return f"Signal {signal_id[:8]}… tidak ditemukan."
    return (
        f"🧠 Reasoning — {row['symbol']} {row['direction']}\n"
        f"Tier {row['tier']} · {row['setup_type']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{row['reasoning'] or '(kosong)'}"
    )


def resolve_menu_callback(
    data: str,
    memory: "MemoryEngine",
) -> tuple[str, dict | None]:
    """
    callback_data → (text, reply_markup).
    Semua menu pakai tombol; tidak ada command text yang wajib diingat user.
    """
    data = (data or "").strip()
    if data in ("menu:home", "menu:start", ""):
        return format_main_menu_text(), _kb_main_menu()
    if data == "menu:radar":
        return _fmt_radar(memory), _kb_back()
    if data == "menu:market":
        return _fmt_market(memory), _kb_back()
    if data == "menu:blackswan":
        return _fmt_blackswan(memory), _kb_back()
    if data == "menu:journal":
        return _fmt_journal(memory), _kb_back()
    if data == "menu:performance":
        return _fmt_performance(memory), _kb_back()
    if data == "menu:accuracy":
        return _fmt_accuracy(memory), _kb_back()
    if data == "menu:settings":
        return _fmt_settings_view(memory), _kb_back()
    if data == "menu:breadth":
        return _fmt_breadth(memory), _kb_back()
    if data.startswith("sig:reason:"):
        sid = data.split(":", 2)[-1]
        return _fmt_reasoning(memory, sid), _kb_back()
    return "Tombol tidak dikenal. Ketuk 🏠 Menu.", _kb_main_menu()


class TelegramBot:
    """Bot API thin client — sendMessage + getUpdates + callback answer."""

    def __init__(self, token: str | None = None):
        self.token = (token or os.environ.get("CRYPTONE_TELEGRAM_BOT_TOKEN", "")).strip()

    def available(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.token}/{method}"

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
        reply_markup: dict | None = None,
    ) -> bool:
        if not self.available() or not chat_id:
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            raw = await http_post_json(self._url("sendMessage"), payload, timeout=20.0)
            if not raw.get("ok"):
                desc = raw.get("description", "unknown")
                if "Too Many Requests" in str(desc) or raw.get("error_code") == 429:
                    params = raw.get("parameters") or {}
                    retry = float(params.get("retry_after", 5))
                    raise TelegramRateLimitError(
                        f"telegram 429 retry={retry}", retry_after=retry,
                    )
                logger.warning("▸ telegram send fail · chat=%s · %s", chat_id, desc)
                return False
            return True
        except TelegramRateLimitError:
            raise
        except Exception as e:
            logger.warning("▸ telegram error · %s", type(e).__name__)
            return False

    async def answer_callback(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        if not self.available() or not callback_query_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
            payload["show_alert"] = show_alert
        try:
            await http_post_json(self._url("answerCallbackQuery"), payload, timeout=10.0)
        except Exception:
            pass

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> bool:
        if not self.available():
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            raw = await http_post_json(self._url("editMessageText"), payload, timeout=15.0)
            return bool(raw.get("ok"))
        except Exception:
            return False

    async def get_updates(self, offset: int = 0, timeout: int = 0) -> list[dict]:
        if not self.available():
            return []
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        try:
            raw = await http_post_json(self._url("getUpdates"), payload, timeout=timeout + 10)
            if not raw.get("ok"):
                return []
            return list(raw.get("result") or [])
        except Exception as e:
            logger.debug("getUpdates fail-soft: %s", type(e).__name__)
            return []


def _allowed_chat_ids() -> set[str]:
    ids = set()
    for k in ("CRYPTONE_USER_CHAT_ID", "CRYPTONE_OPERATOR_CHAT_ID"):
        v = os.environ.get(k, "").strip()
        if v:
            ids.add(v)
    return ids


async def handle_telegram_updates(
    bot: TelegramBot,
    memory: "MemoryEngine",
    *,
    dry_run: bool = False,
    long_poll_sec: int = 0,
) -> int:
    """
    Poll getUpdates, proses /start + callback tombol.
    long_poll_sec>0 → Telegram long-poll (respons tombol ~detik, bukan per cycle).
    """
    global _TG_UPDATE_OFFSET
    if not bot.available() or dry_run:
        return 0
    allowed = _allowed_chat_ids()
    updates = await bot.get_updates(
        offset=_TG_UPDATE_OFFSET, timeout=max(0, int(long_poll_sec)),
    )
    n = 0
    for upd in updates:
        uid = int(upd.get("update_id") or 0)
        if uid >= _TG_UPDATE_OFFSET:
            _TG_UPDATE_OFFSET = uid + 1

        # --- callback tombol ---
        cq = upd.get("callback_query")
        if cq:
            cq_id = str(cq.get("id") or "")
            data = str(cq.get("data") or "")
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {})
            chat_id = str(chat.get("id") or "")
            mid = msg.get("message_id")
            if allowed and chat_id not in allowed:
                await bot.answer_callback(cq_id, "Unauthorized", show_alert=True)
                continue
            # Jawab dulu biar spinner Telegram hilang instan
            await bot.answer_callback(cq_id)
            text, markup = resolve_menu_callback(data, memory)
            if mid is not None:
                ok = await bot.edit_message(chat_id, int(mid), text, reply_markup=markup)
                if not ok:
                    await bot.send_message(chat_id, text, reply_markup=markup)
            else:
                await bot.send_message(chat_id, text, reply_markup=markup)
            n += 1
            continue

        # --- text message: /start /menu saja → buka keyboard ---
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        body = (msg.get("text") or "").strip()
        if not chat_id or not body:
            continue
        if allowed and chat_id not in allowed:
            continue
        low = body.lower()
        if low in ("/start", "/menu", "menu", "start"):
            await bot.send_message(
                chat_id,
                format_main_menu_text(),
                reply_markup=_kb_main_menu(),
            )
            n += 1
        elif low.startswith("/"):
            # Arahkan ke tombol — jangan biarkan user hafal command list
            await bot.send_message(
                chat_id,
                "Pakai tombol di menu ya — lebih cepat.\n"
                "Ketuk /menu kalau keyboard hilang.",
                reply_markup=_kb_main_menu(),
            )
            n += 1
    return n


async def telegram_ui_loop(
    bot: TelegramBot,
    memory: "MemoryEngine",
    stop: asyncio.Event,
    *,
    dry_run: bool = False,
    poll_sec: int = 25,
) -> None:
    """
    Background long-poll getUpdates — tombol respons ~1–3s, bukan nunggu cycle 5m.
    WAL + check_same_thread=False → read path aman paralel dengan cycle utama.
    """
    logger.info("📲 telegram UI loop · long-poll %ds", poll_sec)
    while not stop.is_set():
        try:
            n = await handle_telegram_updates(
                bot, memory, dry_run=dry_run, long_poll_sec=poll_sec,
            )
            if n:
                logger.info("📲 telegram UI · handled %d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("telegram UI loop: %s", type(e).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
    logger.info("📲 telegram UI loop · stopped")


async def sleep_with_ui(
    bot: TelegramBot,
    memory: "MemoryEngine",
    total_sec: float,
    *,
    dry_run: bool = False,
    slice_sec: float = 2.0,
) -> None:
    """Fallback: pecah sleep cycle jadi slice + short poll (kalau UI loop mati)."""
    end = time.monotonic() + max(0.0, total_sec)
    while time.monotonic() < end:
        if not dry_run and bot.available():
            try:
                await handle_telegram_updates(
                    bot, memory, dry_run=dry_run, long_poll_sec=0,
                )
            except Exception:
                pass
        left = end - time.monotonic()
        if left <= 0:
            break
        await asyncio.sleep(min(slice_sec, left))


# §13  DELIVERY QUEUE
# =============================================================================

class TelegramDeliveryQueue:
    def __init__(self):
        self._queue: asyncio.Queue[TelegramMessage] = asyncio.Queue()

    async def enqueue(self, message: TelegramMessage) -> None:
        await self._queue.put(message)

    def empty(self) -> bool:
        return self._queue.empty()

    async def drain_once(
        self,
        bot: TelegramBot,
        *,
        dry_run: bool = False,
        min_interval: float = 1.1,
    ) -> int:
        """Kirim semua pesan tertunda. Return jumlah terkirim."""
        sent = 0
        while not self._queue.empty():
            msg = self._queue.get_nowait()
            if dry_run:
                logger.info(
                    "▸ DRY-RUN → chat %s | %s",
                    msg.chat_id, msg.text.replace("\n", " · ")[:120],
                )
                sent += 1
                continue
            try:
                ok = await bot.send_message(
                    msg.chat_id,
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=msg.reply_markup,
                )
                if ok:
                    sent += 1
                    logger.info("📤 SENT · %s", msg.text.split("\n")[0][:90])
                else:
                    logger.warning("▸ SEND FAIL → chat %s", msg.chat_id)
            except TelegramRateLimitError as e:
                logger.warning("▸ telegram rate limit · sleep %.0fs", e.retry_after)
                await asyncio.sleep(e.retry_after)
                await self._queue.put(msg)
                break
            await asyncio.sleep(min_interval)
        return sent

# =============================================================================
# §14  run_live_service + recovery
# =============================================================================

async def run_live_service(dry_run: bool = False) -> None:
    """
    Entry point --live / --dry-run (§XIII.3, §XVII).
    Loop cycle sampai max_runtime; SIGTERM flush bersih.
    """
    db_path = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    cycle_sec = int(os.environ.get("CRYPTONE_CYCLE_INTERVAL_SEC", str(DEFAULT_CYCLE_INTERVAL_SEC)))
    max_runtime = int(os.environ.get("CRYPTONE_MAX_RUNTIME_SEC", str(DEFAULT_MAX_RUNTIME_SEC)))
    pinned = parse_symbol_list(os.environ.get("CRYPTONE_PINNED_SYMBOLS", ""))

    logger.info("══════════════════════════════════════")
    logger.info("🚀 CRYPTONE V%s  ·  LIVE", PROTOCOL_VERSION)
    logger.info("   cycle %ss · max %ss · dry_run=%s", cycle_sec, max_runtime, dry_run)
    logger.info("   db %s", db_path)
    logger.info("══════════════════════════════════════")

    # Source banner sekali di startup
    status = await probe_sources()
    log_source_banner(status)
    if not status.get("hyperliquid", {}).get("ok"):
        logger.error("Abort: Hyperliquid critical FAIL")
        return

    # Integrity gate (§XVIII.5) — corrupt → restore backup → fresh schema
    if Path(db_path).exists():
        healthy = await startup_db_check(db_path)
        if not healthy:
            logger.error("DB integrity FAIL · %s", db_path)
            if not restore_from_previous_artifact():
                db_path = create_fresh_db()
            elif not await startup_db_check(db_path):
                logger.error("DB still corrupt after restore · rebuild")
                db_path = create_fresh_db()

    memory = MemoryEngine(db_path)
    memory.start()
    queue = TelegramDeliveryQueue()
    bot = TelegramBot()
    if bot.available():
        logger.info("🤖 telegram ready")
    else:
        logger.warning("🤖 telegram OFF · missing token")

    # Cascade WS (Binance forceOrder) — background, fail-soft, independent of Telegram
    buf = get_cascade_buffer()
    buf.bind_memory(memory)
    buf.start()
    logger.info("🌊 cascade buffer · starting Binance forceOrder WS")

    # HL L2 book WS — background, fail-soft, real microstructure confirm
    hl_buf = get_hl_book_buffer()
    hl_buf.start()
    logger.info("📖 HL book buffer · starting l2Book WS")

    # Telegram Super App UI — long-poll terpisah biar tombol tidak nunggu cycle 5m
    ui_stop = asyncio.Event()
    ui_task: asyncio.Task | None = None
    if bot.available() and not dry_run:
        ui_task = asyncio.create_task(
            telegram_ui_loop(bot, memory, ui_stop, dry_run=dry_run, poll_sec=25),
            name="telegram_ui",
        )
        logger.info("📲 telegram UI · background long-poll on")

    loop = asyncio.get_event_loop()

    def _sigterm_handler():
        logger.info("SIGTERM — flush & exit")
        loop.create_task(flush_and_exit(memory, queue, timeout=20))

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except NotImplementedError:
        pass

    await startup_recovery(memory)

    # Bootstrap: dari artifact workflow ATAU auto-fill di sini (tutup celah DB kosong)
    bs = memory.get_bootstrap_info()
    if bs:
        logger.info(
            "📥 bootstrap · %sd · fund=%d · atr=%d · at %s",
            bs["bootstrap_days"], bs["funding_samples"], bs["atr_samples"],
            str(bs["bootstrapped_at"])[:19],
        )
    else:
        auto_days = int(os.environ.get("CRYPTONE_AUTO_BOOTSTRAP_DAYS", "0") or "0")
        if auto_days > 0:
            logger.warning(
                "📥 bootstrap · BELUM di DB · auto-bootstrap %sd (artifact miss)",
                auto_days,
            )
            memory.close()
            await run_bootstrap(days=auto_days)
            memory = MemoryEngine(db_path)
            memory.start()
            bs = memory.get_bootstrap_info()
            if bs:
                logger.info(
                    "📥 bootstrap · auto OK · %sd · fund=%d · atr=%d",
                    bs["bootstrap_days"], bs["funding_samples"], bs["atr_samples"],
                )
            else:
                logger.error("📥 bootstrap · auto GAGAL · threshold tetap cold-start")
        else:
            logger.warning(
                "📥 bootstrap · BELUM · set CRYPTONE_AUTO_BOOTSTRAP_DAYS "
                "atau jalankan workflow Bootstrap"
            )

    t0 = time.monotonic()
    cycle_n = 0
    total_signals = 0
    total_sent = 0
    try:
        while True:
            elapsed = time.monotonic() - t0
            if elapsed >= max_runtime:
                logger.info(
                    "🏁 max-runtime · %dm · cycles=%d · signals=%d · sent=%d",
                    int(elapsed // 60), cycle_n, total_signals, total_sent,
                )
                break
            cycle_n += 1
            rem = max_runtime - elapsed
            logger.info(
                "🔄 CYCLE %d · ⏱ %dm · left %dm · active %d",
                cycle_n, int(elapsed // 60), int(rem // 60),
                memory.count_active_signals(),
            )
            try:
                # Outcome tracker — free slot sebelum scan baru
                # (tombol dilayani telegram_ui_loop background)
                closed_ev = await monitor_active_signals(memory)
                user_chat = os.environ.get("CRYPTONE_USER_CHAT_ID", "").strip()
                op_chat = os.environ.get("CRYPTONE_OPERATOR_CHAT_ID", "").strip()
                for ev in closed_ev:
                    reason = ev.get("reason") or ""
                    if reason == "TP1":
                        # Notifikasi TP1 (SL→BE) ke user — jangan skip total
                        px_s = f" @ {ev['px']:.6g}" if ev.get("px") else ""
                        plain_tp1 = (
                            f"🎯 TP1 {ev['symbol']} {ev['direction']}{px_s}\n"
                            f"SL → breakeven · sisa posisi → TP2"
                        )
                        for chat in {c for c in (user_chat, op_chat) if c}:
                            await queue.enqueue(TelegramMessage(
                                chat_id=chat, text=plain_tp1, priority="normal",
                            ))
                        continue
                    icon = {
                        "PROFIT": "✅", "LOSS": "🛑",
                        "PARTIAL": "🎯", "NEUTRAL": "⏱",
                    }.get(ev["outcome"], "•")
                    rr_s = ""
                    if ev.get("realized_rr") is not None:
                        rr_s = f" · R:R {ev['realized_rr']:.2f}"
                    px_s = f" @ {ev['px']:.6g}" if ev.get("px") else ""
                    plain_c = (
                        f"{icon} CLOSE {ev['symbol']} {ev['direction']} · "
                        f"{ev['outcome']} · {reason}{px_s}{rr_s}"
                    )
                    # User selalu dapat close; operator copy (jika beda)
                    targets_c = []
                    if user_chat:
                        targets_c.append(user_chat)
                    if op_chat and op_chat != user_chat:
                        targets_c.append(op_chat)
                    if not targets_c and op_chat:
                        targets_c.append(op_chat)
                    for chat in targets_c:
                        await queue.enqueue(TelegramMessage(
                            chat_id=chat,
                            text=plain_c,
                            priority="normal",
                        ))

                # Black swan Telegram alert (set by check_black_swan)
                raw_bs = memory.get_runtime("black_swan_alert_pending")
                if raw_bs:
                    memory.set_runtime("black_swan_alert_pending", "")
                    try:
                        payload = json.loads(raw_bs)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                    if payload.get("resume"):
                        plain_bs = (
                            f"✅ BLACK SWAN cleared\n"
                            f"held {payload.get('hours', '?')}h · indicators normal"
                        )
                    else:
                        inds = payload.get("indicators") or []
                        plain_bs = (
                            f"🚨 BLACK SWAN MODE\n"
                            f"indicators: {', '.join(inds)}\n"
                            f"count {payload.get('count', '?')} · "
                            f"active signals extended +6h"
                        )
                    for chat in {c for c in (user_chat, op_chat) if c}:
                        await queue.enqueue(TelegramMessage(
                            chat_id=chat, text=plain_bs, priority="critical",
                        ))

                # thin RSS (fail-soft, max 1/5m) — feeds BS#7 + L5 bias
                try:
                    n_head = await poll_rss_headline_count(memory)
                    if n_head:
                        logger.debug("📰 RSS · %d headlines · bias=%s", n_head, rss_news_bias())
                except Exception as e:
                    logger.debug("RSS skip: %s", type(e).__name__)

                buf = get_cascade_buffer()
                c_map = buf.snapshot_usd()
                c_side = buf.snapshot_side()
                if c_map:
                    top = sorted(c_map.items(), key=lambda x: -x[1])[:5]
                    top_s = ", ".join(f"{s}=${u/1e6:.2f}M" for s, u in top)
                    logger.info(
                        "🌊 cascade 5m · %d sym · top %s · ws=%s",
                        len(c_map), top_s,
                        "up" if buf.connected else "down",
                    )
                hl_buf = get_hl_book_buffer()
                logger.info("📖 HL book · %s", hl_buf.status_note())
                sigs = await run_pipeline_cycle(
                    memory,
                    pinned=pinned,
                    cascade_map=c_map,
                    cascade_side_map=c_side,
                    dry_run=dry_run,
                )
                total_signals += len(sigs)
                user_chat = os.environ.get("CRYPTONE_USER_CHAT_ID", "").strip()
                op_chat = os.environ.get("CRYPTONE_OPERATOR_CHAT_ID", "").strip()
                # sinyal ke user; operator dapat copy Tier A / audit
                for s in sigs:
                    arrow = "🟢" if s.direction == "LONG" else "🔴"
                    logger.info(
                        "%s %s %s · %s · Tier %s · RR %.2f · conf %.0f%% · %d/%d · %s",
                        arrow, s.symbol, s.direction, s.horizon, s.tier,
                        s.rr, s.confidence * 100,
                        s.confirm_count, s.active_agent_count, s.setup_type,
                    )
                    plain = format_signal_message(s)
                    kb = _kb_signal(s.signal_id)
                    targets: list[str] = []
                    if user_chat:
                        targets.append(user_chat)
                    if op_chat and op_chat != user_chat and s.tier == "A":
                        targets.append(op_chat)
                    if not targets and op_chat:
                        targets.append(op_chat)
                    for chat in targets:
                        await queue.enqueue(TelegramMessage(
                            chat_id=chat,
                            text=plain,
                            priority="critical" if s.tier == "A" else "normal",
                            reply_markup=kb,
                        ))
                n_sent = await queue.drain_once(bot, dry_run=dry_run)
                total_sent += n_sent
            except Exception as e:
                logger.exception("▸ cycle error (continue): %s", e)

            remaining = max_runtime - (time.monotonic() - t0)
            if remaining <= 0:
                break
            wait = min(cycle_sec, remaining)
            if ui_task is not None and not ui_task.done():
                # UI loop sudah long-poll — jangan double getUpdates
                await asyncio.sleep(wait)
            else:
                # Fallback: slice sleep + short poll
                await sleep_with_ui(
                    bot, memory, wait, dry_run=dry_run, slice_sec=2.0,
                )
    finally:
        # Stop UI loop dulu
        ui_stop.set()
        if ui_task is not None:
            ui_task.cancel()
            try:
                await ui_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            await get_cascade_buffer().stop()
        except Exception:
            pass
        try:
            await get_hl_book_buffer().stop()
        except Exception:
            pass
        await memory.flush_writes()
        memory.close()
        logger.info(
            "🛑 STOP · cycles=%d · signals=%d · sent=%d · dry=%s",
            cycle_n, total_signals, total_sent, dry_run,
        )


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        exp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp
    except (ValueError, TypeError):
        return None


def _realized_rr(
    direction: str,
    entry_low: float,
    entry_high: float,
    original_stop: float,
    exit_px: float,
    tp1: float | None = None,
    tp1_hit: bool = False,
) -> float:
    """Realized R memakai risiko awal, dengan bobot 50/50 setelah TP1."""
    entry = (float(entry_low) + float(entry_high)) / 2.0
    risk = abs(entry - float(original_stop))
    if risk <= 0:
        return 0.0
    if direction == "LONG":
        r_exit = (exit_px - entry) / risk
    else:
        r_exit = (entry - exit_px) / risk
    if tp1_hit and tp1:
        r_tp1 = abs(float(tp1) - entry) / risk
        return 0.5 * r_tp1 + 0.5 * r_exit
    return r_exit


async def monitor_active_signals(memory: MemoryEngine) -> list[dict]:
    """
    Cek harga vs SL / TP1 / TP2 untuk semua sinyal aktif (§XVI.3–§XVI.4).
    Prefer candle 1m high/low sejak last_checked_at (tangkap wick).
    Fallback: mid snapshot kalau candle gagal.
    - SL hit → LOSS, kecuali sudah TP1 → PARTIAL (§XVI.3)
    - TP2 hit → PROFIT
    - TP1 hit → flag, tetap aktif
    - expires → NEUTRAL / PARTIAL
    """
    active = memory.get_active_signals()
    if not active:
        return []

    mids: dict[str, float] = {}
    try:
        mids = await hl_all_mids(memory)
    except Exception as e:
        logger.warning("monitor · mids unavailable: %s", type(e).__name__)

    now = now_utc()
    events: list[dict] = []
    # Cache candles per symbol for this cycle
    candle_cache: dict[str, list[Candle]] = {}

    async def _window_hl(sym: str, since: datetime | None) -> tuple[float | None, float | None, float | None]:
        """
        High/low/close sejak `since`. TF adaptif:
        ≤2h → 1m, ≤24h → 5m, >24h → 15m (tangkap wick downtime panjang §XVI.9).
        """
        cache_key = sym
        if cache_key in candle_cache:
            candles = candle_cache[cache_key]
        else:
            start_dt = since or (now - timedelta(minutes=max(SIGNAL_COOLDOWN_MIN, 10)))
            span_h = (now - start_dt).total_seconds() / 3600.0
            if span_h <= 2:
                interval = "1m"
            elif span_h <= 24:
                interval = "5m"
            else:
                interval = "15m"
                # bound extreme downtime (max 7d)
                if span_h > 168:
                    start_dt = now - timedelta(days=7)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(now.timestamp() * 1000)
            try:
                candles = await hl_candle_snapshot(sym, interval, start_ms, end_ms, memory)
            except Exception:
                candles = []
            candle_cache[cache_key] = candles
        if not candles:
            return None, None, None
        hi = max(c.high for c in candles)
        lo = min(c.low for c in candles)
        return hi, lo, candles[-1].close

    for row in active:
        sid = row["signal_id"]
        sym = row["symbol"]
        direction = str(row.get("direction") or "LONG").upper()
        state = str(row.get("state") or "PENDING").upper()
        stop = float(row.get("stop_loss") or 0)
        tp1 = float(row.get("tp1") or 0)
        tp2 = float(row.get("tp2") or 0)
        entry_lo = float(row.get("entry_zone_low") or 0)
        entry_hi = float(row.get("entry_zone_high") or 0)
        already_tp1 = bool(row.get("tp1_hit"))
        raw_original_stop = row.get("original_stop")
        original_stop = (
            float(raw_original_stop)
            if raw_original_stop is not None
            else stop
        )

        # ARMED signals can resolve on expiry before fetching market data.
        exp = _parse_ts(row.get("expires_at") or row.get("valid_until"))
        if state == "ARMED" and exp is not None and now >= exp:
            outcome = "PARTIAL" if already_tp1 else "NEUTRAL"
            memory.resolve_signal(sid, outcome, tp1_hit=already_tp1 or None)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "expired", "px": None,
            })
            logger.info(
                "⏱ CLOSE %s %s · %s · expired · was_tp1=%s",
                sym, direction, outcome, already_tp1,
            )
            continue

        since = _parse_ts(row.get("last_checked_at")) or _parse_ts(row.get("created_at"))
        hi, lo, last_c = await _window_hl(sym, since)
        mid = mids.get(sym)
        if hi is None or lo is None:
            if mid is None or mid <= 0:
                if state == "PENDING" and exp is not None and now >= exp:
                    memory.resolve_signal(sid, "NEUTRAL")
                    events.append({
                        "signal_id": sid, "symbol": sym, "direction": direction,
                        "outcome": "NEUTRAL", "reason": "expired", "px": None,
                    })
                    continue
                memory.touch_signal_check(sid)
                continue
            hi = lo = last_c = mid

        # PENDING: tunggu entry zone. Setelah arm → evaluasi SL/TP di window yang sama.
        if state == "PENDING":
            entered = (
                entry_lo > 0
                and entry_hi >= entry_lo
                and hi >= entry_lo
                and lo <= entry_hi
            )
            if entered:
                memory.arm_signal(sid)
                state = "ARMED"
                logger.info("🔒 ARMED %s · entry zone reached · eval SL/TP same window", sym)
                # fall through — jangan continue (hindari miss SL/TP candle yang sama)
            else:
                if exp is not None and now >= exp:
                    memory.resolve_signal(sid, "NEUTRAL")
                    events.append({
                        "signal_id": sid, "symbol": sym, "direction": direction,
                        "outcome": "NEUTRAL", "reason": "expired", "px": None,
                    })
                    logger.info(
                        "⏱ CLOSE %s %s · NEUTRAL · expired while PENDING",
                        sym, direction,
                    )
                    continue
                memory.touch_signal_check(sid)
                continue

        # Time stop: close an ARMED signal that has stalled in its entry zone.
        armed_at = _parse_ts(row.get("armed_at"))
        horizon = str(row.get("horizon") or "").upper()
        time_stop_h = TIME_STOP_HOURS.get(horizon)
        current_px = last_c or mid
        if (
            armed_at is not None
            and time_stop_h is not None
            and (now - armed_at).total_seconds() / 3600.0 >= time_stop_h
            and current_px is not None
            and entry_lo > 0
            and entry_hi >= entry_lo
            and entry_lo * 0.997 <= current_px <= entry_hi * 1.003
        ):
            memory.resolve_signal(sid, "NEUTRAL")
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "NEUTRAL", "reason": "TIME_STOP", "px": current_px,
            })
            logger.info(
                "⏰ TIME_STOP %s %s · %.6g still in entry zone",
                sym, direction, current_px,
            )
            continue

        # §XVI.4 — toleransi dalam satuan ATR, BUKAN % harga
        # Bug lama: tol = price * 0.05 → SL "kena" padahal harga masih di entry zone
        entry_mid = (
            (entry_lo + entry_hi) / 2.0 if (entry_lo and entry_hi)
            else (mid or last_c or 0.0)
        )
        atr_est = 0.0
        hist = memory.get_atr_history(sym, "1h") if memory else []
        if hist:
            atr_est = float(hist[-1])
        elif entry_mid and stop:
            # fallback: risk ≈ 1.5×ATR (INTRADAY default)
            atr_est = abs(entry_mid - stop) / 1.5
        tol_tp = atr_est * HIT_TOLERANCE_ATR_DEFAULT  # 0.05×ATR ke arah TP
        tol_sl = atr_est * 0.02  # ketat: harus benar-benar sentuh/lewati SL

        # Priority: SL > TP2 > TP1 (intrabar unknown → conservative SL first)
        if direction == "LONG":
            # SL harus di bawah entry (kalau tidak, data invalid → skip SL)
            sl_valid = stop > 0 and entry_mid > 0 and stop < entry_mid
            sl_hit = sl_valid and lo <= (stop + tol_sl)
            tp2_hit = tp2 > 0 and hi >= (tp2 - tol_tp)
            tp1_hit_now = tp1 > 0 and hi >= (tp1 - tol_tp)
            ref_px = lo if sl_hit else (hi if (tp2_hit or tp1_hit_now) else (last_c or mid))
        else:
            sl_valid = stop > 0 and entry_mid > 0 and stop > entry_mid
            sl_hit = sl_valid and hi >= (stop - tol_sl)
            tp2_hit = tp2 > 0 and lo <= (tp2 + tol_tp)
            tp1_hit_now = tp1 > 0 and lo <= (tp1 + tol_tp)
            ref_px = hi if sl_hit else (lo if (tp2_hit or tp1_hit_now) else (last_c or mid))

        # 2) SL — PARTIAL if already TP1 (§XVI.3)
        if sl_hit:
            outcome = "PARTIAL" if already_tp1 else "LOSS"
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                float(ref_px or stop), tp1=tp1, tp1_hit=already_tp1,
            )
            memory.resolve_signal(
                sid, outcome, realized_rr=rr, tp1_hit=True if already_tp1 else None,
            )
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "SL", "px": ref_px, "realized_rr": rr,
            })
            icon = "🎯" if outcome == "PARTIAL" else "🛑"
            logger.info(
                "%s CLOSE %s %s · %s · SL @ %.6g · R:R %.2f · was_tp1=%s",
                icon, sym, direction, outcome, ref_px or 0, rr, already_tp1,
            )
            continue

        # 3) TP2
        if tp2_hit:
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                float(ref_px or tp2), tp1=tp1, tp1_hit=already_tp1,
            )
            memory.resolve_signal(sid, "PROFIT", realized_rr=rr, tp1_hit=True)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "PROFIT", "reason": "TP2", "px": ref_px, "realized_rr": rr,
            })
            logger.info(
                "✅ CLOSE %s %s · PROFIT · TP2 @ %.6g · R:R %.2f",
                sym, direction, ref_px or 0, rr,
            )
            continue

        # 4) TP1
        if tp1_hit_now and not already_tp1:
            new_stop = memory.move_stop_to_breakeven(sid)
            memory.touch_signal_check(sid, tp1_hit=True)
            if new_stop is not None:
                logger.info("🔒 SL→BE %s · stop=%.6g", sym, new_stop)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "PARTIAL", "reason": "TP1", "px": ref_px,
            })
            logger.info(
                "🎯 TP1 %s %s · hit @ %.6g · tetap aktif → TP2",
                sym, direction, ref_px or 0,
            )
            continue

        memory.touch_signal_check(sid)

    if events:
        n_close = sum(
            1 for e in events
            if e["outcome"] in ("PROFIT", "LOSS", "NEUTRAL", "PARTIAL")
            and e.get("reason") != "TP1"
        )
        n_tp1 = sum(1 for e in events if e.get("reason") == "TP1")
        logger.info(
            "📊 monitor · checked=%d · closed=%d · tp1=%d · active_left=%d",
            len(active), n_close, n_tp1, memory.count_active_signals(),
        )
    return events


async def startup_recovery(memory: MemoryEngine) -> None:
    """
    §XIII.10 / §XVI.9 — recovery setelah restart.
    1) Jalankan lifecycle penuh (candle → TP/SL/PARTIAL) dulu
    2) Baru expire sisa yang lewat valid_until → NEUTRAL
    Jangan langsung NEUTRAL tanpa cek market (rusak trust_score).
    """
    now = now_utc()
    active_before = memory.count_active_signals()
    # Full lifecycle pass (lookback adaptif di monitor)
    try:
        events = await monitor_active_signals(memory)
    except Exception as e:
        logger.warning("startup_recovery monitor fail-soft: %s", type(e).__name__)
        events = []
    closed_market = sum(
        1 for e in events
        if e.get("reason") not in ("TP1",) and e.get("outcome") in (
            "PROFIT", "LOSS", "PARTIAL", "NEUTRAL",
        )
    )
    # Expire sisa yang sudah lewat waktu tanpa resolve market
    active = memory.get_active_signals()
    closed_exp = 0
    for row in active:
        exp = _parse_ts(row.get("expires_at") or row.get("valid_until"))
        if exp is not None and now >= exp:
            already_tp1 = bool(row.get("tp1_hit"))
            outcome = "PARTIAL" if already_tp1 else "NEUTRAL"
            memory.resolve_signal(
                row["signal_id"], outcome, tp1_hit=True if already_tp1 else None,
            )
            closed_exp += 1
    recent = 0
    if memory._conn:
        cutoff = (now - timedelta(minutes=SIGNAL_COOLDOWN_MIN)).isoformat()
        row = memory._conn.execute(
            "SELECT COUNT(*) AS n FROM signal_history WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        recent = int(row["n"]) if row else 0
    logger.info(
        "startup_recovery: before=%d · market_closed=%d · expired=%d · "
        "active_left=%d · recent_%dm=%d",
        active_before, closed_market, closed_exp,
        memory.count_active_signals(), SIGNAL_COOLDOWN_MIN, recent,
    )


def restore_from_previous_artifact() -> bool:
    """
    Pulihkan DB dari backup lokal (.bak / .prev) bila integrity gagal.
    Di GH Actions artifact sudah di-download workflow sebelum Python start —
    fungsi ini cover local / corrupt path saja.
    """
    db = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    for cand in (f"{db}.bak", f"{db}.prev", "./data/cryptone_v45.db.bak"):
        p = Path(cand)
        if p.is_file() and p.stat().st_size > 1024:
            try:
                Path(db).parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(p, db)
                logger.info("♻️ restore DB from %s → %s", p, db)
                return True
            except OSError as e:
                logger.warning("restore failed %s: %s", p, e)
    logger.warning("♻️ restore · no usable backup found")
    return False


def create_fresh_db() -> str:
    """
    Schema kosong via MemoryEngine. Return path DB yang dipakai
    (bisa .fresh jika unlink gagal) — caller harus re-bind path.
    """
    db = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    p = Path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            logger.error("create_fresh_db unlink fail: %s — try alternate path", e)
            p = Path(str(db) + ".fresh")
            os.environ["CRYPTONE_DB_PATH"] = str(p)
    mem = MemoryEngine(str(p))
    mem.start()
    mem.close()
    logger.info("🆕 create_fresh_db · %s", p)
    return str(p)


async def flush_and_exit(memory: MemoryEngine, queue: Any, timeout: int = 20) -> None:
    """Handler SIGTERM: flush lalu hard-exit (sys.exit di task asyncio tidak cukup)."""
    try:
        await asyncio.wait_for(memory.flush_writes(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("flush timeout %ss", timeout)
    try:
        await get_cascade_buffer().stop()
    except Exception:
        pass
    try:
        await get_hl_book_buffer().stop()
    except Exception:
        pass
    try:
        memory.close()
    except Exception:
        pass
    os._exit(0)


async def startup_db_check(db_path: str) -> bool:
    """PRAGMA integrity_check. False → restore / rebuild."""
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return row is not None and row[0] == "ok"
    except Exception:
        return False


# =============================================================================
# §15  CLI ENTRY
# =============================================================================

def parse_symbol_list(s: str) -> list[str]:
    if not s or not s.strip():
        return []
    return [x.strip().upper() for x in s.split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cryptone_v45.py",
        description="Cryptone V4.5 — Market Screener Radar + Trader Assistant",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Main loop produksi")
    mode.add_argument("--bootstrap", action="store_true", help="Backtest historis")
    mode.add_argument("--test", action="store_true", help="Jalankan test suite")
    mode.add_argument("--dry-run", action="store_true", help="Live tanpa Telegram push")
    mode.add_argument("--check", action="store_true", help="Preflight env + source")
    mode.add_argument("--recover", action="store_true", help="Startup recovery saja")

    p.add_argument("--days", type=int, default=60)
    p.add_argument("--pin", type=parse_symbol_list, default=[])
    p.add_argument("--db", default=None)
    p.add_argument("--log-level", default=None)
    p.add_argument("--cycle-interval", type=int, default=None)
    p.add_argument("--max-runtime", type=int, default=None)
    p.add_argument("--section", type=int, default=None)
    p.add_argument("--unit", action="store_true")
    p.add_argument("--integration", action="store_true")
    return p


def resolve_config(args: argparse.Namespace) -> dict[str, Any]:
    """Prioritas: CLI > env > default (§XVII.5)."""
    def _pick(cli_val, env_key, default):
        if cli_val is not None:
            return cli_val
        env = os.environ.get(env_key)
        if env is not None and env != "":
            return env
        return default

    db = _pick(args.db, "CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    log_level = _pick(args.log_level, "CRYPTONE_LOG_LEVEL", "INFO")
    cycle = _pick(args.cycle_interval, "CRYPTONE_CYCLE_INTERVAL_SEC", DEFAULT_CYCLE_INTERVAL_SEC)
    max_rt = _pick(args.max_runtime, "CRYPTONE_MAX_RUNTIME_SEC", DEFAULT_MAX_RUNTIME_SEC)
    pin = args.pin or parse_symbol_list(os.environ.get("CRYPTONE_PINNED_SYMBOLS", ""))

    return {
        "db": str(db),
        "log_level": str(log_level),
        "cycle_interval": int(cycle),
        "max_runtime": int(max_rt),
        "pin": pin,
        "days": args.days,
    }


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cfg = resolve_config(args)
    setup_logging(cfg["log_level"])

    logger.info("Cryptone V%s | mode=%s | db=%s", PROTOCOL_VERSION,
                next(k for k in ("live", "bootstrap", "test", "dry_run", "check", "recover")
                     if getattr(args, k.replace("-", "_"), False) or getattr(args, k, False)),
                cfg["db"])

    if args.check:
        ok = asyncio.run(run_preflight())
        return 0 if ok else 4

    if args.bootstrap:
        if not (7 <= args.days <= 365):
            logger.error("--days must be 7–365")
            return 2
        asyncio.run(run_bootstrap(days=args.days))
        return 0

    if args.test:
        # Delegate ke test module
        logger.info("Running tests — section=%s unit=%s integration=%s",
                    args.section, args.unit, args.integration)
        try:
            import cryptone_v45_test as tests
            return tests.run_tests(
                section=args.section,
                unit_only=args.unit,
                integration_only=args.integration,
            )
        except ImportError:
            logger.error("cryptone_v45_test.py not found")
            return 1

    if args.recover:
        mem = MemoryEngine(cfg["db"])
        mem.start()
        asyncio.run(startup_recovery(mem))
        mem.close()
        return 0

    if args.live or args.dry_run:
        # Validasi env live
        if args.live:
            for key in ("GEMINI_API_KEY", "CRYPTONE_TELEGRAM_BOT_TOKEN",
                        "CRYPTONE_OPERATOR_CHAT_ID", "CRYPTONE_USER_CHAT_ID"):
                if not os.environ.get(key):
                    logger.error("Missing required env for --live: %s", key)
                    return 3
        # CLI flags → env (run_live_service only reads env)
        if args.max_runtime is not None:
            os.environ["CRYPTONE_MAX_RUNTIME_SEC"] = str(args.max_runtime)
        if args.cycle_interval is not None:
            os.environ["CRYPTONE_CYCLE_INTERVAL_SEC"] = str(args.cycle_interval)
        if args.db:
            os.environ["CRYPTONE_DB_PATH"] = str(args.db)
        asyncio.run(run_live_service(dry_run=args.dry_run))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
