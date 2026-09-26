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
import signal
import sqlite3
import statistics
import sys
import time
import uuid
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

PROTOCOL_VERSION = "4.5.0"
SCHEMA_VERSION = 2

# WIB = UTC+7 (hanya untuk tampilan; semua TIMESTAMP di DB = UTC)
WIB = timezone(timedelta(hours=7))
UTC = timezone.utc

logger = logging.getLogger("cryptone")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def now_wib() -> datetime:
    return datetime.now(tz=WIB)


def to_wib_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


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
MIN_RR = {
    "SCALPING": 1.5,
    "INTRADAY": 2.0,
    "SWING": 3.0,
}
MIN_CONFIRMATIONS = 2  # locked §II.7 — 2 dari 3
MAX_CONCURRENT_SIGNALS = 10
MAX_SIGNALS_PER_CYCLE = 3  # cap spam saat funding ekstrem berkepanjangan
SIGNAL_COOLDOWN_MIN = 30
EVENT_BLACKOUT_MIN = 60
TIER_A_MIN_RR = 2.5
TIER_A_MIN_CONFIRMS = 3
TIER_B_MIN_RR = 2.0
TIER_B_MIN_CONFIRMS = 2
BLACK_SWAN_TRIGGER_COUNT = 3  # locked — 3 dari 7
BLACK_SWAN_MIN_DURATION_H = 2
MAX_USER_WALLETS = 5
UNIVERSE_SCAN_INTERVAL_SEC = 900  # 15 menit
TRIGGER_CHECK_TIER1_SEC = 300     # 5 menit
CORRELATION_UPDATE_SEC = 3600
VOLUME_BASELINE_UPDATE_DAYS = 14
PARTIAL_WEIGHT = 0.5  # locked — bobot PARTIAL di win_rate
DEFAULT_CYCLE_INTERVAL_SEC = 300
DEFAULT_MAX_RUNTIME_SEC = 19800  # 5.5 jam

# --- Kategori B — Dynamic (per symbol, dari histori) ---
# Threshold dihitung runtime: Funding P95, Cascade P90, ATR P20, dll.
# Min sample = 30, lookback 30 hari. Cold-start → Kategori D.

# --- Kategori C — Semi-Dynamic (boundary + nilai tengah dinamis) ---
ENTRY_ZONE_ATR_MIN = 0.3
ENTRY_ZONE_ATR_MAX = 2.0
CONFIDENCE_FLOOR_MIN = 0.3
CONFIDENCE_FLOOR_MAX = 0.7
HIT_TOLERANCE_ATR_MIN = 0.0
HIT_TOLERANCE_ATR_MAX = 0.15
SESSION_SPIKE_MIN = 1.3
SESSION_SPIKE_MAX = 3.0
STOP_HUNT_VOLUME_Z_DEFAULT = 2.0
STOP_HUNT_OI_Z_DEFAULT = 2.0
STOP_HUNT_PRICE_CHANGE_DEFAULT = 0.005  # 0.5%
STOP_HUNT_XDIV_DEFAULT = 0.005

# --- Kategori D — Starting (cold-start) ---
CONFIDENCE_COLD_START = 0.5
HIT_TOLERANCE_ATR_DEFAULT = 0.05
RELIABILITY_DEFAULT = 0.5
VOLUME_BASELINE_FALLBACK = 1.0
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
    parse_mode: str = "Markdown"
    chart_bytes: bytes | None = None


@dataclass
class SignalRecord:
    """Record sinyal aktif / history (kolom inti §VI.3 + §XVI.8)."""
    signal_id: str
    symbol: str
    direction: str  # LONG | SHORT
    horizon: str    # SCALPING | INTRADAY | SWING
    tier: str       # A | B | C
    entry_zone_low: float
    entry_zone_high: float
    stop_loss: float
    tp1: float
    tp2: float
    rr: float
    confidence: float
    trigger_type: str
    setup_type: str
    reasoning: str = ""
    created_at: datetime | None = None
    valid_until: datetime | None = None
    tp1_hit: bool = False
    tp1_hit_at: datetime | None = None
    last_checked_at: datetime | None = None
    realized_rr: float | None = None
    outcome: str | None = None  # PROFIT | PARTIAL | LOSS | NEUTRAL | None=ACTIVE
    resolved_at: datetime | None = None


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
            schema_version INTEGER DEFAULT 1
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
            schema_version INTEGER DEFAULT 1
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
                "INSERT INTO metric_funding "
                "(symbol, rate, recorded_at, is_simulated, schema_version) "
                "VALUES (:symbol, :rate, :recorded_at, :is_simulated, 1)",
                {**params, "is_simulated": params.get("is_simulated", 0)},
            )
        elif table == "metric_atr":
            self._conn.execute(
                "INSERT INTO metric_atr "
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
            self._conn.execute(
                "INSERT OR REPLACE INTO signal_history ("
                "signal_id, symbol, direction, horizon, tier, "
                "entry_zone_low, entry_zone_high, stop_loss, tp1, tp2, rr, "
                "confidence, trigger_type, reasoning, setup_type, "
                "tp1_hit, last_checked_at, created_at, valid_until, "
                "is_simulated, schema_version"
                ") VALUES ("
                ":signal_id, :symbol, :direction, :horizon, :tier, "
                ":entry_zone_low, :entry_zone_high, :stop_loss, :tp1, :tp2, :rr, "
                ":confidence, :trigger_type, :reasoning, :setup_type, "
                "0, :last_checked_at, :created_at, :valid_until, "
                ":is_simulated, 2)",
                params,
            )
        elif table == "active_signals":
            self._conn.execute(
                "INSERT OR REPLACE INTO active_signals (signal_id, symbol, expires_at) "
                "VALUES (:signal_id, :symbol, :expires_at)",
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
        """Jumlah USD cascade per bucket event (untuk P90)."""
        if not self._conn:
            return []
        cutoff = (now_utc() - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT amount_usd FROM liquidation_log "
            "WHERE symbol = ? AND recorded_at >= ? ORDER BY recorded_at",
            (symbol, cutoff),
        ).fetchall()
        return [float(r["amount_usd"]) for r in rows]

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
        """True kalau ada sinyal symbol ini dibuat < cooldown_min yang lalu."""
        if not self._conn:
            return False
        cutoff = (now_utc() - timedelta(minutes=cooldown_min)).isoformat()
        row = self._conn.execute(
            "SELECT 1 FROM signal_history WHERE symbol = ? AND created_at >= ? LIMIT 1",
            (symbol, cutoff),
        ).fetchone()
        return row is not None

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

    def resolve_signal(
        self,
        signal_id: str,
        outcome: str,
        *,
        realized_rr: float | None = None,
        tp1_hit: bool | None = None,
    ) -> None:
        """Tutup sinyal: outcome + realized_rr, hapus dari active_signals."""
        if not self._conn:
            return
        ts = now_utc().isoformat()
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
        self._conn.commit()

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
LISTING_AGE_MIN_DAYS = 7         # Layer 0 filter


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
    Cold-start: threshold abs 0.01% per 8h (≈0.0001) sebagai jembatan D.
    """
    hist = memory.get_funding_history(asset.symbol) if memory else []
    # funding HL sudah dalam bentuk rate (mis. 0.0001 = 0.01%)
    cold = 0.0001
    thr = _p_threshold([abs(x) for x in hist], p_value, cold_default_abs=cold)
    val = abs(asset.funding)
    if val <= thr:
        return None
    # strength: seberapa jauh di atas threshold, cap 1.0
    strength = clamp((val - thr) / thr if thr > 0 else 1.0, 0.0, 1.0)
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
    )


def evaluate_confirms(
    results: list[ConfirmResult],
) -> tuple[bool, int, int]:
    """
    Returns (passed, confirm_count, n_active).
    Agent 'off' (detail agent off / no data yang menandai non-aktif)
    tidak dihitung ke N — hanya yang punya data.
    """
    inactive = {
        "agent off", "no data", "no binance data", "no xref data", "no oi",
    }
    active = [r for r in results if r.detail not in inactive]
    n = len(active)
    need = min_confirm_required(n)
    if need is None:
        return False, 0, n
    count = sum(1 for r in active if r.confirmed)
    return count >= need, count, n


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

    zone_w = clamp(1.0, ENTRY_ZONE_ATR_MIN, ENTRY_ZONE_ATR_MAX) * atr
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
    )


def assign_tier(rr: float, confirm_count: int) -> str:
    """
    §I.2 Layer 6 tier.
    A: RR≥2.5 + 3 confirms | B: RR≥2.0 + 2 confirms | C: sisanya.
    Epsilon 1e-9: hindari float miss (RR desain 3.0/1.5 = 2.0 tepat).
    Catatan: confidence ≠ tier. Conf tinggi + tier C = trigger kuat tapi
    confirm/RR belum tembus gerbang B (harusnya jarang untuk INTRADAY).
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


async def context_modifier(
    symbol: str,
    direction: str,
    anchor: str | None,
    *,
    us10y: float | None = None,
    fear_greed: float | None = None,
) -> float:
    """
    L5 Context — multiplier 0.5–1.2 (§I.2).
    Fail-soft: data kosong → 1.0 (netral).
    - US10Y tinggi (>4.5): tekan LONG, sedikit angkat SHORT
    - Fear&Greed: contrarian — extreme fear angkat LONG, extreme greed angkat SHORT
    """
    mult = 1.0
    _ = (symbol, anchor)
    if us10y is not None:
        if us10y >= 4.5:
            mult *= 0.92 if direction == "LONG" else 1.05
        elif us10y <= 3.5:
            mult *= 1.05 if direction == "LONG" else 0.95
    if fear_greed is not None:
        # 0–100. Extreme fear ≤25 → favor LONG; extreme greed ≥75 → favor SHORT
        if fear_greed <= 25:
            mult *= 1.08 if direction == "LONG" else 0.94
        elif fear_greed <= 40:
            mult *= 1.03 if direction == "LONG" else 0.97
        elif fear_greed >= 75:
            mult *= 0.94 if direction == "LONG" else 1.08
        elif fear_greed >= 60:
            mult *= 0.97 if direction == "LONG" else 1.03
    return clamp(mult, 0.5, 1.2)


# alias kompat
def context_modifier_default(
    symbol: str,
    direction: str,
    anchor: str | None,
) -> float:
    return 1.0


def veto_checks(
    memory: "MemoryEngine | None",
    symbol: str,
    confidence_final: float,
    confidence_floor: float = CONFIDENCE_FLOOR_COLD,
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
    if memory.symbol_on_cooldown(symbol):
        return False, "symbol cooldown"
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
                await memory.enqueue_write("metric_atr", {
                    "symbol": symbol,
                    "timeframe": "1h",
                    "value": atr,
                    "recorded_at": now_utc().isoformat(),
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


async def process_trigger(
    asset: AssetCtx,
    trigger: TriggerHit,
    memory: "MemoryEngine | None",
    anchor: str | None,
    cascade_side: str | None = None,
) -> PipelineSignal | None:
    """
    Satu trigger → L2→L6. Return PipelineSignal atau None (gagal gate).
    cascade_side: LONG/SHORT dari side liquidasi (BUY liq = short cascade pressure).
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

    # L3 confirms
    # Tanpa L2 book: microstructure pakai funding-proxy (tetap ACTIVE, bukan "no data")
    # supaya N≥2 bersama cross_exchange. Flow off kalau wallets kosong (§X.2).
    hl_px = asset.mid_px or asset.mark_px
    ref_px, ref_src = await cross_exchange_price(asset.symbol, memory)

    micro = microstructure_confirm(direction, imbalance=None, absorption=None)
    if micro.detail == "no data":
        fund_ok = (
            (direction == "SHORT" and asset.funding > 0)
            or (direction == "LONG" and asset.funding < 0)
        )
        micro = ConfirmResult(
            "microstructure",
            fund_ok,
            "funding_proxy" if fund_ok else "funding_proxy_no_align",
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

    passed, conf_count, n_active = evaluate_confirms(confirms)
    if not passed:
        logger.debug(
            "%s L3 fail confirms=%d n=%d %s",
            asset.symbol, conf_count, n_active,
            [f"{r.agent}:{r.confirmed}" for r in confirms],
        )
        return None

    atr = await estimate_atr(asset.symbol, hl_px, memory)
    if atr is None:
        return None

    timing = analyst_timing(
        direction, hl_px, atr, horizon, trigger.strength, conf_count,
    )
    if timing is None:
        return None

    # L5 — macro FRED US10Y + Fear&Greed (fail-soft None)
    us10y = await fred_latest("DGS10") if _env_ok("FRED_API_KEY") else None
    fng = await fear_greed_latest()
    ctx_mult = await context_modifier(
        asset.symbol, direction, anchor, us10y=us10y, fear_greed=fng,
    )
    conf_final = clamp(timing.base_confidence * ctx_mult, 0.0, 1.0)

    # L6 veto + tier
    ok, reason = veto_checks(memory, asset.symbol, conf_final)
    if not ok:
        logger.debug("%s L6 veto: %s", asset.symbol, reason)
        return None

    tier = assign_tier(timing.rr, conf_count)
    lifetime_h = SIGNAL_LIFETIME_HOURS.get(horizon, 24)
    created = now_utc()
    valid_until = created + timedelta(hours=lifetime_h)
    sid = str(uuid.uuid4())

    reasoning = (
        f"{trigger.setup_type} | {trigger.trigger_type} "
        f"raw={trigger.raw_value:.6g} thr={trigger.threshold:.6g} "
        f"str={trigger.strength:.2f} | confirm {conf_count}/{n_active} | "
        f"ctx×{ctx_mult:.2f}"
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
    if memory is not None:
        ts = now_utc().isoformat()
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
    dry_run: bool = False,
) -> list[PipelineSignal]:
    """
    Satu cycle penuh L0–L6.
    cascade_map: symbol → USD cascade 5m (dari WS buffer; kosong = no cascade trigger).
    """
    cascade_map = cascade_map or {}
    try:
        candidates, anchor = await run_universe_scan(memory, pinned=pinned)
    except DataUnavailable as e:
        logger.error("L0 fail-soft skip cycle: %s", e)
        return []

    if not candidates:
        logger.info("L0: no candidates")
        return []

    # Black swan gate: freeze sinyal baru (§VII.3)
    bs = await check_black_swan(memory, anchor, candidates, cascade_map)
    if bs.active:
        logger.warning("cycle skip new signals — BLACK SWAN %s", bs.indicators)
        return []
    if bs.mode == "ELEVATED":
        logger.info("elevated risk indicators=%s — Tier A only later", bs.indicators)

    setup_enabled = memory.get_setup_enabled()
    produced: list[PipelineSignal] = []

    # Market funding polarity (observability — bukan filter)
    n_fund_pos = sum(1 for a in candidates if a.funding > 0)
    n_fund_neg = sum(1 for a in candidates if a.funding < 0)
    n_fund_zero = len(candidates) - n_fund_pos - n_fund_neg

    # Rank by |funding| ekstrem dulu (hemat API ATR) — top slice
    ranked = sorted(candidates, key=lambda a: abs(a.funding), reverse=True)

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

            sig = await process_trigger(asset, trigger, memory, anchor)
            if sig is None:
                n_gate_fail += 1
                continue
            if not dry_run:
                await persist_signal(memory, sig, simulated=False)
            else:
                await persist_signal(memory, sig, simulated=True)
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
) -> bool:
    """#3 Anchor drop >3% ATAU top2 correlated avg drop >3% searah."""
    if anchor_drop_pct is not None and abs(anchor_drop_pct) > 0.03:
        return True
    if (
        top2_avg_drop_pct is not None
        and abs(top2_avg_drop_pct) > 0.03
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
        # prevDay drop kasar sebagai proxy correlation/anchor move
        anchor_drop = None
        for a in candidates:
            if a.symbol == anchor and a.prev_day_px > 0 and hl_px:
                anchor_drop = (hl_px - a.prev_day_px) / a.prev_day_px
                break
    else:
        anchor_drop = None

    state = evaluate_black_swan_indicators(
        atr_now=atr_now,
        atr_hist=atr_hist,
        cascade_usd_5m=cascade_total,
        funding_now=funding_now,
        funding_1h_ago=None,  # butuh histori 1h — diisi bootstrap/metric
        hl_px=hl_px,
        xref_px=xref_px,
        anchor_drop_pct=anchor_drop,
    )

    if state.active:
        logger.warning(
            "BLACK SWAN MODE indicators=%s count=%d",
            state.indicators, state.count,
        )
        memory.set_runtime("black_swan_mode", "1")
        await memory.enqueue_write("liquidation_log", {
            # reuse path? better blackswan_log — add dispatch
            "symbol": anchor or "MARKET",
            "side": "BS",
            "amount_usd": float(cascade_total or 0),
            "exchange": "blackswan",
            "recorded_at": now_utc().isoformat(),
        })
        # proper log via runtime + dedicated insert
        if memory._conn:
            memory._conn.execute(
                "INSERT INTO blackswan_log "
                "(triggered_indicators, trigger_count, started_at) "
                "VALUES (?, ?, ?)",
                (json.dumps(state.indicators), state.count, now_utc().isoformat()),
            )
            memory._conn.commit()
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
GEMINI_MODEL = "gemini-2.0-flash"


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

    def available(self) -> bool:
        return bool(self.api_key)

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        if now - self._minute_start >= 60:
            self._minute_start = now
            self._calls_this_minute = 0
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

        url = (
            f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent"
            f"?key={self.api_key}"
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
            raw = await http_post_json(url, body, timeout=30.0)
            self._calls_this_minute += 1
            ms = (time.monotonic() - t0) * 1000
            text = ""
            for cand in raw.get("candidates") or []:
                for part in (cand.get("content") or {}).get("parts") or []:
                    text += part.get("text") or ""
            if not text:
                return LLMResult("", function_name, ms, success=False, error="empty response")
            if use_cache:
                self._cache_put(key, function_name, text, ttl_h=cache_ttl_h)
            if self.memory is not None:
                await self.memory.enqueue_write("llm_call_log", {
                    # dispatched if we add table support — log via runtime for now
                })
            return LLMResult(text.strip(), function_name, ms, success=True)
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            logger.warning("▸ gemini fail-soft · %s · %s", function_name, type(e).__name__)
            return LLMResult("", function_name, ms, success=False, error=str(e)[:80])

    async def sector_tag(self, symbol: str) -> str | None:
        """Tag sector kasar untuk universe (cache 24h)."""
        prompt = (
            f"Crypto perpetual symbol '{symbol}' on Hyperliquid. "
            "Reply with ONE sector tag only from: "
            "L1, L2, DeFi, Meme, AI, GameFi, Infra, Stable, RWA, Other. "
            "No explanation."
        )
        r = await self.generate("sector_tag", prompt, max_tokens=16, cache_ttl_h=24.0)
        if not r.success or not r.text:
            return None
        tag = r.text.strip().split()[0].strip(",.")
        return tag[:32]


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

    logger.info(
        "📥 BOOTSTRAP DONE · symbols=%d · funding=%d · atr=%d · anchor=%s",
        len(targets), funding_rows, atr_rows, anchor,
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
    status["telegram"] = {
        "ok": _env_ok("CRYPTONE_TELEGRAM_BOT_TOKEN") and (
            _env_ok("CRYPTONE_USER_CHAT_ID") or _env_ok("CRYPTONE_OPERATOR_CHAT_ID")
        ),
        "note": (
            "token+chat"
            if _env_ok("CRYPTONE_TELEGRAM_BOT_TOKEN")
            else "token missing"
        ),
    }
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

    # --- Not yet wired (honest) ---
    status["binance_ws_liq"] = {"ok": None, "note": "not wired (cascade buffer)"}
    status["hl_ws"] = {"ok": None, "note": "not wired (L2 book)"}
    status["rss_news"] = {"ok": None, "note": "not wired"}
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
# §12  TELEGRAM UI
# =============================================================================

TELEGRAM_API = "https://api.telegram.org"


class TelegramBot:
    """Bot API thin client — sendMessage, fail-soft + rate limit."""

    def __init__(self, token: str | None = None):
        self.token = (token or os.environ.get("CRYPTONE_TELEGRAM_BOT_TOKEN", "")).strip()

    def available(self) -> bool:
        return bool(self.token)

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_preview: bool = True,
    ) -> bool:
        if not self.available() or not chat_id:
            return False
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            raw = await http_post_json(url, payload, timeout=20.0)
            if not raw.get("ok"):
                desc = raw.get("description", "unknown")
                # 429 rate limit
                if "Too Many Requests" in str(desc) or raw.get("error_code") == 429:
                    params = raw.get("parameters") or {}
                    retry = float(params.get("retry_after", 5))
                    raise TelegramRateLimitError(f"telegram 429 retry={retry}", retry_after=retry)
                logger.warning("▸ telegram send fail · chat=%s · %s", chat_id, desc)
                return False
            return True
        except TelegramRateLimitError:
            raise
        except Exception as e:
            logger.warning("▸ telegram error · %s", type(e).__name__)
            return False

    async def send_signal(self, chat_id: str, sig: "PipelineSignal") -> bool:
        arrow = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
        text = (
            f"{arrow}  <b>{sig.symbol}</b> · {sig.horizon} · Tier <b>{sig.tier}</b>\n"
            f"Setup: {sig.setup_type}\n"
            f"Entry: <code>{sig.entry_low:.6g}</code> – <code>{sig.entry_high:.6g}</code>\n"
            f"SL: <code>{sig.stop_loss:.6g}</code>\n"
            f"TP1: <code>{sig.tp1:.6g}</code>  TP2: <code>{sig.tp2:.6g}</code>\n"
            f"R:R 1:{sig.rr:.2f} · conf {sig.confidence:.0%}\n"
            f"Confirms: {sig.confirm_count}/{sig.active_agent_count}\n"
            f"<i>{sig.reasoning[:120]}</i>"
        )
        return await self.send_message(chat_id, text, parse_mode="HTML")


# =============================================================================
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
                ok = await bot.send_message(msg.chat_id, msg.text, parse_mode=None)
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

    async def run_sender_loop(self, bot_client: TelegramBot, min_interval: float = 1.1) -> None:
        while True:
            msg = await self._queue.get()
            try:
                await bot_client.send_message(msg.chat_id, msg.text)
            except TelegramRateLimitError as e:
                await asyncio.sleep(e.retry_after)
                await self._queue.put(msg)
            await asyncio.sleep(min_interval)


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
                create_fresh_db()
            elif not await startup_db_check(db_path):
                logger.error("DB still corrupt after restore · rebuild")
                create_fresh_db()

    memory = MemoryEngine(db_path)
    memory.start()
    queue = TelegramDeliveryQueue()
    bot = TelegramBot()
    if bot.available():
        logger.info("🤖 telegram ready")
    else:
        logger.warning("🤖 telegram OFF · missing token")

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
                # Outcome tracker dulu — free slot sebelum scan baru
                closed_ev = await monitor_active_signals(memory)
                user_chat = os.environ.get("CRYPTONE_USER_CHAT_ID", "").strip()
                op_chat = os.environ.get("CRYPTONE_OPERATOR_CHAT_ID", "").strip()
                for ev in closed_ev:
                    if ev.get("reason") == "TP1":
                        continue  # partial, jangan spam close-msg
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
                        f"{ev['outcome']} · {ev['reason']}{px_s}{rr_s}"
                    )
                    targets_c: list[str] = []
                    if op_chat:
                        targets_c.append(op_chat)
                    elif user_chat:
                        targets_c.append(user_chat)
                    for chat in targets_c:
                        await queue.enqueue(TelegramMessage(
                            chat_id=chat,
                            text=plain_c,
                            priority="normal",
                        ))

                sigs = await run_pipeline_cycle(
                    memory, pinned=pinned, cascade_map={}, dry_run=dry_run,
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
                    plain = (
                        f"{'🟢' if s.direction == 'LONG' else '🔴'} "
                        f"{s.symbol} {s.direction} · {s.horizon} · Tier {s.tier}\n"
                        f"Entry {s.entry_low:.4g}–{s.entry_high:.4g} | "
                        f"SL {s.stop_loss:.4g} | TP1 {s.tp1:.4g} TP2 {s.tp2:.4g}\n"
                        f"R:R 1:{s.rr:.2f} · conf {s.confidence:.0%} · "
                        f"confirms {s.confirm_count}/{s.active_agent_count}\n"
                        f"{s.setup_type}"
                    )
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
                        ))
                n_sent = await queue.drain_once(bot, dry_run=dry_run)
                total_sent += n_sent
            except Exception as e:
                logger.exception("▸ cycle error (continue): %s", e)

            remaining = max_runtime - (time.monotonic() - t0)
            if remaining <= 0:
                break
            await asyncio.sleep(min(cycle_sec, remaining))
    finally:
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
    stop: float,
    exit_px: float,
) -> float:
    """Approx realized R dari mid-entry ke exit vs risk ke SL."""
    entry = (float(entry_low) + float(entry_high)) / 2.0
    risk = abs(entry - float(stop))
    if risk <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_px - entry) / risk
    return (entry - exit_px) / risk


async def monitor_active_signals(memory: MemoryEngine) -> list[dict]:
    """
    Cek harga HL vs SL / TP1 / TP2 untuk semua sinyal aktif.
    - SL hit → LOSS (close)
    - TP2 hit → PROFIT (close)
    - TP1 hit (belum) → flag tp1_hit, tetap aktif (PARTIAL path)
    - expires_at lewat → NEUTRAL (close)
    Return list event yang di-resolve (untuk log / Telegram).
    Fail-soft: HL mids gagal → skip cycle, tetap cek expiry.
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

    for row in active:
        sid = row["signal_id"]
        sym = row["symbol"]
        direction = str(row.get("direction") or "LONG").upper()
        stop = float(row.get("stop_loss") or 0)
        tp1 = float(row.get("tp1") or 0)
        tp2 = float(row.get("tp2") or 0)
        entry_lo = float(row.get("entry_zone_low") or 0)
        entry_hi = float(row.get("entry_zone_high") or 0)
        already_tp1 = bool(row.get("tp1_hit"))

        # 1) time expiry
        exp = _parse_ts(row.get("expires_at") or row.get("valid_until"))
        if exp is not None and now >= exp:
            outcome = "PARTIAL" if already_tp1 else "NEUTRAL"
            memory.resolve_signal(sid, outcome, tp1_hit=already_tp1 or None)
            ev = {
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "expired", "px": None,
            }
            events.append(ev)
            logger.info(
                "⏱ CLOSE %s %s · %s · expired · was_tp1=%s",
                sym, direction, outcome, already_tp1,
            )
            continue

        px = mids.get(sym)
        if px is None or px <= 0:
            memory.touch_signal_check(sid)
            continue

        # 2) SL
        sl_hit = (direction == "LONG" and px <= stop) or (
            direction == "SHORT" and px >= stop
        )
        if sl_hit and stop > 0:
            rr = _realized_rr(direction, entry_lo, entry_hi, stop, px)
            memory.resolve_signal(sid, "LOSS", realized_rr=rr, tp1_hit=already_tp1 or None)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "LOSS", "reason": "SL", "px": px, "realized_rr": rr,
            })
            logger.info(
                "🛑 CLOSE %s %s · LOSS · SL hit @ %.6g · R:R %.2f",
                sym, direction, px, rr,
            )
            continue

        # 3) TP2 (full profit)
        tp2_hit = (direction == "LONG" and px >= tp2) or (
            direction == "SHORT" and px <= tp2
        )
        if tp2_hit and tp2 > 0:
            rr = _realized_rr(direction, entry_lo, entry_hi, stop, px)
            memory.resolve_signal(sid, "PROFIT", realized_rr=rr, tp1_hit=True)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "PROFIT", "reason": "TP2", "px": px, "realized_rr": rr,
            })
            logger.info(
                "✅ CLOSE %s %s · PROFIT · TP2 @ %.6g · R:R %.2f",
                sym, direction, px, rr,
            )
            continue

        # 4) TP1 (partial — keep active)
        tp1_hit = (direction == "LONG" and px >= tp1) or (
            direction == "SHORT" and px <= tp1
        )
        if tp1_hit and tp1 > 0 and not already_tp1:
            memory.touch_signal_check(sid, tp1_hit=True)
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": "PARTIAL", "reason": "TP1", "px": px,
            })
            logger.info(
                "🎯 TP1 %s %s · hit @ %.6g · tetap aktif → TP2",
                sym, direction, px,
            )
            continue

        memory.touch_signal_check(sid)

    if events:
        n_close = sum(1 for e in events if e["outcome"] in ("PROFIT", "LOSS", "NEUTRAL", "PARTIAL") and e.get("reason") != "TP1")
        n_tp1 = sum(1 for e in events if e.get("reason") == "TP1")
        logger.info(
            "📊 monitor · checked=%d · closed=%d · tp1=%d · active_left=%d",
            len(active), n_close, n_tp1, memory.count_active_signals(),
        )
    return events


async def startup_recovery(memory: MemoryEngine) -> None:
    """Tutup sinyal expired / orphan (§XIII.10)."""
    now = now_utc()
    active = memory.get_active_signals()
    closed = 0
    for row in active:
        exp = _parse_ts(row.get("expires_at") or row.get("valid_until"))
        if exp is not None and now >= exp:
            memory.mark_signal_expired(row["signal_id"], outcome="NEUTRAL")
            closed += 1
    # Observability: recent history (cooldown window) vs active residual
    recent = 0
    if memory._conn:
        cutoff = (now - timedelta(minutes=SIGNAL_COOLDOWN_MIN)).isoformat()
        row = memory._conn.execute(
            "SELECT COUNT(*) AS n FROM signal_history WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        recent = int(row["n"]) if row else 0
    logger.info(
        "startup_recovery: active=%d expired_closed=%d · recent_%dm=%d",
        len(active) - closed, closed, SIGNAL_COOLDOWN_MIN, recent,
    )
    if recent > 0 and (len(active) - closed) == 0:
        logger.info(
            "startup_recovery: history ada, active kosong — cooldown tetap jaga anti re-fire"
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


def create_fresh_db() -> None:
    """Schema kosong via MemoryEngine (hapus file corrupt dulu)."""
    db = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    p = Path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            p.unlink()
        except OSError as e:
            logger.error("create_fresh_db unlink fail: %s", e)
            return
    mem = MemoryEngine(db)
    mem.start()
    mem.close()
    logger.info("🆕 create_fresh_db · %s", db)


async def flush_and_exit(memory: MemoryEngine, queue: Any, timeout: int = 20) -> None:
    """Handler SIGTERM: flush lalu exit 0."""
    try:
        await asyncio.wait_for(memory.flush_writes(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("flush timeout %ss", timeout)
    memory.close()
    sys.exit(0)


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
