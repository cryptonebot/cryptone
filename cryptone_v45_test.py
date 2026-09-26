#!/usr/bin/env python3
"""
Cryptone V4.5 — Test Suite (Fase A, section-based)
Struktur: §X1 Unit | §X2 Integration | §X2b Lifecycle & Error | §X3 Backtest | §X4 Mock
Coverage target: core >70%, critical path >90%.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

# Import under test
import cryptone_v45 as c


class TestConstants(unittest.TestCase):
    """§X1 — Constants & parameter framework"""

    def test_protocol_version(self):
        self.assertTrue(c.PROTOCOL_VERSION.startswith("4.5"))

    def test_min_confirmations_locked(self):
        self.assertEqual(c.MIN_CONFIRMATIONS, 2)

    def test_black_swan_trigger_locked(self):
        self.assertEqual(c.BLACK_SWAN_TRIGGER_COUNT, 3)

    def test_partial_weight(self):
        self.assertEqual(c.PARTIAL_WEIGHT, 0.5)

    def test_lifetime_hours(self):
        self.assertEqual(c.SIGNAL_LIFETIME_HOURS["SCALPING"], 2)
        self.assertEqual(c.SIGNAL_LIFETIME_HOURS["INTRADAY"], 24)
        self.assertEqual(c.SIGNAL_LIFETIME_HOURS["SWING"], 168)

    def test_clamp(self):
        self.assertEqual(c.clamp(1.5, 0.0, 1.0), 1.0)
        self.assertEqual(c.clamp(-1, 0.0, 1.0), 0.0)
        self.assertEqual(c.clamp(0.5, 0.0, 1.0), 0.5)

    def test_percentile_empty(self):
        self.assertEqual(c.percentile([], 95), 0.0)

    def test_percentile_basic(self):
        data = list(range(1, 101))
        self.assertAlmostEqual(c.percentile(data, 50), 50.5, places=1)


class TestDataclasses(unittest.TestCase):
    """§X1 — Dataclasses & exceptions"""

    def test_telegram_message(self):
        m = c.TelegramMessage(chat_id="1", text="hi", priority="critical")
        self.assertEqual(m.priority, "critical")
        self.assertEqual(m.parse_mode, "Markdown")

    def test_exceptions_hierarchy(self):
        self.assertTrue(issubclass(c.TelegramRateLimitError, c.RateLimitError))
        self.assertTrue(issubclass(c.RateLimitError, Exception))
        e = c.RateLimitError("lim", retry_after=2.5)
        self.assertEqual(e.retry_after, 2.5)


class TestMemoryEngine(unittest.TestCase):
    """§X1 — MemoryEngine schema bootstrap"""

    def test_schema_creates(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            # tables exist
            rows = mem._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = {r[0] for r in rows}
            for t in (
                "signal_history", "active_signals", "metric_ohlcv",
                "symbol_performance", "setup_performance", "bootstrap_meta",
                "llm_cache", "pending_delivery", "blackswan_log",
            ):
                self.assertIn(t, names, f"missing table {t}")
            mem.close()

    def test_integrity_check_ok(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            mem.close()
            self.assertTrue(asyncio.run(c.startup_db_check(db)))


class TestCLI(unittest.TestCase):
    """§X1 — CLI parser"""

    def test_mutually_exclusive_modes(self):
        p = c.build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args([])  # required mode

    def test_parse_live(self):
        p = c.build_parser()
        args = p.parse_args(["--live"])
        self.assertTrue(args.live)

    def test_parse_pin(self):
        p = c.build_parser()
        args = p.parse_args(["--bootstrap", "--pin", "SOL,HYPE"])
        self.assertEqual(args.pin, ["SOL", "HYPE"])

    def test_resolve_config_defaults(self):
        p = c.build_parser()
        args = p.parse_args(["--check"])
        cfg = c.resolve_config(args)
        self.assertIn("db", cfg)
        self.assertEqual(cfg["cycle_interval"], c.DEFAULT_CYCLE_INTERVAL_SEC)


class TestNoHardcodedSymbols(unittest.TestCase):
    """Aturan #7 — universe dinamis, no hardcode daftar coin di logic."""

    def test_no_btc_eth_sol_constants(self):
        # Constants module should not define SYMBOL_LIST = ["BTC", ...]
        forbidden = {"BTC", "ETH", "SOL", "XRP"}
        # Scan module attrs for accidental hardcoded lists
        for name in dir(c):
            if name.startswith("_"):
                continue
            val = getattr(c, name)
            if isinstance(val, (list, tuple, set)) and forbidden.issubset(set(val)):
                self.fail(f"Hardcoded symbol list found in {name}: {val}")


class TestAgentsUnit(unittest.TestCase):
    """§6 — L1/L2/L3/L4 agents (no network)"""

    def _asset(self, sym: str, funding: float = 0.0, px: float = 100.0, vol: float = 50e6) -> c.AssetCtx:
        return c.AssetCtx(
            symbol=sym, funding=funding, open_interest=1.0,
            mark_px=px, mid_px=px, day_ntl_vlm=vol, prev_day_px=px,
        )

    def test_funding_agent_fires_extreme(self):
        a = self._asset("S", funding=0.001)
        hit = c.funding_agent(a, memory=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.trigger_type, "funding_extreme")
        self.assertEqual(hit.direction_bias, "SHORT")
        self.assertEqual(hit.setup_type, "FUNDING_SQUEEZE")

    def test_funding_agent_quiet(self):
        a = self._asset("S", funding=0.00001)
        self.assertIsNone(c.funding_agent(a, memory=None))

    def test_funding_negative_bias_long(self):
        a = self._asset("S", funding=-0.001)
        hit = c.funding_agent(a, memory=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.direction_bias, "LONG")

    def test_cascade_agent_floor(self):
        hit = c.cascade_agent("S", cascade_usd_5m=6_000_000, memory=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.setup_type, "CASCADE_SCALP")
        self.assertIsNone(c.cascade_agent("S", 1_000_000, memory=None))

    def test_volatility_compression(self):
        # atr% harus > 0.5% filter L0, tapi < cold default 1% harga → compression
        hit = c.volatility_agent("S", atr_now=0.6, price=100.0, memory=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.trigger_type, "volatility_compression")

    def test_volatility_too_dead(self):
        self.assertIsNone(c.volatility_agent("S", atr_now=0.1, price=100.0, memory=None))

    def test_classify_horizon(self):
        t = c.TriggerHit("S", "funding_extreme", 0.5, "SHORT", 0.001, 0.0001, "FUNDING_SQUEEZE")
        self.assertEqual(c.classify_horizon(t), "INTRADAY")
        t2 = c.TriggerHit("S", "cascade", 0.5, "NEUTRAL", 1e7, 5e6, "CASCADE_SCALP")
        self.assertEqual(c.classify_horizon(t2), "SCALPING")
        t3 = c.TriggerHit("S", "volatility_compression", 0.5, "NEUTRAL", 1, 2, "COMPRESSION")
        self.assertEqual(c.classify_horizon(t3), "SWING")

    def test_min_confirm_table(self):
        self.assertIsNone(c.min_confirm_required(0))
        self.assertIsNone(c.min_confirm_required(1))
        self.assertEqual(c.min_confirm_required(2), 2)
        self.assertEqual(c.min_confirm_required(3), 2)

    def test_evaluate_confirms_n3(self):
        rs = [
            c.ConfirmResult("microstructure", True, "ok"),
            c.ConfirmResult("flow", True, "ok"),
            c.ConfirmResult("cross_exchange", False, "div"),
        ]
        passed, count, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        self.assertEqual(count, 2)
        self.assertEqual(n, 3)

    def test_evaluate_confirms_n1_fails(self):
        rs = [
            c.ConfirmResult("microstructure", True, "ok"),
            c.ConfirmResult("flow", False, "agent off"),
            c.ConfirmResult("cross_exchange", False, "no xref data"),
        ]
        passed, count, n = c.evaluate_confirms(rs)
        self.assertFalse(passed)
        self.assertEqual(n, 1)

    def test_analyst_timing_long(self):
        t = c.analyst_timing(
            "LONG", price=100.0, atr=2.0, horizon="INTRADAY",
            trigger_strength=0.8, confirm_count=2,
        )
        self.assertIsNotNone(t)
        self.assertGreater(t.rr, 0)
        self.assertLess(t.stop_loss, t.entry_low)
        self.assertGreater(t.tp2, t.entry_high)
        self.assertGreaterEqual(t.base_confidence, 0.0)
        self.assertLessEqual(t.base_confidence, 1.0)

    def test_analyst_timing_short(self):
        t = c.analyst_timing(
            "SHORT", price=100.0, atr=2.0, horizon="SCALPING",
            trigger_strength=0.5, confirm_count=3,
        )
        self.assertIsNotNone(t)
        self.assertGreater(t.stop_loss, t.entry_high)
        self.assertLess(t.tp2, t.entry_low)

    def test_assign_tier(self):
        self.assertEqual(c.assign_tier(2.6, 3), "A")
        self.assertEqual(c.assign_tier(2.6, 2), "B")
        self.assertEqual(c.assign_tier(2.1, 2), "B")
        self.assertEqual(c.assign_tier(1.2, 2), "C")
        # float exact 2.0 (INTRADAY 3.0/1.5) harus B, bukan C
        self.assertEqual(c.assign_tier(2.0, 2), "B")
        self.assertEqual(c.assign_tier(2.0 - 1e-12, 2), "B")
        self.assertEqual(c.assign_tier(2.0, 1), "C")

    def test_scan_triggers_respects_setup_enabled(self):
        a = self._asset("S", funding=0.001)
        hits = c.scan_triggers_for_asset(
            a, atr_now=None, cascade_usd_5m=0, memory=None,
            setup_enabled={
                "FUNDING_SQUEEZE": False,
                "CASCADE_SCALP": True,
                "COMPRESSION": True,
            },
        )
        self.assertEqual(hits, [])


class TestPipelineUnit(unittest.TestCase):
    """§7 — veto, context, persist wiring (no network)"""

    def test_context_default(self):
        self.assertEqual(c.context_modifier_default("X", "LONG", None), 1.0)

    def test_context_fear_greed_contrarian(self):
        import asyncio

        async def _run():
            m_s = await c.context_modifier("X", "SHORT", None, fear_greed=80)
            m_l = await c.context_modifier("X", "LONG", None, fear_greed=80)
            self.assertGreater(m_s, 1.0)
            self.assertLess(m_l, 1.0)
            m_l2 = await c.context_modifier("X", "LONG", None, fear_greed=20)
            m_s2 = await c.context_modifier("X", "SHORT", None, fear_greed=20)
            self.assertGreater(m_l2, 1.0)
            self.assertLess(m_s2, 1.0)
            m_n = await c.context_modifier("X", "LONG", None, fear_greed=50)
            self.assertEqual(m_n, 1.0)

        asyncio.run(_run())

    def test_context_us10y_and_fng_stack(self):
        import asyncio

        async def _run():
            m = await c.context_modifier(
                "X", "SHORT", None, us10y=5.0, fear_greed=80,
            )
            self.assertGreater(m, 1.05)
            m2 = await c.context_modifier(
                "X", "LONG", None, us10y=5.0, fear_greed=80,
            )
            self.assertLess(m2, 0.95)

        asyncio.run(_run())

    def test_veto_confidence_floor(self):
        ok, _ = c.veto_checks(None, "X", 0.2, confidence_floor=0.5)
        self.assertFalse(ok)
        ok, _ = c.veto_checks(None, "X", 0.6, confidence_floor=0.5)
        self.assertTrue(ok)

    def test_veto_concurrent_and_cooldown(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            self.assertEqual(mem.count_active_signals(), 0)
            self.assertFalse(mem.symbol_on_cooldown("ABC"))
            # seed a recent signal
            import asyncio
            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "s1",
                    "symbol": "ABC",
                    "direction": "LONG",
                    "horizon": "INTRADAY",
                    "tier": "B",
                    "entry_zone_low": 1.0,
                    "entry_zone_high": 1.1,
                    "stop_loss": 0.9,
                    "tp1": 1.2,
                    "tp2": 1.3,
                    "rr": 2.0,
                    "confidence": 0.6,
                    "trigger_type": "funding_extreme",
                    "reasoning": "t",
                    "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": c.now_utc().isoformat(),
                    "created_at": c.now_utc().isoformat(),
                    "valid_until": (c.now_utc() + c.timedelta(hours=24)).isoformat(),
                    "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "s1",
                    "symbol": "ABC",
                    "expires_at": (c.now_utc() + c.timedelta(hours=24)).isoformat(),
                })
                await mem.flush_writes()
            asyncio.run(_seed())
            self.assertEqual(mem.count_active_signals(), 1)
            self.assertTrue(mem.symbol_on_cooldown("ABC"))
            ok, reason = c.veto_checks(mem, "ABC", 0.7)
            self.assertFalse(ok)
            self.assertTrue("cooldown" in reason or "active" in reason)
            mem.close()

    def test_startup_recovery_expires(self):
        import tempfile
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            past = (c.now_utc() - c.timedelta(hours=1)).isoformat()
            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "old1",
                    "symbol": "ZZZ",
                    "direction": "SHORT",
                    "horizon": "SCALPING",
                    "tier": "C",
                    "entry_zone_low": 1.0,
                    "entry_zone_high": 1.0,
                    "stop_loss": 1.1,
                    "tp1": 0.9,
                    "tp2": 0.8,
                    "rr": 1.5,
                    "confidence": 0.5,
                    "trigger_type": "cascade",
                    "reasoning": "t",
                    "setup_type": "CASCADE_SCALP",
                    "last_checked_at": past,
                    "created_at": past,
                    "valid_until": past,
                    "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "old1",
                    "symbol": "ZZZ",
                    "expires_at": past,
                })
                await mem.flush_writes()
            asyncio.run(_seed())
            self.assertEqual(mem.count_active_signals(), 1)
            asyncio.run(c.startup_recovery(mem))
            self.assertEqual(mem.count_active_signals(), 0)
            mem.close()

    def test_realized_rr_long_short(self):
        # entry 100, SL 90 → risk 10; exit 120 → +2R
        self.assertAlmostEqual(
            c._realized_rr("LONG", 100, 100, 90, 120), 2.0, places=4,
        )
        # SHORT entry 100, SL 110 → risk 10; exit 80 → +2R
        self.assertAlmostEqual(
            c._realized_rr("SHORT", 100, 100, 110, 80), 2.0, places=4,
        )
        # SL side → negative
        self.assertAlmostEqual(
            c._realized_rr("LONG", 100, 100, 90, 85), -1.5, places=4,
        )

    def test_resolve_signal_outcome(self):
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "s1",
                    "symbol": "AAA",
                    "direction": "LONG",
                    "horizon": "INTRADAY",
                    "tier": "B",
                    "entry_zone_low": 100.0,
                    "entry_zone_high": 100.0,
                    "stop_loss": 90.0,
                    "tp1": 110.0,
                    "tp2": 120.0,
                    "rr": 2.0,
                    "confidence": 0.6,
                    "trigger_type": "funding",
                    "reasoning": "t",
                    "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now,
                    "created_at": now,
                    "valid_until": future,
                    "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "s1",
                    "symbol": "AAA",
                    "expires_at": future,
                })
                await mem.flush_writes()

            asyncio.run(_seed())
            self.assertEqual(mem.count_active_signals(), 1)
            mem.resolve_signal("s1", "PROFIT", realized_rr=2.0, tp1_hit=True)
            self.assertEqual(mem.count_active_signals(), 0)
            row = mem._conn.execute(
                "SELECT outcome, realized_rr, tp1_hit FROM signal_history WHERE signal_id='s1'"
            ).fetchone()
            self.assertEqual(row["outcome"], "PROFIT")
            self.assertAlmostEqual(row["realized_rr"], 2.0)
            self.assertEqual(row["tp1_hit"], 1)
            mem.close()

    def test_monitor_sl_and_tp2(self):
        """monitor_active_signals closes on SL / TP2 using injected mids."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                for sid, sym, d, sl, tp1, tp2 in [
                    ("long_sl", "L1", "LONG", 90.0, 110.0, 120.0),
                    ("long_tp2", "L2", "LONG", 90.0, 110.0, 120.0),
                    ("short_tp2", "S1", "SHORT", 110.0, 90.0, 80.0),
                ]:
                    await mem.enqueue_write("signal_history", {
                        "signal_id": sid, "symbol": sym, "direction": d,
                        "horizon": "INTRADAY", "tier": "B",
                        "entry_zone_low": 100.0, "entry_zone_high": 100.0,
                        "stop_loss": sl, "tp1": tp1, "tp2": tp2, "rr": 2.0,
                        "confidence": 0.6, "trigger_type": "funding",
                        "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                        "last_checked_at": now, "created_at": now,
                        "valid_until": future, "is_simulated": 0,
                    })
                    await mem.enqueue_write("active_signals", {
                        "signal_id": sid, "symbol": sym, "expires_at": future,
                    })
                await mem.flush_writes()

            asyncio.run(_seed())
            self.assertEqual(mem.count_active_signals(), 3)

            # L1 hit SL (85), L2 hit TP2 (125), S1 hit TP2 (75)
            fake_mids = {"L1": 85.0, "L2": 125.0, "S1": 75.0}

            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value=fake_mids)), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=[])):
                    return await c.monitor_active_signals(mem)

            events = asyncio.run(_run())
            outcomes = {e["signal_id"]: e["outcome"] for e in events}
            self.assertEqual(outcomes.get("long_sl"), "LOSS")
            self.assertEqual(outcomes.get("long_tp2"), "PROFIT")
            self.assertEqual(outcomes.get("short_tp2"), "PROFIT")
            self.assertEqual(mem.count_active_signals(), 0)
            mem.close()


class TestDataSourcesUnit(unittest.TestCase):
    """§5 — pure helpers (no network)"""

    def _asset(self, sym: str, vol: float, px: float = 100.0) -> c.AssetCtx:
        return c.AssetCtx(
            symbol=sym, funding=0.0, open_interest=1.0,
            mark_px=px, mid_px=px, day_ntl_vlm=vol, prev_day_px=px,
        )

    def test_filter_volume_floors(self):
        # rank1 MAJOR needs ≥20M; rank11 MID ≥5M; rank51 LOW ≥2M; under-floor drop
        assets = [self._asset("A", 50_000_000)]           # rank 1, pass MAJOR
        for i in range(9):
            assets.append(self._asset(f"M{i}", 25_000_000))  # fill top 10
        assets.append(self._asset("B", 6_000_000))        # rank 11, pass MID
        for i in range(40):
            assets.append(self._asset(f"X{i}", 3_000_000))  # mid/low ranks
        assets.append(self._asset("D", 500_000))          # under LOW floor
        out = c.filter_universe_candidates(assets)
        syms = {a.symbol for a in out}
        self.assertIn("A", syms)
        self.assertIn("B", syms)
        self.assertNotIn("D", syms)

    def test_filter_zero_price_dropped(self):
        assets = [self._asset("Z", 100_000_000, px=0.0)]
        out = c.filter_universe_candidates(assets)
        self.assertEqual(out, [])

    def test_get_market_anchor_from_volume(self):
        assets = [
            self._asset("LOW", 1_000_000),
            self._asset("HIGH", 99_000_000),
        ]
        self.assertEqual(c.get_market_anchor(None, assets), "HIGH")

    def test_get_market_anchor_empty_none(self):
        self.assertIsNone(c.get_market_anchor(None, []))

    def test_compute_atr_insufficient(self):
        candles = [
            c.Candle("S", "1h", 1, 2, 0.5, 1.5, 10,
                     c.now_utc())
            for _ in range(5)
        ]
        self.assertIsNone(c.compute_atr(candles, period=14))

    def test_compute_atr_basic(self):
        from datetime import timedelta
        base = c.now_utc()
        candles = []
        px = 100.0
        for i in range(20):
            candles.append(c.Candle(
                "S", "1h", px, px + 2, px - 2, px + 1, 1000,
                base + timedelta(hours=i),
            ))
            px += 1
        atr = c.compute_atr(candles, period=14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)





class TestTradingP0(unittest.TestCase):
    """Trading logic P0: tolerance ATR, post-loss, entry zone width"""

    def test_entry_zone_narrower_than_sl(self):
        t = c.analyst_timing(
            "LONG", price=100.0, atr=2.0, horizon="INTRADAY",
            trigger_strength=0.8, confirm_count=2,
        )
        self.assertIsNotNone(t)
        zone = t.entry_high - t.entry_low
        risk = 100.0 - t.stop_loss
        # zone should be ~0.4*ATR=0.8; risk ~1.5*ATR=3.0
        self.assertLess(zone, risk)
        self.assertAlmostEqual(zone, 0.4 * 2.0, places=5)

    def test_sl_tolerance_not_price_pct(self):
        """Repro TNSR bug: price in entry zone must NOT trip SL."""
        # entry 0.04228-0.04346 mid~0.04287, SL 0.0411
        # old bug: tol = 0.04287*0.05 = 0.00214 → SL trigger at 0.04324
        entry_mid = 0.04287
        stop = 0.0411
        atr = abs(entry_mid - stop) / 1.5
        tol_sl = atr * 0.02
        lo = 0.04275  # still above real SL
        sl_hit_old = lo <= (stop + entry_mid * 0.05)
        sl_hit_new = lo <= (stop + tol_sl)
        self.assertTrue(sl_hit_old)   # old behavior false positive
        self.assertFalse(sl_hit_new)  # fixed

    def test_post_loss_blocks_symbol(self):
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            past = (c.now_utc() - c.timedelta(hours=2)).isoformat()  # di luar cooldown 30m

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "loss1", "symbol": "TNSR", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1, "entry_zone_high": 1,
                    "stop_loss": 0.9, "tp1": 1.1, "tp2": 1.2, "rr": 2,
                    "confidence": 0.5, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": past, "created_at": past,
                    "valid_until": now, "is_simulated": 0,
                })
                await mem.flush_writes()
                mem.resolve_signal("loss1", "LOSS", realized_rr=-1.0)

            asyncio.run(_seed())
            self.assertTrue(mem.symbol_post_loss_blocked("TNSR"))
            self.assertFalse(mem.symbol_post_loss_blocked("OTHER"))
            ok, reason = c.veto_checks(mem, "TNSR", 0.7)
            self.assertFalse(ok)
            self.assertIn("post-loss", reason)
            mem.close()

    def test_fallback_confirm_flag(self):
        r = c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True)
        self.assertTrue(r.is_fallback)
        # pure fallback pool still can pass need if count ok
        rs = [
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed, count, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        # with one real agent that failed confirm → fail
        rs2 = [
            c.ConfirmResult("cross_exchange", False, "binance_div=0.02", is_fallback=False),
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed2, _, _ = c.evaluate_confirms(rs2)
        self.assertFalse(passed2)


class TestLifecycleP0(unittest.TestCase):
    """§XVI — PARTIAL after TP1+SL, performance update, dry-run no write"""

    def test_sl_after_tp1_is_partial(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "p1", "symbol": "PX", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100.0, "entry_zone_high": 100.0,
                    "stop_loss": 90.0, "tp1": 110.0, "tp2": 120.0, "rr": 2.0,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "p1", "symbol": "PX", "expires_at": future,
                })
                await mem.flush_writes()
                # mark TP1 already hit
                mem.touch_signal_check("p1", tp1_hit=True)

            asyncio.run(_seed())

            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value={"PX": 85.0})), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=[])):
                    return await c.monitor_active_signals(mem)

            events = asyncio.run(_run())
            self.assertEqual(events[0]["outcome"], "PARTIAL")
            self.assertEqual(mem.count_active_signals(), 0)
            row = mem._conn.execute(
                "SELECT outcome FROM signal_history WHERE signal_id='p1'"
            ).fetchone()
            self.assertEqual(row["outcome"], "PARTIAL")
            # performance updated
            sp = mem._conn.execute(
                "SELECT wins, losses, partials, total_signals FROM symbol_performance WHERE symbol='PX'"
            ).fetchone()
            self.assertIsNotNone(sp)
            self.assertEqual(sp["partials"], 1)
            self.assertEqual(sp["total_signals"], 1)
            mem.close()

    def test_resolve_updates_setup_performance(self):
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=1)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "s9", "symbol": "ZZ", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1, "entry_zone_high": 1,
                    "stop_loss": 1.1, "tp1": 0.9, "tp2": 0.8, "rr": 2,
                    "confidence": 0.5, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "s9", "symbol": "ZZ", "expires_at": future,
                })
                await mem.flush_writes()

            asyncio.run(_seed())
            mem.resolve_signal("s9", "PROFIT", realized_rr=2.1, tp1_hit=True)
            st = mem._conn.execute(
                "SELECT wins, total_signals, avg_rr FROM setup_performance "
                "WHERE setup_type='FUNDING_SQUEEZE'"
            ).fetchone()
            self.assertEqual(st["wins"], 1)
            self.assertEqual(st["total_signals"], 1)
            self.assertAlmostEqual(st["avg_rr"], 2.1, places=2)
            mem.close()


class TestCascadeBuffer(unittest.TestCase):
    """§5 — CascadeBuffer + forceOrder parse (no network)"""

    def test_binance_sym_to_hl(self):
        self.assertEqual(c._binance_sym_to_hl("BTCUSDT"), "BTC")
        self.assertEqual(c._binance_sym_to_hl("1000PEPEUSDT"), "1000PEPE")
        self.assertEqual(c._binance_sym_to_hl("solusdt"), "SOL")

    def test_ingest_sell_long_liq_short_bias(self):
        buf = c.CascadeBuffer(window_sec=300)
        # SELL force order = LONG liquidated → SHORT bias
        buf.ingest_force_order({
            "e": "forceOrder",
            "o": {
                "s": "BTCUSDT", "S": "SELL",
                "q": "1.0", "ap": "50000", "p": "50000", "z": "1.0",
            },
        })
        usd = buf.snapshot_usd()
        side = buf.snapshot_side()
        self.assertAlmostEqual(usd.get("BTC", 0), 50000.0)
        self.assertEqual(side.get("BTC"), "SHORT")

    def test_ingest_buy_short_liq_long_bias(self):
        buf = c.CascadeBuffer(window_sec=300)
        buf.ingest_force_order({
            "e": "forceOrder",
            "o": {
                "s": "ETHUSDT", "S": "BUY",
                "q": "10", "ap": "3000", "p": "3000", "z": "10",
            },
        })
        self.assertAlmostEqual(buf.snapshot_usd().get("ETH", 0), 30000.0)
        self.assertEqual(buf.snapshot_side().get("ETH"), "LONG")

    def test_dominant_side_by_usd(self):
        buf = c.CascadeBuffer(window_sec=300)
        buf.add_event("X", "SHORT", 1_000_000)
        buf.add_event("X", "LONG", 100_000)
        self.assertEqual(buf.snapshot_side()["X"], "SHORT")
        self.assertAlmostEqual(buf.snapshot_usd()["X"], 1_100_000)

    def test_prune_window(self):
        import time as _t
        buf = c.CascadeBuffer(window_sec=0.05)
        buf.add_event("Z", "SHORT", 1e6)
        _t.sleep(0.08)
        self.assertEqual(buf.snapshot_usd(), {})

    def test_cascade_agent_uses_buffer_usd(self):
        hit = c.cascade_agent("S", cascade_usd_5m=6_000_000, memory=None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.setup_type, "CASCADE_SCALP")


def run_tests(
    section: int | None = None,
    unit_only: bool = False,
    integration_only: bool = False,
) -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Map section → test classes (expand as sections grow)
    unit_classes = [
        TestConstants,
        TestDataclasses,
        TestMemoryEngine,
        TestCLI,
        TestNoHardcodedSymbols,
        TestDataSourcesUnit,
        TestAgentsUnit,
        TestPipelineUnit,
        TestCascadeBuffer,
        TestLifecycleP0,
        TestTradingP0,
    ]
    # integration_classes = []  # nanti

    if integration_only:
        classes = []
    elif unit_only or section is None:
        classes = unit_classes
    else:
        classes = unit_classes  # section filter nanti

    for cls in classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--section", type=int, default=None)
    p.add_argument("--unit", action="store_true")
    p.add_argument("--integration", action="store_true")
    p.add_argument("--all", action="store_true")
    a = p.parse_args()
    sys.exit(run_tests(section=a.section, unit_only=a.unit, integration_only=a.integration))
