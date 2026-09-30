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
from collections import defaultdict, deque
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


def _as_wib(t: datetime | None = None) -> datetime:
    """
    Normalisasi ke WIB untuk gate jam. Naive datetime = UTC (konvensi seluruh
    codebase & DB: semua TIMESTAMP UTC); WIB hanya untuk tampilan/lokalisasi.
    """
    if t is None:
        return now_wib()
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(WIB)


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
    t = _as_wib(now)
    h = t.hour
    in_session = SESSION_START_HOUR_WIB <= h < SESSION_END_HOUR_WIB
    if in_session:
        return True, f"session_ok_{h:02d}wib"
    if horizon == "SWING":
        return True, f"session_offhours_swing_{h:02d}wib"
    if tier == "A":
        return True, f"session_offhours_tierA_{h:02d}wib"
    return False, f"session_reject_{h:02d}wib"


# Quiet hours default 23:00–07:00 WIB (§XII.8). Tier A tetap push; B/C ditahan.
QUIET_HOURS_ENABLED = True
QUIET_START_HOUR_WIB = 23
QUIET_END_HOUR_WIB = 7
# Delivery mode: active | hybrid | passive (§XII.7)
# active  = push semua tier yang lolos gate (B tetap valid — min confirm 2)
# hybrid  = Tier A realtime; Tier B/C tetap generate+DB, push di luar quiet saja
# passive = tidak push (hanya Radar/Journal tombol)
DEFAULT_DELIVERY_MODE = "active"


def in_quiet_hours(now: datetime | None = None) -> bool:
    if not QUIET_HOURS_ENABLED:
        return False
    if os.environ.get("CRYPTONE_QUIET_HOURS", "1").strip() in ("0", "false", "False", "no"):
        return False
    t = _as_wib(now)
    h = t.hour
    if QUIET_START_HOUR_WIB > QUIET_END_HOUR_WIB:
        # wraps midnight: 23..23,0..6
        return h >= QUIET_START_HOUR_WIB or h < QUIET_END_HOUR_WIB
    return QUIET_START_HOUR_WIB <= h < QUIET_END_HOUR_WIB


def get_delivery_mode(memory: "MemoryEngine | None" = None) -> str:
    if memory is not None:
        m = (memory.get_runtime("delivery_mode") or "").strip().lower()
        if m in ("active", "hybrid", "passive"):
            return m
    env = os.environ.get("CRYPTONE_DELIVERY_MODE", DEFAULT_DELIVERY_MODE).strip().lower()
    if env in ("active", "hybrid", "passive"):
        return env
    return DEFAULT_DELIVERY_MODE


def should_push_signal(
    tier: str,
    memory: "MemoryEngine | None" = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """
    Keputusan push Telegram — tidak memblok generate/persist.
    Tier B tetap layak (confirm≥2); quiet/hybrid hanya menunda push.
    """
    mode = get_delivery_mode(memory)
    tier = (tier or "C").upper()
    if mode == "passive":
        return False, "mode_passive"
    if mode == "active":
        if in_quiet_hours(now) and tier not in ("A",):
            return False, "quiet_hold_bc"
        return True, "mode_active"
    # hybrid
    if tier == "A":
        return True, "hybrid_tierA"
    if in_quiet_hours(now):
        return False, "hybrid_quiet_hold"
    return True, "hybrid_push_bc"


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
MAX_CONCURRENT_SIGNALS = 10
MAX_SIGNALS_PER_CYCLE = 3  # cap spam saat funding ekstrem berkepanjangan
MAX_SIGNALS_PER_SETUP_PER_CYCLE = 2  # cegah satu setup memonopoli satu cycle (berlaku utk SEMUA setup, termasuk CASCADE_SCALP)
# Counter-trend: sweep searah SAJA tidak cukup menembus veto trend struktur.
# Sweep tanpa pergeseran struktur = stop-hunt yang sering lanjut searah trend;
# BOS/CHoCH searah tidak bisa jadi syarat karena begitu terjadi, last_trend
# otomatis berbalik (tidak lagi "counter-trend"). Bukti pengganti = konfirmasi
# REAL independen (book + taker-flow + …), bukan echo funding.
COUNTER_TREND_MIN_REAL_CONFIRMS = 2
# Kalibrasi confidence berbasis outcome LIVE (Kategori B: lookback 30 hari).
# Shrinkage Bayesian: bobot data = n/(n+K). Sampel kecil → hampir tak berpengaruh.
CAL_LOOKBACK_DAYS = 30
CAL_SHRINK_K = 30       # = MIN_SAMPLE_DYNAMIC; n=30 → data & model berbobot sama
CAL_MAX_DOWN = 0.15     # confidence maksimal turun 0.15 dari model
CAL_MAX_UP = 0.05       # asimetris: bukti historis tidak boleh menggelembungkan conf
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
            px REAL,
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
        CREATE INDEX IF NOT EXISTS idx_active_signal_symbol
            ON active_signals(symbol);

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
        # metric_oi lama tidak punya harga (px) → ΔOI×Δharga tak bisa di-warm dari DB.
        oi_cols = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(metric_oi)").fetchall()
        }
        if "px" not in oi_cols:
            self._conn.execute("ALTER TABLE metric_oi ADD COLUMN px REAL")
        self._conn.execute(
            "DELETE FROM metric_oi WHERE id NOT IN "
            "(SELECT MIN(id) FROM metric_oi GROUP BY symbol, recorded_at)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_oi_symbol_time "
            "ON metric_oi(symbol, recorded_at)"
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
        # Isolasi per-item: satu row buruk (constraint / tipe / param hilang)
        # tidak boleh membuang sisa batch. Statement sqlite gagal = rollback
        # statement itu saja; item lain tetap ter-commit. Row beracun di-drop
        # (bukan di-requeue) supaya tidak macet berulang tiap cycle.
        failed = 0
        try:
            for table, params in batch:
                try:
                    self._dispatch_write(table, params)
                except Exception as e:  # noqa: BLE001 — fail-soft per item
                    failed += 1
                    logger.warning(
                        "flush_writes: drop %s row (%s: %s)",
                        table, type(e).__name__, str(e)[:120],
                    )
        finally:
            self._conn.commit()
        if failed:
            logger.warning("flush_writes: %d/%d writes dropped", failed, len(batch))
        logger.debug("flushed %d/%d writes", len(batch) - failed, len(batch))

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
        elif table == "metric_oi":
            self._conn.execute(
                "INSERT OR IGNORE INTO metric_oi "
                "(symbol, value, px, recorded_at, is_simulated, schema_version) "
                "VALUES (:symbol, :value, :px, :recorded_at, :is_simulated, 1)",
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
        elif table == "wallet_txlog":
            self._conn.execute(
                "INSERT INTO wallet_txlog "
                "(address, direction, amount_usd, to_label, tx_hash, recorded_at) "
                "VALUES (:address, :direction, :amount_usd, :to_label, :tx_hash, :recorded_at)",
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
    ) -> float:
        """
        Bobot loss beruntun (opsional per setup).
        LOSS = 1.0, PARTIAL = 0.5, PROFIT/NEUTRAL memutus streak.
        Return float supaya threshold (3 / 5) tetap comparable.
        """
        if not self._conn:
            return 0.0
        # Ambil lebih banyak row: PARTIAL tidak memutus, tapi butuh sample
        limit = max(n * 3, n)
        if setup_type:
            rows = self._conn.execute(
                "SELECT outcome FROM signal_history "
                "WHERE outcome IS NOT NULL AND is_simulated = 0 "
                "AND setup_type = ? "
                "ORDER BY resolved_at DESC LIMIT ?",
                (setup_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT outcome FROM signal_history "
                "WHERE outcome IS NOT NULL AND is_simulated = 0 "
                "ORDER BY resolved_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        score = 0.0
        for r in rows:
            oc = r["outcome"]
            if oc == "LOSS":
                score += 1.0
            elif oc == "PARTIAL":
                score += 0.5
            else:
                break  # PROFIT / NEUTRAL memutus
        return score

    def load_oi_snapshots(
        self, max_age_sec: float,
    ) -> list[tuple[str, float, float, float]]:
        """(symbol, epoch_ts, oi, px) dari metric_oi dalam max_age_sec terakhir, urut waktu."""
        if not self._conn:
            return []
        cutoff = (now_utc() - timedelta(seconds=max_age_sec)).isoformat()
        out: list[tuple[str, float, float, float]] = []
        for r in self._conn.execute(
            "SELECT symbol, value, px, recorded_at FROM metric_oi "
            "WHERE recorded_at >= ? AND px IS NOT NULL AND is_simulated = 0 "
            "ORDER BY recorded_at",
            (cutoff,),
        ).fetchall():
            t = _parse_ts(r["recorded_at"])
            if t is not None:
                out.append((str(r["symbol"]), t.timestamp(), float(r["value"]), float(r["px"])))
        return out

    def prune_oi_snapshots(self, max_age_sec: float) -> None:
        """Buang metric_oi lebih tua dari max_age_sec (hanya basis ΔOI yang berguna)."""
        if not self._conn:
            return
        cutoff = (now_utc() - timedelta(seconds=max_age_sec)).isoformat()
        self._conn.execute("DELETE FROM metric_oi WHERE recorded_at < ?", (cutoff,))
        self._conn.commit()

    def prune_ephemeral(self) -> None:
        """Hapus llm_cache expired + data_source_health >30d (cegah DB bloat)."""
        if not self._conn:
            return
        now_s = now_utc().isoformat()
        cutoff_health = (now_utc() - timedelta(days=30)).isoformat()
        try:
            self._conn.execute("DELETE FROM llm_cache WHERE expires_at < ?", (now_s,))
            self._conn.execute(
                "DELETE FROM data_source_health WHERE recorded_at < ?", (cutoff_health,),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("prune_ephemeral: %s", type(e).__name__)

    def outcome_counts(
        self,
        *,
        setup_type: str | None = None,
        symbol: str | None = None,
        lookback_days: int = CAL_LOOKBACK_DAYS,
    ) -> tuple[int, int, int]:
        """
        (wins, partials, losses) sinyal LIVE yang resolved dalam lookback.
        NEUTRAL (bukan trade terbukti, §XVI.3) dan is_simulated=1 dikeluarkan.
        Rolling window (bukan lifetime) → outcome era logika lama ikut kedaluwarsa.
        """
        if not self._conn:
            return 0, 0, 0
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        sql = (
            "SELECT outcome, COUNT(*) AS n FROM signal_history "
            "WHERE outcome IN ('PROFIT','PARTIAL','LOSS') "
            "AND is_simulated = 0 AND resolved_at >= ?"
        )
        params: list[Any] = [cutoff]
        if setup_type:
            sql += " AND setup_type = ?"
            params.append(setup_type)
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        sql += " GROUP BY outcome"
        counts = {
            str(r["outcome"]): int(r["n"])
            for r in self._conn.execute(sql, params).fetchall()
        }
        return (
            counts.get("PROFIT", 0),
            counts.get("PARTIAL", 0),
            counts.get("LOSS", 0),
        )

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
                last_updated = ?
            WHERE symbol = ?
            """,
            (win, loss, partial, neutral, rr, rr, ts, symbol),
        )
        # trust_score dihitung SATU tempat di sini dari counter pasca-increment.
        # Belum ada trade resolved (baru NEUTRAL) → tetap default 0.5 dari INSERT.
        sp = self._conn.execute(
            "SELECT wins, losses, partials FROM symbol_performance WHERE symbol = ?",
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


def _circuit_cooldown(source: str, seconds: float) -> None:
    """
    Cooldown pendek eksplisit untuk soft rate-limit (429 / "Max rate").
    Bukan kegagalan sumber → tidak menambah `failures`; tidak pernah
    memperpendek cooldown yang sudah lebih panjang.
    """
    st = _circuit[source]
    st["open_until"] = max(float(st.get("open_until") or 0.0), time.monotonic() + seconds)


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


HL_CANDLE_MAX_PER_CALL = 5000   # batas API candleSnapshot
HL_CANDLE_MAX_PAGES = 20        # pengaman: ≤100k candle per pemanggilan


async def hl_candle_snapshot(
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    memory: "MemoryEngine | None" = None,
) -> list[Candle]:
    """
    OHLCV history. interval: 1m|5m|15m|1h|4h|1d …
    Max 5000 candle per call; otomatis paginate dengan startTime = t terakhir+1
    sampai respons < batas / end_ms tercapai (dedupe by open-time, urut naik).
    """
    async def _page(page_start: int) -> list:
        async def _call():
            return await http_post_json(HL_INFO_URL, {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": page_start,
                    "endTime": end_ms,
                },
            })

        raw = await with_retry("hyperliquid_rest", _call, memory)
        if not isinstance(raw, list):
            raise DataUnavailable("hyperliquid_rest: candleSnapshot not list")
        return raw

    rows: list = []
    cursor = start_ms
    truncated = False
    for page_no in range(HL_CANDLE_MAX_PAGES):
        try:
            page = await _page(cursor)
        except DataUnavailable:
            if page_no == 0:
                raise
            truncated = True   # halaman lanjutan gagal → kembalikan yang sudah ada
            break
        rows.extend(page)
        if len(page) < HL_CANDLE_MAX_PER_CALL:
            break
        try:
            nxt = int(page[-1]["t"]) + 1
        except (KeyError, TypeError, ValueError, IndexError):
            truncated = True
            break
        if nxt <= cursor or nxt >= end_ms:
            break
        cursor = nxt
    else:
        truncated = True
    if truncated:
        logger.warning(
            "hl_candle_snapshot %s %s: hasil dipotong (pagination tidak tuntas)",
            coin, interval,
        )

    out: list[Candle] = []
    seen: set[int] = set()
    for row in rows:
        try:
            t_ms = int(row["t"])
            if t_ms in seen:
                continue
            candle = Candle(
                symbol=coin,
                timeframe=interval,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row["v"]),
                bar_time=datetime.fromtimestamp(t_ms / 1000.0, tz=UTC),
            )
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(t_ms)
        out.append(candle)
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
    Layer 0 filter (§I.2):
    - volume floor by rank
    - drop zero price
    - soft dead-coin: |day change| < 0.05% AND OI sangat kecil → skip
      (ATR ketat di process_trigger setelah ATR real tersedia)
    pinned tetap harus lolos filter (bukan whitelist bypass).
    """
    ranked = sorted(assets, key=lambda a: a.day_ntl_vlm, reverse=True)
    out: list[AssetCtx] = []
    pinned_set = {p.upper() for p in (pinned or [])}

    for i, a in enumerate(ranked):
        rank = i + 1
        px = a.mid_px or a.mark_px
        if px <= 0:
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
        # Soft dead: flat 24h DAN volume cuma marginally di atas floor
        if rank > 10 and a.prev_day_px > 0:
            day_chg = abs(px - a.prev_day_px) / px
            if day_chg < 0.0003 and a.day_ntl_vlm < floor * 1.05:
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
        self._events: deque[tuple[float, str, str, float]] = deque()
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
        ev = self._events
        # timestamp monotonic & append-only → urut naik: pangkas dari kepala,
        # O(1) amortized per event (bukan rebuild list O(N) tiap add_event).
        while ev and ev[0][0] < cut:
            ev.popleft()

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


# -----------------------------------------------------------------------------
# HL public trades buffer — independent real confirm for Tier A/B
# -----------------------------------------------------------------------------
# Stream: wss://api.hyperliquid.xyz/ws
# subscription {"type": "trades", "coin": SYM}
#
# L2 book dan trade flow adalah dua observasi berbeda: book mengukur resting
# liquidity, sedangkan trades mengukur aggressive taker flow. Saat Binance/
# Bybit/Etherscan tidak tersedia, keduanya tetap bisa membentuk real_count=2
# tanpa mengubah fallback proxy menjadi seolah-olah data independen.

HL_TRADE_FLOW_WINDOW_SEC = 120.0
HL_TRADE_FLOW_MIN_SAMPLES = 3
HL_TRADE_FLOW_MIN_IMBALANCE = 0.15


class HLTradeFlowBuffer:
    """Rolling aggressive buy/sell notional imbalance dari HL trades WS."""

    def __init__(
        self,
        window_sec: float = HL_TRADE_FLOW_WINDOW_SEC,
        min_samples: int = HL_TRADE_FLOW_MIN_SAMPLES,
    ):
        self.window_sec = window_sec
        self.min_samples = min_samples
        # symbol_hl → [(monotonic_ts, signed_notional)]
        self._trades: dict[str, list[tuple[float, float]]] = {}
        self._desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_msg_mono: float = 0.0
        self.total_msgs = 0
        self.last_error: str = ""

    def set_symbols(self, symbols: list[str]) -> None:
        self._desired = {s for s in symbols if s}

    def _prune(self, symbol: str, now_m: float | None = None) -> list[tuple[float, float]]:
        now_m = now_m if now_m is not None else time.monotonic()
        cutoff = now_m - self.window_sec
        rows = [
            row for row in self._trades.get(symbol, [])
            if row[0] >= cutoff
        ]
        if rows:
            self._trades[symbol] = rows
        else:
            self._trades.pop(symbol, None)
        return rows

    def get_imbalance(self, symbol: str) -> float | None:
        rows = self._prune(symbol)
        if len(rows) < self.min_samples:
            return None
        gross = sum(abs(notional) for _, notional in rows)
        if gross <= 0:
            return None
        return clamp(sum(notional for _, notional in rows) / gross, -1.0, 1.0)

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
        """Parse HL trades payload; malformed or unknown sides fail soft."""
        if not isinstance(msg, dict) or msg.get("channel") != "trades":
            return
        data = msg.get("data")
        rows = data if isinstance(data, list) else [data]
        now_m = time.monotonic()
        accepted = 0
        for trade in rows:
            if not isinstance(trade, dict):
                continue
            coin = str(trade.get("coin") or "")
            side = str(trade.get("side") or "").upper()
            try:
                notional = float(trade.get("px")) * float(trade.get("sz"))
            except (TypeError, ValueError):
                continue
            if not coin or notional <= 0:
                continue
            if side in {"B", "BUY"}:
                signed = notional
            elif side in {"A", "SELL"}:
                signed = -notional
            else:
                continue
            bucket = self._trades.setdefault(coin, [])
            bucket.append((now_m, signed))
            accepted += 1
        if accepted:
            for coin in {str(t.get("coin") or "") for t in rows if isinstance(t, dict)}:
                if coin:
                    self._prune(coin, now_m)
            self.total_msgs += accepted
            self.last_msg_mono = now_m

    async def _reconcile_subscriptions(self, ws) -> None:
        to_add = list(self._desired - self._subscribed)[:HL_BOOK_SUB_BATCH]
        to_remove = list(self._subscribed - self._desired)[:HL_BOOK_SUB_BATCH]
        for sym in to_add:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": sym},
            }))
            self._subscribed.add(sym)
        for sym in to_remove:
            await ws.send(json.dumps({
                "method": "unsubscribe",
                "subscription": {"type": "trades", "coin": sym},
            }))
            self._subscribed.discard(sym)
            self._trades.pop(sym, None)

    async def _ws_loop(self) -> None:
        try:
            import websockets
        except ImportError:
            self.last_error = "websockets not installed"
            logger.error("HL trade flow WS · %s", self.last_error)
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
                    self._subscribed = set()
                    self._trades.clear()
                    logger.info("📈 HL trade flow WS · connected")
                    await self._reconcile_subscriptions(ws)
                    last_reconcile = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            raw = None
                        now_m = time.monotonic()
                        if now_m - last_reconcile >= HL_BOOK_RECONCILE_SEC:
                            await self._reconcile_subscriptions(ws)
                            last_reconcile = now_m
                        if raw is None:
                            continue
                        try:
                            if isinstance(raw, (bytes, bytearray)):
                                raw = raw.decode("utf-8", errors="ignore")
                            self.ingest(json.loads(raw))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"[:80]
                logger.warning(
                    "HL trade flow WS · disconnect · %s · retry %.0fs",
                    self.last_error, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._ws_loop(), name="hl_trade_flow_ws")

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


_hl_trade_flow_buffer = HLTradeFlowBuffer()


def get_hl_trade_flow_buffer() -> HLTradeFlowBuffer:
    return _hl_trade_flow_buffer


# -----------------------------------------------------------------------------
# HL open-interest delta — sumber real ke-3 (Tier A tanpa cross-exchange)
# -----------------------------------------------------------------------------
# Book (resting liquidity), trades (taker flow), dan ΔOI×Δharga (posisi baru vs
# tutup posisi) adalah tiga observasi berbeda dari HL. Berbeda dari
# oi_structure_confirm (echo funding+OI level, is_fallback=True), ini memakai
# perubahan OI antar snapshot cycle — bukan turunan funding.
# Semantik (price/OI matrix): LONG = OI naik + harga naik (posisi baru masuk
# searah); SHORT = OI naik + harga turun. Cascade TIDAK memakai ini: likuidasi
# justru menurunkan OI, jadi jalur cascade tetap pakai oi_structure fallback.
# Threshold = default Kategori D (cold-start); belum ada histori untuk percentile.
OI_DELTA_MIN_AGE_SEC = 600.0     # baseline snapshot minimal 10 menit lalu
OI_DELTA_MAX_AGE_SEC = 3600.0    # …dan maksimal 1 jam (lebih tua = bukan "kini")
OI_DELTA_MIN_OI_PCT = 0.005      # OI harus berubah ≥ 0.5%
OI_DELTA_MIN_PX_PCT = 0.002      # harga harus bergerak ≥ 0.2%
OI_SNAPSHOT_KEEP_SEC = OI_DELTA_MAX_AGE_SEC + 600.0   # retensi buffer & metric_oi


class OISnapshotBuffer:
    """
    Snapshot (OI, harga) per simbol per cycle. Waktu internal = monotonic.
    Dipersist ke metric_oi (persist_oi_snapshots) dan di-seed ulang saat startup
    (seed) → restart tidak mengulang warm-up 10 menit selama jeda < OI_DELTA_MAX_AGE_SEC.
    """

    def __init__(self, maxlen: int = 40):
        self.maxlen = maxlen
        self._snaps: dict[str, list[tuple[float, float, float]]] = {}

    def record(
        self, assets: list["AssetCtx"], now_m: float | None = None,
    ) -> list[tuple[str, float, float]]:
        """Catat snapshot; return [(symbol, oi, px)] yang benar-benar tercatat (untuk persist)."""
        now_m = now_m if now_m is not None else time.monotonic()
        keep_after = now_m - OI_SNAPSHOT_KEEP_SEC
        recorded: list[tuple[str, float, float]] = []
        for a in assets:
            px = a.mid_px or a.mark_px
            if a.open_interest <= 0 or px <= 0:
                continue
            rows = self._snaps.setdefault(a.symbol, [])
            rows.append((now_m, float(a.open_interest), float(px)))
            rows[:] = [r for r in rows if r[0] >= keep_after][-self.maxlen:]
            recorded.append((a.symbol, float(a.open_interest), float(px)))
        return recorded

    def seed(
        self,
        rows: list[tuple[str, float, float, float]],
        now_m: float | None = None,
        now_epoch: float | None = None,
    ) -> int:
        """
        Warm dari (symbol, epoch_ts, oi, px) hasil metric_oi. Epoch → monotonic lewat
        umur (jeda restart ikut terhitung). Panggil sekali saat startup, sebelum record().
        Return jumlah snapshot yang masuk.
        """
        now_m = now_m if now_m is not None else time.monotonic()
        now_e = now_epoch if now_epoch is not None else time.time()
        keep_after = now_m - OI_SNAPSHOT_KEEP_SEC
        n = 0
        for sym, ts_e, oi, px in rows:
            if oi <= 0 or px <= 0:
                continue
            ts_m = now_m - (now_e - ts_e)
            if ts_m < keep_after or ts_m > now_m:
                continue
            self._snaps.setdefault(sym, []).append((ts_m, float(oi), float(px)))
            n += 1
        for rs in self._snaps.values():
            rs.sort(key=lambda r: r[0])
            rs[:] = rs[-self.maxlen:]
        return n

    def get_delta(
        self,
        symbol: str,
        oi_now: float,
        px_now: float,
        now_m: float | None = None,
    ) -> tuple[float, float] | None:
        """(oi_pct, px_pct) vs snapshot terbaru yang umurnya dalam [MIN_AGE, MAX_AGE]; None = belum warm."""
        if oi_now <= 0 or px_now <= 0:
            return None
        now_m = now_m if now_m is not None else time.monotonic()
        base = None
        for ts, oi, px in reversed(self._snaps.get(symbol, [])):
            age = now_m - ts
            if age < OI_DELTA_MIN_AGE_SEC:
                continue
            if age > OI_DELTA_MAX_AGE_SEC:
                break
            base = (oi, px)
            break
        if base is None or base[0] <= 0 or base[1] <= 0:
            return None
        return (oi_now - base[0]) / base[0], (px_now - base[1]) / base[1]


_oi_snapshot_buffer = OISnapshotBuffer()


def get_oi_snapshot_buffer() -> OISnapshotBuffer:
    return _oi_snapshot_buffer


async def persist_oi_snapshots(
    memory: "MemoryEngine", recorded: list[tuple[str, float, float]],
) -> None:
    """Tulis snapshot cycle ini ke metric_oi (1 baris/simbol/cycle) + pangkas yang basi."""
    if recorded:
        ts = now_utc().isoformat()
        for sym, oi, px in recorded:
            await memory.enqueue_write("metric_oi", {
                "symbol": sym, "value": oi, "px": px, "recorded_at": ts,
            })
        await memory.flush_writes()
    memory.prune_oi_snapshots(OI_SNAPSHOT_KEEP_SEC)


def warm_oi_snapshot_buffer(memory: "MemoryEngine") -> int:
    """Startup: seed OISnapshotBuffer dari metric_oi. Fail-soft → 0 (warm-up biasa)."""
    try:
        n = get_oi_snapshot_buffer().seed(
            memory.load_oi_snapshots(OI_SNAPSHOT_KEEP_SEC)
        )
    except Exception as e:  # noqa: BLE001 — warm-up tidak boleh menjatuhkan startup
        logger.warning("OI warm-up dari DB gagal: %s", type(e).__name__)
        return 0
    logger.info("📈 OI warm-up · %d snapshot dari DB", n)
    return n


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
# continuation = ikut arah pressure cascade (default, legacy)
# exhaustion = fade cascade (mean-revert) — set CRYPTONE_CASCADE_MODE=exhaustion
ATR_PCT_MIN_FILTER = 0.005       # Layer 0: ATR > 0.5%
BASE_CONF_W1, BASE_CONF_W2, BASE_CONF_W3 = 0.35, 0.30, 0.20  # max raw 0.85; hindari conf 100% palsu


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
    sl_basis: str = "atr"       # "atr" | "structure" | "structure_widened_min_risk"
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
    # Fresh flip ke ekstrem = alpha (crowding baru), BUKAN diskoun.
    # Konsistensi 3 sample searah → sedikit boost (crowding mature).
    if memory:
        consistency = memory.get_funding_history(asset.symbol, lookback_days=1)
        if len(consistency) >= 3:
            last3 = consistency[-3:]
            same_sign = (
                all(x > 0 for x in last3)
                if asset.funding > 0
                else all(x < 0 for x in last3)
            )
            if same_sign:
                strength = clamp(strength * 1.1, 0.0, 1.0)
            # else: keep full strength — flip ke ekstrem tetap valid
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


def cascade_mode() -> str:
    """continuation (default) | exhaustion — env CRYPTONE_CASCADE_MODE."""
    m = (os.environ.get("CRYPTONE_CASCADE_MODE") or "continuation").strip().lower()
    if m in ("exhaustion", "fade", "mean_revert", "mean-revert"):
        return "exhaustion"
    return "continuation"


def resolve_cascade_direction(side: str | None) -> str | None:
    """
    Map cascade liquidasi side → trade direction.
    continuation: long-liq pressure → SHORT (legacy)
    exhaustion: long-liq dump → bounce LONG (fade)
    """
    if side not in ("LONG", "SHORT"):
        return None
    if cascade_mode() == "exhaustion":
        return "LONG" if side == "SHORT" else "SHORT"
    return side


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
        direction_bias="NEUTRAL",  # diisi breakout detect di process_trigger
        raw_value=atr_now,
        threshold=thr,
        setup_type="COMPRESSION",
    )


def compression_breakout_direction(
    candles: list,
    lookback: int = 20,
) -> str | None:
    """
    Arah breakout dari compression: close tembus high/low lookback (ex-last bar).
    LONG jika close > max(high[:-1]), SHORT jika close < min(low[:-1]).
    None = belum breakout → compression tetap pending (jangan force sinyal).
    """
    if not candles or len(candles) < max(5, lookback // 2):
        return None
    window = candles[-(lookback + 1):]
    if len(window) < 3:
        return None
    prior = window[:-1]
    last = window[-1]
    try:
        hi = max(float(c.high) for c in prior)
        lo = min(float(c.low) for c in prior)
        cl = float(last.close)
    except (TypeError, ValueError, AttributeError):
        return None
    # Butuh clear break, bukan touch tipis
    if hi > 0 and cl > hi * 1.0005:
        return "LONG"
    if lo > 0 and cl < lo * 0.9995:
        return "SHORT"
    return None


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


def trade_flow_confirm(
    direction: str,
    imbalance: float | None,
) -> ConfirmResult:
    """
    HL taker-flow confirm: +imbalance = aggressive buying, - = selling.
    None berarti stream belum warm/stale dan tidak dihitung sebagai agent aktif.
    """
    if imbalance is None:
        return ConfirmResult("trade_flow", False, "agent off")
    if direction == "LONG" and imbalance >= HL_TRADE_FLOW_MIN_IMBALANCE:
        return ConfirmResult("trade_flow", True, f"buy_imbalance={imbalance:.2f}")
    if direction == "SHORT" and imbalance <= -HL_TRADE_FLOW_MIN_IMBALANCE:
        return ConfirmResult("trade_flow", True, f"sell_imbalance={imbalance:.2f}")
    return ConfirmResult("trade_flow", False, f"imbalance={imbalance:.2f}")


def oi_delta_confirm(
    direction: str,
    delta: tuple[float, float] | None,
) -> ConfirmResult:
    """
    ΔOI × Δharga dari snapshot HL antar cycle (REAL, bukan echo funding).
    None = belum warm → agent off ("no oi delta", tidak dihitung aktif).
    """
    if delta is None:
        return ConfirmResult("oi_delta", False, "no oi delta")
    oi_pct, px_pct = delta
    detail = f"oi{oi_pct:+.2%}_px{px_pct:+.2%}"
    if oi_pct >= OI_DELTA_MIN_OI_PCT:
        if direction == "LONG" and px_pct >= OI_DELTA_MIN_PX_PCT:
            return ConfirmResult("oi_delta", True, detail)
        if direction == "SHORT" and px_pct <= -OI_DELTA_MIN_PX_PCT:
            return ConfirmResult("oi_delta", True, detail)
    return ConfirmResult("oi_delta", False, detail)


# ----- Etherscan + Exchange Wallet Flow (§V.6 / §X) -----
ETHERSCAN_API = "https://api.etherscan.io/v2/api"  # V1 deprecated 2026
_ETHERSCAN_RATE_COOLDOWN_SEC = 5.0   # soft rate-limit; beda dari circuit 5-gagal/300s
_ETHERSCAN_CACHE: dict[str, tuple[float, list[dict]]] = {}  # address → (mono_ts, txs)
_ETHERSCAN_CACHE_TTL = 300.0  # 5 menit (§X)
_NETFLOW_CACHE: tuple[float, float | None] = (0.0, None)  # mono, usd
_NETFLOW_CACHE_TTL = 120.0
_ETH_USD_CACHE: tuple[float, float] = (0.0, 0.0)


def parse_exchange_wallets() -> list[tuple[str, str]]:
    """
    CRYPTONE_EXCHANGE_WALLETS: JSON object {label: address} atau array
    [{"label":"...","address":"0x..."}] atau comma-separated addresses.
    """
    raw = (os.environ.get("CRYPTONE_EXCHANGE_WALLETS") or "").strip()
    if not raw:
        return []
    out: list[tuple[str, str]] = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for label, addr in data.items():
                a = str(addr).strip()
                if a.startswith("0x") and len(a) >= 42:
                    out.append((str(label), a))
        elif isinstance(data, list):
            for i, item in enumerate(data):
                if isinstance(item, str) and item.startswith("0x"):
                    out.append((f"w{i}", item.strip()))
                elif isinstance(item, dict):
                    a = str(item.get("address") or "").strip()
                    lab = str(item.get("label") or f"w{i}")
                    if a.startswith("0x"):
                        out.append((lab, a))
    except json.JSONDecodeError:
        for i, part in enumerate(raw.split(",")):
            a = part.strip()
            if a.startswith("0x") and len(a) >= 42:
                out.append((f"w{i}", a))
    return out


async def _eth_usd_price(memory: "MemoryEngine | None" = None) -> float:
    """Rough ETHUSD for netflow USD — HL mid, fallback 3000."""
    global _ETH_USD_CACHE
    now_m = time.monotonic()
    if now_m - _ETH_USD_CACHE[0] < 60 and _ETH_USD_CACHE[1] > 0:
        return _ETH_USD_CACHE[1]
    px = 3000.0
    try:
        mids = await hl_all_mids(memory)
        for k in ("ETH", "WETH"):
            if k in mids and mids[k] > 0:
                px = float(mids[k])
                break
    except Exception:
        pass
    _ETH_USD_CACHE = (now_m, px)
    return px


async def etherscan_account_txs(
    address: str,
    memory: "MemoryEngine | None" = None,
    *,
    offset: int = 50,
) -> list[dict]:
    """Native ETH txs (desc). Cache 5m. Fail-soft []."""
    return await _etherscan_list(address, "txlist", memory, offset=offset)


async def etherscan_token_txs(
    address: str,
    memory: "MemoryEngine | None" = None,
    *,
    offset: int = 80,
) -> list[dict]:
    """ERC-20 token txs (desc). Cache 5m. Fail-soft []."""
    return await _etherscan_list(address, "tokentx", memory, offset=offset)


async def _etherscan_list(
    address: str,
    action: str,
    memory: "MemoryEngine | None",
    *,
    offset: int,
) -> list[dict]:
    global _ETHERSCAN_CACHE
    key = (os.environ.get("ETHERSCAN_API_KEY") or "").strip()
    if not key or not address:
        return []
    cache_key = f"{action}:{address.lower()}"
    now_m = time.monotonic()
    hit = _ETHERSCAN_CACHE.get(cache_key)
    if hit and now_m - hit[0] < _ETHERSCAN_CACHE_TTL:
        return hit[1]

    if not _circuit_allow("etherscan_rest"):
        return hit[1] if hit else []

    async def _call() -> Any:
        # Etherscan API V2 — wajib chainid=1 (ETH mainnet); V1 deprecated
        qs = (
            f"{ETHERSCAN_API}?chainid=1&module=account&action={action}"
            f"&address={address}&startblock=0&endblock=99999999"
            f"&page=1&offset={offset}&sort=desc&apikey={key}"
        )
        return await http_get_json(qs, timeout=20.0)

    try:
        raw = await with_retry("etherscan_rest", _call, memory)
    except Exception as e:
        logger.debug(
            "etherscan %s fail-soft %s: %s",
            action, address[:10], type(e).__name__,
        )
        return hit[1] if hit else []

    rows: list[dict] = []
    if isinstance(raw, dict) and str(raw.get("status")) == "1":
        result = raw.get("result") or []
        if isinstance(result, list):
            rows = [r for r in result if isinstance(r, dict)]
    elif isinstance(raw, dict):
        msg = str(raw.get("result") or raw.get("message") or "")
        if "rate limit" in msg.lower() or "Max rate" in msg:
            _circuit_cooldown("etherscan_rest", _ETHERSCAN_RATE_COOLDOWN_SEC)
            # Rate-limit ≠ "tidak ada tx": jangan cache [] (menimpa data segar
            # 5 menit) — pakai cache lama bila ada, tanpa memperbarui timestamp.
            return hit[1] if hit else []
        elif msg and "deprecated" in msg.lower():
            logger.warning("etherscan V1 deprecated — use v2/api · %s", msg[:80])
        elif msg and action == "tokentx":
            # log once-level debug; probe will surface sample_txs
            logger.debug("etherscan %s notok · %s", action, msg[:80])

    _ETHERSCAN_CACHE[cache_key] = (now_m, rows)
    return rows


# USDT / USDC mainnet — mayoritas flow exchange = stable, bukan native ETH
_STABLE_CONTRACTS = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
}


def _tx_usd_native(tx: dict, eth_px: float) -> float:
    try:
        val_wei = int(tx.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0
    if val_wei <= 0:
        return 0.0
    return (val_wei / 1e18) * eth_px


def _tx_usd_token(tx: dict) -> float:
    """USD value for known stables only (1:1)."""
    contract = str(tx.get("contractAddress") or "").lower()
    meta = _STABLE_CONTRACTS.get(contract)
    if not meta:
        return 0.0
    _, decimals = meta
    try:
        raw = int(tx.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0
    if raw <= 0:
        return 0.0
    return raw / (10 ** decimals)


async def compute_exchange_netflow_usd(
    memory: "MemoryEngine | None" = None,
    lookback_hours: float = 6.0,
) -> float | None:
    """
    Netflow exchange wallets (native ETH + USDT/USDC).
    Inflow ke address = deposit (bearish bias untuk crowded long).
    Outflow = withdraw (bullish bias).
    netflow = inflow_usd − outflow_usd.
    """
    global _NETFLOW_CACHE
    now_m = time.monotonic()
    if now_m - _NETFLOW_CACHE[0] < _NETFLOW_CACHE_TTL and _NETFLOW_CACHE[1] is not None:
        return _NETFLOW_CACHE[1]

    wallets = parse_exchange_wallets()
    if not wallets or not _env_ok("ETHERSCAN_API_KEY"):
        _NETFLOW_CACHE = (now_m, None)
        return None

    eth_px = await _eth_usd_price(memory)
    cutoff = time.time() - lookback_hours * 3600.0
    inflow = 0.0
    outflow = 0.0
    n_tx = 0

    for label, addr in wallets[:8]:
        addr_l = addr.lower()
        native = await etherscan_account_txs(addr, memory)
        await asyncio.sleep(0.21)
        tokens = await etherscan_token_txs(addr, memory)
        await asyncio.sleep(0.21)

        for tx, kind in [(t, "eth") for t in native] + [(t, "tok") for t in tokens]:
            try:
                ts = int(tx.get("timeStamp") or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff:
                continue
            usd = _tx_usd_native(tx, eth_px) if kind == "eth" else _tx_usd_token(tx)
            if usd < 100:  # noise floor $100
                continue
            frm = str(tx.get("from") or "").lower()
            to = str(tx.get("to") or "").lower()
            if to == addr_l:
                inflow += usd
                direction = "IN"
            elif frm == addr_l:
                outflow += usd
                direction = "OUT"
            else:
                continue
            n_tx += 1
            if memory is not None and getattr(memory, "_write_queue", None) is not None:
                try:
                    memory._write_queue.put_nowait((
                        "wallet_txlog",
                        {
                            "address": addr,
                            "direction": direction,
                            "amount_usd": float(usd),
                            "to_label": label,
                            "tx_hash": str(tx.get("hash") or ""),
                            "recorded_at": datetime.fromtimestamp(
                                ts, tz=UTC
                            ).isoformat(),
                        },
                    ))
                except Exception:
                    pass

    net = inflow - outflow
    _NETFLOW_CACHE = (now_m, net)
    if memory is not None:
        try:
            await memory.flush_writes()
        except Exception:
            pass
    logger.info(
        "🔗 etherscan flow · wallets=%d · txs=%d · in=$%.0f out=$%.0f net=$%.0f",
        len(wallets), n_tx, inflow, outflow, net,
    )
    return net


def netflow_history_from_db(
    memory: "MemoryEngine | None",
    lookback_days: int = 30,
) -> list[float]:
    """Bucket netflow samples from wallet_txlog for percentile thresholds."""
    if memory is None or memory._conn is None:
        return []
    cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
    try:
        rows = memory._conn.execute(
            "SELECT direction, amount_usd, recorded_at FROM wallet_txlog "
            "WHERE recorded_at >= ? ORDER BY recorded_at",
            (cutoff,),
        ).fetchall()
    except Exception:
        return []
    buckets: dict[str, float] = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r["recorded_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        h = (dt.hour // 6) * 6
        key = dt.replace(hour=h, minute=0, second=0, microsecond=0).isoformat()
        signed = float(r["amount_usd"] or 0)
        if str(r["direction"]).upper() in ("OUT", "OUTFLOW"):
            signed = -signed
        buckets[key] = buckets.get(key, 0.0) + signed
    return list(buckets.values())


async def probe_etherscan() -> tuple[bool, str]:
    """Live probe — Etherscan V2 txlist+tokentx sample dari wallet env."""
    if not _env_ok("ETHERSCAN_API_KEY"):
        return False, "no ETHERSCAN_API_KEY"
    wallets = parse_exchange_wallets()
    addr = wallets[0][1] if wallets else "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    key = (os.environ.get("ETHERSCAN_API_KEY") or "").strip()
    try:
        # Direct V2 call so probe surfaces key/migration errors clearly
        qs = (
            f"{ETHERSCAN_API}?chainid=1&module=account&action=tokentx"
            f"&address={addr}&page=1&offset=5&sort=desc&apikey={key}"
        )
        raw = await http_get_json(qs, timeout=20.0)
        if not isinstance(raw, dict):
            return False, "bad response"
        if str(raw.get("status")) != "1":
            msg = str(raw.get("result") or raw.get("message") or "NOTOK")[:60]
            return False, f"api: {msg}"
        tokens = raw.get("result") or []
        n_tok = len(tokens) if isinstance(tokens, list) else 0
        native = await etherscan_account_txs(addr, None, offset=5)
        n = n_tok + len(native)
        return True, f"ok · wallets={len(wallets)} · sample_txs={n}"
    except Exception as e:
        return False, str(e)[:40]



def flow_confirm(
    direction: str,
    netflow: float | None,
    netflow_p80: float | None = None,
    netflow_p20: float | None = None,
) -> ConfirmResult:
    """
    §X.5: SHORT + inflow tinggi (p80) = bearish confirm;
    LONG + outflow (p20) = bullish confirm.
    netflow None → agent off (wallet env kosong / no key).
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
        "no oi delta",
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
    # Wajib ≥1 confirm REAL — pure-fallback (semua echo) = funding printer, reject.
    if real_count < 1:
        return False, count, real_count, n
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
        # risk 2×ATR, reward 6×ATR → natural RR 3.0 (= MIN_RR SWING).
        # Sebelumnya TP2=5× → RR 2.5 < min_rr 3.0 → SEMUA SWING ATR mati.
        sl_mult, tp1_mult, tp2_mult = 2.0, 3.0, 6.0
        min_rr = MIN_RR["SWING"]
    else:  # INTRADAY
        sl_mult, tp1_mult, tp2_mult = 1.5, 1.5, 3.0
        min_rr = MIN_RR["INTRADAY"]

    # Zone sempit (0.4×ATR) — DIRECTIONAL, bukan center di mid market.
    # LONG: zone di bawah harga (tunggu pullback). SHORT: zone di atas (tunggu bounce).
    # Kalau zone center di price → candle pertama selalu overlap → ARMED instan (bug).
    zone_w = clamp(ENTRY_ZONE_ATR_WIDTH, ENTRY_ZONE_ATR_MIN, ENTRY_ZONE_ATR_MAX) * atr
    # Offset: tepi zone terdekat ≈ 0.15×ATR dari harga sekarang (luar overlap instan)
    gap = max(0.15 * atr, zone_w * 0.25)
    if direction == "LONG":
        entry_high = price - gap
        entry_low = entry_high - zone_w
        if entry_low <= 0:
            entry_low = max(price * 0.995, entry_high * 0.99)
        entry_mid = (entry_low + entry_high) / 2.0
        stop = entry_mid - sl_mult * atr
        tp1 = entry_mid + tp1_mult * atr
        tp2 = entry_mid + tp2_mult * atr
        risk = entry_mid - stop
        reward = tp2 - entry_mid
    elif direction == "SHORT":
        entry_low = price + gap
        entry_high = entry_low + zone_w
        entry_mid = (entry_low + entry_high) / 2.0
        stop = entry_mid + sl_mult * atr
        tp1 = entry_mid - tp1_mult * atr
        tp2 = entry_mid - tp2_mult * atr
        risk = stop - entry_mid
        reward = entry_mid - tp2
    else:
        return None

    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr - 1e-9:
        return None

    norm = normalize_timing_levels(
        direction, price, atr,
        entry_low, entry_high, stop, tp1, tp2, horizon,
    )
    if norm is None:
        return None
    entry_low, entry_high, stop, tp1, tp2, rr = norm

    rr_quality = min(rr / 4.0, 1.0)  # RR 4 = full quality point
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
# Risk floor — cegah SL mepet entry (R:R 900+ / instant stop di noise)
MIN_RISK_ATR_FRAC = 0.35            # risk ≥ 0.35×ATR
MIN_RISK_PRICE_FRAC = 0.0010        # risk ≥ 0.10% harga (alt kecil)
# Structural RR sengaja TIDAK di-cap: menggeser SL demi RR cantik merusak basis
# stop & analisis outcome. Tidak ada konstanta cap RR.



def _min_risk(price: float, atr: float) -> float:
    return max(atr * MIN_RISK_ATR_FRAC, price * MIN_RISK_PRICE_FRAC, 1e-12)


def normalize_timing_levels(
    direction: str,
    price: float,
    atr: float,
    entry_low: float,
    entry_high: float,
    stop: float,
    tp1: float,
    tp2: float,
    horizon: str,
) -> tuple[float, float, float, float, float, float] | None:
    """
    Enforce SL side + minimum risk.
    Structural RR is intentionally not capped: moving SL to manufacture a
    prettier RR corrupts the stop basis and the later outcome analysis.
    Return (entry_low, entry_high, stop, tp1, tp2, rr) or None if irreparable.
    """
    if price <= 0 or atr <= 0:
        return None
    if entry_high < entry_low:
        entry_low, entry_high = entry_high, entry_low
    entry_mid = (entry_low + entry_high) / 2.0
    min_r = _min_risk(price, atr)

    if horizon == "SCALPING":
        tp1_mult, tp2_mult = 1.0, 1.5
        min_rr = MIN_RR["SCALPING"]
    elif horizon == "SWING":
        tp1_mult, tp2_mult = 3.0, 6.0  # match ATR path natural RR 3.0
        min_rr = MIN_RR["SWING"]
    else:
        tp1_mult, tp2_mult = 1.5, 3.0
        min_rr = MIN_RR["INTRADAY"]

    if direction == "LONG":
        # SL must be strictly below entry zone
        if stop >= entry_low:
            stop = entry_low - min_r
        risk = entry_mid - stop
        if risk < min_r:
            stop = entry_mid - min_r
            risk = min_r
        if stop >= entry_low:
            stop = entry_low - max(min_r * 0.5, price * 1e-6)
            risk = entry_mid - stop
        if risk <= 0:
            return None
        # Ensure TPs above entry
        if tp1 <= entry_high:
            tp1 = entry_mid + tp1_mult * atr
        if tp2 <= tp1:
            tp2 = entry_mid + tp2_mult * atr
        reward = tp2 - entry_mid
    elif direction == "SHORT":
        # SL must be strictly above entry zone
        if stop <= entry_high:
            stop = entry_high + min_r
        risk = stop - entry_mid
        if risk < min_r:
            stop = entry_mid + min_r
            risk = min_r
        if stop <= entry_high:
            stop = entry_high + max(min_r * 0.5, price * 1e-6)
            risk = stop - entry_mid
        if risk <= 0:
            return None
        if tp1 >= entry_low:
            tp1 = entry_mid - tp1_mult * atr
        if tp2 >= tp1:
            tp2 = entry_mid - tp2_mult * atr
        reward = entry_mid - tp2
    else:
        return None

    if risk <= 0:
        return None
    rr = reward / risk
    if rr < min_rr - 1e-9:
        return None
    # Final side check
    if direction == "LONG" and not (stop < entry_low and tp2 > entry_high):
        return None
    if direction == "SHORT" and not (stop > entry_high and tp2 < entry_low):
        return None
    return entry_low, entry_high, stop, tp1, tp2, rr


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

    # Normalize: SL side + min-risk floor saja. RR TIDAK di-cap (cap lama menggeser SL
    # demi RR cantik → merusak basis stop & analisis outcome). Kalau floor melebarkan
    # SL, sl_basis diberi label 'structure_widened_min_risk'.
    structural_stop = stop
    norm = normalize_timing_levels(
        direction, price, atr,
        entry_low, entry_high, stop, tp1, tp2, horizon,
    )
    if norm is None:
        return None
    entry_low, entry_high, stop, tp1, tp2, rr = norm
    stop_widened = not math.isclose(
        stop, structural_stop,
        rel_tol=1e-12,
        abs_tol=max(1e-12, atr * 1e-9),
    )

    rr_quality = min(rr / 4.0, 1.0)  # RR 4 = full quality point
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
        sl_basis="structure_widened_min_risk" if stop_widened else "structure",
        entry_basis=entry_basis,
    )


def assign_tier(rr: float, confirm_count: int) -> str:
    """
    §I.2 Layer 6 tier.
    A: RR≥2.5 + 3 confirms | B: RR≥2.0 + 2 confirms | C: sisanya.
    Epsilon 1e-9: hindari float miss (RR desain 3.0/1.5 = 2.0 tepat).
    Catatan: confidence ≠ tier. Conf tinggi + tier C = trigger kuat tapi
    confirm/RR belum tembus gerbang B (harusnya jarang untuk INTRADAY).

    CATATAN: di env dengan cross-exchange terblok (mis. GH Actions), source
    real yang tersedia semuanya dari HL: microstructure book, taker trade-flow
    (HLTradeFlowBuffer), dan ΔOI×Δharga (OISnapshotBuffer, hanya non-cascade &
    setelah ada baseline ≥10 menit — di-seed dari metric_oi saat startup, jadi
    hanya DB kosong / jeda restart >1 jam yang mengulang warm-up). Maka
    real_count max = 3 → Tier A reachable tanpa Binance/Bybit/Etherscan; tanpa
    ΔOI (cascade / belum ada baseline) max = 2 → Tier B. Ambang TIER_*_MIN_CONFIRMS tidak dilonggarkan.
    """
    if rr + 1e-9 >= TIER_A_MIN_RR and confirm_count >= TIER_A_MIN_CONFIRMS:
        return "A"
    if rr + 1e-9 >= TIER_B_MIN_RR and confirm_count >= TIER_B_MIN_CONFIRMS:
        return "B"
    return "C"


# ----- L1 scan helper (semua trigger per asset) -----


def compute_confluence(
    *,
    real_count: int,
    has_structure: bool,
    structure_aligned: bool,
    htf_ok: bool,
    micro_real: bool,
    cascade_boost: bool = False,
) -> int:
    """
    §IV.7 soft confluence 0–5 (display + mild conf boost, bukan pengganti confirm).
    1) real confirm ≥1  2) real confirm ≥2  3) structure aligned
    4) HTF ok  5) micro book real / cascade boost
    """
    score = 0
    if real_count >= 1:
        score += 1
    if real_count >= 2:
        score += 1
    if has_structure and structure_aligned:
        score += 1
    if htf_ok:
        score += 1
    if micro_real or cascade_boost:
        score += 1
    return min(score, 5)



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
    confluence: int = 0          # 0–5 score (§IV.7 / §XII)
    real_count: int = 0          # non-fallback confirms (tier gate)



# --- CoinGecko sector fallback (§III.4 tier-2, zero-auth, NOT price source) ---
_CG_LIST_CACHE: tuple[float, dict[str, str]] = (0.0, {})  # mono → {symbol_upper: id}
_CG_LIST_TTL = 86400.0
_CG_CATEGORY_MAP = {
    "layer-1": "L1",
    "layer-2": "L2",
    "decentralized-finance-defi": "DEFI",
    "decentralized-exchange": "DEFI",
    "lending-borrowing": "DEFI",
    "yield-farming": "DEFI",
    "meme-token": "MEME",
    "memes": "MEME",
    "artificial-intelligence": "AI",
    "ai-agents": "AI",
    "gaming": "GAMEFI",
    "play-to-earn": "GAMEFI",
    "metaverse": "GAMEFI",
    "oracle": "INFRA",
    "infrastructure": "INFRA",
    "stablecoins": "STABLE",
    "real-world-assets-rwa": "RWA",
    "tokenized-real-world-assets": "RWA",
}


# Hard overrides — ticker collision di /coins/list (BTC≠batcat)
_CG_ID_OVERRIDE: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "DOT": "polkadot", "LINK": "chainlink", "ATOM": "cosmos", "NEAR": "near",
    "APT": "aptos", "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
    "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk", "WLD": "worldcoin-wld",
    "TAO": "bittensor", "AAVE": "aave", "UNI": "uniswap", "MKR": "maker",
    "LDO": "lido-dao", "CRV": "curve-dao-token", "PENDLE": "pendle",
    "ENA": "ethena", "JUP": "jupiter-exchange-solana", "PYTH": "pyth-network",
    "TIA": "celestia", "SEI": "sei-network", "INJ": "injective-protocol",
    "FET": "fetch-ai", "RENDER": "render-token", "HYPE": "hyperliquid",
}


async def _coingecko_symbol_id(symbol: str) -> str | None:
    """Map HL ticker → CoinGecko id via /coins/list (cached 24h)."""
    global _CG_LIST_CACHE
    ticker = symbol.upper()
    if ticker.startswith("1000") and len(ticker) > 4:
        ticker = ticker[4:]
    if ticker in _CG_ID_OVERRIDE:
        return _CG_ID_OVERRIDE[ticker]

    now_m = time.monotonic()
    if now_m - _CG_LIST_CACHE[0] < _CG_LIST_TTL and _CG_LIST_CACHE[1]:
        mapping = _CG_LIST_CACHE[1]
    else:
        if not _circuit_allow("coingecko_rest"):
            return (_CG_LIST_CACHE[1] or {}).get(ticker)

        async def _call() -> Any:
            return await http_get_json(
                "https://api.coingecko.com/api/v3/coins/list",
                timeout=25.0,
            )

        try:
            raw = await with_retry("coingecko_rest", _call, None)
        except Exception as e:
            logger.debug("coingecko list fail-soft: %s", type(e).__name__)
            return None
        # Collect candidates per symbol; prefer id == name-ish / shortest
        buckets: dict[str, list[tuple[str, str]]] = {}
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or "").upper()
                cid = str(row.get("id") or "").strip()
                name = str(row.get("name") or "").strip().lower()
                if not sym or not cid:
                    continue
                buckets.setdefault(sym, []).append((cid, name))
        mapping = {}
        for sym, cands in buckets.items():
            # Prefer id that equals symbol lower, or name starts with symbol
            cands_sorted = sorted(
                cands,
                key=lambda t: (
                    0 if t[0] == sym.lower() else 1,
                    0 if t[1].startswith(sym.lower()) else 1,
                    len(t[0]),
                ),
            )
            mapping[sym] = cands_sorted[0][0]
        if mapping:
            _CG_LIST_CACHE = (now_m, mapping)
    return mapping.get(ticker)


async def coingecko_sector_tag(symbol: str) -> str | None:
    """
    §III.4 fallback — CoinGecko categories → sector whitelist.
    Hanya tagging; bukan source harga/OHLCV (HL tetap primary).
    """
    cid = await _coingecko_symbol_id(symbol)
    if not cid:
        return None
    if not _circuit_allow("coingecko_rest"):
        return None

    async def _call() -> Any:
        return await http_get_json(
            f"https://api.coingecko.com/api/v3/coins/{cid}"
            f"?localization=false&tickers=false&market_data=false"
            f"&community_data=false&developer_data=false&sparkline=false",
            timeout=20.0,
        )

    try:
        raw = await with_retry("coingecko_rest", _call, None)
    except Exception as e:
        logger.debug("coingecko coin fail-soft %s: %s", symbol, type(e).__name__)
        return None
    if not isinstance(raw, dict):
        return None
    cats = raw.get("categories") or []
    if not isinstance(cats, list):
        return None
    for cat in cats:
        key = str(cat or "").strip().lower().replace(" ", "-")
        # direct map
        for cg_key, tag in _CG_CATEGORY_MAP.items():
            if cg_key in key or key in cg_key:
                return tag
        # loose
        low = str(cat or "").lower()
        if "meme" in low:
            return "MEME"
        if "defi" in low or "dex" in low:
            return "DEFI"
        if "layer 1" in low or "layer-1" in low:
            return "L1"
        if "layer 2" in low or "layer-2" in low or "rollup" in low:
            return "L2"
        if "artificial intelligence" in low or low == "ai":
            return "AI"
        if "game" in low:
            return "GAMEFI"
        if "stable" in low:
            return "STABLE"
        if "real-world" in low or "rwa" in low:
            return "RWA"
    return "OTHER"


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
    dxy: float | None = None,
    fear_greed: float | None = None,
    anchor_corr: float | None = None,
    news_bias: str | None = None,
    sector: str | None = None,
    sector_bias: str | None = None,
    stablecoin_flow_7d_pct: float | None = None,
    cryptopanic_bias: str | None = None,
) -> float:
    """
    L5 Context — multiplier 0.5–1.2 (§I.2).
    Fail-soft: data kosong → 1.0 (netral).
    US10Y / DXY / F&G / corr / RSS / sector / stablecoin flow / CryptoPanic.
    """
    mult = 1.0
    _ = (symbol, anchor)
    if us10y is not None:
        if us10y >= 4.5:
            mult *= 0.92 if direction == "LONG" else 1.05
        elif us10y <= 3.5:
            mult *= 1.05 if direction == "LONG" else 0.95
    # DXY kuat → risk-off crypto; lemah → risk-on
    if dxy is not None:
        if dxy >= 105.0:
            mult *= 0.94 if direction == "LONG" else 1.04
        elif dxy <= 100.0:
            mult *= 1.03 if direction == "LONG" else 0.97
    if fear_greed is not None:
        if fear_greed <= 25:
            mult *= 1.08 if direction == "LONG" else 0.94
        elif fear_greed <= 40:
            mult *= 1.03 if direction == "LONG" else 0.97
        elif fear_greed >= 75:
            mult *= 0.94 if direction == "LONG" else 1.08
        elif fear_greed >= 60:
            mult *= 0.97 if direction == "LONG" else 1.03
    if anchor_corr is not None:
        if anchor_corr >= 0.75:
            mult *= 0.97
        elif anchor_corr <= 0.2:
            mult *= 1.03
    if news_bias in ("BULLISH", "BEARISH"):
        if news_bias == "BULLISH":
            mult *= 1.04 if direction == "LONG" else 0.96
        else:
            mult *= 1.04 if direction == "SHORT" else 0.96
    if sector and sector_bias in ("BULLISH", "BEARISH"):
        if sector_bias == "BULLISH":
            mult *= 1.03 if direction == "LONG" else 0.97
        else:
            mult *= 1.03 if direction == "SHORT" else 0.97
    # DefiLlama stablecoin circulating 7d % — inflow = risk-on soft
    if stablecoin_flow_7d_pct is not None:
        if stablecoin_flow_7d_pct >= 1.5:
            mult *= 1.03 if direction == "LONG" else 0.97
        elif stablecoin_flow_7d_pct <= -1.5:
            mult *= 0.97 if direction == "LONG" else 1.03
    if cryptopanic_bias in ("BULLISH", "BEARISH"):
        if cryptopanic_bias == "BULLISH":
            mult *= 1.03 if direction == "LONG" else 0.97
        else:
            mult *= 1.03 if direction == "SHORT" else 0.97
    return clamp(mult, 0.5, 1.2)



# Official FOMC decision days 2026–2027 (statement ~18:00 UTC = 14:00 ET)
_FOMC_DECISION_DAYS: tuple[str, ...] = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-05-05", "2027-06-16",
    "2027-07-28", "2027-09-15", "2027-11-03", "2027-12-15",
)


def _first_friday(year: int, month: int) -> datetime:
    """First Friday of month at 12:30 UTC (NFP release window)."""
    d = datetime(year, month, 1, 12, 30, tzinfo=UTC)
    # weekday: Mon=0 … Fri=4
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def builtin_macro_events(horizon_days: int = 45) -> list[dict]:
    """
    Seed kalender zero-scrape: FOMC resmi + NFP (Jumat pertama).
    Selalu tersedia tanpa Gemini/env — L6 blackout tidak buta.
    """
    now = now_utc()
    end = now + timedelta(days=horizon_days)
    out: list[dict] = []
    for day in _FOMC_DECISION_DAYS:
        try:
            dt = datetime.fromisoformat(day).replace(
                hour=18, minute=0, second=0, tzinfo=UTC,
            )
        except ValueError:
            continue
        if now - timedelta(hours=2) <= dt <= end:
            out.append({"name": "FOMC", "at": dt, "impact": "high"})
    # NFP: first Friday of current + next 2 months
    y, m = now.year, now.month
    for _ in range(3):
        nfp = _first_friday(y, m)
        if now - timedelta(hours=2) <= nfp <= end:
            out.append({"name": "NFP", "at": nfp, "impact": "high"})
        m += 1
        if m > 12:
            m = 1
            y += 1
    out.sort(key=lambda x: x["at"])
    return out


def _normalize_macro_items(data: list) -> list[dict]:
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("event") or "macro").strip()
        at_raw = item.get("at") or item.get("time") or item.get("when")
        if not name or not at_raw:
            continue
        try:
            if isinstance(at_raw, datetime):
                dt = at_raw
            else:
                dt = datetime.fromisoformat(str(at_raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        impact = str(item.get("impact") or "high").strip().lower()
        if impact not in ("high", "medium", "low"):
            impact = "high"
        out.append({"name": name, "at": dt, "impact": impact})
    return out


def parse_macro_events(
    memory: "MemoryEngine | None" = None,
) -> list[dict]:
    """
    §V.3 #12 Forex Factory role — event calendar.
    Merge (dedup by name+day):
      1. builtin (FOMC + NFP) — selalu ada
      2. memory runtime macro_events_json (Gemini harian)
      3. CRYPTONE_MACRO_EVENTS env JSON (override manual)
    Env/Gemini entries menang atas builtin untuk name+hari yang sama.
    """
    merged: dict[tuple[str, str], dict] = {}

    def _put(items: list[dict], *, prefer: bool) -> None:
        for ev in items:
            key = (ev["name"].upper(), ev["at"].strftime("%Y-%m-%d"))
            if prefer or key not in merged:
                merged[key] = ev

    _put(builtin_macro_events(), prefer=False)

    raw = ""
    if memory is not None:
        raw = (memory.get_runtime("macro_events_json") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                _put(_normalize_macro_items(data), prefer=True)
        except json.JSONDecodeError:
            logger.warning("macro_events runtime invalid JSON — ignore")

    env_raw = (os.environ.get("CRYPTONE_MACRO_EVENTS") or "").strip()
    if env_raw:
        try:
            data = json.loads(env_raw)
            if isinstance(data, list):
                _put(_normalize_macro_items(data), prefer=True)
        except json.JSONDecodeError:
            logger.warning("CRYPTONE_MACRO_EVENTS invalid JSON — ignore")

    out = sorted(merged.values(), key=lambda x: x["at"])
    return out


def macro_events_today(memory: "MemoryEngine | None" = None) -> list[dict]:
    """High/medium events in local-UTC calendar day (today)."""
    now = now_utc()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + timedelta(days=1)
    return [
        ev for ev in parse_macro_events(memory)
        if day0 <= ev["at"] < day1 and ev["impact"] in ("high", "medium")
    ]


def format_macro_digest(memory: "MemoryEngine | None" = None) -> str:
    """Teks alert Telegram: event hari ini + next 7 hari."""
    now = now_utc()
    events = parse_macro_events(memory)
    today = macro_events_today(memory)
    week = [
        ev for ev in events
        if now - timedelta(hours=1) <= ev["at"] <= now + timedelta(days=7)
        and ev["impact"] in ("high", "medium")
    ]
    lines = ["📅 MACRO CALENDAR (FF role)", "━━━━━━━━━━━━━━━━━━━"]
    if today:
        lines.append("Hari ini:")
        for ev in today:
            t = ev["at"].strftime("%H:%MZ")
            lines.append(f"  • {ev['name']} · {t} · {ev['impact'].upper()}")
            # blackout note
            delta = (ev["at"] - now).total_seconds() / 60.0
            if abs(delta) < EVENT_BLACKOUT_MIN:
                lines.append(f"    ⛔ BLACKOUT aktif ({delta:+.0f}m)")
    else:
        lines.append("Hari ini: tidak ada high-impact")
    lines.append("")
    lines.append("7 hari ke depan:")
    if not week:
        lines.append("  — kosong")
    else:
        for ev in week[:8]:
            d = ev["at"].strftime("%a %d %b %H:%MZ")
            lines.append(f"  • {ev['name']} · {d} · {ev['impact'].upper()}")
    lines.append("")
    lines.append(
        f"Engine: veto sinyal baru ±{EVENT_BLACKOUT_MIN}m di sekitar event high/medium."
    )
    return "\n".join(lines)


def minutes_to_next_macro(
    memory: "MemoryEngine | None" = None,
) -> tuple[float | None, str | None]:
    """
    Menit ke event makro berikutnya (high/medium).
    Prioritas: env override → runtime (Gemini) → CRYPTONE_MACRO_EVENTS.
    Return (minutes, event_name) atau (None, None).
    """
    raw = (os.environ.get("CRYPTONE_MINUTES_TO_NEXT_MACRO") or "").strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return float(raw), "override"
    events = parse_macro_events(memory)
    if not events:
        return None, None
    now = now_utc()
    # 1) Event di dalam blackout window (±60m) — prioritas
    near = None
    future = None
    for ev in events:
        if ev["impact"] == "low":
            continue
        delta_m = (ev["at"] - now).total_seconds() / 60.0
        if -EVENT_BLACKOUT_MIN <= delta_m <= EVENT_BLACKOUT_MIN:
            if near is None or abs(delta_m) < abs(near[0]):
                near = (delta_m, str(ev["name"]))
        elif delta_m > 0 and (future is None or delta_m < future[0]):
            future = (delta_m, str(ev["name"]))
    if near is not None:
        return near
    if future is not None:
        return future
    return None, None


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
    # Macro blackout dulu (env-only, tidak butuh DB)
    mins, ev_name = minutes_to_next_macro(memory)
    if mins is not None and abs(mins) < EVENT_BLACKOUT_MIN:
        label = ev_name or "macro"
        return False, f"macro blackout {label} {mins:.0f}m"
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
    return True, "ok"


# Read-through cache ATR 1h. ATR14 dari bar 1h berubah lambat, jadi refetch tiap
# cycle 5 menit untuk ~178 simbol itu boros (178 REST call serial). Hanya hasil
# fetch LIVE yang di-cache; fallback DB TIDAK pernah di-cache
# supaya data buruk tidak "menempel" sampai TTL habis.
# Trade-off: baris metric_atr (basis P20 compression) terisi ~1 sampel per TTL
# per simbol, bukan per cycle — sampel 5-menit berdekatan sangat berautokorelasi
# jadi distribusi P20 nyaris tidak berubah, hanya cold-start (<30 sampel) lebih lama.
ATR_CACHE_TTL_SEC = 900.0            # 15 menit = 3 cycle default
ATR_PREFETCH_CONCURRENCY = 6         # paralel terbatas, hindari burst rate-limit HL
# Jejak audit fail-closed: ATR tak tersedia (candle+DB kosong) → WARNING, tapi
# maksimal 1x per simbol per jam (outage HL = ~178 simbol × tiap cycle → spam).
ATR_UNAVAILABLE_WARN_TTL_SEC = 3600.0
_ATR_UNAVAILABLE_WARNED: dict[str, float] = {}
_ATR_CACHE: dict[str, tuple[float, float]] = {}  # symbol → (monotonic_ts, atr)


def _atr_cache_get(symbol: str) -> float | None:
    hit = _ATR_CACHE.get(symbol)
    if hit is None:
        return None
    if time.monotonic() - hit[0] >= ATR_CACHE_TTL_SEC:
        _ATR_CACHE.pop(symbol, None)
        return None
    return hit[1]


async def estimate_atr(
    symbol: str,
    price: float,
    memory: "MemoryEngine | None",
) -> float | None:
    """
    ATR 1h dari candle HL (read-through cache TTL ATR_CACHE_TTL_SEC);
    fallback histori DB. Tanpa keduanya → None (fail-closed): ATR fabrikasi
    (mis. flat 1% harga) akan lolos filter L0 dan membentuk SL/TP palsu.
    Caller sudah menangani None (tidak ada sinyal / trigger compression skip).
    """
    cached = _atr_cache_get(symbol)
    if cached is not None:
        return cached
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
            _ATR_CACHE[symbol] = (time.monotonic(), atr)
            _ATR_UNAVAILABLE_WARNED.pop(symbol, None)
            return atr
    except DataUnavailable as e:
        logger.debug("ATR fetch %s fail-soft: %s", symbol, e)
    if memory is not None:
        hist = memory.get_atr_history(symbol, "1h")
        if hist:
            return hist[-1]
    now_m = time.monotonic()
    last = _ATR_UNAVAILABLE_WARNED.get(symbol)
    if last is None or now_m - last >= ATR_UNAVAILABLE_WARN_TTL_SEC:
        _ATR_UNAVAILABLE_WARNED[symbol] = now_m
        logger.warning(
            "estimate_atr %s: candle & histori DB kosong → None "
            "(fail-closed: tidak membentuk SL/TP dari ATR fabrikasi)",
            symbol,
        )
    else:
        logger.debug("estimate_atr %s: ATR tidak tersedia → None", symbol)
    return None


async def prefetch_atr(
    items: list[tuple[str, float]],
    memory: "MemoryEngine | None",
) -> int:
    """
    Hangatkan _ATR_CACHE untuk simbol yang belum segar, paralel terbatas
    (ATR_PREFETCH_CONCURRENCY). Fail-soft per simbol. Return jumlah simbol
    yang benar-benar di-fetch (cache miss) — dipakai untuk observability.
    """
    todo = [(s, p) for s, p in items if _atr_cache_get(s) is None]
    if not todo:
        return 0
    sem = asyncio.Semaphore(ATR_PREFETCH_CONCURRENCY)

    async def _one(sym: str, px: float) -> None:
        async with sem:
            try:
                await estimate_atr(sym, px, memory)
            except Exception as e:  # noqa: BLE001 — prefetch tidak boleh menjatuhkan cycle
                logger.debug("ATR prefetch %s fail-soft: %s", sym, type(e).__name__)

    await asyncio.gather(*(_one(s, p) for s, p in todo))
    return len(todo)


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
        return True, "htf_insufficient"
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


def calibrate_confidence(
    memory: "MemoryEngine | None",
    conf: float,
    symbol: str,
    setup_type: str | None,
) -> tuple[float, str]:
    """
    Tarik confidence model ke frekuensi kemenangan REAL (setup lalu simbol) lewat
    shrinkage Bayesian: conf' = (1-w)·conf + w·winrate, w = n/(n+CAL_SHRINK_K).
    PARTIAL berbobot PARTIAL_WEIGHT. Tanpa data → conf tidak berubah (return note "").
    Bukan `conf × trust_score`: perkalian literal menghukum simbol baru (trust 0.5
    → conf separuh) dan tidak membedakan sampel 2 vs 200. Perubahan dibatasi
    [-CAL_MAX_DOWN, +CAL_MAX_UP] supaya history era lama / sampel tipis tidak bisa
    membekukan setup (conf < floor → tak ada sinyal baru → tak ada data pulih).
    """
    if memory is None:
        return conf, ""
    cur = conf
    parts: list[str] = []
    scopes: list[tuple[str, dict[str, str]]] = []
    if setup_type:
        scopes.append(("setup", {"setup_type": setup_type}))
    if symbol:
        scopes.append(("sym", {"symbol": symbol}))
    for label, kw in scopes:
        wins, partials, losses = memory.outcome_counts(**kw)
        n = wins + partials + losses
        if n <= 0:
            continue
        winrate = (wins + PARTIAL_WEIGHT * partials) / n
        w = n / (n + CAL_SHRINK_K)
        cur = (1.0 - w) * cur + w * winrate
        parts.append(f"{label} n={n} wr={winrate:.2f}")
    if not parts:
        return conf, ""
    cur = clamp(cur, conf - CAL_MAX_DOWN, conf + CAL_MAX_UP)
    cur = clamp(cur, 0.0, 0.97)
    return cur, f"cal {conf:.2f}→{cur:.2f} ({'; '.join(parts)})"


async def process_trigger(
    asset: AssetCtx,
    trigger: TriggerHit,
    memory: "MemoryEngine | None",
    anchor: str | None,
    cascade_side: str | None = None,
    corr_map: dict[str, float] | None = None,
    hl_book: "HLBookBuffer | None" = None,
    hl_trade_flow: "HLTradeFlowBuffer | None" = None,
    atr_hint: float | None = None,
    netflow_hint: float | None = None,
    netflow_p80: float | None = None,
    netflow_p20: float | None = None,
    oi_history: "OISnapshotBuffer | None" = None,
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
            direction = resolve_cascade_direction(cascade_side) or cascade_side
        elif trigger.trigger_type == "volatility_compression":
            # Breakout detect dari candle 1h — hidupkan COMPRESSION (bukan dead code)
            try:
                end_ms = int(now_utc().timestamp() * 1000)
                start_ms = end_ms - 30 * 3_600_000  # ~30 bar 1h
                bars = await hl_candle_snapshot(
                    asset.symbol, "1h", start_ms, end_ms, memory,
                )
                direction = compression_breakout_direction(bars or [], lookback=20) or "NEUTRAL"
            except Exception:
                direction = "NEUTRAL"
            if direction not in ("LONG", "SHORT"):
                logger.debug(
                    "%s COMPRESSION skip · no breakout yet", asset.symbol,
                )
                return None
        else:
            return None

    # HTF 4H bias gate — cascade adalah event-driven exception (§B2).
    htf_ok, htf_note = True, "cascade_skip"
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
    trade_imbalance = (
        hl_trade_flow.get_imbalance(asset.symbol)
        if hl_trade_flow is not None else None
    )

    # Flow agent — Etherscan exchange netflow (§X). Prefer cycle-level hint.
    netflow_usd: float | None = netflow_hint
    nf_p80, nf_p20 = netflow_p80, netflow_p20
    if netflow_usd is None:
        try:
            netflow_usd = await compute_exchange_netflow_usd(memory, lookback_hours=6.0)
            if netflow_usd is not None and memory is not None:
                hist = netflow_history_from_db(memory)
                if len(hist) >= 5:
                    nf_p80 = percentile(hist, 80)
                    nf_p20 = percentile(hist, 20)
                else:
                    nf_p80, nf_p20 = 50_000.0, -50_000.0
        except Exception as e:
            logger.debug("%s flow fetch fail-soft: %s", asset.symbol, type(e).__name__)
            netflow_usd = None

    xref = cross_exchange_confirm(hl_px, ref_px, source=ref_src)
    confirms = [
        micro,
        trade_flow_confirm(direction, trade_imbalance),
        flow_confirm(direction, netflow_usd, nf_p80, nf_p20),
        xref,
    ]
    # Kalau semua xref mati (GH Actions block Binance/Bybit): satu agent
    # pengganti supaya L3 tidak stuck N=1. Prioritas: ΔOI×Δharga REAL (bila
    # snapshot sudah warm & bukan cascade) → membuka real_count=3 / Tier A;
    # kalau tidak, oi_structure (echo funding, is_fallback). DIGANTI, bukan
    # ditambah, jadi n_active & ambang `need` tidak berubah.
    if xref.detail == "no xref data":
        oi_delta = None
        if oi_history is not None and trigger.trigger_type != "cascade":
            oi_delta = oi_history.get_delta(asset.symbol, asset.open_interest, hl_px)
        if oi_delta is not None:
            confirms.append(oi_delta_confirm(direction, oi_delta))
        else:
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

    # L0/L4 ATR% gate — coin terlalu mati tidak layak trade (§I.2 / §IV.4)
    # Cascade exception: event-driven, boleh ATR kecil.
    if trigger.trigger_type != "cascade" and hl_px > 0:
        atr_pct = atr / hl_px
        if atr_pct < ATR_PCT_MIN_FILTER:
            logger.debug(
                "%s atr_pct_reject %.4f < %.4f",
                asset.symbol, atr_pct, ATR_PCT_MIN_FILTER,
            )
            return None

    # Fetch structure once — funding/compression wajib bukti struktur.
    structure_quick = await get_structure_context(asset.symbol, atr, memory)

    # FUNDING_SQUEEZE: tanpa structure context = printer noise → reject
    # Cascade tetap event-driven (boleh tanpa structure).
    if trigger.trigger_type == "funding_extreme" and structure_quick is None:
        logger.debug("%s funding_no_structure", asset.symbol)
        return None

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
        # Funding INTRADAY: wajib BOS/CHoCH ATAU sweep searah (no silent ATR-only)
        if trigger.trigger_type == "funding_extreme" and not (has_event or sweep_aligned):
            logger.debug(
                "%s funding_no_bos_sweep %s",
                asset.symbol, event_direction,
            )
            return None
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
        if trigger.trigger_type != "cascade" and trend_opposes:
            if not sweep_aligned:
                logger.debug(
                    "%s trend_oppose %s vs %s",
                    asset.symbol, structure_quick.last_trend, direction,
                )
                return None
            # Counter-trend lewat sweep: butuh bukti REAL independen, bukan
            # sweep sendirian (lihat COUNTER_TREND_MIN_REAL_CONFIRMS).
            if real_count < COUNTER_TREND_MIN_REAL_CONFIRMS:
                logger.debug(
                    "%s counter_trend_sweep_weak %s vs %s real=%d<%d",
                    asset.symbol, structure_quick.last_trend, direction,
                    real_count, COUNTER_TREND_MIN_REAL_CONFIRMS,
                )
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

    # L5 — FRED US10Y+DXY, F&G, DefiLlama, CryptoPanic, RSS (fail-soft)
    us10y = await fred_latest("DGS10") if _env_ok("FRED_API_KEY") else None
    dxy = await fred_dxy() if _env_ok("FRED_API_KEY") else None
    fng = await fear_greed_latest()
    stable_pct = await defillama_stablecoin_flow_7d_pct(memory)
    cp_bias = await cryptopanic_news_bias(memory)
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
        us10y=us10y, dxy=dxy, fear_greed=fng, anchor_corr=anchor_corr,
        news_bias=rss_news_bias(asset.symbol),
        sector=sector,
        sector_bias=sector_news_bias(sector),
        stablecoin_flow_7d_pct=stable_pct,
        cryptopanic_bias=cp_bias,
    )
    # Soft ceiling 0.97 — conf 100% hanya teoritis, tidak realistis di production
    conf_final = clamp(timing.base_confidence * ctx_mult, 0.0, 0.97)
    # Kalibrasi outcome-driven (rolling 30 hari, live only) SEBELUM floor veto.
    conf_final, cal_note = calibrate_confidence(
        memory, conf_final, asset.symbol, trigger.setup_type,
    )

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

    # Confluence score (display + mild boost max +5% conf)
    has_struct = structure_quick is not None
    struct_aligned = False
    if has_struct:
        event_direction = "UP" if direction == "LONG" else "DOWN"
        struct_aligned = any(
            e.kind in {"BOS", "CHoCH"} and e.direction == event_direction
            for e in structure_quick.events
        ) or (
            structure_quick.sweep is not None
            and structure_quick.sweep.direction == direction
        )
    micro_real = any(
        r.agent == "microstructure" and r.confirmed and not r.is_fallback
        for r in confirms
    )
    conf_score = compute_confluence(
        real_count=real_count,
        has_structure=has_struct,
        structure_aligned=struct_aligned,
        htf_ok=htf_ok,
        micro_real=micro_real,
        cascade_boost=(trigger.trigger_type == "cascade"),
    )
    # Mild boost only — tidak menggantikan confirm gate
    conf_final = clamp(conf_final * (1.0 + 0.01 * conf_score), 0.0, 0.97)

    reasoning = (
        f"{trigger.setup_type} | {trigger.trigger_type} "
        f"raw={trigger.raw_value:.6g} thr={trigger.threshold:.6g} "
        f"str={trigger.strength:.2f} | confirm {conf_count}/{n_active} "
        f"(real {real_count}) | confu {conf_score}/5 | "
        f"ctx×{ctx_mult:.2f} | sl={timing.sl_basis} entry={timing.entry_basis}"
        + (f" | {cal_note}" if cal_note else "")
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
        real_count=real_count,
        reasoning=reasoning,
        created_at=created,
        valid_until=valid_until,
        sl_basis=timing.sl_basis,
        entry_basis=timing.entry_basis,
        state="PENDING",
        confluence=conf_score,
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
                    # §III.4 tier-2: CoinGecko fallback kalau Gemini gagal
                    if sector is None:
                        try:
                            sector = await coingecko_sector_tag(asset.symbol)
                            if sector:
                                logger.debug(
                                    "sector tag · %s via coingecko → %s",
                                    asset.symbol, sector,
                                )
                        except Exception as e:
                            logger.debug(
                                "coingecko sector fail-soft %s: %s",
                                asset.symbol, type(e).__name__,
                            )
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
    get_hl_trade_flow_buffer().set_symbols([a.symbol for a in candidates])
    # Snapshot OI/harga tiap cycle → basis ΔOI×Δharga (oi_delta_confirm).
    await persist_oi_snapshots(memory, get_oi_snapshot_buffer().record(candidates))
    # Kasih WS loop 1 reconcile tick (HL_BOOK_RECONCILE_SEC=3) — jangan block lama
    await asyncio.sleep(0.3)

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

    paused_until = memory.get_runtime("signals_paused_until") if memory else None
    if paused_until:
        try:
            pu = datetime.fromisoformat(str(paused_until).replace("Z", "+00:00"))
            if pu.tzinfo is None:
                pu = pu.replace(tzinfo=UTC)
            if now_utc() < pu:
                logger.info("⏸ signals paused until %s — skip new", str(paused_until)[:19])
                return []
            memory.set_runtime("signals_paused_until", "")
        except (ValueError, TypeError):
            pass

    setup_enabled = memory.get_setup_enabled()
    # Setup yang sedang kena circuit-breaker (3 LOSS beruntun pada setup itu)
    # di-skip di L1 — bukan baru ditolak di L6 setelah seluruh kerja L2–L5
    # (fetch structure/HTF) terbuang — dan pause-nya dicatat di INFO supaya
    # terlihat di live log tanpa harus cek DB manual.
    paused_setups: list[str] = []
    for _st in list(setup_enabled):
        if setup_enabled.get(_st, True) and memory.setup_signals_paused(_st):
            setup_enabled[_st] = False
            _until = str(memory.get_runtime(f"setup_paused_until:{_st}") or "")
            paused_setups.append(f"{_st} until {_until[:19]}")
    if paused_setups:
        logger.info("⏸ setup-loss pause aktif · %s — L1 skip", " | ".join(paused_setups))
    produced: list[PipelineSignal] = []

    # Market funding polarity (observability — bukan filter)
    n_fund_pos = sum(1 for a in candidates if a.funding > 0)
    n_fund_neg = sum(1 for a in candidates if a.funding < 0)
    n_fund_zero = len(candidates) - n_fund_pos - n_fund_neg

    # Jangan rank berdasarkan |funding|: itu membuat FUNDING_SQUEEZE
    # memonopoli ATR budget dan COMPRESSION tidak pernah mendapat L1.
    # Cascade boleh diprioritaskan karena event-driven, tetapi funding level
    # tidak boleh menentukan urutan scan.
    ranked = sorted(
        candidates,
        key=lambda a: float((cascade_map or {}).get(a.symbol, 0.0)),
        reverse=True,
    )
    setup_counts: dict[str, int] = defaultdict(int)

    # ATR untuk semua kandidat yang akan discan: cache read-through + fetch
    # paralel terbatas. Tanpa ini L1 penuh = ~178 REST call serial per cycle.
    try:
        scan_items = [
            (a.symbol, a.mid_px or a.mark_px)
            for a in ranked
            if not memory.symbol_on_cooldown(a.symbol)
            and not memory.has_active_symbol(a.symbol)
        ]
        n_atr_fetched = await prefetch_atr(scan_items, memory)
        # INFO hanya kalau fetch berarti (cold/warm-up); cycle rutin → DEBUG.
        logger.log(
            logging.INFO if n_atr_fetched >= 5 else logging.DEBUG,
            "ATR prefetch · fetched=%d cache_hit=%d",
            n_atr_fetched, len(scan_items) - n_atr_fetched,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("ATR prefetch skip fail-soft: %s", type(e).__name__)

    # Etherscan netflow once per cycle (cache internal + share ke semua trigger)
    cycle_netflow: float | None = None
    cycle_nf_p80 = cycle_nf_p20 = None
    try:
        cycle_netflow = await compute_exchange_netflow_usd(memory, lookback_hours=6.0)
        if cycle_netflow is not None:
            hist = netflow_history_from_db(memory)
            if len(hist) >= 5:
                cycle_nf_p80 = percentile(hist, 80)
                cycle_nf_p20 = percentile(hist, 20)
            else:
                cycle_nf_p80, cycle_nf_p20 = 50_000.0, -50_000.0
    except Exception as e:
        logger.debug("cycle netflow fail-soft: %s", type(e).__name__)

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
        # Full L1 untuk setiap kandidat (skip berdasarkan funding = bias
        # struktural). Biaya ATR ditekan oleh prefetch_atr + _ATR_CACHE di atas;
        # di sini estimate_atr hampir selalu cache hit.
        atr_guess = await estimate_atr(
            asset.symbol, asset.mid_px or asset.mark_px, memory,
        )
        hits = scan_triggers_for_asset(
            asset, atr_guess, cas, memory, setup_enabled=setup_enabled,
        )
        if not hits:
            continue

        # Prioritas event-driven: CASCADE > strength tertinggi
        # (funding tidak boleh selalu menang cuma karena urutan list)
        hits.sort(
            key=lambda h: (
                0 if h.setup_type == "CASCADE_SCALP" else 1,
                -float(h.strength or 0.0),
            )
        )

        for trigger in hits:
            if setup_counts[trigger.setup_type] >= MAX_SIGNALS_PER_SETUP_PER_CYCLE:
                continue
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
                hl_trade_flow=get_hl_trade_flow_buffer(),
                oi_history=get_oi_snapshot_buffer(),
                atr_hint=atr_guess,
                netflow_hint=cycle_netflow,
                netflow_p80=cycle_nf_p80,
                netflow_p20=cycle_nf_p20,
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
            setup_counts[sig.setup_type] += 1
            break  # satu sinyal per symbol per cycle

    n_sig_long = sum(1 for s in produced if s.direction == "LONG")
    n_sig_short = sum(1 for s in produced if s.direction == "SHORT")
    # Setup mix (transparan — kenapa bukan 100% funding)
    from collections import Counter
    setup_cnt = Counter(s.setup_type for s in produced)
    setup_note = " · ".join(f"{k}={v}" for k, v in sorted(setup_cnt.items())) if setup_cnt else "—"

    # Circuit snapshot (transparan)
    circ = []
    for src in ("hyperliquid_rest", "binance_rest", "bybit_rest"):
        st = _circuit[src]
        if st.get("open_until") and time.monotonic() < st["open_until"]:
            circ.append(f"{src}=OPEN")
        else:
            circ.append(f"{src}=ok")
    bs_mode = "0"
    if memory is not None:
        bs_mode = str(memory.get_runtime("black_swan_mode") or "0")
    book_note = ""
    try:
        book_note = get_hl_book_buffer().status_note()
    except Exception:
        pass
    cas_note = ""
    try:
        buf = get_cascade_buffer()
        n_cas = len(buf.snapshot_usd()) if hasattr(buf, "snapshot_usd") else 0
        cas_note = f"{n_cas} sym · ws={'up' if buf.connected else 'down'}"
    except Exception:
        pass
    log_cycle_summary(
        candidates=len(candidates),
        fund_pos=n_fund_pos,
        fund_neg=n_fund_neg,
        fund_zero=n_fund_zero,
        trig_l=n_trig_long,
        trig_s=n_trig_short,
        trig_n=n_trig_neutral,
        gate_fail=n_gate_fail,
        cooldown=n_cooldown,
        sig_n=len(produced),
        sig_l=n_sig_long,
        sig_s=n_sig_short,
        anchor=anchor,
        circ_notes=circ,
        book_note=book_note,
        cascade_note=cas_note,
        bs_mode=bs_mode,
        setup_note=setup_note,
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
            if sdk_result.error and "RateLimit" in (sdk_result.error or ""):
                self._skip_until = max(
                    self._skip_until, time.monotonic() + 120.0,
                )
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


    async def _generate_rest_only(
        self,
        function_name: str,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 512,
        cache_ttl_h: float = 4.0,
    ) -> LLMResult:
        """generateContent REST only — no interactions SDK (hemat quota)."""
        if not self.available():
            return LLMResult("", function_name, 0, success=False, error="no api key")
        if time.monotonic() < self._skip_until:
            return LLMResult("", function_name, 0, success=False, error="rate limited")
        key = _llm_cache_key(function_name, prompt)
        cached = self._cache_get(key)
        if cached is not None:
            return LLMResult(cached, function_name, 0, cached=True)
        if not self._rate_ok():
            return LLMResult("", function_name, 0, success=False, error="rate limited")
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
            url = (
                f"{GEMINI_BASE}/models/{model}:generateContent"
                f"?key={self.api_key}"
            )
            self._calls_this_minute += 1
            raw = await http_post_json(url, body, timeout=30.0)
            ms = (time.monotonic() - t0) * 1000
            text_out = ""
            for cand in (raw or {}).get("candidates") or []:
                for part in ((cand.get("content") or {}).get("parts") or []):
                    if isinstance(part, dict) and part.get("text"):
                        text_out += part["text"]
            if not text_out.strip():
                return LLMResult("", function_name, ms, success=False, error="empty")
            self._cache_put(key, function_name, text_out.strip(), ttl_h=cache_ttl_h)
            self._consecutive_failures = 0
            return LLMResult(text_out.strip(), function_name, ms, success=True)
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            err = type(e).__name__
            msg = str(e)
            if "429" in msg or "RateLimit" in err or "RESOURCE_EXHAUSTED" in msg:
                self._skip_until = max(self._skip_until, time.monotonic() + 120.0)
                err = "RateLimitError"
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._skip_until = max(self._skip_until, time.monotonic() + 60.0)
            return LLMResult("", function_name, ms, success=False, error=err)


    async def refresh_macro_calendar(
        self,
        memory: "MemoryEngine | None" = None,
    ) -> list[dict]:
        """
        Isi kalender makro via Gemini (1×/hari). Fail-soft [].
        Simpan runtime macro_events_json → L6 veto otomatis.
        Rate-limit: max 1 attempt/day; jangan spam interactions API.
        """
        mem = memory if memory is not None else self.memory
        if mem is None or not self.available():
            return []
        today = now_utc().strftime("%Y-%m-%d")
        if (mem.get_runtime("macro_events_day") or "") == today:
            return parse_macro_events(mem)

        # Already rate-limited this session → skip without more 429 noise
        if time.monotonic() < self._skip_until:
            logger.info(
                "📅 macro calendar · gemini paused (rate-limit) · env override OK"
            )
            mem.set_runtime("macro_events_day", today)  # don't retry every restart today
            return parse_macro_events(mem)

        prompt = (
            f"Today is {today} (UTC). List the next 10 days of high-impact "
            f"US macro events that move risk assets: FOMC rate decision, "
            f"FOMC minutes, CPI, Core CPI, PPI, NFP, GDP, Core PCE only.\n"
            f"Return ONLY a pure JSON array (no markdown, no commentary):\n"
            f'[{{"name":"FOMC","at":"YYYY-MM-DDTHH:MM:SSZ","impact":"high"}}]\n'
            f"Use standard UTC times: FOMC 18:00:00Z, CPI/NFP/PPI 12:30:00Z, "
            f"GDP 12:30:00Z. impact must be high or medium. "
            f"If exact date unknown, omit that event."
        )
        # Pure REST generateContent — skip interactions (startup 429 spam)
        r = await self._generate_rest_only(
            "macro_calendar",
            prompt,
            max_tokens=400,
            temperature=0.1,
            cache_ttl_h=12.0,
        )

        if not r or not r.success or not r.text:
            err = (r.error if r else "empty") or "empty"
            # Transient network/HTTP → JANGAN kunci hari (retry di cycle/restart berikutnya)
            transient = err in (
                "HTTPError", "TimeoutError", "ClientError", "ClientConnectorError",
                "ServerTimeoutError", "aiohttp", "OSError", "empty",
            ) or "timeout" in err.lower() or "connection" in err.lower()
            if "RateLimit" in err or "rate limit" in err.lower() or "429" in err:
                self._skip_until = max(self._skip_until, time.monotonic() + 120.0)
                mem.set_runtime("macro_events_day", today)  # quota habis → stop hari ini
                logger.info(
                    "📅 macro calendar · rate-limited · builtin FOMC+NFP tetap aktif"
                )
            elif transient:
                logger.info(
                    "📅 macro calendar · gemini transient (%s) · retry later · builtin OK",
                    err,
                )
            else:
                # Response jelek / parse issue model → kunci hari, jangan spam
                mem.set_runtime("macro_events_day", today)
                logger.info(
                    "📅 macro calendar · gemini empty (%s) · builtin FOMC+NFP aktif",
                    err,
                )
            return parse_macro_events(mem)

        # Sukses → kunci hari
        mem.set_runtime("macro_events_day", today)

        raw_text = r.text.strip()
        if "```" in raw_text:
            parts = raw_text.split("```")
            raw_text = parts[1] if len(parts) > 1 else raw_text
            if raw_text.lstrip().lower().startswith("json"):
                raw_text = raw_text.lstrip()[4:]
        start_i = raw_text.find("[")
        end_i = raw_text.rfind("]")
        if start_i < 0 or end_i <= start_i:
            logger.warning("📅 macro calendar · no JSON array in response")
            return parse_macro_events(mem)
        try:
            data = json.loads(raw_text[start_i : end_i + 1])
        except json.JSONDecodeError:
            logger.warning("📅 macro calendar · JSON parse fail")
            return parse_macro_events(mem)
        if not isinstance(data, list):
            return parse_macro_events(mem)

        cleaned: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            at = item.get("at") or item.get("time")
            if not name or not at:
                continue
            impact = str(item.get("impact") or "high").lower()
            if impact not in ("high", "medium", "low"):
                impact = "high"
            cleaned.append({"name": name, "at": str(at), "impact": impact})
        payload = json.dumps(cleaned, separators=(",", ":"))
        mem.set_runtime("macro_events_json", payload)
        logger.info(
            "📅 macro calendar · gemini · %d events · day=%s",
            len(cleaned), today,
        )
        return parse_macro_events(mem)



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
# Primary L5 news (CryptoPanic API paid since Apr 2026 → RSS-only)
RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://thedefiant.io/feed",
    "https://blockworks.co/feed",
    "https://news.google.com/rss/search?q=cryptocurrency+OR+bitcoin+OR+ethereum+when:1d&hl=en-US&gl=US&ceid=US:en",
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

# (epoch_ts, title) — wall-clock, sama dengan published_parsed feed.
# JANGAN campur dengan time.monotonic(): umur dihitung via time.time().
_RECENT_HEADLINES: list[tuple[float, str]] = []
_RSS_HEADLINE_TTL_SEC = 6 * 3600.0  # match poll window
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
    """Aggregate recent (dalam TTL) headlines per sector into _SECTOR_BIAS_CACHE."""
    global _SECTOR_BIAS_CACHE
    agg: dict[str, list[int]] = defaultdict(list)
    now_e = time.time()
    for ts, title in _RECENT_HEADLINES:
        if now_e - ts > _RSS_HEADLINE_TTL_SEC:
            continue
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
    Poll multi-RSS feeds for L5 news bias + volume.
    Window: 6h (bukan 30m — crypto outlet jarang publish tiap menit).
    Fail-soft: network/parse error → last count / 0.
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
            logger.warning("RSS · feedparser not installed · pip install feedparser")
            return 0, None, []
        # Beberapa outlet blok User-Agent default Python
        try:
            feedparser.USER_AGENT = (
                "CryptoneBot/4.5 (+https://github.com/cryptonebot/cryptone; RSS L5)"
            )
        except Exception:
            pass
        ua = getattr(feedparser, "USER_AGENT", "CryptoneBot/4.5")
        headers = {
            "User-Agent": ua,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        }

        def _parse(url: str):
            # request_headers optional — mock/test boleh parse(url) saja
            try:
                return feedparser.parse(url, request_headers=headers)
            except TypeError:
                return feedparser.parse(url)

        now_epoch = time.time()
        cutoff = now_epoch - 6 * 3600  # 6 jam — cukup untuk bias + volume
        total = bull = bear = 0
        titles: list[tuple[float, str]] = []
        seen: set[str] = set()
        feeds_ok = 0
        feeds_fail = 0
        for url in RSS_FEEDS:
            try:
                feed = _parse(url)
            except Exception as e:
                feeds_fail += 1
                logger.debug("RSS fail %s: %s", url.split("/")[2], type(e).__name__)
                continue
            entries = list(getattr(feed, "entries", []) or [])
            # bozo + empty → treat soft-fail
            if not entries:
                feeds_fail += 1
                logger.debug(
                    "RSS empty %s status=%s bozo=%s",
                    url.split("/")[2],
                    getattr(feed, "status", "?"),
                    getattr(feed, "bozo_exception", None),
                )
                continue
            feeds_ok += 1
            for e in entries[:40]:
                title = (getattr(e, "title", "") or "").strip()
                if not title:
                    continue
                uid = (
                    getattr(e, "id", None)
                    or getattr(e, "link", None)
                    or title.lower()
                )
                uid = str(uid).strip().lower()
                if uid in seen:
                    continue
                seen.add(uid)
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
                # Tanggal invalid / masa depan → treat as now
                if published is not None and published > now_epoch + 300:
                    published = now_epoch
                if published is not None and published < cutoff:
                    continue
                ts = float(published) if published is not None else now_epoch
                total += 1
                titles.append((ts, title))
                s = _score_headline(title)
                if s > 0:
                    bull += 1
                elif s < 0:
                    bear += 1
        # Fallback: kalau window 6h kosong tapi feed hidup, ambil top entries
        # (outlet batch publish — residual 0-headlines di startup malam/sepi)
        if total == 0 and feeds_ok > 0:
            seen.clear()
            for url in RSS_FEEDS:
                try:
                    feed = _parse(url)
                except Exception:
                    continue
                for e in list(getattr(feed, "entries", []) or [])[:8]:
                    title = (getattr(e, "title", "") or "").strip()
                    if not title:
                        continue
                    uid = str(
                        getattr(e, "id", None)
                        or getattr(e, "link", None)
                        or title.lower()
                    ).strip().lower()
                    if uid in seen:
                        continue
                    seen.add(uid)
                    total += 1
                    titles.append((now_epoch, title))
                    s = _score_headline(title)
                    if s > 0:
                        bull += 1
                    elif s < 0:
                        bear += 1
            if total:
                logger.info(
                    "RSS · fallback top-entries · %d headlines · feeds_ok=%d",
                    total, feeds_ok,
                )
        elif total == 0:
            logger.warning(
                "RSS · 0 headlines · feeds_ok=%d fail=%d · check feedparser/network",
                feeds_ok, feeds_fail,
            )
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
    # Rolling merge: keep prior headlines still inside TTL (wall-clock epoch).
    # Poll ini lebih dulu → menang saat dedup; yang lewat TTL dibuang.
    now_e = time.time()
    merged: list[tuple[float, str]] = []
    seen_t: set[str] = set()
    for ts, title in list(titles) + list(_RECENT_HEADLINES):
        if now_e - ts > _RSS_HEADLINE_TTL_SEC:
            continue
        key = title.lower()
        if key in seen_t:
            continue
        seen_t.add(key)
        merged.append((min(ts, now_e), title))   # clamp jam feed yang di masa depan
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
        now_e = time.time()
        scores: list[int] = []
        for ts, title in _RECENT_HEADLINES:
            if now_e - ts > _RSS_HEADLINE_TTL_SEC:
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



# --- L5 extra sources: DXY (FRED), DefiLlama, CryptoPanic ---
_DXY_CACHE: tuple[float, float | None] = (0.0, None)
_STABLE_CACHE: tuple[float, float | None] = (0.0, None)
_CP_CACHE: tuple[float, str | None] = (0.0, None)
_DXY_TTL = 3600.0
_STABLE_TTL = 1800.0
_CP_TTL = 600.0


async def fred_dxy() -> float | None:
    """FRED DTWEXBGS (Trade Weighted USD) as DXY proxy — cache 1h."""
    global _DXY_CACHE
    now_m = time.monotonic()
    if now_m - _DXY_CACHE[0] < _DXY_TTL and _DXY_CACHE[1] is not None:
        return _DXY_CACHE[1]
    val = await fred_latest("DTWEXBGS")
    if val is not None:
        _DXY_CACHE = (now_m, val)
    return val


async def defillama_stablecoin_flow_7d_pct(
    memory: "MemoryEngine | None" = None,
) -> float | None:
    """
    §V.3 #7 — % change total stablecoin circulating USD over ~7d.
    Positive = net mint/inflow (risk-on soft). Fail-soft None.
    """
    global _STABLE_CACHE
    now_m = time.monotonic()
    if now_m - _STABLE_CACHE[0] < _STABLE_TTL and _STABLE_CACHE[1] is not None:
        return _STABLE_CACHE[1]
    if not _circuit_allow("defillama_rest"):
        return _STABLE_CACHE[1]

    async def _call() -> Any:
        return await http_get_json(
            "https://stablecoins.llama.fi/stablecoincharts/all",
            timeout=20.0,
        )

    try:
        raw = await with_retry("defillama_rest", _call, memory)
    except Exception as e:
        logger.debug("defillama fail-soft: %s", type(e).__name__)
        return _STABLE_CACHE[1]

    rows: list = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = raw.get("data") or raw.get("charts") or []
    if len(rows) < 8:
        return _STABLE_CACHE[1]

    def _usd(point: Any) -> float | None:
        if not isinstance(point, dict):
            return None
        t = point.get("totalCirculatingUSD") or point.get("totalCirculating")
        if isinstance(t, dict):
            v = t.get("peggedUSD") or t.get("USD") or next(iter(t.values()), None)
        else:
            v = t
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    latest = _usd(rows[-1])
    older = _usd(rows[-8]) if len(rows) >= 8 else _usd(rows[0])
    if latest is None or older is None or older <= 0:
        return _STABLE_CACHE[1]
    pct = (latest - older) / older * 100.0
    _STABLE_CACHE = (now_m, pct)
    return pct


async def cryptopanic_news_bias(
    memory: "MemoryEngine | None" = None,
) -> str | None:
    """
    §V.3 #9 role — aggregated news bias.
    CryptoPanic API free tier discontinued (Apr 2026, now paid).
    Replacement: expanded multi-RSS aggregate (same bias signal, $0).
    """
    _ = memory
    # Ensure fresh poll if stale; bias filled by poll_rss_headline_count
    if _RSS_LAST_POLL <= 0 or (time.monotonic() - _RSS_LAST_POLL) > 300:
        try:
            await poll_rss_headline_count(memory)
        except Exception:
            pass
    return _RSS_CACHE_BIAS



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
    if _env_ok("ETHERSCAN_API_KEY"):
        try:
            t0 = time.monotonic()
            es_ok, es_note = await probe_etherscan()
            ms = (time.monotonic() - t0) * 1000
            status["etherscan"] = {
                "ok": es_ok,
                "note": f"{es_note} · {ms:.0f}ms",
            }
        except Exception as e:
            status["etherscan"] = {"ok": False, "note": str(e)[:50]}
    else:
        status["etherscan"] = {
            "ok": None,
            "note": "optional, no ETHERSCAN_API_KEY",
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
    n_w = len(parse_exchange_wallets())
    if n_w > 0 and _env_ok("ETHERSCAN_API_KEY"):
        status["wallets"] = {"ok": True, "note": f"{n_w} exchange wallets · flow on"}
    elif n_w > 0:
        status["wallets"] = {"ok": False, "note": f"{n_w} wallets but no ETHERSCAN key"}
    else:
        status["wallets"] = {
            "ok": None,
            "note": "flow agent off · set CRYPTONE_EXCHANGE_WALLETS",
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
            "note": "wired · starts with live loop",
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
            "note": "wired · starts with live loop",
        }

    # --- RSS multi-feed (CryptoPanic role, $0) ---
    try:
        n_head = await poll_rss_headline_count(None)
        bias = _RSS_CACHE_BIAS or "flat"
        status["rss_news"] = {
            "ok": True if n_head > 0 else None,
            "note": f"{n_head} headlines · bias={bias} · {len(RSS_FEEDS)} feeds",
        }
    except Exception as e:
        status["rss_news"] = {"ok": False, "note": str(e)[:50]}
    # DefiLlama stablecoin
    try:
        t0 = time.monotonic()
        pct = await defillama_stablecoin_flow_7d_pct(None)
        ms = (time.monotonic() - t0) * 1000
        if pct is not None:
            status["defillama"] = {
                "ok": True,
                "note": f"stable 7d {pct:+.2f}% · {ms:.0f}ms",
            }
        else:
            status["defillama"] = {
                "ok": False,
                "note": f"empty · {ms:.0f}ms",
            }
    except Exception as e:
        status["defillama"] = {"ok": False, "note": str(e)[:50]}
    # §V.3 #12 Forex Factory role — builtin FOMC/NFP + Gemini/env merge
    mins, evn = minutes_to_next_macro()
    n_ev = len(parse_macro_events())
    n_today = len(macro_events_today())
    if mins is not None:
        status["macro_calendar"] = {
            "ok": True,
            "note": f"next={evn} {mins:.0f}m · events={n_ev} · today={n_today}",
        }
    elif n_ev > 0:
        status["macro_calendar"] = {
            "ok": True,
            "note": f"events={n_ev} · today={n_today} · FOMC+NFP seed",
        }
    else:
        status["macro_calendar"] = {
            "ok": False,
            "note": "empty calendar (unexpected)",
        }

    return status


def log_source_banner(status: dict[str, dict[str, Any]]) -> None:
    """
    Tree log modern — source + system/UI capability map.
    ✅ hidup/done · ❌ gagal · ⚪ skip/belum wired
    """
    def note(name: str) -> str:
        st = status.get(name) or {}
        return str(st.get("note") or "")[:48]

    def leaf(name: str, label: str, last: bool = False, pipe: str = "│") -> str:
        st = status.get(name) or {}
        icon = _src_icon(st.get("ok"))
        br = "└─" if last else "├─"
        return f"  {pipe}  {br} {icon} {label} · {note(name)}"

    def feat(ok: bool, label: str, detail: str = "", last: bool = False, pipe: str = "│") -> str:
        icon = "✅" if ok else "⚪"
        br = "└─" if last else "├─"
        tail = f" · {detail}" if detail else ""
        return f"  {pipe}  {br} {icon} {label}{tail}"

    n_ok = sum(1 for s in status.values() if s.get("ok") is True)
    n_fail = sum(1 for s in status.values() if s.get("ok") is False)
    n_skip = sum(1 for s in status.values() if s.get("ok") is None)

    quiet_on = QUIET_HOURS_ENABLED and os.environ.get(
        "CRYPTONE_QUIET_HOURS", "1"
    ).strip() not in ("0", "false", "False", "no")
    mode = os.environ.get("CRYPTONE_DELIVERY_MODE", DEFAULT_DELIVERY_MODE)

    lines = [
        "📡 SOURCES",
        "  ├─ 📈 MARKET",
        leaf("hyperliquid", "Hyperliquid"),
        leaf("binance", "Binance REST"),
        leaf("bybit", "Bybit REST"),
        leaf("binance_ws_liq", "Binance WS liq"),
        leaf("hl_ws", "HL WS L2 book", last=True),
        "  ├─ 🧠 AI / MACRO",
        leaf("gemini", "Gemini"),
        leaf("fred", "FRED US10Y"),
        leaf("fear_greed", "Fear&Greed"),
        leaf("etherscan", "Etherscan"),
        leaf("wallets", "Wallet flow", last=True),
        "  ├─ 📨 DELIVERY",
        leaf("telegram", "Telegram Bot API", last=True),
        "  ├─ 📰 NEWS",
        leaf("rss_news", "RSS feeds"),
        leaf("defillama", "DefiLlama"),
        leaf("macro_calendar", "Macro calendar (FF)", last=True),
        f"  ── sources {n_ok} live · {n_fail} down · {n_skip} skip",
        "",
        "🧩 SYSTEM / GATES",
        feat(True, "Funding + Cascade + Compression triggers"),
        feat(True, "Structure BOS/CHoCH/sweep + FVG entry"),
        feat(True, "Funding wajib structure (anti-printer)"),
        feat(True, "ATR% gate + HTF 4H + session filter"),
        feat(True, "Post-loss penalty + consec circuit"),
        feat(True, "PENDING→ARMED + time-stop + outcome"),
        feat(True, "Confluence 0–5 + Tier A/B/C"),
        feat(True, "Quiet hours + delivery mode", f"{mode}"),
        feat(True, "Etherscan flow agent (env wallets)"),
        feat(True, "HL real confirm: book + taker-flow + ΔOI"),
        feat(True, "Conf kalibrasi outcome live 30d (shrinkage)"),
        feat(True, "ATR cache + prefetch · kuota per setup"),
        feat(True, "OI change series persist (metric_oi)", last=True),
        "",
        "📱 TELEGRAM SUPER APP",
        feat(True, "Menu tombol (Radar/Market/BS/…)"),
        feat(True, "Long-poll UI loop (responsive)"),
        feat(True, "Kartu sinyal + Reasoning button"),
        feat(True, "Journal / Performance / Accuracy"),
        feat(True, "Breadth + Settings view"),
        feat(quiet_on, "Quiet hours", f"{QUIET_START_HOUR_WIB:02d}–{QUIET_END_HOUR_WIB:02d} WIB"),
        feat(True, "Settings tombol (mode/setup/pause)"),
        feat(True, "Position calculator (calc RISK ENTRY SL)"),
        feat(True, "Chart render (matplotlib)"),
        feat(False, "Playbook + custom alerts", last=True),
    ]
    for ln in lines:
        logger.info(ln)


def log_cycle_header(
    cycle_n: int,
    elapsed_m: int,
    left_m: int,
    active: int,
) -> None:
    """Header cycle — satu blok rapi."""
    logger.info("──────────────────────────────────────")
    logger.info(
        "🔄 CYCLE %d  ·  ⏱ %dm elapsed  ·  left %dm  ·  active %d",
        cycle_n, elapsed_m, left_m, active,
    )


def log_cycle_summary(
    *,
    candidates: int,
    fund_pos: int,
    fund_neg: int,
    fund_zero: int,
    trig_l: int,
    trig_s: int,
    trig_n: int,
    gate_fail: int,
    cooldown: int,
    sig_n: int,
    sig_l: int,
    sig_s: int,
    anchor: str | None,
    circ_notes: list[str],
    book_note: str = "",
    cascade_note: str = "",
    bs_mode: str = "0",
    setup_note: str = "",
) -> None:
    """
    Ringkasan cycle multi-baris — transparan, scan-friendly.
    """
    bs_label = {
        "1": "🚨 BLACK_SWAN",
        "ELEVATED": "⚠️ ELEVATED",
    }.get(bs_mode, "✅ normal")
    circ_s = " · ".join(circ_notes) if circ_notes else "—"
    lines = [
        f"  ├─ universe {candidates} · anchor {anchor or '—'}",
        f"  ├─ funding  +{fund_pos} / −{fund_neg} / 0={fund_zero}",
        f"  ├─ trigger  L{trig_l} S{trig_s} N{trig_n}",
        f"  ├─ gates    fail {gate_fail} · cooldown {cooldown}",
        f"  ├─ signals  {sig_n}  (L{sig_l}/S{sig_s})",
        f"  ├─ risk     {bs_label}",
    ]
    if setup_note:
        lines.append(f"  ├─ setups   {setup_note}")
    if book_note:
        lines.append(f"  ├─ book     {book_note}")
    if cascade_note:
        lines.append(f"  ├─ cascade  {cascade_note}")
    lines.append(f"  └─ circuit  {circ_s}")
    logger.info("📊 CYCLE DETAIL")
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
        ]
    }


def _kb_signal(signal_id: str) -> dict:
    """§XII.2 tombol di kartu sinyal."""
    sid = (signal_id or "")[:36]
    return {
        "inline_keyboard": [
            [
                {"text": "🧠 Reasoning", "callback_data": f"sig:reason:{sid}"},
                {"text": "📊 Chart", "callback_data": f"sig:chart:{sid}"},
            ],
            [
                {"text": "📓 Journal", "callback_data": "menu:journal"},
                {"text": "🏠 Menu", "callback_data": "menu:home"},
            ],
        ]
    }


def _kb_back() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "⬅️ Menu", "callback_data": "menu:home"}],
        ]
    }


def _kb_back_to_signal(signal_id: str) -> dict:
    """Setelah Reasoning/Chart: kembali ke kartu sinyal."""
    sid = (signal_id or "")[:36]
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Kartu sinyal", "callback_data": f"sig:card:{sid}"},
                {"text": "🏠 Menu", "callback_data": "menu:home"},
            ],
        ]
    }


def _ui_px(x: float | int | None) -> str:
    """Format harga ringkas, konsisten di semua kartu Telegram."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if v == 0:
        return "0"
    ax = abs(v)
    if ax >= 1000:
        return f"{v:,.2f}"
    if ax >= 100:
        return f"{v:.2f}"
    if ax >= 1:
        return f"{v:.4f}"
    if ax >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6g}"


def _ui_sep() -> str:
    return "────────────────────"


def _ui_header(icon: str, title: str) -> str:
    return f"{icon} {title}\n{_ui_sep()}"


def _fmt_signal_card_from_db(memory: "MemoryEngine", signal_id: str) -> str:
    """Rekonstruksi kartu sinyal dari DB (restore setelah Reasoning)."""
    if not memory._conn or not signal_id:
        return "Signal tidak ditemukan."
    row = memory._conn.execute(
        "SELECT symbol, direction, horizon, tier, setup_type, trigger_type, "
        "entry_zone_low, entry_zone_high, stop_loss, tp1, tp2, rr, confidence, "
        "state, reasoning FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return f"Signal {signal_id[:8]}… tidak ditemukan / sudah expired."
    arrow = "🟢" if row["direction"] == "LONG" else "🔴"
    state = row["state"] or "PENDING"
    # sl/entry basis ditulis generate_signal ke reasoning ("sl=<x> entry=<y>").
    # Tidak ada di reasoning (row lama/rusak) → "?" — jangan menebak "structure".
    reason = row["reasoning"] or ""
    m_sl = re.search(r"(?:^|\s)sl=(\S+)", reason)
    m_en = re.search(r"(?:^|\s)entry=(\S+)", reason)
    sl_b = m_sl.group(1) if m_sl else "?"
    en_b = m_en.group(1) if m_en else "?"
    el = float(row["entry_zone_low"] or 0)
    eh = float(row["entry_zone_high"] or 0)
    sl = float(row["stop_loss"] or 0)
    tp1 = float(row["tp1"] or 0)
    tp2 = float(row["tp2"] or 0)
    rr = float(row["rr"] or 0)
    conf = float(row["confidence"] or 0)
    trigger = (row["trigger_type"] or "").strip()
    plain = (
        f"{arrow} {row['symbol']}  {row['direction']}\n"
        f"{row['horizon']} · Tier {row['tier']} · {row['setup_type']}\n"
        f"{state} · SL {sl_b} · entry {en_b}\n"
        f"\n"
        f"Entry  {_ui_px(el)} – {_ui_px(eh)}\n"
        f"SL     {_ui_px(sl)}\n"
        f"TP1    {_ui_px(tp1)}\n"
        f"TP2    {_ui_px(tp2)}\n"
        f"\n"
        f"R:R 1:{rr:.2f}  ·  conf {conf:.0%}"
    )
    if trigger:
        plain += f"\n{trigger}"
    return plain


def format_signal_message(s: "PipelineSignal") -> str:
    """Pesan sinyal Telegram §XII.2 — bersih, mobile-friendly, no residual."""
    arrow = "🟢" if s.direction == "LONG" else "🔴"
    state = getattr(s, "state", "PENDING") or "PENDING"
    sl_b = getattr(s, "sl_basis", "atr") or "atr"
    en_b = getattr(s, "entry_basis", "atr") or "atr"
    plain = (
        f"{arrow} {s.symbol}  {s.direction}\n"
        f"{s.horizon} · Tier {s.tier} · {s.setup_type}\n"
        f"{state} · SL {sl_b} · entry {en_b}\n"
        f"\n"
        f"Entry  {_ui_px(s.entry_low)} – {_ui_px(s.entry_high)}\n"
        f"SL     {_ui_px(s.stop_loss)}\n"
        f"TP1    {_ui_px(s.tp1)}\n"
        f"TP2    {_ui_px(s.tp2)}\n"
        f"\n"
        f"R:R 1:{s.rr:.2f}  ·  conf {s.confidence:.0%}\n"
        f"confirms {s.confirm_count}/{s.active_agent_count}"
        f" · real {getattr(s, 'real_count', 0)}"
        f"  ·  {s.trigger_type}"
    )
    if abs(float(getattr(s, "context_mult", 1.0) or 1.0) - 1.0) >= 0.01:
        plain += f"  ·  ctx×{s.context_mult:.2f}"
    confu = int(getattr(s, "confluence", 0) or 0)
    if confu > 0:
        plain += f"\nconfluence {confu}/5"
    return plain


def format_main_menu_text() -> str:
    return (
        f"{_ui_header('🤖', 'Cryptone V4.5')}\n"
        f"Market Radar + Trader Assistant\n"
        f"\n"
        f"Ketuk tombol di bawah.\n"
        f"Sinyal real-time tetap push otomatis."
    )


def _fmt_radar(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    if not rows:
        return (
            f"{_ui_header('📊', 'Radar')}\n"
            f"Tidak ada sinyal aktif.\n"
            f"Push otomatis saat setup lolos gate."
        )
    order = {"A": 0, "B": 1, "C": 2}
    rows = sorted(rows, key=lambda r: order.get(str(r.get("tier") or "C"), 9))
    lines = [_ui_header("📊", f"Radar — {len(rows)} aktif")]
    for r in rows[:12]:
        arrow = "🟢" if str(r.get("direction") or "").upper() == "LONG" else "🔴"
        st = str(r.get("state") or "PENDING")
        setup = r.get("setup_type") or "—"
        rr = float(r.get("rr") or 0)
        lines.append(
            f"{arrow} {r.get('symbol')} {r.get('direction')}  ·  "
            f"Tier {r.get('tier')}  ·  {st}\n"
            f"   {setup}  ·  R:R 1:{rr:.2f}"
        )
    if len(rows) > 12:
        lines.append(f"\n… +{len(rows) - 12} lainnya")
    return "\n".join(lines)


def _fmt_market(memory: "MemoryEngine") -> str:
    anchor = memory.get_last_anchor() or "—"
    bs = memory.get_runtime("black_swan_mode", "0") or "0"
    bs_label = {
        "1": "🚨 BLACK SWAN",
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
    bias = rss_news_bias() or "n/a"
    return (
        f"{_ui_header('🌍', 'Market Snapshot')}\n"
        f"Anchor        {anchor}\n"
        f"Black swan    {bs_label}\n"
        f"Active        {n_act}\n"
        f"Funding rows  {fund_n:,}\n"
        f"ATR rows      {atr_n:,}\n"
        f"RSS bias      {bias}"
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
    since = started[:19] if started and started != "—" else "—"
    lines = [
        _ui_header("🚨", "Black Swan"),
        f"Mode     {label}",
        f"Since    {since}",
    ]
    if memory._conn:
        try:
            row = memory._conn.execute(
                "SELECT triggered_indicators, trigger_count, started_at "
                "FROM blackswan_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                lines.append(f"Last     {row['triggered_indicators']}")
                lines.append(f"Count    {row['trigger_count']}")
        except Exception:
            pass
    return "\n".join(lines)


def _fmt_journal(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    lines = [_ui_header("📓", "Journal — active")]
    if not rows:
        lines.append("Kosong — belum ada posisi aktif.")
    for r in rows[:10]:
        arrow = "🟢" if str(r.get("direction") or "").upper() == "LONG" else "🔴"
        el = _ui_px(r.get("entry_zone_low"))
        eh = _ui_px(r.get("entry_zone_high"))
        sl = _ui_px(r.get("stop_loss"))
        lines.append(
            f"{arrow} {r.get('symbol')} {r.get('direction')}  ·  "
            f"{r.get('state')}  ·  Tier {r.get('tier')}\n"
            f"   Entry {el}–{eh}  ·  SL {sl}"
        )
    if memory._conn:
        try:
            recent = memory._conn.execute(
                "SELECT symbol, direction, outcome, realized_rr, resolved_at "
                "FROM signal_history WHERE outcome IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT 5"
            ).fetchall()
            if recent:
                lines.append("")
                lines.append("Recent closed")
                lines.append(_ui_sep())
                for r in recent:
                    rr = r["realized_rr"]
                    rr_s = f"  ·  R:R {rr:+.2f}" if rr is not None else ""
                    oc = str(r["outcome"] or "")
                    icon = {
                        "PROFIT": "✅", "LOSS": "🛑",
                        "PARTIAL": "🎯", "NEUTRAL": "⏱",
                    }.get(oc, "•")
                    lines.append(
                        f"{icon} {oc}  {r['symbol']} {r['direction']}{rr_s}"
                    )
        except Exception:
            pass
    return "\n".join(lines)


def _fmt_performance(memory: "MemoryEngine") -> str:
    lines = [_ui_header("📊", "Performance — setup")]
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
        l = int(r["losses"] or 0)
        wr = ((w + PARTIAL_WEIGHT * p) / n * 100) if n else 0
        avg = r["avg_rr"]
        avg_s = f"{avg:.2f}" if avg is not None else "—"
        lines.append(
            f"🎯 {r['setup_type']}\n"
            f"   n={n}  ·  W {w} / L {l} / P {p}\n"
            f"   WR {wr:.0f}%  ·  avg R:R {avg_s}"
        )
    return "\n".join(lines)


def _fmt_accuracy(memory: "MemoryEngine") -> str:
    lines = [_ui_header("🎯", "Accuracy Tracker")]
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
    w = int(row["w"] or 0)
    l = int(row["l"] or 0)
    p = int(row["p"] or 0)
    n = int(row["n"] or 0)
    wr = (w + PARTIAL_WEIGHT * p) / t * 100
    avg = row["avg_rr"]
    avg_s = f"{avg:.2f}" if avg is not None else "—"
    lines.extend([
        f"Resolved     {t}",
        f"PROFIT {w}  ·  LOSS {l}  ·  PARTIAL {p}  ·  NEUTRAL {n}",
        f"Win rate     {wr:.1f}%   (W + 0.5×P)",
        f"Avg R:R      {avg_s}",
    ])
    return "\n".join(lines)


def _kb_settings(memory: "MemoryEngine") -> dict:
    setups = memory.get_setup_enabled()
    mode = get_delivery_mode(memory)
    is_paused = memory.signals_paused()
    rows = [
        [
            {
                "text": f"{'✅' if mode == 'active' else '⬜'} Active",
                "callback_data": "set:mode:active",
            },
            {
                "text": f"{'✅' if mode == 'hybrid' else '⬜'} Hybrid",
                "callback_data": "set:mode:hybrid",
            },
            {
                "text": f"{'✅' if mode == 'passive' else '⬜'} Passive",
                "callback_data": "set:mode:passive",
            },
        ],
        [
            {
                "text": f"{'✅' if setups.get('FUNDING_SQUEEZE', True) else '⏸'} Funding",
                "callback_data": "set:setup:FUNDING_SQUEEZE",
            },
            {
                "text": f"{'✅' if setups.get('CASCADE_SCALP', True) else '⏸'} Cascade",
                "callback_data": "set:setup:CASCADE_SCALP",
            },
            {
                "text": f"{'✅' if setups.get('COMPRESSION', True) else '⏸'} Compress",
                "callback_data": "set:setup:COMPRESSION",
            },
        ],
        [
            {
                "text": "▶️ Resume signals" if is_paused else "⏸ Pause 1h",
                "callback_data": "set:pause:toggle",
            },
        ],
        [
            {"text": "🧮 Position calc", "callback_data": "menu:calc"},
            {"text": "⬅️ Menu", "callback_data": "menu:home"},
        ],
    ]
    return {"inline_keyboard": rows}


def _fmt_settings_view(memory: "MemoryEngine") -> str:
    setups = memory.get_setup_enabled()
    mode = get_delivery_mode(memory)
    lines = [
        _ui_header("⚙️", "Settings"),
        f"Delivery      {mode}",
        f"Cooldown cooldown {SIGNAL_COOLDOWN_MIN}m",
        f"Post-loss     {POST_LOSS_COOLDOWN_MIN}m",
        f"Max concurrent {MAX_CONCURRENT_SIGNALS}",
        f"Max / cycle   {MAX_SIGNALS_PER_CYCLE}",
        f"Session WIB   {SESSION_START_HOUR_WIB}:00–{SESSION_END_HOUR_WIB}:00",
        f"Quiet WIB     {QUIET_START_HOUR_WIB}:00–{QUIET_END_HOUR_WIB}:00 "
        f"({'on' if QUIET_HOURS_ENABLED else 'off'})",
        f"Entry zone    {ENTRY_ZONE_ATR_WIDTH}× ATR",
        "",
        "Setups",
        _ui_sep(),
        f"  FUNDING_SQUEEZE  {'ON' if setups.get('FUNDING_SQUEEZE', True) else 'OFF'}",
        f"  CASCADE_SCALP    {'ON' if setups.get('CASCADE_SCALP', True) else 'OFF'}",
        f"  COMPRESSION      {'ON' if setups.get('COMPRESSION', True) else 'OFF'}",
        "",
        "Tier B tetap valid (confirm ≥ 2).",
        "Quiet hanya tahan push B/C — sinyal tetap di DB/Radar.",
    ]
    paused = memory.get_runtime("signals_paused_until")
    if paused:
        lines.append(f"\n⏸ paused until {str(paused)[:19]}")
    return "\n".join(lines)


def _fmt_calc_help() -> str:
    return (
        f"{_ui_header('🧮', 'Position Calculator')}\n"
        f"Kirim pesan format:\n"
        f"calc RISK ENTRY SL\n"
        f"\n"
        f"Contoh:\n"
        f"calc 50 2.85 2.90\n"
        f"→ risk $50, entry 2.85, SL 2.90\n"
        f"\n"
        f"qty = risk / |entry − SL|"
    )


def _parse_calc_message(text: str) -> str | None:
    """Return formatted calc result or None if not a calc command."""
    t = (text or "").strip()
    if not t.lower().startswith("calc"):
        return None
    parts = t.replace(",", " ").split()
    if len(parts) < 4:
        return "Format: calc RISK ENTRY SL\nContoh: calc 50 2.85 2.90"
    try:
        risk = float(parts[1])
        entry = float(parts[2])
        sl = float(parts[3])
    except ValueError:
        return "Angka tidak valid. Contoh: calc 50 2.85 2.90"
    dist = abs(entry - sl)
    if dist <= 0 or risk <= 0:
        return "Risk dan |entry − SL| harus > 0."
    qty = risk / dist
    notional = qty * entry
    return (
        f"{_ui_header('🧮', 'Position size')}\n"
        f"Risk      ${_ui_px(risk)}\n"
        f"Entry     {_ui_px(entry)}\n"
        f"SL        {_ui_px(sl)}\n"
        f"Distance  {_ui_px(dist)}  ({dist / entry * 100:.2f}%)\n"
        f"Qty       {_ui_px(qty)}\n"
        f"Notional  ${_ui_px(notional)}\n"
        f"\n"
        f"Cek leverage & min lot di exchange."
    )


def _fmt_breadth(memory: "MemoryEngine") -> str:
    """Rough breadth from last funding snapshot signs in DB."""
    lines = [_ui_header("📈", "Market Breadth (funding)")]
    if not memory._conn:
        return "\n".join(lines) + "\n(no db)"
    try:
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
    total = len(rows)
    lines.extend([
        f"Symbols     {total}",
        f"Funding +   {pos}   ({pos / total * 100:.0f}%)  crowded long",
        f"Funding −   {neg}   ({neg / total * 100:.0f}%)  crowded short",
        f"Flat        {zero}",
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
    body = (row["reasoning"] or "").strip() or "(kosong)"
    title = f"Reasoning — {row['symbol']} {row['direction']}"
    return (
        f"{_ui_header('🧠', title)}\n"
        f"Tier {row['tier']}  ·  {row['setup_type']}\n"
        f"\n"
        f"{body}"
    )



async def render_signal_chart_png(
    memory: "MemoryEngine",
    signal_id: str,
) -> tuple[bytes | None, str]:
    """
    §XII chart — candle + entry zone / SL / TP overlay (Telegram-friendly).
    Dark theme, header (simbol + arah + R:R + conf), auto-zoom ke level, teks besar
    agar terbaca di layar HP. Pure matplotlib. Fail-soft (None, error_text).
    """
    if not memory._conn or not signal_id:
        return None, "Signal tidak ditemukan."
    row = memory._conn.execute(
        "SELECT symbol, direction, horizon, tier, setup_type, "
        "entry_zone_low, entry_zone_high, stop_loss, tp1, tp2, rr, confidence, state "
        "FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return None, f"Signal {signal_id[:8]}… tidak ditemukan."

    symbol = str(row["symbol"])
    direction = str(row["direction"] or "")
    el = float(row["entry_zone_low"] or 0)
    eh = float(row["entry_zone_high"] or 0)
    sl = float(row["stop_loss"] or 0)
    tp1 = float(row["tp1"] or 0)
    tp2 = float(row["tp2"] or 0)
    rr = float(row["rr"] or 0)
    conf = float(row["confidence"] or 0)
    tier = str(row["tier"] or "?")
    setup = str(row["setup_type"] or "")
    state = str(row["state"] or "")

    horizon = str(row["horizon"] or "INTRADAY").upper()
    interval = "15m" if horizon == "SCALPING" else ("4h" if horizon == "SWING" else "1h")
    bars = 80
    end_ms = int(now_utc().timestamp() * 1000)
    bar_ms = {"15m": 15 * 60_000, "1h": 3_600_000, "4h": 4 * 3_600_000}.get(interval, 3_600_000)
    start_ms = end_ms - bars * bar_ms

    try:
        candles = await hl_candle_snapshot(symbol, interval, start_ms, end_ms, memory)
    except Exception as e:
        return None, f"Chart gagal ambil candle: {type(e).__name__}"
    if not candles or len(candles) < 10:
        n = len(candles) if candles else 0
        return None, f"Chart · data candle {symbol} kurang ({n} bars)"

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.ticker import FuncFormatter, MaxNLocator
        from io import BytesIO
    except ImportError as e:
        return None, f"Chart lib missing: {e} (pip install matplotlib)"

    def _px_fmt(x: float) -> str:
        ax = abs(x)
        if ax >= 1000:
            return f"{x:,.1f}"
        if ax >= 100:
            return f"{x:.2f}"
        if ax >= 1:
            return f"{x:.4f}"
        if ax >= 0.01:
            return f"{x:.5f}"
        return f"{x:.6g}"

    def _vol_fmt(x: float) -> str:
        ax = abs(x)
        if ax >= 1e9:
            return f"{x / 1e9:.1f}B"
        if ax >= 1e6:
            return f"{x / 1e6:.1f}M"
        if ax >= 1e3:
            return f"{x / 1e3:.0f}K"
        return f"{x:g}"

    fig = None
    try:
        by_time: dict = {}
        for c in candles:
            bt = c.bar_time
            if getattr(bt, "tzinfo", None) is not None:
                bt = bt.astimezone(UTC).replace(tzinfo=None)
            by_time[bt] = c
        times = sorted(by_time)
        series = [by_time[t] for t in times]
        n = len(series)
        opens = [float(c.open) for c in series]
        highs = [float(c.high) for c in series]
        lows = [float(c.low) for c in series]
        closes = [float(c.close) for c in series]
        vols = [float(getattr(c, "volume", 0.0) or 0.0) for c in series]
        has_vol = sum(vols) > 0

        # Palet dark ala TradingView
        BG = "#131722"
        PANEL = "#1b1f2b"
        GRID = "#2a2e39"
        TEXT = "#e6e9f0"
        MUTED = "#8b90a0"
        GREEN = "#26a69a"
        GREEN_BRIGHT = "#4caf50"
        RED = "#ef5350"
        BLUE = "#42a5f5"
        ORANGE = "#ff9800"
        DARK_INK = "#0b0e14"

        is_long = direction.upper() == "LONG"
        dir_color = GREEN_BRIGHT if is_long else RED
        conf_s = f"{conf:.0%}" if conf <= 1.0 else f"{conf:.0f}%"

        # Geometri (fraksi figure) — semua posisi ditentukan eksplisit
        fig_w, fig_h, dpi = 10.0, 6.8, 160
        left, right = 0.075, 0.845
        width = right - left
        if has_vol:
            rect_p = [left, 0.33, width, 0.515]
            rect_v = [left, 0.10, width, 0.20]
        else:
            rect_p = [left, 0.10, width, 0.745]
            rect_v = None

        with plt.rc_context({"font.family": "DejaVu Sans"}):
            fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=BG)
            ax_v = fig.add_axes(rect_v) if rect_v else None
            ax = fig.add_axes(rect_p, sharex=ax_v) if ax_v is not None else fig.add_axes(rect_p)
            ax_x = ax_v if ax_v is not None else ax

            for a in (ax, ax_v):
                if a is None:
                    continue
                a.set_facecolor(PANEL)
                a.set_axisbelow(True)
                a.grid(True, color=GRID, lw=0.6, alpha=0.75)
                for spine in a.spines.values():
                    spine.set_color(GRID)
                    spine.set_linewidth(0.8)
                a.tick_params(axis="y", labelsize=9.5, labelcolor=TEXT, length=0, pad=6)
                a.tick_params(axis="x", labelsize=9.5, labelcolor=MUTED, length=3, color=GRID)
            if ax_v is not None:
                ax.tick_params(axis="x", labelbottom=False)

            # Auto y-zoom: rentang candle + semua level sinyal
            zoom = [min(lows), max(highs)]
            zoom.extend(v for v in (el, eh, sl, tp1, tp2) if v and v > 0)
            y_min, y_max = min(zoom), max(zoom)
            pad = max((y_max - y_min) * 0.08, abs(y_max) * 0.002, 1e-9)
            ax.set_ylim(y_min - pad, y_max + pad)
            ax.set_xlim(-1.5, n + 0.5)

            # Candle
            xs = list(range(n))
            cols = [GREEN if cl >= op else RED for op, cl in zip(opens, closes)]
            min_body = (y_max - y_min + 2 * pad) * 0.002
            ax.vlines(xs, lows, highs, colors=cols, linewidth=1.1, zorder=3)
            ax.bar(
                xs,
                [max(abs(cl - op), min_body) for op, cl in zip(opens, closes)],
                bottom=[min(op, cl) for op, cl in zip(opens, closes)],
                width=0.66, color=cols, linewidth=0, zorder=4,
            )

            # Volume
            if ax_v is not None:
                ax_v.bar(xs, vols, width=0.72, color=cols, alpha=0.8, linewidth=0, zorder=3)
                ax_v.set_ylim(0, max(vols) * 1.12)
                ax_v.yaxis.set_major_locator(MaxNLocator(nbins=3, prune="lower"))
                ax_v.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _vol_fmt(v)))
                ax_v.text(
                    0.008, 0.93, "VOL", transform=ax_v.transAxes,
                    ha="left", va="top", fontsize=8.5, fontweight="bold", color=MUTED,
                )

            # Sumbu waktu (UTC)
            tick_idx = sorted({round(i * (n - 1) / 4) for i in range(5)})
            ax_x.set_xticks(tick_idx)
            ax_x.set_xticklabels([times[i].strftime("%d/%m\n%H:%M") for i in tick_idx])

            # Entry zone band
            if el > 0 and eh > 0 and eh >= el:
                ax.axhspan(el, eh, facecolor=BLUE, alpha=0.14, zorder=2, linewidth=0)
                ax.axhline(el, color=BLUE, lw=1.0, ls=(0, (4, 3)), alpha=0.85, zorder=5)
                ax.axhline(eh, color=BLUE, lw=1.0, ls=(0, (4, 3)), alpha=0.85, zorder=5)

            # Level SL / TP + harga sekarang
            badges: list[tuple[float, str, str]] = [
                (p, c, lb)
                for p, c, lb in ((sl, RED, "SL"), (tp1, GREEN, "TP1"), (tp2, GREEN_BRIGHT, "TP2"))
                if p > 0
            ]
            for price, color, _label in badges:
                ax.axhline(price, color=color, lw=1.6, alpha=0.95, zorder=5)
            if el > 0 and eh > 0:
                badges.append(((el + eh) / 2.0, BLUE, "ENTRY"))

            last_px = closes[-1]
            ax.axhline(last_px, color=ORANGE, lw=1.1, ls=":", alpha=0.85, zorder=5)

            # Badge harga di sisi kanan — digeser bila level saling berdekatan
            ymin, ymax = ax.get_ylim()
            yspan = max(ymax - ymin, 1e-12)
            gap = yspan * (0.27 / (rect_p[3] * fig_h))
            edge_m = yspan * 0.02
            ordered = sorted(badges + [(last_px, ORANGE, "NOW")], key=lambda t: t[0])
            ys = [float(p) for p, _c, _l in ordered]
            ys[0] = max(ys[0], ymin + edge_m)
            for i in range(1, len(ys)):
                ys[i] = max(ys[i], ys[i - 1] + gap)
            if ys[-1] > ymax - edge_m:
                ys[-1] = ymax - edge_m
                for i in range(len(ys) - 2, -1, -1):
                    ys[i] = min(ys[i], ys[i + 1] - gap)
            for (price, color, label), y in zip(ordered, ys):
                ax.annotate(
                    f" {label} {_px_fmt(price)} ",
                    xy=(1.0, y),
                    xycoords=("axes fraction", "data"),
                    xytext=(6, 0),
                    textcoords="offset points",
                    va="center", ha="left",
                    fontsize=9.5, fontweight="bold", color=DARK_INK,
                    bbox=dict(boxstyle="round,pad=0.28", facecolor=color, edgecolor="none"),
                    annotation_clip=False,
                    zorder=8,
                )

            ax.yaxis.set_major_locator(MaxNLocator(nbins=7))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _px_fmt(v)))

            # Header: simbol + arah (kiri), R:R + conf (kanan), meta di baris kedua
            renderer = fig.canvas.get_renderer()

            def _frac_w(artist) -> float:
                return artist.get_window_extent(renderer).width / fig.bbox.width

            def _pill_pad(fs: float, pad: float) -> float:
                return 2 * pad * fs / 72.0 / fig_w

            hdr_l, hdr_r, y1, y2 = 0.025, 0.975, 0.945, 0.893

            sym_t = fig.text(
                hdr_l, y1, symbol, ha="left", va="center",
                fontsize=24, fontweight="bold", color="#ffffff",
            )
            pill_fs = 12.0
            fig.text(
                hdr_l + _frac_w(sym_t) + 0.016, y1,
                ("▲ " if is_long else "▼ ") + (direction.upper() or "—"),
                ha="left", va="center", fontsize=pill_fs, fontweight="bold", color=DARK_INK,
                bbox=dict(boxstyle="round,pad=0.4", facecolor=dir_color, edgecolor="none"),
            )

            stat_fs = 11.5
            x_edge = hdr_r - _pill_pad(stat_fs, 0.45) / 2
            for txt in (f"CONF {conf_s}", f"R:R 1:{rr:.2f}"):
                t = fig.text(
                    x_edge, y1, txt, ha="right", va="center",
                    fontsize=stat_fs, fontweight="bold", color=TEXT,
                    bbox=dict(boxstyle="round,pad=0.45", facecolor=PANEL, edgecolor=GRID, lw=1.0),
                )
                x_edge -= _frac_w(t) + _pill_pad(stat_fs, 0.45) + 0.012

            fig.text(
                hdr_l, y2,
                f"{interval}  ·  Tier {tier}  ·  {setup or '—'}  ·  {state or '—'}",
                ha="left", va="center", fontsize=10.5, color=MUTED,
            )
            fig.text(
                hdr_r, y2, f"Entry {_px_fmt(el)} – {_px_fmt(eh)}",
                ha="right", va="center", fontsize=10.5, color=MUTED,
            )
            fig.add_artist(Line2D(
                [hdr_l, hdr_r], [0.864, 0.864], transform=fig.transFigure,
                color=dir_color, lw=1.6, alpha=0.6,
            ))
            fig.text(hdr_r, 0.018, "UTC", ha="right", va="bottom", fontsize=8.5, color=MUTED)

            buf = BytesIO()
            fig.savefig(buf, format="png", dpi=dpi, facecolor=BG, edgecolor="none")
        png = buf.getvalue()
        if not png:
            return None, "Chart render empty"
        arrow = "🟢" if is_long else "🔴"
        caption = (
            f"{arrow} {symbol} {direction} · {interval}\n"
            f"Tier {tier} · {setup} · {state}\n"
            f"R:R 1:{rr:.2f} · conf {conf_s}"
        )
        return png, caption
    except Exception as e:
        logger.warning("chart render fail-soft: %s: %s", type(e).__name__, e)
        return None, f"Chart render error: {type(e).__name__}: {e}"
    finally:
        if fig is not None:
            plt.close(fig)


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
        return _fmt_settings_view(memory), _kb_settings(memory)
    if data == "menu:calc":
        return _fmt_calc_help(), _kb_back()
    if data == "menu:breadth":
        return _fmt_breadth(memory), _kb_back()
    if data.startswith("set:mode:"):
        mode = data.split(":")[-1].strip().lower()
        if mode in ("active", "hybrid", "passive"):
            memory.set_runtime("delivery_mode", mode)
            return (
                f"✅ Delivery mode → {mode}\n\n" + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        return (
            f"⚠️ Mode '{mode}' tidak dikenal\n\n" + _fmt_settings_view(memory),
            _kb_settings(memory),
        )
    if data.startswith("set:setup:"):
        key = data.split(":")[-1].strip().upper()
        setups = memory.get_setup_enabled()
        if key in setups:
            setups[key] = not setups[key]
            memory.set_runtime("setup_enabled_json", json.dumps(setups))
            state = "ON" if setups[key] else "OFF"
            return (
                f"{'✅' if setups[key] else '⏸'} {key} → {state}\n\n"
                + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        return (
            f"⚠️ Setup {key} tidak dikenal\n\n" + _fmt_settings_view(memory),
            _kb_settings(memory),
        )
    if data == "set:pause:toggle":
        paused = memory.get_runtime("signals_paused_until")
        if paused:
            memory.set_runtime("signals_paused_until", "")
            msg = "▶️ Signals resumed"
        else:
            until = (now_utc() + timedelta(hours=1)).isoformat()
            memory.set_runtime("signals_paused_until", until)
            msg = f"⏸ Signals paused 1h until {until[:19]} UTC"
        return msg + "\n\n" + _fmt_settings_view(memory), _kb_settings(memory)
    if data.startswith("sig:reason:"):
        sid = data.split(":", 2)[-1]
        return _fmt_reasoning(memory, sid), _kb_back_to_signal(sid)
    if data.startswith("sig:card:"):
        sid = data.split(":", 2)[-1]
        return _fmt_signal_card_from_db(memory, sid), _kb_signal(sid)
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

    
    async def send_photo(
        self,
        chat_id: str,
        png_bytes: bytes,
        *,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> bool:
        """Upload PNG chart ke Telegram (multipart). Fail-soft False."""
        if not self.available() or not chat_id or not png_bytes:
            return False
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field(
            "photo",
            png_bytes,
            filename="chart.png",
            content_type="image/png",
        )
        if caption:
            form.add_field("caption", caption[:1024])
        if reply_markup is not None:
            form.add_field("reply_markup", json.dumps(reply_markup))
        url = self._url("sendPhoto")
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form) as resp:
                    raw = await resp.json(content_type=None)
                    return bool(raw.get("ok"))
        except Exception as e:
            logger.debug("sendPhoto fail-soft: %s", type(e).__name__)
            return False

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
            # Chart butuh async candle fetch + PNG upload
            if data.startswith("sig:chart:"):
                sid = data.split(":", 2)[-1]
                png, caption = await render_signal_chart_png(memory, sid)
                if png:
                    ok = await bot.send_photo(
                        chat_id, png,
                        caption=caption,
                        reply_markup=_kb_back_to_signal(sid),
                    )
                    if not ok:
                        await bot.send_message(
                            chat_id,
                            caption + "\n(chart upload gagal)",
                            reply_markup=_kb_back_to_signal(sid),
                        )
                else:
                    await bot.send_message(
                        chat_id,
                        caption or "Chart tidak tersedia.",
                        reply_markup=_kb_back_to_signal(sid),
                    )
                n += 1
                continue
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
        elif low.startswith("calc"):
            calc_out = _parse_calc_message(body)
            if calc_out:
                await bot.send_message(chat_id, calc_out, reply_markup=_kb_back())
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
    hl_flow_buf = get_hl_trade_flow_buffer()
    hl_flow_buf.start()
    logger.info("📈 HL trade flow buffer · starting trades WS")
    # Warm-subscribe top volume symbols supaya sub≠0 sebelum cycle pertama
    try:
        warm_assets = status.get("hyperliquid", {}).get("assets") or []
        if warm_assets:
            warm = sorted(warm_assets, key=lambda a: a.day_ntl_vlm, reverse=True)[:25]
            hl_buf.set_symbols([a.symbol for a in warm])
            hl_flow_buf.set_symbols([a.symbol for a in warm])
            logger.info("📖 HL book · warm sub request %d symbols", len(warm))
    except Exception as e:
        logger.debug("HL book warm skip: %s", type(e).__name__)

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
        logger.info("SIGTERM/SIGINT — flush & exit (cancel-safe)")
        loop.create_task(flush_and_exit(memory, queue, timeout=20))

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
        loop.add_signal_handler(signal.SIGINT, _sigterm_handler)
    except NotImplementedError:
        pass

    await startup_recovery(memory)

    # Transparansi: apakah DB artifact benar-benar bawa history (bukan dari nol)
    log_db_continuity(memory)

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

    # ΔOI×Δharga: warm dari metric_oi → Tier A tidak menunggu 10 menit tiap restart
    warm_oi_snapshot_buffer(memory)

    # §V.3 #12 — macro calendar: builtin seed + Gemini enrich (1×/hari)
    try:
        gem = GeminiClient(memory)
        if gem.available():
            await gem.refresh_macro_calendar(memory)
    except Exception as e:
        logger.debug("macro calendar gemini skip: %s", type(e).__name__)
    try:
        evs = parse_macro_events(memory)
        nxt = minutes_to_next_macro(memory)
        today_n = len(macro_events_today(memory))
        logger.info(
            "📅 macro calendar · %d events · today=%d · next=%s %s",
            len(evs),
            today_n,
            (nxt[1] or "—"),
            f"{nxt[0]:.0f}m" if nxt[0] is not None else "",
        )
        # Alert Telegram 1×/hari (operator) — hari ini + 7d
        digest_day = memory.get_runtime("macro_digest_day") or ""
        day_key = now_utc().strftime("%Y-%m-%d")
        if (
            not dry_run
            and bot.available()
            and digest_day != day_key
            and (today_n > 0 or any(
                0 <= ((e["at"] - now_utc()).total_seconds() / 60.0) <= 24 * 7
                for e in evs if e["impact"] in ("high", "medium")
            ))
        ):
            try:
                chat = (
                    os.environ.get("CRYPTONE_OPERATOR_CHAT_ID")
                    or os.environ.get("CRYPTONE_USER_CHAT_ID")
                    or ""
                ).strip()
                if chat:
                    await bot.send_message(chat, format_macro_digest(memory))
                memory.set_runtime("macro_digest_day", day_key)
                logger.info("📅 macro digest · sent to telegram")
            except Exception as e:
                logger.debug("macro digest send fail-soft: %s", type(e).__name__)
    except Exception as e:
        logger.debug("macro calendar summary skip: %s", type(e).__name__)

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
            log_cycle_header(
                cycle_n,
                int(elapsed // 60),
                int(rem // 60),
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
                        px_s = f" @ {_ui_px(ev['px'])}" if ev.get("px") else ""
                        plain_tp1 = (
                            f"🎯 TP1  {ev['symbol']} {ev['direction']}{px_s}\n"
                            f"SL → breakeven  ·  sisa posisi → TP2"
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
                        rr_s = f"  ·  R:R {ev['realized_rr']:+.2f}"
                    px_s = f" @ {_ui_px(ev['px'])}" if ev.get("px") else ""
                    plain_c = (
                        f"{icon} CLOSE  {ev['symbol']} {ev['direction']}\n"
                        f"{ev['outcome']}  ·  {reason}{px_s}{rr_s}"
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
                            f"✅ Black Swan cleared\n"
                            f"Held {payload.get('hours', '?')}h  ·  indicators normal"
                        )
                    else:
                        inds = payload.get("indicators") or []
                        plain_bs = (
                            f"🚨 BLACK SWAN MODE\n"
                            f"Indicators  {', '.join(inds) or '—'}\n"
                            f"Count {payload.get('count', '?')}  ·  "
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
                # cascade top movers only when ada activity (detail ada di CYCLE DETAIL)
                if c_map:
                    top = sorted(c_map.items(), key=lambda x: -x[1])[:3]
                    top_s = ", ".join(f"{s}=${u/1e6:.2f}M" for s, u in top)
                    logger.info("🌊 cascade top · %s", top_s)
                sigs = await run_pipeline_cycle(
                    memory,
                    pinned=pinned,
                    cascade_map=c_map,
                    cascade_side_map=c_side,
                    dry_run=dry_run,
                )
                total_signals += len(sigs)
                if cycle_n % 12 == 0:
                    memory.prune_ephemeral()
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
                    push_ok, push_note = should_push_signal(s.tier, memory)
                    if not push_ok:
                        logger.info(
                            "⏸ hold push %s %s Tier %s · %s (tetap di DB/Radar)",
                            s.symbol, s.direction, s.tier, push_note,
                        )
                        continue
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
        try:
            await get_hl_trade_flow_buffer().stop()
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
        start_dt = since or (now - timedelta(minutes=max(SIGNAL_COOLDOWN_MIN, 10)))
        # key per (sym, since-bucket) — multi-horizon/simbol aman
        cache_key = f"{sym}:{int(start_dt.timestamp())}"
        if cache_key in candle_cache:
            candles = candle_cache[cache_key]
        else:
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

        # Expiry dicek SETELAH evaluasi SL/TP (jangan NEUTRAL kalau wick sudah kena).
        exp = _parse_ts(row.get("expires_at") or row.get("valid_until"))

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

        # Time stop: ARMED tapi harga masih “di sekitar” entry (stale, tak jalan).
        # Band ATR-based (bukan fixed ±0.3% — terlalu sempit di BTC, longgar di alt).
        armed_at = _parse_ts(row.get("armed_at"))
        horizon = str(row.get("horizon") or "").upper()
        time_stop_h = TIME_STOP_HOURS.get(horizon)
        current_px = last_c or mid
        entry_mid_ts = (
            (entry_lo + entry_hi) / 2.0 if (entry_lo > 0 and entry_hi >= entry_lo)
            else None
        )
        atr_ts = 0.0
        if memory is not None and entry_mid_ts:
            hist_ts = memory.get_atr_history(sym, "1h") or []
            if hist_ts:
                atr_ts = float(hist_ts[-1])
        if atr_ts <= 0 and entry_mid_ts and stop:
            # rough: INTRADAY SL ≈ 1.5×ATR
            atr_ts = abs(float(stop) - entry_mid_ts) / 1.5
        # still-in-zone: harga MASIH di entry zone asli (bukan ±0.5 ATR — terlalu longgar).
        # Kalau sudah lari 0.5R+ jangan TIME_STOP seolah stalled.
        still_in_zone = False
        if (
            current_px is not None
            and entry_lo > 0
            and entry_hi >= entry_lo
        ):
            px = float(current_px)
            pad = max((entry_hi - entry_lo) * 0.05, abs(entry_mid_ts or entry_lo) * 0.0005)
            still_in_zone = (entry_lo - pad) <= px <= (entry_hi + pad)
        if (
            armed_at is not None
            and time_stop_h is not None
            and (now - armed_at).total_seconds() / 3600.0 >= time_stop_h
            and current_px is not None
            and still_in_zone
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
        # exit_px = level SL (bukan wick ekstrem) → realized RR ≈ -1.0R, bukan -1.5R
        if sl_hit:
            outcome = "PARTIAL" if already_tp1 else "LOSS"
            exit_px = float(stop) if stop > 0 else float(ref_px or 0)
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                exit_px, tp1=tp1, tp1_hit=already_tp1,
            )
            memory.resolve_signal(
                sid, outcome, realized_rr=rr, tp1_hit=True if already_tp1 else None,
            )
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "SL", "px": exit_px, "realized_rr": rr,
            })
            icon = "🎯" if outcome == "PARTIAL" else "🛑"
            logger.info(
                "%s CLOSE %s %s · %s · SL @ %.6g · R:R %.2f · was_tp1=%s",
                icon, sym, direction, outcome, exit_px, rr, already_tp1,
            )
            continue

        # 3) TP2 — exit di level TP2, bukan wick lewat
        if tp2_hit:
            exit_px = float(tp2) if tp2 > 0 else float(ref_px or 0)
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                exit_px, tp1=tp1, tp1_hit=already_tp1,
            )
            memory.resolve_signal(sid, "PROFIT", realized_rr=rr, tp1_hit=True)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "PROFIT", "reason": "TP2", "px": exit_px, "realized_rr": rr,
            })
            logger.info(
                "✅ CLOSE %s %s · PROFIT · TP2 @ %.6g · R:R %.2f",
                sym, direction, exit_px, rr,
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

        # 5) Expiry ARMED — hanya jika SL/TP tidak resolve di window ini
        if state == "ARMED" and exp is not None and now >= exp:
            outcome = "PARTIAL" if already_tp1 else "NEUTRAL"
            memory.resolve_signal(sid, outcome, tp1_hit=already_tp1 or None)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "expired", "px": current_px,
            })
            logger.info(
                "⏱ CLOSE %s %s · %s · expired after market check · was_tp1=%s",
                sym, direction, outcome, already_tp1,
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



def log_db_continuity(memory: "MemoryEngine") -> None:
    """
    Log ringkas isi DB saat startup — buktikan data tidak hilang antar-run.
    OI warm-up 0 setelah jeda panjang = normal (snapshot basi); signal history harus tetap.
    """
    if not memory._conn:
        logger.warning("📦 DB continuity · no connection")
        return
    try:
        def _n(sql: str) -> int:
            row = memory._conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        n_sig = _n("SELECT COUNT(*) FROM signal_history")
        n_res = _n(
            "SELECT COUNT(*) FROM signal_history "
            "WHERE outcome IS NOT NULL AND is_simulated = 0"
        )
        n_act = memory.count_active_signals()
        n_fund = _n("SELECT COUNT(*) FROM metric_funding")
        n_atr = _n("SELECT COUNT(*) FROM metric_atr")
        n_oi = _n("SELECT COUNT(*) FROM metric_oi")
        n_oi_fresh = 0
        try:
            # ~20 menit terakhir (wall clock)
            cutoff = (now_utc() - timedelta(minutes=20)).isoformat()
            n_oi_fresh = _n(
                f"SELECT COUNT(*) FROM metric_oi WHERE recorded_at >= '{cutoff}'"
            )
        except Exception:
            pass
        logger.info(
            "📦 DB continuity · signals=%d (resolved=%d active=%d) · "
            "fund=%d · atr=%d · oi=%d (fresh20m=%d)",
            n_sig, n_res, n_act, n_fund, n_atr, n_oi, n_oi_fresh,
        )
        if n_sig == 0 and n_fund == 0 and n_atr == 0:
            logger.warning(
                "📦 DB terlihat KOSONG — artifact mungkin tidak ke-download "
                "atau run sebelumnya di-cancel sebelum Upload DB"
            )
        elif n_oi == 0 or n_oi_fresh == 0:
            logger.info(
                "📦 OI warm 0 setelah jeda = normal (butuh ~10m live untuk ΔOI); "
                "bukan berarti history sinyal hilang"
            )
    except Exception as e:
        logger.debug("DB continuity log fail-soft: %s", type(e).__name__)


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
    """Handler SIGTERM (cancel GH Actions): flush + WAL checkpoint lalu exit."""
    logger.info("🧹 graceful stop · flush DB sebelum runner mati")
    try:
        await asyncio.wait_for(memory.flush_writes(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("flush timeout %ss", timeout)
    try:
        memory.checkpoint_wal()
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
    try:
        await get_hl_trade_flow_buffer().stop()
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
