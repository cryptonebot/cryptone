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
import asyncio
import json
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
        "state, reasoning, sl_basis, entry_basis "
        "FROM signal_history WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if not row:
        return f"Signal {signal_id[:8]}… tidak ditemukan / sudah expired."
    arrow = "🟢" if row["direction"] == "LONG" else "🔴"
    state = row["state"] or "PENDING"
    # Prefer kolom dedicated; fallback regex reasoning untuk row legacy.
    sl_b = (row["sl_basis"] if "sl_basis" in row.keys() else None) or None
    en_b = (row["entry_basis"] if "entry_basis" in row.keys() else None) or None
    if not sl_b or not en_b:
        reason = row["reasoning"] or ""
        m_sl = re.search(r"(?:^|\s)sl=(\S+)", reason)
        m_en = re.search(r"(?:^|\s)entry=(\S+)", reason)
        sl_b = sl_b or (m_sl.group(1) if m_sl else "?")
        en_b = en_b or (m_en.group(1) if m_en else "?")
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
    """Pesan sinyal Telegram §XII.2 — kartu multi-baris + tombol (bukan compact 1-line)."""
    arrow = "🟢" if s.direction == "LONG" else "🔴"
    state = getattr(s, "state", "PENDING") or "PENDING"
    sl_b = getattr(s, "sl_basis", "atr") or "atr"
    en_b = getattr(s, "entry_basis", "atr") or "atr"
    build = globals().get("CRYPTONE_BUILD", "dev")
    plain = (
        f"{arrow} {s.symbol}  {s.direction}\n"
        f"{s.horizon} · Tier {s.tier} · {s.setup_type}\n"
        f"status {state} · SL {sl_b} · entry {en_b}\n"
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
    plain += f"\n\n↓ tombol: Reasoning · Chart · Journal\nbuild {build}"
    return plain


def format_main_menu_text() -> str:
    return "\n".join([
        _ui_header("🤖", "Cryptone V4.5"),
        "Market Radar + Trader Assistant",
        "",
        "📊 Radar     posisi aktif",
        "🌍 Market    snapshot + bias hari ini",
        "📈 Breadth   funding crowded",
        "📓 Journal   open + recent close",
        "🎯 Accuracy  expectancy & WR",
        "",
        "Sinyal real-time tetap push otomatis.",
        "Ketuk tombol di bawah.",
    ])


def _fmt_radar(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    sess = _session_status_line()
    if not rows:
        return "\n".join([
            _ui_header("📊", "Radar"),
            f"Session  {sess}",
            "",
            "Tidak ada sinyal aktif.",
            "Push otomatis saat setup lolos gate.",
        ])
    order = {"A": 0, "B": 1, "C": 2}
    state_order = {"ARMED": 0, "PENDING": 1}
    rows = sorted(rows, key=lambda r: (
        order.get(str(r.get("tier") or "C"), 9),
        state_order.get(str(r.get("state") or "").upper(), 9),
    ))
    n_arm = sum(1 for r in rows if str(r.get("state") or "").upper() == "ARMED")
    n_pend = sum(1 for r in rows if str(r.get("state") or "").upper() == "PENDING")
    lines = [
        _ui_header("📊", f"Radar — {len(rows)} aktif"),
        f"Session  {sess}",
        f"ARMED {n_arm}  ·  PENDING {n_pend}",
        "",
    ]
    for r in rows[:12]:
        _dir = str(r.get("direction") or "").upper()
        arrow = "🟢" if _dir == "LONG" else ("🔴" if _dir == "SHORT" else "⚪")
        st = str(r.get("state") or "PENDING")
        setup = r.get("setup_type") or "—"
        rr = float(r.get("rr") or 0)
        tier = r.get("tier") or "?"
        el = _ui_px(r.get("entry_zone_low"))
        eh = _ui_px(r.get("entry_zone_high"))
        conf = r.get("confidence")
        conf_s = f"  ·  conf {float(conf) * 100:.0f}%" if conf is not None else ""
        age_s = ""
        created = _parse_ts(r.get("created_at"))
        if created is not None:
            h = (now_utc() - created).total_seconds() / 3600.0
            age_s = f"  ·  {h:.1f}h" if h < 48 else f"  ·  {h / 24:.1f}d"
        sl = _ui_px(r.get("stop_loss"))
        hit_tp1 = bool(r.get("tp1_hit"))
        tp1_hit = "  ·  TP1✓" if hit_tp1 else ""
        sl_disp = f"{sl} (=BE)" if hit_tp1 else sl
        lines.append(
            f"{arrow} {r.get('symbol')} {_dir}\n"
            f"   Tier {tier}  ·  {st}{tp1_hit}  ·  {setup}\n"
            f"   Entry {el}–{eh}\n"
            f"   SL {sl_disp}  ·  R:R 1:{rr:.2f}{conf_s}{age_s}"
        )
        lines.append("")
    if len(rows) > 12:
        lines.append(f"… +{len(rows) - 12} lainnya")
    return "\n".join(lines).rstrip()


def _fng_label(v: float | None) -> str:
    if v is None:
        return "—"
    v = float(v)
    if v <= 25:
        return f"{v:.0f} Extreme Fear"
    if v <= 40:
        return f"{v:.0f} Fear"
    if v <= 55:
        return f"{v:.0f} Neutral"
    if v <= 75:
        return f"{v:.0f} Greed"
    return f"{v:.0f} Extreme Greed"


def _session_status_line(now: datetime | None = None) -> str:
    t = _as_wib(now or now_utc())
    h = t.hour
    in_sess = SESSION_START_HOUR_WIB <= h < SESSION_END_HOUR_WIB
    quiet = in_quiet_hours(t)
    if quiet:
        return f"Quiet · {h:02d}:00 WIB (B/C hold)"
    if in_sess:
        return f"Session open · {h:02d}:00 WIB"
    return f"Off-hours · {h:02d}:00 WIB (Tier A only)"


def _latest_funding_rows(memory: "MemoryEngine", hours: float = 6.0) -> list:
    if not memory._conn:
        return []
    try:
        cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
        return list(memory._conn.execute(
            """
            SELECT f.symbol, f.rate
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


def _rule_market_today(
    *,
    pos: int,
    neg: int,
    total: int,
    fng: float | None,
    bs: str,
    bias: str,
) -> str:
    """Ringkas hari ini tanpa LLM — cepat & deterministik."""
    if total <= 0:
        return "Data funding masih tipis — tunggu cycle berikutnya."
    pct_long = pos / total * 100.0
    notes: list[str] = []
    if pct_long >= 75:
        notes.append("Funding sangat crowded long → bias short / mean-revert.")
    elif pct_long >= 60:
        notes.append("Mayoritas long funding → pantau FUNDING_SQUEEZE short.")
    elif pct_long <= 25:
        notes.append("Funding crowded short → bias bounce / long squeeze.")
    elif pct_long <= 40:
        notes.append("Mayoritas short funding → pantau squeeze long.")
    else:
        notes.append("Funding relatif seimbang — prioritaskan struktur + confirm.")
    if fng is not None:
        if fng <= 25:
            notes.append("F&G Extreme Fear → jangan kejar short agresif.")
        elif fng >= 75:
            notes.append("F&G Extreme Greed → waspada chase long.")
    if bs == "1":
        notes.append("BLACK SWAN aktif — hanya setup kualitas tinggi.")
    elif bs == "ELEVATED":
        notes.append("Risk elevated — prefer Tier A.")
    if bias and bias not in ("n/a", "neutral", "—", ""):
        notes.append(f"RSS bias: {bias}.")
    return " ".join(notes[:3])


async def refresh_market_brief(memory: "MemoryEngine") -> None:
    """Gemini market brief → runtime (max 1×/jam). Fail-soft.

    Pakai gem.generate() (SDK+REST+rate limit), bukan _generate_interactions.
    F&G diambil fresh (cache TTL internal fear_greed_latest) agar restart
    tidak pakai last_fng usang dari runtime_settings.
    """
    try:
        last = float(memory.get_runtime("market_brief_ts") or "0")
    except (TypeError, ValueError):
        last = 0.0
    if time.time() - last < 3600:
        return
    gem = GeminiClient(memory)
    if not gem.available():
        return
    fng_fresh = await fear_greed_latest()
    if fng_fresh is None:
        return
    fng_raw = f"{float(fng_fresh):.0f}"
    try:
        memory.set_runtime("last_fng", fng_raw)
        try:
            memory.set_runtime("last_fng_ts", str(time.time()))
        except Exception:
            pass
    except Exception:
        pass
    rows = _latest_funding_rows(memory, 6.0)
    pos = sum(1 for r in rows if float(r["rate"]) > 0)
    total = max(len(rows), 1)
    bias = rss_news_bias() or "n/a"
    _lang_label = {"id": "Bahasa Indonesia", "en": "English"}.get(
        BRIEF_LANGUAGE, "Bahasa Indonesia",
    )
    prompt = (
        f"Crypto market brief in {_lang_label}, max {BRIEF_MAX_SENTENCES} "
        f"sentences, minimal emoji. "
        f"Funding long {pos}/{total} ({pos / total * 100:.0f}%). "
        f"Fear&Greed={fng_raw}. RSS={bias}. "
        f"Fokus bias arah singkat untuk trader intraday."
    )
    # Stamp dulu — cegah retry storm tiap cycle
    memory.set_runtime("market_brief_ts", str(time.time()))
    try:
        res = await gem.generate(
            "market_brief",
            prompt,
            temperature=0.3,
            max_tokens=120,
            use_cache=False,
        )
        text_out = (res.text or "").strip() if res and res.success else ""
        if text_out:
            memory.set_runtime("market_brief", text_out[:500])
            logger.info("🌍 market brief · refreshed")
        else:
            err = getattr(res, "error", None) or "no text"
            # Capacity → sudah di-stamp 1h; log soft
            logger.info("🌍 market brief · defer · %s", err)
    except Exception as e:
        logger.info("🌍 market brief · skip · %s", type(e).__name__)


def _fmt_market(memory: "MemoryEngine") -> str:
    """Snapshot pasar + ringkasan hari ini (rule-based; Gemini brief opsional cache)."""
    anchor = memory.get_last_anchor() or "—"
    bs = memory.get_runtime("black_swan_mode", "0") or "0"
    bs_label = {
        "1": "🚨 BLACK SWAN",
        "ELEVATED": "⚠️ ELEVATED",
    }.get(bs, "✅ NORMAL")
    n_act = memory.count_active_signals()
    fund_n = atr_n = 0
    if memory._conn:
        try:
            cutoff_24h = (now_utc() - timedelta(hours=24)).isoformat()
            fund_n = int(memory._conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM metric_funding "
                "WHERE recorded_at >= ?",
                (cutoff_24h,),
            ).fetchone()["n"])
            atr_n = int(memory._conn.execute(
                "SELECT COUNT(DISTINCT symbol) AS n FROM metric_atr "
                "WHERE recorded_at >= ?",
                (cutoff_24h,),
            ).fetchone()["n"])
        except Exception:
            pass

    fng_v: float | None = None
    try:
        raw_fng = (memory.get_runtime("last_fng") or "").strip()
        if raw_fng:
            try:
                ts_fng = float(memory.get_runtime("last_fng_ts") or "0")
            except (TypeError, ValueError):
                ts_fng = 0.0
            # >6h = restart panjang / data basi — jangan tampilkan sebagai "sekarang"
            if ts_fng > 0 and (time.time() - ts_fng) < FNG_STALE_SEC:
                fng_v = float(raw_fng)
    except (TypeError, ValueError):
        fng_v = None
    if fng_v is None:
        try:
            raw = _FNG_CACHE.get("value")
            fng_v = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            fng_v = None

    rows = _latest_funding_rows(memory, 6.0)
    pos = sum(1 for r in rows if float(r["rate"]) > 0)
    neg = sum(1 for r in rows if float(r["rate"]) < 0)
    total = len(rows)
    tilt = "—"
    if total > 0:
        tilt = f"+{pos / total * 100:.0f}% long / −{neg / total * 100:.0f}% short"

    bias = rss_news_bias() or "n/a"
    sess = _session_status_line()
    macro_line = "—"
    try:
        mins, ev_name, ev_impact = minutes_to_next_macro(memory)
        if mins is not None and ev_name:
            past = mins < 0
            am = abs(mins)
            if am < 60:
                when = f"{am:.0f}m"
            elif am < 24 * 60:
                when = f"{am / 60:.1f}h"
            else:
                when = f"{am / (24 * 60):.1f}d"
            if past:
                when = f"{when} lalu"
            macro_line = f"{ev_name} · {when} · {ev_impact or '?'}"
    except Exception:
        pass

    today = _rule_market_today(
        pos=pos, neg=neg, total=total, fng=fng_v, bs=str(bs), bias=str(bias),
    )
    gem_brief = (memory.get_runtime("market_brief") or "").strip()

    lines = [
        _ui_header("🌍", "Market Snapshot"),
        f"Session     {sess}",
        f"Anchor      {anchor}",
        f"Black swan  {bs_label}",
        f"Active      {n_act}",
        f"F&G         {_fng_label(fng_v)}",
        f"Funding     {tilt}  ({total} sym · 6h)",
        f"Coverage    fund {fund_n} · atr {atr_n}  (24h)",
        f"Next macro  {macro_line}",
        f"RSS bias    {bias}",
        "",
        "Hari ini",
        _ui_sep(),
        today,
    ]
    if gem_brief:
        lines.extend(["", "AI brief", _ui_sep(), gem_brief[:500]])
    return "\n".join(lines)


def _fmt_blackswan(memory: "MemoryEngine") -> str:
    mode = memory.get_runtime("black_swan_mode", "0") or "0"
    started = memory.get_runtime("black_swan_started_at") or "—"
    if mode == "1":
        label = "🚨 ACTIVE — BLACK SWAN"
        tip = "Hanya setup kualitas tinggi. Sinyal aktif +6h extend."
    elif mode == "ELEVATED":
        label = "⚠️ ELEVATED (Tier A only)"
        tip = "Prefer Tier A. Kurangi size / frekuensi."
    else:
        label = "✅ NORMAL"
        tip = "Kondisi risk normal — pipeline penuh."
    since = "—"
    if started and started != "—":
        dt = _parse_ts(started)
        if dt is not None:
            since = dt.astimezone(WIB).strftime("%d %b %H:%M WIB")
        else:
            since = str(started)[:19]
    lines = [
        _ui_header("🚨", "Black Swan"),
        f"Mode     {label}",
        f"Since    {since}",
        "",
        tip,
    ]
    if memory._conn:
        try:
            row = memory._conn.execute(
                "SELECT triggered_indicators, trigger_count, started_at "
                "FROM blackswan_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                lines.extend([
                    "",
                    "Last event",
                    _ui_sep(),
                    f"Indicators  {row['triggered_indicators'] or '—'}",
                    f"Count       {row['trigger_count']}",
                    f"At          {str(row['started_at'] or '—')[:19]}",
                ])
        except Exception:
            pass
    return "\n".join(lines)


def _fmt_journal(memory: "MemoryEngine") -> str:
    rows = memory.get_active_signals()
    lines = [
        _ui_header("📓", "Journal"),
        f"Active  {len(rows)}",
        "",
    ]
    if not rows:
        lines.append("Tidak ada posisi aktif.")
    else:
        lines.append("Open")
        lines.append(_ui_sep())
        for r in rows[:10]:
            _dir = str(r.get("direction") or "").upper()
            arrow = "🟢" if _dir == "LONG" else ("🔴" if _dir == "SHORT" else "⚪")
            el = _ui_px(r.get("entry_zone_low"))
            eh = _ui_px(r.get("entry_zone_high"))
            sl = _ui_px(r.get("stop_loss"))
            tp1 = _ui_px(r.get("tp1"))
            tp2 = _ui_px(r.get("tp2"))
            st = str(r.get("state") or "PENDING")
            hit_tp1 = bool(r.get("tp1_hit"))
            tp1_note = "  ·  TP1✓" if hit_tp1 else ""
            sl_disp = f"{sl} (=BE)" if hit_tp1 else sl
            lines.append(
                f"{arrow} {r.get('symbol')} {_dir}  ·  {st}{tp1_note}\n"
                f"   Entry {el}–{eh}\n"
                f"   SL {sl_disp}  ·  TP1 {tp1}  ·  TP2 {tp2}"
            )
            lines.append("")
    if memory._conn:
        try:
            recent = memory._conn.execute(
                "SELECT symbol, direction, outcome, realized_rr, resolved_at, "
                "setup_type "
                "FROM signal_history WHERE outcome IS NOT NULL "
                "AND COALESCE(is_shadow, 0) = 0 AND COALESCE(is_simulated, 0) = 0 "
                f"ORDER BY resolved_at DESC LIMIT {RECENT_CLOSED_LIMIT}"
            ).fetchall()
            if recent:
                lines.append("Recent closed")
                lines.append(_ui_sep())
                for r in recent:
                    rr = r["realized_rr"]
                    rr_s = f"  R:R {rr:+.2f}" if rr is not None else ""
                    oc = str(r["outcome"] or "")
                    icon = {
                        "PROFIT": "✅", "LOSS": "🛑",
                        "PARTIAL": "🎯", "NEUTRAL": "⏱",
                    }.get(oc, "•")
                    age_s = ""
                    dt = _parse_ts(r["resolved_at"])
                    if dt is not None:
                        delta_h = (now_utc() - dt).total_seconds() / 3600.0
                        age_s = (
                            f"  ·  {delta_h:.1f}h"
                            if delta_h < 24
                            else f"  ·  {delta_h / 24:.1f}d"
                        )
                    setup = r["setup_type"] or ""
                    setup_s = f"  ·  {setup}" if setup else ""
                    lines.append(
                        f"{icon} {oc}  {r['symbol']} {r['direction']}"
                        f"{rr_s}{age_s}{setup_s}"
                    )
        except Exception:
            pass
    return "\n".join(lines).rstrip()


def _fmt_performance(memory: "MemoryEngine") -> str:
    lines = [_ui_header("📊", "Performance")]
    rows = []
    if memory._conn:
        try:
            rows = list(memory._conn.execute(
                "SELECT setup_type, total_signals, wins, partials, losses, avg_rr "
                "FROM setup_performance "
                "WHERE total_signals > 0 "
                "ORDER BY total_signals DESC LIMIT 8"
            ).fetchall())
        except Exception:
            rows = []
    if not rows:
        lines.append("Belum ada sample setup.")
        lines.append("Akan terisi setelah sinyal resolved.")
        return "\n".join(lines)
    lines.append("Per setup (rolling)")
    lines.append(_ui_sep())
    for sp in rows:
        name = sp["setup_type"] or "?"
        n = int(sp["total_signals"] or 0)
        w = int(sp["wins"] or 0)
        p = int(sp["partials"] or 0)
        l = int(sp["losses"] or 0)
        if n <= 0:
            continue
        wr = (w + PARTIAL_WEIGHT * p) / n * 100.0
        try:
            avg = sp["avg_rr"]
        except (KeyError, IndexError):
            avg = None
        avg_s = f"{float(avg):+.2f}R" if avg is not None else "—"
        lines.append(
            f"{name}\n"
            f"   n={n}  W{w}/P{p}/L{l}  ·  WR {wr:.0f}%  ·  avg {avg_s}"
        )
    try:
        streak = memory.consecutive_losses(5)
        lines.extend(["", f"Loss streak  {streak:.1f}  (PARTIAL=0.25)"])
    except Exception:
        pass
    return "\n".join(lines)




def _kb_shadow_gates(memory: "MemoryEngine") -> dict:
    """Tombol drill-down per gate (max 8) + back."""
    buttons: list[list[dict]] = []
    if memory._conn:
        try:
            cutoff = (now_utc() - timedelta(days=30)).isoformat()
            rows = memory._conn.execute(
                "SELECT reject_reason, COUNT(*) AS n FROM signal_history "
                "WHERE COALESCE(is_shadow,0)=1 AND outcome IS NOT NULL "
                "AND created_at >= ? "
                "GROUP BY reject_reason ORDER BY n DESC LIMIT 8",
                (cutoff,),
            ).fetchall()
            for r in rows:
                gate = str(r["reject_reason"] or "?")
                label = f"{gate[:28]} ({int(r['n'])})"
                key = gate[:SHADOW_GATE_KEY_MAX]
                buttons.append([{
                    "text": label,
                    "callback_data": f"shadow:gate:{key}",
                }])
        except Exception:
            pass
    buttons.append([{"text": "⬅️ Menu", "callback_data": "menu:home"}])
    return {"inline_keyboard": buttons}


def _fmt_shadow_gate_detail(memory: "MemoryEngine", gate: str) -> str:
    """5–8 shadow terakhir untuk satu gate — konkret, bukan agregat."""
    lines = [_ui_header("🧬", f"Shadow · {gate[:40]}")]
    if not memory._conn:
        lines.append("No DB")
        return "\n".join(lines)
    try:
        rows = memory._conn.execute(
            "SELECT symbol, direction, outcome, realized_rr, resolved_at "
            "FROM signal_history "
            "WHERE COALESCE(is_shadow,0)=1 "
            "AND (reject_reason = ? OR substr(reject_reason, 1, ?) = ?) "
            "AND outcome IS NOT NULL "
            f"ORDER BY resolved_at DESC LIMIT {RECENT_CLOSED_LIMIT}",
            (gate, len(gate), gate),
        ).fetchall()
    except Exception as e:
        lines.append(f"fail-soft · {type(e).__name__}")
        return "\n".join(lines)
    if not rows:
        lines.append("Belum ada shadow resolved untuk gate ini.")
        return "\n".join(lines)
    for r in rows:
        oc = str(r["outcome"] or "")
        icon = {"PROFIT": "✅", "LOSS": "🛑", "PARTIAL": "🎯", "NEUTRAL": "⏱"}.get(oc, "•")
        rr = r["realized_rr"]
        rr_s = f" {float(rr):+.2f}R" if rr is not None else ""
        lines.append(
            f"{icon} {r['symbol']} {r['direction']} · {oc}{rr_s}"
        )
    lines.append("")
    lines.append("Counterfactual — bukan sinyal real.")
    return "\n".join(lines)


def _fmt_shadow_report(memory: "MemoryEngine") -> str:
    """Telegram view: gate ROI + top regret (cap ~3500 char)."""
    try:
        body = format_shadow_gate_report(memory, days=30)
    except Exception as e:
        body = f"Shadow report fail-soft · {type(e).__name__}"
    try:
        drift = format_gate_drift_report(memory, short_days=7, long_days=30)
    except Exception:
        drift = ""
    try:
        regret = symbol_regret_score(memory, days=30, top_n=5)
    except Exception:
        regret = []
    lines = [body]
    if drift and "0 proven" not in drift and "no DB" not in drift:
        lines.append("")
        lines.append(drift)
    if regret:
        lines.append("")
        lines.append("Top regret (decay 15d)")
        lines.append("───────────────────────────")
        for sym, total, n in regret:
            lines.append(f"  {sym:<12} +{total:.2f}R weighted · n={n}")
    lines.append("")
    lines.append("Shadow = counterfactual gate reject.")
    lines.append("Bukan sinyal real — tidak push Telegram.")
    text_out = "\n".join(lines)
    if len(text_out) > 3500:
        text_out = text_out[:3490] + "\n…"
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
    lines.extend([
        "Window      30 hari",
        f"Proven      {m.n_trades}    NEUTRAL {m.n_neutral}",
        f"PROFIT {m.wins}  ·  LOSS {m.losses}  ·  PARTIAL {m.partials}",
        "",
        f"Win rate    {wr_pct:.1f}%",
        f"            [{bar}]",
        f"Expectancy  {m.expectancy_r:+.3f}R",
        f"Exec (fee)  {m.expectancy_exec_r:+.3f}R",
        f"PF          {pf_s}",
        f"MaxDD       {m.max_drawdown_r:.2f}R",
        "",
        "Catatan",
        _ui_sep(),
        f"WR = (W + {PARTIAL_WEIGHT}×P) / proven",
        "NEUTRAL tidak masuk expectancy.",
    ])
    if m.n_trades < 10:
        lines.append("Sample masih kecil — jangan overfit.")
    elif m.expectancy_r > 0.2:
        lines.append("Expectancy positif — edge terlihat.")
    elif m.expectancy_r < 0:
        lines.append("Expectancy negatif — audit setup / gate.")
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
    mode = get_delivery_mode(memory)
    setups = memory.get_setup_enabled()
    sess = _session_status_line()
    post_loss = POST_LOSS_COOLDOWN_MIN
    entry_mult = ENTRY_ZONE_ATR_WIDTH
    lines = [
        _ui_header("⚙️", "Settings"),
        f"Delivery       {mode}",
        f"Session now    {sess}",
        f"Session WIB    {SESSION_START_HOUR_WIB:02d}:00–{SESSION_END_HOUR_WIB:02d}:00",
        f"Quiet WIB      {QUIET_START_HOUR_WIB:02d}:00–{QUIET_END_HOUR_WIB:02d}:00 "
        f"({'on' if QUIET_HOURS_ENABLED else 'off'})",
        f"Data cooldown  {SIGNAL_COOLDOWN_MIN}m",
        f"Post-loss box  {post_loss}m",
        f"Max concurrent {MAX_CONCURRENT_SIGNALS}",
        f"Max / cycle    {MAX_SIGNALS_PER_CYCLE}",
        f"Entry zone     {entry_mult}× ATR",
        "",
        "Setups",
        _ui_sep(),
        f"  FUNDING_SQUEEZE  {'ON' if setups.get('FUNDING_SQUEEZE', True) else 'OFF'}",
        f"  CASCADE_SCALP    {'ON' if setups.get('CASCADE_SCALP', True) else 'OFF'}",
        f"  COMPRESSION      {'ON' if setups.get('COMPRESSION', True) else 'OFF'}",
        "",
        "Tier B valid (confirm ≥ 2).",
        "Quiet menahan push B/C — tetap di DB/Radar.",
    ]
    paused = memory.get_runtime("signals_paused_until")
    if paused:
        lines.append("")
        lines.append(f"⏸ paused until {str(paused)[:19]}")
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
    parts = t.split()  # whitespace only — no comma replace
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
    """Breadth funding + top crowded + interpretasi."""
    lines = [_ui_header("📈", "Market Breadth")]
    rows = _latest_funding_rows(memory, 6.0)
    if not rows:
        lines.append("Belum ada sample funding (6h).")
        return "\n".join(lines)
    parsed: list[tuple[str, float]] = []
    for r in rows:
        try:
            parsed.append((str(r["symbol"]), float(r["rate"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not parsed:
        lines.append("Belum ada sample funding valid.")
        return "\n".join(lines)
    pos = sum(1 for _, rate in parsed if rate > 0)
    neg = sum(1 for _, rate in parsed if rate < 0)
    zero = len(parsed) - pos - neg
    total = len(parsed)
    pct_p = pos / total * 100.0
    pct_n = neg / total * 100.0
    lines.extend([
        f"Universe    {total} symbols (6h)",
        f"Long fund   {pos}  ({pct_p:.0f}%)  crowded long",
        f"Short fund  {neg}  ({pct_n:.0f}%)  crowded short",
        f"Flat        {zero}",
        "",
    ])
    bar_len = 12
    n_long_bar = max(0, min(bar_len, int(round(pct_p / 100 * bar_len))))
    bar = "█" * n_long_bar + "░" * (bar_len - n_long_bar)
    lines.append(f"Tilt  [{bar}]  {pct_p:.0f}% long")
    lines.append("")
    top_long = sorted(parsed, key=lambda x: -x[1])[:5]
    top_short = sorted(parsed, key=lambda x: x[1])[:5]
    if top_long and top_long[0][1] > 0:
        lines.append("Top long funding")
        for sym, rate in top_long:
            if rate <= 0:
                break
            lines.append(f"  {sym:<8}  {rate:+.5f}")
        lines.append("")
    if top_short and top_short[0][1] < 0:
        lines.append("Top short funding")
        for sym, rate in top_short:
            if rate >= 0:
                break
            lines.append(f"  {sym:<8}  {rate:+.5f}")
        lines.append("")
    lines.append("Baca cepat")
    lines.append(_ui_sep())
    if pct_p >= 75:
        lines.append("Sangat crowded long → kandidat SHORT squeeze.")
    elif pct_p >= 60:
        lines.append("Bias long funding → pantau FUNDING_SQUEEZE short.")
    elif pct_p <= 25:
        lines.append("Sangat crowded short → kandidat LONG squeeze.")
    elif pct_p <= 40:
        lines.append("Bias short funding → pantau bounce / long setup.")
    else:
        lines.append("Seimbang — andalkan struktur + L3 confirm, bukan funding saja.")
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
    if len(body) > 900:
        body = body[:900] + "…"
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
    if data == "menu:shadow":
        return _fmt_shadow_report(memory), _kb_shadow_gates(memory)
    if data.startswith("shadow:gate:"):
        gate = data.split(":", 2)[-1]
        return _fmt_shadow_gate_detail(memory, gate), _kb_back()
    if data == "menu:settings":
        return _fmt_settings_view(memory), _kb_settings(memory)
    if data == "menu:calc":
        return _fmt_calc_help(), _kb_back()
    if data == "menu:breadth":
        return _fmt_breadth(memory), _kb_back()
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
            "text": (text[:3997] + "…") if len(text) > 4000 else text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
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
    """
    Poll getUpdates, proses /start + callback tombol.
    long_poll_sec>0 → Telegram long-poll (respons tombol ~detik, bukan per cycle).
    """
    global _TG_UPDATE_OFFSET
    if not bot.available() or dry_run:
        return 0
    global _TG_AUTH_WARNED
    allowed = _allowed_chat_ids()
    if not allowed:
        # Fail-closed + warn-once (hindari spam tiap long-poll ~25s)
        if not _TG_AUTH_WARNED:
            logger.warning(
                "telegram UI · no allowed chat_ids · UI disabled until env fixed"
            )
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

        # --- callback tombol ---
        cq = upd.get("callback_query")
        if cq:
            cq_id = str(cq.get("id") or "")
            data = str(cq.get("data") or "")
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {})
            chat_id = str(chat.get("id") or "")
            mid = msg.get("message_id")
            if chat_id not in allowed:
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
        if chat_id not in allowed:
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
    _last_saved_off = _TG_UPDATE_OFFSET
    while not stop.is_set():
        try:
            n = await handle_telegram_updates(
                bot, memory, dry_run=dry_run, long_poll_sec=poll_sec,
            )
            if n:
                logger.info("📲 telegram UI · handled %d", n)
            # Persist offset tiap batch handled (atau berubah)
            if _TG_UPDATE_OFFSET != _last_saved_off:
                try:
                    memory.set_runtime("tg_update_offset", str(_TG_UPDATE_OFFSET))
                    _last_saved_off = _TG_UPDATE_OFFSET
                except Exception:
                    pass
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
                await asyncio.sleep(e.retry_after)
                await self._queue.put(msg)
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


def _format_bs_alert_text(payload: dict) -> str:
    """Format black-swan Telegram text from pending payload."""
    if payload.get("resume"):
        return (
            "✅ Black Swan cleared\n"
            f"Held {payload.get('hours', '?')}h  ·  indicators normal"
        )
    inds = payload.get("indicators") or []
    return (
        "🚨 BLACK SWAN MODE\n"
        f"Indicators  {', '.join(str(x) for x in inds) or '—'}\n"
        f"Count {payload.get('count', '?')}  ·  "
        "active signals extended +6h"
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
    _engine._CIRCUIT_MEMORY = memory  # type: ignore[attr-defined]
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
            await refresh_binance_usdt_symbols(memory)
            await gem.refresh_macro_calendar(memory)
    except Exception as e:
        logger.debug("macro calendar gemini skip: %s", type(e).__name__)
    try:
        evs = parse_macro_events(memory)
        nxt = minutes_to_next_macro(memory)
        today_n = len(macro_events_today(memory))
        logger.info(
            "📅 macro calendar · %d events · today=%d · next=%s %s%s",
            len(evs),
            today_n,
            (nxt[1] or "—"),
            f"{nxt[0]:.0f}m" if nxt[0] is not None else "",
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
                    sym = ev.get("symbol") or "?"
                    direction = ev.get("direction") or ""
                    sid_ev = str(ev.get("signal_id") or "")
                    outcome_ev = str(ev.get("outcome") or "")
                    dedup_key = f"{sid_ev}:{reason}:{outcome_ev}"
                    if sid_ev and not memory.claim_alert(
                        dedup_key, signal_id=sid_ev, reason=str(reason),
                    ):
                        logger.debug(
                            "⏭ alert dedup skip · %s %s · %s",
                            sym, direction, reason,
                        )
                        continue
                    px_s = f" @ {_ui_px(ev['px'])}" if ev.get("px") is not None else ""
                    rr_s = ""
                    if ev.get("realized_rr") is not None:
                        rr_s = f"\nR:R {ev['realized_rr']:+.2f}"
                    if reason == "TP1":
                        plain_tp1 = (
                            f"🎯 TP1 HIT  {sym} {direction}{px_s}\n"
                            f"SL → breakeven\n"
                            f"Sisa posisi → TP2 (masih aktif)"
                        )
                        for chat in {c for c in (user_chat, op_chat) if c}:
                            await queue.enqueue(TelegramMessage(
                                chat_id=chat, text=plain_tp1, priority="normal",
                            ))
                        continue
                    icon = {
                        "PROFIT": "✅", "LOSS": "🛑",
                        "PARTIAL": "🎯", "NEUTRAL": "⏱",
                    }.get(ev.get("outcome"), "•")
                    # Alasan human-readable
                    reason_label = {
                        "TP2": "Take Profit 2",
                        "SL": "Stop Loss",
                        "TIME_STOP": "Time stop (stall di zone)",
                        "expired": "Expired",
                        "arm_timeout": "No entry (arm timeout)",
                        "missed_entry": "Missed entry (price through TP)",
                    }.get(reason, reason)
                    plain_c = (
                        f"{icon} CLOSE  {sym} {direction}\n"
                        f"{ev.get('outcome')}  ·  {reason_label}{px_s}{rr_s}\n"
                        f"build {globals().get('CRYPTONE_BUILD', '?')}"
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
                    top = sorted(c_map.items(), key=lambda x: -x[1])[:3]
                    top_s = ", ".join(f"{s}=${u/1e6:.2f}M" for s, u in top)
                    logger.info("🌊 cascade top · %s", top_s)
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
                    plain = format_signal_message(s)
                    kb = _kb_signal(s.signal_id)
                    push_ok, push_note = should_push_signal(s.tier, memory)
                    if not push_ok:
                        logger.info(
                            "⏸ hold push %s %s Tier %s · %s (tetap di DB/Radar)",
                            s.symbol, s.direction, s.tier, push_note,
                        )
                        continue
                    # Sanity: kartu harus multi-line (cegah compact format lolos)
                    if "\n" not in plain or "build " not in plain:
                        logger.error(
                            "▸ CARD FORMAT BUG · %s · lines=%d · forcing rebuild",
                            s.symbol, plain.count("\n") + 1,
                        )
                        plain = format_signal_message(s)
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
    if tp1_hit and tp1 is not None:
        tp1_f = float(tp1)
        # guard: tp1 == entry → partial leg 0 (hindari artefak)
        r_tp1 = abs(tp1_f - entry) / risk if abs(tp1_f - entry) > 1e-12 * max(abs(entry), 1.0) else 0.0
        return 0.5 * r_tp1 + 0.5 * r_exit
    return r_exit


_NO_PRICE_WARNED: dict[str, float] = {}
_NO_PRICE_WARN_TTL = 3600.0


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
        mids = await _engine.hl_all_mids(memory)
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
                    memory.resolve_signal(
                        sid, outcome, tp1_hit=True if already_tp1 else None,
                    )
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
                since = now
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
                    memory.resolve_signal(sid, "NEUTRAL", reason=reason)
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
        entry_mid_ts = (
            (entry_lo + entry_hi) / 2.0 if (entry_lo > 0 and entry_hi >= entry_lo)
            else None
        )
        still_in_zone = False
        if (
            current_px is not None
            and entry_lo > 0
            and entry_hi >= entry_lo
        ):
            px = float(current_px)
            # Pad longgar: 50% lebar zone ATAU 0.25×ATR — noise tidak escape TIME_STOP
            zone_w = max(entry_hi - entry_lo, 0.0)
            atr_pad = 0.0
            hist_pad = memory.get_atr_history(sym, "1h") if memory else []
            if hist_pad:
                atr_pad = float(hist_pad[-1]) * 0.25
            elif entry_mid_ts:
                atr_pad = abs(entry_mid_ts) * 0.002
            pad = max(zone_w * 0.50, atr_pad, abs(entry_mid_ts or entry_lo) * 0.001)
            still_in_zone = (entry_lo - pad) <= px <= (entry_hi + pad)

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
        # TP2 melewati TP1 secara logika → selalu weighted 0.5/0.5 (bukan full R)
        if tp2_hit:
            exit_px = float(tp2) if tp2 > 0 else float(ref_px or 0)
            rr = _realized_rr(
                direction, entry_lo, entry_hi, original_stop,
                exit_px, tp1=tp1, tp1_hit=True,
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

        # 5) TIME_STOP — setelah SL/TP; kalau sudah TP1 → PARTIAL (bukan NEUTRAL)
        if (
            state == "ARMED"
            and armed_at is not None
            and time_stop_h is not None
            and (now - armed_at).total_seconds() / 3600.0 >= time_stop_h
            and current_px is not None
            and still_in_zone
        ):
            if already_tp1:
                # Sudah bank TP1; stall di zone = scratch/BE → PARTIAL
                exit_px = float(current_px)
                rr = _realized_rr(
                    direction, entry_lo, entry_hi, original_stop,
                    exit_px, tp1=tp1, tp1_hit=True,
                )
                memory.resolve_signal(
                    sid, "PARTIAL", realized_rr=rr, tp1_hit=True,
                    reason="TIME_STOP",
                )
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
                memory.resolve_signal(sid, "NEUTRAL", reason="TIME_STOP")
                events.append({
                    "signal_id": sid, "symbol": sym, "direction": direction,
                    "outcome": "NEUTRAL", "reason": "TIME_STOP", "px": current_px,
                })
                logger.info(
                    "⏰ TIME_STOP %s %s · NEUTRAL · %.6g still in entry zone",
                    sym, direction, current_px,
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
            memory.resolve_signal(
                sid, outcome, realized_rr=rr, tp1_hit=already_tp1 or None,
            )
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
    False → corrupt (restore/rebuild).
    OperationalError (locked) → re-raise: jangan rebuild DB sehat yang sedang di-lock.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return row is not None and row[0] == "ok"
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # locked / busy — bukan corrupt
        raise
    except Exception:
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
        asyncio.run(run_live_service(dry_run=args.dry_run))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
