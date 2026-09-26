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

    def test_loss_circuit_breaker_constants(self):
        self.assertEqual(c.SETUP_LOSS_TRIGGER, 3)
        self.assertEqual(c.SETUP_LOSS_PAUSE_H, 2)
        self.assertEqual(c.GLOBAL_LOSS_TRIGGER, 5)
        self.assertEqual(c.GLOBAL_LOSS_PAUSE_H, 6)
        self.assertEqual(c.GEMINI_MODEL, "gemini-2.5-flash")

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

    def test_funding_cold_start_and_consistency_penalty(self):
        class _Memory:
            def get_funding_history(self, symbol, lookback_days=30):
                if lookback_days == 1:
                    return [0.0001, -0.0002, 0.0005]
                return []

        self.assertIsNotNone(c.funding_agent(self._asset("S", 0.0005), memory=None))
        self.assertIsNone(c.funding_agent(self._asset("S", 0.0002), memory=None))
        hit = c.funding_agent(self._asset("S", 0.0005), memory=_Memory())
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit.strength, (0.0005 - 0.0003) / 0.0003 * 0.5)

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
        passed, count, _, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        self.assertEqual(count, 2)
        self.assertEqual(n, 3)

    def test_evaluate_confirms_n1_fails(self):
        rs = [
            c.ConfirmResult("microstructure", True, "ok"),
            c.ConfirmResult("flow", False, "agent off"),
            c.ConfirmResult("cross_exchange", False, "no xref data"),
        ]
        passed, count, _, n = c.evaluate_confirms(rs)
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

    def test_gemini_failure_spam_stops_after_three(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            failing_post = AsyncMock(side_effect=RuntimeError("boom"))
            with patch("cryptone_v45.http_post_json", new=failing_post):
                results = [
                    await client.generate(
                        "test", f"prompt-{i}", use_cache=False,
                    )
                    for i in range(4)
                ]
            return results, failing_post

        results, failing_post = asyncio.run(_run())
        self.assertEqual(failing_post.await_count, 3)
        self.assertEqual(results[-1].error, "rate limited")
        self.assertFalse(results[-1].success)
        self.assertEqual(results[0].error, "boom")
        self.assertGreater(results[-1].latency_ms, -1)

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

    def test_cooldown_uses_resolved_at(self):
        import asyncio
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            created = (c.now_utc() - c.timedelta(minutes=40)).isoformat()
            resolved = (c.now_utc() - c.timedelta(minutes=5)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "cd1", "symbol": "CD", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100.0, "entry_zone_high": 100.0,
                    "stop_loss": 90.0, "tp1": 110.0, "tp2": 120.0, "rr": 2.0,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": resolved, "created_at": created,
                    "valid_until": resolved, "is_simulated": 0,
                })
                await mem.flush_writes()
                mem._conn.execute(
                    "UPDATE signal_history SET outcome='PROFIT', resolved_at=? "
                    "WHERE signal_id='cd1'",
                    (resolved,),
                )
                mem._conn.commit()

            asyncio.run(_seed())
            self.assertTrue(mem.symbol_on_cooldown("CD", cooldown_min=30))
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

    def test_partial_realized_rr_uses_original_stop(self):
        # TP1 at 1R, then BE exit: realized RR is +0.5R, not zero.
        self.assertAlmostEqual(
            c._realized_rr(
                "LONG", 100, 100, 90, 100, tp1=110, tp1_hit=True,
            ),
            0.5,
            places=4,
        )

    def test_tp2_realized_rr_uses_original_stop(self):
        self.assertAlmostEqual(
            c._realized_rr(
                "LONG", 100, 100, 90, 120, tp1=110, tp1_hit=False,
            ),
            2.0,
            places=4,
        )

    def test_loss_before_tp1_uses_original_stop(self):
        self.assertAlmostEqual(
            c._realized_rr(
                "LONG", 100, 100, 90, 90, tp1=110, tp1_hit=False,
            ),
            -1.0,
            places=4,
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
                        "valid_until": future, "is_simulated": 0, "state": "ARMED",
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

    def test_signal_message_not_truncated(self):
        """Regression: Telegram signal text must never slice content mid-word.

        Old bug: `plain += f"\\n{s.reasoning[:80]}"` could cut the reasoning
        string (setup|trigger|raw|thr|str|confirm|ctx) at an arbitrary byte
        offset, landing mid-word (e.g. "...confir"). format_signal_message
        builds the text from structured fields only, so nothing is sliced.
        """
        sig = c.PipelineSignal(
            signal_id="s1", symbol="TNSR", direction="LONG", horizon="INTRADAY",
            tier="B", setup_type="FUNDING_SQUEEZE",
            trigger_type="a_very_long_trigger_type_name_that_would_have_been_cut_before",
            entry_low=0.04228, entry_high=0.04346, stop_loss=0.0411,
            tp1=0.0451, tp2=0.0480, rr=2.0, confidence=0.69,
            base_confidence=0.6, context_mult=1.15,
            confirm_count=2, active_agent_count=2,
            reasoning="FUNDING_SQUEEZE | funding_extreme raw=-0.000723709 "
                      "thr=-0.0001 str=1.00 | confirm 2/2 | ctx×1.15",
            created_at=c.now_utc(), valid_until=c.now_utc(),
        )
        msg = c.format_signal_message(sig)
        # trigger_type must appear whole, never sliced
        self.assertIn(sig.trigger_type, msg)
        # never derived from slicing sig.reasoning
        self.assertNotIn(sig.reasoning[:80], msg)
        # required fields all present
        for expected in ("Entry", "SL", "TP1", "TP2", "R:R", "confirms", "ctx×1.15"):
            self.assertIn(expected, msg)

    def test_signal_message_omits_neutral_ctx(self):
        """ctx× suffix only shown when context_mult meaningfully != 1.0."""
        sig = c.PipelineSignal(
            signal_id="s2", symbol="LTC", direction="SHORT", horizon="INTRADAY",
            tier="B", setup_type="FUNDING_SQUEEZE", trigger_type="funding_extreme",
            entry_low=73.50, entry_high=73.99, stop_loss=75.60,
            tp1=71.89, tp2=70.04, rr=2.0, confidence=0.90,
            base_confidence=0.9, context_mult=1.0,
            confirm_count=2, active_agent_count=2, reasoning="x",
            created_at=c.now_utc(), valid_until=c.now_utc(),
        )
        msg = c.format_signal_message(sig)
        self.assertNotIn("ctx×", msg)

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
        passed, count, real_count, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        self.assertEqual(count, 2)
        self.assertEqual(real_count, 0)
        # with one real agent that failed confirm → fail
        rs2 = [
            c.ConfirmResult("cross_exchange", False, "binance_div=0.02", is_fallback=False),
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed2, _, _, _ = c.evaluate_confirms(rs2)
        self.assertFalse(passed2)

    def test_real_confirm_count_is_separate_from_fallbacks(self):
        rs = [
            c.ConfirmResult("microstructure", True, "book", is_fallback=False),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
            c.ConfirmResult("cross_exchange", True, "xref_div=0", is_fallback=True),
        ]
        passed, total_count, real_count, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        self.assertEqual((total_count, real_count, n), (3, 1, 3))
        self.assertEqual(c.assign_tier(2.0, real_count), "C")


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
                    "valid_until": future, "is_simulated": 0, "state": "ARMED",
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

    def test_tp1_moves_stop_to_breakeven(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "be1", "symbol": "BE", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100.0, "entry_zone_high": 102.0,
                    "stop_loss": 90.0, "tp1": 110.0, "tp2": 120.0, "rr": 2.0,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0, "state": "ARMED",
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "be1", "symbol": "BE", "expires_at": future,
                })
                await mem.flush_writes()

            asyncio.run(_seed())

            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value={"BE": 110.0})), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=[])):
                    return await c.monitor_active_signals(mem)

            events = asyncio.run(_run())
            self.assertEqual(events[0]["reason"], "TP1")
            row = mem._conn.execute(
                "SELECT stop_loss, tp1_hit, state FROM signal_history WHERE signal_id='be1'"
            ).fetchone()
            self.assertEqual(row["stop_loss"], 101.0)
            self.assertEqual(row["tp1_hit"], 1)
            self.assertEqual(row["state"], "ARMED")
            mem.close()

    def test_pending_waits_for_entry_zone(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                for sid, sym in (("pending_out", "PO"), ("pending_in", "PI")):
                    await mem.enqueue_write("signal_history", {
                        "signal_id": sid, "symbol": sym, "direction": "LONG",
                        "horizon": "INTRADAY", "tier": "B",
                        "entry_zone_low": 100.0, "entry_zone_high": 102.0,
                        "stop_loss": 110.0, "tp1": 112.0, "tp2": 120.0, "rr": 2.0,
                        "confidence": 0.6, "trigger_type": "funding",
                        "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                        "last_checked_at": now, "created_at": now,
                        "valid_until": future, "is_simulated": 0, "state": "PENDING",
                    })
                    await mem.enqueue_write("active_signals", {
                        "signal_id": sid, "symbol": sym, "expires_at": future,
                    })
                await mem.flush_writes()

            asyncio.run(_seed())

            async def _run():
                with patch(
                    "cryptone_v45.hl_all_mids",
                    new=AsyncMock(return_value={"PO": 95.0, "PI": 101.0}),
                ), patch(
                    "cryptone_v45.hl_candle_snapshot",
                    new=AsyncMock(return_value=[]),
                ):
                    return await c.monitor_active_signals(mem)

            self.assertEqual(asyncio.run(_run()), [])
            rows = mem._conn.execute(
                "SELECT signal_id, state, outcome FROM signal_history "
                "WHERE signal_id IN ('pending_out', 'pending_in') ORDER BY signal_id"
            ).fetchall()
            self.assertEqual(
                [(r["signal_id"], r["state"], r["outcome"]) for r in rows],
                [("pending_in", "ARMED", None), ("pending_out", "PENDING", None)],
            )
            armed = mem._conn.execute(
                "SELECT armed_at FROM signal_history WHERE signal_id='pending_in'"
            ).fetchone()
            self.assertIsNotNone(armed["armed_at"])
            self.assertEqual(mem.count_active_signals(), 2)
            mem.close()

    def test_time_stop_only_closes_stalled_armed_signal(self):
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            created = (c.now_utc() - c.timedelta(hours=7)).isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                for sid, sym in (("ts_zone", "TSZ"), ("ts_profit", "TSP")):
                    await mem.enqueue_write("signal_history", {
                        "signal_id": sid, "symbol": sym, "direction": "LONG",
                        "horizon": "INTRADAY", "tier": "B",
                        "entry_zone_low": 100.0, "entry_zone_high": 102.0,
                        "stop_loss": 90.0, "tp1": 120.0, "tp2": 130.0, "rr": 2.0,
                        "confidence": 0.6, "trigger_type": "funding",
                        "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                        "last_checked_at": created, "created_at": created,
                        "armed_at": created,
                        "valid_until": future, "is_simulated": 0, "state": "ARMED",
                    })
                    await mem.enqueue_write("active_signals", {
                        "signal_id": sid, "symbol": sym, "expires_at": future,
                    })
                await mem.flush_writes()

            asyncio.run(_seed())

            async def _run():
                with patch(
                    "cryptone_v45.hl_all_mids",
                    new=AsyncMock(return_value={"TSZ": 101.0, "TSP": 110.0}),
                ), patch(
                    "cryptone_v45.hl_candle_snapshot",
                    new=AsyncMock(return_value=[]),
                ):
                    return await c.monitor_active_signals(mem)

            events = asyncio.run(_run())
            self.assertEqual(
                [(e["signal_id"], e["reason"]) for e in events],
                [("ts_zone", "TIME_STOP")],
            )
            rows = mem._conn.execute(
                "SELECT signal_id, outcome FROM signal_history "
                "WHERE signal_id IN ('ts_zone', 'ts_profit') ORDER BY signal_id"
            ).fetchall()
            self.assertEqual(
                [(r["signal_id"], r["outcome"]) for r in rows],
                [("ts_profit", None), ("ts_zone", "NEUTRAL")],
            )
            self.assertEqual(mem.count_active_signals(), 1)
            mem.close()

    def test_fresh_armed_not_killed_by_time_stop(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            old_created = (c.now_utc() - c.timedelta(hours=20)).isoformat()
            fresh_armed = (c.now_utc() - c.timedelta(minutes=5)).isoformat()
            future = (c.now_utc() + c.timedelta(hours=6)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "fresh", "symbol": "FRESH", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100.0, "entry_zone_high": 102.0,
                    "stop_loss": 90.0, "original_stop": 90.0,
                    "tp1": 120.0, "tp2": 130.0, "rr": 2.0,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": fresh_armed, "created_at": old_created,
                    "armed_at": fresh_armed, "valid_until": future,
                    "is_simulated": 0, "state": "ARMED",
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "fresh", "symbol": "FRESH", "expires_at": future,
                })
                await mem.flush_writes()

            asyncio.run(_seed())

            async def _run():
                with patch(
                    "cryptone_v45.hl_all_mids",
                    new=AsyncMock(return_value={"FRESH": 101.0}),
                ), patch(
                    "cryptone_v45.hl_candle_snapshot",
                    new=AsyncMock(return_value=[]),
                ):
                    return await c.monitor_active_signals(mem)

            self.assertEqual(asyncio.run(_run()), [])
            row = mem._conn.execute(
                "SELECT outcome FROM signal_history WHERE signal_id='fresh'"
            ).fetchone()
            self.assertIsNone(row["outcome"])
            self.assertEqual(mem.count_active_signals(), 1)
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

    def test_loss_circuit_breakers_setup_and_global(self):
        import asyncio
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=1)).isoformat()

            async def _seed_and_resolve(sid, setup):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sid, "symbol": sid, "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100, "entry_zone_high": 100,
                    "stop_loss": 90, "tp1": 110, "tp2": 120, "rr": 2,
                    "confidence": 0.6, "trigger_type": "test",
                    "reasoning": "test", "setup_type": setup,
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0,
                })
                await mem.flush_writes()
                mem.resolve_signal(sid, "LOSS", realized_rr=-1.0)

            # Three losses on one setup must pause only that setup.
            for i in range(3):
                asyncio.run(_seed_and_resolve(f"funding-{i}", "FUNDING_SQUEEZE"))
            self.assertEqual(
                mem.consecutive_losses(
                    c.SETUP_LOSS_TRIGGER, setup_type="FUNDING_SQUEEZE",
                ),
                3,
            )
            self.assertTrue(mem.setup_signals_paused("FUNDING_SQUEEZE"))
            self.assertFalse(mem.setup_signals_paused("CASCADE_SCALP"))
            self.assertFalse(mem.signals_paused())
            self.assertEqual(
                c.veto_checks(
                    mem, "NEW", 0.9, setup_type="FUNDING_SQUEEZE",
                ),
                (False, "setup-loss pause"),
            )
            self.assertEqual(
                c.veto_checks(
                    mem, "NEW", 0.9, setup_type="CASCADE_SCALP",
                ),
                (True, "ok"),
            )

            # Two more losses anywhere raise the global 6h pause.
            asyncio.run(_seed_and_resolve("cascade-0", "CASCADE_SCALP"))
            asyncio.run(_seed_and_resolve("cascade-1", "CASCADE_SCALP"))
            self.assertTrue(mem.signals_paused())
            self.assertEqual(
                c.veto_checks(
                    mem, "OTHER", 0.9, setup_type="CASCADE_SCALP",
                ),
                (False, "global-loss pause"),
            )
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


class TestStructureP1(unittest.TestCase):
    """§P1 Structure detector: swing points, BOS/CHoCH, liquidity sweep."""

    @staticmethod
    def _mk_candles(bars: list[tuple[float, float, float, float]]) -> list[c.Candle]:
        """bars: list of (open, high, low, close). bar_time increments 15m."""
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        out = []
        for i, (o, h, l, cl) in enumerate(bars):
            out.append(c.Candle(
                symbol="X", timeframe="15m", open=o, high=h, low=l, close=cl,
                volume=100.0, bar_time=base + timedelta(minutes=15 * i),
            ))
        return out

    def test_find_swing_points_uptrend(self):
        # Higher highs / higher lows pattern with clear pivots
        bars = [
            (10, 11, 9, 10.5),
            (10.5, 12, 10, 11.5),
            (11.5, 10.5, 9.5, 10),   # local low pivot
            (10, 13, 10.2, 12.8),
            (12.8, 14, 12.5, 13.5),  # local high pivot
            (13.5, 12, 11, 11.5),
            (11.5, 11.8, 10.8, 11),  # local low pivot
            (11, 14.5, 11.2, 14),
            (14, 15.5, 13.8, 15),    # local high pivot
            (15, 14, 13, 13.5),
        ]
        candles = self._mk_candles(bars)
        swings = c.find_swing_points(candles, lookback=2)
        self.assertGreaterEqual(len(swings), 2)
        kinds = {s.kind for s in swings}
        self.assertTrue(kinds.issubset({"HIGH", "LOW"}))

    def test_find_swing_points_insufficient_data(self):
        candles = self._mk_candles([(10, 11, 9, 10.5)] * 3)
        swings = c.find_swing_points(candles, lookback=2)
        self.assertEqual(swings, [])

    def test_detect_structure_events_needs_two_swings(self):
        events, trend = c.detect_structure_events([], [])
        self.assertEqual(events, [])
        self.assertEqual(trend, "RANGE")

    def test_detect_structure_events_choch_to_up(self):
        # Two prior HIGH swings (downtrend structure), then a close breaks
        # the most recent HIGH swing -> CHoCH to UP.
        bars = [(10, 10, 10, 10)] * 4 + [(10, 15, 10, 15)]
        candles = self._mk_candles(bars)
        swings = [
            c.SwingPoint(idx=0, price=14.0, kind="HIGH", bar_time=candles[0].bar_time),
            c.SwingPoint(idx=1, price=9.0, kind="LOW", bar_time=candles[1].bar_time),
            c.SwingPoint(idx=2, price=12.0, kind="HIGH", bar_time=candles[2].bar_time),
            c.SwingPoint(idx=3, price=9.5, kind="LOW", bar_time=candles[3].bar_time),
        ]
        events, trend = c.detect_structure_events(candles, swings)
        # close of bar 5 (15) breaks the last HIGH swing (12) -> bullish break
        self.assertTrue(any(e.direction == "UP" for e in events))
        self.assertEqual(trend, "UP")

    def test_detect_liquidity_sweep_bullish(self):
        # Establish a swing low at 10, then a wick below it that closes back above
        bars = [
            (11, 11.5, 10.5, 11),
            (11, 11.2, 10, 10.2),    # swing low pivot @ 10
            (10.2, 10.8, 10.1, 10.5),
            (10.5, 10.6, 10.3, 10.4),
            (10.4, 10.5, 9.0, 10.45),  # sweep: wick to 9.0, closes back at 10.45
        ]
        candles = self._mk_candles(bars)
        swings = c.find_swing_points(candles, lookback=1)
        sweep = c.detect_liquidity_sweep(candles, swings, atr=0.5, max_age_bars=3)
        if sweep is not None:
            self.assertEqual(sweep.direction, "LONG")

    def test_detect_liquidity_sweep_no_data_fail_soft(self):
        self.assertIsNone(c.detect_liquidity_sweep([], [], atr=1.0))
        self.assertIsNone(c.detect_liquidity_sweep([], [], atr=0.0))

    def test_build_structure_context_insufficient_candles(self):
        candles = self._mk_candles([(10, 11, 9, 10.5)] * 5)
        ctx = c.build_structure_context(candles, atr=1.0)
        self.assertIsNone(ctx)

    def test_build_structure_context_zero_atr(self):
        candles = self._mk_candles([(10, 11, 9, 10.5)] * 30)
        ctx = c.build_structure_context(candles, atr=0.0)
        self.assertIsNone(ctx)

    def test_build_structure_context_happy_path(self):
        import random
        random.seed(42)
        bars = []
        price = 100.0
        for i in range(40):
            o = price
            move = random.uniform(-1.5, 1.8)
            cl = o + move
            h = max(o, cl) + random.uniform(0.1, 0.5)
            l = min(o, cl) - random.uniform(0.1, 0.5)
            bars.append((o, h, l, cl))
            price = cl
        candles = self._mk_candles(bars)
        ctx = c.build_structure_context(candles, atr=1.0)
        self.assertIsNotNone(ctx)
        self.assertIn(ctx.last_trend, ("UP", "DOWN", "RANGE"))
        self.assertEqual(ctx.atr, 1.0)

    def _mk_structure_ctx(self, swings, sweep=None, atr=1.0):
        return c.StructureContext(
            swings=swings, events=[], last_trend="RANGE", sweep=sweep, atr=atr,
        )

    def test_analyst_timing_structural_no_swings_fail_soft(self):
        ctx = self._mk_structure_ctx(swings=[])
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNone(result)

    def test_analyst_timing_structural_long_swing_low(self):
        swings = [
            c.SwingPoint(idx=0, price=99.5, kind="LOW", bar_time=c.now_utc()),
            c.SwingPoint(idx=1, price=99.9, kind="HIGH", bar_time=c.now_utc()),
        ]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.sl_basis, "structure")
        self.assertEqual(result.entry_basis, "atr")  # no sweep -> price-based entry
        # SL below swing low (99.5) minus buffer
        self.assertLess(result.stop_loss, 99.5)
        self.assertGreater(result.rr, 0)

    def test_analyst_timing_structural_long_with_sweep(self):
        sweep = c.SweepEvent(
            direction="LONG", swept_level=99.5, wick_price=99.4,
            close_price=99.9, at=c.now_utc(),
        )
        swings = [c.SwingPoint(idx=0, price=99.5, kind="LOW", bar_time=c.now_utc())]
        ctx = self._mk_structure_ctx(swings, sweep=sweep, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.entry_basis, "structure")
        self.assertLess(result.stop_loss, sweep.wick_price)
        # entry zone centered around sweep close, not raw price
        self.assertLess(result.entry_low, sweep.close_price + 1.0)

    def test_structural_rr_uses_entry_mid(self):
        sweep = c.SweepEvent(
            direction="LONG", swept_level=91.0, wick_price=90.75,
            close_price=95.0, at=c.now_utc(),
        )
        ctx = self._mk_structure_ctx(
            [c.SwingPoint(idx=0, price=91.0, kind="LOW", bar_time=c.now_utc())],
            sweep=sweep,
            atr=5.0,
        )
        result = c.analyst_timing_structural(
            "LONG", 97.5, 5.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.stop_loss, 90.0, places=6)
        self.assertAlmostEqual(result.tp2, 110.0, places=6)
        self.assertAlmostEqual(result.rr, 3.0, places=6)

    def test_analyst_timing_structural_short_swing_high(self):
        swings = [
            c.SwingPoint(idx=0, price=100.5, kind="HIGH", bar_time=c.now_utc()),
            c.SwingPoint(idx=1, price=100.1, kind="LOW", bar_time=c.now_utc()),
        ]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "SHORT", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertGreater(result.stop_loss, 100.5)

    def test_analyst_timing_structural_rejects_when_sl_wrong_side(self):
        # swing LOW is ABOVE current price -> nonsensical for LONG SL, must reject
        swings = [c.SwingPoint(idx=0, price=101.0, kind="LOW", bar_time=c.now_utc())]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNone(result)

    def test_analyst_timing_structural_rejects_low_rr(self):
        # swing LOW very close to price -> tiny risk is fine (higher RR), but
        # swing LOW far below entry relative to TP mult -> RR below min must reject
        swings = [c.SwingPoint(idx=0, price=50.0, kind="LOW", bar_time=c.now_utc())]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNone(result)

    def test_analyst_timing_structural_invalid_direction(self):
        swings = [c.SwingPoint(idx=0, price=95.0, kind="LOW", bar_time=c.now_utc())]
        ctx = self._mk_structure_ctx(swings)
        result = c.analyst_timing_structural(
            "NEUTRAL", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNone(result)

    def test_analyst_timing_atr_fallback_has_basis_tags(self):
        result = c.analyst_timing("LONG", 100.0, 1.0, "INTRADAY", 1.0, 2)
        self.assertIsNotNone(result)
        self.assertEqual(result.sl_basis, "atr")
        self.assertEqual(result.entry_basis, "atr")

    def test_process_trigger_structure_direction_gates(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        asset = c.AssetCtx(
            symbol="STR", funding=0.001, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )

        async def _run(trigger, structure, current_asset=asset):
            with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
                 patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(100.0, "test"))), \
                 patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=structure)), \
                 patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
                 patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45._env_ok", return_value=False), \
                 patch("cryptone_v45.veto_checks", return_value=(True, "")):
                return await c.process_trigger(current_asset, trigger, None, None)

        short_trigger = c.TriggerHit(
            "STR", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003,
            "FUNDING_SQUEEZE",
        )
        trend_up = c.StructureContext(
            swings=[], events=[], last_trend="UP", sweep=None, atr=1.0,
        )
        self.assertIsNone(asyncio.run(_run(short_trigger, trend_up)))

        sweep_short = c.SweepEvent(
            direction="SHORT", swept_level=101.0, wick_price=101.2,
            close_price=100.0, at=c.now_utc(),
        )
        aligned = c.StructureContext(
            swings=[],
            events=[c.StructureEvent("CHoCH", "DOWN", 101.0, c.now_utc())],
            last_trend="UP", sweep=sweep_short, atr=1.0,
        )
        self.assertIsNotNone(asyncio.run(_run(short_trigger, aligned)))

        long_asset = c.AssetCtx(
            symbol="STR", funding=-0.001, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )
        long_trigger = c.TriggerHit(
            "STR", "funding_extreme", 1.0, "LONG", -0.001, -0.0003,
            "FUNDING_SQUEEZE",
        )
        self.assertIsNone(asyncio.run(_run(long_trigger, aligned, long_asset)))
        sweep_long = c.SweepEvent(
            direction="LONG", swept_level=99.0, wick_price=98.8,
            close_price=100.0, at=c.now_utc(),
        )
        aligned_long = c.StructureContext(
            swings=[],
            events=[c.StructureEvent("CHoCH", "UP", 99.0, c.now_utc())],
            last_trend="DOWN", sweep=sweep_long, atr=1.0,
        )
        self.assertIsNotNone(asyncio.run(_run(long_trigger, aligned_long, long_asset)))

        cascade = c.TriggerHit(
            "STR", "cascade", 1.0, "SHORT", 5_000_001.0, 5_000_000.0,
            "CASCADE_SCALP",
        )
        # FIX-2: SCALPING requires a same-direction BOS/CHoCH,
        # including CASCADE_SCALP.
        self.assertIsNone(asyncio.run(_run(cascade, trend_up)))

    def test_htf_bias_requires_30_bars_and_three_blocks(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        short = self._mk_candles([(10.0, 11.0, 9.0, 10.0)] * 12)

        async def _bias(candles):
            with patch("cryptone_v45.binance_klines", new=AsyncMock(return_value=candles)), \
                 patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=[])):
                return await c.htf_bias_gate("HTF", "SHORT", None)

        self.assertEqual(asyncio.run(_bias(short)), (True, "htf_short"))

        bars = []
        for block in range(3):
            for i in range(10):
                base = 100.0 + block * 10 + i * 0.1
                bars.append((base, base + 2.0, base - 1.0, base + 1.0))
        self.assertEqual(asyncio.run(_bias(self._mk_candles(bars))), (False, "htf_4h_up"))

    def test_cascade_exempt_from_htf_gate(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        asset = c.AssetCtx(
            symbol="CAS", funding=0.001, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )
        cascade = c.TriggerHit(
            "CAS", "cascade", 1.0, "SHORT", 5_000_001.0, 5_000_000.0,
            "CASCADE_SCALP",
        )
        funding = c.TriggerHit(
            "CAS", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003,
            "FUNDING_SQUEEZE",
        )

        async def _run(trigger):
            htf = AsyncMock(return_value=(False, "htf_4h_up"))
            with patch("cryptone_v45.htf_bias_gate", new=htf), \
                 patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(100.0, "test"))), \
                 patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=None)), \
                 patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
                 patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45._env_ok", return_value=False), \
                 patch("cryptone_v45.veto_checks", return_value=(True, "")):
                result = await c.process_trigger(asset, trigger, None, None)
            return result, htf

        cascade_result, cascade_htf = asyncio.run(_run(cascade))
        self.assertIsNotNone(cascade_result)
        cascade_htf.assert_not_awaited()

        funding_result, funding_htf = asyncio.run(_run(funding))
        self.assertIsNone(funding_result)
        funding_htf.assert_awaited_once()


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
        TestStructureP1,
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
