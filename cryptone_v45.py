#!/usr/bin/env python3
"""
Cryptone V4.5 — App entry (Telegram UI · Delivery · Live · CLI)

Engine core: cryptone_engine.py (§1–§11).
Source of Truth: docs/CRYPTONE_V45_MASTER.md

  Radar, bukan executor.
  Math first, LLM second.
  Fail-soft, tapi alert.
  Context ≠ Vote.
"""

from __future__ import annotations

# Re-export full engine surface so `import cryptone_v45` / tests / patches tetap stabil.
# Star-import hanya public names — mirror private (_*) supaya unittest.mock.patch
# dan test suite yang mengais _ATR_CACHE / _env_ok / _parse_* tetap jalan.
import cryptone_engine as _engine
from cryptone_engine import *  # noqa: F403

for _name, _val in list(_engine.__dict__.items()):
    if _name.startswith("__") and _name.endswith("__"):
        continue
    globals()[_name] = _val
del _name, _val

import argparse
import hashlib
import sqlite3
import urllib.error
import urllib.request
import math
import asyncio
import json
from html import escape as _html_escape
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# =============================================================================
# §12  TELEGRAM UI — Super App (tombol, bukan command text)
# =============================================================================
# Master §XII.1 Menu Utama pakai tombol. User ketuk → callback_query.
# Text /start /menu tetap dibuka sebagai entry, isinya keyboard.

TELEGRAM_API = "https://api.telegram.org"

# Label SL wajib mengikuti arah. Engine: LONG → stop di bawah swing low,
# SHORT → stop = swing high + buffer (di atas swing). Label lama hardcode
# "di bawah swing" untuk kedua arah → SHORT tampil salah.
_SL_BASIS_LABEL_BY_DIR: dict[str, dict[str, str]] = {
    "LONG": {
        "atr": "ATR-based",
        "structure": "di bawah swing",
        "structure_widened_min_risk": "di bawah swing (dilebarkan)",
    },
    "SHORT": {
        "atr": "ATR-based",
        "structure": "di atas swing",
        "structure_widened_min_risk": "di atas swing (dilebarkan)",
    },
}
# Fail-soft: arah tidak dikenal → label netral, tidak menebak sisi stop.
_SL_BASIS_LABEL_NEUTRAL: dict[str, str] = {
    "atr": "ATR-based",
    "structure": "di dekat swing",
    "structure_widened_min_risk": "di dekat swing (dilebarkan)",
}
# Ambang sampel untuk menampilkan statistik ke user. Samakan dengan
# ambang hist WR (n≥5). Di bawah ini ditandai "sampel kecil".
MIN_STAT_SAMPLE = 5


def _sl_basis_label(sl_basis, direction=None) -> str:
    key = str(sl_basis or "")
    table = _SL_BASIS_LABEL_BY_DIR.get((direction or "").upper(), _SL_BASIS_LABEL_NEUTRAL)
    return table.get(key, key or "—")
_ENTRY_BASIS_LABEL = {
    "atr": "ATR zone",
    "structure": "swing level",
    "fvg": "FVG gap",
}


# Offset getUpdates (module-level, single process)
_TG_UPDATE_OFFSET = 0
_TG_AUTH_WARNED = False  # warn-once when chat_ids empty



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
                {"text": "🧬 Shadows", "callback_data": "menu:shadow"},
            ],
            [
                {"text": "🔍 Explorer", "callback_data": "menu:explore"},
                {"text": "⚙️ Settings", "callback_data": "menu:settings"},
            ],
            [
                {"text": "❓ Help", "callback_data": "menu:help"},
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


def _kb_help() -> dict:
    """Halaman utama panduan: pilih kategori."""
    return {
        "inline_keyboard": [
            [
                {"text": "📡 Pantau", "callback_data": "help:pantau"},
                {"text": "📒 Catatan", "callback_data": "help:catatan"},
            ],
            [{"text": "🛠 Alat", "callback_data": "help:alat"}],
            [{"text": "⬅️ Menu", "callback_data": "menu:home"}],
        ]
    }


def _kb_help_sub() -> dict:
    """Halaman kategori: kembali ke panduan atau ke menu."""
    return {
        "inline_keyboard": [
            [
                {"text": "⬅️ Panduan", "callback_data": "menu:help"},
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
    sid = signal_id or ""
    return {
        "inline_keyboard": [
            [
                {"text": "📋 Kartu sinyal", "callback_data": f"sig:card:{sid}"},
                {"text": "📊 Chart", "callback_data": f"sig:chart:{sid}"},
            ],
            [
                {"text": "🧠 Reasoning", "callback_data": f"sig:reason:{sid}"},
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


# =============================================================================
# §12.1  UI KIT — single source of truth. Semua view WAJIB lewat helper ini.
# =============================================================================
_SEP_W = 24
_SEP_LINE = "─" * _SEP_W
_SEP_HEAVY = "━" * _SEP_W


def _ui_sep() -> str:
    return _SEP_LINE


def _ui_header(icon: str, title: str, subtitle: str | None = None) -> str:
    head = f"{icon} {title}"
    if subtitle:
        head += f"\n{subtitle}"
    return f"{head}\n{_SEP_HEAVY}"


def _ui_kv(label: str, value: str, width: int = 10) -> str:  # width: deprecated, tidak dipakai
    """Baris 'label: value'. Telegram plain text memakai font proporsional,
    jadi padding spasi tidak sejajar; pemisah eksplisit dipakai. `width` dipertahankan untuk kompatibilitas."""
    if not label:
        return value
    return f"{label}: {value}"


def _ui_section(title: str) -> str:
    return f"{title}\n{_SEP_LINE}"


def _ui_tier_badge(tier: Any) -> str:
    t = str(tier or "").upper()
    return f"T{t}" if t in ("A", "B", "C") else "—"


def _ui_state_badge(state: Any) -> str:
    st = str(state or "PENDING").upper()
    if st == "ARMED":
        return "🔒 ARMED"
    if st == "PENDING":
        return "⏳ PENDING"
    return f"· {st}"


def _ui_conf_pct(confidence: Any) -> str:
    """Terima 0–1 ATAU 0–100. Cegah output '7500%' dari row legacy."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return "—"
    if c <= 0:
        return "—"
    if c <= 1.0:
        return f"{c * 100:.0f}%"
    if c <= 100.0:
        return f"{c:.0f}%"
    return "—"


# =============================================================================
# §12.2  SIGNAL CARD — satu renderer, dua jalur (live push + DB reload)
# =============================================================================


def _hist_wr_label(memory, symbol: str, setup: str | None = None) -> str | None:
    """Setup WR 30d jika n≥5. Bukan probabilitas sinyal ini."""
    if memory is None:
        return None
    try:
        w, p, l = memory.outcome_counts(setup_type=setup, lookback_days=30)
        n = w + p + l
        if n < MIN_STAT_SAMPLE:
            return None
        wr = (w + 0.5 * p) / n
        return f"{wr:.0%} (n={n})"
    except Exception:
        return None



def _worst_fill_rr(direction: str, entry_lo: float, entry_hi: float, stop: float, tp2: float) -> float | None:
    """RR at worst fill (edge of zone). None if invalid."""
    if tp2 <= 0 or stop <= 0 or entry_lo <= 0 or entry_hi <= 0:
        return None
    d = (direction or "").upper()
    if d == "LONG":
        fill = entry_hi
        risk = fill - stop
        reward = tp2 - fill
    elif d == "SHORT":
        fill = entry_lo
        risk = stop - fill
        reward = fill - tp2
    else:
        return None
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _rr_to_target(
    direction: str, entry_low: float, entry_high: float, stop: float, target: float,
) -> float | None:
    """RR ke satu target dari titik entry mid. None jika geometri tidak valid
    (target tidak di sisi profit, atau risk nol). Fail-soft."""
    if min(entry_low, entry_high, stop, target) <= 0:
        return None
    d = (direction or "").upper()
    mid = (entry_low + entry_high) / 2.0
    risk = abs(mid - stop)
    if risk <= 0:
        return None
    if d == "LONG" and target <= mid:
        return None
    if d == "SHORT" and target >= mid:
        return None
    if d not in ("LONG", "SHORT"):
        return None
    return abs(target - mid) / risk


_CAL_SAMPLE_RE = re.compile(r"\b(setup|sym) n=(\d+) wr=([\d.]+)")


def _annotate_low_sample(text: str) -> str:
    """Tandai komponen kalibrasi dengan sampel kecil. Perhitungan engine tidak
    diubah (sudah di-shrink); ini hanya label tampilan agar n=1 tidak terbaca
    sebagai bukti."""
    def _sub(m: "re.Match[str]") -> str:
        n = int(m.group(2))
        if n >= MIN_STAT_SAMPLE:
            return m.group(0)
        return f"{m.group(1)} n={n} wr={m.group(3)} (sampel kecil, bobot rendah)"
    return _CAL_SAMPLE_RE.sub(_sub, text or "")


def _rr_summary(direction, entry_low, entry_high, stop, tp1, tp2, rr) -> str:
    """Tampilkan RR per target (TP1 dan TP2) supaya tidak terbaca sebagai RR ke TP1.
    rr = nilai engine (TP2, mid). RR TP1 dihitung dari geometri kartu."""
    rr_tp1 = _rr_to_target(direction, entry_low, entry_high, stop, tp1)
    if rr_tp1 is None:
        return f"1:{rr:.2f} ke TP2 (mid)"
    return f"TP1 1:{rr_tp1:.2f} · TP2 1:{rr:.2f} (mid)"


_TRIGGER_LABEL: dict[str, str] = {
    "momentum_trend": "momentum tren",
}


def _ui_tier_label(tier: Any) -> str:
    """Label tier di kartu: A = prioritas eksekusi; B = konteks pasar; C = watch."""
    t = str(tier or "").upper()
    if t == "A":
        return "Tier A · prioritas"
    if t == "B":
        return "Tier B · konteks"
    if t == "C":
        return "Tier C · watch"
    return "Tier —"


def _confirm_bar(got: int, total: int) -> str:
    """Bar konfirmasi ▰▱ (hanya untuk total kecil yang muat di layar HP)."""
    if total <= 0 or total > 8:
        return ""
    filled = min(max(int(got), 0), total)
    return "▰" * filled + "▱" * (total - filled)


def _render_signal_card(
    *,
    symbol: str,
    direction: str,
    horizon: str,
    tier: str,
    setup_type: str,
    trigger_type: str = "",
    state: str = "PENDING",
    entry_low: float,
    entry_high: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    rr: float,
    confidence: float,
    sl_basis: str = "atr",
    entry_basis: str = "atr",
    confirm_count: int | None = None,
    active_agent_count: int | None = None,
    real_count: int | None = None,
    context_mult: float | None = None,
    confluence: int | None = None,
    hist_wr: str | None = None,
    original_stop_for_rr: float | None = None,
    include_footer: bool = True,
) -> str:
    dir_u = (direction or "").upper()
    arrow = "🟢" if dir_u == "LONG" else ("🔴" if dir_u == "SHORT" else "⚪")
    sl_label = _sl_basis_label(sl_basis, dir_u)
    en_label = _ENTRY_BASIS_LABEL.get(str(entry_basis), str(entry_basis or "—"))
    setup_h = (setup_type or "—").replace("_", " ").lower()
    trig_h = _TRIGGER_LABEL.get(str(trigger_type or ""), str(trigger_type or "").replace("_", " "))
    tier_u = str(tier or "").upper()

    # Blok 1 — harga: yang dicek trader pertama kali.
    lines: list[str] = [
        f"{arrow} {symbol}  {dir_u}  ·  {_ui_tier_label(tier)}  ·  {horizon}",
        "",
        f"🎯 Entry {_ui_px(entry_low)} – {_ui_px(entry_high)}",
        f"🛑 SL {_ui_px(stop_loss)} · {sl_label}",
        f"🏁 TP1 {_ui_px(tp1)}  ·  TP2 {_ui_px(tp2)}",
        "",
    ]
    # Blok 2 — kualitas setup dan keyakinan.
    worst = _worst_fill_rr(
        dir_u, entry_low, entry_high,
        original_stop_for_rr if original_stop_for_rr else stop_loss, tp2,
    )
    lines.append(f"⚖️ R:R {_rr_summary(dir_u, entry_low, entry_high, stop_loss, tp1, tp2, rr)}")
    if worst is not None:
        lines.append(f"🧯 R:R terburuk (entry di ujung): 1:{worst:.2f}")
    lines.append(f"✨ Keyakinan {_ui_conf_pct(confidence)}  ·  {_ui_state_badge(state)}")
    if hist_wr:
        lines.append(f"📊 Winrate historis: {hist_wr}")
    if tier_u == "B":
        lines.append("📡 Konteks pasar — pantau arah; bukan prioritas size penuh")
    elif tier_u == "C":
        lines.append("👀 Watch only — konfirmasi/RR belum tembus B")

    # Blok 3 — konteks eksekusi.
    lines += ["", _ui_sep(), f"📐 Setup: {setup_h} · entry {en_label}"]
    if confirm_count is not None and active_agent_count is not None:
        got = int(confirm_count)
        total = int(active_agent_count)
        real_s = f" (real {int(real_count)})" if real_count is not None else ""
        bar = _confirm_bar(got, total)
        bar_part = f"{bar} " if bar else ""
        lines.append(f"🔍 Konfirmasi {bar_part}{got}/{total}{real_s}")
    if trig_h:
        lines.append(f"⚡ Pemicu: {trig_h}")
    extras: list[str] = []
    if context_mult is not None:
        try:
            ctx = float(context_mult)
            if abs(ctx - 1.0) >= 0.01:
                extras.append(f"🌊 Konteks pasar ×{ctx:.2f}")
        except (TypeError, ValueError):
            pass
    if confluence is not None:
        try:
            if int(confluence) > 0:
                extras.append(f"🧩 Confluence {int(confluence)}/5")
        except (TypeError, ValueError):
            pass
    if extras:
        lines.append("  ·  ".join(extras))

    build_s = globals().get("CRYPTONE_BUILD", "")
    if include_footer and build_s:
        lines += ["", f"build {build_s}"]
    return "\n".join(lines)


def format_signal_message(s: "PipelineSignal", memory=None) -> str:
    hist = _hist_wr_label(memory, s.symbol, s.setup_type)
    return _render_signal_card(
        symbol=s.symbol,
        direction=s.direction,
        horizon=s.horizon,
        tier=s.tier,
        setup_type=s.setup_type,
        trigger_type=s.trigger_type,
        state=getattr(s, "state", "PENDING") or "PENDING",
        entry_low=s.entry_low,
        entry_high=s.entry_high,
        stop_loss=s.stop_loss,
        tp1=s.tp1,
        tp2=s.tp2,
        rr=s.rr,
        confidence=s.confidence,
        sl_basis=getattr(s, "sl_basis", "atr") or "atr",
        entry_basis=getattr(s, "entry_basis", "atr") or "atr",
        confirm_count=s.confirm_count,
        active_agent_count=s.active_agent_count,
        real_count=getattr(s, "real_count", 0),
        context_mult=getattr(s, "context_mult", 1.0),
        confluence=getattr(s, "confluence", 0),
        hist_wr=hist,
    )


_REASONING_CONFIRM_RE = re.compile(r"confirm\s+(\d+)/(\d+)\s+\(real\s+(\d+)\)")
_REASONING_CTX_RE = re.compile(r"ctx[×x]\s*([\d.]+)")
_REASONING_CONFLUENCE_RE = re.compile(r"confu\s+(\d+)/5")
_REASONING_SL_RE = re.compile(r"\bsl=([A-Za-z0-9_]+)")
_REASONING_ENTRY_RE = re.compile(r"\bentry=([A-Za-z0-9_]+)")


def _parse_reasoning_meta(reasoning: str) -> dict[str, Any]:
    """Best-effort parse metrik opsional dari string reasoning."""
    out: dict[str, Any] = {}
    if not reasoning:
        return out
    m = _REASONING_CONFIRM_RE.search(reasoning)
    if m:
        try:
            out["confirm_count"] = int(m.group(1))
            out["active_agent_count"] = int(m.group(2))
            out["real_count"] = int(m.group(3))
        except (ValueError, TypeError):
            pass
    m = _REASONING_CTX_RE.search(reasoning)
    if m:
        try:
            out["context_mult"] = float(m.group(1))
        except (ValueError, TypeError):
            pass
    m = _REASONING_CONFLUENCE_RE.search(reasoning)
    if m:
        try:
            out["confluence"] = int(m.group(1))
        except (ValueError, TypeError):
            pass
    m = _REASONING_SL_RE.search(reasoning)
    if m:
        out["sl_basis_parsed"] = m.group(1)
    m = _REASONING_ENTRY_RE.search(reasoning)
    if m:
        out["entry_basis_parsed"] = m.group(1)
    return out


def _fmt_signal_card_from_db(memory: "MemoryEngine", signal_id: str) -> str:
    if not memory._conn or not signal_id:
        return "Signal tidak ditemukan."
    row = memory._conn.execute(
        "SELECT symbol, direction, horizon, tier, setup_type, trigger_type, "
        "entry_zone_low, entry_zone_high, stop_loss, original_stop, tp1, tp2, rr, confidence, "
        "state, reasoning, sl_basis, entry_basis "
        "FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return f"Signal {signal_id[:8]}… tidak ditemukan / sudah expired."

    keys = row.keys() if hasattr(row, "keys") else []
    sl_b = (row["sl_basis"] if "sl_basis" in keys else None) or None
    en_b = (row["entry_basis"] if "entry_basis" in keys else None) or None
    meta = _parse_reasoning_meta(row["reasoning"] or "")
    if not sl_b:
        sl_b = meta.get("sl_basis_parsed", "atr")
    if not en_b:
        en_b = meta.get("entry_basis_parsed", "atr")

    return _render_signal_card(
        symbol=str(row["symbol"]),
        direction=str(row["direction"] or ""),
        horizon=str(row["horizon"] or ""),
        tier=str(row["tier"] or "?"),
        setup_type=str(row["setup_type"] or ""),
        trigger_type=str(row["trigger_type"] or ""),
        state=str(row["state"] or "PENDING"),
        entry_low=float(row["entry_zone_low"] or 0),
        entry_high=float(row["entry_zone_high"] or 0),
        stop_loss=float(row["stop_loss"] or 0),
        tp1=float(row["tp1"] or 0),
        tp2=float(row["tp2"] or 0),
        rr=float(row["rr"] or 0),
        confidence=float(row["confidence"] or 0),
        sl_basis=str(sl_b or "atr"),
        entry_basis=str(en_b or "atr"),
        confirm_count=meta.get("confirm_count"),
        active_agent_count=meta.get("active_agent_count"),
        real_count=meta.get("real_count"),
        context_mult=meta.get("context_mult"),
        confluence=meta.get("confluence"),
        hist_wr=_hist_wr_label(memory, str(row["symbol"]), str(row["setup_type"] or "")),
        original_stop_for_rr=float(row["original_stop"] or 0) or None,
        include_footer=True,
    )


def _radar_rows_safe(memory) -> list[dict]:
    """Baris radar aktif untuk menu/help. Fail-soft: error → list kosong."""
    if memory is None:
        return []
    try:
        return list(memory.get_active_signals() or [])
    except Exception:
        return []


def _radar_headline(rows: list[dict]) -> str:
    """Satu kalimat status radar. Prioritas: ARMED (butuh perhatian) > PENDING."""
    if not rows:
        return "Tenang, belum ada setup aktif"
    n_arm = sum(1 for r in rows if str(r.get("state") or "").upper() == "ARMED")
    if n_arm:
        return f"{n_arm} ARMED · siap entry"
    return f"{len(rows)} setup · menunggu konfirmasi"


def format_main_menu_text(memory=None) -> str:
    """Menu = status radar + sesi pasar adaptif. Tanpa ulang quiet B/C / ketuk tombol."""
    rows = _radar_rows_safe(memory)
    lines = [
        "🤖 Cryptone V4.5",
        "Market Radar + Trader Assistant",
        "",
        f"▸ {_radar_headline(rows)}",
        f"▸ {_session_desk_line(memory=memory)}",
    ]
    if rows:
        lines.append(f"· {len(rows)} setup di Radar")
    try:
        if memory is not None:
            _rows = _latest_funding_rows(memory, 6.0) or []
            if _rows:
                _pct8 = [float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0 for r in _rows]
                _nl, _ns, _, _ = _ramai_split(_pct8)
                if _nl + _ns >= SQUEEZE_MIN_RAMAI:
                    if _ns > _nl * 1.5:
                        lines.append("· Crowd short padat di koin ramai")
                    elif _nl > _ns * 1.5:
                        lines.append("· Crowd long padat di koin ramai")
    except Exception:  # noqa: BLE001
        pass
    lines += [
        "",
        f"· build {CRYPTONE_BUILD}",
    ]
    return "\n".join(lines)



# Panduan dipecah per kategori supaya tiap layar pendek dan muat di HP.
_HELP_SECTIONS: dict[str, tuple[str, list[tuple[str, str]], str]] = {
    "pantau": (
        "📡 Pantau",
        [
            ("📊 Radar", "Setup yang dipantau. ARMED siap entry, PENDING menunggu konfirmasi."),
            ("🌍 Market", "Snapshot pasar dan bias hari ini."),
            ("🚨 Black Swan", "Deteksi kondisi pasar ekstrem."),
            ("📈 Breadth", "Funding dan seberapa ramai posisi."),
        ],
        "Sinyal push datang otomatis. Tiap sinyal punya 🧠 Reasoning dan 📊 Chart.",
    ),
    "catatan": (
        "📒 Catatan",
        [
            ("📓 Journal", "Posisi terbuka dan close terakhir."),
            ("📊 Performance", "Ringkasan hasil trading."),
            ("🎯 Accuracy", "Expectancy dan win rate."),
            ("🧬 Shadows", "Laporan shadow dan gate uji."),
        ],
        "",
    ),
    "alat": (
        "🛠 Alat",
        [
            ("🔍 Explorer", "Jelajahi coin dengan chart."),
            ("⚙️ Settings", "Mode dan pengaturan bot."),
            ("🧮 calc RISK ENTRY SL", "Hitung ukuran posisi. Contoh: calc 50 2.85 2.90"),
            ("/menu", "Buka menu utama kapan saja."),
        ],
        "",
    ),
}


def format_help_text() -> str:
    """Halaman utama panduan: cuma pengantar dan pilihan kategori."""
    return "\n".join([
        "❓ Panduan",
        "Menu itu status cepat. Panduan ini menjelaskan tiap tombol.",
        "",
        "Pilih topik di bawah.",
        "",
        f"· build {CRYPTONE_BUILD}",
    ])


def format_help_section(key: str) -> str:
    title, items, footer = _HELP_SECTIONS.get(key, _HELP_SECTIONS["pantau"])
    lines = [title, ""]
    for head, desc in items:
        lines += [f"▸ {head}", desc, ""]
    if footer:
        lines += [footer, ""]
    lines.append(f"· build {CRYPTONE_BUILD}")
    return "\n".join(lines)


async def _fmt_radar(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    sess = _session_status_line()
    if not rows:
        return "\n".join([
            _ui_header("📊", "Radar"),
            _ui_kv("Session", sess),
            "",
            "Tidak ada sinyal aktif.",
            "Push otomatis saat setup lolos gate.",
        ])

    mids_fresh = await _refresh_mids_if_stale(memory)
    mids_for_tag = _LAST_MIDS if mids_fresh else {}

    order = {"A": 0, "B": 1, "C": 2}
    state_order = {"ARMED": 0, "PENDING": 1}
    # ARMED dulu (butuh perhatian), baru tier
    rows = sorted(rows, key=lambda r: (
        state_order.get(str(r.get("state") or "").upper(), 9),
        order.get(str(r.get("tier") or "C"), 9),
    ))
    n_arm = sum(1 for r in rows if str(r.get("state") or "").upper() == "ARMED")
    n_pend = sum(1 for r in rows if str(r.get("state") or "").upper() == "PENDING")

    lines = [
        _ui_header("📊", f"Radar · {len(rows)} aktif"),
        _ui_kv("Session", sess),
        _ui_kv("State", f"ARMED {n_arm}  ·  PENDING {n_pend}"),
        "",
    ]

    CAP = 10
    for r in rows[:CAP]:
        _dir = str(r.get("direction") or "").upper()
        arrow = "🟢" if _dir == "LONG" else ("🔴" if _dir == "SHORT" else "⚪")
        setup = str(r.get("setup_type") or "—").replace("_", " ")
        rr = float(r.get("rr") or 0)
        conf_s = ""
        try:
            raw_c = r.get("confidence")
            if raw_c is not None and float(raw_c) > 0:
                conf_s = f"  ·  {_ui_conf_pct(raw_c)}"
        except (TypeError, ValueError):
            pass
        age_s = ""
        created = _parse_ts(r.get("created_at"))
        if created is not None:
            h = (now_utc() - created).total_seconds() / 3600.0
            age_s = f"  ·  {h:.1f}h" if h < 48 else f"  ·  {h / 24:.1f}d"
        tp1_flag = "  ✅TP1" if bool(r.get("tp1_hit")) else ""
        lines.append(
            f"{arrow} {r.get('symbol')} {_dir}  {_ui_tier_badge(r.get('tier'))}{tp1_flag}\n"
            f"   {_ui_state_badge(r.get('state'))}  ·  {setup}\n"
            f"   R:R 1:{rr:.2f}{conf_s}{age_s}\n"
            f"   {_ui_px(r.get('entry_zone_low'))} – {_ui_px(r.get('entry_zone_high'))}{_zone_tag(r, mids_for_tag)}"
        )
        lines.append("")

    if len(rows) > CAP:
        lines.append(f"… +{len(rows) - CAP} lagi · buka 📓 Journal")
    return "\n".join(lines).rstrip()



_MIDS_TTL_SEC = 20.0
_LAST_MIDS_AT: float = 0.0  # time.monotonic() saat _LAST_MIDS terakhir diisi


async def _refresh_mids_if_stale(memory: "MemoryEngine") -> bool:
    """Pastikan _LAST_MIDS segar (<= _MIDS_TTL_SEC). True kalau segar setelah call ini.
    Fail-soft: kalau fetch gagal/timeout, return False dan caller tidak menampilkan px."""
    global _LAST_MIDS_AT
    if _LAST_MIDS and (time.monotonic() - _LAST_MIDS_AT) < _MIDS_TTL_SEC:
        return True
    try:
        mids = await asyncio.wait_for(_engine.hl_all_mids(memory), timeout=4.0)
    except Exception as e:
        logger.debug("radar mids refresh fail-soft: %s", type(e).__name__)
        return False
    if not mids:
        return False
    _LAST_MIDS.clear()
    _LAST_MIDS.update(mids)
    _LAST_MIDS_AT = time.monotonic()
    return True


def _zone_tag(row: dict, mids: dict) -> str:
    """px vs entry zone — glanceable actionability."""
    try:
        sym = str(row.get("symbol") or "")
        px = float(mids.get(sym) or 0)
        lo = float(row.get("entry_zone_low") or 0)
        hi = float(row.get("entry_zone_high") or 0)
        if px <= 0 or lo <= 0:
            return ""
        if lo <= px <= hi:
            return f"  (px {_ui_px(px)} IN)"
        # posisi relatif, bukan arah harga
        return f"  (px {_ui_px(px)} wait)"
    except Exception:
        return ""


def _last_cycle_line(memory: "MemoryEngine") -> str:
    """Waktu siklus terakhir. Tanpa 'x menit lalu': snapshot bisa terbaca lama
    setelah terkirim, jadi selalu pakai jam absolut."""
    try:
        start = _parse_ts(memory.get_runtime("last_cycle_start_ts") or "")
        end = _parse_ts(memory.get_runtime("last_cycle_end_ts") or "")
        if start is None and end is None:
            return "belum ada"
        running = start is not None and (end is None or start > end)
        if running:
            age = (now_utc() - start).total_seconds()
            if age >= 600:
                return f"belum selesai sejak {_as_wib(start).strftime('%H:%M')} WIB (kemungkinan error)"
            return f"sedang berjalan sejak {_as_wib(start).strftime('%H:%M')} WIB"
        age = (now_utc() - end).total_seconds()
        wib = _as_wib(end).strftime("%H:%M")
        if age < 600:
            return f"{wib} WIB"
        return f"{wib} WIB (sudah lama)"
    except Exception:
        return "belum ada"


def _cascade_parts(regime: str, current: str) -> tuple[str, str | None]:
    """(saran dalam bahasa trader, catatan mode bot jika berbeda dari saran). Informasi, bukan perintah."""
    rec = {
        "trending_up_healthy": ("continuation", "ikuti arah tekanan (trend bersih)"),
        "trending_down_healthy": ("continuation", "ikuti arah tekanan (trend bersih)"),
        "trending_up_crowded": ("exhaustion", "waspada climax (long padat)"),
        "trending_down_crowded": ("exhaustion", "waspada climax (short padat)"),
        "ranging": ("exhaustion", "lebih cocok mean-revert"),
        "volatile": ("continuation", "ikuti tekanan, ukuran kecil"),
        "mixed": ("continuation", "default aman"),
    }.get(regime, ("continuation", "default aman"))
    rec_mode, advice = rec
    note = f"bot di mode {current}" if current and current != rec_mode else None
    return advice, note


def _regime_label(raw: str) -> str:
    """Kode internal regime → bahasa trader."""
    m = {
        "trending_up_healthy": "tren naik",
        "trending_down_healthy": "tren turun",
        "trending_up_crowded": "tren naik, long padat",
        "trending_down_crowded": "tren turun, short padat",
        "volatile": "volatil",
        "ranging": "sideways",
        "mixed": "campuran",
    }
    return m.get(raw, raw.replace("_", " ").lower())


# Tangga emoji per level: satu tempat, dipakai semua kartu, supaya ikon selalu sinkron dengan label.
# Hanya di baris utama (bukan baris └), supaya kartu Telegram tetap rapi.
_FNG_LADDER: tuple[tuple[float, str, str], ...] = (
    (25.0, "😱", "takut ekstrem"),
    (40.0, "😟", "takut"),
    (55.0, "😐", "netral"),
    (75.0, "🤑", "serakah"),
    (float("inf"), "🚀", "serakah ekstrem"),
)
_VOL_ICON: dict[str, str] = {"TIPIS": "🪫", "MENURUN": "📉", "NORMAL": "📊", "TINGGI": "🌊"}
_MAG_ICON: dict[str, str] = {"FLAT": "💤", "TINGGI": "🌡️", "EKSTRIM": "🌋"}
_BIAS_ICON: dict[str, str] = {"BULLISH": "🟢", "BEARISH": "🔴", "NETRAL": "⚪", "NEUTRAL": "⚪"}
_SQUEEZE_ICON: dict[str, str] = {"LEMAH": "⚪", "SEDANG": "🟡", "KUAT": "🔴"}
_OI_ICON: dict[str, str] = {"naik": "📈", "turun": "📉", "datar": "➖"}
_REGIME_ICON: dict[str, str] = {
    "trending_up_healthy": "📈", "trending_down_healthy": "📉",
    "trending_up_crowded": "📈", "trending_down_crowded": "📉",
    "volatile": "⚡", "ranging": "↔️", "mixed": "🔀",
}


def _with_icon(icon: str | None, text: str) -> str:
    return f"{icon} {text}" if icon else text


def _fng_parts(v: float | None) -> tuple[str, str]:
    """(emoji, label) Fear & Greed. Tanpa data → ('', 'belum ada data')."""
    if v is None:
        return "", "belum ada data"
    v = float(v)
    for upper, icon, label in _FNG_LADDER:
        if v <= upper:
            return icon, label
    return _FNG_LADDER[-1][1], _FNG_LADDER[-1][2]  # NaN-safe: tidak pernah jatuh ke None


def _fng_label(v: float | None) -> str:
    if v is None:
        return "belum ada data"
    icon, label = _fng_parts(v)
    return f"{icon} {float(v):.0f} · {label}"


def _session_status_line(now: datetime | None = None) -> str:
    """Status sesi bot (quiet / likuid / luar). Dipakai Snapshot & Settings."""
    t = _as_wib(now or now_utc())
    h = t.hour
    in_sess = SESSION_START_HOUR_WIB <= h < SESSION_END_HOUR_WIB
    if in_quiet_hours(t):
        return (f"🌙 Sesi tenang · {h:02d}:00 WIB — sinyal B/C ditahan "
                f"sampai {QUIET_END_HOUR_WIB:02d}:00")
    if in_sess:
        return f"🟢 Sesi aktif · {h:02d}:00 WIB (sampai {SESSION_END_HOUR_WIB:02d}:00)"
    return f"🔴 Di luar sesi · {h:02d}:00 WIB — hanya Tier A"


def _session_name(now: datetime | None = None) -> str:
    """Nama jendela sesi dari jam WIB (fakta jam, bukan penilaian)."""
    t = _as_wib(now or now_utc())
    h = t.hour + t.minute / 60.0
    if 7.0 <= h < 14.0:
        return "🌏 Asia"
    if 14.0 <= h < 18.0:
        return "🇪🇺 Eropa buka"
    if 18.0 <= h < 19.0:
        return "🇪🇺→🇺🇸 Menjelang US"
    if 19.0 <= h < 23.0:
        return "🔥 Overlap Eropa–US"
    if 23.0 <= h or h < 2.0:
        return "🇺🇸 US lanjut"
    return "🌙 Malam US / pra-Asia"


_FLOW_WORD: dict[str, str] = {"RAMAI": "ramai", "NORMAL": "normal", "SEPI": "sepi"}


def _session_desk_line(now: datetime | None = None, memory=None) -> str:
    """Nama sesi (dari jam) + keterangan volume LIVE: volume 1 jam terakhir dibanding jam yang
    sama 7 hari terakhir (persentil, lihat universe_flow_regime). Tanpa data → jujur, tidak
    menebak dari jam."""
    name = _session_name(now)
    flow = universe_flow_regime(memory, now) if memory is not None else {"label": "belum ada data"}
    lbl = str(flow.get("label") or "")
    if lbl in _FLOW_WORD:
        return (f"{name} · volume {_FLOW_WORD[lbl]} vs jam sama 7 hari "
                f"(persentil {flow['rank']:.0f})")
    return f"{name} · volume {lbl}"



_FUNDING_ROWS_CACHE: dict[float, tuple[float, list]] = {}


def _latest_funding_rows(memory: "MemoryEngine", hours: float = 6.0) -> list:
    """Latest funding per symbol. Cached 60s — Market/Breadth/brief share."""
    if not memory._conn:
        return []
    now = time.time()
    hit = _FUNDING_ROWS_CACHE.get(hours)
    if hit and now - hit[0] < 60.0:
        return hit[1]
    try:
        cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
        rows = list(memory._conn.execute(
            """
            SELECT f.symbol, f.rate, f.recorded_at
            FROM metric_funding f
            INNER JOIN (
                SELECT symbol, MAX(recorded_at) AS m
                FROM metric_funding
                WHERE recorded_at >= ?
                GROUP BY symbol
            ) g ON f.symbol = g.symbol AND f.recorded_at = g.m
            """,
            (cutoff,),
        ).fetchall())
    except Exception:
        return []
    _FUNDING_ROWS_CACHE[hours] = (now, rows)
    return rows


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _liq_dominant_side(s: dict | None) -> str | None:
    """'LONG' bila long dominan terlikuidasi (≥65%), 'SHORT' bila short dominan (≤35%),
    'MIX' bila keduanya terkena, None bila tidak ada data."""
    if not s:
        return None
    tot = float(s["long_usd"]) + float(s["short_usd"])
    if tot <= 0:
        return None
    share = float(s["long_usd"]) / tot * 100.0
    if share >= 65:
        return "LONG"
    if share <= 35:
        return "SHORT"
    return "MIX"


def _liq_lines(label: str, s: dict | None, side: str | None) -> list[str]:
    if s is None:
        return [f"• {label}: data belum tersedia"]
    if side is None:
        return [f"• {label}: belum ada likuidasi tercatat"]
    if side == "LONG":
        return [f"• {label}: long dominan terlikuidasi", "  └ posisi long dipaksa keluar"]
    if side == "SHORT":
        return [f"• {label}: short dominan terlikuidasi", "  └ posisi short dipaksa keluar"]
    return [f"• {label}: kedua sisi terkena", "  └ belum ada dominasi"]


def _median_mag(rates_hourly: list[float]) -> tuple[str, str]:
    """(label besar funding, sub-baris) dari MEDIAN, bukan rata-rata: satu koin ekstrem tidak
    boleh membalik label arah pasar. Rata-rata tetap disebut di baris outlier (brief)."""
    if not rates_hourly:
        return "belum ada data", ""
    med_h = _median(rates_hourly)
    head = _mag_parts(funding_snapshot_summary([med_h]))[0]
    return head, f"median {med_h * FUNDING_PERIODS_PER_8H * 100:+.4f}% per 8 jam"


CROWD_ADAPTIVE_PCTL = 90.0   # ambang naik hanya saat >10% universe sudah di atas lantai
CROWD_MIN_UNIVERSE = 10      # di bawah jumlah ini persentil tidak bermakna → pakai lantai


def _crowd_thr_pct8(rates_pct8h: list[float]) -> float:
    """Ambang koin RAMAI (% per 8 jam, |funding|), dari data universe hari ini.

    Lantai = FUNDING_CROWD_8H (0.03%/8h, tidak pernah lebih longgar dari sebelumnya).
    Saat pasar panas dan >10% koin sudah di atas lantai, ambang naik ke persentil ke-90
    |funding| universe, supaya 'ramai' tetap selektif dan tidak membanjiri snapshot.
    Mengembalikan lantai bila data < CROWD_MIN_UNIVERSE."""
    floor = FUNDING_CROWD_8H * 100.0
    mags = sorted(abs(v) for v in rates_pct8h)
    if len(mags) < CROWD_MIN_UNIVERSE:
        return floor
    idx = min(len(mags) - 1, max(0, math.ceil(CROWD_ADAPTIVE_PCTL / 100.0 * len(mags)) - 1))
    return max(floor, mags[idx])


def _ramai_split(rates_pct8h: list[float]) -> tuple[int, int, int, float]:
    """(long ramai, short ramai, netral, persen long di antara koin ramai).
    Ambang adaptif via _crowd_thr_pct8 (lantai FUNDING_CROWD_8H). Tanpa koin ramai → 50.0 (netral)."""
    thr = _crowd_thr_pct8(rates_pct8h)
    nl = sum(1 for v in rates_pct8h if v >= thr)
    ns = sum(1 for v in rates_pct8h if v <= -thr)
    nn = len(rates_pct8h) - nl - ns
    share = nl / (nl + ns) * 100.0 if (nl + ns) > 0 else 50.0
    return nl, ns, nn, share


def _market_read(memory: "MemoryEngine", *, view: str = "snapshot") -> list[str]:
    """Interpretasi pasar. view='snapshot' = cockpit keputusan; view='breadth' = scanner kerumunan.

    Snapshot: fokus bias, volume/F&G, OI, skor/kesimpulan — tanpa blok Posisi (anti-duplikasi).
    Breadth: posisi + outlier funding — detail koin & likuidasi di blok terpisah.
    """
    view = (view or "snapshot").lower().strip()
    if view not in ("snapshot", "breadth"):
        view = "snapshot"

    pairs: list[tuple[str, float]] = []
    for r in _latest_funding_rows(memory, 6.0) or []:
        try:
            pairs.append((str(r["symbol"]), float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0))
        except (TypeError, ValueError, KeyError):
            continue
    if not pairs:
        if view == "breadth":
            return ["▸ Baca cepat", "• Belum ada data funding 6 jam"]
        return ["▸ 🧭 Baca silang", "• Belum ada data funding 6 jam", "  └ ringkasan belum bisa dibuat"]

    vals = [v for _, v in pairs]
    n = len(vals)
    med = _median(vals)
    mean = sum(vals) / n
    med_flat = funding_snapshot_summary(
        [_median(vals) / (FUNDING_PERIODS_PER_8H * 100.0)]
    )["mag"] == "FLAT"
    # Satu definisi crowd untuk seluruh snapshot: dari koin RAMAI (≥ FUNDING_CROWD_8H),
    # bukan dari tanda funding (baseline +0.01% bukan kerumunan). Posisi, sentimen, dan skor
    # squeeze semuanya membaca `crowd` ini — tidak ada dua definisi yang bisa bertentangan.
    rn_l, rn_s, _rn_n, ramai_share = _ramai_split(vals)
    ramai_ok = (rn_l + rn_s) >= SQUEEZE_MIN_RAMAI
    crowd = None
    if ramai_ok and ramai_share >= 70.0:
        crowd = "LONG"
    elif ramai_ok and ramai_share <= 30.0:
        crowd = "SHORT"
    outlier_conflict = med != 0 and mean != 0 and (med > 0) != (mean > 0)
    outlier_sym, outlier_val = max(pairs, key=lambda p: abs(p[1]))

    # --- shared: posisi ringkas ---
    posisi: list[str] = ["▸ 📍 Posisi"]
    if crowd == "LONG":
        posisi += [f"• Dari {rn_l + rn_s} koin ramai: {rn_l} long · {rn_s} short",
                   "  └ long dominan · rawan long squeeze jika harga turun"]
    elif crowd == "SHORT":
        posisi += [f"• Dari {rn_l + rn_s} koin ramai: {rn_l} long · {rn_s} short",
                   "  └ short dominan · rawan short squeeze jika harga naik"]
    elif rn_l + rn_s > 0:
        posisi += [
            f"• Koin ramai: {rn_l} long · {rn_s} short",
            "  └ belum cukup untuk squeeze, pantau",
        ]
    else:
        posisi += ["• Posisi relatif seimbang", "  └ tidak ada koin ramai di kedua sisi"]

    # Outlier hanya di Breadth (scanner kerumunan) — Snapshot cukup tahu ada conflict di kesimpulan
    if view == "breadth" and (outlier_conflict or abs(outlier_val) >= 0.5):
        posisi += [
            f"• Outlier: {outlier_sym} {outlier_val:+.2f}% per 8 jam",
            f"  └ tarik rata-rata dari median {_pct_signed(med, 4)} ke {_pct_signed(mean, 4)}",
            "     peringkat top bisa bias",
        ]

    if view == "breadth":
        # Scanner: posisi + baca cepat. Daftar koin ada di blok Breadth, jadi tidak diulang di sini.
        top_pct = max(v for _, v in pairs)
        bottom_pct = min(v for _, v in pairs)
        extreme = FUNDING_BAND_EXTREME_8H * 100.0
        baca: list[str] = ["▸ ⚡ Baca cepat"]
        if top_pct >= extreme or bottom_pct <= -extreme:
            baca += ["• Squeeze rawan per koin, bukan market-wide",
                     "  └ pantau koin ekstrem di daftar Breadth"]
        if med_flat and crowd is not None:
            side_id = "long" if crowd == "LONG" else "short"
            baca += [f"• Koin ramai condong {side_id}, median datar",
                     "  └ kerumunan ada, tekanan funding belum ekstrem"]
        if len(baca) == 1:
            baca += ["• Tidak ada koin ekstrem funding",
                     "  └ andalkan struktur per-koin di Explorer"]
        spaced: list[str] = []
        for ln in posisi + baca:
            if ln.startswith("▸") and spaced:
                spaced.append("")
            spaced.append(ln)
        return spaced

    # --- Snapshot: Baca silang = insight turunan (bukan ulang makro/book/funding mentah) ---
    fokus: list[str] = ["▸ 🧭 Baca silang"]
    try:
        n_long, n_short = memory.count_active_by_direction()
    except Exception:  # noqa: BLE001
        n_long = n_short = 0
    if n_long >= n_short + 2:
        book_tilt = "LONG"
    elif n_short >= n_long + 2:
        book_tilt = "SHORT"
    elif (n_long + n_short) == 0:
        book_tilt = "EMPTY"
    else:
        book_tilt = "MIX"

    # Radar × crowd (crowd sudah dihitung di atas; None = belum padat)
    if book_tilt == "EMPTY":
        fokus.append("• Radar × crowd: book kosong · tunggu sinyal")
    elif crowd is None:
        fokus.append(
            f"• Radar × crowd: radar {book_tilt.lower()} · crowd belum padat"
        )
        fokus.append("  └ kerumunan funding belum membentuk sisi")
    elif book_tilt == "MIX":
        fokus.append(
            f"• Radar × crowd: book campur × crowd {crowd.lower()}"
        )
        fokus.append("  └ tidak ada bias book yang jelas")
    elif book_tilt == crowd:
        fokus.append(
            f"• Radar × crowd: SELARAS {crowd.lower()}"
        )
        fokus.append("  └ book dan koin ramai mengarah sama")
    else:
        fokus.append(
            f"• Radar × crowd: DIVERGEN · book {book_tilt.lower()} × crowd {crowd.lower()}"
        )
        fokus.append("  └ hati-hati size — dua sinyal beda arah")

    # Radar × berita (RSS) — turunan, bukan ulang baris Berita
    try:
        _bias_raw = (rss_news_bias() or "netral").strip().lower()
    except Exception:  # noqa: BLE001
        _bias_raw = "netral"
    if "bear" in _bias_raw:
        news_tilt = "SHORT"
    elif "bull" in _bias_raw:
        news_tilt = "LONG"
    else:
        news_tilt = None
    if book_tilt in ("LONG", "SHORT") and news_tilt:
        if book_tilt == news_tilt:
            fokus.append(f"• Radar × berita: selaras {book_tilt.lower()}")
        else:
            fokus.append(
                f"• Radar × berita: book {book_tilt.lower()} lawan berita {news_tilt.lower()}"
            )
    elif book_tilt == "EMPTY" and news_tilt:
        fokus.append(f"• Berita condong {news_tilt.lower()} · book masih kosong")

    # Setup dominan di book (kalau ada) — belum tampil di Snapshot
    try:
        if memory._conn and (n_long + n_short) > 0:
            _sp = memory._conn.execute(
                "SELECT s.setup_type AS st, COUNT(*) AS n "
                "FROM active_signals a "
                "JOIN signal_history s ON s.signal_id = a.signal_id "
                "WHERE s.outcome IS NULL AND s.setup_type IS NOT NULL "
                "GROUP BY s.setup_type ORDER BY n DESC LIMIT 1"
            ).fetchone()
            if _sp and int(_sp["n"] or 0) > 0:
                _st = str(_sp["st"] or "").replace("_", " ").title()
                fokus.append(f"• Setup dominan di book: {_st} · n={int(_sp['n'])}")
    except Exception:  # noqa: BLE001
        pass

    # Sisa sesi likuid (bukan ulang header sesi)
    try:
        _nw = _as_wib(now_utc())
        _h = _nw.hour + _nw.minute / 60.0
        if SESSION_START_HOUR_WIB <= _nw.hour < SESSION_END_HOUR_WIB:
            _left = SESSION_END_HOUR_WIB - _h
            if _left <= 1.0:
                fokus.append(f"• Sisa sesi: {_left * 60:.0f} menit · mendekati quiet")
            else:
                fokus.append(f"• Sisa sesi: {_left:.1f} jam · sampai {SESSION_END_HOUR_WIB}:00 WIB")
        elif in_quiet_hours(_nw):
            fokus.append("• Di luar sesi (quiet) · push Tier B ditahan")
        else:
            fokus.append("• Di luar jam likuid · hanya Tier A / SWING")
    except Exception:  # noqa: BLE001
        pass

    out = list(fokus)

    try:
        now = now_utc()
        s6 = memory.get_liquidation_summary(now - timedelta(hours=6), top_n=0)
    except Exception:  # noqa: BLE001
        s6 = None
    liq6 = _liq_dominant_side(s6)
    # Satu baris konteks saja; angka lengkap di Breadth
    if liq6 == "LONG":
        out += ["▸ 💧 Likuidasi", "• 6 jam: long dominan keluar"]
    elif liq6 == "SHORT":
        out += ["▸ 💧 Likuidasi", "• 6 jam: short dominan keluar"]
    elif liq6 == "MIX":
        out += ["▸ 💧 Likuidasi", "• 6 jam: kedua sisi terkena"]
    elif s6 is not None:
        out += ["▸ 💧 Likuidasi", "• 6 jam: belum ada dominasi"]

    uvol = universe_volume_regime(memory) or {}
    vlabel = str(uvol.get("label") or "").upper()
    vtxt, vpct = _volume_parts(uvol)
    out.append("▸ 📊 Volume & sentimen")
    if vtxt == "belum ada data":
        out.append("• Volume belum ada data")
    else:
        out.append(f"• Volume {vtxt}")
        if vpct:
            out.append(f"  └ {vpct}")
        out.append({
            "TIPIS": "  └ breakout lemah, ukuran lebih kecil",
            "MENURUN": "  └ waspada breakout palsu",
            "TINGGI": "  └ partisipasi kuat, arah lebih terpercaya",
            "NORMAL": "  └ tidak ada tekanan volume",
        }.get(vlabel, "  └ volume belum bisa dinilai"))
    out += _holiday_volume_lines()
    fng = _current_fng(memory)
    if fng is None:
        out.append("• Fear & Greed belum tersedia")
    else:
        out.append(f"• Sentimen {_fng_label(fng)}")
        # Kalimat squeeze hanya untuk sisi yang benar-benar padat (crowd), bukan generik.
        if fng <= 25:
            if crowd == "SHORT":
                out.append("  └ rebound tajam bisa memicu short squeeze")
            elif crowd == "LONG":
                out.append("  └ lanjutan turun bisa memicu long squeeze")
            else:
                out.append("  └ tidak ada sisi padat; rebound belum punya pemicu squeeze")
        elif fng <= 40:
            out.append("  └ hati-hati mengejar short di harga bawah")
        elif fng < 60:
            out.append("  └ tidak ada dorongan emosi ekstrem")
        elif fng < 75:
            out.append("  └ hati-hati mengejar long di harga atas")
        else:
            if crowd == "LONG":
                out.append("  └ koreksi tajam bisa memicu long squeeze")
            elif crowd == "SHORT":
                out.append("  └ kenaikan tajam bisa memicu short squeeze")
            else:
                out.append("  └ tidak ada sisi padat; koreksi belum punya pemicu squeeze")

    anchor = memory.get_last_anchor() if hasattr(memory, "get_last_anchor") else None
    oi_info = None
    if anchor and anchor != "—":
        try:
            oi_info = anchor_oi_delta_pct(memory, str(anchor), lookback_hours=24.0)
        except Exception:
            oi_info = None
    if oi_info is not None:
        out.append("▸ 📦 Open interest")
        out.append(
            f"• {oi_info['symbol']} OI 24 jam "
            f"{_with_icon(_OI_ICON.get(str(oi_info['label'])), str(oi_info['label']))} "
            f"({oi_info['oi_delta_pct']:+.1f}%)"
        )
        if oi_info["label"] == "naik" and crowd == "LONG":
            out.append("  └ long numpuk + OI naik → tekanan squeeze lebih nyata")
        elif oi_info["label"] == "turun" and crowd == "LONG":
            out.append("  └ long padat tapi OI turun → posisi long sedang berkurang")
        elif oi_info["label"] == "naik" and crowd == "SHORT":
            out.append("  └ short padat + OI naik → rawan short squeeze")
        elif oi_info["label"] == "turun" and crowd == "SHORT":
            out.append("  └ short padat tapi OI turun → short sedang menutup")

    med_h = _median([v / (FUNDING_PERIODS_PER_8H * 100.0) for v in vals]) if vals else 0.0
    mag_m, _side_m = funding_magnitude_label(med_h)
    _rn_l, _rn_s, _rn_n, ramai_share = _ramai_split(vals)
    sq = squeeze_confidence_score(
        ramai_share if (_rn_l + _rn_s) >= SQUEEZE_MIN_RAMAI else 50.0,
        mag_m, vlabel, n_symbols=n,
    )

    out.append("▸ ✨ Kesimpulan")
    out.append(
        f"• Skor squeeze {sq['score']}/{sq['max']} · "
        f"{_with_icon(_SQUEEZE_ICON.get(str(sq['label']).upper()), str(sq['label']).lower())}"
    )
    if sq["thin_sample"]:
        out.append("  └ sample koin tipis — skor kurang andal")
    if sq["reasons"]:
        out.append("  └ " + ", ".join(sq["reasons"]))
    if sq["score"] <= 1:
        out.append("  └ lemah — jangan entry hanya karena breadth")
    if crowd == "LONG" and liq6 == "LONG":
        out += ["• Long padat & long terlikuidasi", "  └ unwinding long sedang berjalan",
                "  └ tunggu struktur membentuk dasar"]
    elif crowd == "SHORT" and liq6 == "SHORT":
        out += ["• Short padat & short terlikuidasi", "  └ squeeze naik sedang berjalan",
                "  └ tunggu struktur membentuk puncak"]
    elif crowd == "LONG":
        out += ["• Long padat, likuidasi belum dominan", "  └ waspada long squeeze",
                "  └ konteks squeeze, entry butuh struktur"]
    elif crowd == "SHORT":
        out += ["• Short padat, likuidasi belum dominan", "  └ waspada short squeeze",
                "  └ konteks squeeze, entry butuh struktur"]
    elif outlier_conflict:
        out += ["• Sinyal campur", "  └ median & rata-rata beda arah"]
    else:
        out += ["• Funding & likuidasi = konteks", "  └ bukan sinyal tunggal",
                "  └ bukan sinyal entry — tunggu struktur"]
    if outlier_conflict and crowd is not None:
        # konflik median vs rata-rata selalu dilaporkan, meski crowd terdeteksi
        out += ["• Sinyal campur", "  └ median & rata-rata beda arah"]

    if view == "snapshot":
        sector_block = sector_reading_lines(memory, pairs, crowd)
        if sector_block:
            out += ["• Sektor ramai"] + sector_block
    spaced = []
    for ln in out:
        if ln.startswith("▸") and spaced:
            spaced.append("")
        spaced.append(ln)
    return spaced



_MAG_ID: dict[str, str] = {"FLAT": "datar", "TINGGI": "tinggi", "EKSTRIM": "ekstrem"}
_SIDE_ID: dict[str, str] = {"LONG": "long", "SHORT": "short"}
_VOL_ID: dict[str, str] = {"NORMAL": "normal", "TIPIS": "tipis", "MENURUN": "menurun", "TINGGI": "tinggi"}
_BIAS_ID: dict[str, str] = {"BULLISH": "bullish", "BEARISH": "bearish", "NETRAL": "netral", "NEUTRAL": "netral"}


def _volume_parts(uvol: dict) -> tuple[str, str | None]:
    """(label volume, persentase terhadap baseline 7 hari)."""
    label = str(uvol.get("label") or "").upper()
    txt = _VOL_ID.get(label, label.lower() or "belum ada data")
    if label in _VOL_ICON:
        txt = _with_icon(_VOL_ICON[label], txt)
    ratio = uvol.get("ratio")
    if ratio is None:
        return txt, None
    return txt, f"{float(ratio) * 100:.0f}% dari rata-rata 7 hari"


def _bias_text(bias: str) -> str:
    key = str(bias or "").upper()
    return _with_icon(_BIAS_ICON.get(key), _BIAS_ID.get(key, str(bias or "netral").lower()))


def _mag_parts(summary: dict) -> tuple[str, str]:
    """(label besar funding, rata-rata per 8 jam). Per 8 jam ditulis lengkap, bukan '/8h'."""
    mag = str(summary["mag"]).upper()
    side = str(summary["side"]).upper()
    head = _MAG_ID.get(mag, mag.lower())
    if side in _SIDE_ID:
        head += f", {_SIDE_ID[side]}"
    head = _with_icon(_MAG_ICON.get(mag), head)
    return head, f"rata-rata {summary['avg_8h_pct']:+.4f}% per 8 jam"


def _mag_text(summary: dict) -> str:
    head, avg = _mag_parts(summary)
    return f"{head} · {avg}"


def _universe_vol_line(uvol: dict) -> str:
    """Satu baris ringkas untuk regime volume universe."""
    label = str(uvol.get("label") or "—")
    ratio = uvol.get("ratio")
    if ratio is None:
        return label
    return f"{label} · {ratio * 100:.0f}% baseline 7 hari"


# Sukses → refresh tiap cycle (0 detik = tidak ada jeda tambahan; cycle sendiri 5 menit).
# Prompt identik di-cache LLM (cache_ttl_h=1), jadi cycle dengan angka sama tidak memanggil API.
# Kuota Gemini ketat? CRYPTONE_BRIEF_INTERVAL_SEC=900 (misal) untuk melonggarkan.
BRIEF_OK_INTERVAL_SEC = float(_env_int("CRYPTONE_BRIEF_INTERVAL_SEC", 0, 0, 3600))
BRIEF_BACKOFF_BASE_SEC = 300.0     # gagal pertama → retry 5 menit
BRIEF_BACKOFF_MAX_SEC = 3600.0     # backoff maksimum
# Budget token brief. Model thinking ikut memakai maxOutputTokens; 400 terlalu kecil dan
# membuat jawaban terpotong (ditolak) atau kosong → brief lama tampil berhari-hari.
BRIEF_MAX_TOKENS = 1200
BRIEF_REJECT_BACKOFF_SEC = 900.0   # output AI ditolak fact-check → tunggu 15 menit, jangan tiap cycle
BRIEF_CALL_TIMEOUT_SEC = 40.0      # brief inline; log menunjukkan 30s/60s gagal, 40s batas tengah


def brief_next_backoff(streak: int, err: str | None) -> float:
    """Jadwal retry brief setelah gagal.

    - Kapasitas Gemini (503 / high demand / unavailable): **tetap 10 menit** —
      selaras pause client; jangan naik ke 60m (itu yang bikin brief basi berjam-jam).
    - 429 rate-limit: 5–15 menit.
    - Error lain: eksponensial 5m→10m→20m→40m→60m (cap).
    """
    e_ = (err or "").lower()
    # Capacity / overload: fixed window, independent of streak
    if any(k in e_ for k in ("503", "high demand", "unavailable", "capacity", "resource exhausted")):
        try:
            from cryptone_engine import GEMINI_PAUSE_HIGH_DEMAND_SEC
            return float(GEMINI_PAUSE_HIGH_DEMAND_SEC)
        except Exception:  # noqa: BLE001
            return 600.0
    if any(k in e_ for k in ("429", "rate limit", "rate_limit", "quota")):
        return min(900.0, max(300.0, BRIEF_BACKOFF_BASE_SEC * max(1, int(streak))))
    s_ = max(1, int(streak))
    return min(BRIEF_BACKOFF_MAX_SEC, BRIEF_BACKOFF_BASE_SEC * (2 ** min(s_ - 1, 4)))


def _brief_stored_facts(memory) -> dict | None:
    """Fakta (jumlah, persen, label) yang dipakai SAAT brief dibuat dan lolos fact-check.
    Validasi tampil harus memakai fakta ini, bukan data cycle sekarang: breadth/volume bergeser
    tiap cycle, dan brief yang valid saat dibuat tidak boleh hilang hanya karena angka bergeser."""
    try:
        raw = memory.get_runtime("market_brief_facts") or ""
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return {
            "pos": int(d["pos"]), "neg": int(d["neg"]), "total": int(d["total"]),
            "mag": str(d["mag"]), "vol_label": str(d.get("vol_label") or ""),
            "pcts": [float(x) for x in d.get("pcts", [])],
            "counts": {int(x) for x in d.get("counts", [])},
        }
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _brief_runtime_float(memory, key: str) -> float:
    try:
        return float(memory.get_runtime(key) or "0")
    except (TypeError, ValueError):
        return 0.0


def _brief_breadth_facts(rows) -> dict:
    """Angka breadth yang dipakai brief. Sumber sama dengan kartu Snapshot (funding 6 jam),
    supaya angka di brief selalu bisa dicocokkan dengan yang tampil di kartu."""
    rates = [float(r["rate"]) for r in (rows or [])]
    rates8 = [x * FUNDING_PERIODS_PER_8H * 100.0 for x in rates]
    rl, rs, _rn, _ = _ramai_split(rates8) if rates8 else (0, 0, 0, 50.0)
    return {
        "pos": sum(1 for x in rates if x > 0),
        "neg": sum(1 for x in rates if x < 0),
        "total": len(rates),
        "ramai_long": rl,
        "ramai_short": rs,
        "ramai_total": rl + rs,  # "dari 13 koin ramai" di kartu = long + short
    }


def _brief_extra_pcts(memory, rows=None) -> list[float]:
    """Persen sah selain breadth yang boleh disebut brief: volume (ratio vs baseline), OI 24 jam,
    serta rata-rata & median funding per 8 jam (keduanya tampil di prompt/kartu, mis. +0.0100%)."""
    extra: list[float] = []
    if rows:
        rates8 = [float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0 for r in rows]
        extra.append(abs(sum(rates8) / len(rates8)))
        extra.append(abs(_median(rates8)))
    try:
        extra.append(float((universe_volume_regime(memory) or {}).get("ratio")) * 100.0)
    except (TypeError, ValueError):
        pass
    try:
        _anc = memory.get_last_anchor() if hasattr(memory, "get_last_anchor") else None
        _oi = anchor_oi_delta_pct(memory, str(_anc), lookback_hours=24.0) if _anc and _anc != "—" else None
        if _oi:
            extra.append(abs(float(_oi["oi_delta_pct"])))
    except Exception:  # noqa: BLE001 — OI hanya memperluas daftar angka sah
        pass
    return extra


def _brief_stale_gate(
    gem_brief: str, brief_reason: str | None, ok_age: float | None,
) -> tuple[str, str | None, float | None]:
    """Teks AI yang terlalu tua tidak dibaca sebagai analisis hari ini. Return (teks, alasan, jam_basi)."""
    if gem_brief and ok_age is not None and ok_age > BRIEF_STALE_HIDE_SEC:
        return "", "basi", ok_age / 3600.0
    return gem_brief, brief_reason, None


def _brief_set_status(memory, msg: str) -> None:
    """Catat hasil refresh terakhir (ok / alasan gagal) ke runtime dan log.
    Hanya dicatat ke log saat status berubah, supaya tidak banjir tiap cycle. Tidak tampil di Telegram."""
    try:
        prev = (memory.get_runtime("market_brief_last_status") or "").strip()
        memory.set_runtime("market_brief_last_status", msg[:160])
        memory.set_runtime("market_brief_last_status_ts", str(time.time()))
    except Exception:  # noqa: BLE001
        return
    if msg != prev:
        if msg == "ok":
            logger.info("🌍 market brief · status · ok")
        else:
            logger.warning("🌍 market brief · status · %s", msg)


async def refresh_market_brief(memory: "MemoryEngine") -> None:
    """Gemini market brief → runtime. Fail-soft dengan jadwal persisten.
    - Sukses: refresh tiap BRIEF_OK_INTERVAL_SEC (default 0 = tiap cycle).
    - Gagal API/format: backoff eksponensial (brief_next_backoff).
    - Jawaban Gemini ditolak fact-check: API sudah menjawab, jadi ini BUKAN kegagalan.
      Tidak ada retry rapat; tunggu jadwal normal. Brief tersimpan sebelumnya tetap berlaku
      (umurnya ditampilkan apa adanya).
    - Gemini sedang pause: tidak ada panggilan API sama sekali.
    """
    now = time.time()
    t_brief = time.monotonic()
    gem = get_gemini_client(memory)
    next_ts = _brief_runtime_float(memory, "market_brief_next_ts")
    # Jika client sudah tidak pause tapi next_ts masih jauh (backoff capacity
    # lama dari build sebelumnya), clamp ke sekarang supaya cycle berikutnya
    # boleh coba lagi tanpa nunggu 60m sisa.
    if gem.available() and not gem.is_paused() and next_ts - now > 900:
        try:
            last_err = (memory.get_runtime("market_brief_last_err") or "").lower()
        except Exception:  # noqa: BLE001
            last_err = ""
        if any(k in last_err for k in ("503", "capacity", "unavailable", "high demand", "paused")):
            memory.set_runtime("market_brief_next_ts", str(now))
            next_ts = now
            logger.info("🌍 market brief · clamp stale capacity backoff")
    if now < next_ts:
        return
    if not gem.available():
        _brief_set_status(memory, "API key Gemini tidak tersedia")
        memory.set_runtime("market_brief_next_ts", str(now + BRIEF_BACKOFF_MAX_SEC))
        return
    if gem.is_paused():
        _brief_set_status(memory, "Gemini sedang pause (kapasitas/kuota)")
        remain = gem.pause_remaining()
        memory.set_runtime(
            "market_brief_next_ts",
            str(now + min(max(remain, 30.0), 600.0)),
        )
        logger.debug("🌍 market brief · skip · gemini paused %.0fs", remain)
        return

    def _fail(err: str) -> None:
        try:
            streak = int(memory.get_runtime("market_brief_fail_streak") or "0") + 1
        except (TypeError, ValueError):
            streak = 1
        backoff = brief_next_backoff(streak, err)
        _brief_set_status(memory, f"gagal: {err or 'tanpa keterangan'}")
        memory.set_runtime("market_brief_fail_streak", str(streak))
        memory.set_runtime("market_brief_last_err", str(err or "")[:120])
        memory.set_runtime("market_brief_next_ts", str(now + backoff))
        logger.info(
            "🌍 market brief · defer · %s · %.1fs · retry %dm",
            err, time.monotonic() - t_brief, int(backoff // 60),
        )

    try:
        fng_fresh = await fear_greed_latest()
    except Exception:
        fng_fresh = None
    if fng_fresh is not None:
        fng_raw = f"{float(fng_fresh):.0f}"
        try:
            memory.set_runtime("last_fng", fng_raw)
            memory.set_runtime("last_fng_ts", str(time.time()))
        except Exception:  # noqa: BLE001
            pass
    else:
        # F&G hanya satu bahan brief. Pakai nilai segar terakhir; bila tidak ada, brief tetap
        # dibuat tanpa F&G (sebelumnya: return → brief mati selama API F&G bermasalah).
        _fng_cached = _current_fng(memory)
        fng_raw = f"{float(_fng_cached):.0f}" if _fng_cached is not None else "n/a"
    try:
        rows = _latest_funding_rows(memory, 6.0) or []
        facts = _brief_breadth_facts(rows)
        pos, neg, total = facts["pos"], facts["neg"], facts["total"]
        fund_summary = funding_snapshot_summary([float(r["rate"]) for r in rows])
        uvol = universe_volume_regime(memory) or {}
        vol_label = str(uvol.get("label") or "")
        bias = rss_news_bias() or "netral"
        # Angka sah = persis yang nanti dicek brief_check saat tampil (satu sumber, tidak ada selisih).
        allowed_pcts: set[float] = (
            {pos / total * 100.0, neg / total * 100.0} if total > 0 else set()
        )
        allowed_pcts.update(_brief_extra_pcts(memory, rows))
        counts = {pos, neg, total, facts["ramai_long"], facts["ramai_short"], facts["ramai_total"]}
        _lang_label = {"id": "Bahasa Indonesia", "en": "English"}.get(
            BRIEF_LANGUAGE, "Bahasa Indonesia",
        )
    except Exception as e:  # noqa: BLE001 — persiapan gagal tetap harus dijadwal ulang
        _fail(f"persiapan data {type(e).__name__}")
        return
    prompt = (
        f"Tulis brief pasar crypto dalam {_lang_label}. "
        f"Panjang: {BRIEF_MAX_SENTENCES} kalimat utuh (bukan bullet). "
        f"Gaya: laporan desk trader profesional — jelas, mengalir, tanpa emoji, "
        f"tanpa perintah beli/jual. "
        f"Fakta (pakai angka ini apa adanya, jangan hitung ulang atau menambah angka lain): "
        f"koin ramai = |funding| tinggi per 8 jam, bukan seluruh universe — "
        f"sekarang {facts['ramai_total']} koin ramai "
        f"({facts['ramai_long']} long, {facts['ramai_short']} short) dari {total} simbol; "
        f"tanda funding: {pos} positif, {neg} negatif (bukan rate); "
        f"magnitude funding = {fund_summary['mag']} "
        f"({fund_summary['side']}, avg {fund_summary['avg_8h_pct']:+.4f}% per 8 jam); "
        f"volume universe = {_universe_vol_line(uvol)}; "
        f"Fear&Greed = {fng_raw}; bias berita = {bias}. "
        f"Jelaskan arti untuk bias intraday (crowd vs magnitude vs sentimen), "
        f"bukan menempel daftar angka. "
        f"Jika magnitude FLAT, jangan sebut funding tinggi/mahal/ekstrem. "
        f"Jangan sebut persentase sebagai biaya funding. "
        f"Jangan invent angka, persen, atau jumlah koin di luar fakta di atas."
    )
    try:
        res = await asyncio.wait_for(
            gem.generate(
                "market_brief",
                prompt,
                temperature=0.3,
                max_tokens=BRIEF_MAX_TOKENS,
                use_cache=True,
                cache_ttl_h=1.0,
                timeout_sec=BRIEF_CALL_TIMEOUT_SEC,
            ),
            timeout=BRIEF_CALL_TIMEOUT_SEC + 2.0,  # worst-case blok ≤ 42s
        )
    except asyncio.TimeoutError:
        _fail("timed out")
        return
    except Exception as e:  # noqa: BLE001 — fail-soft
        _fail(f"error {type(e).__name__}")
        return

    text_out = (res.text or "").strip() if res and res.success else ""
    if not text_out:
        _fail(getattr(res, "error", None) or "no text")
        return
    stored = text_out
    if len(stored) > 500:
        cut = max(
            stored.rfind(". ", 0, 500),
            stored.rfind("! ", 0, 500),
            stored.rfind("? ", 0, 500),
        )
        stored = stored[:cut + 1] if cut >= 60 else ""
    if not is_clean_market_brief(stored):
        logger.info("🌍 market brief · reject invalid · %r", text_out[:60])
        # Brief lama (lolos fact-check saat dibuat) tetap dipakai; umurnya tampil apa adanya.
        _fail("format tidak lolos cek (kalimat terpotong/bocor)")
        return
    _viol = brief_fact_violation(
        stored, fund_summary["mag"], allowed_pcts, counts=counts, vol_label=vol_label,
    )
    if _viol:
        logger.info("🌍 market brief · fact-check reject · %s", _viol)
        _brief_set_status(memory, f"angka AI ditolak fact-check ({_viol.split(':')[0]})")
        memory.set_runtime("market_brief_fail_streak", "0")
        memory.set_runtime("market_brief_next_ts", str(now + BRIEF_REJECT_BACKOFF_SEC))
        return
    try:
        memory.set_runtime("market_brief_facts", json.dumps({
            "pos": pos, "neg": neg, "total": total,
            "mag": str(fund_summary["mag"]), "vol_label": vol_label,
            "pcts": sorted(float(x) for x in allowed_pcts),
            "counts": sorted(int(x) for x in counts),
        }))
    except Exception:  # noqa: BLE001 — tanpa fakta tersimpan, tampil jatuh ke validasi data sekarang
        pass
    memory.set_runtime("market_brief", stored)
    memory.set_runtime("market_brief_ts", str(now))
    # Umur tampil brief wajib dari timestamp SUKSES ini (bukan dari jadwal retry)
    memory.set_runtime("market_brief_ok_ts", str(time.time()))
    _brief_set_status(memory, "ok")
    memory.set_runtime("market_brief_fail_streak", "0")
    memory.set_runtime("market_brief_next_ts", str(now + BRIEF_OK_INTERVAL_SEC))
    logger.info("🌍 market brief · refreshed")

def _macro_parts(memory: "MemoryEngine") -> tuple[str, str | None]:
    """(kalimat utama, detail) untuk kalender makro, dalam bahasa trader."""
    try:
        mins, ev_name, ev_impact = minutes_to_next_macro(memory)
    except Exception:
        return "Belum ada data kalender", None
    if mins is None or not ev_name:
        return "Belum ada data kalender", None
    past = mins < 0
    am = abs(mins)
    if am < 60:
        when = f"{am:.0f} menit"
    elif am < 24 * 60:
        when = f"{am / 60:.1f} jam"
    else:
        when = f"{am / (24 * 60):.1f} hari"
    if past:
        return f"{ev_name} baru lewat", f"{when} lalu"
    if am > 24 * 60:
        return "Tidak ada rilis besar 24 jam ke depan", f"berikutnya {ev_name} dalam {when}"
    return f"{ev_name} dalam {when}", (f"dampak {ev_impact}" if ev_impact else None)


def _current_fng(memory: "MemoryEngine") -> float | None:
    """Fear & Greed terkini: runtime, atau cache lokal bila runtime belum ada.
    Data lebih dari FNG_STALE_SEC dianggap basi dan tidak ditampilkan sebagai 'sekarang'."""
    fng_v: float | None = None
    try:
        raw_fng = (memory.get_runtime("last_fng") or "").strip()
        if raw_fng:
            try:
                ts_fng = float(memory.get_runtime("last_fng_ts") or "0")
            except (TypeError, ValueError):
                ts_fng = 0.0
            if ts_fng > 0 and (time.time() - ts_fng) < FNG_STALE_SEC:
                fng_v = float(raw_fng)
    except (TypeError, ValueError):
        fng_v = None
    if fng_v is None:
        try:
            raw = _FNG_CACHE.get("value")
            ts_c = float(_FNG_CACHE.get("ts") or 0)
            if raw is not None and (time.time() - ts_c) < FNG_STALE_SEC:
                fng_v = float(raw)
        except (TypeError, ValueError):
            fng_v = None
    return fng_v


def _market_verdict(memory: "MemoryEngine", bs: str) -> str:
    """Satu baris kesimpulan dari kondisi yang sudah dihitung. Urutan: risiko > jendela makro > kerumunan."""
    if bs == "1":
        return "🚨 Ringkas: risiko tinggi, pipeline ketat"
    if bs == "ELEVATED":
        return "⚠️ Ringkas: waspada, hanya Tier A"
    try:
        mins, ev_name, ev_imp = minutes_to_next_macro(memory)
    except Exception:  # noqa: BLE001
        mins, ev_name, ev_imp = None, None, None
    if mins is not None and ev_name:
        window = EVENT_BLACKOUT_MIN if (ev_imp or "high") == "high" else EVENT_BLACKOUT_MEDIUM_MIN
        if abs(mins) < window:
            return f"⏸ Ringkas: jeda entry, {ev_name} dekat"
    rates: list[float] = []
    try:
        _rows = _latest_funding_rows(memory, 6.0) or []
    except Exception:  # noqa: BLE001 — verdict tidak boleh gagal karena funding
        _rows = []
    for r in _rows:
        try:
            rates.append(float(r["rate"]))
        except (TypeError, ValueError, KeyError):
            continue
    if rates:
        # Samakan dengan kartu Pasar: koin RAMAI, bukan % tanda rate + median FLAT.
        _pct8 = [float(x) * FUNDING_PERIODS_PER_8H * 100.0 for x in rates]
        _nl, _ns, _nn, _share = _ramai_split(_pct8)
        _n_ramai = _nl + _ns
        if _n_ramai >= SQUEEZE_MIN_RAMAI and _share >= 70.0:
            return "👀 Ringkas: long padat di koin ramai · awasi long squeeze"
        if _n_ramai >= SQUEEZE_MIN_RAMAI and _share <= 30.0:
            return "👀 Ringkas: short padat di koin ramai · awasi short squeeze"
        if _n_ramai >= SQUEEZE_MIN_RAMAI:
            return "🟡 Ringkas: ada koin ramai, belum condong satu sisi"
    return "🟡 Ringkas: belum ada crowd dominan · tunggu setup"


def _short_name(name: str, n: int) -> str:
    """Pangkas nama event di batas kata, bukan di tengah kata."""
    s = str(name).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut).rstrip() + "…"


def _wib_when(at, now) -> str:
    """Jam WIB; tambah tanggal bila bukan hari ini."""
    t, n = _as_wib(at), _as_wib(now)
    if t.date() == n.date():
        return f"{t:%H:%M}"
    return f"{t.day:02d} {_BULAN_ID[t.month - 1]} {t:%H:%M}"


def _macro_calendar_lines(memory: "MemoryEngine") -> list[str]:
    """Event USD/JPY/EUR/CNY high (medium USD) 48 jam ke depan, selain event pertama yang sudah
    tampil di baris utama Makro. Hanya event yang boleh memicu jeda."""
    try:
        evs = parse_macro_events(memory)
    except Exception:  # noqa: BLE001
        return []
    now = now_utc()
    lo, hi = now - timedelta(minutes=30), now + timedelta(hours=48)
    upcoming = sorted(
        (e for e in evs if str(e.get("impact")) in ("high", "medium") and lo <= e["at"] <= hi),
        key=lambda e: e["at"],
    )
    out: list[str] = []
    for e in upcoming[1:4]:
        dot = "🔴" if str(e.get("impact")) == "high" else "🟠"
        tag = f"{e['ccy']} " if e.get("ccy") and e.get("ccy") != "USD" else ""
        out.append(f"• {dot} {tag}{_short_name(e['name'], 28)} · {_wib_when(e['at'], now)} WIB")
    return out


def _crypto_info_lines() -> list[str]:
    """Konteks crypto (tidak memblok entry): opsi Deribit, futures CME, libur bursa AS."""
    now = now_utc()
    try:
        evs = crypto_info_events(now)
    except Exception:  # noqa: BLE001 — konteks tidak boleh menjatuhkan snapshot
        return []
    if not evs:
        return []
    lines: list[str] = []
    for ev in evs[:4]:
        d = ev["at"]
        if ev["kind"] == "deribit":
            t = _as_wib(d)
            lines.append(f"• Opsi Deribit bulanan · {t.day:02d} {_BULAN_ID[t.month - 1]} {t:%H:%M} WIB")
        elif ev["kind"] == "cme":
            lines.append(f"• Futures CME kuartalan · {d.day:02d} {_BULAN_ID[d.month - 1]}")
        else:
            label = ev["name"].replace("Bursa AS libur · ", "AS libur · ")
            lines.append(f"• {label} · {d.day:02d} {_BULAN_ID[d.month - 1]}")
    return ["", "▸ 🗓 Konteks crypto"] + lines


def _holiday_volume_lines() -> list[str]:
    """Hari libur bursa AS hari ini: ETF & CME tutup → volume tipis (konteks, bukan jeda)."""
    today = now_utc().date()
    name = us_market_holidays(today.year).get(today)
    if not name:
        return []
    return [
        "• Bursa AS libur hari ini",
        f"  └ {name}: ETF & CME tutup, volume tipis",
        "  └ breakout lebih sering palsu",
    ]



SECTOR_VOL_CHANGE_PCT = 20.0   # volume sektor naik/turun ≥20% vs 24 jam lalu (heuristik awal)
SECTOR_DECOUPLE_PCT = 3.0      # fallback band (poin) bila riwayat 7 hari belum cukup
SECTOR_DECOUPLE_FLOOR_PCT = 1.0  # band tidak pernah lebih sempit dari ini (noise harga)
SECTOR_DECOUPLE_K = 2.0        # band = K × simpangan baku spread 24 jam sektor−BTC (7 hari)
SECTOR_CALIB_HOURS = 7 * 24
SECTOR_CALIB_MIN_POINTS = 48   # jumlah jam spread 24 jam minimum sebelum band adaptif dipakai
SECTOR_PRICE_FLAT_PCT = 0.5    # |return| di bawah ini dibaca "datar"


def _sector_hot_lines(memory: "MemoryEngine", rows: list) -> list[str]:
    """Narasi: sektor apa yang paling banyak di koin ramai, dengan contoh koin RAMAI hari ini.
    Contoh diambil dari data (koin ramai di sektor itu, |funding| terbesar), bukan dari daftar
    statis taksonomi. Koin ramai tanpa sektor dihitung terpisah supaya penyebutnya jujur."""
    if not rows or memory is None:
        return []
    pcts: list[tuple[str, float]] = []
    for r in rows:
        try:
            pct8 = float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0
            sym = str(r["symbol"] or "")
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if sym:
            pcts.append((sym, pct8))
    thr = _crowd_thr_pct8([p for _, p in pcts])
    counts: dict[str, int] = {}
    members: dict[str, list[tuple[str, float]]] = {}
    ramai = 0
    for sym, pct8 in pcts:
        if abs(pct8) < thr:
            continue
        ramai += 1
        tag = narrative_sector_for(memory, sym)
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
            members.setdefault(tag, []).append((sym, abs(pct8)))
    if ramai == 0:
        return []
    mapped = sum(counts.values())
    if not counts:
        return ["• Narasi: belum terpetakan", f"  └ tanpa sektor: {ramai} dari {ramai} ramai"]
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    labels = " · ".join(f"{sector_label(t)} {n}" for t, n in ranked[:2])
    lead, lead_n = ranked[0]
    out = [
        f"• Narasi ramai: {labels}",
        f"  └ {sector_label(lead)} dominan · {lead_n} dari {ramai} ramai",
    ]
    ex = [s for s, _ in sorted(members[lead], key=lambda m: (-m[1], m[0]))[:3]]
    if ex:
        out.append(f"  └ contoh {sector_label(lead)}: {', '.join(ex)}")
    if ramai - mapped > 0:
        out.append(f"  └ tanpa sektor: {ramai - mapped} dari {ramai} ramai")
    return out


def _px_latest(memory: "MemoryEngine", sym: str) -> tuple[float, str] | None:
    row = memory._conn.execute(
        "SELECT px, recorded_at FROM metric_oi WHERE symbol = ? AND px IS NOT NULL "
        "ORDER BY recorded_at DESC LIMIT 1", (sym,),
    ).fetchone()
    return None if row is None else (float(row["px"]), str(row["recorded_at"]))


RET_BASE_TOL_H = 3.0  # basis 24 jam harus berasal dari 3 jam sekitar 24 jam lalu, tidak lebih tua


def _px_before(memory: "MemoryEngine", sym: str, cutoff_iso: str) -> float | None:
    lo = (datetime.fromisoformat(cutoff_iso) - timedelta(hours=RET_BASE_TOL_H)).isoformat()
    row = memory._conn.execute(
        "SELECT px FROM metric_oi WHERE symbol = ? AND px IS NOT NULL "
        "AND recorded_at <= ? AND recorded_at >= ? "
        "ORDER BY recorded_at DESC LIMIT 1", (sym, cutoff_iso, lo),
    ).fetchone()
    return None if row is None else float(row["px"])


def _ret_24h(memory: "MemoryEngine", sym: str) -> float | None:
    """Return harga 24 jam (%) dari metric_oi. None bila riwayat belum 24 jam."""
    latest = _px_latest(memory, sym)
    if latest is None:
        return None
    cutoff = (now_utc() - timedelta(hours=24)).isoformat()
    base = _px_before(memory, sym, cutoff)
    if base is None or base <= 0:
        return None
    return (latest[0] / base - 1.0) * 100.0


def _vol_24h_detail(memory: "MemoryEngine", syms: list[str]) -> tuple[float | None, int, float]:
    """(perubahan volume % sekelompok, jumlah koin yang terpakai, jam riwayat volume SUNGGUHAN).
    Titik 24 jam lalu harus berasal dari jendela RET_BASE_TOL_H; koin tanpa titik itu dilewati.
    Jam riwayat = sejak baris volume PERTAMA koin kelompok ini (bukan riwayat harga)."""
    cutoff_dt = now_utc() - timedelta(hours=24)
    cutoff = cutoff_dt.isoformat()
    lo = (cutoff_dt - timedelta(hours=RET_BASE_TOL_H)).isoformat()
    now_sum = then_sum = 0.0
    used = 0
    earliest: str | None = None
    for sym in syms:
        now_row = memory._conn.execute(
            "SELECT day_ntl_vlm FROM metric_volume WHERE symbol = ? "
            "ORDER BY recorded_at DESC LIMIT 1", (sym,),
        ).fetchone()
        then_row = memory._conn.execute(
            "SELECT day_ntl_vlm FROM metric_volume WHERE symbol = ? "
            "AND recorded_at <= ? AND recorded_at >= ? "
            "ORDER BY recorded_at DESC LIMIT 1", (sym, cutoff, lo),
        ).fetchone()
        first = memory._conn.execute(
            "SELECT MIN(recorded_at) AS t FROM metric_volume WHERE symbol = ?", (sym,),
        ).fetchone()
        if first is not None and first["t"] and (earliest is None or str(first["t"]) < earliest):
            earliest = str(first["t"])
        if now_row is None or then_row is None:
            continue
        now_sum += float(now_row["day_ntl_vlm"])
        then_sum += float(then_row["day_ntl_vlm"])
        used += 1
    hours = 0.0
    if earliest:
        try:
            hours = max(0.0, (now_utc() - datetime.fromisoformat(earliest)).total_seconds() / 3600.0)
        except ValueError:
            hours = 0.0
    if used == 0 or then_sum <= 0:
        return None, used, hours
    return (now_sum / then_sum - 1.0) * 100.0, used, hours


def _vol_24h_change(memory: "MemoryEngine", syms: list[str]) -> float | None:
    """Perubahan volume (%) total sekelompok koin: sekarang vs ≈24 jam lalu. None bila belum cukup."""
    return _vol_24h_detail(memory, syms)[0]


def _history_hours(memory: "MemoryEngine") -> float:
    """Berapa jam riwayat harga yang sudah terkumpul (untuk pesan 'data belum cukup')."""
    row = memory._conn.execute("SELECT MIN(recorded_at) AS t FROM metric_oi").fetchone()
    if row is None or not row["t"]:
        return 0.0
    try:
        first = datetime.fromisoformat(str(row["t"]))
    except ValueError:
        return 0.0
    return max(0.0, (now_utc() - first).total_seconds() / 3600.0)


def _hourly_px(memory: "MemoryEngine", syms: list[str], hours: float) -> dict[str, dict[str, float]]:
    """Harga per jam (kunci 'YYYY-MM-DDTHH', sampel terakhir per jam) dari metric_oi."""
    cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
    marks = ",".join("?" * len(syms))
    rows = memory._conn.execute(
        f"SELECT symbol, px, recorded_at FROM metric_oi WHERE symbol IN ({marks}) "
        f"AND px IS NOT NULL AND recorded_at >= ? ORDER BY recorded_at",
        (*syms, cutoff),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(str(r["symbol"]), {})[str(r["recorded_at"])[:13]] = float(r["px"])
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _sector_btc_calibration(memory: "MemoryEngine", members: list[str]) -> dict | None:
    """Kalibrasi sektor vs BTC dari riwayat harga sungguhan (7 hari, per jam).

    - corr: korelasi return 1 jam median sektor vs BTC.
    - band: K × simpangan baku spread return 24 jam (sektor − BTC), dengan lantai.
      Spread 24 jam saling tumpang tindih, jadi n efektif lebih kecil dari jumlah titik;
      itu sebabnya band dibatasi lantai dan dihitung hanya bila titik ≥ SECTOR_CALIB_MIN_POINTS.
    Mengembalikan None bila data belum cukup (pemanggil memakai SECTOR_DECOUPLE_PCT)."""
    syms = sorted({s for s in members if s != "BTC"})
    if not syms:
        return None
    series = _hourly_px(memory, syms + ["BTC"], SECTOR_CALIB_HOURS)
    btc = series.get("BTC", {})
    if len(btc) < SECTOR_CALIB_MIN_POINTS:
        return None
    fmt = "%Y-%m-%dT%H"

    def _shift(key: str, hrs: int) -> str | None:
        try:
            return (datetime.strptime(key, fmt) + timedelta(hours=hrs)).strftime(fmt)
        except ValueError:
            return None

    def _med_ret(hour: str, prev: str) -> float | None:
        rs = []
        for s in syms:
            pxs = series.get(s, {})
            a, b = pxs.get(hour), pxs.get(prev)
            if a is not None and b is not None and b > 0:
                rs.append((a / b - 1.0) * 100.0)
        return _median(rs) if rs else None

    r1_sec, r1_btc, spreads = [], [], []
    for hour in sorted(btc):
        prev1, prev24 = _shift(hour, -1), _shift(hour, -24)
        if prev1 is None or prev24 is None:
            continue
        if prev1 in btc and btc[prev1] > 0 and hour in btc:
            b1 = (btc[hour] / btc[prev1] - 1.0) * 100.0
            s1 = _med_ret(hour, prev1)
            if s1 is not None:
                r1_sec.append(s1)
                r1_btc.append(b1)
        if prev24 in btc and btc[prev24] > 0 and hour in btc:
            b24 = (btc[hour] / btc[prev24] - 1.0) * 100.0
            s24 = _med_ret(hour, prev24)
            if s24 is not None:
                spreads.append(s24 - b24)
    if len(spreads) < SECTOR_CALIB_MIN_POINTS:
        return None
    mean = sum(spreads) / len(spreads)
    sd = math.sqrt(sum((x - mean) ** 2 for x in spreads) / (len(spreads) - 1))
    band = max(SECTOR_DECOUPLE_FLOOR_PCT, SECTOR_DECOUPLE_K * sd)
    corr = _pearson(r1_sec, r1_btc)
    return {"band": band, "corr": corr, "n": len(spreads)}


def sector_reading_lines(memory: "MemoryEngine", pairs: list[tuple[str, float]],
                         crowd: str | None) -> list[str]:
    """Kesimpulan per sektor dominan: arah harga, arah volume, dan posisi kerumunan.
    Ramai ≠ arah: crowd menjelaskan posisi, harga & volume menjelaskan gerak."""
    if memory is None or memory._conn is None or not pairs:
        return []
    thr = _crowd_thr_pct8([p for _, p in pairs])
    groups: dict[str, list[tuple[str, float]]] = {}
    for sym, pct8 in pairs:
        tag = narrative_sector_for(memory, sym)
        if tag and tag.upper() != "OTHER":
            groups.setdefault(tag.upper(), []).append((sym, pct8))
    best: tuple[str, list[tuple[str, float]], list[tuple[str, float]]] | None = None
    for tag, members in groups.items():
        ramai = [m for m in members if abs(m[1]) >= thr]
        if not ramai:
            continue
        if best is None or len(ramai) > len(best[2]):
            best = (tag, members, ramai)
    if best is None:
        return []
    tag, members, ramai = best
    label = sector_label(tag)
    lines: list[str] = []
    ramai_pos = sum(1 for _, p in ramai if p > 0)
    crowd_side = "LONG" if ramai_pos > len(ramai) - ramai_pos else (
        "SHORT" if ramai_pos < len(ramai) - ramai_pos else None)

    rets = [r for r in (_ret_24h(memory, sym) for sym, _ in members) if r is not None]
    if not rets:
        return [
            f"• {label}: data harga 24 jam belum cukup",
            f"  └ riwayat {_history_hours(memory):.0f} jam dari 24 jam",
        ]
    ret = _median(rets)
    btc = _ret_24h(memory, "BTC")
    vol, _vol_used, vol_hours = _vol_24h_detail(memory, [sym for sym, _ in members])
    calib = _sector_btc_calibration(memory, [sym for sym, _ in members])
    band = calib["band"] if calib else SECTOR_DECOUPLE_PCT

    if btc is not None and abs(ret - btc) >= band:
        lines.append(f"• {label} decoupled dari BTC")
        lines.append(f"  └ {label} {ret:+.1f}% vs BTC {btc:+.1f}%")
    else:
        if ret >= SECTOR_PRICE_FLAT_PCT:
            price_word = "naik"
        elif ret <= -SECTOR_PRICE_FLAT_PCT:
            price_word = "turun"
        else:
            price_word = "datar"
        if vol is None:
            vol_word = "volume belum cukup"
        elif vol >= SECTOR_VOL_CHANGE_PCT:
            vol_word = "volume naik"
        elif vol <= -SECTOR_VOL_CHANGE_PCT:
            vol_word = "volume turun"
        else:
            vol_word = "volume stabil"
        lines.append(f"• {label} {price_word}, {vol_word}")
        lines.append(f"  └ {label} {ret:+.1f}% (24 jam)")
        if vol is None:
            if vol_hours >= 24:
                lines.append("  └ celah data volume dalam 24 jam")
            else:
                lines.append(f"  └ riwayat volume {vol_hours:.0f} dari 24 jam")
        elif price_word == "naik" and vol_word == "volume naik":
            lines.append("  └ naik didukung volume: momentum nyata")
        elif price_word == "naik" and vol_word != "volume naik":
            lines.append("  └ naik tanpa volume: rawan balik")
        elif price_word == "turun" and vol_word == "volume naik":
            lines.append("  └ turun dengan volume: tekanan jual")
        elif price_word == "turun":
            lines.append("  └ turun tanpa tekanan volume")
        else:
            lines.append("  └ belum ada arah yang jelas")

    if calib is not None:
        if calib["corr"] is not None:
            lines.append(f"  └ korelasi 7 hari vs BTC {calib['corr']:+.2f}")
        lines.append(f"  └ band normal ±{calib['band']:.1f} pp (dari riwayat)")
    if crowd_side == "LONG":
        lines.append("  └ long padat: ramai bukan arah, awasi flush")
    elif crowd_side == "SHORT":
        lines.append("  └ short padat: awasi short squeeze")
    return lines


def _fmt_market(memory: "MemoryEngine") -> str:
    """Snapshot pasar + ringkasan hari ini (rule-based; Gemini brief opsional cache)."""
    anchor = memory.get_last_anchor() or "—"
    bs = memory.get_runtime("black_swan_mode", "0") or "0"
    bs_label = {
        "1": "🚨 aktif",
        "ELEVATED": "⚠️ waspada (Tier A saja)",
    }.get(bs, "✅ normal")
    n_act = memory.count_active_signals()
    atr_n = 0
    if memory._conn:
        try:
            cutoff_24h = (now_utc() - timedelta(hours=24)).isoformat()
            atr_n = int(memory._conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM metric_atr "
                "WHERE recorded_at >= ?",
                (cutoff_24h,),
            ).fetchone()["n"])
        except Exception:
            pass

    rows = _latest_funding_rows(memory, 6.0)
    pos = sum(1 for r in rows if float(r["rate"]) > 0)
    neg = sum(1 for r in rows if float(r["rate"]) < 0)
    total = len(rows)
    fund_summary = funding_snapshot_summary([float(r["rate"]) for r in rows])

    bias = rss_news_bias() or "netral"
    sess = _session_status_line()
    try:
        _ok_raw = memory.get_runtime("market_brief_ok_ts") or ""
        _ok_age = (time.time() - float(_ok_raw)) if _ok_raw else None
    except (TypeError, ValueError):
        _ok_age = None
    # Brief tersimpan (termasuk yang dibuat build lama) divalidasi ulang terhadap
    # data SAAT INI; basi/halusinasi disembunyikan, bukan ditampilkan.
    _vol_label = str((universe_volume_regime(memory) or {}).get("label") or "")
    _extra = _brief_extra_pcts(memory, rows)
    _bf = _brief_breadth_facts(rows)
    _bf_facts = _brief_stored_facts(memory)
    if _bf_facts is not None:
        gem_brief, brief_reason = brief_check(
            memory.get_runtime("market_brief") or "", _ok_age,
            _bf_facts["mag"], _bf_facts["pos"], _bf_facts["neg"], _bf_facts["total"],
            vol_label=_bf_facts["vol_label"] or None, extra_pcts=_bf_facts["pcts"],
            extra_counts=_bf_facts["counts"],
        )
    else:  # brief dari build lama (tanpa fakta tersimpan): cek terhadap data sekarang
        gem_brief, brief_reason = brief_check(
            memory.get_runtime("market_brief") or "", _ok_age,
            fund_summary["mag"], pos, neg, total,
            vol_label=_vol_label, extra_pcts=_extra,
            extra_counts={_bf["ramai_long"], _bf["ramai_short"], _bf["ramai_total"]},
        )
    gem_brief = gem_brief or ""
    gem_brief, brief_reason, _stale_h = _brief_stale_gate(gem_brief, brief_reason, _ok_age)
    # Ringkasan data: selalu tersedia dari angka yang sudah tampil di kartu.
    _rates8 = [float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0 for r in rows] if total > 0 else []
    _rl, _rs, _rn, _ = _ramai_split(_rates8) if _rates8 else (0, 0, 0, 50.0)
    data_text = data_brief_text(ramai_long=_rl, ramai_short=_rs, mag=fund_summary["mag"])

    try:
        regime = detect_market_regime(
            memory, anchor if (isinstance(anchor, str) and anchor != "—") else None,
            fund_pos=pos, fund_total=total,
        )
    except Exception:
        regime = "mixed"
    if total > 0:
        _pct8 = [float(r["rate"]) * FUNDING_PERIODS_PER_8H * 100.0 for r in rows]
        _nl, _ns, _nn, _ = _ramai_split(_pct8)
        funding_head = f"• Ramai: 🔥 {_nl} long · ❄️ {_ns} short"
        funding_sub = f"  └ {total} koin, 6 jam"
        mag_head, mag_sub = _median_mag([float(r["rate"]) for r in rows])
        mag_block = [f"• Besar funding: {mag_head}", f"  └ {mag_sub}"]
    else:
        funding_head, funding_sub, mag_block = "• Funding: belum ada data", None, []
    macro_head, macro_sub = _macro_parts(memory)
    cas_advice, cas_note = _cascade_parts(str(regime), cascade_mode(memory))
    watch_parts = _bs_watch_parts(memory) if bs not in ("1", "ELEVATED") else None
    bs_sub = None
    if watch_parts:
        cnt_w, names_w = watch_parts
        bs_label = f"👀 pantau ({cnt_w}/{len(_BS_IND_LABEL)})"
        bs_sub = ", ".join(names_w) or None
    lines = [
        _ui_header("🌍", "Market Snapshot"),
        "Cockpit keputusan · regime, risiko, skor",
        str(sess),
        f"📡 Scan terakhir {_last_cycle_line(memory)}",
        f"{_REGIME_ICON.get(str(regime), '📈')} {_regime_label(str(regime)).capitalize()}"
        + (f" · acuan {anchor}" if anchor and anchor != "—" else ""),
        _market_verdict(memory, bs),
        "",
        "▸ 🛡️ Risiko",
        f"• Black swan: {bs_label}",
        *([f"  └ {bs_sub}"] if bs_sub else []),
        f"• Cascade: {cas_advice}",
        *([f"  └ {cas_note}"] if cas_note else []),
        f"• Posisi aktif: {n_act} sinyal",
        "",
        "▸ 📊 Pasar",
        funding_head,
        *([funding_sub] if funding_sub else []),
        *mag_block,
        *_sector_hot_lines(memory, rows),
        f"• Berita: {_bias_text(bias)}",
        "",
        "▸ 📅 Makro",
        f"• {macro_head}",
        *([f"  └ {macro_sub}"] if macro_sub else []),
        *_macro_calendar_lines(memory),
        *_crypto_info_lines(),
        "",
        *_market_read(memory),
        "",
        f"ℹ️ Cakupan ATR: {atr_n} koin (24 jam)",
    ]
    # AI brief selalu ada di Snapshot. Valid → teks Gemini; else ringkasan data adaptif
    # (tanpa pesan error kasar — bias tetap terbaca tiap buka menu).
    if gem_brief:
        lines.extend(["", f"▸ 🤖 AI brief · {brief_age_note(_ok_age)}", gem_brief[:500]])
    else:
        soft = {
            "angka tidak cocok data": "angka bergeser · refresh cycle berikutnya",
            "belum pernah jadi": "AI belum jadi · ringkasan data",
            "format tidak lolos cek": "output AI ditolak · ringkasan data",
            "jam sistem salah": "jam sistem · ringkasan data",
            "basi": "AI belum bisa diperbarui",
            "": "ringkasan data",
        }.get(str(brief_reason or ""), str(brief_reason or "ringkasan data"))
        if _stale_h is not None:
            soft = f"AI belum bisa diperbarui · brief terakhir {_stale_h:.0f} jam lalu"
        lines.extend([
            "",
            "▸ 🤖 AI brief",
            data_text,
            f"  └ {soft}",
        ])
    return "\n".join(lines)


_BS_IND_LABEL: dict[str, str] = {
    "atr_spike": "ATR melonjak",
    "volume_cascade": "volume ekstrem",
    "correlation_break": "korelasi antar-koin pecah",
    "funding_flip": "funding berbalik arah",
    "mega_cascade": "likuidasi besar",
    "cross_exchange_div": "harga beda antar-exchange",
    "news_spike": "lonjakan berita",
}


BS_WATCH_MAX_AGE_SEC = 1500.0  # > interval evaluasi black swan (≤15 menit): lebih tua = stale


def _bs_watch_parts(memory: "MemoryEngine") -> tuple[int, list[str]] | None:
    """(jumlah indikator aktif, nama indikator) dari WATCH. None jika tidak ada,
    rusak, atau sudah stale (throttle black swan: status lama tidak boleh tampil sebagai terkini)."""
    raw = memory.get_runtime("black_swan_watch") or ""
    if not raw:
        return None
    try:
        d = json.loads(raw)
        ts = _parse_ts(d.get("ts") or "")
        if ts is None or (now_utc() - ts).total_seconds() > BS_WATCH_MAX_AGE_SEC:
            return None
        names = [_BS_IND_LABEL.get(str(n), str(n)) for n in d.get("indicators", [])]
        cnt = int(d.get("count", len(names)))
    except (ValueError, TypeError, AttributeError):
        return None
    return cnt, names


def _bs_watch_text(memory: "MemoryEngine") -> str | None:
    parts = _bs_watch_parts(memory)
    if parts is None:
        return None
    cnt, names = parts
    txt = f"{cnt}/{len(_BS_IND_LABEL)} indikator aktif"
    return txt + (f" · {', '.join(names)}" if names else "")


_BULAN_ID: tuple[str, ...] = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
                              "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _pemicu_bar(n: int) -> str:
    k = BLACK_SWAN_TRIGGER_COUNT
    n = min(max(int(n), 0), k)
    return "█" * n + "░" * (k - n)


def _fund8_pct(rate_hourly: float) -> str:
    """Rate funding HL per JAM → teks persen per 8 jam (satuan yang sama dengan Explorer/Snapshot)."""
    return f"{rate_hourly * FUNDING_PERIODS_PER_8H * 100:+.4f}%"


def _bs_funding_flip_text(rd: dict) -> str:
    """Funding berbalik arah itu indikator biner (tanda berubah atau tidak), jadi '% dari ambang'
    selalu 0 saat tanda sama dan menyesatkan. Tampilkan statusnya + funding sekarang."""
    now, prev = rd.get("now"), rd.get("prev")
    if now is None or prev is None:  # reading lama (tanpa nilai mentah): jangan karang angka
        return "belum berbalik" if float(rd.get("value") or 0.0) <= 0 else "berbalik arah"
    now, prev = float(now), float(prev)
    if not bool(rd.get("sign_flip")):
        return f"belum berbalik · {_fund8_pct(now)} per 8 jam"
    qualifies = float(rd.get("value") or 0.0) > float(rd.get("threshold") or 0.0)
    path = f"{_fund8_pct(prev)} → {_fund8_pct(now)}"
    if qualifies:
        return f"berbalik {path} per 8 jam"
    return f"berbalik tipis {path} · di bawah ambang"


def _bs_reading_text(key: str, rd: dict) -> str:
    """Seberapa dekat indikator ke ambang, dalam satuan yang mudah dibaca."""
    v = float(rd.get("value") or 0.0)
    t = float(rd.get("threshold") or 0.0)
    if t <= 0:
        return "ambang belum ada"
    if key == "funding_flip":
        return _bs_funding_flip_text(rd)
    if key == "mega_cascade":
        return f"{_usd_short(v)} dari ambang {_usd_short(t)}"
    if key == "cross_exchange_div":
        return f"selisih {v * 100:.2f}% / {t * 100:.0f}%"
    if key == "correlation_break":
        return f"{v * 100:.1f}% / {t * 100:.0f}% (1h)"
    return f"{v / t * 100:.0f}% dari ambang"


def _fmt_blackswan(memory: "MemoryEngine") -> str:
    """Kartu Black Swan: mode + grid 7 indikator + arti mode + event terakhir.

    Mode jarang aktif (butuh ≥3/7 ekstrem). Saat NORMAL tetap informatif.
    """
    mode = memory.get_runtime("black_swan_mode", "0") or "0"
    started = memory.get_runtime("black_swan_started_at") or "—"
    watch = _bs_watch_text(memory) if mode not in ("1", "ELEVATED") else None

    # Last eval (semua mode) — grid indikator
    active_inds: set[str] = set()
    readings: dict = {}
    readings_present = False
    eval_count = 0
    eval_ts = None
    try:
        raw = memory.get_runtime("black_swan_last_eval") or ""
        if raw:
            d = json.loads(raw)
            active_inds = {str(x) for x in (d.get("indicators") or [])}
            readings_present = "readings" in d
            readings = dict(d.get("readings") or {})
            eval_count = int(d.get("count") or len(active_inds))
            eval_ts = _parse_ts(d.get("ts") or "")
    except Exception:
        pass
    # Fallback WATCH payload
    if not active_inds and watch:
        parts = _bs_watch_parts(memory)
        if parts:
            eval_count, names = parts
            # names already human labels — map reverse skip; use watch only for count

    if mode == "1":
        label = "🚨 ACTIVE"
        tip_lines = [
            "• Pipeline ketat — hanya setup kualitas tinggi",
            f"• Sinyal aktif diperpanjang +{BS_SIGNAL_EXTEND_H:g} jam",
            f"• Tahan min {BLACK_SWAN_MIN_DURATION_H:g} jam sebelum auto-clear",
        ]
    elif mode == "ELEVATED":
        label = "⚠️ ELEVATED"
        tip_lines = [
            "• Prefer Tier A saja",
            "• Kurangi size / frekuensi entry",
            f"• {eval_count} indikator ekstrem aktif" if eval_count else "• Beberapa indikator ekstrem aktif",
        ]
    elif watch or eval_count == 1:
        label = "👀 WATCH"
        detail = watch or (f"{eval_count}/7 indikator" if eval_count else "1 indikator")
        tip_lines = [
            f"• Pantau: {detail}",
            "• Belum mode risiko — pipeline normal",
        ]
    else:
        label = "✅ NORMAL"
        tip_lines = [
            "• Kondisi risk aman — pipeline penuh",
            f"• Trigger butuh ≥{BLACK_SWAN_TRIGGER_COUNT} dari 7 indikator ekstrem",
            "• Event jarang (desain fail-safe, bukan harian)",
        ]

    since = "—"
    if started and started != "—":
        dt = _parse_ts(started)
        if dt is not None:
            _t = dt.astimezone(WIB)
            since = f"{_t.day:02d} {_BULAN_ID[_t.month - 1]} {_t:%H:%M} WIB"
        else:
            since = str(started)[:19]

    lines = [
        _ui_header("🚨", "Black Swan"),
        "Radar ekstrem · 7 indikator",
        "",
        f"• Mode: {label}",
        f"• Sejak: {since}",
        f"• Aktif: {eval_count}/7 indikator",
        f"• Pemicu: {_pemicu_bar(eval_count)} {eval_count}/{BLACK_SWAN_TRIGGER_COUNT} untuk aktif",
        "",
        "▸ Indikator",
    ]
    # Fixed order matching evaluate_black_swan_indicators
    order = [
        "atr_spike", "volume_cascade", "correlation_break",
        "funding_flip", "mega_cascade", "cross_exchange_div", "news_spike",
    ]
    for key in order:
        lab = _BS_IND_LABEL.get(key, key)
        on = key in active_inds
        mark = "🔴" if on else "⚪"
        rd = readings.get(key)
        if rd:
            lines.append(f"{mark}  {lab} · {_bs_reading_text(key, rd)}")
        elif readings_present:
            lines.append(f"{mark}  {lab} · data belum cukup")
        else:
            lines.append(f"{mark}  {lab}")

    lines += ["", "▸ Arti mode"] + tip_lines

    if eval_ts is not None:
        lines += ["", f"▸ Evaluasi terakhir · {eval_ts.astimezone(WIB):%H:%M} WIB"]

    if memory._conn:
        try:
            row = memory._conn.execute(
                "SELECT triggered_indicators, trigger_count, started_at "
                "FROM blackswan_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                raw_inds = row["triggered_indicators"] or "[]"
                try:
                    ind_list = json.loads(raw_inds) if isinstance(raw_inds, str) else list(raw_inds)
                except Exception:
                    ind_list = [str(raw_inds)]
                human = ", ".join(_BS_IND_LABEL.get(str(x), str(x)) for x in ind_list) or "—"
                at = str(row["started_at"] or "—")[:19]
                lines.extend([
                    "",
                    "▸ Event terakhir (log)",
                    f"• {human}",
                    f"• Count {row['trigger_count']} · {at}",
                ])
        except Exception:
            pass
    return "\n".join(lines)



_SETUP_SHORT: dict[str, str] = {
    "TREND_CONTINUATION": "Trend",
    "FUNDING_SQUEEZE": "Funding",
    "COMPRESSION": "Compression",
    "CASCADE_SCALP": "Cascade",
}
_REASON_SHORT: dict[str, str] = {
    "arm_timeout": "no-fill", "missed_entry": "missed",
    "TIME_STOP": "stall", "time_stop": "stall",
    "expired": "exp", "SL": "SL", "TP1": "TP1", "TP2": "TP2",
    "no_candles": "no-data", "truncated": "no-data", "bad_entry": "bad-entry",
}
_OUTCOME_ICON: dict[str, str] = {"PROFIT": "✅", "LOSS": "🛑", "PARTIAL": "🎯", "NEUTRAL": "⏱"}


def _setup_label(setup) -> str:
    key = str(setup or "?").upper()
    return _SETUP_SHORT.get(key, key.replace("_", " ").title())


def _dur_text(hours: float) -> str:
    """Durasi ringkas: jam di bawah 24 jam, hari selebihnya."""
    return f"{hours:.1f} jam" if hours < 24 else f"{hours / 24:.1f} hari"


def _dir_arrow(direction) -> str:
    d = str(direction or "").upper()
    return "🟢" if d == "LONG" else ("🔴" if d == "SHORT" else "⚪")


def _journal_excursion_line(r) -> str | None:
    """Detail satu trade tertutup: MFE/MAE dan lama posisi tahan (armed → resolved)."""
    parts: list[str] = []
    mfe, mae = r["mfe_r"], r["mae_r"]
    if mfe is not None and mae is not None:
        parts.append(f"MFE +{float(mfe):.2f}R · MAE -{float(mae):.2f}R")
    start = _parse_ts(r["armed_at"])
    end = _parse_ts(r["resolved_at"])
    if start is not None and end is not None and end >= start:
        parts.append(f"tahan {_dur_text((end - start).total_seconds() / 3600.0)}")
    return ("   " + " · ".join(parts)) if parts else None


def _fmt_journal(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    armed = [r for r in rows if str(r.get("state") or "").upper() != "PENDING"]
    pending = [r for r in rows if str(r.get("state") or "").upper() == "PENDING"]
    armed.sort(key=lambda r: (0 if r.get("tp1_hit") else 1, str(r.get("symbol") or "")))
    tp1_n = sum(1 for r in armed if r.get("tp1_hit"))
    lines = [
        _ui_header("📓", "Journal"),
        f"Aktif {len(armed)} · TP1 kena {tp1_n} · pending {len(pending)}",
    ]

    if armed:
        lines += ["", "▸ Aktif"]
        for r in armed[:6]:
            hit = bool(r.get("tp1_hit"))
            tp1_tag = " · TP1 ✅" if hit else ""
            sl = "BE" if hit else _ui_px(r.get("stop_loss"))
            lines.append(f"{_dir_arrow(r.get('direction'))} {r.get('symbol')} {str(r.get('direction') or '').upper()}{tp1_tag}")
            lines.append(
                f"   Entry {_ui_px(r.get('entry_zone_low'))}–{_ui_px(r.get('entry_zone_high'))} · SL {sl}"
            )
            lines.append(
                f"   TP1 {_ui_px(r.get('tp1'))} · TP2 {_ui_px(r.get('tp2'))}"
            )
        if len(armed) > 6:
            lines.append(f"+{len(armed) - 6} lagi · lihat Radar")

    if pending:
        lines += ["", "▸ Pending"]
        for r in pending[:4]:
            lines.append(
                f"{_dir_arrow(r.get('direction'))} {r.get('symbol')} {str(r.get('direction') or '').upper()}"
                f" · entry {_ui_px(r.get('entry_zone_low'))}–{_ui_px(r.get('entry_zone_high'))}"
            )

    if memory._conn:
        try:
            recent = memory._conn.execute(
                "SELECT s.symbol, s.direction, s.outcome, s.realized_rr, s.resolved_at, "
                "s.setup_type, s.resolve_reason, s.armed_at, "
                "e.mfe_r, e.mae_r "
                "FROM signal_history s "
                "LEFT JOIN signal_excursion e ON e.signal_id = s.signal_id "
                "WHERE s.outcome IS NOT NULL "
                "AND COALESCE(s.is_shadow, 0) = 0 AND COALESCE(s.is_simulated, 0) = 0 "
                f"ORDER BY s.resolved_at DESC LIMIT {RECENT_CLOSED_LIMIT}"
            ).fetchall()
            if recent:
                lines += ["", "▸ Terakhir ditutup"]
                for r in recent:
                    oc = str(r["outcome"] or "")
                    icon = _OUTCOME_ICON.get(oc, "•")
                    raw_reason = str(r["resolve_reason"] or "")
                    lookup = raw_reason[7:] if raw_reason.startswith("shadow_") else raw_reason
                    short = _REASON_SHORT.get(lookup, "")
                    if not short and lookup:
                        short = next((v for k, v in _REASON_SHORT.items() if lookup.startswith(k)), lookup[:12])
                    if oc == "NEUTRAL":
                        result = short or "tidak terisi"
                    elif r["realized_rr"] is not None:
                        result = f"{float(r['realized_rr']):+.2f}R"
                    else:
                        result = oc.lower()
                    age = ""
                    dt = _parse_ts(r["resolved_at"])
                    if dt is not None:
                        age = f" · {_dur_text((now_utc() - dt).total_seconds() / 3600.0)} lalu"
                    lines.append(
                        f"{icon} {r['symbol']} {str(r['direction'] or '').upper()} · {result}"
                        f" · {_setup_label(r['setup_type'])}{age}"
                    )
                    detail = _journal_excursion_line(r)
                    if detail:
                        lines.append(detail)
        except Exception:  # noqa: BLE001 — jurnal tetap tampil walau bagian riwayat gagal
            pass
    return "\n".join(lines).rstrip()


def _parse_fs_raw_from_reasoning(reasoning: str) -> float | None:
    """Ambil raw= dari reasoning FUNDING_SQUEEZE. None jika tidak ada/parse gagal."""
    if not reasoning:
        return None
    m = re.search(r"\braw=([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", str(reasoning))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _bucket_text(sub: list) -> str:
    n = len(sub)
    if not n:
        return "belum ada"
    w = sum(1 for r in sub if r["outcome"] == "PROFIT")
    p = sum(1 for r in sub if r["outcome"] == "PARTIAL")
    rrs = []
    for r in sub:
        try:
            if r["realized_rr"] is not None:
                rrs.append(float(r["realized_rr"]))
        except (TypeError, ValueError):
            pass
    e = (sum(rrs) / len(rrs)) if rrs else 0.0
    wr = (w + PARTIAL_WEIGHT * p) / n * 100.0
    flag = " · sampel kecil" if n < 10 else ""
    return f"E {e:+.2f}R · WR {wr:.0f}% · n={n}{flag}"


def _fs_audit_lines(memory: "MemoryEngine", *, limit: int = 12) -> list[str]:
    """Funding squeeze: edge per arah dan per magnitude (trade terbukti, NEUTRAL dikecualikan)."""
    out = ["▸ Funding squeeze"]
    if not memory._conn:
        out.append("• DB tidak tersedia")
        return out
    try:
        rows = list(memory._conn.execute(
            "SELECT symbol, direction, tier, outcome, realized_rr, reasoning, "
            "created_at FROM signal_history "
            "WHERE setup_type = 'FUNDING_SQUEEZE' "
            "AND IFNULL(is_shadow, 0) = 0 "
            "AND outcome IN ('PROFIT', 'LOSS', 'PARTIAL') "
            "ORDER BY COALESCE(resolved_at, created_at) DESC LIMIT 40"
        ).fetchall())
    except Exception as e:  # noqa: BLE001
        out.append(f"• Gagal baca data ({type(e).__name__})")
        return out
    if not rows:
        out.append("• Belum ada trade terbukti")
        return out

    by_dir: dict[str, list] = {"LONG": [], "SHORT": []}
    by_mag: dict[str, list] = {"FLAT": [], "TINGGI": [], "EKSTRIM": [], "?": []}
    for r in rows:
        d = str(r["direction"] or "").upper()
        if d in by_dir:
            by_dir[d].append(r)
        raw = _parse_fs_raw_from_reasoning(str(r["reasoning"] or ""))
        if raw is None:
            by_mag["?"].append(r)
        else:
            mag, _ = funding_magnitude_label(raw)
            by_mag.setdefault(mag, []).append(r)

    out.append(f"• Long: {_bucket_text(by_dir['LONG'])}")
    out.append(f"• Short: {_bucket_text(by_dir['SHORT'])}")
    out.append(f"• Ekstrem: {_bucket_text(by_mag.get('EKSTRIM', []))}")
    out.append(f"• Tinggi: {_bucket_text(by_mag.get('TINGGI', []))}")
    out.append("• Gate: hanya Tinggi/Ekstrem, Flat ditolak")
    try:
        streak = memory.consecutive_losses(5)
        out.append(f"• Loss streak {streak:.1f} · circuit 5.0")
    except Exception:  # noqa: BLE001
        pass
    return out


def _setup_expectancy_lines(memory: "MemoryEngine") -> list[str]:
    """Expectancy per setup dari trade terbukti (NEUTRAL dikecualikan)."""
    out = ["▸ Per setup"]
    try:
        trades = load_resolved_trades(memory, live_only=True)
    except Exception as e:  # noqa: BLE001 — tampilkan kegagalan, jangan diam
        out.append(f"• Data belum bisa dibaca ({type(e).__name__})")
        return out
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(str(t.setup_type or "?"), []).append(t)
    rows = []
    for setup, sub in groups.items():
        m = compute_strategy_metrics(sub)
        if m.n_trades:
            rows.append((m.expectancy_r, setup, m))
    if not rows:
        out.append("• Belum ada trade terbukti")
        return out
    for exp, setup, m in sorted(rows, key=lambda x: -x[0]):
        flag = " · sampel kecil" if m.n_trades < 10 else ""
        out.append(
            f"• {_setup_label(setup)}: E {exp:+.2f}R · WR {m.win_rate * 100:.0f}% · n={m.n_trades}{flag}"
        )
    return out


def _tier_compare_lines(memory: "MemoryEngine") -> list[str]:
    """Tier A/B/C dari trade terbukti (NEUTRAL dikecualikan)."""
    out = ["▸ Tier"]
    try:
        trades = load_resolved_trades(memory, live_only=True)
    except Exception as e:  # noqa: BLE001
        out.append(f"• Data tier belum bisa dibaca ({type(e).__name__})")
        return out
    for tier in ("A", "B", "C"):
        sub = [t for t in trades if t.tier == tier]
        m = compute_strategy_metrics(sub) if sub else None
        if m is None or m.n_trades == 0:
            out.append(f"• {tier}: belum ada trade terbukti")
            continue
        flag = " · sampel kecil" if m.n_trades < 10 else ""
        out.append(
            f"• {tier}: WR {m.win_rate * 100:.0f}% · E {m.expectancy_r:+.2f}R · n={m.n_trades}{flag}"
        )
    return out


def _tier_setup_cross_lines(memory: "MemoryEngine") -> list[str]:
    """E per tier × setup. Hanya sel dengan n ≥ 5 supaya angka tidak menyesatkan."""
    out = ["▸ Tier × setup (n ≥ 5)"]
    try:
        trades = load_resolved_trades(memory, live_only=True)
    except Exception as e:  # noqa: BLE001
        out.append(f"• Data silang belum bisa dibaca ({type(e).__name__})")
        return out
    groups: dict[tuple[str, str], list] = {}
    for t in trades:
        key = (str(t.tier or "?").upper(), str(t.setup_type or "?"))
        groups.setdefault(key, []).append(t)
    shown = 0
    hidden = 0
    for (tier, setup), sub in sorted(groups.items()):
        if len(sub) < 5:
            hidden += 1
            continue
        m = compute_strategy_metrics(sub)
        out.append(
            f"• {tier} · {_setup_label(setup)}: E {m.expectancy_r:+.2f}R · WR {m.win_rate * 100:.0f}% · n={m.n_trades}"
        )
        shown += 1
    if shown == 0:
        out.append("• Belum ada sel dengan n ≥ 5")
    if hidden:
        out.append(f"• {hidden} sel lain disembunyikan (n < 5)")
    return out


def _fmt_performance(memory: "MemoryEngine") -> str:
    lines = [_ui_header("📊", "Performance")]
    try:
        trades = load_resolved_trades(memory, live_only=True)
    except Exception:  # noqa: BLE001
        trades = []
    if not trades:
        lines += ["", "Belum ada trade terbukti.", "Akan terisi setelah sinyal resolved."]
        return "\n".join(lines)
    m = compute_strategy_metrics(trades)
    lines += [f"{m.n_trades} trade · WR {m.win_rate * 100:.0f}% · E {m.expectancy_r:+.2f}R", ""]
    lines += _setup_expectancy_lines(memory) + [""]
    lines += _tier_compare_lines(memory) + [""]
    lines += _tier_setup_cross_lines(memory) + [""]
    lines += _fs_audit_lines(memory)
    return "\n".join(lines).rstrip()



def _kb_shadow_back() -> dict:
    """Dari detail gate → balik ke Shadow, bukan Menu."""
    return {"inline_keyboard": [
        [{"text": "⬅️ Shadow", "callback_data": "menu:shadow"}],
    ]}


_SHADOW_WINDOW_DAYS = 30
_SHADOW_INFO_MIN = 0.20  # proven/n di bawah ini = data belum cukup untuk menilai
_SHADOW_RESOLVED = ("PROFIT", "LOSS", "PARTIAL")
_SHADOW_CONF_RE = re.compile(r"^confirms_fail:real(\d+)_conf(\d+)_n(\d+)$")
_SHADOW_GATE_SHORT = {
    "htf_down_vs_long": "HTF turun, lawan LONG",
    "htf_up_vs_short": "HTF naik, lawan SHORT",
    "regime_oppose": "Regime lawan arah",
    "sweep_oppose": "Sweep lawan arah",
}
_SHADOW_VERDICT_ICON = {"ketat": "🔴", "benar": "🟢", "abu": "⚠️", "kurang": "⚪"}
_SHADOW_VERDICT_TEXT = {
    "ketat": "terlalu ketat, pertimbangkan longgarkan",
    "benar": "gate benar, pertahankan",
    "abu": "abu-abu, pantau",
    "kurang": "data belum cukup",
}
_SHADOW_OUTCOME_ICON = {"PROFIT": "✅", "LOSS": "🛑", "PARTIAL": "🎯", "NEUTRAL": "⏱"}


def _gate_short(reason: str) -> str:
    """Label gate yang bisa dibaca trader. Tidak pernah memotong angka di dalamnya."""
    r = str(reason or "")
    if r in _SHADOW_GATE_SHORT:
        return _SHADOW_GATE_SHORT[r]
    m = _SHADOW_CONF_RE.match(r)
    if m:
        return f"Konfirmasi {m.group(2)}/{m.group(3)} agen setuju"
    return r.replace("_", " ") or "tidak diketahui"


def _shadow_gate_stats(memory: "MemoryEngine", days: int = _SHADOW_WINDOW_DAYS) -> list[dict]:
    """Statistik per gate dari shadow ter-resolve. Diurutkan dari dampak terbesar."""
    if not getattr(memory, "_conn", None):
        return []
    cutoff = (now_utc() - timedelta(days=days)).isoformat()
    rows = memory._conn.execute(
        "SELECT reject_reason, outcome, realized_rr FROM signal_history "
        "WHERE COALESCE(is_shadow,0)=1 AND outcome IS NOT NULL AND created_at >= ?",
        (cutoff,),
    ).fetchall()
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(str(r["reject_reason"] or "unknown"), []).append(r)
    out: list[dict] = []
    for reason, items in buckets.items():
        n = len(items)
        proven = [i for i in items if i["outcome"] in _SHADOW_RESOLVED]
        d = {"reason": reason, "n": n, "proven": len(proven),
             "wr": None, "exp": None, "cost": 0.0, "verdict": "kurang"}
        # Ambang ganda: rasio terbukti cukup DAN jumlah terbukti minimal MIN_STAT_SAMPLE
        if len(proven) >= MIN_STAT_SAMPLE and len(proven) / n >= _SHADOW_INFO_MIN:
            wins = sum(1 for i in proven if i["outcome"] == "PROFIT")
            parts = sum(1 for i in proven if i["outcome"] == "PARTIAL")
            wr = (wins + 0.5 * parts) / len(proven) * 100
            rrs = [max(-8.0, min(8.0, float(i["realized_rr"])))
                   for i in proven if i["realized_rr"] is not None]
            exp = sum(rrs) / len(rrs) if rrs else 0.0
            if exp > 0.15 and wr > 55:
                verdict = "ketat"      # sinyal yang ditolak ternyata menang → gate terlalu ketat
            elif exp < -0.10:
                verdict = "benar"      # sinyal yang ditolak memang rugi → gate benar
            else:
                verdict = "abu"
            d.update(wr=wr, exp=exp, cost=n * exp, verdict=verdict)
        out.append(d)
    out.sort(key=lambda d: -abs(d["cost"]))
    return out


def _shadow_exp_window(memory: "MemoryEngine", since_iso: str) -> dict[str, tuple[int, float]]:
    rows = memory._conn.execute(
        "SELECT reject_reason, realized_rr FROM signal_history "
        "WHERE COALESCE(is_shadow,0)=1 AND outcome IN ('PROFIT','LOSS','PARTIAL') "
        "AND created_at >= ?",
        (since_iso,),
    ).fetchall()
    acc: dict[str, list[float]] = {}
    for r in rows:
        acc.setdefault(str(r["reject_reason"] or "unknown"), []).append(
            max(-8.0, min(8.0, float(r["realized_rr"] or 0.0)))
        )
    return {k: (len(v), sum(v) / len(v)) for k, v in acc.items()}


def _shadow_drift(memory: "MemoryEngine", short_days: int = 7, long_days: int = 30,
                  min_n: int = 5) -> list[tuple[str, float, float, str]]:
    """Gate yang E[R]-nya bergeser antara 7 hari dan 30 hari (hanya yang sample-nya cukup)."""
    if not getattr(memory, "_conn", None):
        return []
    now = now_utc()
    short = _shadow_exp_window(memory, (now - timedelta(days=short_days)).isoformat())
    long_ = _shadow_exp_window(memory, (now - timedelta(days=long_days)).isoformat())
    out: list[tuple[str, float, float, str, float]] = []
    for reason in set(short) & set(long_):
        ns, es = short[reason]
        nl, el = long_[reason]
        if ns < min_n or nl < min_n:
            continue
        delta = es - el
        if delta < -0.20:
            word = "makin benar"    # E rejected makin rugi → gate makin tepat
        elif delta > 0.20:
            word = "makin ketat"    # E rejected makin untung → gate makin ketat
        else:
            word = "stabil"
        out.append((reason, es, el, word, abs(delta)))
    out.sort(key=lambda x: -x[4])
    return [(r, es, el, w) for r, es, el, w, _ in out[:5]]


def _shadow_row(d: dict) -> str:
    icon = _SHADOW_VERDICT_ICON[d["verdict"]]
    head = f"{icon} {_gate_short(d['reason'])}"
    if d["exp"] is None:
        body = f"   n={d['n']} · {d['proven']} terbukti · {_SHADOW_VERDICT_TEXT['kurang']}"
    else:
        body = (f"   n={d['n']} · WR {d['wr']:.0f}% · E {d['exp']:+.2f}R · "
                f"{_SHADOW_VERDICT_TEXT[d['verdict']]}")
    return head + "\n" + body


def _kb_shadow_gates(memory: "MemoryEngine") -> dict:
    """Satu gate per baris (label penuh, bisa dibaca) + verdict di ikon. Max 6."""
    buttons: list[list[dict]] = []
    try:
        stats = _shadow_gate_stats(memory)[:6]
    except Exception:  # noqa: BLE001
        stats = []
    for d in stats:
        key = d["reason"][:SHADOW_GATE_KEY_MAX]
        label = f"{_SHADOW_VERDICT_ICON[d['verdict']]} {_gate_short(d['reason'])} · {d['n']}"
        buttons.append([{"text": label, "callback_data": f"shadow:gate:{key}"}])
    buttons.append([{"text": "⬅️ Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": buttons}


def _fmt_shadow_gate_detail(memory: "MemoryEngine", gate: str) -> str:
    """Detail satu gate: verdict, angka, dan 5–8 shadow terakhir. Kode asli ditampilkan utuh."""
    lines = [f"🧬 Shadow · {_gate_short(gate)}", f"Kode gate: {gate}"]
    if not getattr(memory, "_conn", None):
        lines.append("Database tidak tersedia.")
        return "\n".join(lines)
    try:
        stat = next((d for d in _shadow_gate_stats(memory)
                     if d["reason"] == gate or d["reason"].startswith(gate)), None)
    except Exception:  # noqa: BLE001
        stat = None
    if stat is not None:
        lines.append("")
        if stat["exp"] is None:
            lines.append(f"n={stat['n']} · {stat['proven']} terbukti")
        else:
            lines.append(f"n={stat['n']} · WR {stat['wr']:.0f}% · E {stat['exp']:+.2f}R")
        lines.append(f"{_SHADOW_VERDICT_ICON[stat['verdict']]} {_SHADOW_VERDICT_TEXT[stat['verdict']]}")
    try:
        rows = memory._conn.execute(
            "SELECT symbol, direction, outcome, realized_rr FROM signal_history "
            "WHERE COALESCE(is_shadow,0)=1 "
            "AND (reject_reason = ? OR substr(reject_reason, 1, ?) = ?) "
            "AND outcome IS NOT NULL "
            f"ORDER BY resolved_at DESC LIMIT {RECENT_CLOSED_LIMIT}",
            (gate, len(gate), gate),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        lines += ["", f"Gagal memuat riwayat · {type(e).__name__}"]
        return "\n".join(lines)
    if not rows:
        lines += ["", "Belum ada shadow ter-resolve untuk gate ini."]
    else:
        lines += ["", f"{len(rows)} shadow terakhir"]
        for r in rows:
            oc = str(r["outcome"] or "")
            rr = r["realized_rr"]
            rr_s = f" · {float(rr):+.2f}R" if rr is not None else ""
            lines.append(
                f"{_SHADOW_OUTCOME_ICON.get(oc, '•')} {r['symbol']} "
                f"{str(r['direction'] or '').upper()} · {oc}{rr_s}"
            )
    lines += ["", "Counterfactual, bukan sinyal real."]
    return "\n".join(lines)


def _fmt_shadow_report(memory: "MemoryEngine") -> str:
    """Shadow Lab: kesimpulan dulu, lalu gate paling berpengaruh, tren, dan koin regret."""
    try:
        stats = _shadow_gate_stats(memory)
    except Exception as e:  # noqa: BLE001
        return f"🧬 Shadow Lab\nTidak bisa memuat data · {type(e).__name__}"
    if not stats:
        return (f"🧬 Shadow Lab · {_SHADOW_WINDOW_DAYS} hari\n"
                "Belum ada shadow yang ter-resolve.\n"
                "Laporan muncul setelah ada sample.")
    total = sum(d["n"] for d in stats)
    cnt = {k: 0 for k in _SHADOW_VERDICT_ICON}
    for d in stats:
        cnt[d["verdict"]] += 1
    ketat_cost = sum(d["cost"] for d in stats if d["verdict"] == "ketat")
    lines = [
        f"🧬 Shadow Lab · {_SHADOW_WINDOW_DAYS} hari",
        f"{total:,} sinyal ditolak sudah ter-resolve".replace(",", "."),
        "Counterfactual: hasil seandainya sinyal yang ditolak tetap diambil. Bukan sinyal real.",
        "",
        "▸ Kesimpulan",
    ]
    if cnt["ketat"]:
        lines.append(f"🔴 {cnt['ketat']} gate terlalu ketat, sekitar +{ketat_cost:.0f}R potensi terlewat")
    if cnt["benar"]:
        lines.append(f"🟢 {cnt['benar']} gate terbukti benar")
    if cnt["abu"]:
        lines.append(f"⚠️ {cnt['abu']} gate abu-abu, pantau")
    if cnt["kurang"]:
        lines.append(f"⚪ {cnt['kurang']} gate data belum cukup")
    lines += ["", "▸ Gate paling berpengaruh", "\n\n".join(_shadow_row(d) for d in stats[:6])]
    try:
        drift = _shadow_drift(memory)
    except Exception:  # noqa: BLE001
        drift = []
    if drift:
        lines += ["", "▸ Tren 7 hari vs 30 hari"]
        for reason, es, el, word in drift:
            lines.append(f"· {_gate_short(reason)}: E {es:+.2f}R (7 hari) vs {el:+.2f}R (30 hari), {word}")
    try:
        regret = symbol_regret_score(memory, days=_SHADOW_WINDOW_DAYS, top_n=5)
    except Exception:  # noqa: BLE001
        regret = []
    regret = [x for x in regret if x[2] >= MIN_STAT_SAMPLE]
    if regret:
        lines += ["", "▸ Koin paling banyak alpha terbuang"]
        for sym, total_r, n in regret:
            lines.append(f"· {sym}  +{total_r:.1f}R · n={n}")
    text_out = "\n".join(lines)
    if len(text_out) > 3500:
        cut = text_out.rfind("\n", 0, 3490)
        if cut < 2000:
            cut = 3490
        text_out = text_out[:cut] + "\n…"
    return text_out


def _fmt_accuracy(memory: "MemoryEngine") -> str:
    lines = [_ui_header("🎯", "Accuracy Tracker")]
    try:
        since = now_utc() - timedelta(days=30)
        trades = load_resolved_trades(memory, live_only=True, since=since)
        m = compute_strategy_metrics(trades)
    except Exception as e:
        logger.debug("accuracy fmt: %s", type(e).__name__)
        lines.append("Belum ada outcome.")
        return "\n".join(lines)
    if m.n_trades == 0 and m.n_neutral == 0:
        lines.append("Belum ada outcome (30d).")
        return "\n".join(lines)
    if m.n_trades == 0 and m.n_neutral > 0:
        lines.append(
            f"Tidak ada trade fill dalam 30d (NEUTRAL {m.n_neutral})."
        )
        lines.append("Cek arm_timeout / entry zone vs volatilitas.")
        return "\n".join(lines)
    pf_s = "∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    wr_pct = m.win_rate * 100.0
    bar_n = max(0, min(10, int(round(wr_pct / 10))))
    bar = "█" * bar_n + "░" * (10 - bar_n)
    sample_badge = "⚠️ sample kecil" if m.n_trades < 10 else (
        "✅ sample cukup" if m.n_trades >= 30 else "· sample sedang"
    )
    wr_icon = "🟢" if wr_pct >= 50 else ("🟡" if wr_pct >= 40 else "🔴")
    # Status line
    if m.expectancy_r >= 0.15 and wr_pct >= 45:
        status = "✅ on track"
    elif m.expectancy_r >= 0:
        status = "🟡 positif, di bawah target"
    else:
        status = "🔴 negatif — audit setup"
    lines.extend([
        _ui_kv("Window", "30 hari"),
        _ui_kv("Proven", f"{m.n_trades}   NEUTRAL {m.n_neutral}   {sample_badge}"),
        f"W{m.wins} / L{m.losses} / P{m.partials}",
        "",
        _ui_kv("Status", status),
        _ui_kv("Win rate", f"{wr_icon} {wr_pct:.1f}%  [{bar}]"),
        _ui_kv("Expectancy", f"{m.expectancy_r:+.3f}R"),
        _ui_kv("Exec (fee)", f"{m.expectancy_exec_r:+.3f}R"),
        _ui_kv("PF", pf_s),
        _ui_kv("MaxDD", f"{m.max_drawdown_r:.2f}R"),
        "",
        _ui_section("Target"),
        "WR >50%  ·  E > +0.15R  ·  PF >1.3",
    ])
    # Loss streak warning
    try:
        streak = memory.consecutive_losses(5)
        if streak >= 5.0:
            lines.append("")
            lines.append(f"⚠️ Loss streak {streak:.1f} — circuit pause aktif")
        elif streak >= 3.0:
            lines.append("")
            lines.append(f"Loss streak {streak:.1f} / 5.0")
    except Exception:
        pass
    if m.n_trades < 10:
        lines.append("")
        lines.append("Sample kecil — jangan overfit.")
    elif m.expectancy_r < 0:
        lines.append("")
        lines.append("Expectancy negatif — audit setup / gate.")
    return "\n".join(lines)


_KILL_ENV_TRUE = ("1", "true", "yes", "on")  # harus sama dengan kill_switch_active() di engine


def kill_switch_env_forced() -> bool:
    """True kalau CRYPTONE_KILL_SWITCH di env memaksa STOP — UI tidak bisa mengubahnya."""
    env = (os.environ.get("CRYPTONE_KILL_SWITCH") or "").strip().lower()
    return env in _KILL_ENV_TRUE


def _kill_since_text(memory: "MemoryEngine") -> str:
    """'sejak 14:05 WIB · 3h 12m' kalau timestamp runtime ada; kosong kalau tidak."""
    try:
        raw = memory.get_runtime("kill_switch_set_at") or ""
        ts = _parse_ts(raw) if raw else None
        if ts is None:
            return ""
        mins = max(0, int((now_utc() - ts).total_seconds() // 60))
        dur = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"
        return f"sejak {_as_wib(ts).strftime('%H:%M')} WIB · {dur}"
    except Exception:
        return ""


def _kill_status_text(memory: "MemoryEngine") -> str:
    """Status kill yang jujur: env dan runtime dilaporkan terpisah."""
    if kill_switch_env_forced():
        return "ON 🛑 · dikunci env CRYPTONE_KILL_SWITCH (ubah di GitHub Actions)"
    if memory.kill_switch_active():
        since = _kill_since_text(memory)
        return f"ON 🛑 · {since}" if since else "ON 🛑"
    return "off"


def _kb_kill_button(memory: "MemoryEngine") -> dict:
    """Tombol kill: target-based (set:kill:on / set:kill:off), terkunci kalau env memaksa ON."""
    if kill_switch_env_forced():
        return {"text": "Kill: ON 🔒 env", "callback_data": "set:kill:locked"}
    if memory.kill_switch_active():
        return {"text": "Kill: ON 🛑", "callback_data": "set:kill:off"}
    return {"text": "Kill: OFF", "callback_data": "set:kill:on"}


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
        ],
        [
            {
                "text": f"{'✅' if setups.get('COMPRESSION', True) else '⏸'} Compress",
                "callback_data": "set:setup:COMPRESSION",
            },
            {
                "text": f"{'✅' if setups.get('TREND_CONTINUATION', True) else '⏸'} Trend",
                "callback_data": "set:setup:TREND_CONTINUATION",
            },
        ],
        [
            {
                "text": (lambda cm: f"{'🔄' if cm == 'exhaustion' else ('🤖' if cm == 'auto' else '➡️')} Cascade · {cm}")(cascade_mode_raw(memory)),
                "callback_data": "set:cascade:toggle",
            },
        ],
        [
            {
                "text": "▶️ Resume signals" if is_paused else "⏸ Pause 1h",
                "callback_data": "set:pause:toggle",
            },
            _kb_kill_button(memory),
        ],
        [
            {"text": "🧮 Position calc", "callback_data": "menu:calc"},
            {"text": "⬅️ Menu", "callback_data": "menu:home"},
        ],
    ]
    return {"inline_keyboard": rows}


def _cascade_setting_label(memory: "MemoryEngine") -> str:
    """'exhaustion (manual)' / 'continuation (default)' / 'auto → exhaustion'.
    Membedakan setelan user dari hasil otomatis, supaya label tidak membingungkan."""
    raw = cascade_mode_raw(memory)
    if raw == "auto":
        return f"auto → {cascade_mode(memory)}"
    explicit = bool((memory.get_runtime("cascade_mode") or "").strip())
    return f"{raw} ({'manual' if explicit else 'default'})"


_DELIVERY_ID: dict[str, str] = {"active": "aktif", "passive": "pasif"}
_SETUP_NAME: tuple[tuple[str, str], ...] = (
    ("FUNDING_SQUEEZE", "Funding squeeze"),
    ("CASCADE_SCALP", "Cascade scalp"),
    ("COMPRESSION", "Compression"),
    ("TREND_CONTINUATION", "Trend continuation"),
)


def _fmt_settings_view(memory: "MemoryEngine") -> str:
    mode = get_delivery_mode(memory)
    setups = memory.get_setup_enabled()
    quiet_txt = (
        f"{QUIET_START_HOUR_WIB:02d}:00–{QUIET_END_HOUR_WIB:02d}:00 "
        f"({'aktif' if QUIET_HOURS_ENABLED else 'mati'})"
    )
    post_loss = (
        f"{POST_LOSS_COOLDOWN_MIN // 60}j {POST_LOSS_COOLDOWN_MIN % 60:02d}m"
        if POST_LOSS_COOLDOWN_MIN >= 60 else f"{POST_LOSS_COOLDOWN_MIN}m"
    )
    lines = [
        _ui_header("⚙️", "Settings"),
        _ui_kv("Mode kirim", _DELIVERY_ID.get(mode, mode)),
        _ui_kv("Kill switch", _kill_status_text(memory)),
        _ui_kv("Status", _session_status_line()),
        _ui_kv("Jam tenang", quiet_txt),
        _ui_kv("Cooldown", f"{SIGNAL_COOLDOWN_MIN}m"),
        _ui_kv("Cooldown loss", post_loss),
        _ui_kv("Maks sinyal aktif", f"{MAX_CONCURRENT_SIGNALS}"),
        _ui_kv("Maks per siklus", f"{MAX_SIGNALS_PER_CYCLE}"),
        _ui_kv("Mode cascade", _cascade_setting_label(memory)),
        _ui_kv("Zona entry", f"{ENTRY_ZONE_ATR_WIDTH:g}× ATR"),
        "",
        _ui_section("Setup"),
    ]
    for key, name in _SETUP_NAME:
        on = setups.get(key, True)
        lines.append(f"{'✅' if on else '⏸'} {name}: {'ON' if on else 'OFF'}")
    lines += [
        "",
        f"Tier B: RR ≥ {TIER_B_MIN_RR:g} & konfirmasi ≥ {TIER_B_MIN_CONFIRMS} · konteks pasar (bukan alpha utama).",
        (
            f"Jam tenang {QUIET_START_HOUR_WIB:02d}–{QUIET_END_HOUR_WIB:02d} WIB: "
            "hanya Tier A dikirim; B/C tetap di DB/Radar (tidak dikirim ulang)."
            if QUIET_HOURS_ENABLED else "Jam tenang mati: semua tier dikirim."
        ),
    ]
    # Circuit breaker per setup (if any still active)
    try:
        for key, name in _SETUP_NAME:
            until = memory.get_runtime(f"setup_paused_until:{key}")
            if until:
                dt = _parse_ts(until)
                if dt is not None and now_utc() < dt:
                    lines.append(f"⏸ {name} di-pause sampai {str(until)[:16]}")
    except Exception:
        pass
    if memory.signals_paused():
        paused = memory.get_runtime("signals_paused_until") or ""
        lines.extend(["", f"⏸ Sinyal di-pause sampai {str(paused)[:19]}"])
    return "\n".join(lines)


def _fmt_calc_help() -> str:
    return "\n".join([
        _ui_header("🧮", "Position Calculator"),
        "Hitung qty dari risk dollar & jarak SL.",
        "",
        "▸ Format",
        "calc RISK ENTRY SL",
        "",
        "▸ Contoh",
        "calc 50 2.85 2.90",
        "  └ risk $50 · entry 2.85 · SL 2.90 → SHORT",
        "calc 50 2.90 2.85",
        "  └ risk $50 · entry 2.90 · SL 2.85 → LONG",
        "",
        "qty = risk / |entry − SL|",
    ])

def _parse_calc_message(text: str) -> str | None:
    """Return formatted calc result or None if not a calc command."""
    t = (text or "").strip()
    if not t or t.split(maxsplit=1)[0].lower() != "calc":
        return None
    parts = t.split()  # whitespace only — no comma replace
    if len(parts) < 4:
        return "Format: calc RISK ENTRY SL\nContoh: calc 50 2.85 2.90"
    try:
        risk = float(parts[1])
        entry = float(parts[2])
        sl = float(parts[3])
    except ValueError:
        return "Angka tidak valid. Contoh: calc 50 2.85 2.90"
    if not (math.isfinite(risk) and math.isfinite(entry) and math.isfinite(sl)):
        return "Angka harus finite (bukan inf/nan). Contoh: calc 50 2.85 2.90"
    if entry <= 0:
        return "Entry harus > 0. Contoh: calc 50 2.85 2.90"
    dist = abs(entry - sl)
    if dist <= 0 or risk <= 0:
        return "Risk dan |entry − SL| harus > 0."
    qty = risk / dist
    notional = qty * entry
    pct = dist / entry * 100.0
    side = "LONG" if sl < entry else "SHORT"
    side_icon = "🟢" if side == "LONG" else "🔴"
    sl_hint = "SL di bawah entry" if side == "LONG" else "SL di atas entry"
    if qty < 0.01:
        qty_s = f"{qty:.8f}".rstrip("0").rstrip(".")
    else:
        qty_s = _ui_px(qty)
    lines = [
        _ui_header("🧮", "Position size"),
        f"{side_icon}  {side}  ·  {sl_hint}",
        "",
        f"• Risk       ${risk:,.2f}",
        f"• Entry      {_ui_px(entry)}",
        f"• Stop       {_ui_px(sl)}",
        f"• Jarak      {_ui_px(dist)}  ({pct:.2f}%)",
        "",
        f"• Qty        {qty_s}",
        f"• Notional   ${notional:,.2f}",
        "",
        "⚠️  Cek leverage & min lot di exchange.",
        "Ketik ulang: calc RISK ENTRY SL",
    ]
    return "\n".join(lines)




def _fmt_count(n: int) -> str:
    """Angka kejadian dengan pemisah ribuan titik (format Indonesia)."""
    return f"{int(n):,}".replace(",", ".")


BREADTH_TOP_N = 3  # koin ramai per sisi yang tampil; teks dan tombol Breadth memakai daftar yang sama


def _breadth_parsed(memory) -> list[tuple[str, float]]:
    """Sampel funding 6 jam sebagai (simbol kanonik, rate per jam). Satu-satunya pintu masuk Breadth."""
    parsed: list[tuple[str, float]] = []
    for r in _latest_funding_rows(memory, 6.0) or []:
        try:
            sym = canon_hl_symbol(str(r["symbol"]))
            rate = float(r["rate"])
        except (TypeError, ValueError, KeyError):
            continue
        if _EXPLORER_SYM_RE.match(sym):
            parsed.append((sym, rate))
    return parsed


def _breadth_thr_pct8(parsed: list[tuple[str, float]]) -> float:
    """Ambang ramai Breadth (% per 8 jam) — sama dengan snapshot (_crowd_thr_pct8)."""
    return _crowd_thr_pct8([r * FUNDING_PERIODS_PER_8H * 100.0 for _, r in parsed])


def _breadth_crowd(parsed: list[tuple[str, float]]):
    """Koin ramai per sisi (|funding per 8 jam| ≥ ambang adaptif), paling padat dulu.

    Return (long, short, jumlah_netral). Entri = (simbol, funding per 8 jam dalam fraksi).
    """
    per8 = [(s, r * FUNDING_PERIODS_PER_8H) for s, r in parsed]
    thr = _breadth_thr_pct8(parsed) / 100.0
    longs = sorted((p for p in per8 if p[1] >= thr), key=lambda p: (-p[1], p[0]))
    shorts = sorted((p for p in per8 if p[1] <= -thr), key=lambda p: (p[1], p[0]))
    return longs, shorts, len(per8) - len(longs) - len(shorts)


def _breadth_liq_top(memory) -> list[tuple[str, float, float]]:
    """Top likuidasi 24 jam (simbol, long USD, short USD) khusus perp inti Hyperliquid.

    Likuidasi Binance untuk aset non-crypto (XAU, SOXL, dll) tidak punya candle di HL,
    jadi tidak boleh jadi tombol Explorer. Fail-soft: tanpa data → [].
    """
    if not hasattr(memory, "get_liquidation_summary"):
        return []
    try:
        s24 = memory.get_liquidation_summary(now_utc() - timedelta(hours=24), top_n=BREADTH_TOP_N)
    except Exception:  # noqa: BLE001 — daftar likuidasi tidak boleh menggagalkan kartu
        return []
    core = set(_engine._HL_UNIVERSE_CANON.values())  # kosong saat startup → jangan buang semua koin
    out: list[tuple[str, float, float]] = []
    for sym, lu, su in s24.get("top", []) or []:
        s_ = canon_hl_symbol(str(sym))
        if not _EXPLORER_SYM_RE.match(s_):
            continue
        if core and s_ not in core:
            continue
        out.append((s_, float(lu), float(su)))
    return out


def _breadth_picks(parsed: list[tuple[str, float]], liq_top) -> list[tuple[str, str]]:
    """Koin yang tampil di Breadth sebagai (simbol, ikon). Teks dan tombol sama-sama dari sini."""
    longs, shorts, _ = _breadth_crowd(parsed)
    picks = [(s, "🔥") for s, _ in longs[:BREADTH_TOP_N]]
    picks += [(s, "❄️") for s, _ in shorts[:BREADTH_TOP_N]]
    seen = {s for s, _ in picks}
    for sym, _lu, _su in liq_top:
        if sym not in seen:
            picks.append((sym, "💥"))
            seen.add(sym)
    return picks


def _breadth_universe_size(memory) -> int | None:
    """Jumlah koin universe dari snapshot terakhir (untuk cakupan). None bila tidak tersedia."""
    try:
        if memory._conn:
            row = memory._conn.execute(
                "SELECT n_assets FROM metric_universe_volume ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
            if row and row["n_assets"]:
                return int(row["n_assets"])
    except Exception:  # noqa: BLE001 — cakupan hanya informasi tambahan
        pass
    return None


def _shown_tie(items: list[tuple[str, float]]) -> bool:
    """True bila dua koin yang tampil punya funding identik (kemungkinan tertahan di batas)."""
    shown = [round(r * 100.0, 4) for _, r in items]
    return len(set(shown)) < len(shown)


def _funding_data_window(rows) -> str | None:
    """Jendela waktu data funding (WIB) dari recorded_at per koin: 'HH:MM' atau 'HH:MM–HH:MM'."""
    ts = []
    for r in rows or []:
        try:
            t = _parse_ts(r["recorded_at"])
        except (KeyError, TypeError):
            continue
        if t is not None:
            ts.append(t)
    if not ts:
        return None
    lo, hi = _wib_label(min(ts), False), _wib_label(max(ts), False)
    return lo if lo == hi else f"{lo}–{hi}"


def _pct_signed(x: float, digits: int = 2) -> str:
    """Persen bertanda. Nilai yang secara pembulatan nol → '0.00%' (bukan '-0.00%')."""
    eps = 0.5 * 10 ** -digits
    return f"{0:.{digits}f}%" if abs(x) < eps else f"{x:+.{digits}f}%"


def _breadth_coin_line(sym: str, r8: float) -> str:
    if r8 >= FUNDING_BAND_EXTREME_8H:
        flag = " 🔥"
    elif r8 <= -FUNDING_BAND_EXTREME_8H:
        flag = " ❄️"
    else:
        flag = ""
    return f"• {sym}  {r8 * 100:+.4f}% per 8 jam{flag}"


def _sisi_bar(pos_n: int, neg_n: int, total: int) -> str:
    """Bar porsi koin funding positif (long) vs negatif (short). Netral = sisanya."""
    if total <= 0:
        return "Semua koin  belum ada data"
    pl = pos_n / total * 100.0
    ps = neg_n / total * 100.0
    filled = max(0, min(12, round(pl / 100.0 * 12)))
    return f"Semua koin  {'█' * filled}{'░' * (12 - filled)}  {pl:.0f}% long · {ps:.0f}% short"


def _breadth_funding_block(parsed: list[tuple[str, float]], n_full: int | None) -> list[str]:
    """Cakupan, tally ramai/netral, median, dan koin ramai per sisi (paling padat dulu)."""
    total = len(parsed)
    head = f"Universe {n_full or total} koin · funding 6 jam"
    if n_full and total != n_full:
        head += f" · {total} terbaca"
    if n_full and total < max(10, n_full * 0.4):
        head += " · sample tipis"
    mag_head, mag_sub = _median_mag([r for _, r in parsed])
    longs, shorts, netral = _breadth_crowd(parsed)
    pos_n = sum(1 for _, r in parsed if r > 0)
    neg_n = sum(1 for _, r in parsed if r < 0)
    out = [
        head,
        _sisi_bar(pos_n, neg_n, total),
        f"Ramai (≥ {_breadth_thr_pct8(parsed):.2f}% per 8 jam): "
        f"🔥 {len(longs)} long · ❄️ {len(shorts)} short · {netral} netral",
        f"Besar funding: {mag_head}",
        f"  └ {mag_sub}",
    ]
    tie_note = "  └ Nilai sama persis di beberapa koin: peringkat kurang bermakna."
    if longs:
        shown = longs[:BREADTH_TOP_N]
        out += ["", "▸ 🔥 Long ramai · rawan long squeeze"]
        out += [_breadth_coin_line(s, r) for s, r in shown]
        if len(longs) > len(shown):
            out.append(f"  └ +{len(longs) - len(shown)} koin lain (lihat Explorer)")
        if _shown_tie(shown):
            out.append(tie_note)
    if shorts:
        shown = shorts[:BREADTH_TOP_N]
        out += ["", "▸ ❄️ Short ramai · rawan short squeeze"]
        out += [_breadth_coin_line(s, r) for s, r in shown]
        if len(shorts) > len(shown):
            out.append(f"  └ +{len(shorts) - len(shown)} koin lain (lihat Explorer)")
        if _shown_tie(shown):
            out.append(tie_note)
    return out


def _breadth_liq_block(memory) -> list[str]:
    """Likuidasi realized (Binance USDT-M) lintas koin: 6 & 24 jam. Kosong bila tidak ada API."""
    if not hasattr(memory, "get_liquidation_summary"):
        return []
    out = ["▸ 💥 Likuidasi realized · Binance USDT-M"]
    try:
        now = now_utc()
        s6 = memory.get_liquidation_summary(now - timedelta(hours=6), top_n=3)
        s24 = memory.get_liquidation_summary(now - timedelta(hours=24), top_n=3)
    except Exception as e:  # noqa: BLE001 — breadth tidak boleh gagal karena likuidasi
        out.append(f"Belum bisa dibaca ({type(e).__name__}).")
        return out
    n6 = int(s6["long_n"]) + int(s6["short_n"])
    n24 = int(s24["long_n"]) + int(s24["short_n"])
    if n24 == 0:
        out.append("Belum ada likuidasi tercatat dalam 24 jam.")
        return out
    out.append(f"• 6 jam: long {_usd_short(s6['long_usd'])} · short {_usd_short(s6['short_usd'])}")
    out.append(f"• 24 jam: long {_usd_short(s24['long_usd'])} · short {_usd_short(s24['short_usd'])}")
    tot6 = s6["long_usd"] + s6["short_usd"]
    if tot6 > 0:
        side = "long" if s6["long_usd"] >= s6["short_usd"] else "short"
        share = max(s6["long_usd"], s6["short_usd"]) / tot6 * 100.0
        out.append(f"• Sisi yang dilikuidasi: {side} ({share:.0f}% dari 6 jam)")
    out.append(f"  └ {_fmt_count(n6)} kejadian (6 jam) · {_fmt_count(n24)} (24 jam)")
    top = _breadth_liq_top(memory)
    if top:
        out += ["", "▸ 🪙 Koin terbanyak dilikuidasi · 24 jam"]
        for sym, lu, su in top:
            out.append(f"• {sym}: long {_usd_short(lu)} · short {_usd_short(su)}")
    return out


HIP3_BREADTH_SHOW = 6  # aset HIP-3 yang tampil di Breadth (teks dan tombol)


def _breadth_hip3_liq(memory) -> dict[str, tuple[float, float]]:
    """Likuidasi Binance 24 jam untuk basis aset HIP-3 (XAU, dst.) → {base: (long, short)}. Fail-soft."""
    if not hasattr(memory, "get_liquidation_summary"):
        return {}
    bases = {r["base"].upper() for r in _engine.hip3_rows(10 ** 6)}
    if not bases:
        return {}
    try:
        s24 = memory.get_liquidation_summary(now_utc() - timedelta(hours=24), top_n=500)
    except Exception:  # noqa: BLE001 — likuidasi opsional untuk blok HIP-3
        return {}
    out: dict[str, tuple[float, float]] = {}
    for sym, lu, su in s24.get("top", []) or []:
        b = str(sym).upper()
        if b in bases:
            out[b] = (float(lu), float(su))
    return out


def _breadth_hip3_block(memory) -> tuple[list[str], list[tuple[str, str]]]:
    """Blok saham & komoditas (HIP-3): teks baris + (key, ikon) untuk tombol. Kosong bila cache belum ada."""
    rows = _engine.hip3_rows(HIP3_BREADTH_SHOW)
    if not rows:
        return [], []
    liq = _breadth_hip3_liq(memory)
    lines = ["▸ 🏛 Saham & komoditas · HIP-3 (funding per 8 jam)"]
    picks: list[tuple[str, str]] = []
    for r in rows:
        pct8 = r["funding"] * FUNDING_PERIODS_PER_8H * 100.0
        line = f"• {r['base']}: {pct8:+.4f}% · vol 24j {_usd_short(r['vlm'])}"
        lu = liq.get(r["base"].upper())
        if lu is not None and (lu[0] + lu[1]) > 0:
            line += f" · likuidasi 24j L {_usd_short(lu[0])} / S {_usd_short(lu[1])}"
        lines.append(line)
        picks.append((_engine.hip3_key(r["coin"]), "🏛"))
    lines.append("  └ funding Hyperliquid · likuidasi Binance USDT-M (aset non-crypto)")
    return lines, picks


def _fmt_breadth(memory: "MemoryEngine") -> str:
    """Scanner kerumunan: ramai per sisi, median, posisi, likuidasi, dan label tombol koin.

    Bukan cockpit — itu Snapshot (regime, skor, makro, F&G).
    """
    parsed = _breadth_parsed(memory)
    window = _funding_data_window(_latest_funding_rows(memory, 6.0) or [])
    subtitle = (f"Scanner kerumunan · data {window} WIB" if window
                else "Scanner kerumunan · belum ada data funding")
    out = [_ui_header("📈", "Market Breadth", subtitle)]
    if parsed:
        out += _breadth_funding_block(parsed, _breadth_universe_size(memory))
    else:
        out.append("Belum ada sample funding (6 jam).")
    out += [""] + _market_read(memory, view="breadth")
    liq = _breadth_liq_block(memory)
    if liq:
        out += [""] + liq
    h3_lines, h3_picks = _breadth_hip3_block(memory)
    if h3_lines:
        out += [""] + h3_lines
    out += ["", "Sumber: funding Hyperliquid (6 jam) · likuidasi Binance USDT-M (24 jam)"]
    if _breadth_picks(parsed, _breadth_liq_top(memory)) or h3_picks:
        out += ["🔍 Ketuk koin untuk detail di Explorer:"]
    return "\n".join(out)


_REASON_STR_RE = re.compile(r"\bstr=([\d.]+)")
_REASON_CAL_RE = re.compile(r"cal ([\d.]+)→([\d.]+) \(([^)]*)\)")
_REASON_SETUP_WR_RE = re.compile(r"setup n=(\d+) wr=([\d.]+)")
_REASON_SYM_WR_RE = re.compile(r"sym n=(\d+) wr=([\d.]+)")


def _reasoning_detail_lines(body: str, trigger: str) -> list[str]:
    """Detail mesin dalam bahasa biasa. Angka mentah (raw/thr) tidak ditampilkan; utuh di DB."""
    meta = _parse_reasoning_meta(body)
    out: list[str] = []
    if trigger:
        out.append(f"Pemicu: {_TRIGGER_LABEL.get(trigger, trigger.replace('_', ' '))}")
    m = _REASON_STR_RE.search(body)
    if m:
        out.append(f"Kekuatan sinyal: {float(m.group(1)):.2f}")
    if meta.get("confirm_count") is not None and meta.get("active_agent_count") is not None:
        real_s = f" ({int(meta['real_count'])} nyata)" if meta.get("real_count") is not None else ""
        out.append(f"Konfirmasi: {meta['confirm_count']} dari {meta['active_agent_count']} agen{real_s}")
    if meta.get("confluence") is not None:
        out.append(f"Confluence struktur: {meta['confluence']}/5")
    if meta.get("context_mult") is not None:
        out.append(f"Konteks pasar: ×{float(meta['context_mult']):.2f}")
    cal = _REASON_CAL_RE.search(body)
    if cal:
        out.append(f"Kalibrasi keyakinan: {cal.group(1)} → {cal.group(2)}")
        inner = _annotate_low_sample(cal.group(3))
        pretty = _REASON_SETUP_WR_RE.sub(
            lambda k: f"setup {k.group(1)} sinyal, winrate {float(k.group(2)) * 100:.0f}%", inner)
        pretty = _REASON_SYM_WR_RE.sub(
            lambda k: f"koin {k.group(1)} sinyal, winrate {float(k.group(2)) * 100:.0f}%", pretty)
        out.append(f"  └ {pretty.replace('; ', ' · ')}")
    return out


def _fmt_reasoning(memory: "MemoryEngine", signal_id: str,
                   ai_text: str | None = None, ai_reason: str = "") -> str:
    """Reasoning satu sinyal: kepala, penjelasan AI (bila ada), lalu detail mesin."""
    if not memory._conn or not signal_id:
        return "Reasoning tidak tersedia."
    row = memory._conn.execute(
        "SELECT symbol, direction, reasoning, setup_type, tier, trigger_type, "
        "sl_basis, entry_basis FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return f"Signal {signal_id[:8]}… tidak ditemukan."
    body = (row["reasoning"] or "").strip()
    sl_b = str(row["sl_basis"] or "")
    en_b = str(row["entry_basis"] or "")
    for tok in body.replace("|", " ").split():  # fallback bila kolom kosong
        if tok.startswith("sl=") and not sl_b:
            sl_b = tok[3:]
        if tok.startswith("entry=") and not en_b:
            en_b = tok[6:]
    sl_h = _sl_basis_label(sl_b, row["direction"])
    en_h = _ENTRY_BASIS_LABEL.get(en_b, en_b or "—")
    setup_h = str(row["setup_type"] or "—").replace("_", " ").lower()
    lines = [
        _ui_header("🧠", f"Reasoning — {row['symbol']} {row['direction']}"),
        f"{_ui_tier_label(row['tier'])} · {setup_h}",
        f"SL: {sl_h} · Entry: {en_h}",
        "",
    ]
    if ai_text:
        lines += [_ui_section("Penjelasan"), ai_text, ""]
    elif ai_reason:
        lines += [f"⚠️ AI belum tersedia — {ai_reason}.", "Detail mesin di bawah.", ""]
    lines.append(_ui_section("Detail"))
    detail = _reasoning_detail_lines(body, str(row["trigger_type"] or ""))
    lines += detail or ["Tidak ada detail tercatat."]
    return "\n".join(lines)


# ----- AI reasoning (Gemini): penjelasan manusiawi di atas data sinyal -----
# Prinsip: AI hanya boleh menyusun ulang FAKTA yang sudah dihitung bot.
# Tidak boleh menambah kondisi pasar (harga, volume, berita, level S/R, dll).
# Output yang melanggar aturan ditolak → user tetap dapat teks mesin (fail-soft).
_AI_REASONING_LOG = logging.getLogger("cryptone.ai_reasoning")
AI_REASONING_FN = "signal_reasoning"
AI_REASONING_MAX_TOKENS = 220
AI_REASONING_TTL_H = 24.0
AI_REASONING_MAX_CHARS = 700
# Batas tunggu panggilan AI. Handler memproses update secara berurutan, jadi
# timeout default 30 detik akan menunda semua tombol lain. Gagal → teks mesin.
AI_REASONING_TIMEOUT_SEC = 12.0
# Istilah kondisi pasar yang tidak ada di fakta sinyal. Jika muncul di output
# dan tidak ada di prompt, output ditolak. Dicocokkan dengan batas kata.
_AI_UNGROUNDED_TERMS: tuple[str, ...] = (
    "volume", "funding", "retest", "breakout", "breakdown", "berita", "news",
    "support", "resistance", "resistensi", "likuidasi", "liquidation",
    "open interest", "whale", "paus", "sentimen", "sentiment", "fed", "etf",
    "harga saat ini", "current price", "pullback", "bounce", "rebound",
)


def _load_signal_facts(memory, signal_id: str) -> dict | None:
    """Kumpulkan fakta sinyal dari DB. None jika sinyal tidak ada / DB tidak siap."""
    if memory is None or not getattr(memory, "_conn", None) or not signal_id:
        return None
    row = memory._conn.execute(
        "SELECT symbol, direction, horizon, tier, setup_type, trigger_type, "
        "entry_zone_low, entry_zone_high, stop_loss, tp1, tp2, rr, confidence, "
        "reasoning, sl_basis, entry_basis "
        "FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return None
    reasoning = row["reasoning"] or ""
    meta = _parse_reasoning_meta(reasoning)
    direction = str(row["direction"] or "").upper()
    sl_b = str(row["sl_basis"] or meta.get("sl_basis_parsed") or "atr")
    en_b = str(row["entry_basis"] or meta.get("entry_basis_parsed") or "atr")
    entry_low = float(row["entry_zone_low"] or 0)
    entry_high = float(row["entry_zone_high"] or 0)
    stop = float(row["stop_loss"] or 0)
    tp1 = float(row["tp1"] or 0)
    tp2 = float(row["tp2"] or 0)
    cal_m = re.search(r"cal [\d.]+→[\d.]+ \(([^)]*)\)", reasoning)
    return {
        "symbol": str(row["symbol"]),
        "direction": direction,
        "horizon": str(row["horizon"] or ""),
        "tier": str(row["tier"] or "?"),
        "setup": str(row["setup_type"] or "").replace("_", " ").lower(),
        "trigger": str(row["trigger_type"] or ""),
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "rr_tp2": float(row["rr"] or 0),
        "rr_tp1": _rr_to_target(direction, entry_low, entry_high, stop, tp1),
        "confidence": float(row["confidence"] or 0),
        "confirm": meta.get("confirm_count"),
        "active": meta.get("active_agent_count"),
        "real": meta.get("real_count"),
        "confluence": meta.get("confluence"),
        "context_mult": meta.get("context_mult"),
        "sl_basis": _sl_basis_label(sl_b, direction),
        "entry_basis": _ENTRY_BASIS_LABEL.get(en_b, en_b),
        "hist_wr": _hist_wr_label(memory, str(row["symbol"]), str(row["setup_type"] or "")),
        "calibration": _annotate_low_sample(cal_m.group(1)) if cal_m else "",
    }


def _build_ai_reasoning_prompt(f: dict) -> str:
    """Prompt berbasis fakta. Setiap baris fakta berasal dari perhitungan bot."""
    facts: list[str] = [
        f"- Koin: {f['symbol']}, arah {f['direction']}, horizon {f['horizon']}, tier {f['tier']}",
        f"- Jenis setup: {f['setup']}. Pemicu: {f['trigger'] or 'tidak tercatat'}",
        f"- Entry {_ui_px(f['entry_low'])} sampai {_ui_px(f['entry_high'])}; "
        f"SL {_ui_px(f['stop'])} ({f['sl_basis']}); "
        f"TP1 {_ui_px(f['tp1'])}; TP2 {_ui_px(f['tp2'])}",
    ]
    rr_line = f"- R:R ke TP2 dari titik tengah entry: 1:{f['rr_tp2']:.2f}"
    if f["rr_tp1"] is not None:
        rr_line += f"; ke TP1: 1:{f['rr_tp1']:.2f}"
    facts.append(rr_line)
    facts.append(f"- Skor keyakinan bot: {f['confidence'] * 100:.0f}%")
    if f["confirm"] is not None and f["active"] is not None:
        real_s = ""
        if f["real"] is not None:
            real_s = f", {int(f['real'])} di antaranya konfirmasi nyata"
        facts.append(f"- Konfirmasi: {int(f['confirm'])} dari {int(f['active'])} agen{real_s}")
    if f["confluence"] is not None:
        facts.append(f"- Confluence struktur: {int(f['confluence'])}/5")
    if f["context_mult"] is not None and abs(float(f["context_mult"]) - 1.0) >= 0.01:
        facts.append(f"- Faktor konteks bot: x{float(f['context_mult']):.2f}")
    if f["hist_wr"]:
        facts.append(f"- Winrate historis setup ini (30 hari): {f['hist_wr']}")
    if f["calibration"]:
        facts.append(f"- Kalibrasi keyakinan: {f['calibration']}")

    rule_lines = [
        "Kamu menulis penjelasan singkat untuk trader retail tentang satu sinyal trading.",
        "Aturan wajib:",
        "1. Pakai HANYA fakta di daftar. Dilarang menyebut kondisi pasar yang tidak ada "
        "di daftar: harga saat ini, volume, funding, likuidasi, berita, level support atau "
        "resistance, sentimen, retest, pullback, atau bounce.",
        "2. Dilarang menyebut nama field, angka mentah bertanda '=', atau istilah kode "
        "seperti raw, thr, str, atau cal. Tulis dalam bahasa biasa.",
        "3. Urutan penjelasan: (a) konfirmasi dan R:R, (b) apa yang menguatkan dan "
        "melemahkan sinyal, (c) hal yang perlu diperhatikan trader sebelum memutuskan sendiri.",
        "4. Jika data historis ditandai kecil atau tidak ada, katakan datanya belum cukup "
        "kuat. Jangan jadikan winrate sebagai peluang pasti.",
        "5. Jangan menjamin profit, jangan memberi perintah beli atau jual, dan jangan "
        "memprediksi harga.",
        "6. Maksimal 3 kalimat, Bahasa Indonesia santai namun profesional. Teks polos saja: "
        "tanpa judul, bullet, markdown, atau kode.",
    ]
    return "\n".join(rule_lines) + "\n\nFakta sinyal:\n" + "\n".join(facts)


def _ai_text_is_grounded(text: str, prompt: str) -> bool:
    """Tolak output yang menyebut istilah pasar yang tidak ada di FAKTA sinyal.
    Hanya bagian setelah 'Fakta sinyal:' yang dibandingkan: daftar aturan memuat
    kata-kata terlarang itu sendiri, jadi mencocokkan seluruh prompt tidak pernah menolak."""
    low = text.lower()
    facts_low = prompt.split("Fakta sinyal:", 1)[-1].lower()
    for term in _AI_UNGROUNDED_TERMS:
        if re.search(r"\b" + re.escape(term) + r"\b", low) and term not in facts_low:
            return False
    return True


def _sanitize_ai_text(text: str) -> str | None:
    """Teks polos saja. Tolak (None) jika terlalu panjang, bukan memotongnya:
    potongan di tengah kalimat sudah pernah jadi bug 'kepotong' di Telegram."""
    t = (text or "").replace("*", "").replace("`", "").replace("#", "").strip()
    if not t or len(t) > AI_REASONING_MAX_CHARS:
        return None
    return t


def _ai_failure_reason(err: str) -> str:
    """Terjemahkan error teknis AI ke alasan yang bisa dipahami user."""
    e = (err or "").lower()
    if "no api key" in e:
        return "kunci Gemini belum diset di server"
    if "paused" in e or "rate limit" in e or "quota" in e or "429" in e:
        return "kuota AI sedang dibatasi, coba beberapa menit lagi"
    if "timeout" in e or "timed out" in e:
        return "AI tidak merespons tepat waktu"
    if "unavailable" in e or "503" in e or "high demand" in e or "capacity" in e:
        return "layanan AI sedang sibuk"
    if "empty" in e or "interactions" in e:
        return "AI tidak mengembalikan jawaban"
    return "AI sedang tidak bisa dipakai"


async def _generate_ai_reasoning_ex(memory, signal_id: str) -> tuple[str | None, str]:
    """(teks AI, alasan gagal). Alasan kosong bila berhasil. Gagal dicatat di log, bukan diam-diam."""
    facts = _load_signal_facts(memory, signal_id)
    if facts is None:
        return None, "data sinyal tidak ditemukan"
    prompt = _build_ai_reasoning_prompt(facts)
    try:
        gem = get_gemini_client(memory)
        if not gem.available():
            _AI_REASONING_LOG.warning("ai_reasoning: GEMINI_API_KEY belum diset (sid=%s)", signal_id[:8])
            return None, _ai_failure_reason("no api key")
        res = await gem.generate(
            AI_REASONING_FN, prompt,
            max_tokens=AI_REASONING_MAX_TOKENS,
            temperature=0.2,
            cache_ttl_h=AI_REASONING_TTL_H,
            thinking_level="low",
            timeout_sec=AI_REASONING_TIMEOUT_SEC,
        )
    except Exception as e:  # fail-soft: AI tidak boleh menghentikan tombol
        _AI_REASONING_LOG.warning("ai_reasoning error sid=%s: %s", signal_id[:8], e)
        return None, _ai_failure_reason(str(e))
    if not res.success or not res.text:
        err = (getattr(res, "error", None) or "").strip()
        _AI_REASONING_LOG.warning("ai_reasoning gagal sid=%s: %s", signal_id[:8], err or "tanpa pesan")
        return None, _ai_failure_reason(err)
    text = _sanitize_ai_text(res.text)
    if text is None:
        _AI_REASONING_LOG.info("ai_reasoning ditolak: panjang/kosong sid=%s", signal_id[:8])
        return None, "jawaban AI terlalu panjang"
    if not _ai_text_is_grounded(text, prompt):
        _AI_REASONING_LOG.info("ai_reasoning ditolak: istilah tak berdasar sid=%s", signal_id[:8])
        return None, "jawaban AI memuat klaim di luar fakta"
    return text, ""


async def _generate_ai_reasoning(memory, signal_id: str) -> str | None:
    """Penjelasan AI untuk satu sinyal. None jika tidak tersedia / ditolak aturan."""
    text, _ = await _generate_ai_reasoning_ex(memory, signal_id)
    return text


REASON_PLACEHOLDER_TEXT = "⏳ Menyusun penjelasan…"
REASON_FAILED_TEXT = "⚠️ Penjelasan gagal dimuat. Ketuk 🧠 Reasoning lagi."


async def _handle_reason_callback(
    data: str, chat_id, mid, is_photo: bool, bot, memory,
) -> None:
    """Tombol 🧠 Reasoning. Spinner sudah dijawab oleh pemanggil (answer_callback).
    Tanpa status sementara, user menunggu tanpa perubahan apa pun selama AI bekerja.
    Pesan placeholder dijamin berganti: jika generate gagal, tampilkan pesan gagal,
    bukan membiarkan '⏳' tertinggal."""
    sid = data.split(":", 2)[-1]
    markup = _kb_back_to_signal(sid)
    can_edit_text = (not is_photo) and bool(mid) and str(mid).strip().isdigit()
    if can_edit_text:
        await bot.edit_message(chat_id, int(mid), REASON_PLACEHOLDER_TEXT, reply_markup=None)
    try:
        text = await _fmt_reasoning_with_ai(memory, sid)
    except Exception as e:  # noqa: BLE001 — placeholder tidak boleh tertinggal
        logger.warning("reason callback fail-soft sid=%s: %s", sid[:8], type(e).__name__)
        text = REASON_FAILED_TEXT
    if is_photo:
        # Foto tidak bisa di-edit jadi teks: kirim pesan baru lalu hapus foto.
        await _send_text_replacing(bot, chat_id, mid, True, text, markup)
        return
    if can_edit_text and await bot.edit_message(chat_id, int(mid), text, reply_markup=markup):
        return
    await bot.send_message(chat_id, text, reply_markup=markup)


async def _fmt_reasoning_with_ai(memory, signal_id: str) -> str:
    """Penjelasan AI di atas, detail mesin di bawah. Tanpa AI: detail mesin + alasan yang jelas."""
    ai_text, reason = await _generate_ai_reasoning_ex(memory, signal_id)
    if reason == "data sinyal tidak ditemukan":
        return _fmt_reasoning(memory, signal_id)
    return _fmt_reasoning(memory, signal_id, ai_text=ai_text, ai_reason=reason)


_LAST_MIDS: dict[str, float] = {}
# key = f"{signal_id}:{state}" — state tercetak di dalam PNG, jadi wajib ikut key.
# value = (monotonic_ts, png_bytes, caption) — caption ikut di-cache agar tap kedua tidak kehilangan caption.
_CHART_CACHE: dict[str, tuple[float, bytes, str]] = {}
_CHART_CACHE_TTL = 60.0
_CHART_RENDER_LOCK = None  # lazy asyncio.Lock


# ----- Coin Explorer (read-only; candle HL real + likuidasi Binance tersimpan) -----
import bisect as _bisect  # noqa: E402

EXPLORER_TFS: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
EXPLORER_BAR_MS: dict[str, int] = {
    "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
EXPLORER_BARS = 100
EXPLORER_CACHE_TTL = 60.0
_EXPLORER_CACHE: dict[tuple[str, str], dict] = {}
_EXPLORER_CACHE_MAX = 60


def _explorer_cache_store(key: tuple[str, str], value: dict) -> None:
    """Simpan ke cache explorer dengan batas; entri paling lama dibuang."""
    if key not in _EXPLORER_CACHE and len(_EXPLORER_CACHE) >= _EXPLORER_CACHE_MAX:
        oldest = min(_EXPLORER_CACHE, key=lambda k: _EXPLORER_CACHE[k].get("ts", 0.0))
        _EXPLORER_CACHE.pop(oldest, None)
    _EXPLORER_CACHE[key] = value
_EXPLORER_RENDER_SEM = asyncio.Semaphore(1)   # render CPU-bound: satu per waktu
_EXPLORER_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,15}$")  # huruf kecil boleh: kPEPE
_EXPLORER_CTX_TTL = 60.0
_EXPLORER_CTX_CACHE: dict = {"ts": 0.0, "by_sym": {}}


def explorer_top_coins(memory) -> list[str]:
    """Daftar top volume dari siklus terakhir. Kosong bila belum ada data siklus:
    tidak ada fallback hardcode (Aturan #7, universe dinamis)."""
    try:
        raw = memory.get_runtime("explorer_top_coins") or ""
        data = json.loads(raw) if raw else []
        syms = [canon_hl_symbol(str(x)) for x in data if _EXPLORER_SYM_RE.match(str(x).strip())]
        if syms:
            return syms[:EXPLORER_TOP_N]
    except Exception:  # noqa: BLE001
        pass
    return []


def _liq_is_long(side: str) -> bool:
    """Bias tersimpan SHORT = order SELL = long dilikuidasi (merah)."""
    return str(side).upper() == "SHORT"


def liq_bar_index(ts: float, bar_times: list[float], bar_s: float) -> int | None:
    """Index bar (epoch detik, ascending) yang memuat ts; None bila di luar chart."""
    if not bar_times or ts < bar_times[0] or ts >= bar_times[-1] + bar_s:
        return None
    return _bisect.bisect_right(bar_times, ts) - 1


def volume_profile(candles, n_bins: int = 28) -> tuple[list[float], list[float]]:
    """Volume per bin harga. Volume tiap candle dibagi proporsional ke bin yang
    dilewati rentang [low, high]; doji masuk satu bin. Total volume dipertahankan."""
    if not candles or n_bins < 1:
        return [], []
    lo = min(c.low for c in candles)
    hi = max(c.high for c in candles)
    if hi <= lo:
        return [lo, hi], [sum(float(c.volume or 0) for c in candles)]
    width = (hi - lo) / n_bins
    edges = [lo + i * width for i in range(n_bins + 1)]
    vols = [0.0] * n_bins
    for c in candles:
        v = float(c.volume or 0)
        if v <= 0:
            continue
        if c.high <= c.low:
            i = min(n_bins - 1, max(0, int((c.low - lo) / width)))
            vols[i] += v
            continue
        span = c.high - c.low
        first = max(0, min(n_bins - 1, int((c.low - lo) / width)))
        last = max(0, min(n_bins - 1, int((c.high - lo) / width)))
        for i in range(first, last + 1):
            ov = min(c.high, edges[i + 1]) - max(c.low, edges[i])
            if ov > 0:
                vols[i] += v * ov / span
    return edges, vols


def poc_price(edges: list[float], vols: list[float]) -> float | None:
    """Pusat bin volume terbanyak (Point of Control)."""
    if not vols or len(edges) < 2 or sum(vols) <= 0:
        return None
    i = max(range(len(vols)), key=lambda k: vols[k])
    return (edges[i] + edges[i + 1]) / 2.0


def _fmt_px(x: float) -> str:
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


def _usd_short(x: float) -> str:
    return fmt_usd_short(x)


def _wib_label(dt: datetime, with_date: bool) -> str:
    local = dt.astimezone(WIB)
    return local.strftime("%d/%m %H:%M") if with_date else local.strftime("%H:%M")


def _fmt_dur(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} dtk"
    if sec < 3600:
        return f"{sec // 60} mnt"
    h, rem = divmod(sec, 3600)
    return f"{h} jam {rem // 60} mnt"


def _symbol_liq_coverage_note(symbol: str) -> str:
    """Satu baris status: kenapa belum ada likuidasi tercatat. Dipanggil di main thread."""
    buf = get_cascade_buffer()
    if not buf.connected:
        err = (buf.last_error or "belum tersambung").strip()
        return f"WS likuidasi Binance tidak tersambung ({err[:60]})"
    if buf.total_events == 0:
        return "WS likuidasi tersambung, belum ada event sejak bot start"
    try:
        pair = _hl_sym_to_binance_pair(symbol)
    except Exception:  # noqa: BLE001
        pair = None
    if pair is None:
        return f"{symbol} tidak punya pair USDT-M di Binance, jadi tidak ada feed untuk koin ini"
    cnt, _ = buf.symbol_stats(symbol)
    if cnt > 0:
        return f"{cnt} event {symbol} sudah diterima, belum terlihat di DB (cek flush)"
    since = buf.feed_started_at
    if since is None:
        return f"0 likuidasi {symbol}, feed baru mulai"
    start = _wib_label(datetime.fromtimestamp(since, tz=UTC), False)
    return f"0 likuidasi {symbol} sejak feed hidup {start} WIB ({_fmt_dur(time.time() - since)})"


_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _spark(values: list[float]) -> str:
    """Sparkline blok dari deret harga. Deret datar → garis tengah."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return "▄" * len(vals)
    top = len(_SPARK_BLOCKS) - 1
    return "".join(
        _SPARK_BLOCKS[min(top, int((v - lo) / (hi - lo) * top))] for v in vals
    )


def _funding_line(rate_hourly: float) -> str:
    """Funding HL (dibayar per jam). Label magnitude memakai aturan breadth yang sama.
    'per jam' ditulis lengkap: Telegram menautkan '/jam' sebagai perintah berwarna biru."""
    mag, side = _engine.funding_magnitude_label(rate_hourly)
    pct_h = rate_hourly * 100.0
    if mag == "FLAT":
        tag = "datar"
    else:
        tag = f"{_MAG_ID.get(mag, mag.lower())}, ramai {'long' if side == 'LONG' else 'short'}"
    return f"Funding: {pct_h:+.4f}% per jam · {tag}"


def _funding_sub_line(rate_hourly: float, data_time: str | None = None) -> str:
    """Baris tahunan funding. data_time (HH:MM WIB) menyebut kapan angka itu diambil, supaya
    tampilan Explorer dan Breadth tidak terlihat bertentangan."""
    ann = rate_hourly * 24 * 365 * 100.0
    base = f"  └ setara {ann:+.1f}% per tahun"
    return f"{base} · data {data_time} WIB" if data_time else base


async def _explorer_asset_ctx(memory, symbol: str) -> dict | None:
    """Funding, volume 24 jam, OI, dan perubahan 24 jam dari metaAndAssetCtxs.

    Cache 60 dtk supaya klik berulang tidak membanjiri REST. Fail-soft → None
    (caption tetap tampil dengan catatan data belum tersedia).
    """
    h3 = _engine._HIP3_CTX.get(symbol)
    if h3 is not None:
        px = h3["mid_px"] or h3["mark_px"]
        chg = (px - h3["prev_px"]) / h3["prev_px"] * 100.0 if h3["prev_px"] > 0 and px > 0 else None
        return {
            "funding": h3["funding"], "funding_time": None, "funding_src": "live",
            "vol24": h3["vlm"], "oi_usd": h3["oi_usd"], "chg24": chg,
        }
    now_m = time.monotonic()
    if not _EXPLORER_CTX_CACHE["by_sym"] or now_m - _EXPLORER_CTX_CACHE["ts"] >= _EXPLORER_CTX_TTL:
        try:
            assets = await _engine.hl_meta_and_asset_ctxs(memory)
        except Exception:  # noqa: BLE001
            return None
        _EXPLORER_CTX_CACHE["by_sym"] = {a.symbol: a for a in assets}
        _EXPLORER_CTX_CACHE["ts"] = now_m
    a = _EXPLORER_CTX_CACHE["by_sym"].get(_engine.canon_hl_symbol(symbol))
    if a is None:
        return None
    px = a.mid_px or a.mark_px
    chg24 = None
    if a.prev_day_px > 0 and px > 0:
        chg24 = (px - a.prev_day_px) / a.prev_day_px * 100.0
    # Satu sumber untuk semua tampilan: baris funding scan terakhir (sama dengan Breadth & snapshot).
    # Live metaAndAssetCtxs hanya cadangan bila scan belum punya baris untuk koin ini.
    fund, fund_time, fund_src = a.funding, None, "live"
    try:
        for r in _latest_funding_rows(memory, 6.0) or []:
            if canon_hl_symbol(str(r["symbol"])) == _engine.canon_hl_symbol(symbol):
                fund = float(r["rate"])
                fund_time = _wib_label(datetime.fromisoformat(str(r["recorded_at"])), False)
                fund_src = "scan"
                break
    except (TypeError, ValueError, KeyError, AttributeError):
        fund, fund_time, fund_src = a.funding, None, "live"
    return {
        "funding": fund,
        "funding_time": fund_time,
        "funding_src": fund_src,
        "vol24": a.day_ntl_vlm,
        "oi_usd": a.open_interest * px if px > 0 else 0.0,
        "chg24": chg24,
    }


def _liq_reason_id(reason: str) -> str:
    """Alasan teknis peta estimasi → bahasa biasa."""
    r = (reason or "").lower()
    if "candle" in r:
        return "data harga belum cukup"
    if "snapshot oi" in r or "riwayat oi" in r:
        return "riwayat OI belum cukup (koin mungkin belum masuk pemantauan bot)"
    if "tidak ada kenaikan oi" in r:
        return "belum ada kenaikan OI di jendela chart"
    if "rentang tampilan" in r:
        return "level likuidasi di luar rentang chart"
    if "horizon 24 jam" in r:
        return "TF ini terlalu besar untuk horizon 24 jam, pilih 5m, 15m, atau 1h"
    return "data belum cukup"


def _explorer_caption(symbol, tf, series, liqs, coverage_start, poc, coverage_note: str = "",
                      liq_est: dict | None = None, liq_val: dict | None = None,
                      asset_ctx: dict | None = None) -> str:
    """Caption Telegram. Tiap baris satu fakta, sumber disebut, tanpa istilah mesin.
    Titik di chart = data likuidasi (sudah terjadi). Peta = model dari OI, bukan data."""
    first_open = series[0].open
    last_close = series[-1].close
    chg = (last_close - first_open) / first_open * 100.0 if first_open else 0.0
    trend = "▲" if chg >= 0 else "▼"
    n_bar = len(series)
    long_liq = [e for e in liqs if _liq_is_long(e["side"])]
    short_liq = [e for e in liqs if not _liq_is_long(e["side"])]
    lines = [
        f"📊 {symbol} · {tf.upper()} · {n_bar} bar",
        _ui_sep(),
        f"Harga: {_fmt_px(last_close)}",
        f"Dalam {n_bar} bar: {trend} {chg:+.2f}%",
    ]
    spark_vals = [c.close for c in series[-24:]]
    spark = _spark(spark_vals)
    if spark:
        lines.append(f"Pergerakan {len(spark_vals)} bar terakhir: {spark}")
    if poc is not None:
        lines.append(f"Area volume terbanyak (POC): {_fmt_px(poc)}")
    lines += ["", "📈 Hyperliquid"]
    if asset_ctx is not None:
        if asset_ctx.get("funding") is not None:
            fr = float(asset_ctx["funding"])
            lines.append(_funding_line(fr))
            ft = asset_ctx.get("funding_time") if asset_ctx.get("funding_src") == "scan" else None
            lines.append(_funding_sub_line(fr, ft))
        lines.append(
            f"Volume 24 jam {_usd_short(asset_ctx.get('vol24') or 0)} · "
            f"OI {_usd_short(asset_ctx.get('oi_usd') or 0)}"
        )
        if asset_ctx.get("chg24") is not None:
            lines.append(f"Perubahan 24 jam {asset_ctx['chg24']:+.2f}%")
    else:
        lines.append("Funding & volume belum tersedia, coba lagi sebentar")
    lines += [
        "",
        f"🔴🟢 Likuidasi tercatat (Binance) dalam {n_bar} bar:",
        f"  Long dilikuidasi: {_usd_short(sum(e['usd'] for e in long_liq))} · {len(long_liq)} kejadian",
        f"  Short dilikuidasi: {_usd_short(sum(e['usd'] for e in short_liq))} · {len(short_liq)} kejadian",
    ]
    if liqs:
        lines.append(f"  Terakhir: {_wib_label(liqs[-1]['time'], False)} WIB")
    lines.append("")
    if coverage_note:
        lines.append(f"ℹ️ {coverage_note}")
    elif coverage_start is not None:
        lines.append(f"ℹ️ Data likuidasi sejak {_wib_label(coverage_start, True)} WIB")
    lines.append("")
    if liq_est is not None and liq_est.get("ok"):
        nb, na = liq_est.get("nearest_below"), liq_est.get("nearest_above")
        lines.append("🧭 Estimasi peta (model dari OI Hyperliquid)")
        if liq_est.get("cover_from") is not None:
            since = datetime.fromtimestamp(liq_est["cover_from"], tz=UTC)
            lines.append(f"Dihitung dari OI sejak {_wib_label(since, True)} WIB")
        lines.append(
            f"Di bawah harga: ~{_usd_short(liq_est['below_usd'])}"
            + (f" · terdekat {_fmt_px(nb)}" if nb is not None else "")
        )
        lines.append(
            f"Di atas harga: ~{_usd_short(liq_est['above_usd'])}"
            + (f" · terdekat {_fmt_px(na)}" if na is not None else "")
        )
        if liq_val and liq_val.get("n", 0) >= 5:
            n, hit, base = liq_val["n"], liq_val["hit"], liq_val.get("base", 0)
            lift = liq_val.get("lift")
            hit_txt = f"{hit / n * 100:.0f}%"
            if not base:
                # Pembanding acak tidak kena zona sama sekali → tidak ada dasar perbandingan.
                lines.append(
                    f"Kalibrasi: {hit}/{n} ({hit_txt}) kejadian jatuh di zona. "
                    "Pembanding zona waktu lain juga tidak pernah kena, jadi belum bisa disimpulkan."
                )
            else:
                rnd = base / n * 100
                rnd_txt = f"{rnd:.1f}%" if rnd < 10 else f"{rnd:.0f}%"
                ok = lift is not None and lift >= LIQ_CALIB_MIN_LIFT
                verdict = "lebih baik dari zona waktu lain" if ok else "belum lebih baik dari zona waktu lain"
                lines.append(
                    f"Kalibrasi: {hit}/{n} ({hit_txt}) kejadian jatuh di zona, "
                    f"zona waktu lain {rnd_txt} → {verdict}"
                )
        else:
            lines.append("Kalibrasi: data likuidasi belum cukup untuk diuji")
        lines.append("Catatan: kalibrasi memakai likuidasi Binance, peta dari OI Hyperliquid.")
    elif liq_est is not None:
        lines.append(f"🧭 Peta estimasi belum tersedia: {_liq_reason_id(liq_est.get('reason'))}")
    lines += ["", "Titik di chart = likuidasi yang sudah terjadi (data). Peta = perkiraan, bukan data."]
    text = "\n".join(lines)
    if len(text) > 1024:  # batas caption Telegram; potong di batas baris, bukan di tengah kata
        cut = text[:1024]
        text = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return text


LOGO_PX = 256                    # resolusi lencana logo (di-downscale saat render)
LOGO_INNER_RATIO = 0.88          # logo mengisi 88% diameter bingkai → bingkai rapat di sekitar logo
LOGO_OK_TTL_SEC = 24 * 3600.0    # logo yang berhasil disimpan 24 jam
LOGO_MISS_TTL_SEC = 3600.0       # gagal ambil → coba lagi setelah 1 jam (tidak membanjiri API)
LOGO_FETCH_TIMEOUT_SEC = 6.0
_LOGO_CACHE: dict[str, tuple[float, bytes | None]] = {}


def _fetch_image_bytes_sync(url: str, timeout: float = 4.0) -> bytes | None:
    """Unduh gambar (PNG/JPG). Gagal → None; tidak pernah melempar."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cryptone-v45"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(2_000_000)
        return data or None
    except Exception:  # noqa: BLE001 — logo opsional
        return None


def _logo_from_coingecko_sync(cg_id: str) -> bytes | None:
    try:
        meta = _http_get_json(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}"
            "?localization=false&tickers=false&market_data=false"
            "&community_data=false&developer_data=false&sparkline=false",
            timeout=4.0,
        )
        img = (meta.get("image") or {}) if isinstance(meta, dict) else {}
        url = img.get("large") or img.get("small")
    except Exception:  # noqa: BLE001
        return None
    return _fetch_image_bytes_sync(url) if url else None


def _logo_from_coingecko_search_sync(ticker: str) -> bytes | None:
    """Cari simbol di CoinGecko. Ticker sering dipakai banyak token → ambil kecocokan
    simbol persis dengan market_cap_rank terkecil (bukan yang kebetulan pertama)."""
    try:
        from urllib.parse import quote
        res = _http_get_json(f"https://api.coingecko.com/api/v3/search?query={quote(ticker)}", timeout=4.0)
        coins = res.get("coins") if isinstance(res, dict) else None
        best = None
        for coin in coins or []:
            if str(coin.get("symbol") or "").upper() != ticker.upper() or not coin.get("large"):
                continue
            rank = coin.get("market_cap_rank")
            rank = rank if isinstance(rank, int) and rank > 0 else 10**9
            if best is None or rank < best[0]:
                best = (rank, coin)
        if best is None:
            return None
        return _fetch_image_bytes_sync(str(best[1]["large"]))
    except Exception:  # noqa: BLE001
        return None


def _logo_from_hl_cdn_sync(ticker: str) -> bytes | None:
    """Logo resmi Hyperliquid (SVG). Hanya jika cairosvg terpasang; kalau tidak, dilewati."""
    try:
        import cairosvg  # type: ignore
    except ImportError:
        return None
    svg = _fetch_image_bytes_sync(f"https://app.hyperliquid.xyz/coins/{ticker.upper()}.svg")
    if not svg:
        return None
    try:
        return cairosvg.svg2png(bytestring=svg, output_width=256, output_height=256)
    except Exception:  # noqa: BLE001
        return None


def _logo_from_coincap_sync(ticker: str) -> bytes | None:
    return _fetch_image_bytes_sync(f"https://assets.coincap.io/assets/icons/{ticker.lower()}@2x.png")


async def coin_logo_png(symbol: str, memory=None) -> bytes | None:
    """Logo koin, berurutan: CoinGecko (id pemetaan) → CoinGecko search (simbol persis)
    → CoinCap → Hyperliquid CDN (SVG, hanya bila cairosvg ada) → None (monogram).
    Cache 24 jam (gagal 1 jam). Fail-soft: tidak pernah melempar."""
    ticker = _engine._base_ticker(symbol) or str(symbol or "").upper()
    now_m = time.monotonic()
    hit = _LOGO_CACHE.get(ticker)
    if hit is not None:
        ts, data = hit
        ttl = LOGO_OK_TTL_SEC if data else LOGO_MISS_TTL_SEC
        if now_m - ts < ttl:
            return data

    async def _go() -> bytes | None:
        # Urutan: logo resmi HL (koin yang sama dengan candle) → CoinGecko id terpetakan
        # → CoinGecko search (rank terkecil). CoinCap by-ticker dihapus: salah koin.
        data = await asyncio.to_thread(_logo_from_hl_cdn_sync, ticker)
        if data is None:
            cg_id = None
            try:
                cg_id = await _engine._coingecko_symbol_id(symbol, memory)
            except Exception:  # noqa: BLE001
                cg_id = None
            if cg_id:
                await _engine._cg_pace()
                data = await asyncio.to_thread(_logo_from_coingecko_sync, cg_id)
        if data is None:
            data = await asyncio.to_thread(_logo_from_coingecko_search_sync, ticker)
        return data

    try:
        data = await asyncio.wait_for(_go(), timeout=LOGO_FETCH_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — timeout/error → monogram
        data = None
    _LOGO_CACHE[ticker] = (now_m, data)
    return data


def _normalize_logo(img, box: int):
    """Logo saja (tanpa flag). Lihat _normalize_logo_mode."""
    return _normalize_logo_mode(img, box)[0]


def _normalize_logo_mode(img, box: int):
    """Seragamkan ukuran visual semua logo: buang ruang kosong di sekitar isi, lalu skalakan isi
    supaya pas di kotak `box` × `box` (rasio dijaga).
    - Latar transparan  → potong berdasarkan alpha.
    - Latar putih       → potong berdasarkan piksel non-putih.
    - Kotak penuh warna (ubin persegi) → tidak dipotong, diperkecil ke sisi = box/√2 supaya sudutnya
      tetap di dalam lingkaran (tidak terpotong).
    Return PIL RGBA."""
    from PIL import Image
    rgba = img.convert("RGBA")
    w, h = rgba.size
    if w == 0 or h == 0:
        raise ValueError("logo kosong")
    corners = [rgba.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if all(px[3] < 16 for px in corners):
        mask = rgba.getchannel("A").point(lambda a: 255 if a >= 16 else 0)
    elif all(px[3] >= 200 and min(px[:3]) >= 235 for px in corners):
        mask = rgba.convert("L").point(lambda v: 255 if v < 235 else 0)
    else:
        # ubin persegi penuh warna: crop ke persegi tengah, lalu isi lingkaran (cover), bukan di-fit
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        tile_img = rgba.crop((left, top, left + side, top + side)).resize((box, box), Image.LANCZOS)
        return tile_img, True
    if mask is not None:
        bb = mask.getbbox()
        if bb is not None:
            rgba = rgba.crop(bb)
    # skala ke sisi `box` dua arah (kecil dibesarkan, besar diperkecil) → ukuran visual seragam
    scale = min(box / rgba.width, box / rgba.height)
    new_size = (max(1, round(rgba.width * scale)), max(1, round(rgba.height * scale)))
    return rgba.resize(new_size, Image.LANCZOS), False


def _coin_badge_rgba(logo_png: bytes | None, ticker: str, size: int = LOGO_PX):
    """Lencana bundar putih: logo di-fit (tidak terpotong, rasio dijaga) di tengah lingkaran.
    Tanpa logo → monogram tiker gelap di lingkaran putih. Return PIL RGBA (transparan di luar lingkaran)."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    disc = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    inner = int(size * LOGO_INNER_RATIO)
    logo = None
    if logo_png:
        try:
            logo, is_tile = _normalize_logo_mode(Image.open(BytesIO(logo_png)), inner)
        except Exception:  # noqa: BLE001 — gambar rusak → monogram
            logo, is_tile = None, False
    if logo is not None and is_tile:
        # ubin penuh: isi lingkaran dalam bingkai putih (latar logo jadi isi bulat, seperti TradingView)
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        layer.paste(logo, ((size - inner) // 2, (size - inner) // 2), logo)
        circ = Image.new("L", (size, size), 0)
        ImageDraw.Draw(circ).ellipse(((size - inner) // 2, (size - inner) // 2,
                                      (size + inner) // 2 - 1, (size + inner) // 2 - 1), fill=255)
        layer.putalpha(circ)
        disc = Image.alpha_composite(disc, layer)
    elif logo is not None:
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        layer.paste(logo, ((size - logo.width) // 2, (size - logo.height) // 2), logo)
        disc = Image.alpha_composite(disc, layer)
    else:
        draw = ImageDraw.Draw(disc)
        text = (ticker or "?")[:4].upper()
        try:
            font = ImageFont.load_default(size=int(size * 0.30))
        except TypeError:  # Pillow lama tanpa parameter size
            font = ImageFont.load_default()
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        draw.text(((size - (r - l)) / 2 - l, (size - (b - t)) / 2 - t), text,
                  fill=(19, 23, 34, 255), font=font)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(disc, (0, 0), mask)
    return out


def _render_explorer_png_sync(symbol, tf, candles, liqs, coverage_start, coverage_note: str = "",
                              liq_est: dict | None = None, liq_val: dict | None = None,
                              asset_ctx: dict | None = None, logo_png: bytes | None = None):
    """Render CPU-bound (dipanggil via to_thread). Return (png | None, caption | error)."""
    try:
        import logging as _lg
        import math
        import numpy as np
        import matplotlib
        _lg.getLogger("matplotlib.font_manager").setLevel(_lg.WARNING)
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter, MaxNLocator
        from io import BytesIO
    except ImportError as e:
        return None, f"Chart lib missing: {e} (pip install matplotlib)"

    series = sorted(candles, key=lambda c: c.bar_time)
    n = len(series)
    times = [c.bar_time.timestamp() for c in series]
    bar_s = EXPLORER_BAR_MS[tf] / 1000.0
    edges, vols = volume_profile(series, n_bins=28)
    poc = poc_price(edges, vols)
    marks = []
    for e in liqs:
        idx = liq_bar_index(e["time"].timestamp(), times, bar_s)
        if idx is None:
            continue
        ref = series[idx]
        # Harga likuidasi tidak tersimpan (hanya USD) → titik di tengah bar terkait
        marks.append((idx, (ref.high + ref.low) / 2.0, float(e["usd"]), _liq_is_long(e["side"])))

    BG, PANEL, GRID = "#131722", "#1b1f2b", "#2a2e39"
    TEXT, MUTED = "#e6e9f0", "#8b90a0"
    GREEN, RED, BLUE = "#26a69a", "#ef5350", "#42a5f5"
    fig = None
    try:
        fig = plt.figure(figsize=(12.0, 7.2), dpi=130, facecolor=BG)
        ax = fig.add_axes([0.06, 0.125, 0.76, 0.755], facecolor=PANEL)
        ax_vp = fig.add_axes([0.83, 0.125, 0.13, 0.755], sharey=ax, facecolor=PANEL)
        for a in (ax, ax_vp):
            a.grid(True, color=GRID, lw=0.6, alpha=0.5)
            a.tick_params(colors=TEXT, labelsize=9)
            for sp in a.spines.values():
                sp.set_color(GRID)
        ax_vp.tick_params(labelleft=False)

        if liq_est is not None and liq_est.get("ok"):
            H = np.array(liq_est["H"], dtype=float).T  # (bin, bar)
            pos = H[H > 0]
            vmax = float(np.percentile(pos, 99)) if pos.size else 1.0
            vmax = max(vmax, 1e-9)
            # Threshold (seperti slider "Liquidity Threshold"): sel lemah tidak digambar → tidak noise
            Hm = np.where(H >= LIQ_EST_DRAW_THR * vmax, H, np.nan)
            cmap = plt.get_cmap("viridis").copy()
            cmap.set_bad(alpha=0.0)
            ax.imshow(Hm, origin="lower", aspect="auto", cmap=cmap, alpha=0.5,
                      vmin=0.0, vmax=vmax, zorder=0,
                      extent=[-0.5, n - 0.5, liq_est["edges"][0], liq_est["edges"][-1]])
        xs = list(range(n))
        cols = [GREEN if c.close >= c.open else RED for c in series]
        ax.vlines(xs, [c.low for c in series], [c.high for c in series],
                  colors=cols, linewidth=1.0, zorder=2)
        bottoms = [min(c.open, c.close) for c in series]
        heights = [max(abs(c.close - c.open), max(c.high - c.low, 1e-12) * 0.02) for c in series]
        ax.bar(xs, heights, bottom=bottoms, width=0.6, color=cols, zorder=3)

        if vols and max(vols) > 0:
            bin_h = edges[1] - edges[0]
            ax_vp.barh(edges[:-1], vols, height=bin_h, align="edge",
                       color=BLUE, alpha=0.35, zorder=2)
        if poc is not None:
            ax.axhline(poc, color=BLUE, lw=0.9, ls="--", alpha=0.8, zorder=1)
            ax.text(n - 1, poc, f" POC {_fmt_px(poc)} ", color=TEXT, fontsize=10,
                    fontweight="bold", va="bottom", ha="right", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.25", facecolor=BG, edgecolor=BLUE,
                              alpha=0.85, lw=0.8))
        if marks:
            umax = max(m[2] for m in marks) or 1.0
            ax.scatter([m[0] for m in marks], [m[1] for m in marks],
                       s=[30 + 420 * math.sqrt(m[2] / umax) for m in marks],
                       c=[RED if m[3] else GREEN for m in marks],
                       alpha=0.55, edgecolors="none", zorder=4)

        lo_p = min(c.low for c in series)
        hi_p = max(c.high for c in series)
        pad = (hi_p - lo_p) * 0.05 or hi_p * 0.001
        ax.set_ylim(lo_p - pad, hi_p + pad)
        ax.set_xlim(-1, n)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=8))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: _fmt_px(x)))
        step = max(1, n // 6)
        tick_idx = list(range(0, n, step))
        ax.set_xticks(tick_idx)
        ax.set_xticklabels(
            [_wib_label(series[i].bar_time, tf == "1d") for i in tick_idx],
            color=TEXT, fontsize=11,
        )
        ax_vp.set_xticks([])
        ax.set_xlabel("Waktu (WIB)", color=MUTED, fontsize=9, labelpad=1)
        # Lencana logo kiri atas (kotak 1:1 dalam inci: 0.6 in × 0.6 in)
        logo_ax = fig.add_axes([0.04, 0.89, 0.042, 0.042 * 12.0 / 7.2])  # 0.5 in x 0.5 in → kotak 1:1
        logo_ax.imshow(np.asarray(_coin_badge_rgba(logo_png, _engine._base_ticker(symbol) or symbol)))
        logo_ax.axis("off")
        fig.text(0.097, 0.935, f"{symbol} · {tf.upper()} · {n} bar",
                 fontsize=14, fontweight="bold", color=TEXT, va="center")
        fig.text(0.097, 0.9, "Volume profile · likuidasi Binance · peta estimasi",
                 fontsize=8.5, color=MUTED, va="center")
        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D
        handles = [
            Line2D([], [], marker="o", ls="", color=RED, markersize=7, alpha=0.8,
                   label="Long dilikuidasi (ukuran titik = nilai USD)"),
            Line2D([], [], marker="o", ls="", color=GREEN, markersize=7, alpha=0.8,
                   label="Short dilikuidasi"),
        ]
        if liq_est is not None and liq_est.get("ok"):
            handles.append(Patch(facecolor=plt.get_cmap("viridis")(0.6), alpha=0.5,
                                 label="Estimasi peta likuidasi (model OI)"))
        # satu baris: long · short · estimasi
        fig.legend(handles=handles, loc="lower left", bbox_to_anchor=(0.06, 0.012),
                   ncol=len(handles), frameon=False, fontsize=9.5, labelcolor=TEXT,
                   handlelength=1.4, columnspacing=2.0, borderaxespad=0.0)
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=130, facecolor=BG)
        png = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        return None, f"Chart error: {type(e).__name__}"
    finally:
        if fig is not None:
            plt.close(fig)

    caption = _explorer_caption(symbol, tf, series, liqs, coverage_start, poc, coverage_note,
                                liq_est=liq_est, liq_val=liq_val, asset_ctx=asset_ctx)
    return png, caption


# ----- Peta likuidasi ESTIMASI (model; bukan posisi nyata) -----
# Asumsi yang dibuat secara eksplisit supaya bisa dikritik:
#  - tiap kenaikan OI dianggap posisi baru di harga close bar itu
#  - arah dari ΔOI × arah harga (harga naik → long, turun → short)
#  - leverage dibagi ke tier dengan bobot yang diasumsikan
#  - posisi hilang begitu harga menyentuh level likuidasinya
LIQ_EST_TIERS: tuple[tuple[float, float], ...] = (
    (10.0, 0.30), (20.0, 0.30), (25.0, 0.15), (50.0, 0.15), (100.0, 0.10),
)
LIQ_EST_MMR = 0.005          # maintenance margin 0,5% (asumsi)
LIQ_EST_RANGE_PCT = 0.12     # tampilkan level dalam ±12% dari rentang harga
LIQ_EST_MIN_BARS = 10
LIQ_EST_DRAW_THR = 0.15   # sel < 15% puncak tidak digambar (anti-noise)
LIQ_EST_MIN_OI_POINTS = 4
LIQ_EST_MIN_COVERAGE = 0.5  # riwayat OI minimal separuh window peta
LIQ_EST_HORIZON_SEC = 24 * 3600.0  # horizon peta: hanya posisi yang dibuka dalam 24 jam terakhir chart


def liq_level_est(entry: float, lev: float, long_side: bool, mmr: float = LIQ_EST_MMR) -> float:
    if long_side:
        return entry * (1.0 - 1.0 / lev + mmr)
    return entry * (1.0 + 1.0 / lev - mmr)


def estimate_liq_heatmap(
    candles, oi_series, bar_s: float,
    n_bins: int = 60, tiers=LIQ_EST_TIERS, mmr: float = LIQ_EST_MMR,
    range_pct: float = LIQ_EST_RANGE_PCT,
) -> dict:
    """Peta likuidasi estimasi: H[bar][bin] = USD posisi yang masih hidup & level likuidasinya di bin itu."""
    out: dict = {"ok": False, "reason": "", "edges": [], "H": [],
                 "below_usd": 0.0, "above_usd": 0.0,
                 "nearest_below": None, "nearest_above": None, "n_positions": 0,
                 "cover_from": None}
    if len(candles) < LIQ_EST_MIN_BARS:
        out["reason"] = f"candle {len(candles)} bar (min {LIQ_EST_MIN_BARS})"
        return out
    if len(oi_series) < LIQ_EST_MIN_OI_POINTS:
        out["reason"] = f"snapshot OI {len(oi_series)} (min {LIQ_EST_MIN_OI_POINTS})"
        return out
    # Syarat cakupan: riwayat OI harus menutup minimal separuh window, supaya peta awal
    # (baru beberapa menit) tidak tampil meyakinkan padahal belum mewakili apa-apa.
    # Window peta = min(panjang chart, horizon). Riwayat OI harus menutup separuh window.
    # Horizon 24 jam: posisi yang dibuka sebelum jendela ini tidak ikut (TF tidak menentukannya).
    if LIQ_EST_HORIZON_SEC / bar_s < LIQ_EST_MIN_BARS:
        out["reason"] = f"horizon 24 jam hanya {LIQ_EST_HORIZON_SEC / bar_s:.0f} bar di TF ini"
        return out
    t_end = max(c.bar_time.timestamp() for c in candles) + bar_s
    t_start = t_end - LIQ_EST_HORIZON_SEC
    oi_series = [p for p in oi_series if p[0] >= t_start]
    window_s = min(len(candles) * bar_s, LIQ_EST_HORIZON_SEC)
    span = oi_series[-1][0] - oi_series[0][0] if len(oi_series) >= 2 else 0.0
    need = LIQ_EST_MIN_COVERAGE * window_s
    if span < need:
        out["reason"] = f"riwayat OI {span / 3600:.1f} jam, butuh ≥ {need / 3600:.1f} jam"
        return out
    series = sorted(candles, key=lambda c: c.bar_time)
    times = [c.bar_time.timestamp() for c in series]
    n = len(series)
    a = min(c.low for c in series) * (1.0 - range_pct)
    b_top = max(c.high for c in series) * (1.0 + range_pct)
    step = (b_top - a) / n_bins
    edges = [a + i * step for i in range(n_bins + 1)]

    # 1) Peristiwa OI per bar. Kenaikan OI = posisi baru (sisi dari arah harga × ΔOI × harga).
    #    Penurunan OI = penutupan sukarela: pangkas SEMUA posisi hidup secara pro-rata sebesar
    #    fraksi OI yang hilang. (Versi lama mengabaikan penurunan → posisi yang sudah ditutup
    #    tetap dihitung hidup sampai likuidasi, peta membengkak.)
    events: dict[int, list[tuple]] = {}
    n_open = 0
    for (_t0, oi0, p0), (t1, oi1, p1) in zip(oi_series, oi_series[1:]):
        d = oi1 - oi0
        k = liq_bar_index(t1, times, bar_s)
        if k is None:
            continue
        if d > 0 and p1 > 0:
            side = "LONG" if p1 >= p0 else "SHORT"
            events.setdefault(k, []).append(("open", side, d * p1, p1))
            n_open += 1
        elif d < 0 and oi0 > 0:
            events.setdefault(k, []).append(("close", min(1.0, -d / oi0)))
    if not n_open:
        out["reason"] = "tidak ada kenaikan OI dalam window"
        return out

    # 2) Simulasi bar demi bar. Posisi hidup dari bar pembukaan sampai level tersentuh
    #    (dicek mulai bar berikutnya, seperti sebelumnya). Penutupan OI memangkas usd.
    alive: list[dict] = []
    H = [[0.0] * n_bins for _ in range(n)]
    n_pos = 0
    for b in range(n):
        bar = series[b]
        alive = [
            p for p in alive
            if not (p["k"] < b and (bar.low <= p["lvl"] if p["long"] else bar.high >= p["lvl"]))
        ]
        for ev in events.get(b, []):
            if ev[0] == "open":
                _, side, usd_side, entry = ev
                is_long = side == "LONG"
                for lev, w in tiers:
                    lvl = liq_level_est(entry, lev, is_long, mmr)
                    if lvl < a or lvl > b_top:
                        continue
                    bi = min(n_bins - 1, max(0, int((lvl - a) / step)))
                    alive.append({"k": b, "usd": usd_side * w, "lvl": lvl, "long": is_long, "bi": bi})
                    n_pos += 1
            else:
                frac = ev[1]
                for p in alive:
                    p["usd"] *= (1.0 - frac)
        alive = [p for p in alive if p["usd"] > 0]
        for p in alive:
            H[b][p["bi"]] += p["usd"]

    if not n_pos:
        out["reason"] = "semua level likuidasi di luar rentang tampilan"
        return out

    # 3) Status di bar terakhir: posisi hidup, di bawah vs di atas harga
    px_last = series[n - 1].close
    below = above = 0.0
    nb = na = None
    for p in alive:
        lvl = p["lvl"]
        if lvl < px_last:
            below += p["usd"]
            nb = lvl if nb is None else max(nb, lvl)
        elif lvl > px_last:
            above += p["usd"]
            na = lvl if na is None else min(na, lvl)
    out.update(ok=True, edges=edges, H=H, below_usd=below, above_usd=above,
               nearest_below=nb, nearest_above=na, n_positions=n_pos,
               cover_from=oi_series[0][0])
    return out


LIQ_CALIB_MIN_LIFT = 1.2  # hit-rate harus ≥1,2× kontrol waktu-lain agar dianggap bermakna
CALIB_CONTROL_OFFSETS: tuple[int, ...] = (-24, -12, 12, 24)  # bar; zona dari waktu lain, harga sama


def validate_liq_estimate(est: dict, candles, liqs: list[dict], bar_s: float, thr_frac: float = 0.10) -> dict:
    """Kalibrasi kasar: apakah likuidasi tercatat (realized) jatuh di zona estimasi yang aktif?

    Dibandingkan dengan KONTROL waktu: rentang harga bar likuidasi yang SAMA dicek terhadap zona
    dari bar lain (offset ±CALIB_CONTROL_OFFSETS). Zona itu ada di level harga yang sama, jadi
    tidak ada bias ‘geser grid’ (versi lama menggeser setengah lebar grid, sehingga kontrol hampir
    selalu 0 dan lift tidak pernah bisa dihitung). Kontrol = rata-rata hit dari semua offset valid.
    `lift` = hit-rate / hit-rate kontrol. `base` = jumlah hit kontrol yang diharapkan (float).
    Kejadian dinilai hanya bila punya ≥1 pembanding valid: baris kontrol sebelum riwayat OI
    dilewati (tidak ada data); baris kontrol kosong sesudah riwayat dihitung 0 (zona memang tidak ada).
    """
    res = {"n": 0, "hit": 0, "base": 0.0, "lift": None}
    if not est.get("ok") or not liqs:
        return res
    series = sorted(candles, key=lambda c: c.bar_time)
    times = [c.bar_time.timestamp() for c in series]
    edges, H = est["edges"], est["H"]
    a, step = edges[0], edges[1] - edges[0]
    cover = est.get("cover_from")
    for e in liqs:
        k = liq_bar_index(e["time"].timestamp(), times, bar_s)
        # Zona yang dibandingkan = zona HIDUP sebelum bar likuidasi (H[k-1]); posisi yang
        # tersentuh di bar k sudah dihapus dari H[k], jadi tidak boleh dipakai.
        if k is None or k < 1 or k >= len(H):
            continue
        row = H[k - 1]
        mx = max(row) if row else 0.0
        if mx <= 0:
            continue
        thr = thr_frac * mx
        bar = series[k]
        lo_b = min(len(row) - 1, max(0, int((bar.low - a) / step)))
        hi_b = min(len(row) - 1, max(0, int((bar.high - a) / step)))
        # Pembanding waktu-lain. Baris SEBELUM riwayat OI dimulai tidak punya data (dilewati).
        # Baris sesudahnya dengan zona kosong = zona memang tidak ada saat itu → tidak bisa kena (0).
        ctrl_hits: list[float] = []
        for off in CALIB_CONTROL_OFFSETS:
            ci = k - 1 + off
            if ci < 0 or ci >= len(H):
                continue
            if cover is not None and times[ci] < cover:
                continue
            crow = H[ci]
            cmx = max(crow) if crow else 0.0
            if cmx <= 0:
                ctrl_hits.append(0.0)
                continue
            cthr = thr_frac * cmx
            ctrl_hits.append(1.0 if any(crow[bi] >= cthr for bi in range(lo_b, hi_b + 1)) else 0.0)
        # Kejadian dinilai hanya bila punya pembanding (bar cukup dan ada riwayat OI sebelumnya).
        if not ctrl_hits:
            continue
        res["n"] += 1
        if any(row[bi] >= thr for bi in range(lo_b, hi_b + 1)):
            res["hit"] += 1
        res["base"] += sum(ctrl_hits) / len(ctrl_hits)
    # lift hanya bermakna bila kontrol punya kejadian. Kontrol nol kejadian berarti TIDAK ADA
    # pembanding (bukan 'peta pasti lebih baik'): lift tetap None dan caption jujur soal itu.
    if res["n"] and res["base"] > 0:
        res["lift"] = res["hit"] / res["base"]
    return res


async def render_explorer_chart(memory, symbol: str, tf: str) -> tuple[bytes | None, str]:
    """Candle HL (live fetch) + likuidasi tersimpan → PNG. Fail-soft (None, pesan)."""
    if tf not in EXPLORER_BAR_MS:
        tf = "1h"
    # HIP-3: candle memakai nama coin HL ('xyz:XAU'); likuidasi Binance memakai basis ('XAU').
    is_hip3 = _engine.hip3_coin(symbol) is not None
    coin = _engine.hip3_coin(symbol) or symbol
    base = _engine.hip3_base(symbol) if is_hip3 else symbol
    end_ms = int(now_utc().timestamp() * 1000)
    start_ms = end_ms - EXPLORER_BARS * EXPLORER_BAR_MS[tf]
    try:
        candles = await _engine.hl_candle_snapshot(coin, tf, start_ms, end_ms, memory)
    except Exception as e:  # noqa: BLE001
        return None, f"{base} {tf.upper()}: gagal ambil candle ({type(e).__name__}). Coba lagi."
    n = len(candles) if candles else 0
    if n < 10:
        return None, f"{base} {tf.upper()}: data candle kurang ({n} bar). Coba TF lain."
    liqs: list[dict] = []
    coverage = None
    try:
        liqs = memory.get_liquidations(base, start_ms, end_ms)
        coverage = memory.liquidation_coverage_start(base)
    except Exception:  # noqa: BLE001
        liqs, coverage = [], None
    note = _symbol_liq_coverage_note(base) if coverage is None else ""
    bar_s = EXPLORER_BAR_MS[tf] / 1000.0
    est: dict | None = None
    val: dict | None = None
    try:
        oi = memory.get_oi_series(symbol, start_ms, end_ms)
        est = estimate_liq_heatmap(candles, oi, bar_s)
        val = validate_liq_estimate(est, candles, liqs, bar_s)
    except Exception as e:  # noqa: BLE001 — estimasi opsional; chart tetap tampil
        logger.debug("liq estimate fail-soft: %s", type(e).__name__)
        est, val = {"ok": False, "reason": "gagal dihitung"}, None
    ctx = await _explorer_asset_ctx(memory, symbol)
    # Logo: aset HIP-3 pakai monogram (pencarian ticker CoinGecko bisa salah koin, mis. "XAU").
    logo = None if is_hip3 else await coin_logo_png(symbol, memory)
    async with _EXPLORER_RENDER_SEM:
        return await asyncio.to_thread(
            _render_explorer_png_sync, base, tf, candles, liqs, coverage, note,
            est, val, ctx, logo,
        )


EXPLORER_LIQ_SIG_USD = 100_000.0  # total likuidasi 24j minimum agar dianggap sinyal
EXPLORER_LIQ_DOM_RATIO = 2.0      # satu sisi ≥ 2× sisi lain = dominan


def _explorer_read(r8: float | None, lu: float, su: float) -> tuple[str, str]:
    """(ikon, kalimat baca) per koin. Observasi dari funding + likuidasi, bukan rekomendasi."""
    crowd = None
    if r8 is not None:
        if r8 >= FUNDING_CROWD_8H:
            crowd = "long"
        elif r8 <= -FUNDING_CROWD_8H:
            crowd = "short"
    liq = None
    if lu + su >= EXPLORER_LIQ_SIG_USD:
        if lu >= EXPLORER_LIQ_DOM_RATIO * su:
            liq = "long"
        elif su >= EXPLORER_LIQ_DOM_RATIO * lu:
            liq = "short"
    if crowd == "long" and liq == "long":
        return "🔥", "long ramai dan likuidasi long dominan → tekanan turun"
    if crowd == "short" and liq == "short":
        return "❄️", "short ramai dan likuidasi short dominan → tekanan naik"
    if crowd == "long" and liq == "short":
        return "🔥", "long ramai, short yang terlikuidasi → arah bisa berbalik"
    if crowd == "short" and liq == "long":
        return "❄️", "short ramai, long yang terlikuidasi → arah bisa berbalik"
    if crowd == "long":
        return "🔥", "long ramai → rawan long squeeze"
    if crowd == "short":
        return "❄️", "short ramai → rawan short squeeze"
    if liq == "long":
        return "💥", "likuidasi long dominan → tekanan turun"
    if liq == "short":
        return "💥", "likuidasi short dominan → tekanan naik"
    return "·", "netral, tidak ada sisi yang ramai"


def _explorer_rows(memory) -> list[dict]:
    """Satu baris data per koin di daftar Explorer. Sumber tunggal untuk teks dan tombol."""
    coins = explorer_top_coins(memory)
    if not coins:
        return []
    fund: dict[str, float] = {}
    try:
        for r in _latest_funding_rows(memory, 6.0) or []:
            try:
                fund[canon_hl_symbol(str(r["symbol"]))] = float(r["rate"])
            except (TypeError, ValueError, KeyError):
                continue
    except Exception:  # noqa: BLE001 — explorer tetap tampil tanpa funding
        fund = {}
    liq: dict[str, tuple[float, float]] = {}
    try:
        s24 = memory.get_liquidation_summary(now_utc() - timedelta(hours=24), top_n=500)
        for sym, lu, su in s24.get("top", []):
            liq[canon_hl_symbol(str(sym))] = (float(lu), float(su))
    except Exception:  # noqa: BLE001
        liq = {}
    rows: list[dict] = []
    for sym in coins:
        rate = fund.get(sym)
        r8 = rate * FUNDING_PERIODS_PER_8H if rate is not None else None
        has_liq = sym in liq
        lu, su = liq.get(sym, (0.0, 0.0))
        if r8 is None and not has_liq:
            icon, read = "·", "belum ada data funding atau likuidasi"
        else:
            icon, read = _explorer_read(r8, lu, su)
        rows.append({"sym": sym, "r8": r8, "lu": lu, "su": su,
                     "has_liq": has_liq, "icon": icon, "read": read})
    return rows


def _fmt_explorer_list_text(memory) -> str:
    ts_raw = memory.get_runtime("explorer_top_coins_ts") or ""
    try:
        upd = f"Diperbarui {_wib_label(datetime.fromtimestamp(float(ts_raw), tz=UTC), False)} WIB"
    except (TypeError, ValueError):
        upd = "Belum ada data cycle: daftar muncul setelah siklus pertama selesai."
    rows = _explorer_rows(memory)
    lines = [
        "🔍 Coin Explorer",
        f"{len(rows)} koin top volume siklus terakhir · ketuk koin untuk chart" if rows
        else "Top volume siklus terakhir · ketuk koin untuk chart",
        upd,
    ]
    if not rows:
        lines.append("Belum ada koin untuk ditampilkan.")
        return "\n".join(lines)
    n_long = sum(1 for r in rows if r["icon"] == "🔥")
    n_short = sum(1 for r in rows if r["icon"] == "❄️")
    n_liq = sum(1 for r in rows if r["icon"] == "💥")
    n_nodata = sum(1 for r in rows if r["r8"] is None and not r["has_liq"])
    n_flat = len(rows) - n_long - n_short - n_liq - n_nodata
    parts = []
    if n_long:
        parts.append(f"🔥 {n_long} long ramai")
    if n_short:
        parts.append(f"❄️ {n_short} short ramai")
    if n_liq:
        parts.append(f"💥 {n_liq} likuidasi dominan")
    if n_flat:
        parts.append(f"{n_flat} netral")
    if n_nodata:
        parts.append(f"{n_nodata} belum ada data")
    if not parts:
        parts.append("tidak ada sinyal")
    lines += [
        "",
        "▸ Ringkasan",
        " · ".join(parts),
        f"Ramai = funding ≥ {FUNDING_CROWD_8H * 100:.2f}% per 8 jam (normal ~0.01%). "
        f"Dominan = satu sisi likuidasi ≥ {EXPLORER_LIQ_DOM_RATIO:.0f}× sisi lain, total ≥ "
        f"{_usd_short(EXPLORER_LIQ_SIG_USD)}.",
        "",
    ]
    for r in rows:
        if r["r8"] is None:
            lines.append(f"▸ {r['sym']}  funding belum ada")
        else:
            prefix = f"{r['icon']} " if r["icon"] != "·" else ""
            lines.append(f"▸ {prefix}{r['sym']}  {r['r8'] * 100:+.4f}% per 8 jam")
        if r["has_liq"]:
            lines.append(f"   Likuidasi 24j: L {_usd_short(r['lu'])} · S {_usd_short(r['su'])}")
        else:
            lines.append("   Likuidasi 24j: tidak ada data")
        lines.append(f"   → {r['read']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _kb_explorer_list(memory) -> dict:
    try:
        rows = _explorer_rows(memory)
    except Exception:  # noqa: BLE001
        rows = [{"sym": s, "icon": "·"} for s in explorer_top_coins(memory)]
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for r in rows:
        icon = r.get("icon", "·")
        text = f"{icon} {r['sym']}" if icon != "·" else r["sym"]
        row.append({"text": text, "callback_data": f"coin:view:{r['sym']}:1h"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": buttons}


def _kb_explorer_chart(sym: str, tf: str, origin: str = "e") -> dict:
    """Keyboard chart koin. origin='b' = dibuka dari Breadth → kembali ke Breadth;
    'e' (default) = dari Explorer → kembali ke daftar Explorer. Asal dibawa di setiap tombol TF."""
    sfx = ":b" if origin == "b" else ""
    tf_row = [
        {"text": ("✅ " if t == tf else "") + t, "callback_data": f"coin:view:{sym}:{t}{sfx}"}
        for t in EXPLORER_TFS
    ]
    if origin == "b":
        back = {"text": "⬅️ Breadth", "callback_data": "menu:breadth"}
    else:
        back = {"text": "⬅️ Daftar koin", "callback_data": "menu:explore"}
    return {"inline_keyboard": [
        tf_row,
        [back, {"text": "🏠 Menu", "callback_data": "menu:home"}],
    ]}


def _kb_breadth(memory) -> dict:
    """Tombol koin Breadth: persis koin yang tampil di teks (sumber sama: _breadth_picks)."""
    try:
        picks = _breadth_picks(_breadth_parsed(memory), _breadth_liq_top(memory))
        picks += _breadth_hip3_block(memory)[1]
    except Exception:  # noqa: BLE001 — kartu tetap tampil tanpa tombol koin
        picks = []
    buttons: list[list[dict]] = []
    row: list[dict] = []
    for sym, icon in picks:
        label = _engine.hip3_base(sym)  # aset HIP-3 tampil sebagai ticker (XAU), key internal tetap
        row.append({"text": f"{icon} {label}", "callback_data": f"coin:view:{sym}:1h:b"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "⬅️ Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": buttons}


def _explorer_parse(data: str) -> tuple[str, str] | None:
    """coin:view:SYM:TF[:e|:b] → (SYM, TF). Format 4-bagian (lama/Explorer) tetap valid."""
    parts = data.split(":")
    if len(parts) not in (4, 5) or parts[0] != "coin" or parts[1] != "view":
        return None
    if len(parts) == 5 and parts[4] not in ("e", "b"):
        return None
    sym = _engine.hip3_canon_key(parts[2]) or canon_hl_symbol(parts[2])
    tf = parts[3]
    if not (_engine.hip3_coin(sym) or _EXPLORER_SYM_RE.match(sym)) or tf not in EXPLORER_BAR_MS:
        return None
    return sym, tf


def _explorer_origin(data: str) -> str:
    """'b' bila koin dibuka dari Breadth, selain itu 'e' (Explorer)."""
    parts = data.split(":")
    return "b" if len(parts) == 5 and parts[4] == "b" else "e"



async def _send_text_replacing(bot, chat_id, mid, is_photo: bool, text: str, markup) -> None:
    """Pesan teks baru; bila pesan asal adalah foto, hapus foto itu (foto tidak bisa di-edit jadi teks)."""
    await bot.send_message(chat_id, text, reply_markup=markup)
    if is_photo and mid and str(mid).strip().isdigit():
        await bot.delete_message(chat_id, int(mid))


async def _handle_coin_callback(data: str, chat_id, mid, is_photo: bool, bot, memory) -> None:
    parsed = _explorer_parse(data)
    if parsed is None:
        await _send_text_replacing(
            bot, chat_id, mid, is_photo,
            "Koin/TF tidak valid. Buka 🔍 Explorer lagi.", _kb_explorer_list(memory),
        )
        return
    sym, tf = parsed
    key = (sym, tf)
    now_m = time.monotonic()
    hit = _EXPLORER_CACHE.get(key)
    wait_mid: int | None = None
    try:
        if hit and hit.get("png") and (now_m - hit["ts"]) < EXPLORER_CACHE_TTL:
            png, caption, fid = hit["png"], hit["caption"], hit.get("file_id")
        else:
            # Umpan balik langsung: render bisa beberapa detik; tanpa ini layar diam dan user menekan lagi.
            wait_mid = await bot.send_text_ex(chat_id, f"⏳ Menyiapkan chart {sym} {tf.upper()}…")
            png, caption = await render_explorer_chart(memory, sym, tf)
            fid = None
            if png:
                _explorer_cache_store(key, {"ts": now_m, "png": png, "caption": caption, "file_id": None})
            else:
                _EXPLORER_CACHE.pop(key, None)  # error tidak di-cache
        kb = _kb_explorer_chart(sym, tf, _explorer_origin(data))
        if not png:
            await _send_text_replacing(bot, chat_id, mid, is_photo, caption, kb)
            return

        ok, new_fid = False, None
        if is_photo and mid and str(mid).strip().isdigit():
            # Ganti media pesan yang sama (tanpa kirim pesan baru)
            ok, new_fid = await bot.edit_media_photo(
                chat_id, int(mid), png=png, file_id=fid, caption=caption, reply_markup=kb,
            )
            if not ok and fid:
                ok, new_fid = await bot.edit_media_photo(
                    chat_id, int(mid), png=png, file_id=None, caption=caption, reply_markup=kb,
                )
        if not ok:
            ok, new_fid = await bot.send_photo_ex(
                chat_id, png, caption=caption, reply_markup=kb, file_id=fid,
            )
            if not ok and fid:
                ok, new_fid = await bot.send_photo_ex(chat_id, png, caption=caption, reply_markup=kb)
        if ok:
            if new_fid and key in _EXPLORER_CACHE:
                _EXPLORER_CACHE[key]["file_id"] = new_fid
            if not is_photo and mid and str(mid).strip().isdigit():
                await bot.delete_message(chat_id, int(mid))  # rapikan daftar koin setelah chart muncul
            return
        await bot.send_message(chat_id, caption + "\n(chart upload gagal)", reply_markup=kb)
    except Exception as e:  # noqa: BLE001 — render gagal tidak boleh membiarkan pesan tunggu menggantung
        logger.warning("coin chart fail-soft %s %s: %s", sym, tf, type(e).__name__)
        await _send_text_replacing(
            bot, chat_id, mid, is_photo,
            f"Gagal memuat chart {sym} {tf.upper()}. Coba ketuk lagi.", _kb_explorer_list(memory),
        )
    finally:
        if wait_mid:
            await bot.delete_message(chat_id, wait_mid)


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

    state = str(row["state"] or "")
    cache_key = f"{signal_id}:{state}"
    # Cache 60s per (signal, state) — cegah spam tap Chart tanpa menampilkan state basi
    now_m = time.monotonic()
    hit = _CHART_CACHE.get(cache_key)
    if hit and (now_m - hit[0]) < _CHART_CACHE_TTL:
        return hit[1], hit[2]

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
    horizon = str(row["horizon"] or "INTRADAY").upper()
    interval = "15m" if horizon == "SCALPING" else ("4h" if horizon == "SWING" else "1h")
    bars = 80
    end_ms = int(now_utc().timestamp() * 1000)
    bar_ms = {"15m": 15 * 60_000, "1h": 3_600_000, "4h": 4 * 3_600_000}.get(interval, 3_600_000)
    start_ms = end_ms - bars * bar_ms

    try:
        candles = await _engine.hl_candle_snapshot(symbol, interval, start_ms, end_ms, memory)
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
        _CHART_CACHE[cache_key] = (time.monotonic(), png, caption)
        if len(_CHART_CACHE) > 40:
            oldest = min(_CHART_CACHE, key=lambda k: _CHART_CACHE[k][0])
            _CHART_CACHE.pop(oldest, None)
        return png, caption
    except Exception as e:
        logger.warning("chart render fail-soft: %s: %s", type(e).__name__, e)
        return None, f"Chart render error: {type(e).__name__}: {e}"
    finally:
        if fig is not None:
            plt.close(fig)


HTML_MENU_KEYS: frozenset[str] = frozenset({
    "menu:market", "menu:blackswan", "menu:breadth", "menu:settings", "menu:explore",
    "menu:home", "menu:start", "menu:help", "menu:shadow",
    "help:pantau", "help:catatan", "help:alat",
})


def _menu_html(text: str) -> str:
    """Escape semua teks dinamis, lalu tebalkan judul dan header seksi (▸ ...) serta judul seksi plain."""
    lines = text.split("\n")
    out: list[str] = []
    for i, ln in enumerate(lines):
        esc = _html_escape(ln, quote=False)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if i == 0 or ln.startswith("▸ ") or nxt.startswith("────"):
            out.append(f"<b>{esc}</b>" if ln.strip() else esc)
        else:
            out.append(esc)
    return "\n".join(out)


async def _send_menu_message(bot, chat_id, text: str, markup: dict) -> None:
    """Kirim teks menu/panduan dengan judul bold. Gagal → kirim polos."""
    ok = await bot.send_message(chat_id, _menu_html(text), reply_markup=markup, parse_mode="HTML")
    if not ok:
        await bot.send_message(chat_id, text, reply_markup=markup)


async def resolve_menu_callback(
    data: str,
    memory: "MemoryEngine",
) -> tuple[str, dict | None]:
    """
    callback_data → (text, reply_markup).
    Semua menu pakai tombol; tidak ada command text yang wajib diingat user.
    """
    data = (data or "").strip()
    if data in ("menu:home", "menu:start", ""):
        return format_main_menu_text(memory), _kb_main_menu()
    if data == "menu:help":
        return format_help_text(), _kb_help()
    if data.startswith("help:"):
        return format_help_section(data.split(":", 1)[1]), _kb_help_sub()
    if data == "menu:radar":
        return await _fmt_radar(memory), _kb_back()
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
    if data == "menu:shadow":
        return _fmt_shadow_report(memory), _kb_shadow_gates(memory)
    if data.startswith("shadow:gate:"):
        gate = data.split(":", 2)[-1]
        return _fmt_shadow_gate_detail(memory, gate), _kb_shadow_back()
    if data == "menu:settings":
        return _fmt_settings_view(memory), _kb_settings(memory)
    if data == "menu:calc":
        return _fmt_calc_help(), _kb_back()
    if data == "menu:breadth":
        return _fmt_breadth(memory), _kb_breadth(memory)
    if data == "menu:explore":
        return _fmt_explorer_list_text(memory), _kb_explorer_list(memory)
    if data.startswith("set:mode:"):
        mode = data.split(":")[-1].strip().lower()
        if mode == "hybrid":
            mode = "active"
        if mode in ("active", "passive"):
            memory.set_runtime("delivery_mode", mode)
            return (
                "✅ Delivery mode → " + mode + "\n\n" + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        return (
            "⚠️ Mode '" + mode + "' tidak dikenal\n\n" + _fmt_settings_view(memory),
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
    if data == "set:cascade:toggle":
        cur = cascade_mode_raw(memory)
        # cycle explicit: continuation → exhaustion → auto → continuation
        nxt = {"continuation": "exhaustion", "exhaustion": "auto", "auto": "continuation"}.get(cur, "continuation")
        memory.set_runtime("cascade_mode", nxt)
        resolved = cascade_mode(memory) if nxt == "auto" else nxt
        label = f"{nxt} (resolved: {resolved})" if nxt == "auto" else nxt
        return (
            f"Cascade mode → {label}\n\n" + _fmt_settings_view(memory),
            _kb_settings(memory),
        )
    if data == "set:pause:toggle":
        # Pakai signals_paused() — timestamp masa lalu bukan "sedang pause"
        if memory.signals_paused():
            memory.set_runtime("signals_paused_until", "")
            msg = "▶️ Signals resumed"
        else:
            until = (now_utc() + timedelta(hours=1)).isoformat()
            memory.set_runtime("signals_paused_until", until)
            msg = f"⏸ Signals paused 1h until {until[:19]} UTC"
        return msg + "\n\n" + _fmt_settings_view(memory), _kb_settings(memory)
    if data in ("set:kill:on", "set:kill:off"):
        # Target-based: tombol menyatakan tujuan, bukan membalik state.
        want_on = data == "set:kill:on"
        if kill_switch_env_forced():
            return (
                "🔒 Kill switch dikunci env CRYPTONE_KILL_SWITCH.\n"
                "Ubah env di GitHub Actions, UI tidak bisa mengubahnya.\n\n"
                + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        if memory.kill_switch_active() == want_on:
            return (
                ("🛑 Kill switch sudah ON." if want_on else "🟢 Kill switch sudah OFF.")
                + "\n\n" + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        if want_on:
            prompt = "⚠️ Yakin AKTIFKAN kill switch?\nPipeline skip total sampai di-off."
            confirm = "set:kill:confirm:on"
        else:
            prompt = "⚠️ Yakin MATIKAN kill switch?\nPipeline akan hidup lagi."
            confirm = "set:kill:confirm:off"
        return (
            prompt,
            {"inline_keyboard": [
                [
                    {"text": "✅ Ya", "callback_data": confirm},
                    {"text": "❌ Batal", "callback_data": "menu:settings"},
                ],
            ]},
        )
    if data == "set:kill:locked":
        return (
            "🔒 Kill switch dikunci env CRYPTONE_KILL_SWITCH=1.\n"
            "Pipeline tetap skip sampai env di-unset.\n\n" + _fmt_settings_view(memory),
            _kb_settings(memory),
        )
    if data in ("set:kill:confirm:on", "set:kill:confirm:off"):
        # Idempoten: double-tap "Ya" tidak membalik state dua kali.
        want_on = data == "set:kill:confirm:on"
        if kill_switch_env_forced():
            return (
                "🔒 Kill switch dikunci env — perubahan tidak diterapkan.\n\n"
                + _fmt_settings_view(memory),
                _kb_settings(memory),
            )
        if want_on:
            memory.set_runtime("kill_switch", "1")
            memory.set_runtime("kill_switch_set_at", now_utc().isoformat())
            msg = "🛑 Kill switch ON — pipeline skip total"
        else:
            memory.set_runtime("kill_switch", "0")
            memory.set_runtime("kill_switch_set_at", "")
            msg = "🟢 Kill switch OFF — pipeline aktif"
        return msg + "\n\n" + _fmt_settings_view(memory), _kb_settings(memory)
    if data in ("set:kill:toggle", "set:kill:confirm"):
        # Tombol lama dari pesan yang sudah terkirim: tujuannya tidak diketahui,
        # jadi jangan ubah state. Minta operator buka Settings lagi.
        return (
            "ℹ️ Tombol kill ini sudah kedaluwarsa — tidak ada yang diubah.\n"
            "Buka ⚙️ Settings lagi lalu tekan tombol Kill.\n\n"
            + _fmt_settings_view(memory),
            _kb_settings(memory),
        )
    if data.startswith("sig:reason:"):
        sid = data.split(":", 2)[-1]
        return await _fmt_reasoning_with_ai(memory, sid), _kb_back_to_signal(sid)
    if data.startswith("sig:card:"):
        sid = data.split(":", 2)[-1]
        return _fmt_signal_card_from_db(memory, sid), _kb_signal(sid)
    return "Tombol tidak dikenal. Ketuk 🏠 Menu.", _kb_main_menu()


def _tg_multipart_sync(url: str, fields: dict, file_field: str, png: bytes) -> dict:
    """Multipart POST (urllib, tanpa aiohttp). Dipanggil via to_thread."""
    import io
    import uuid
    boundary = "----cryptone" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode())
        buf.write(str(v).encode("utf-8"))
        buf.write(b"\r\n")
    buf.write((
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"chart.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode())
    buf.write(png)
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "cryptone-v45"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _tg_result_file_id(raw) -> tuple[bool, str | None]:
    """(ok, file_id foto terbesar) dari respons sendPhoto / editMessageMedia."""
    if not isinstance(raw, dict) or not raw.get("ok"):
        return False, None
    res = raw.get("result")
    photos = res.get("photo") if isinstance(res, dict) else None
    if isinstance(photos, list) and photos and isinstance(photos[-1], dict):
        fid = str(photos[-1].get("file_id") or "") or None
        return True, fid
    return True, None


def _tg_http_error_is_not_modified(e) -> bool:
    try:
        body = e.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return False
    return "message is not modified" in body.lower()


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
            "chat_id": str(chat_id),
            "text": (text[:3997] + "…") if len(text) > 4000 else text,
            "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            # Bot API JSON body: InlineKeyboardMarkup as object (dict).
            payload["reply_markup"] = reply_markup
        try:
            raw = await http_post_json(self._url("sendMessage"), payload, timeout=20.0)
            if not raw.get("ok"):
                desc = str(raw.get("description", "unknown"))
                if "Too Many Requests" in desc or raw.get("error_code") == 429:
                    params = raw.get("parameters") or {}
                    retry = float(params.get("retry_after", 5))
                    raise TelegramRateLimitError(
                        f"telegram 429 retry={retry}", retry_after=retry,
                    )
                # Markup invalid → retry sekali tanpa keyboard (text tetap multi-line)
                if reply_markup is not None and "reply_markup" in desc.lower():
                    logger.warning(
                        "▸ telegram markup reject · retry text-only · %s", desc[:80],
                    )
                    payload.pop("reply_markup", None)
                    raw2 = await http_post_json(
                        self._url("sendMessage"), payload, timeout=20.0,
                    )
                    if raw2.get("ok"):
                        return True
                    desc = str(raw2.get("description", desc))
                logger.warning("▸ telegram send fail · chat=%s · %s", chat_id, desc)
                return False
            return True
        except TelegramRateLimitError:
            raise
        except Exception as e:
            logger.warning("▸ telegram error · %s", type(e).__name__)
            return False

    async def send_text_ex(self, chat_id, text: str, *, reply_markup: dict | None = None) -> int | None:
        """Kirim teks dan kembalikan message_id (untuk pesan sementara yang dihapus nanti). Fail-soft."""
        if not self.available() or not chat_id:
            return None
        payload: dict[str, Any] = {
            "chat_id": str(chat_id), "text": text[:4000], "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            raw = await http_post_json(self._url("sendMessage"), payload, timeout=20.0)
        except Exception as e:  # noqa: BLE001
            logger.debug("send_text_ex fail-soft: %s", type(e).__name__)
            return None
        if not raw.get("ok"):
            return None
        try:
            return int(raw["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

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
        try:
            import aiohttp  # type: ignore
        except ImportError:
            aiohttp = None  # type: ignore
        if aiohttp is None:
            # urllib multipart fallback (env tanpa aiohttp)
            try:
                import io, uuid as _uuid
                boundary = "----cryptone" + _uuid.uuid4().hex
                buf = io.BytesIO()
                def _part(name, value, filename=None, ctype=None):
                    buf.write(f"--{boundary}\r\n".encode())
                    disp = f'Content-Disposition: form-data; name="{name}"'
                    if filename:
                        disp += f'; filename="{filename}"'
                    buf.write(f"{disp}\r\n".encode())
                    if ctype:
                        buf.write(f"Content-Type: {ctype}\r\n".encode())
                    buf.write(b"\r\n")
                    buf.write(value if isinstance(value, (bytes, bytearray)) else str(value).encode())
                    buf.write(b"\r\n")
                _part("chat_id", str(chat_id))
                if caption:
                    _part("caption", caption[:1024])
                if reply_markup is not None:
                    _part("reply_markup", json.dumps(reply_markup))
                _part("photo", png_bytes, filename="chart.png", ctype="image/png")
                buf.write(f"--{boundary}--\r\n".encode())
                data = buf.getvalue()
                req = urllib.request.Request(
                    self._url("sendPhoto"), data=data,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                             "User-Agent": "cryptone-v45"}, method="POST",
                )
                def _do():
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        return json.loads(resp.read().decode())
                raw = await asyncio.to_thread(_do)
                return bool(raw.get("ok"))
            except Exception as e:
                logger.debug("sendPhoto urllib fail-soft: %s", type(e).__name__)
                return False
        try:
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
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form) as resp:
                    raw = await resp.json(content_type=None)
                    return bool(raw.get("ok"))
        except Exception as e:
            logger.debug("sendPhoto fail-soft: %s", type(e).__name__)
            return False

    async def delete_message(self, chat_id, message_id) -> bool:
        if not self.available() or not chat_id or not str(message_id).strip().isdigit():
            return False
        try:
            raw = await http_post_json(
                self._url("deleteMessage"),
                {"chat_id": str(chat_id), "message_id": int(message_id)},
                timeout=10.0,
            )
            return bool(isinstance(raw, dict) and raw.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def send_photo_ex(
        self, chat_id, png: bytes | None, *, caption: str = "",
        reply_markup: dict | None = None, file_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Kirim foto; pakai file_id bila ada (tanpa upload). Return (ok, file_id foto)."""
        if not self.available() or not chat_id or (not png and not file_id):
            return False, None
        try:
            if file_id:
                payload: dict = {"chat_id": str(chat_id), "photo": file_id}
                if caption:
                    payload["caption"] = caption[:1024]
                if reply_markup is not None:
                    payload["reply_markup"] = reply_markup
                raw = await http_post_json(self._url("sendPhoto"), payload, timeout=30.0)
            else:
                fields: dict = {"chat_id": str(chat_id)}
                if caption:
                    fields["caption"] = caption[:1024]
                if reply_markup is not None:
                    fields["reply_markup"] = json.dumps(reply_markup)
                raw = await asyncio.to_thread(
                    _tg_multipart_sync, self._url("sendPhoto"), fields, "photo", png,
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("sendPhoto_ex fail-soft: %s", type(e).__name__)
            return False, None
        return _tg_result_file_id(raw)

    async def edit_media_photo(
        self, chat_id, message_id, *, png: bytes | None = None, file_id: str | None = None,
        caption: str = "", reply_markup: dict | None = None,
    ) -> tuple[bool, str | None]:
        """Ganti foto+caption+keyboard pesan yang sudah ada. 'not modified' dianggap sukses."""
        if not self.available() or not chat_id or not str(message_id).strip().isdigit():
            return False, None
        if not png and not file_id:
            return False, None
        media: dict = {"type": "photo", "caption": caption[:1024]}
        try:
            if file_id:
                media["media"] = file_id
                payload: dict = {
                    "chat_id": str(chat_id), "message_id": int(message_id), "media": media,
                }
                if reply_markup is not None:
                    payload["reply_markup"] = reply_markup
                raw = await http_post_json(self._url("editMessageMedia"), payload, timeout=30.0)
            else:
                media["media"] = "attach://chart"
                fields: dict = {
                    "chat_id": str(chat_id),
                    "message_id": str(int(message_id)),
                    "media": json.dumps(media),
                }
                if reply_markup is not None:
                    fields["reply_markup"] = json.dumps(reply_markup)
                raw = await asyncio.to_thread(
                    _tg_multipart_sync, self._url("editMessageMedia"), fields, "chart", png,
                )
        except urllib.error.HTTPError as e:
            if _tg_http_error_is_not_modified(e):
                return True, None
            return False, None
        except Exception:  # noqa: BLE001
            return False, None
        return _tg_result_file_id(raw)

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        if not self.available():
            return False
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": (text[:3997] + "…") if len(text) > 4000 else text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            raw = await http_post_json(self._url("editMessageText"), payload, timeout=15.0)
            if raw.get("ok"):
                return True
            desc = str(raw.get("description") or "")
            # Same content — treat as success (hindari duplikasi pesan fallback)
            if "message is not modified" in desc.lower():
                return True
            return False
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
    """Process Telegram updates; persist offset INSIDE handler (anti double-toggle)."""
    global _TG_UPDATE_OFFSET, _TG_AUTH_WARNED
    if not bot.available() or dry_run:
        return 0
    allowed = _allowed_chat_ids()
    if not allowed:
        if not _TG_AUTH_WARNED:
            logger.warning("telegram UI · no allowed chat_ids · UI disabled")
            _TG_AUTH_WARNED = True
        return 0

    updates = await bot.get_updates(
        offset=_TG_UPDATE_OFFSET, timeout=max(0, int(long_poll_sec)),
    )
    n = 0
    for upd in updates:
        uid = int(upd.get("update_id") or 0)
        if uid >= _TG_UPDATE_OFFSET:
            _TG_UPDATE_OFFSET = uid + 1
            # Persist SEBELUM handle — crash mid-batch ≠ replay toggle
            try:
                memory.set_runtime("tg_update_offset", str(_TG_UPDATE_OFFSET))
            except Exception:  # noqa: BLE001
                pass
        try:
            await _handle_one_update(upd, bot, memory, allowed, dry_run)
            n += 1
        except Exception as e:  # noqa: BLE001 — per-update fail-soft
            logger.warning(
                "telegram update %d fail-soft: %s", uid, type(e).__name__,
            )
            # Spinner sudah hilang (answer_callback); beri feedback supaya user tidak "sunyi".
            cq = upd.get("callback_query") or {}
            fb_chat = str(((cq.get("message") or {}).get("chat") or {}).get("id") or "")
            if fb_chat and fb_chat in allowed:
                try:
                    await bot.send_message(
                        fb_chat,
                        "⚠️ Menu error — ketuk ulang atau 🏠 Menu.",
                        reply_markup=_kb_main_menu(),
                    )
                except Exception:  # noqa: BLE001
                    pass

    # Offset sudah di-persist per-update di atas (anti replay).
    return n


async def _handle_one_update(
    upd: dict,
    bot: TelegramBot,
    memory: "MemoryEngine",
    allowed: set[str],
    dry_run: bool,
) -> None:
    """Proses satu update. Isolasi per-update supaya batch tidak mati."""
    cq = upd.get("callback_query")
    if cq:
        cq_id = str(cq.get("id") or "")
        data = str(cq.get("data") or "")
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        mid = msg.get("message_id")
        if chat_id not in allowed:
            await bot.answer_callback(cq_id, "Unauthorized", show_alert=True)
            return
        await bot.answer_callback(cq_id)
        if data.startswith("sig:chart:"):
            sid = data.split(":", 2)[-1]
            png, caption = await render_signal_chart_png(memory, sid)
            if png:
                ok = await bot.send_photo(
                    chat_id, png, caption=caption,
                    reply_markup=_kb_back_to_signal(sid),
                )
                if not ok:
                    await bot.send_message(
                        chat_id, caption + "\n(chart upload gagal)",
                        reply_markup=_kb_back_to_signal(sid),
                    )
            else:
                await bot.send_message(
                    chat_id, caption or "Chart tidak tersedia.",
                    reply_markup=_kb_back_to_signal(sid),
                )
            return
        is_photo = bool(msg.get("photo"))
        if data.startswith("coin:view:"):
            await _handle_coin_callback(data, chat_id, mid, is_photo, bot, memory)
            return
        if data.startswith("sig:reason:"):
            await _handle_reason_callback(data, chat_id, mid, is_photo, bot, memory)
            return
        text, markup = await resolve_menu_callback(data, memory)
        if is_photo:
            await _send_text_replacing(bot, chat_id, mid, True, text, markup)
            return
        pm = "HTML" if (data in HTML_MENU_KEYS or data.startswith("shadow:gate:")) else None
        body = _menu_html(text) if pm else text
        if mid and str(mid).strip().isdigit():
            ok = await bot.edit_message(chat_id, int(mid), body, reply_markup=markup, parse_mode=pm)
            if not ok:
                ok = await bot.send_message(chat_id, body, reply_markup=markup, parse_mode=pm)
                if not ok and pm:
                    await bot.send_message(chat_id, text, reply_markup=markup)
        else:
            ok = await bot.send_message(chat_id, body, reply_markup=markup, parse_mode=pm)
            if not ok and pm:
                await bot.send_message(chat_id, text, reply_markup=markup)
        return

    msg = upd.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    body = (msg.get("text") or "").strip()
    if not chat_id or not body or chat_id not in allowed:
        return
    low = body.lower()
    if low in ("/start", "/menu", "menu", "start"):
        await _send_menu_message(
            bot, chat_id, format_main_menu_text(memory), _kb_main_menu(),
        )
    elif low in ("/help", "help"):
        await _send_menu_message(bot, chat_id, format_help_text(), _kb_help())
    elif low.split(maxsplit=1)[:1] == ["calc"]:
        calc_out = _parse_calc_message(body)
        if calc_out:
            await bot.send_message(chat_id, calc_out, reply_markup=_kb_back())
    elif low.startswith("/"):
        await bot.send_message(
            chat_id,
            "Pakai tombol di menu ya — lebih cepat.\n"
            "Ketuk /menu kalau keyboard hilang.",
            reply_markup=_kb_main_menu(),
        )


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
    global _TG_UPDATE_OFFSET
    # Restore offset dari DB — cegah replay update Telegram (~24h) saat restart
    try:
        raw_off = (memory.get_runtime("tg_update_offset") or "").strip()
        if raw_off.isdigit():
            _TG_UPDATE_OFFSET = int(raw_off)
            logger.info("📲 telegram UI · offset restored %d", _TG_UPDATE_OFFSET)
    except Exception:
        pass
    logger.info("📲 telegram UI loop · long-poll %ds", poll_sec)
    # Offset persist ada di handle_telegram_updates (anti race double-toggle)
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

def _telegram_msg_to_payload(msg: TelegramMessage) -> dict:
    """Serialize TelegramMessage for pending_delivery (tanpa chart_bytes)."""
    return {
        "text": msg.text,
        "priority": msg.priority,
        "parse_mode": msg.parse_mode,
        "reply_markup": msg.reply_markup,
    }


def _telegram_msg_from_payload(chat_id: str, payload: dict) -> TelegramMessage:
    return TelegramMessage(
        chat_id=str(chat_id),
        text=str(payload.get("text") or ""),
        priority=str(payload.get("priority") or "normal"),
        parse_mode=payload.get("parse_mode"),
        reply_markup=payload.get("reply_markup"),
    )


class TelegramDeliveryQueue:
    """
    In-memory queue + optional SQLite pending_delivery fallback.
    Gagal kirim (bukan rate-limit break) → persist; startup reload → retry.
    """

    MAX_PENDING_ATTEMPTS = 8

    def __init__(self, memory: "MemoryEngine | None" = None):
        self._queue: asyncio.Queue[TelegramMessage] = asyncio.Queue()
        self._memory = memory

    def bind_memory(self, memory: "MemoryEngine") -> None:
        self._memory = memory

    async def enqueue(self, message: TelegramMessage) -> None:
        await self._queue.put(message)

    def empty(self) -> bool:
        return self._queue.empty()

    def _persist_failed(self, msg: TelegramMessage, error: str, attempts: int = 1) -> None:
        if self._memory is None:
            logger.warning(
                "▸ SEND FAIL (no memory) → chat %s · %s", msg.chat_id, error[:80],
            )
            return
        try:
            self._memory.enqueue_pending_delivery(
                msg.chat_id,
                _telegram_msg_to_payload(msg),
                attempts=attempts,
                last_error=error[:200],
            )
            logger.warning(
                "▸ SEND FAIL → pending_delivery chat %s · %s",
                msg.chat_id, error[:80],
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "▸ pending_delivery persist fail: %s · chat %s",
                type(e).__name__, msg.chat_id,
            )

    def load_from_db(self) -> int:
        """Muat pending_delivery ke memory queue (startup). Return jumlah dimuat."""
        if self._memory is None:
            return 0
        try:
            self._memory.prune_pending_delivery(
                max_age_hours=48.0, max_attempts=self.MAX_PENDING_ATTEMPTS,
            )
            rows = self._memory.load_pending_deliveries(limit=200)
        except Exception as e:  # noqa: BLE001
            logger.warning("load pending_delivery fail-soft: %s", type(e).__name__)
            return 0
        n = 0
        for row in rows:
            if not row["payload"].get("text"):
                self._memory.delete_pending_delivery(row["id"])
                continue
            msg = _telegram_msg_from_payload(row["chat_id"], row["payload"])
            # tandai id di object agar drain bisa hapus/bump
            msg._pending_id = row["id"]  # type: ignore[attr-defined]
            msg._pending_attempts = row["attempts"]  # type: ignore[attr-defined]
            self._queue.put_nowait(msg)
            n += 1
        if n:
            logger.info("📬 pending_delivery · loaded %d", n)
        return n

    async def drain_once(
        self,
        bot: TelegramBot,
        *,
        dry_run: bool = False,
        min_interval: float = TELEGRAM_DRAIN_MIN_INTERVAL_SEC,
    ) -> int:
        """Kirim semua pesan tertunda. Return jumlah terkirim."""
        sent = 0
        while not self._queue.empty():
            msg = self._queue.get_nowait()
            pending_id = getattr(msg, "_pending_id", None)
            attempts = int(getattr(msg, "_pending_attempts", 0) or 0)
            if dry_run:
                logger.info(
                    "▸ DRY-RUN → chat %s | %s",
                    msg.chat_id, msg.text.replace("\n", " · ")[:120],
                )
                sent += 1
                if pending_id is not None and self._memory is not None:
                    self._memory.delete_pending_delivery(int(pending_id))
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
                    n_lines = msg.text.count("\n") + 1
                    has_kb = bool(msg.reply_markup and (
                        msg.reply_markup.get("inline_keyboard")
                        if isinstance(msg.reply_markup, dict) else True
                    ))
                    logger.info(
                        "📤 SENT · lines=%d · kb=%s · build=%s · %s",
                        n_lines, has_kb, globals().get("CRYPTONE_BUILD", "?"),
                        msg.text.split("\n")[0][:70],
                    )
                    if pending_id is not None and self._memory is not None:
                        self._memory.delete_pending_delivery(int(pending_id))
                else:
                    # soft fail: persist / bump
                    if pending_id is not None and self._memory is not None:
                        new_att = attempts + 1
                        if new_att >= self.MAX_PENDING_ATTEMPTS:
                            self._memory.delete_pending_delivery(int(pending_id))
                            logger.error(
                                "▸ pending_delivery drop after %d attempts · chat %s",
                                new_att, msg.chat_id,
                            )
                        else:
                            self._memory.bump_pending_delivery(
                                int(pending_id), new_att, "send_returned_false",
                            )
                    else:
                        self._persist_failed(msg, "send_returned_false", attempts=1)
            except TelegramRateLimitError as e:
                logger.warning("▸ telegram rate limit · sleep %.0fs", e.retry_after)
                await asyncio.sleep(max(0.0, float(getattr(e, "retry_after", 1) or 1)))
                # Retry SEGERA setelah sleep — jangan tunggu cycle berikutnya
                try:
                    ok = await bot.send_message(
                        msg.chat_id,
                        msg.text,
                        parse_mode=msg.parse_mode,
                        reply_markup=msg.reply_markup,
                    )
                    if ok:
                        sent += 1
                        n_lines = msg.text.count("\n") + 1
                        has_kb = bool(msg.reply_markup and (
                            msg.reply_markup.get("inline_keyboard")
                            if isinstance(msg.reply_markup, dict) else True
                        ))
                        logger.info(
                            "📤 SENT · lines=%d · kb=%s · build=%s · %s",
                            n_lines, has_kb, globals().get("CRYPTONE_BUILD", "?"),
                            msg.text.split("\n")[0][:70],
                        )
                        if pending_id is not None and self._memory is not None:
                            self._memory.delete_pending_delivery(int(pending_id))
                        await asyncio.sleep(min_interval)
                        continue
                except TelegramRateLimitError:
                    await self._queue.put(msg)
                    break
                except Exception as e2:  # noqa: BLE001
                    err = f"{type(e2).__name__}: {e2}"
                    if pending_id is not None and self._memory is not None:
                        new_att = attempts + 1
                        if new_att >= self.MAX_PENDING_ATTEMPTS:
                            self._memory.delete_pending_delivery(int(pending_id))
                        else:
                            self._memory.bump_pending_delivery(
                                int(pending_id), new_att, err[:200],
                            )
                    else:
                        self._persist_failed(msg, err, attempts=1)
                else:
                    # ok was False
                    if pending_id is not None and self._memory is not None:
                        new_att = attempts + 1
                        if new_att >= self.MAX_PENDING_ATTEMPTS:
                            self._memory.delete_pending_delivery(int(pending_id))
                        else:
                            self._memory.bump_pending_delivery(
                                int(pending_id), new_att, "send_returned_false",
                            )
                    else:
                        self._persist_failed(msg, "send_returned_false", attempts=1)
                break
            except Exception as e:  # noqa: BLE001 — network / unexpected
                err = f"{type(e).__name__}: {e}"
                if pending_id is not None and self._memory is not None:
                    new_att = attempts + 1
                    if new_att >= self.MAX_PENDING_ATTEMPTS:
                        self._memory.delete_pending_delivery(int(pending_id))
                    else:
                        self._memory.bump_pending_delivery(
                            int(pending_id), new_att, err[:200],
                        )
                else:
                    self._persist_failed(msg, err, attempts=1)
            await asyncio.sleep(min_interval)
        return sent

    async def flush_to_pending(self) -> int:
        """Saat shutdown: sisa queue → pending_delivery agar tidak hilang."""
        n = 0
        while not self._queue.empty():
            msg = self._queue.get_nowait()
            pending_id = getattr(msg, "_pending_id", None)
            if pending_id is not None:
                # sudah di DB — biarkan
                n += 1
                continue
            self._persist_failed(msg, "shutdown_flush", attempts=0)
            n += 1
        return n

# =============================================================================
# §14  run_live_service + recovery
# =============================================================================



def _format_close_alert(
    *,
    outcome: str,
    symbol: str,
    direction: str,
    reason: str,
    px: float | None = None,
    realized_rr: float | None = None,
) -> str:
    """Notifikasi close/TP1 rapi untuk Telegram (trader-facing)."""
    dir_u = (direction or "").upper()
    dir_icon = "🟢" if dir_u == "LONG" else ("🔴" if dir_u == "SHORT" else "⚪")
    reason_label = {
        "TP1": "Take Profit 1",
        "TP2": "Take Profit 2",
        "SL": "Stop Loss",
        "TIME_STOP": "Time stop · stall di zone",
        "expired": "Expired",
        "arm_timeout": "No entry · arm timeout",
        "missed_entry": "Missed entry · harga lewat TP",
    }.get(reason, reason or "—")
    if reason == "TP1":
        lines = [
            f"🎯  TP1 HIT  ·  {symbol} {dir_icon} {dir_u}",
            "━━━━━━━━━━━━━━━━",
        ]
        if px is not None:
            lines.append(f"Exit     {_ui_px(px)}")
        lines += [
            "SL       → breakeven",
            "Sisa     → TP2 (masih aktif)",
        ]
        return "\n".join(lines)
    icon = {
        "PROFIT": "✅", "LOSS": "🛑",
        "PARTIAL": "🎯", "NEUTRAL": "⏱",
    }.get(str(outcome or "").upper(), "•")
    oc = str(outcome or "—").upper()
    lines = [
        f"{icon}  CLOSE  ·  {symbol} {dir_icon} {dir_u}",
        "━━━━━━━━━━━━━━━━",
        f"Hasil    {oc}",
        f"Alasan   {reason_label}",
    ]
    if px is not None:
        lines.append(f"Exit     {_ui_px(px)}")
    if realized_rr is not None:
        try:
            lines.append(f"R:R      {float(realized_rr):+.2f}")
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)


def _format_bs_alert_text(payload: dict) -> str:
    """Format black-swan Telegram text from pending payload."""
    if payload.get("resume"):
        hrs = payload.get("hours", "?")
        return (
            "✅  BLACK SWAN CLEARED\n"
            "━━━━━━━━━━━━━━━━\n"
            f"Held     {hrs}h\n"
            "Status   indikator normal\n"
            "Pipeline penuh kembali"
        )
    inds = payload.get("indicators") or []
    labels = [_BS_IND_LABEL.get(str(x), str(x)) for x in inds]
    ind_line = ", ".join(labels) if labels else "—"
    return (
        "🚨  BLACK SWAN MODE\n"
        "━━━━━━━━━━━━━━━━\n"
        f"Indikator  {ind_line}\n"
        f"Count      {payload.get('count', '?')}/7 aktif\n"
        "Aksi       sinyal aktif +6h\n"
        "Mode       hanya setup kualitas tinggi"
    )


async def _dispatch_bs_alert(
    memory: "MemoryEngine",
    queue: "TelegramDeliveryQueue",
    user_chat: str,
    op_chat: str,
) -> None:
    """Drain black_swan_alert_pending once and enqueue critical messages.

    Parse dulu, baru clear — jangan lose alert kalau JSON corrupt.
    Multi-runner: claim_alert supaya hanya 1 proses yang push.
    """
    raw = memory.get_runtime("black_swan_alert_pending")
    if not raw:
        return
    try:
        _ck = "bs_alert:" + hashlib.sha256(str(raw).encode()).hexdigest()[:24]
        if not memory.claim_alert(_ck, reason="bs_alert"):
            memory.set_runtime("black_swan_alert_pending", "")
            return
    except Exception:
        pass
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict) or not payload:
        logger.warning(
            "BS alert · unexpected payload %r — fallback generic",
            str(raw)[:80],
        )
        payload = {"indicators": ["unparsed"], "count": 0}
    # Clear hanya setelah payload siap (valid atau fallback)
    memory.set_runtime("black_swan_alert_pending", "")
    plain = _format_bs_alert_text(payload)
    for chat in {c for c in (user_chat, op_chat) if c}:
        await queue.enqueue(TelegramMessage(
            chat_id=chat, text=plain, priority="critical",
        ))


async def run_live_service(dry_run: bool = False) -> None:
    """
    Entry point --live / --dry-run (§XIII.3, §XVII).
    Loop cycle sampai max_runtime; SIGTERM flush bersih.
    """
    # Set di engine module — star-import hanya copy, bukan reference.
    _engine.mark_engine_started()
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
    # Warm Binance USDT-M symbol cache (fail-soft) for k-prefix resolve
    try:
        await refresh_binance_usdt_symbols(None, force=False)
    except Exception as e:
        logger.debug("binance symbol cache warm fail-soft: %s", type(e).__name__)

    # Integrity gate (§XVIII.5) — corrupt → restore backup → fresh schema
    # OperationalError (locked) ≠ corrupt — lanjut, WAL recover sendiri.
    if Path(db_path).exists():
        try:
            healthy = await startup_db_check(db_path)
        except sqlite3.OperationalError as e:
            logger.warning(
                "DB locked · %s · lanjut apa adanya (WAL recover)", type(e).__name__,
            )
            healthy = True
        if not healthy:
            try:
                _gc = sqlite3.connect(db_path, timeout=10.0)
                n_sig_guard = int(_gc.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0])
                _gc.close()
            except Exception:
                n_sig_guard = 0
            if n_sig_guard > 1000:
                logger.error(
                    "DB corrupt tapi berisi %d sinyal · TIDAK di-rebuild, run dihentikan",
                    n_sig_guard,
                )
                return
            logger.error("DB integrity FAIL · %s", db_path)
            if not restore_from_previous_artifact():
                db_path = create_fresh_db()
            else:
                try:
                    still_ok = await startup_db_check(db_path)
                except sqlite3.OperationalError:
                    still_ok = True
                if not still_ok:
                    logger.error("DB still corrupt after restore · rebuild")
                    db_path = create_fresh_db()

    memory = MemoryEngine(db_path)
    memory.start()
    queue = TelegramDeliveryQueue(memory)
    bot = TelegramBot()
    if bot.available():
        logger.info("🤖 telegram ready · build %s", globals().get("CRYPTONE_BUILD", "?"))
        logger.info("📋 signal card = multi-line + inline keyboard (Reasoning/Chart/Journal)")
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

    # Telegram UI start SETELAH bootstrap (hindari memory lama setelah rebind)
    ui_stop = asyncio.Event()
    ui_task: asyncio.Task | None = None

    loop = asyncio.get_event_loop()

    def _sigterm_handler():
        logger.info("SIGTERM/SIGINT — flush & exit (cancel-safe)")
        loop.create_task(flush_and_exit(memory, queue, timeout=20))

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
        loop.add_signal_handler(signal.SIGINT, _sigterm_handler)
    except NotImplementedError:
        pass

    circuit_hydrate(memory)
    _engine.set_circuit_memory(memory)  # type: ignore[attr-defined]
    await startup_recovery(memory)
    # Muat pesan Telegram yang gagal di-run sebelumnya
    try:
        queue.load_from_db()
    except Exception as e:
        logger.debug("pending_delivery load skip: %s", type(e).__name__)

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
            queue.bind_memory(memory)
            # Cascade WS tetap jalan di background — WAJIB rebind ke memory baru
            # (kalau tidak, write queue menunjuk engine yang sudah close)
            get_cascade_buffer().bind_memory(memory)
            _engine.set_circuit_memory(memory)  # type: ignore[attr-defined]
            try:
                circuit_hydrate(memory)
            except Exception as e:
                logger.debug("circuit hydrate after bootstrap fail-soft: %s", type(e).__name__)
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
    try:
        warm_atr_cache(memory)
    except Exception as e:
        logger.debug("ATR warm skip: %s", type(e).__name__)
    try:
        warm_htf_cache(memory)
    except Exception as e:
        logger.debug("HTF warm skip: %s", type(e).__name__)

    # Feed FF (USD memicu jeda, non-USD info) — berjalan walau Gemini tidak aktif
    try:
        await refresh_macro_feed(memory)
    except Exception as e:
        logger.debug("macro feed skip: %s", type(e).__name__)
    # §V.3 #12 — macro calendar: builtin seed + Gemini enrich (1×/hari)
    try:
        gem = get_gemini_client(memory)
        if gem.available():
            await refresh_binance_usdt_symbols(memory)
            await gem.refresh_macro_calendar(memory)
    except Exception as e:
        logger.debug("macro calendar gemini skip: %s", type(e).__name__)
    try:
        evs = parse_macro_events(memory)
        nxt = minutes_to_next_macro(memory)
        today_n = len(macro_events_today(memory))
        nxt_wib = ""
        if nxt[0] is not None:
            at = now_utc() + timedelta(minutes=float(nxt[0]))
            nxt_wib = " · " + _as_wib(at).strftime("%H:%M WIB")
        logger.info(
            "📅 macro calendar · %d events · today=%d · next=%s %s%s%s",
            len(evs),
            today_n,
            (nxt[1] or "—"),
            f"{nxt[0]:.0f}m" if nxt[0] is not None else "",
            nxt_wib,
            f" · {nxt[2]}" if nxt[0] is not None and nxt[2] else "",
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

    # Telegram Super App UI — start SETELAH bootstrap/rebind memory
    if bot.available() and not dry_run and ui_task is None:
        ui_task = asyncio.create_task(
            telegram_ui_loop(bot, memory, ui_stop, dry_run=dry_run, poll_sec=25),
            name="telegram_ui",
        )
        logger.info("📲 telegram UI · background long-poll on · memory bound")

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
            try:
                memory.set_runtime("last_cycle_start_ts", now_utc().isoformat())
            except Exception:
                pass
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
                _seen_tp1_symbols: set[str] = set()
                for ev in closed_ev:
                    reason = ev.get("reason") or ""
                    sym = ev.get("symbol") or "?"
                    direction = ev.get("direction") or ""
                    sid_ev = str(ev.get("signal_id") or "")
                    outcome_ev = str(ev.get("outcome") or "")
                    # TP1: dedup per symbol + 5m bucket (dup active → spam)
                    if reason == "TP1":
                        if sym in _seen_tp1_symbols:
                            logger.debug("⏭ TP1 alert skip dup · %s", sym)
                            continue
                        _seen_tp1_symbols.add(sym)
                        bucket = int(time.time() // 300)
                        dedup_key = f"tp1:{sym}:{bucket}"
                    else:
                        dedup_key = f"{sid_ev}:{reason}:{outcome_ev}"
                    if sid_ev and not memory.claim_alert(
                        dedup_key, signal_id=sid_ev, reason=str(reason),
                    ):
                        logger.debug(
                            "⏭ alert dedup skip · %s %s · %s",
                            sym, direction, reason,
                        )
                        continue
                    if reason == "TP1":
                        plain_tp1 = _format_close_alert(
                            outcome="PARTIAL",
                            symbol=sym,
                            direction=direction,
                            reason="TP1",
                            px=ev.get("px"),
                        )
                        for chat in {c for c in (user_chat, op_chat) if c}:
                            await queue.enqueue(TelegramMessage(
                                chat_id=chat, text=plain_tp1, priority="normal",
                            ))
                        continue
                    plain_c = _format_close_alert(
                        outcome=str(ev.get("outcome") or ""),
                        symbol=sym,
                        direction=direction,
                        reason=str(reason or ""),
                        px=ev.get("px"),
                        realized_rr=ev.get("realized_rr"),
                    )
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
                # Drain lifecycle alerts SEGERA — jangan nunggu pipeline selesai
                # (kalau cycle error di scan, close alert tetap terkirim)
                if closed_ev:
                    await queue.drain_once(bot, dry_run=dry_run)

                # Black swan Telegram alert (set by check_black_swan)
                await _dispatch_bs_alert(memory, queue, user_chat, op_chat)

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
                    logger.info("🌊 cascade top · %s", format_cascade_top(c_map))
                memory._cycle_n_hint = cycle_n  # type: ignore[attr-defined]
                sigs = await run_pipeline_cycle(
                    memory,
                    pinned=pinned,
                    cascade_map=c_map,
                    cascade_side_map=c_side,
                    dry_run=dry_run,
                )
                total_signals += len(sigs)
                try:
                    await refresh_market_brief(memory)
                except Exception:
                    pass
                # BS alert set di pipeline — kirim cycle ini (jangan nunggu 5m)
                _uch = os.environ.get("CRYPTONE_USER_CHAT_ID", "").strip()
                _och = os.environ.get("CRYPTONE_OPERATOR_CHAT_ID", "").strip()
                await _dispatch_bs_alert(memory, queue, _uch, _och)
                if cycle_n % 12 == 0:
                    memory.prune_ephemeral()
                    try:
                        backup_db(memory.db_path, suffix=".periodic.bak")
                    except Exception as e:
                        logger.debug("periodic backup skip: %s", type(e).__name__)
                    try:
                        await backfill_shadow_outcomes(memory, limit=40)
                    except Exception as e:
                        logger.debug("shadow backfill skip: %s", type(e).__name__)
                    try:
                        await record_excursions(memory, limit=10)
                    except Exception as e:
                        logger.debug("excursion record skip: %s", type(e).__name__)
                # Pending delivery re-load (Telegram outage > startup limit)
                if cycle_n % 6 == 0:
                    try:
                        n_pend = queue.load_from_db()
                        if n_pend:
                            logger.info("📬 pending_delivery reload · %d", n_pend)
                    except Exception as e:
                        logger.debug("pending reload skip: %s", type(e).__name__)
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
                    plain = format_signal_message(s, memory)
                    kb = _kb_signal(s.signal_id)
                    push_ok, push_note = should_push_signal(s.tier, memory)
                    if not push_ok:
                        logger.info(
                            "⏸ hold push %s %s Tier %s · %s (tetap di DB/Radar)",
                            s.symbol, s.direction, s.tier, push_note,
                        )
                        continue
                    # Sanity: kartu multi-line (tanpa double-render)
                    if plain.count("\n") < 5:
                        logger.error(
                            "▸ CARD FORMAT BUG · %s · lines=%d",
                            s.symbol, plain.count("\n") + 1,
                        )
                    targets: list[str] = []
                    if user_chat:
                        targets.append(user_chat)
                    if op_chat and op_chat != user_chat and s.tier == "A":
                        targets.append(op_chat)
                    if not targets and op_chat:
                        targets.append(op_chat)
                    if not kb or not kb.get("inline_keyboard"):
                        logger.warning("signal push without keyboard · %s", s.signal_id)
                    for chat in targets:
                        await queue.enqueue(TelegramMessage(
                            chat_id=chat,
                            text=plain,
                            priority="critical" if s.tier == "A" else "normal",
                            reply_markup=kb,
                        ))
                n_sent = await queue.drain_once(bot, dry_run=dry_run)
                total_sent += n_sent
                # Heartbeat selesai: hanya kalau siklus tuntas tanpa exception
                memory.set_runtime("last_cycle_end_ts", now_utc().isoformat())
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
    global _LAST_MIDS_AT
    active = memory.get_active_signals()
    if not active:
        return []

    mids: dict[str, float] = {}
    try:
        mids = await _engine.hl_all_mids(memory)
        if mids:
            _LAST_MIDS.clear()
            _LAST_MIDS.update(mids)
            _LAST_MIDS_AT = time.monotonic()
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
                candles = await _engine.hl_candle_snapshot(sym, interval, start_ms, end_ms, memory)
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
        armed_at_row = _parse_ts(row.get("armed_at"))

        since = _parse_ts(row.get("last_checked_at")) or _parse_ts(row.get("created_at"))
        # ARMED: jendela SL/TP tidak boleh menyertakan price action PRE-ARM
        if state == "ARMED" and armed_at_row is not None:
            if since is None or since < armed_at_row:
                since = armed_at_row
        hi, lo, last_c = await _window_hl(sym, since)
        mid = mids.get(sym)
        if hi is None or lo is None:
            if mid is not None and mid > 0:
                hi = lo = last_c = float(mid)
            else:
                # JANGAN touch last_checked_at — preserve window sampai ada harga
                # (bug: touch saat no-data → jendela SL di mana harga lewat ke-skip)
                if exp is not None and now >= exp:
                    # PENDING → NEUTRAL; ARMED+TP1 → PARTIAL; ARMED → NEUTRAL
                    if state == "ARMED" and already_tp1:
                        outcome = "PARTIAL"
                    else:
                        outcome = "NEUTRAL"
                    if not memory.resolve_signal(
                        sid, outcome, tp1_hit=True if already_tp1 else None,
                    ):
                        continue  # race: sudah diselesaikan proses lain
                    events.append({
                        "signal_id": sid, "symbol": sym, "direction": direction,
                        "outcome": outcome, "reason": "expired", "px": None,
                    })
                    logger.info(
                        "⏱ CLOSE %s %s · %s · expired no-price · state=%s was_tp1=%s",
                        sym, direction, outcome, state, already_tp1,
                    )
                    continue
                _now_m = time.monotonic()
                _last_w = _NO_PRICE_WARNED.get(sym)
                if _last_w is None or _now_m - _last_w >= _NO_PRICE_WARN_TTL:
                    _NO_PRICE_WARNED[sym] = _now_m
                    logger.warning(
                        "monitor · %s no price data · window preserved since=%s",
                        sym, since.isoformat() if since else "—",
                    )
                continue
        # Gabungkan mid ke window: forming-bar / gap tidak boleh lolos SL
        if mid is not None and mid > 0:
            mid_f = float(mid)
            hi = max(float(hi), mid_f)
            lo = min(float(lo), mid_f)
            if last_c is None:
                last_c = mid_f

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
                # Reset window ke momen arm — PRE-ARM wick tidak boleh jadi SL/TP.
                # now_utc() bukan `now` (stale dari top of function).
                since = now_utc()
                hi2, lo2, last_c2 = await _window_hl(sym, since)
                if hi2 is not None and lo2 is not None:
                    hi, lo, last_c = hi2, lo2, last_c2
                elif mid is not None and mid > 0:
                    hi = lo = last_c = float(mid)
                if mid is not None and mid > 0:
                    mid_f = float(mid)
                    hi = max(float(hi or mid_f), mid_f)
                    lo = min(float(lo or mid_f), mid_f)
                    if last_c is None:
                        last_c = mid_f
                logger.info(
                    "🔒 ARMED %s · entry zone reached · SL/TP from arm (no pre-arm wick)",
                    sym,
                )
                # fall through — evaluasi SL/TP post-arm saja
            else:
                created = _parse_ts(row.get("created_at"))
                horizon_p = str(row.get("horizon") or "INTRADAY").upper()
                arm_timeout_h = PENDING_ARM_TIMEOUT_HOURS.get(horizon_p, 12)
                px_now = float(mid or last_c or 0) if (mid or last_c) else None

                # 1) Missed entry: harga sudah lewat TP1 searah trade tanpa arm
                #    SHORT: mid <= tp1 → move sudah jalan tanpa fill zone
                #    LONG:  mid >= tp1
                # Missed: harga lewat TP1 DAN sudah melewati sisi entry zone
                # (hindari false positive saat TP1 dekat entry_lo)
                missed = False
                if px_now and tp1 > 0 and entry_lo > 0 and entry_hi >= entry_lo:
                    zone_w = entry_hi - entry_lo
                    clear = max(zone_w * 0.25, abs(px_now) * 0.001)
                    if direction == "SHORT":
                        # harus di bawah entry_lo (sudah lewat zone jual)
                        if px_now <= float(tp1) and px_now < (entry_lo - clear):
                            missed = True
                    elif direction == "LONG":
                        if px_now >= float(tp1) and px_now > (entry_hi + clear):
                            missed = True

                # 2) Arm timeout: zone tidak tersentuh dalam PENDING_ARM_TIMEOUT
                arm_expired = (
                    created is not None
                    and (now - created).total_seconds() / 3600.0 >= arm_timeout_h
                )

                # 3) valid_until hard expiry
                hard_exp = exp is not None and now >= exp

                if missed or arm_expired or hard_exp:
                    reason = (
                        "missed_entry" if missed
                        else ("arm_timeout" if arm_expired else "expired")
                    )
                    if not memory.resolve_signal(sid, "NEUTRAL", reason=reason):
                        continue  # race: sudah diselesaikan proses lain
                    events.append({
                        "signal_id": sid, "symbol": sym, "direction": direction,
                        "outcome": "NEUTRAL", "reason": reason, "px": px_now,
                    })
                    logger.info(
                        "⏱ CLOSE %s %s · NEUTRAL · %s · PENDING · px=%s",
                        sym, direction, reason,
                        f"{px_now:.6g}" if px_now else "—",
                    )
                    continue
                memory.touch_signal_check(sid)
                continue

        # Precompute for TIME_STOP (dievaluasi SETELAH SL/TP — jangan bunuh
        # trade yang sudah TP1 / yang SL-nya di BE sebelum cek market).
        armed_at = _parse_ts(row.get("armed_at"))
        horizon = str(row.get("horizon") or "").upper()
        time_stop_h = TIME_STOP_HOURS.get(horizon)
        current_px = last_c or mid
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
        elif entry_mid:
            # fallback: risk ≈ 1.5×ATR. Pakai original_stop (bukan stop BE)
            # supaya toleransi tidak kolaps ke 0 setelah TP1→breakeven.
            risk_ref = original_stop if original_stop else stop
            if risk_ref:
                atr_est = abs(entry_mid - float(risk_ref)) / 1.5
        # Floor: cold-start / alt baru → toleransi tidak boleh 0 (fail-harsh)
        if entry_mid and entry_mid > 0:
            atr_est = max(float(atr_est), float(entry_mid) * 0.001)
        else:
            atr_est = max(float(atr_est), 1e-8)
        tol_tp = atr_est * HIT_TOLERANCE_ATR_DEFAULT  # 0.05×ATR ke arah TP
        tol_sl = atr_est * 0.02  # ketat: harus benar-benar sentuh/lewati SL

        # Priority: SL > TP2 > TP1 (intrabar unknown → conservative SL first)
        # BE equality (stop == entry_mid setelah TP1) HARUS valid — kalau tidak,
        # PARTIAL di breakeven tidak pernah resolve.
        if direction == "LONG":
            # SL di/di bawah entry (BE equality OK); SL di atas entry = data invalid
            sl_valid = stop > 0 and entry_mid > 0 and stop <= entry_mid + 1e-12
            sl_hit = sl_valid and (
                lo <= (stop + tol_sl)
                or (mid is not None and float(mid) <= (stop + tol_sl))
            )
            tp2_hit = tp2 > 0 and hi >= (tp2 - tol_tp)
            tp1_hit_now = tp1 > 0 and hi >= (tp1 - tol_tp)
            ref_px = lo if sl_hit else (hi if (tp2_hit or tp1_hit_now) else (last_c or mid))
        else:
            # SHORT: SL di/di atas entry
            sl_valid = stop > 0 and entry_mid > 0 and stop >= entry_mid - 1e-12
            sl_hit = sl_valid and (
                hi >= (stop - tol_sl)
                or (mid is not None and float(mid) >= (stop - tol_sl))
            )
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
            if not memory.resolve_signal(
                sid, outcome, realized_rr=rr, tp1_hit=True if already_tp1 else None,
            ):
                continue  # race: sudah diselesaikan proses lain
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
        # TP2 melewati TP1 secara logika → selalu weighted 0.5/0.5 (bukan full R)
        if tp2_hit:
            exit_px = float(tp2) if tp2 > 0 else float(ref_px or 0)
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                exit_px, tp1=tp1, tp1_hit=True,
            )
            if not memory.resolve_signal(sid, "PROFIT", realized_rr=rr, tp1_hit=True):
                continue  # race: sudah diselesaikan proses lain
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

        # 5) TIME_STOP — setelah SL/TP; kalau sudah TP1 → PARTIAL (bukan NEUTRAL)
        # Regime-aware: di crowded/range (skew ekstrem) perpanjang INTRADAY 6→9h
        # dan skip time-stop kalau unrealized R sudah > +0.3.
        effective_ts_h = time_stop_h
        if horizon == "INTRADAY" and memory is not None:
            try:
                rows = memory._conn.execute(
                    "SELECT rate FROM metric_funding "
                    "WHERE recorded_at >= datetime('now', '-6 hours') "
                    "ORDER BY recorded_at DESC LIMIT 200"
                ).fetchall()
                total = len(rows)
                pos = sum(1 for r in rows if float(r["rate"] or 0) > 0)
                skew = (pos / total * 100.0) if total else None
                if skew is not None and (skew >= 75 or skew <= 25):
                    effective_ts_h = 9.0
            except Exception:
                pass
        unrealized_r = None
        if current_px is not None and entry_mid and original_stop:
            risk = abs(entry_mid - float(original_stop))
            if risk > 0:
                if direction == "LONG":
                    unrealized_r = (float(current_px) - entry_mid) / risk
                else:
                    unrealized_r = (entry_mid - float(current_px)) / risk
        if (
            state == "ARMED"
            and armed_at is not None
            and effective_ts_h is not None
            and (now - armed_at).total_seconds() / 3600.0 >= effective_ts_h
            and current_px is not None
            and (unrealized_r is None or unrealized_r < 0.3)
        ):
            if already_tp1:
                # Sudah bank TP1; stall di zone = scratch/BE → PARTIAL
                exit_px = float(current_px)
                rr = _realized_rr(
                    direction, entry_lo, entry_hi, original_stop,
                    exit_px, tp1=tp1, tp1_hit=True,
                )
                if not memory.resolve_signal(
                    sid, "PARTIAL", realized_rr=rr, tp1_hit=True,
                    reason="TIME_STOP",
                ):
                    continue  # race: sudah diselesaikan proses lain
                events.append({
                    "signal_id": sid, "symbol": sym, "direction": direction,
                    "outcome": "PARTIAL", "reason": "TIME_STOP",
                    "px": exit_px, "realized_rr": rr,
                })
                logger.info(
                    "⏰ TIME_STOP %s %s · PARTIAL (was TP1) · %.6g · R:R %.2f",
                    sym, direction, exit_px, rr,
                )
            else:
                # Forced close — catat floating R, bukan buang expectancy
                exit_px = float(current_px)
                rr = _realized_rr(
                    direction, entry_lo, entry_hi, original_stop,
                    exit_px, tp1=tp1, tp1_hit=False,
                )
                if rr is not None and rr >= 0.25:
                    outcome = "PARTIAL"
                elif rr is not None and rr <= -0.25:
                    outcome = "LOSS"
                else:
                    outcome = "NEUTRAL"
                if not memory.resolve_signal(
                    sid, outcome, realized_rr=rr, reason="TIME_STOP",
                ):
                    continue  # race: sudah diselesaikan proses lain
                events.append({
                    "signal_id": sid, "symbol": sym, "direction": direction,
                    "outcome": outcome, "reason": "TIME_STOP",
                    "px": exit_px, "realized_rr": rr,
                })
                logger.info(
                    "⏰ TIME_STOP %s %s · %s · %.6g · R:R %s",
                    sym, direction, outcome, exit_px,
                    f"{rr:.2f}" if rr is not None else "—",
                )
            continue

        # 6) Expiry ARMED — hanya jika SL/TP/TIME_STOP tidak resolve
        if state == "ARMED" and exp is not None and now >= exp:
            outcome = "PARTIAL" if already_tp1 else "NEUTRAL"
            # NEUTRAL = trade tidak terbukti → RR wajib None (bukan residual +R)
            rr = None
            if (
                outcome == "PARTIAL"
                and current_px is not None
                and current_px > 0
            ):
                rr = _realized_rr(
                    direction, entry_lo, entry_hi, original_stop,
                    float(current_px), tp1=tp1, tp1_hit=True,
                )
            if not memory.resolve_signal(
                sid, outcome, realized_rr=rr, tp1_hit=already_tp1 or None,
            ):
                continue  # race: sudah diselesaikan proses lain
            events.append({
                "signal_id": sid, "symbol": sym, "direction": direction,
                "outcome": outcome, "reason": "expired", "px": current_px,
                "realized_rr": rr,
            })
            logger.info(
                "⏱ CLOSE %s %s · %s · expired after market check · was_tp1=%s · R:R %s",
                sym, direction, outcome, already_tp1,
                f"{rr:.2f}" if rr is not None else "—",
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
        def _n(sql: str, params: tuple = ()) -> int:
            row = memory._conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

        n_sig = _n("SELECT COUNT(*) FROM signal_history")
        n_res = _n(
            "SELECT COUNT(*) FROM signal_history "
            "WHERE outcome IS NOT NULL AND is_simulated = 0 AND COALESCE(is_shadow,0)=0"
        )
        n_act = memory.count_active_signals()
        n_fund = _n("SELECT COUNT(*) FROM metric_funding")
        n_atr = _n("SELECT COUNT(*) FROM metric_atr")
        n_oi = _n("SELECT COUNT(*) FROM metric_oi")
        n_oi_fresh = 0
        try:
            # ~20 menit terakhir (wall clock) — parameterized, no f-string SQL
            cutoff = (now_utc() - timedelta(minutes=20)).isoformat()
            n_oi_fresh = _n(
                "SELECT COUNT(*) FROM metric_oi WHERE recorded_at >= ?",
                (cutoff,),
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



async def cleanup_duplicate_active_signals(memory: MemoryEngine) -> int:
    """Cancel active signals duplikat per symbol (keep newest)."""
    if not memory._conn:
        return 0
    try:
        # is_shadow optional — schema lama tanpa kolom tetap OK via try
        rows = memory._conn.execute(
            "SELECT symbol, signal_id, created_at FROM signal_history "
            "WHERE outcome IS NULL "
            "AND signal_id IN (SELECT signal_id FROM active_signals) "
            "ORDER BY symbol, created_at DESC, signal_id DESC"
        ).fetchall()
    except Exception as e:
        logger.debug("dup cleanup query fail-soft: %s", type(e).__name__)
        return 0
    seen: dict[str, str] = {}
    n = 0
    for r in rows:
        sym = str(r["symbol"])
        sid = str(r["signal_id"])
        if sym in seen:
            try:
                # reason= optional depending on signature
                # Duplikat bukan trade: jangan naikkan total_signals/neutrals
                memory.resolve_signal(
                    sid, "NEUTRAL", reason="dup_cleanup", update_performance=False,
                )
                n += 1
            except Exception as e:
                logger.debug("dup resolve fail-soft: %s", type(e).__name__)
        else:
            seen[sym] = sid
    if n:
        logger.warning("🧹 dup active signals cleaned · %d", n)
    return n


async def startup_recovery(memory: MemoryEngine) -> None:
    """
    §XIII.10 / §XVI.9 — recovery setelah restart.
    0) Reconcile active_signals ↔ signal_history (orphan / missing)
    1) Jalankan lifecycle penuh (candle → TP/SL/PARTIAL)
    2) Expire sisa yang lewat valid_until → NEUTRAL
    Jangan langsung NEUTRAL tanpa cek market (rusak trust_score).
    """
    now = now_utc()
    try:
        recon = memory.reconcile_signal_state()
    except Exception as e:
        logger.warning("startup_recovery reconcile fail-soft: %s", type(e).__name__)
        recon = {"orphan_active": 0, "reattached": 0}
    try:
        await cleanup_duplicate_active_signals(memory)
    except Exception as e:
        logger.warning("startup_recovery dup cleanup fail-soft: %s", type(e).__name__)

    # Shadow dulu — sebelum orphan sweep (shadow tidak di active_signals)
    n_shadow = 0
    try:
        n_shadow = await backfill_shadow_outcomes(memory, limit=60)
    except Exception as e:
        logger.warning("startup_recovery shadow backfill fail-soft: %s", type(e).__name__)
    try:
        _sw = shadow_sanity_check(memory, days=7)
        for w in _sw:
            logger.warning("👻 shadow sanity · %s", w)
        try:
            memory.set_runtime("shadow_warns_n", str(len(_sw)))
        except Exception:
            pass
    except Exception as e:
        logger.debug("shadow sanity skip: %s", type(e).__name__)

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
    # Orphan history: outcome NULL, valid_until lewat, tidak di active_signals
    # (bug/migrasi) → resolve supaya tidak menggantung selamanya
    orphan_hist = 0
    if memory._conn:
        try:
            for r in memory._conn.execute(
                "SELECT signal_id, state, tp1_hit FROM signal_history "
                "WHERE outcome IS NULL AND valid_until <= ? "
                "AND COALESCE(is_shadow, 0) = 0 "
                "AND signal_id NOT IN (SELECT signal_id FROM active_signals)",
                (now.isoformat(),),
            ).fetchall():
                already_tp1 = bool(r["tp1_hit"])
                st = str(r["state"] or "PENDING").upper()
                outcome = "PARTIAL" if (st == "ARMED" and already_tp1) else "NEUTRAL"
                memory.resolve_signal(
                    r["signal_id"], outcome,
                    tp1_hit=True if already_tp1 else None,
                )
                orphan_hist += 1
            closed_exp += orphan_hist
        except Exception as e:
            logger.debug("orphan history resolve: %s", type(e).__name__)
    recent = 0
    if memory._conn:
        cutoff = (now - timedelta(minutes=SIGNAL_COOLDOWN_MIN)).isoformat()
        row = memory._conn.execute(
            "SELECT COUNT(*) AS n FROM signal_history WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        recent = int(row["n"]) if row else 0
    dl_open = 0
    try:
        dl_open = memory.count_write_dead_letters(open_only=True)
    except Exception:
        pass
    logger.info(
        "startup_recovery: before=%d · market_closed=%d · expired=%d · "
        "active_left=%d · recent_%dm=%d · orphan=%d · reattached=%d · shadow=%d · dl_open=%d",
        active_before, closed_market, closed_exp,
        memory.count_active_signals(), SIGNAL_COOLDOWN_MIN, recent,
        recon.get("orphan_active", 0), recon.get("reattached", 0), n_shadow, dl_open,
    )


def backup_db(db_path: str | None = None, *, suffix: str = ".bak") -> str | None:
    """
    Salin DB ke path+suffix (default .bak). Return path backup atau None.
    Dipakai sebelum rebuild / setelah integrity OK berkala.
    """
    import shutil
    db = db_path or os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    src = Path(db)
    if not src.is_file() or src.stat().st_size < 64:
        return None
    dest = Path(str(src) + suffix)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        logger.info("💾 backup DB · %s → %s (%.0fKB)", src, dest, dest.stat().st_size / 1024)
        return str(dest)
    except OSError as e:
        logger.warning("backup_db fail: %s", e)
        return None


def restore_from_previous_artifact() -> bool:
    """
    Pulihkan DB dari backup lokal (.bak / .prev) bila integrity gagal.
    Di GH Actions artifact sudah di-download workflow sebelum Python start —
    fungsi ini cover local / corrupt path saja.
    """
    import shutil
    db = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    for cand in (
        f"{db}.prev",
        f"{db}.periodic.bak",
        f"{db}.bak",
        "./data/cryptone_v45.db.bak",
    ):
        p = Path(cand)
        if not (p.is_file() and p.stat().st_size > 1024):
            continue
        try:
            with sqlite3.connect(str(p)) as chk:
                row = chk.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                logger.warning("♻️ restore skip %s · integrity=%s", p, row[0] if row else "?")
                continue
            Path(db).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, db)
            logger.info("♻️ restore DB from %s → %s", p, db)
            return True
        except (OSError, sqlite3.Error) as e:
            logger.warning("restore failed %s: %s", p, e)
    logger.warning("♻️ restore · no usable backup found")
    return False


def create_fresh_db() -> str:
    """
    Schema kosong via MemoryEngine. Return path DB yang dipakai
    (bisa .fresh jika unlink gagal) — caller harus re-bind path.
    Backup dulu jika file lama masih ada (jangan hilangkan bukti korupsi).
    """
    db = os.environ.get("CRYPTONE_DB_PATH", "./data/cryptone_v45.db")
    p = Path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        backup_db(str(p), suffix=".corrupt.bak")
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
    """Handler SIGTERM (cancel GH Actions): flush + pending Telegram + WAL lalu exit."""
    logger.info("🧹 graceful stop · flush DB sebelum runner mati")
    # Persist sisa Telegram queue agar tidak hilang antar-run
    if queue is not None and hasattr(queue, "flush_to_pending"):
        try:
            n = await asyncio.wait_for(queue.flush_to_pending(), timeout=5)
            if n:
                logger.info("📬 shutdown · %d messages → pending_delivery", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("shutdown pending flush: %s", type(e).__name__)
    try:
        await asyncio.wait_for(memory.flush_writes(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("flush timeout %ss", timeout)
    try:
        memory.checkpoint_wal()
    except Exception:
        pass
    try:
        backup_db(memory.db_path, suffix=".prev")
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
    try:
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


async def startup_db_check(db_path: str) -> bool:
    """
    PRAGMA integrity_check.
    False → corrupt (restore/rebuild) — penyebab error selalu dicatat.
    OperationalError (locked) → re-raise: jangan rebuild DB sehat yang sedang di-lock.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            ok = row is not None and row[0] == "ok"
            if not ok:
                logger.error("integrity_check bukan ok · %s", str(row)[:300])
            return ok
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # locked / busy — bukan corrupt
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("integrity_check exception · %s: %s", type(e).__name__, e)
        return False


# =============================================================================
# §15  CLI ENTRY
# =============================================================================

def parse_symbol_list(s: str) -> list[str]:
    if not s or not s.strip():
        return []
    return [x.strip().upper() for x in s.split(",") if x.strip()]



def _parse_report_dt(raw: str | None) -> datetime | None:
    """Parse --since/--until as UTC-aware datetime. None if empty."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise SystemExit(f"invalid datetime {raw!r}: {e}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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
    mode.add_argument("--report", action="store_true",
                    help="Strategy validation report (use --since/--until; not with --dry-run)")

    p.add_argument("--days", type=int, default=60)
    p.add_argument("--pin", type=parse_symbol_list, default=[])
    p.add_argument("--db", default=None)
    p.add_argument("--log-level", default=None)
    p.add_argument("--cycle-interval", type=int, default=None)
    p.add_argument("--max-runtime", type=int, default=None)
    p.add_argument("--section", type=int, default=None)
    p.add_argument("--unit", action="store_true")
    p.add_argument("--integration", action="store_true")
    p.add_argument("--since", default=None,
                   help="Report window start UTC YYYY-MM-DD[THH:MM:SS]")
    p.add_argument("--until", default=None,
                   help="Report window end UTC YYYY-MM-DD[THH:MM:SS]")
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
    for noisy in ("httpx", "httpcore", "google", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cfg = resolve_config(args)
    setup_logging(cfg["log_level"])

    logger.info(
        "Cryptone V%s | mode=%s | db=%s | build=%s",
        PROTOCOL_VERSION,
        next(
            (k for k in ("live", "bootstrap", "test", "dry_run", "check", "recover")
             if getattr(args, k.replace("-", "_"), False) or getattr(args, k, False)),
            "?",
        ),
        cfg["db"],
        globals().get("CRYPTONE_BUILD", "?"),
    )

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
        db_path = cfg["db"]
        if not Path(db_path).is_file():
            logger.error("--recover · DB not found: %s", db_path)
            return 5
        mem = MemoryEngine(db_path)
        mem.start()
        try:
            asyncio.run(startup_recovery(mem))
        finally:
            mem.close()
        return 0

    if args.report:
        db_path = cfg["db"]
        if not Path(db_path).is_file():
            logger.error("--report · DB not found: %s", db_path)
            return 5
        mem = MemoryEngine(db_path)
        mem.start()
        try:
            since = _parse_report_dt(getattr(args, "since", None))
            until = _parse_report_dt(getattr(args, "until", None))
            print(build_strategy_report(
                mem, live_only=True, since=since, until=until,
            ))
            try:
                print()
                print(format_shadow_gate_report(mem, days=30))
                print()
                print(format_gate_drift_report(mem, short_days=7, long_days=30))
                regret = symbol_regret_score(mem, days=30, top_n=5)
                if regret:
                    print()
                    print("══ Top regret (alpha terbuang, decay 15d) ══")
                    for sym, rg, n in regret:
                        print(f"  {sym:<12} +{rg:.2f}R weighted · n={n}")
            except Exception as e:
                logger.debug("shadow report skip: %s", type(e).__name__)
        finally:
            mem.close()
        return 0

    if args.live or args.dry_run:
        # Validasi env live
        if args.live:
            # Math first, LLM second — Gemini opsional (sector/macro enrichment).
            for key in ("CRYPTONE_TELEGRAM_BOT_TOKEN",
                        "CRYPTONE_OPERATOR_CHAT_ID", "CRYPTONE_USER_CHAT_ID"):
                if not os.environ.get(key):
                    logger.error("Missing required env for --live: %s", key)
                    return 3
            if not os.environ.get("GEMINI_API_KEY"):
                logger.warning(
                    "GEMINI_API_KEY missing · live lanjut tanpa LLM "
                    "(sector/macro enrichment soft-off)"
                )
        # CLI flags → env (run_live_service only reads env)
        if args.max_runtime is not None:
            os.environ["CRYPTONE_MAX_RUNTIME_SEC"] = str(args.max_runtime)
        if args.cycle_interval is not None:
            os.environ["CRYPTONE_CYCLE_INTERVAL_SEC"] = str(args.cycle_interval)
        if args.db:
            os.environ["CRYPTONE_DB_PATH"] = str(args.db)
        if args.pin:
            os.environ["CRYPTONE_PINNED_SYMBOLS"] = ",".join(args.pin)
        asyncio.run(run_live_service(dry_run=args.dry_run))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
