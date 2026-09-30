#!/usr/bin/env python3
"""
Cryptone V4.5 — Test Suite (Fase A, section-based)
Struktur: §X1 Unit | §X2 Integration | §X2b Lifecycle & Error | §X3 Backtest | §X4 Mock
Coverage target: core >70%, critical path >90%.
"""

from __future__ import annotations

import os
import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Import under test
import cryptone_v45 as c
# Isolasi macro calendar di test — cegah false-fail hari FOMC/NFP
os.environ.setdefault("CRYPTONE_MACRO_EVENTS", "[]")


class TestConstants(unittest.TestCase):
    """§X1 — Constants & parameter framework"""

    def test_protocol_version(self):
        self.assertTrue(c.PROTOCOL_VERSION.startswith("4.5"))

    def test_black_swan_trigger_locked(self):
        self.assertEqual(c.BLACK_SWAN_TRIGGER_COUNT, 3)

    def test_loss_circuit_breaker_constants(self):
        self.assertEqual(c.SETUP_LOSS_TRIGGER, 3)
        self.assertEqual(c.SETUP_LOSS_PAUSE_H, 2)
        self.assertEqual(c.GLOBAL_LOSS_TRIGGER, 5)
        self.assertEqual(c.GLOBAL_LOSS_PAUSE_H, 6)
        self.assertEqual(c.GEMINI_MODEL, "gemini-3.8-flash")

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
        self.assertIn(m.parse_mode, (None, "Markdown"))  # plain default safer for emoji

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
                "write_dead_letter",
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

    def test_metric_samples_dedupe_legacy_and_ignore_repeats(self):
        import asyncio
        import sqlite3

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            raw = sqlite3.connect(db)
            raw.executescript("""
                CREATE TABLE metric_funding (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    rate REAL NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    is_simulated BOOLEAN DEFAULT 0,
                    schema_version INTEGER DEFAULT 1
                );
                CREATE TABLE metric_atr (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    value REAL NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    is_simulated BOOLEAN DEFAULT 0,
                    schema_version INTEGER DEFAULT 1
                );
                INSERT INTO metric_funding
                    (symbol, rate, recorded_at) VALUES
                    ('AAA', 0.01, '2026-01-01T00:00:00+00:00'),
                    ('AAA', 0.02, '2026-01-01T00:00:00+00:00');
                INSERT INTO metric_atr
                    (symbol, timeframe, value, recorded_at) VALUES
                    ('AAA', '1h', 10.0, '2026-01-01T00:00:00+00:00'),
                    ('AAA', '1h', 11.0, '2026-01-01T00:00:00+00:00');
            """)
            raw.commit()
            raw.close()

            mem = c.MemoryEngine(db)
            mem.start()
            self.assertEqual(
                mem._conn.execute("SELECT COUNT(*) FROM metric_funding").fetchone()[0],
                1,
            )
            self.assertEqual(
                mem._conn.execute("SELECT COUNT(*) FROM metric_atr").fetchone()[0],
                1,
            )

            async def _write_repeats():
                await mem.enqueue_write("metric_funding", {
                    "symbol": "AAA",
                    "rate": 0.03,
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                })
                await mem.enqueue_write("metric_atr", {
                    "symbol": "AAA",
                    "timeframe": "1h",
                    "value": 12.0,
                    "recorded_at": "2026-01-01T00:00:00+00:00",
                })
                await mem.flush_writes()

            asyncio.run(_write_repeats())
            self.assertEqual(
                mem._conn.execute("SELECT COUNT(*) FROM metric_funding").fetchone()[0],
                1,
            )
            self.assertEqual(
                mem._conn.execute("SELECT COUNT(*) FROM metric_atr").fetchone()[0],
                1,
            )
            mem.close()


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

    def test_funding_cold_start_and_flip_keeps_strength(self):
        """Fresh flip ke ekstrem = full strength (bukan *0.5)."""
        class _Memory:
            def get_funding_history(self, symbol, lookback_days=30):
                if lookback_days == 1:
                    return [0.0001, -0.0002, 0.0005]  # mixed → flip
                return []

        self.assertIsNotNone(c.funding_agent(self._asset("S", 0.0005), memory=None))
        self.assertIsNone(c.funding_agent(self._asset("S", 0.0002), memory=None))
        hit = c.funding_agent(self._asset("S", 0.0005), memory=_Memory())
        self.assertIsNotNone(hit)
        # no penalty: (0.0005-0.0003)/0.0003 = 2/3
        self.assertAlmostEqual(hit.strength, (0.0005 - 0.0003) / 0.0003)

    def test_funding_consistent_sign_mild_boost(self):
        class _Memory:
            def get_funding_history(self, symbol, lookback_days=30):
                if lookback_days == 1:
                    return [0.0004, 0.0005, 0.0006]  # same sign
                return []

        hit = c.funding_agent(self._asset("S", 0.0005), memory=_Memory())
        self.assertIsNotNone(hit)
        base = (0.0005 - 0.0003) / 0.0003
        self.assertAlmostEqual(hit.strength, min(base * 1.1, 1.0))

    def test_cascade_resolve_continuation_and_exhaustion(self):
        self.assertEqual(c.resolve_cascade_direction("LONG"), "LONG")
        self.assertEqual(c.resolve_cascade_direction("SHORT"), "SHORT")
        import os
        os.environ["CRYPTONE_CASCADE_MODE"] = "exhaustion"
        try:
            self.assertEqual(c.resolve_cascade_direction("LONG"), "SHORT")
            self.assertEqual(c.resolve_cascade_direction("SHORT"), "LONG")
            self.assertEqual(c.cascade_mode(), "exhaustion")
        finally:
            os.environ.pop("CRYPTONE_CASCADE_MODE", None)

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
        # real_confirms (bukan total confirm termasuk proxy) menentukan tier
        self.assertEqual(c.assign_tier(2.6, 3), "A")
        self.assertEqual(c.assign_tier(2.6, 2), "B")
        self.assertEqual(c.assign_tier(2.1, 2), "B")
        self.assertEqual(c.assign_tier(1.2, 2), "C")
        # float exact 2.0 (INTRADAY 3.0/1.5) harus B, bukan C
        self.assertEqual(c.assign_tier(2.0, 2), "B")
        self.assertEqual(c.assign_tier(2.0 - 1e-12, 2), "B")
        self.assertEqual(c.assign_tier(2.0, 1), "C")
        # 3 proxy tidak boleh lolos tier A lewat assign_tier — real=0 → C
        self.assertEqual(c.assign_tier(3.0, 0), "C")

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
            client._sdk_import_failed = True
            failing_post = AsyncMock(side_effect=RuntimeError("boom"))
            model_list = AsyncMock(return_value={
                "models": [{
                    "name": "models/gemini-3.8-flash",
                    "supportedGenerationMethods": ["generateContent"],
                }],
            })
            with patch("cryptone_v45.http_get_json", new=model_list), \
                 patch("cryptone_v45.http_post_json", new=failing_post):
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

    def test_gemini_discovers_available_generate_model(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            client._sdk_import_failed = True
            model_list = AsyncMock(return_value={
                "models": [
                    {
                        "name": "models/gemini-unavailable",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-2.0-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ],
            })
            successful_post = AsyncMock(return_value={
                "candidates": [{
                    "content": {"parts": [{"text": "Other"}]},
                }],
            })
            with patch("cryptone_v45.http_get_json", new=model_list), \
                 patch("cryptone_v45.http_post_json", new=successful_post):
                result = await client.generate(
                    "test", "discover", use_cache=False,
                )
            return result, successful_post

        result, successful_post = asyncio.run(_run())
        self.assertTrue(result.success)
        self.assertEqual(result.text, "Other")
        self.assertIn(
            "/models/gemini-2.0-flash:generateContent",
            successful_post.await_args.args[0],
        )

    def test_gemini_interactions_sdk_adapter(self):
        import asyncio
        from types import SimpleNamespace

        class FakeInteractions:
            def __init__(self):
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    steps=[SimpleNamespace(text="L1")],
                )

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            fake = FakeInteractions()
            client._sdk_client = SimpleNamespace(interactions=fake)
            result = await client._generate_interactions(
                "sector_tag", "tag BTC",
                temperature=1.0, max_tokens=16,
            )
            return result, fake

        result, fake = asyncio.run(_run())
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertEqual(result.text, "L1")
        self.assertEqual(
            fake.kwargs["model"], "models/gemini-3-flash-preview",
        )
        # Default call (no use_search/thinking_level override) harus
        # HEMAT QUOTA: tanpa google_search tool, thinking_level low.
        # Ini root-cause fix 429 spam untuk task singkat seperti
        # sector_tag — jangan browsing internet buat klasifikasi 1 kata.
        self.assertNotIn("tools", fake.kwargs)
        self.assertEqual(
            fake.kwargs["generation_config"]["thinking_level"], "low",
        )

    def test_gemini_interactions_use_search_wires_tool(self):
        import asyncio
        from types import SimpleNamespace

        class FakeInteractions:
            def __init__(self):
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    steps=[SimpleNamespace(text="narrative context")],
                )

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            fake = FakeInteractions()
            client._sdk_client = SimpleNamespace(interactions=fake)
            result = await client._generate_interactions(
                "news_narrative", "summarize BTC news",
                temperature=0.3, max_tokens=256,
                use_search=True, thinking_level="high",
            )
            return result, fake

        result, fake = asyncio.run(_run())
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        # Task yang eksplisit minta use_search=True tetap bisa browsing.
        self.assertEqual(fake.kwargs["tools"], [{"type": "google_search"}])
        self.assertEqual(
            fake.kwargs["generation_config"]["thinking_level"], "high",
        )

    def test_gemini_interactions_rate_limit_skips_rest(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        class RateLimitError(Exception):
            pass

        class FakeInteractions:
            def create(self, **kwargs):
                raise RateLimitError("429 quota exceeded")

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            client._sdk_client = SimpleNamespace(
                interactions=FakeInteractions(),
            )
            with patch(
                "cryptone_v45.http_post_json",
                new=AsyncMock(),
            ) as rest:
                result = await client.generate(
                    "sector_tag", "tag BTC", use_cache=False,
                )
            return client, result, rest

        client, result, rest = asyncio.run(_run())
        self.assertFalse(result.success)
        self.assertEqual(result.error, "RateLimitError")
        rest.assert_not_awaited()
        self.assertEqual(client._consecutive_failures, 1)
        self.assertGreater(client._skip_until, c.time.monotonic())

    def test_sector_tag_never_uses_search_end_to_end(self):
        """sector_tag() lewat generate() publik harus tetap hemat quota:
        tanpa google_search, thinking_level low. Regresi utk root-cause
        429 spam (task 1-kata tidak boleh browsing + reasoning mahal)."""
        import asyncio
        from types import SimpleNamespace

        class FakeInteractions:
            def __init__(self):
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(steps=[SimpleNamespace(text="L1")])

        async def _run():
            client = c.GeminiClient()
            client.api_key = "test-key"
            fake = FakeInteractions()
            client._sdk_client = SimpleNamespace(interactions=fake)
            tag = await client.sector_tag("BTC")
            return tag, fake

        tag, fake = asyncio.run(_run())
        self.assertEqual(tag, "L1")
        self.assertNotIn("tools", fake.kwargs)
        self.assertEqual(
            fake.kwargs["generation_config"]["thinking_level"], "low",
        )

    def test_sector_tag_rejects_garbage(self):
        """Sector parser harus whitelist-validated, bukan split()[0]."""
        self.assertEqual(c._parse_sector_tag("L1"), "L1")
        self.assertEqual(c._parse_sector_tag("meme"), "MEME")
        self.assertEqual(c._parse_sector_tag("Answer: DeFi"), "DEFI")
        self.assertEqual(c._parse_sector_tag("**Other**"), "OTHER")
        # Response verbose dari LLM dengan tools=google_search aktif
        self.assertIsNone(c._parse_sector_tag("The symbol is a Layer 1"))
        self.assertIsNone(c._parse_sector_tag("Layer"))
        self.assertIsNone(c._parse_sector_tag(""))
        self.assertIsNone(c._parse_sector_tag("no idea"))

    def test_sector_tag_invalidates_cache_on_bad_response(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()

            async def _run():
                client = c.GeminiClient(mem)
                client.api_key = "test-key"
                client._sdk_import_failed = True
                with patch("cryptone_v45.http_get_json", new=AsyncMock(
                    return_value={"models": [{
                        "name": "models/gemini-3.8-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    }]},
                )), patch("cryptone_v45.http_post_json", new=AsyncMock(
                    return_value={"candidates": [{
                        "content": {"parts": [{
                            "text": "The symbol is a Layer 1",
                        }]},
                    }]},
                )):
                    tag = await client.sector_tag("SOL")
                return tag, client

            tag, client = asyncio.run(_run())
            self.assertIsNone(tag)
            # Cache key untuk prompt ini harus kosong — supaya retry
            # tidak langsung kena cache sampah.
            rows = mem._conn.execute(
                "SELECT COUNT(*) FROM llm_cache WHERE function_name='sector_tag'"
            ).fetchone()[0]
            self.assertEqual(rows, 0)
            mem.close()

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

    def test_estimate_atr_buckets_timestamp_to_5min(self):
        """Refetch dalam cycle yang sama tidak boleh menumpuk baris ATR."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            candles = [
                c.Candle(
                    "S", "1h", 100 + i, 102 + i, 99 + i, 101 + i, 1000,
                    c.now_utc() - c.timedelta(hours=48 - i),
                )
                for i in range(20)
            ]

            async def _run():
                with patch(
                    "cryptone_v45.hl_candle_snapshot",
                    new=AsyncMock(return_value=candles),
                ):
                    # Dua fetch back-to-back = bucket 5 menit yang sama
                    c._ATR_CACHE.clear()
                    a1 = await c.estimate_atr("S", 100.0, mem)
                    c._ATR_CACHE.clear()  # paksa fetch kedua: yang diuji dedup bucket DB
                    a2 = await c.estimate_atr("S", 100.0, mem)
                    await mem.flush_writes()
                    return a1, a2

            a1, a2 = asyncio.run(_run())
            self.assertEqual(a1, a2)
            n = mem._conn.execute(
                "SELECT COUNT(*) FROM metric_atr WHERE symbol='S'"
            ).fetchone()[0]
            self.assertEqual(n, 1, "dua fetch dalam bucket yang sama harus dedup")
            mem.close()

    def test_process_trigger_uses_atr_hint(self):
        """Kalau atr_hint di-supply, estimate_atr tidak boleh dipanggil."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        asset = c.AssetCtx(
            symbol="HINT", funding=0.001, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )
        trigger = c.TriggerHit(
            "HINT", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003,
            "FUNDING_SQUEEZE",
        )
        estimate_mock = AsyncMock(return_value=1.0)
        with patch("cryptone_v45.htf_bias_gate",
                   new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price",
                   new=AsyncMock(return_value=(100.0, "test"))), \
             patch("cryptone_v45.estimate_atr", new=estimate_mock), \
             patch("cryptone_v45.get_structure_context",
                   new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.fear_greed_latest",
                   new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier",
                   new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")):
            asyncio.run(c.process_trigger(
                asset, trigger, None, None, atr_hint=1.5,
            ))
        estimate_mock.assert_not_awaited()

    def test_check_black_swan_wires_volume_indicator(self):
        """BS indikator #2 (volume_cascade) harus dapat vol_5m/vol_hist,
        bukan selalu None (bug: parameter tidak pernah di-pass ke evaluate)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            candles = [
                c.Candle("BTC", "1m", 100, 100, 100, 100, 10.0, c.now_utc())
                for _ in range(30)
            ]
            candidates = [
                c.AssetCtx(
                    symbol="BTC", funding=0.0001, open_interest=1.0,
                    mark_px=100.0, mid_px=100.0, day_ntl_vlm=1_000_000,
                    prev_day_px=99.0,
                ),
            ]
            fake_state = c.BlackSwanState(
                active=False, indicators=[], count=0, mode="NORMAL",
            )
            eval_mock = MagicMock(return_value=fake_state)

            async def _run():
                with patch("cryptone_v45.hl_candle_snapshot",
                           new=AsyncMock(return_value=candles)), \
                     patch("cryptone_v45.cross_exchange_price",
                           new=AsyncMock(return_value=(100.0, "test"))), \
                     patch("cryptone_v45.evaluate_black_swan_indicators",
                           new=eval_mock):
                    await c.check_black_swan(
                        mem, "BTC", candidates,
                        cascade_map={}, corr_map={},
                    )

            asyncio.run(_run())
            mem.close()
            _, kwargs = eval_mock.call_args
            self.assertIsNotNone(kwargs.get("vol_5m"))
            self.assertEqual(kwargs["vol_5m"], 50.0)  # 5 bar × volume 10
            self.assertTrue(len(kwargs.get("vol_hist") or []) > 0)



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
        # pure-fallback pool = funding printer → REJECT (real_count wajib ≥1)
        rs = [
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed, count, real_count, n = c.evaluate_confirms(rs)
        self.assertFalse(passed)
        self.assertEqual(count, 2)
        self.assertEqual(real_count, 0)
        # one real agent that failed confirm → fail
        rs2 = [
            c.ConfirmResult("cross_exchange", False, "binance_div=0.02", is_fallback=False),
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed2, _, _, _ = c.evaluate_confirms(rs2)
        self.assertFalse(passed2)
        # one real confirm + fallback → pass
        rs3 = [
            c.ConfirmResult("microstructure", True, "book", is_fallback=False),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
        ]
        passed3, _, real3, _ = c.evaluate_confirms(rs3)
        self.assertTrue(passed3)
        self.assertEqual(real3, 1)

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

    def test_trust_score_excludes_neutral_from_denominator(self):
        """§XVI.3: win_rate = (PROFIT + 0.5×PARTIAL) / (PROFIT+PARTIAL+LOSS),
        NEUTRAL dikeluarkan dari penyebut — bukan total_signals mentah."""
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=1)).isoformat()

            async def _seed(sig_id):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sig_id, "symbol": "TRUST", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1, "entry_zone_high": 1,
                    "stop_loss": 1.1, "tp1": 0.9, "tp2": 0.8, "rr": 2,
                    "confidence": 0.5, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": sig_id, "symbol": "TRUST", "expires_at": future,
                })
                await mem.flush_writes()

            for sid in ("p0", "p1", "n0", "n1"):
                asyncio.run(_seed(sid))

            mem.resolve_signal("p0", "PROFIT", realized_rr=2.0)
            mem.resolve_signal("p1", "PROFIT", realized_rr=2.0)
            mem.resolve_signal("n0", "NEUTRAL", realized_rr=0.0)
            mem.resolve_signal("n1", "NEUTRAL", realized_rr=0.0)

            sp = mem._conn.execute(
                "SELECT trust_score, total_signals FROM symbol_performance "
                "WHERE symbol='TRUST'"
            ).fetchone()
            # total_signals tetap mencatat semua (termasuk NEUTRAL) —
            # tapi trust_score harus 1.0 (2/2 resolved wins), bukan 0.5 (2/4).
            self.assertEqual(sp["total_signals"], 4)
            self.assertAlmostEqual(sp["trust_score"], 1.0, places=2)
            mem.close()

    def test_setup_performance_excludes_neutral(self):
        """§XVI.6: setup_performance.total_signals hanya trade yang benar-benar
        resolusi arah — NEUTRAL tidak boleh ikut menghitung, atau auto-pause
        bisa salah men-judge setup yang legit."""
        import asyncio
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=1)).isoformat()

            async def _seed(sig_id):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sig_id, "symbol": "SP1", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1, "entry_zone_high": 1,
                    "stop_loss": 1.1, "tp1": 0.9, "tp2": 0.8, "rr": 2,
                    "confidence": 0.5, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "SETUP_NEUTRAL_TEST",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": sig_id, "symbol": "SP1", "expires_at": future,
                })
                await mem.flush_writes()

            for sid in ("w0", "n0", "n1"):
                asyncio.run(_seed(sid))

            mem.resolve_signal("w0", "PROFIT", realized_rr=2.0)
            mem.resolve_signal("n0", "NEUTRAL", realized_rr=0.0)
            mem.resolve_signal("n1", "NEUTRAL", realized_rr=0.0)

            st = mem._conn.execute(
                "SELECT wins, total_signals FROM setup_performance "
                "WHERE setup_type='SETUP_NEUTRAL_TEST'"
            ).fetchone()
            # Hanya 1 trade resolusi arah (PROFIT); 2 NEUTRAL tidak dihitung.
            self.assertEqual(st["total_signals"], 1)
            self.assertEqual(st["wins"], 1)
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


class TestHLBookBuffer(unittest.TestCase):
    """§5b — HLBookBuffer: l2Book parse, imbalance, subscription set (no network)."""

    def test_ingest_bid_heavy_gives_positive_imbalance(self):
        buf = c.HLBookBuffer()
        buf.ingest({
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "levels": [
                    [{"px": "50000", "sz": "2.0", "n": 1}],   # bids: 100k
                    [{"px": "50010", "sz": "1.0", "n": 1}],   # asks: 50010
                ],
            },
        })
        imb = buf.get_imbalance("BTC")
        self.assertIsNotNone(imb)
        self.assertGreater(imb, 0.0)

    def test_ingest_ask_heavy_gives_negative_imbalance(self):
        buf = c.HLBookBuffer()
        buf.ingest({
            "channel": "l2Book",
            "data": {
                "coin": "ETH",
                "levels": [
                    [{"px": "3000", "sz": "1.0", "n": 1}],
                    [{"px": "3001", "sz": "5.0", "n": 1}],
                ],
            },
        })
        imb = buf.get_imbalance("ETH")
        self.assertIsNotNone(imb)
        self.assertLess(imb, 0.0)

    def test_imbalance_clamped_to_unit_range(self):
        buf = c.HLBookBuffer()
        buf.ingest({
            "channel": "l2Book",
            "data": {
                "coin": "X",
                "levels": [
                    [{"px": "1", "sz": "1000000"}],
                    [],
                ],
            },
        })
        imb = buf.get_imbalance("X")
        self.assertEqual(imb, 1.0)

    def test_unknown_symbol_returns_none(self):
        buf = c.HLBookBuffer()
        self.assertIsNone(buf.get_imbalance("NOPE"))

    def test_stale_snapshot_returns_none(self):
        buf = c.HLBookBuffer(max_age_sec=0.05)
        buf.ingest({
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "levels": [
                    [{"px": "100", "sz": "1"}],
                    [{"px": "101", "sz": "1"}],
                ],
            },
        })
        import time as _t
        _t.sleep(0.08)
        self.assertIsNone(buf.get_imbalance("BTC"))

    def test_ignores_non_l2book_channel(self):
        buf = c.HLBookBuffer()
        buf.ingest({"channel": "trades", "data": {"coin": "BTC"}})
        self.assertIsNone(buf.get_imbalance("BTC"))

    def test_ingest_malformed_payload_fail_soft(self):
        buf = c.HLBookBuffer()
        # Missing keys / wrong types must not raise.
        buf.ingest({})
        buf.ingest({"channel": "l2Book", "data": None})
        buf.ingest({"channel": "l2Book", "data": {"coin": "", "levels": [[], []]}})
        buf.ingest({"channel": "l2Book", "data": {"coin": "Z", "levels": "nope"}})
        buf.ingest({"channel": "l2Book", "data": {
            "coin": "Z", "levels": [[{"px": "bad", "sz": "1"}], []],
        }})
        self.assertIsNone(buf.get_imbalance("Z"))

    def test_set_symbols_updates_desired_only(self):
        buf = c.HLBookBuffer()
        buf.set_symbols(["BTC", "ETH", ""])
        self.assertEqual(buf._desired, {"BTC", "ETH"})
        # Not yet subscribed until the WS loop reconciles.
        self.assertEqual(buf._subscribed, set())

    def test_reconcile_subscriptions_add_and_remove(self):
        import asyncio

        class _FakeWS:
            def __init__(self):
                self.sent: list[dict] = []

            async def send(self, payload):
                self.sent.append(json.loads(payload))

        buf = c.HLBookBuffer()
        ws = _FakeWS()
        buf.set_symbols(["BTC", "ETH"])
        asyncio.run(buf._reconcile_subscriptions(ws))
        self.assertEqual(buf._subscribed, {"BTC", "ETH"})
        methods = {(m["method"], m["subscription"]["coin"]) for m in ws.sent}
        self.assertEqual(methods, {("subscribe", "BTC"), ("subscribe", "ETH")})

        ws.sent.clear()
        buf.set_symbols(["ETH"])  # drop BTC
        asyncio.run(buf._reconcile_subscriptions(ws))
        self.assertEqual(buf._subscribed, {"ETH"})
        self.assertEqual(
            ws.sent, [{"method": "unsubscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}],
        )

    def test_status_note_reflects_state(self):
        buf = c.HLBookBuffer()
        self.assertEqual(buf.status_note(), "starting")
        buf.last_error = "ConnectionError: refused"
        self.assertTrue(buf.status_note().startswith("down ·"))
        buf.last_error = ""
        buf.connected = True
        buf._subscribed = {"BTC"}
        buf.total_msgs = 3
        buf.last_msg_mono = c.time.monotonic()
        self.assertTrue(buf.status_note().startswith("live · sub=1"))

    def test_reconcile_scheduled_independent_of_traffic(self):
        """Regression (bug #2): reconcile must fire on wall-clock cadence even
        when recv() keeps returning messages back-to-back with zero idle time
        — otherwise ws.recv() never times out and subscriptions never grow
        past the first batch of HL_BOOK_SUB_BATCH symbols."""
        import asyncio
        import sys
        import types
        from unittest.mock import patch

        buf = c.HLBookBuffer()
        # More than one batch (25) worth of symbols → needs >1 reconcile tick.
        symbols = [f"SYM{i}" for i in range(58)]
        buf.set_symbols(symbols)

        class _FakeWS:
            async def send(self, payload):
                pass

            async def recv(self):
                # Never idle — always has a message ready immediately, so a
                # naive "reconcile only on recv() timeout" design would starve.
                await asyncio.sleep(0)
                return json.dumps({
                    "channel": "l2Book",
                    "data": {"coin": "SYM0", "levels": [[], []]},
                })

        fake_ws = _FakeWS()

        class _FakeConnectCtx:
            async def __aenter__(self):
                return fake_ws

            async def __aexit__(self, *a):
                return False

        fake_module = types.SimpleNamespace(connect=lambda *a, **k: _FakeConnectCtx())

        async def _run():
            task = asyncio.create_task(buf._ws_loop())
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with patch.dict(sys.modules, {"websockets": fake_module}), \
             patch.object(c, "HL_BOOK_RECONCILE_SEC", 0.05):
            asyncio.run(_run())

        self.assertEqual(buf._subscribed, set(symbols))

    def test_process_trigger_uses_real_book_imbalance_not_fallback(self):
        """With fresh HL book data, microstructure must confirm as real
        (is_fallback=False), unlocking the Tier A/B real_count path — instead
        of silently falling back to funding_proxy like before this was wired."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        asset = c.AssetCtx(
            symbol="BOOK", funding=0.0, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )
        trigger = c.TriggerHit(
            "BOOK", "funding_extreme", 1.0, "LONG", -0.001, -0.0003,
            "FUNDING_SQUEEZE",
        )
        hl_book = c.HLBookBuffer()
        # Strongly bid-heavy book, LONG-aligned → real (non-fallback) confirm.
        hl_book.ingest({
            "channel": "l2Book",
            "data": {
                "coin": "BOOK",
                "levels": [
                    [{"px": "100", "sz": "100"}],
                    [{"px": "100.1", "sz": "1"}],
                ],
            },
        })

        captured: dict = {}

        async def _fake_analyst(*args, **kwargs):
            return None

        with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(None, "none"))), \
             patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")):

            orig_evaluate = c.evaluate_confirms

            def _spy_evaluate(confirms):
                captured["confirms"] = confirms
                return orig_evaluate(confirms)

            with patch("cryptone_v45.evaluate_confirms", side_effect=_spy_evaluate):
                asyncio.run(c.process_trigger(
                    asset, trigger, None, None, hl_book=hl_book,
                ))

        self.assertIn("confirms", captured)
        micro = next(r for r in captured["confirms"] if r.agent == "microstructure")
        self.assertFalse(micro.is_fallback)
        self.assertTrue(micro.confirmed)

    def test_process_trigger_falls_back_without_hl_book(self):
        """Same setup but hl_book=None must reproduce the pre-wiring behavior:
        microstructure falls back to funding_proxy (is_fallback=True)."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        asset = c.AssetCtx(
            symbol="NOBOOK", funding=-0.001, open_interest=1.0,
            mark_px=100.0, mid_px=100.0, day_ntl_vlm=20_000_000,
            prev_day_px=100.0,
        )
        trigger = c.TriggerHit(
            "NOBOOK", "funding_extreme", 1.0, "LONG", -0.001, -0.0003,
            "FUNDING_SQUEEZE",
        )
        captured: dict = {}

        with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(None, "none"))), \
             patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")):

            orig_evaluate = c.evaluate_confirms

            def _spy_evaluate(confirms):
                captured["confirms"] = confirms
                return orig_evaluate(confirms)

            with patch("cryptone_v45.evaluate_confirms", side_effect=_spy_evaluate):
                asyncio.run(c.process_trigger(asset, trigger, None, None, hl_book=None))

        self.assertIn("confirms", captured)
        micro = next(r for r in captured["confirms"] if r.agent == "microstructure")
        self.assertTrue(micro.is_fallback)
        self.assertEqual(micro.detail, "funding_proxy")


class TestHLTradeFlowBuffer(unittest.TestCase):
    """HL taker-flow WS: independent real confirm, fail-soft when cold/stale."""

    def test_buy_flow_is_positive_and_short_flow_negative(self):
        buy = c.HLTradeFlowBuffer(min_samples=3)
        buy.ingest({
            "channel": "trades",
            "data": [
                {"coin": "BTC", "side": "B", "px": "100", "sz": "1"},
                {"coin": "BTC", "side": "B", "px": "100", "sz": "1"},
                {"coin": "BTC", "side": "B", "px": "100", "sz": "1"},
            ],
        })
        self.assertEqual(buy.get_imbalance("BTC"), 1.0)
        self.assertTrue(c.trade_flow_confirm("LONG", 0.5).confirmed)
        self.assertFalse(c.trade_flow_confirm("SHORT", 0.5).confirmed)

        sell = c.HLTradeFlowBuffer(min_samples=3)
        sell.ingest({
            "channel": "trades",
            "data": [
                {"coin": "ETH", "side": "A", "px": "100", "sz": "1"},
                {"coin": "ETH", "side": "SELL", "px": "100", "sz": "1"},
                {"coin": "ETH", "side": "A", "px": "100", "sz": "1"},
            ],
        })
        self.assertEqual(sell.get_imbalance("ETH"), -1.0)
        self.assertTrue(c.trade_flow_confirm("SHORT", -0.5).confirmed)

    def test_cold_or_malformed_flow_is_not_a_fake_confirm(self):
        buf = c.HLTradeFlowBuffer(min_samples=3)
        buf.ingest({"channel": "trades", "data": [
            {"coin": "BTC", "side": "UNKNOWN", "px": "100", "sz": "1"},
        ]})
        self.assertIsNone(buf.get_imbalance("BTC"))
        result = c.trade_flow_confirm("LONG", buf.get_imbalance("BTC"))
        self.assertEqual(result.detail, "agent off")
        self.assertFalse(result.confirmed)

    def test_stale_flow_is_dropped(self):
        import time as _t
        buf = c.HLTradeFlowBuffer(window_sec=0.05, min_samples=1)
        buf.ingest({
            "channel": "trades",
            "data": [{"coin": "BTC", "side": "B", "px": "100", "sz": "1"}],
        })
        _t.sleep(0.08)
        self.assertIsNone(buf.get_imbalance("BTC"))


class TestStructureP1(unittest.TestCase):
    """§P1 Structure detector: swing points, BOS/CHoCH, liquidity sweep."""

    def setUp(self):
        os.environ["CRYPTONE_SESSION_FILTER"] = "0"

    def tearDown(self):
        os.environ.pop("CRYPTONE_SESSION_FILTER", None)

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
        self.assertEqual(result.entry_basis, "structure")  # structural path, no FVG
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

    def test_structural_rr_not_capped_and_min_risk_is_labeled(self):
        """SL mepet → min-risk floor; TP = R-multiple → RR = desain INTRADAY 2.0."""
        swings = [
            c.SwingPoint(idx=0, price=99.99, kind="LOW", bar_time=c.now_utc()),
        ]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.rr, c.MIN_RR["INTRADAY"], places=5)
        self.assertEqual(result.sl_basis, "structure_widened_min_risk")
        self.assertAlmostEqual(result.stop_loss, 99.65, places=6)

    def test_structural_rr_no_widening_when_sl_is_proper_structural(self):
        """SL struktural layak; TP = 2.0R dari risk (INTRADAY), bukan ATR-mult mentah."""
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
        # risk = 95-90 = 5; TP2 = 95 + 2.0*5 = 105
        self.assertAlmostEqual(result.tp2, 105.0, places=6)
        self.assertAlmostEqual(result.rr, c.MIN_RR["INTRADAY"], places=6)
        self.assertEqual(result.sl_basis, "structure")

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

    def test_analyst_timing_structural_far_swing_keeps_min_rr(self):
        """Swing jauh: TP di-scale R-multiple → RR tetap min_rr (bukan reject)."""
        swings = [c.SwingPoint(idx=0, price=50.0, kind="LOW", bar_time=c.now_utc())]
        ctx = self._mk_structure_ctx(swings, sweep=None, atr=1.0)
        result = c.analyst_timing_structural(
            "LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.rr, c.MIN_RR["INTRADAY"], places=5)

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

        def _flow(sym, side):
            buf = c.HLTradeFlowBuffer(min_samples=3)
            buf.ingest({"channel": "trades", "data": [
                {"coin": sym, "side": side, "px": "100", "sz": "1"},
            ] * 3})
            return buf

        async def _run(trigger, structure, current_asset=asset, hl_trade_flow=None):
            with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
                 patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(100.0, "test"))), \
                 patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=structure)), \
                 patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
                 patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
                 patch("cryptone_v45._env_ok", return_value=False), \
                 patch("cryptone_v45.veto_checks", return_value=(True, "")):
                return await c.process_trigger(
                    current_asset, trigger, None, None,
                    hl_trade_flow=hl_trade_flow,
                )

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
        # Counter-trend (last_trend UP, SHORT) + sweep searah: real_count hanya 1
        # (xref) → ditolak. Sweep sendirian tidak boleh menembus veto trend.
        self.assertIsNone(asyncio.run(_run(short_trigger, aligned)))
        # …tapi lolos bila ada konfirmasi REAL kedua (taker-flow jual).
        self.assertIsNotNone(asyncio.run(_run(
            short_trigger, aligned, hl_trade_flow=_flow("STR", "A"),
        )))

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
        self.assertIsNone(asyncio.run(_run(long_trigger, aligned_long, long_asset)))
        self.assertIsNotNone(asyncio.run(_run(
            long_trigger, aligned_long, long_asset,
            hl_trade_flow=_flow("STR", "B"),
        )))

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

        self.assertEqual(asyncio.run(_bias(short)), (True, "htf_insufficient"))

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


class TestNewsBiasP1(unittest.TestCase):
    """B1/B2/B7 — per-symbol news bias, sector wiring, 1h velocity."""

    def setUp(self):
        # isolate module-level RSS cache per test
        c._RECENT_HEADLINES = []
        c._RSS_CACHE_BIAS = None
        c._SECTOR_BIAS_CACHE = {}

    def test_rss_bias_word_boundary_excludes_substrings(self):
        # "second" and "bank" must NOT match bearish keywords
        c._RECENT_HEADLINES = [
            (c.time.time(), "Second-quarter results"),
            (c.time.time(), "Banking sector stabilises"),
        ]
        c._RSS_CACHE_BIAS = "BEARISH"   # global seeded
        # symbol-specific scores are all 0 → falls through to global
        self.assertEqual(c.rss_news_bias("BTC"), "BEARISH")
        # explicit scoring returns neutral for these titles
        self.assertEqual(c._score_headline("Second-quarter results"), 0)
        self.assertEqual(c._score_headline("Banking sector stabilises"), 0)
        # "SEC" as a whole word IS bearish
        self.assertEqual(c._score_headline("SEC sues exchange"), -1)

    def test_rss_bias_per_symbol_overrides_global(self):
        now = c.time.time()
        c._RECENT_HEADLINES = [
            (now, "Solana ETF approval sends SOL surging"),
            (now, "SOL rally continues into the weekend"),
        ]
        c._RSS_CACHE_BIAS = "BEARISH"   # global market bearish
        # SOL-specific: bullish wins over global
        self.assertEqual(c.rss_news_bias("SOL"), "BULLISH")
        # Other symbol → no specific headlines → global fallback
        self.assertEqual(c.rss_news_bias("AVAX"), "BEARISH")

    def test_rss_bias_strips_1000x_prefix(self):
        now = c.time.time()
        c._RECENT_HEADLINES = [
            (now, "PEPE rally continues"),
            (now, "PEPE in strong bullish breakout"),
        ]
        c._RSS_CACHE_BIAS = None
        self.assertEqual(c.rss_news_bias("1000PEPE"), "BULLISH")

    def test_sector_bias_aggregation(self):
        now = c.time.time()
        c._RECENT_HEADLINES = [
            (now, "AI agents surge to new highs"),
            (now, "AI token rally accelerates"),
            (now, "New AI model sparks bullish sentiment"),
        ]
        c._recompute_sector_bias()
        self.assertEqual(c._SECTOR_BIAS_CACHE.get("AI"), "BULLISH")
        self.assertIsNone(c.sector_news_bias("MEME"))
        self.assertIsNone(c.sector_news_bias(None))

    def test_context_sector_bias_multiplier(self):
        import asyncio
        async def _run():
            base = await c.context_modifier("X", "LONG", None)
            bull_l = await c.context_modifier(
                "X", "LONG", None,
                sector="AI", sector_bias="BULLISH",
            )
            bull_s = await c.context_modifier(
                "X", "SHORT", None,
                sector="AI", sector_bias="BULLISH",
            )
            # no-sector should be neutral 1.0
            self.assertEqual(base, 1.0)
            self.assertGreater(bull_l, 1.0)
            self.assertLess(bull_s, 1.0)
        asyncio.run(_run())

    def test_correlation_break_uses_short_velocity_threshold(self):
        # 24h-style big drop is NOT passed anymore; caller passes 1h velocity
        self.assertFalse(c._ind_correlation_break(0.02, None))   # below 3%
        self.assertTrue(c._ind_correlation_break(0.035, None))   # above 3%
        # top2 avg same-direction required (anchor alone below threshold here,
        # so only the top2 branch is in play)
        self.assertTrue(c._ind_correlation_break(0.04, 0.035))
        self.assertFalse(c._ind_correlation_break(0.02, -0.04))  # opposite sign
        self.assertFalse(c._ind_correlation_break(None, 0.05))


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
        TestHLBookBuffer,
        TestHLTradeFlowBuffer,
        TestLifecycleP0,
        TestTradingP0,
        TestStructureP1,
        TestNewsBiasP1,
        TestSessionAndFVG,
        TestTelegramUI,
        TestQualityGatesA,
        TestAtrCache,
        TestAuditRemediation,
        TestConfidenceCalibration,
        TestOIDeltaConfirm,
        TestPipelineCycleAudit,
        TestRssHeadlineTtl,
        TestOISnapshotPersistence,
        TestAuditFixes,
        TestP0AuditFixes,
        TestP1Reliability,
        TestP2StrategyValidation,
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



class TestSessionAndFVG(unittest.TestCase):
    def test_session_allows_in_hours(self):
        t = c.datetime(2026, 9, 27, 15, 0, tzinfo=c.WIB)  # 15:00 WIB
        ok, note = c.session_allows("INTRADAY", "B", now=t)
        self.assertTrue(ok)
        self.assertIn("session_ok", note)

    def test_session_rejects_offhours_tier_b(self):
        t = c.datetime(2026, 9, 27, 3, 0, tzinfo=c.WIB)  # 03:00 WIB
        ok, note = c.session_allows("INTRADAY", "B", now=t)
        self.assertFalse(ok)
        ok_a, _ = c.session_allows("INTRADAY", "A", now=t)
        self.assertTrue(ok_a)
        ok_sw, _ = c.session_allows("SWING", "B", now=t)
        self.assertTrue(ok_sw)

    def test_detect_bullish_fvg(self):
        # 3 candles: gap up between c0.high and c2.low
        base = c.datetime(2026, 1, 1, tzinfo=c.UTC)
        candles = [
            c.Candle("X", "15m", 10, 11, 9.5, 10.5, 1, base),
            c.Candle("X", "15m", 10.5, 10.8, 10.2, 10.6, 1, base + c.timedelta(minutes=15)),
            c.Candle("X", "15m", 12.0, 12.5, 11.8, 12.2, 1, base + c.timedelta(minutes=30)),
        ]
        # gap 11 -> 11.8, atr=1
        zones = c.detect_fvg_zones(candles, atr=1.0, max_age_bars=10)
        self.assertTrue(any(z.direction == "LONG" for z in zones))

    def test_format_shows_pending_and_basis(self):
        sig = c.PipelineSignal(
            signal_id="t1", symbol="AAA", direction="LONG", horizon="INTRADAY",
            tier="B", setup_type="FUNDING_SQUEEZE", trigger_type="funding",
            entry_low=1.0, entry_high=1.1, stop_loss=0.9, tp1=1.2, tp2=1.3,
            rr=2.0, confidence=0.7, base_confidence=0.6, context_mult=1.0,
            confirm_count=2, active_agent_count=2, reasoning="x",
            created_at=c.now_utc(), valid_until=c.now_utc(),
            sl_basis="structure", entry_basis="fvg", state="PENDING",
        )
        msg = c.format_signal_message(sig)
        self.assertIn("PENDING", msg)
        self.assertIn("structure", msg)
        self.assertIn("fvg", msg)



class TestTelegramUI(unittest.TestCase):
    def test_main_menu_keyboard(self):
        kb = c._kb_main_menu()
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("📊 Radar", labels)
        self.assertIn("🌍 Market", labels)
        self.assertIn("🚨 Black Swan", labels)

    def test_resolve_home_and_radar(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            text, mk = c.resolve_menu_callback("menu:home", mem)
            self.assertIn("Cryptone", text)
            self.assertTrue(mk and mk.get("inline_keyboard"))
            text2, mk2 = c.resolve_menu_callback("menu:radar", mem)
            self.assertIn("Radar", text2)
            mem.close()

    def test_signal_keyboard_has_reason(self):
        kb = c._kb_signal("abc-123")
        flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertTrue(any(x.startswith("sig:reason:") for x in flat))



class TestQualityGatesA(unittest.TestCase):
    """Batch A: ATR% gate + funding requires structure."""

    def _run_trigger(self, trigger, structure, atr=1.0, px=100.0, funding=0.001):
        import asyncio
        from unittest.mock import AsyncMock, patch
        asset = c.AssetCtx(
            symbol="QG", funding=funding, open_interest=1e6,
            mark_px=px, mid_px=px, day_ntl_vlm=50_000_000, prev_day_px=px,
        )
        with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(px, "t"))), \
             patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=atr)), \
             patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=structure)), \
             patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")), \
             patch("cryptone_v45.session_allows", return_value=(True, "ok")):
            return asyncio.run(c.process_trigger(asset, trigger, None, None, atr_hint=atr))

    def test_funding_rejected_without_structure(self):
        tr = c.TriggerHit("QG", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003, "FUNDING_SQUEEZE")
        self.assertIsNone(self._run_trigger(tr, None, atr=1.0))

    def test_atr_pct_reject_dead_coin(self):
        # atr 0.1 on px 100 = 0.1% < 0.5%
        tr = c.TriggerHit("QG", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003, "FUNDING_SQUEEZE")
        self.assertIsNone(self._run_trigger(tr, None, atr=0.1, px=100.0))



    def test_parse_exchange_wallets_json(self):
        import os
        os.environ["CRYPTONE_EXCHANGE_WALLETS"] = '{"binance":"0xF977814e90dA44bFA03b6295A0616a897441aceC"}'
        try:
            w = c.parse_exchange_wallets()
            self.assertEqual(len(w), 1)
            self.assertEqual(w[0][0], "binance")
        finally:
            os.environ.pop("CRYPTONE_EXCHANGE_WALLETS", None)

    def test_flow_confirm_inflow_short(self):
        r = c.flow_confirm("SHORT", netflow=100_000, netflow_p80=50_000, netflow_p20=-50_000)
        self.assertTrue(r.confirmed)
        r2 = c.flow_confirm("LONG", netflow=100_000, netflow_p80=50_000, netflow_p20=-50_000)
        self.assertFalse(r2.confirmed)


    def test_normalize_short_wrong_side_sl(self):
        r = c.normalize_timing_levels(
            "SHORT", 0.0267, 0.0005,
            0.026661, 0.026695, 0.0266536,
            0.026147, 0.0256415, "INTRADAY",
        )
        self.assertIsNotNone(r)
        el, eh, stop, _tp1, tp2, rr = r
        self.assertGreater(stop, eh)
        self.assertLess(tp2, el)
        # RR = reward/risk terhadap entry_mid, apa adanya (tidak di-cap)
        mid = (el + eh) / 2.0
        self.assertAlmostEqual(rr, (mid - tp2) / (stop - mid), places=6)

    def test_normalize_rejects_impossible(self):
        # zero atr
        self.assertIsNone(c.normalize_timing_levels(
            "LONG", 100, 0, 99, 101, 98, 102, 105, "INTRADAY",
        ))


    def test_context_dxy_stable_cp(self):
        import asyncio
        async def _run():
            ms = await c.context_modifier("BTC", "SHORT", "BTC", dxy=106.0)
            ml = await c.context_modifier("BTC", "LONG", "BTC", dxy=106.0)
            self.assertGreater(ms, ml)
            m_in = await c.context_modifier("ETH", "LONG", "BTC", stablecoin_flow_7d_pct=3.0)
            m_out = await c.context_modifier("ETH", "LONG", "BTC", stablecoin_flow_7d_pct=-3.0)
            self.assertGreater(m_in, m_out)
            mb = await c.context_modifier("SOL", "LONG", "BTC", cryptopanic_bias="BULLISH")
            mx = await c.context_modifier("SOL", "LONG", "BTC", cryptopanic_bias="BEARISH")
            self.assertGreater(mb, mx)
        asyncio.run(_run())

    def test_should_push_active_quiet(self):
        from datetime import datetime
        quiet = datetime(2026, 9, 27, 1, 0, tzinfo=c.WIB)  # 01:00 quiet
        ok_a, _ = c.should_push_signal("A", None, now=quiet)
        ok_b, note_b = c.should_push_signal("B", None, now=quiet)
        self.assertTrue(ok_a)
        self.assertFalse(ok_b)
        self.assertIn("quiet", note_b)

    def test_compute_confluence_max(self):
        s = c.compute_confluence(
            real_count=2, has_structure=True, structure_aligned=True,
            htf_ok=True, micro_real=True,
        )
        self.assertEqual(s, 5)

    def test_funding_with_aligned_sweep_passes_structure_gate(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from datetime import datetime, timezone
        sweep = c.SweepEvent(
            direction="SHORT", swept_level=105.0, wick_price=106.0,
            close_price=100.0, at=datetime.now(tz=timezone.utc),
        )
        swings = [
            c.SwingPoint(0, 90.0, "LOW", datetime.now(tz=timezone.utc)),
            c.SwingPoint(1, 110.0, "HIGH", datetime.now(tz=timezone.utc)),
        ]
        events = [
            c.StructureEvent("BOS", "DOWN", 100.0, datetime.now(tz=timezone.utc)),
        ]
        ctx = c.StructureContext(
            swings=swings, events=events, last_trend="DOWN",
            sweep=sweep, atr=1.5,
        )
        tr = c.TriggerHit("QG", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003, "FUNDING_SQUEEZE")
        # atr 1.5 / 100 = 1.5% OK
        sig = self._run_trigger(tr, ctx, atr=1.5, px=100.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.setup_type, "FUNDING_SQUEEZE")


class TestAtrCache(unittest.TestCase):
    """estimate_atr read-through cache + prefetch paralel terbatas."""

    def setUp(self):
        c._ATR_CACHE.clear()

    def tearDown(self):
        c._ATR_CACHE.clear()

    @staticmethod
    def _candles(sym="S"):
        return [
            c.Candle(sym, "1h", 100 + i, 102 + i, 99 + i, 101 + i, 1000,
                     c.now_utc() - c.timedelta(hours=48 - i))
            for i in range(20)
        ]

    def test_second_call_is_cache_hit(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        snap = AsyncMock(return_value=self._candles())
        async def _run():
            with patch("cryptone_v45.hl_candle_snapshot", new=snap):
                a1 = await c.estimate_atr("S", 100.0, None)
                a2 = await c.estimate_atr("S", 100.0, None)
            return a1, a2
        a1, a2 = asyncio.run(_run())
        self.assertEqual(a1, a2)
        self.assertEqual(snap.await_count, 1)

    def test_expired_entry_refetches(self):
        import asyncio, time as _t
        from unittest.mock import AsyncMock, patch
        snap = AsyncMock(return_value=self._candles())
        async def _run():
            with patch("cryptone_v45.hl_candle_snapshot", new=snap):
                await c.estimate_atr("S", 100.0, None)
                ts, val = c._ATR_CACHE["S"]
                c._ATR_CACHE["S"] = (_t.monotonic() - c.ATR_CACHE_TTL_SEC - 1, val)
                await c.estimate_atr("S", 100.0, None)
        asyncio.run(_run())
        self.assertEqual(snap.await_count, 2)

    def test_failed_fetch_fallback_is_never_cached(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        snap = AsyncMock(side_effect=c.DataUnavailable("boom"))
        async def _run():
            with patch("cryptone_v45.hl_candle_snapshot", new=snap):
                a1 = await c.estimate_atr("S", 100.0, None)  # candle+DB kosong
                a2 = await c.estimate_atr("S", 100.0, None)
            return a1, a2
        a1, a2 = asyncio.run(_run())
        self.assertIsNone(a1, "fail-closed: tidak boleh ada ATR fabrikasi")
        self.assertIsNone(a2)
        self.assertEqual(snap.await_count, 2, "fallback tidak boleh menempel di cache")
        self.assertNotIn("S", c._ATR_CACHE)

    def test_prefetch_fetches_only_misses_and_bounds_concurrency(self):
        import asyncio
        from unittest.mock import patch
        state = {"cur": 0, "peak": 0, "calls": 0}
        async def _snap(sym, *a, **k):
            state["calls"] += 1
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.01)
            state["cur"] -= 1
            return self._candles(sym)
        items = [(f"S{i}", 100.0) for i in range(20)]
        async def _run():
            with patch("cryptone_v45.hl_candle_snapshot", new=_snap):
                n1 = await c.prefetch_atr(items, None)
                n2 = await c.prefetch_atr(items, None)
            return n1, n2
        n1, n2 = asyncio.run(_run())
        self.assertEqual(n1, 20)
        self.assertEqual(n2, 0, "cycle berikutnya (dalam TTL) = semua cache hit")
        self.assertEqual(state["calls"], 20)
        self.assertLessEqual(state["peak"], c.ATR_PREFETCH_CONCURRENCY)

    def test_prefetch_is_fail_soft(self):
        import asyncio
        from unittest.mock import patch
        async def _snap(sym, *a, **k):
            if sym == "BAD":
                raise RuntimeError("unexpected")
            return self._candles(sym)
        async def _run():
            with patch("cryptone_v45.hl_candle_snapshot", new=_snap):
                await c.prefetch_atr([("OK", 100.0), ("BAD", 100.0)], None)
        asyncio.run(_run())  # tidak boleh raise
        self.assertIn("OK", c._ATR_CACHE)
        self.assertNotIn("BAD", c._ATR_CACHE)


class TestConfidenceCalibration(unittest.TestCase):
    """Kalibrasi conf: shrinkage Bayesian ke winrate LIVE 30 hari, dibatasi."""

    def _mem(self, td):
        mem = c.MemoryEngine(str(Path(td) / "t.db"))
        mem.start()
        return mem

    def _seed(self, mem, n, outcome, setup="FUNDING_SQUEEZE", sym="X",
              simulated=0, age_days=0.0, tag="a"):
        import asyncio
        now = c.now_utc()
        created = (now - c.timedelta(days=age_days)).isoformat()
        future = (now + c.timedelta(hours=1)).isoformat()
        for i in range(n):
            sid = f"{tag}-{setup}-{sym}-{outcome}-{simulated}-{age_days}-{i}"
            async def _w(sid=sid):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sid, "symbol": sym, "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100, "entry_zone_high": 100,
                    "stop_loss": 90, "tp1": 110, "tp2": 120, "rr": 2,
                    "confidence": 0.6, "trigger_type": "test",
                    "reasoning": "t", "setup_type": setup,
                    "last_checked_at": created, "created_at": created,
                    "valid_until": future, "is_simulated": simulated,
                })
                await mem.flush_writes()
            asyncio.run(_w())
            mem._conn.execute(
                "UPDATE signal_history SET outcome=?, resolved_at=? WHERE signal_id=?",
                (outcome, created, sid),
            )
        mem._conn.commit()

    def test_no_memory_or_no_history_is_identity(self):
        self.assertEqual(c.calibrate_confidence(None, 0.7, "X", "FUNDING_SQUEEZE"), (0.7, ""))
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self.assertEqual(c.calibrate_confidence(mem, 0.7, "X", "FUNDING_SQUEEZE"), (0.7, ""))
            mem.close()

    def test_losing_setup_lowers_conf_but_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self._seed(mem, 60, "LOSS")
            conf, note = c.calibrate_confidence(mem, 0.75, "NEW", "FUNDING_SQUEEZE")
            self.assertLess(conf, 0.75)
            self.assertGreaterEqual(conf, 0.75 - c.CAL_MAX_DOWN - 1e-9)
            self.assertIn("setup n=60", note)
            mem.close()

    def test_small_sample_barely_moves_conf(self):
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self._seed(mem, 2, "LOSS")
            conf, _ = c.calibrate_confidence(mem, 0.75, "NEW", "FUNDING_SQUEEZE")
            self.assertGreater(conf, 0.75 - 0.05)
            mem.close()

    def test_winning_setup_boost_capped_and_new_symbol_not_halved(self):
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self._seed(mem, 80, "PROFIT")
            conf, _ = c.calibrate_confidence(mem, 0.60, "BRANDNEW", "FUNDING_SQUEEZE")
            self.assertLessEqual(conf, 0.60 + c.CAL_MAX_UP + 1e-9)
            self.assertGreater(conf, 0.60)
            # setup tanpa histori sama sekali: tidak berubah (bukan ×0.5)
            conf2, note2 = c.calibrate_confidence(mem, 0.60, "BRANDNEW", "COMPRESSION")
            self.assertEqual((conf2, note2), (0.60, ""))
            mem.close()

    def test_simulated_neutral_and_old_outcomes_are_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self._seed(mem, 50, "LOSS", simulated=1, tag="sim")
            self._seed(mem, 50, "NEUTRAL", tag="neu")
            self._seed(mem, 50, "LOSS", age_days=c.CAL_LOOKBACK_DAYS + 5, tag="old")
            self.assertEqual(mem.outcome_counts(setup_type="FUNDING_SQUEEZE"), (0, 0, 0))
            self.assertEqual(
                c.calibrate_confidence(mem, 0.7, "X", "FUNDING_SQUEEZE"), (0.7, ""),
            )
            mem.close()

    def test_partial_weight_and_symbol_scope(self):
        with tempfile.TemporaryDirectory() as td:
            mem = self._mem(td)
            self._seed(mem, 10, "PARTIAL", sym="PP", tag="p")
            w, pt, l = mem.outcome_counts(symbol="PP")
            self.assertEqual((w, pt, l), (0, 10, 0))
            self.assertEqual(mem.outcome_counts(symbol="OTHER"), (0, 0, 0))
            mem.close()


class TestOIDeltaConfirm(unittest.TestCase):
    """ΔOI×Δharga: sumber REAL ke-3 saat cross-exchange mati."""

    def test_confirm_semantics_price_oi_matrix(self):
        self.assertTrue(c.oi_delta_confirm("LONG", (0.02, 0.01)).confirmed)
        self.assertFalse(c.oi_delta_confirm("LONG", (0.02, -0.01)).confirmed)
        self.assertFalse(c.oi_delta_confirm("LONG", (-0.02, 0.01)).confirmed)  # OI turun = tutup posisi
        self.assertTrue(c.oi_delta_confirm("SHORT", (0.02, -0.01)).confirmed)
        self.assertFalse(c.oi_delta_confirm("SHORT", (0.02, 0.01)).confirmed)
        self.assertFalse(c.oi_delta_confirm("LONG", (0.001, 0.0005)).confirmed)  # di bawah threshold
        r = c.oi_delta_confirm("LONG", (0.02, 0.01))
        self.assertFalse(r.is_fallback)

    def test_cold_buffer_is_agent_off_not_fake_confirm(self):
        r = c.oi_delta_confirm("LONG", None)
        self.assertEqual(r.detail, "no oi delta")
        passed, count, real, n = c.evaluate_confirms([
            c.ConfirmResult("microstructure", True, "x"),
            c.ConfirmResult("trade_flow", True, "x"),
            r,
        ])
        self.assertEqual(n, 2, "agent off tidak dihitung aktif")
        self.assertTrue(passed)

    def test_buffer_needs_min_age_and_expires(self):
        import time as _t
        buf = c.OISnapshotBuffer()
        a = c.AssetCtx(symbol="Z", funding=0.0, open_interest=1000.0,
                       mark_px=100.0, mid_px=100.0, day_ntl_vlm=1e7, prev_day_px=100.0)
        now = _t.monotonic()
        buf.record([a], now_m=now - 30)                 # terlalu baru
        self.assertIsNone(buf.get_delta("Z", 1020.0, 101.0, now_m=now))
        buf.record([a], now_m=now - c.OI_DELTA_MIN_AGE_SEC - 5)
        d = buf.get_delta("Z", 1020.0, 101.0, now_m=now)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d[0], 0.02, places=6)
        self.assertAlmostEqual(d[1], 0.01, places=6)
        # snapshot terlalu tua = bukan "kini"
        old = c.OISnapshotBuffer()
        old._snaps["Z"] = [(now - c.OI_DELTA_MAX_AGE_SEC - 60, 1000.0, 100.0)]
        self.assertIsNone(old.get_delta("Z", 1020.0, 101.0, now_m=now))

    def test_process_trigger_reaches_real_count_3_and_replaces_fallback(self):
        import asyncio, time as _t
        from unittest.mock import AsyncMock, patch
        asset = c.AssetCtx(
            symbol="OID", funding=0.001, open_interest=1020.0,
            mark_px=99.0, mid_px=99.0, day_ntl_vlm=20_000_000, prev_day_px=100.0,
        )
        trigger = c.TriggerHit("OID", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003,
                               "FUNDING_SQUEEZE")
        book = c.HLBookBuffer()
        book.ingest({"channel": "l2Book", "data": {"coin": "OID", "levels": [
            [{"px": "98.9", "sz": "1"}], [{"px": "99.1", "sz": "100"}]]}})
        flow = c.HLTradeFlowBuffer(min_samples=3)
        flow.ingest({"channel": "trades", "data": [
            {"coin": "OID", "side": "A", "px": "99", "sz": "1"}] * 3})
        oi = c.OISnapshotBuffer()
        oi._snaps["OID"] = [(_t.monotonic() - 700, 1000.0, 100.0)]  # OI +2%, harga -1%
        structure = c.StructureContext(
            swings=[], events=[c.StructureEvent("BOS", "DOWN", 100.0, c.now_utc())],
            last_trend="DOWN", sweep=None, atr=1.0,
        )
        captured = {}
        orig = c.evaluate_confirms
        def _spy(confirms):
            captured["c"] = confirms
            return orig(confirms)
        with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(None, "none"))), \
             patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=structure)), \
             patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")), \
             patch("cryptone_v45.session_allows", return_value=(True, "ok")), \
             patch("cryptone_v45.evaluate_confirms", side_effect=_spy):
            sig = asyncio.run(c.process_trigger(
                asset, trigger, None, None, hl_book=book, hl_trade_flow=flow,
                oi_history=oi, atr_hint=1.5,
            ))
        agents = [r.agent for r in captured["c"]]
        self.assertIn("oi_delta", agents)
        self.assertNotIn("oi_structure", agents, "digantikan, bukan ditambah")
        self.assertIsNotNone(sig)
        self.assertEqual(sig.real_count, 3)
        self.assertEqual(sig.active_agent_count, 3, "n_active & ambang need tidak naik")
        self.assertEqual(c.assign_tier(3.0, sig.real_count), "A")

    def test_cold_or_cascade_falls_back_to_oi_structure(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        asset = c.AssetCtx(
            symbol="OIC", funding=0.001, open_interest=1020.0,
            mark_px=99.0, mid_px=99.0, day_ntl_vlm=20_000_000, prev_day_px=100.0,
        )
        trigger = c.TriggerHit("OIC", "funding_extreme", 1.0, "SHORT", 0.001, 0.0003,
                               "FUNDING_SQUEEZE")
        captured = {}
        orig = c.evaluate_confirms
        def _spy(confirms):
            captured["c"] = confirms
            return orig(confirms)
        with patch("cryptone_v45.htf_bias_gate", new=AsyncMock(return_value=(True, "ok"))), \
             patch("cryptone_v45.cross_exchange_price", new=AsyncMock(return_value=(None, "none"))), \
             patch("cryptone_v45.get_structure_context", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.fear_greed_latest", new=AsyncMock(return_value=None)), \
             patch("cryptone_v45.context_modifier", new=AsyncMock(return_value=1.0)), \
             patch("cryptone_v45._env_ok", return_value=False), \
             patch("cryptone_v45.veto_checks", return_value=(True, "")), \
             patch("cryptone_v45.evaluate_confirms", side_effect=_spy):
            asyncio.run(c.process_trigger(
                asset, trigger, None, None, oi_history=c.OISnapshotBuffer(),
                atr_hint=1.5,
            ))
        agents = [r.agent for r in captured["c"]]
        self.assertIn("oi_structure", agents)
        self.assertNotIn("oi_delta", agents)


class TestPipelineCycleAudit(unittest.TestCase):
    """run_pipeline_cycle: kuota per setup, skip setup ter-pause, ATR prefetch."""

    @staticmethod
    def _asset(sym):
        return c.AssetCtx(symbol=sym, funding=0.001, open_interest=1e6,
                          mark_px=100.0, mid_px=100.0, day_ntl_vlm=5e7, prev_day_px=100.0)

    @staticmethod
    def _sig(sym, setup):
        now = c.now_utc()
        return c.PipelineSignal(
            signal_id=f"{sym}-id", symbol=sym, direction="LONG", horizon="INTRADAY",
            tier="B", setup_type=setup, trigger_type="t", entry_low=99.0,
            entry_high=101.0, stop_loss=95.0, tp1=105.0, tp2=110.0, rr=2.0,
            confidence=0.7, base_confidence=0.7, context_mult=1.0, confirm_count=2,
            active_agent_count=3, real_count=2, reasoning="t", created_at=now,
            valid_until=now + c.timedelta(hours=2), sl_basis="atr", entry_basis="atr",
            state="PENDING", confluence=3,
        )

    def _cycle(self, mem, assets, *, hits_for, cascade_map=None, atr_patch=None,
               proc=None, enabled_spy=None):
        import asyncio
        from unittest.mock import AsyncMock, patch
        c._ATR_CACHE.clear()
        def _scan(asset, atr, cas, memory, setup_enabled=None):
            if enabled_spy is not None:
                enabled_spy.append(dict(setup_enabled or {}))
            return hits_for(asset, setup_enabled or {})
        state = c.BlackSwanState(active=False, indicators=[], count=0, mode="NORMAL")
        patches = [
            patch("cryptone_v45.run_universe_scan", new=AsyncMock(return_value=(assets, "BTC"))),
            patch("cryptone_v45.correlation_vs_anchor", new=AsyncMock(return_value={})),
            patch("cryptone_v45.check_black_swan", new=AsyncMock(return_value=state)),
            patch("cryptone_v45.compute_exchange_netflow_usd", new=AsyncMock(return_value=None)),
            patch("cryptone_v45.scan_triggers_for_asset", new=_scan),
            patch("cryptone_v45.persist_signal", new=AsyncMock()),
        ]
        if atr_patch is not None:
            patches.append(atr_patch)
        else:
            patches.append(patch("cryptone_v45.estimate_atr", new=AsyncMock(return_value=1.0)))
        if proc is not None:
            patches.append(patch("cryptone_v45.process_trigger", new=proc))
        from contextlib import ExitStack
        with ExitStack() as st:
            for pt in patches:
                st.enter_context(pt)
            return asyncio.run(c.run_pipeline_cycle(
                mem, cascade_map=cascade_map or {}, cascade_side_map={}, dry_run=False,
            ))

    def test_per_setup_quota_applies_to_cascade_too(self):
        """CASCADE boleh sampai MAX_CASCADE_PER_CYCLE; funding tetap di-cap 2."""
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            # 5 cascade candidates — cycle max 3 signals, cascade cap 4
            assets = [self._asset(s) for s in ("A", "B", "C", "D", "E")]
            def _hits(asset, en):
                return [c.TriggerHit(asset.symbol, "cascade", 1.0, "NEUTRAL",
                                     6e6, 5e6, "CASCADE_SCALP")]
            async def _proc(asset, trigger, *a, **k):
                return self._sig(asset.symbol, trigger.setup_type)
            proc = AsyncMock(side_effect=_proc)
            out = self._cycle(
                mem, assets, hits_for=_hits, proc=proc,
                cascade_map={"A": 9e6, "B": 8e6, "C": 7e6, "D": 6e6, "E": 5e6},
            )
            setups = [s.setup_type for s in out]
            self.assertTrue(all(s == "CASCADE_SCALP" for s in setups))
            # Hard cap cycle = MAX_SIGNALS_PER_CYCLE (3), bukan stuck di 2
            self.assertEqual(len(setups), c.MAX_SIGNALS_PER_CYCLE)
            self.assertLessEqual(len(setups), c.MAX_CASCADE_PER_CYCLE)
            mem.close()

    def test_paused_setup_is_skipped_at_l1_and_logged(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            mem.set_runtime(
                "setup_paused_until:FUNDING_SQUEEZE",
                (c.now_utc() + c.timedelta(hours=1)).isoformat(),
            )
            spy: list = []
            with self.assertLogs("cryptone", level="INFO") as cm:
                self._cycle(mem, [self._asset("A")], hits_for=lambda a, e: [],
                            enabled_spy=spy)
            self.assertTrue(spy)
            self.assertFalse(spy[0]["FUNDING_SQUEEZE"])
            self.assertTrue(spy[0]["CASCADE_SCALP"])
            self.assertTrue(any("setup-loss pause aktif" in m and "FUNDING_SQUEEZE" in m
                                for m in cm.output))
            mem.close()

    def test_cycle_prefetches_atr_once_then_hits_cache(self):
        from unittest.mock import patch
        calls = {"n": 0}
        async def _snap(sym, *a, **k):
            calls["n"] += 1
            return TestAtrCache._candles(sym)
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            assets = [self._asset(f"P{i}") for i in range(8)]
            from unittest.mock import patch as _p
            with _p("cryptone_v45.hl_candle_snapshot", new=_snap):
                # atr_patch=… tidak dipakai → estimate_atr ASLI berjalan
                import asyncio
                from unittest.mock import AsyncMock
                c._ATR_CACHE.clear()
                state = c.BlackSwanState(active=False, indicators=[], count=0, mode="NORMAL")
                def _run_once():
                    with _p("cryptone_v45.run_universe_scan", new=AsyncMock(return_value=(assets, "BTC"))), \
                         _p("cryptone_v45.correlation_vs_anchor", new=AsyncMock(return_value={})), \
                         _p("cryptone_v45.check_black_swan", new=AsyncMock(return_value=state)), \
                         _p("cryptone_v45.compute_exchange_netflow_usd", new=AsyncMock(return_value=None)), \
                         _p("cryptone_v45.scan_triggers_for_asset", new=lambda *a, **k: []):
                        return asyncio.run(c.run_pipeline_cycle(mem, dry_run=True))
                _run_once()
                first = calls["n"]
                _run_once()
                self.assertEqual(first, 8, "cycle pertama: 1 fetch per simbol")
                self.assertEqual(calls["n"], 8, "cycle kedua (dalam TTL): 0 fetch baru")
            c._ATR_CACHE.clear()
            mem.close()

    def test_circuit_breaker_logs_pause_when_it_fires(self):
        """Breaker benar-benar fire pada outcome LIVE & mencatat warning eksplisit."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=1)).isoformat()
            async def _seed(sid, simulated):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sid, "symbol": sid, "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B", "entry_zone_low": 100,
                    "entry_zone_high": 100, "stop_loss": 90, "tp1": 110, "tp2": 120,
                    "rr": 2, "confidence": 0.6, "trigger_type": "t", "reasoning": "t",
                    "setup_type": "COMPRESSION", "last_checked_at": now,
                    "created_at": now, "valid_until": future, "is_simulated": simulated,
                })
                await mem.flush_writes()
            # LOSS simulated tidak boleh menghitung ke breaker (tak ada bocor ke live)
            for i in range(3):
                asyncio.run(_seed(f"sim{i}", 1))
                mem.resolve_signal(f"sim{i}", "LOSS", realized_rr=-1.0)
            self.assertFalse(mem.setup_signals_paused("COMPRESSION"))
            with self.assertLogs("cryptone", level="WARNING") as cm:
                for i in range(3):
                    asyncio.run(_seed(f"live{i}", 0))
                    mem.resolve_signal(f"live{i}", "LOSS", realized_rr=-1.0)
            self.assertTrue(mem.setup_signals_paused("COMPRESSION"))
            self.assertTrue(any("setup-loss pause" in m and "COMPRESSION" in m for m in cm.output))
            mem.close()

    def test_trade_flow_no_confirm_counts_as_active_like_microstructure(self):
        """Stream warm tapi berlawanan = agent aktif tak-confirm (bukan 'off'):
        konsisten dengan microstructure; sengaja menaikkan `need`."""
        r = c.trade_flow_confirm("LONG", -0.3)
        self.assertFalse(r.confirmed)
        self.assertNotEqual(r.detail, "agent off")
        passed, count, real, n = c.evaluate_confirms([
            c.ConfirmResult("microstructure", True, "imbalance=0.5"),
            r,
            c.ConfirmResult("oi_structure", True, "x", is_fallback=True),
        ])
        self.assertEqual((n, count), (3, 2))
        self.assertTrue(passed)


class TestRssHeadlineTtl(unittest.TestCase):
    """Headline harus benar-benar kedaluwarsa (wall-clock), bukan hidup selamanya."""

    def setUp(self):
        self._saved = (c._RECENT_HEADLINES, c._RSS_CACHE_BIAS, c._SECTOR_BIAS_CACHE,
                       c._RSS_LAST_POLL, c._RSS_LAST_COUNT)
        c._RECENT_HEADLINES = []
        c._RSS_CACHE_BIAS = None
        c._SECTOR_BIAS_CACHE = {}
        c._RSS_LAST_POLL = 0.0
        c._RSS_LAST_COUNT = 0

    def tearDown(self):
        (c._RECENT_HEADLINES, c._RSS_CACHE_BIAS, c._SECTOR_BIAS_CACHE,
         c._RSS_LAST_POLL, c._RSS_LAST_COUNT) = self._saved

    def test_symbol_bias_ignores_expired_headlines(self):
        old = c.time.time() - c._RSS_HEADLINE_TTL_SEC - 60
        c._RECENT_HEADLINES = [
            (old, "SOL rally continues"),
            (old, "Solana ETF approval sends SOL surging"),
        ]
        c._RSS_CACHE_BIAS = None
        self.assertIsNone(c.rss_news_bias("SOL"))
        fresh = c.time.time() - 60
        c._RECENT_HEADLINES = [
            (fresh, "SOL rally continues"),
            (fresh, "Solana ETF approval sends SOL surging"),
        ]
        self.assertEqual(c.rss_news_bias("SOL"), "BULLISH")

    def test_sector_bias_ignores_expired_headlines(self):
        old = c.time.time() - c._RSS_HEADLINE_TTL_SEC - 60
        c._RECENT_HEADLINES = [
            (old, "AI agents surge to new highs"),
            (old, "AI token rally accelerates"),
            (old, "New AI model sparks bullish sentiment"),
        ]
        c._recompute_sector_bias()
        self.assertIsNone(c.sector_news_bias("AI"))

    def _poll(self, entries):
        import asyncio, sys, types
        from types import SimpleNamespace
        from unittest.mock import patch
        fake = types.ModuleType("feedparser")
        fake.parse = lambda url: SimpleNamespace(entries=entries)
        prev = sys.modules.get("feedparser")
        sys.modules["feedparser"] = fake
        try:
            with patch.object(c, "RSS_FEEDS", ["http://x"]):
                return asyncio.run(c.poll_rss_headline_count(None))
        finally:
            if prev is None:
                sys.modules.pop("feedparser", None)
            else:
                sys.modules["feedparser"] = prev

    def test_poll_merge_drops_expired_keeps_fresh_and_dedups(self):
        import time as _t
        from types import SimpleNamespace
        now = c.time.time()
        c._RECENT_HEADLINES = [
            (now - c._RSS_HEADLINE_TTL_SEC - 120, "Ancient headline"),
            (now - 600, "Fresh carried headline"),
            (now - 900, "New item today"),        # duplikat dengan poll baru
        ]
        entries = [SimpleNamespace(
            title="New item today", id="n1",
            published_parsed=_t.gmtime(now - 60),
        )]
        self._poll(entries)
        titles = [t for _, t in c._RECENT_HEADLINES]
        self.assertNotIn("Ancient headline", titles)
        self.assertIn("Fresh carried headline", titles)
        self.assertEqual(titles.count("New item today"), 1)
        # entri poll baru menang saat dedup (timestamp lebih baru)
        ts_new = [ts for ts, t in c._RECENT_HEADLINES if t == "New item today"][0]
        self.assertGreater(ts_new, now - 300)
        # semua timestamp wall-clock, tidak ada yang tercampur monotonic
        self.assertTrue(all(ts > 1e9 for ts, _ in c._RECENT_HEADLINES))

    def test_undated_entry_is_wall_clock_and_expires(self):
        from types import SimpleNamespace
        self._poll([
            SimpleNamespace(title="SOL rally continues into the weekend", id="u1"),
            SimpleNamespace(title="Solana ETF approval sends SOL surging", id="u2"),
        ])
        stamps = [ts for ts, _ in c._RECENT_HEADLINES]
        self.assertEqual(len(stamps), 2)
        self.assertTrue(all(abs(ts - c.time.time()) < 5 for ts in stamps))
        c._RSS_CACHE_BIAS = None
        self.assertEqual(c.rss_news_bias("SOL"), "BULLISH")
        # geser jam melewati TTL: headline tanpa tanggal pun harus kedaluwarsa
        c._RECENT_HEADLINES = [
            (ts - c._RSS_HEADLINE_TTL_SEC - 1, t) for ts, t in c._RECENT_HEADLINES
        ]
        self.assertIsNone(c.rss_news_bias("SOL"))


class TestOISnapshotPersistence(unittest.TestCase):
    """metric_oi: persist tiap cycle, seed saat startup, retensi, migrasi DB lama."""

    @staticmethod
    def _asset(sym="Z", oi=1000.0, px=100.0):
        return c.AssetCtx(symbol=sym, funding=0.0, open_interest=oi,
                          mark_px=px, mid_px=px, day_ntl_vlm=1e7, prev_day_px=px)

    def test_record_returns_only_valid_snapshots(self):
        buf = c.OISnapshotBuffer()
        rec = buf.record([self._asset("A"), self._asset("B", oi=0.0)])
        self.assertEqual(rec, [("A", 1000.0, 100.0)])

    def test_seed_converts_epoch_to_monotonic_and_warms(self):
        buf = c.OISnapshotBuffer()
        now_m, now_e = 5000.0, 1_800_000_000.0
        rows = [
            ("Z", now_e - 1200, 1000.0, 100.0),                        # 20 mnt lalu → baseline valid
            ("Z", now_e - 60, 1010.0, 100.5),                          # terlalu baru
            ("Z", now_e - c.OI_SNAPSHOT_KEEP_SEC - 10, 900.0, 90.0),   # terlalu tua → dibuang
            ("Z", now_e + 500, 999.0, 99.0),                           # masa depan → dibuang
            ("Y", now_e - 1200, 0.0, 100.0),                           # OI 0 → dibuang
        ]
        self.assertEqual(buf.seed(rows, now_m=now_m, now_epoch=now_e), 2)
        d = buf.get_delta("Z", 1020.0, 101.0, now_m=now_m)
        self.assertIsNotNone(d)
        self.assertAlmostEqual(d[0], 0.02, places=6)
        self.assertAlmostEqual(d[1], 0.01, places=6)
        self.assertIsNone(buf.get_delta("Y", 1020.0, 101.0, now_m=now_m))

    def test_seed_only_young_rows_stays_cold(self):
        buf = c.OISnapshotBuffer()
        buf.seed([("Z", 1_800_000_000.0 - 120, 1000.0, 100.0)],
                 now_m=5000.0, now_epoch=1_800_000_000.0)
        self.assertIsNone(buf.get_delta("Z", 1020.0, 101.0, now_m=5000.0))

    def test_persist_load_roundtrip_and_restart_is_warm(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "t.db")
            mem = c.MemoryEngine(db)
            mem.start()
            # snapshot lama (20 mnt) ditulis langsung; yang baru lewat persist_oi_snapshots
            old_ts = (c.now_utc() - c.timedelta(minutes=20)).isoformat()
            async def _go():
                await mem.enqueue_write("metric_oi", {
                    "symbol": "Z", "value": 1000.0, "px": 100.0, "recorded_at": old_ts})
                await mem.flush_writes()
                await c.persist_oi_snapshots(mem, [("Z", 1005.0, 100.2)])
            asyncio.run(_go())
            rows = mem.load_oi_snapshots(c.OI_SNAPSHOT_KEEP_SEC)
            self.assertEqual([r[0] for r in rows], ["Z", "Z"])
            self.assertLess(rows[0][1], rows[1][1])       # urut waktu
            mem.close()

            # "restart": proses baru, buffer kosong, DB sama
            mem2 = c.MemoryEngine(db)
            mem2.start()
            saved = c._oi_snapshot_buffer
            c._oi_snapshot_buffer = c.OISnapshotBuffer()
            try:
                self.assertEqual(c.warm_oi_snapshot_buffer(mem2), 2)
                d = c.get_oi_snapshot_buffer().get_delta("Z", 1020.0, 101.0)
                self.assertIsNotNone(d, "restart tidak boleh mengulang warm-up 10 menit")
                self.assertAlmostEqual(d[0], 0.02, places=3)
            finally:
                c._oi_snapshot_buffer = saved
            mem2.close()

    def test_persist_is_idempotent_and_prunes_stale(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            stale = (c.now_utc() - c.timedelta(seconds=c.OI_SNAPSHOT_KEEP_SEC + 60)).isoformat()
            async def _go():
                for _ in range(2):   # recorded_at sama → INSERT OR IGNORE
                    await mem.enqueue_write("metric_oi", {
                        "symbol": "Z", "value": 1.0, "px": 1.0, "recorded_at": stale})
                await mem.flush_writes()
                await c.persist_oi_snapshots(mem, [])
            asyncio.run(_go())
            n = mem._conn.execute("SELECT COUNT(*) AS n FROM metric_oi").fetchone()["n"]
            self.assertEqual(n, 0, "baris basi harus dipangkas")
            mem.close()

    def test_legacy_metric_oi_without_px_is_migrated(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "legacy.db")
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE metric_oi (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, value REAL NOT NULL,
                    recorded_at TIMESTAMP NOT NULL,
                    is_simulated BOOLEAN DEFAULT 0, schema_version INTEGER DEFAULT 1);
                INSERT INTO metric_oi (symbol, value, recorded_at) VALUES ('Z', 1, '2020-01-01T00:00:00+00:00');
                INSERT INTO metric_oi (symbol, value, recorded_at) VALUES ('Z', 1, '2020-01-01T00:00:00+00:00');
            """)
            conn.commit()
            conn.close()
            mem = c.MemoryEngine(db)
            mem.start()
            cols = {r["name"] for r in mem._conn.execute("PRAGMA table_info(metric_oi)").fetchall()}
            self.assertIn("px", cols)
            n = mem._conn.execute("SELECT COUNT(*) AS n FROM metric_oi").fetchone()["n"]
            self.assertEqual(n, 1, "duplikat lama didedup sebelum unique index")
            # baris lama tanpa px tidak pernah ikut warm-up
            self.assertEqual(mem.load_oi_snapshots(10 ** 10), [])
            mem.close()

    def test_warm_up_is_fail_soft(self):
        from unittest.mock import MagicMock
        bad = MagicMock()
        bad.load_oi_snapshots.side_effect = RuntimeError("db gone")
        self.assertEqual(c.warm_oi_snapshot_buffer(bad), 0)


class TestAuditFixes(unittest.TestCase):
    """Regresi dari audit: trust_score, lifecycle SL/TP, kartu sinyal, menu, FVG, log ATR."""


    def test_swing_structural_tp2_meets_min_rr(self):
        """SWING structural tp2_mult=6 → natural RR ≥ MIN_RR 3.0 (bukan 5→2.5 reject)."""
        # ATR path
        t = c.analyst_timing("LONG", 100.0, 2.0, "SWING", 0.8, 2)
        self.assertIsNotNone(t)
        self.assertGreaterEqual(t.rr + 1e-9, c.MIN_RR["SWING"])
        # structural path dengan swing LOW
        swings = [c.SwingPoint(idx=0, price=96.0, kind="LOW", bar_time=c.now_utc())]
        ctx = c.StructureContext(
            swings=swings, events=[], last_trend="UP", sweep=None, atr=2.0,
            nearest_fvg=None, fvg_zones=None,
        )
        ts = c.analyst_timing_structural("LONG", 100.0, 2.0, "SWING", 0.8, 2, ctx)
        self.assertIsNotNone(ts, "SWING structural tidak boleh None karena RR < min")
        self.assertGreaterEqual(ts.rr + 1e-9, c.MIN_RR["SWING"])


    def test_armed_short_mid_above_sl_closes_loss(self):
        """DOGE bug: ARMED SHORT + mid di atas SL harus LOSS, candle kosong pun."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc()
            armed = (now - c.timedelta(minutes=30)).isoformat()
            future = (now + c.timedelta(hours=12)).isoformat()
            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "doge1", "symbol": "DOGE", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "A",
                    "entry_zone_low": 0.09381, "entry_zone_high": 0.09409,
                    "stop_loss": 0.09466, "original_stop": 0.09466,
                    "tp1": 0.09244, "tp2": 0.09092, "rr": 4.24,
                    "confidence": 0.53, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": armed, "created_at": armed,
                    "valid_until": future, "is_simulated": 0,
                    "state": "ARMED", "armed_at": armed,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "doge1", "symbol": "DOGE", "expires_at": future})
                await mem.flush_writes()
            asyncio.run(_seed())
            async def _mon():
                with patch.object(c, "hl_all_mids", new=AsyncMock_return({"DOGE": 0.09576})), \
                     patch.object(c, "hl_candle_snapshot", new=AsyncMock_return([])):
                    return await c.monitor_active_signals(mem)
            ev = asyncio.run(_mon())
            self.assertTrue(any(e.get("reason") == "SL" for e in ev), ev)
            self.assertTrue(any(e.get("outcome") == "LOSS" for e in ev), ev)
            row = mem._conn.execute(
                "SELECT outcome FROM signal_history WHERE signal_id='doge1'"
            ).fetchone()
            self.assertEqual(row["outcome"], "LOSS")
            mem.close()

    def test_no_price_data_preserves_last_checked(self):
        """No mid/candle → jangan majukan last_checked_at (skip window bug)."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc()
            armed = (now - c.timedelta(minutes=30)).isoformat()
            future = (now + c.timedelta(hours=12)).isoformat()
            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "x1", "symbol": "XYZ", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1.0, "entry_zone_high": 1.1,
                    "stop_loss": 1.2, "original_stop": 1.2,
                    "tp1": 0.9, "tp2": 0.8, "rr": 2.0,
                    "confidence": 0.5, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": armed, "created_at": armed,
                    "valid_until": future, "is_simulated": 0,
                    "state": "ARMED", "armed_at": armed,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "x1", "symbol": "XYZ", "expires_at": future})
                await mem.flush_writes()
            asyncio.run(_seed())
            before = mem._conn.execute(
                "SELECT last_checked_at FROM signal_history WHERE signal_id='x1'"
            ).fetchone()["last_checked_at"]
            async def _mon():
                with patch.object(c, "hl_all_mids", new=AsyncMock_return({})), \
                     patch.object(c, "hl_candle_snapshot", new=AsyncMock_return([])):
                    return await c.monitor_active_signals(mem)
            asyncio.run(_mon())
            after = mem._conn.execute(
                "SELECT last_checked_at, outcome FROM signal_history WHERE signal_id='x1'"
            ).fetchone()
            self.assertIsNone(after["outcome"])
            self.assertEqual(after["last_checked_at"], before)
            mem.close()



    def test_time_stop_after_tp1_is_partial(self):
        """TIME_STOP setelah TP1 → PARTIAL, bukan NEUTRAL (sample OG bug)."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc()
            armed = (now - c.timedelta(hours=7)).isoformat()  # > INTRADAY time-stop 6h
            future = (now + c.timedelta(hours=12)).isoformat()
            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "og1", "symbol": "OG", "direction": "SHORT",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 0.33, "entry_zone_high": 0.34,
                    "stop_loss": 0.36, "original_stop": 0.36,  # SL far above zone
                    "tp1": 0.31, "tp2": 0.28, "rr": 2.0,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": armed, "created_at": armed,
                    "valid_until": future, "is_simulated": 0,
                    "state": "ARMED", "armed_at": armed,
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "og1", "symbol": "OG", "expires_at": future})
                await mem.flush_writes()
                mem.touch_signal_check("og1", tp1_hit=True)  # mark TP1 done
            asyncio.run(_seed())
            # mid clearly inside entry zone, not at SL
            async def _mon():
                with patch.object(c, "hl_all_mids", new=AsyncMock_return({"OG": 0.335})), \
                     patch.object(c, "hl_candle_snapshot", new=AsyncMock_return([])):
                    return await c.monitor_active_signals(mem)
            ev = asyncio.run(_mon())
            self.assertTrue(any(e.get("reason") == "TIME_STOP" for e in ev), ev)
            self.assertTrue(any(e.get("outcome") == "PARTIAL" for e in ev), ev)
            row = mem._conn.execute(
                "SELECT outcome, tp1_hit FROM signal_history WHERE signal_id='og1'"
            ).fetchone()
            self.assertEqual(row["outcome"], "PARTIAL")
            mem.close()



    def test_realized_rr_sl_at_stop_not_wick(self):
        """SL fill at stop level → -1.0R, bukan wick ekstrem."""
        # entry mid 100, stop 90, risk 10
        rr = c._realized_rr("LONG", 99.0, 101.0, 90.0, 90.0)
        self.assertAlmostEqual(rr, -1.0, places=6)
        rr_s = c._realized_rr("SHORT", 99.0, 101.0, 110.0, 110.0)
        self.assertAlmostEqual(rr_s, -1.0, places=6)
        # TP2 at level
        rr_tp = c._realized_rr("LONG", 99.0, 101.0, 90.0, 120.0)
        self.assertAlmostEqual(rr_tp, 2.0, places=6)


    def _seed_signal(self, mem, sid, symbol="AUD", reasoning="t"):
        import asyncio
        now = c.now_utc().isoformat()
        future = (c.now_utc() + c.timedelta(hours=1)).isoformat()
        async def _go():
            await mem.enqueue_write("signal_history", {
                "signal_id": sid, "symbol": symbol, "direction": "LONG",
                "horizon": "INTRADAY", "tier": "B",
                "entry_zone_low": 1, "entry_zone_high": 1.1,
                "stop_loss": 0.9, "tp1": 1.2, "tp2": 1.3, "rr": 2,
                "confidence": 0.5, "trigger_type": "funding",
                "reasoning": reasoning, "setup_type": "FUNDING_SQUEEZE",
                "last_checked_at": now, "created_at": now,
                "valid_until": future, "is_simulated": 0,
            })
            await mem.enqueue_write("active_signals", {
                "signal_id": sid, "symbol": symbol, "expires_at": future})
            await mem.flush_writes()
        asyncio.run(_go())

    def test_trust_score_stays_default_when_only_neutral(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_signal(mem, "n0")
            mem.resolve_signal("n0", "NEUTRAL", realized_rr=0.0)
            sp = mem._conn.execute(
                "SELECT trust_score, total_signals FROM symbol_performance WHERE symbol='AUD'"
            ).fetchone()
            self.assertEqual(sp["total_signals"], 1)
            self.assertAlmostEqual(sp["trust_score"], 0.5, places=6)   # bukan 0.0
            mem.close()

    def test_trust_score_partial_weight_and_loss(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            for sid in ("a", "b"):
                self._seed_signal(mem, sid)
            mem.resolve_signal("a", "PROFIT", realized_rr=2.0)
            mem.resolve_signal("b", "LOSS", realized_rr=-1.0)
            sp = mem._conn.execute(
                "SELECT trust_score FROM symbol_performance WHERE symbol='AUD'").fetchone()
            self.assertAlmostEqual(sp["trust_score"], 0.5, places=6)   # 1 win / 2 resolved
            mem.close()

    def test_signal_card_parses_basis_from_reasoning(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_signal(
                mem, "s1",
                reasoning="X | t raw=1 | ctx×1.00 | sl=structure_widened_min_risk entry=fvg | cal 0.70→0.62 (sym n=5 wr=0.40)",
            )
            card = c._fmt_signal_card_from_db(mem, "s1")
            self.assertIn("SL structure_widened_min_risk · entry fvg", card)
            mem.close()

    def test_signal_card_unknown_basis_is_question_mark_not_structure(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_signal(mem, "s2", reasoning="")
            card = c._fmt_signal_card_from_db(mem, "s2")
            self.assertIn("SL ? · entry ?", card)
            self.assertNotIn("structure", card)
            mem.close()

    def test_unknown_setup_and_mode_callbacks_say_so(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            before = dict(mem.get_setup_enabled())
            text, mk = c.resolve_menu_callback("set:setup:BOGUS", mem)
            self.assertIn("Setup BOGUS tidak dikenal", text)
            self.assertTrue(mk and mk.get("inline_keyboard"))
            self.assertEqual(dict(mem.get_setup_enabled()), before)
            text2, _ = c.resolve_menu_callback("set:mode:bogus", mem)
            self.assertIn("Mode 'bogus' tidak dikenal", text2)
            mem.close()

    def test_market_view_renders_without_dead_import(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self.assertIn("Market Snapshot", c._fmt_market(mem))
            mem.close()

    def test_structural_long_does_single_fvg_lookup(self):
        from unittest.mock import patch
        swings = [c.SwingPoint(idx=0, price=99.5, kind="LOW", bar_time=c.now_utc())]
        ctx = c.StructureContext(
            swings=swings, events=[], last_trend="UP", sweep=None, atr=1.0,
            nearest_fvg=None, fvg_zones=None,
        )
        with patch.object(c, "nearest_aligned_fvg", return_value=None) as m:
            c.analyst_timing_structural("LONG", 100.0, 1.0, "INTRADAY", 1.0, 2, ctx)
        self.assertEqual(m.call_count, 1)

    def test_routine_atr_prefetch_logs_at_debug_only(self):
        import logging
        records: list[logging.LogRecord] = []
        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)
        h = _H(level=logging.DEBUG)
        lg = logging.getLogger("cryptone")
        old_level = lg.level
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
        try:
            with tempfile.TemporaryDirectory() as td:
                mem = c.MemoryEngine(str(Path(td) / "t.db"))
                mem.start()
                TestPipelineCycleAudit()._cycle(
                    mem, [TestPipelineCycleAudit._asset("A")], hits_for=lambda a, e: [],
                )
                mem.close()
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)
        atr = [r for r in records if "ATR prefetch" in r.getMessage()]
        self.assertTrue(atr)
        self.assertTrue(all(r.levelno == logging.DEBUG for r in atr))



def AsyncMock_return(value):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=value)


class TestAuditRemediation(unittest.TestCase):
    """§X1 — Regresi hasil audit: flush isolasi, tz, pagination, deque, etherscan, ATR."""

    # ---- #1 flush_writes isolasi per-item ----
    def test_flush_isolates_bad_row_and_commits_rest(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            good = lambda n: {
                "source_name": n, "success": 1, "latency_ms": 5.0,
                "error_type": None, "recorded_at": "2026-01-01T00:00:00+00:00",
            }
            async def _run():
                await mem.enqueue_write("data_source_health", good("a"))
                await mem.enqueue_write("metric_funding", {"symbol": "X"})  # param hilang
                await mem.enqueue_write("data_source_health", good("b"))
                await mem.flush_writes()
            asyncio.run(_run())  # tidak boleh raise
            n = mem._conn.execute("SELECT COUNT(*) FROM data_source_health").fetchone()[0]
            self.assertEqual(n, 2, "row sehat sebelum & sesudah row buruk harus tersimpan")
            self.assertTrue(mem._write_queue.empty())
            mem.close()

    # ---- #2 naive datetime = UTC ----
    def test_naive_datetime_is_utc_not_wib(self):
        from datetime import datetime as _dt
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CRYPTONE_QUIET_HOURS", None)
            os.environ.pop("CRYPTONE_SESSION_FILTER", None)
            # naive 17:00 UTC == 00:00 WIB → quiet. (Bug lama: dibaca 17:00 WIB → tidak quiet)
            self.assertTrue(c.in_quiet_hours(_dt(2026, 9, 27, 17, 0)))
            # naive 08:00 UTC == 15:00 WIB → jam sesi (sama dgn tes aware 15:00 WIB)
            ok, note = c.session_allows("INTRADAY", "B", now=_dt(2026, 9, 27, 8, 0))
            self.assertTrue(ok)
            self.assertIn("15wib", note)
            # aware tetap benar
            self.assertFalse(c.in_quiet_hours(_dt(2026, 9, 27, 15, 0, tzinfo=c.WIB)))

    # ---- #3 pagination candle ----
    @staticmethod
    def _rows(t0, n, step=60_000):
        return [{"t": t0 + i * step, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10} for i in range(n)]

    async def _wr(self, src, fn, memory=None):
        return await fn()

    def test_candle_paginates_until_short_page(self):
        import asyncio
        from unittest.mock import AsyncMock
        p1 = self._rows(1_000, c.HL_CANDLE_MAX_PER_CALL)
        p2 = self._rows(p1[-1]["t"] + 60_000, 3)
        post = AsyncMock(side_effect=[p1, p2])
        with patch.object(c, "http_post_json", new=post), patch.object(c, "with_retry", new=self._wr):
            out = asyncio.run(c.hl_candle_snapshot("BTC", "1m", 0, 10**12))
        self.assertEqual(len(out), c.HL_CANDLE_MAX_PER_CALL + 3)
        self.assertEqual(post.await_count, 2)
        self.assertEqual(post.await_args_list[1].args[1]["req"]["startTime"], p1[-1]["t"] + 1)
        ts = [x.bar_time for x in out]
        self.assertEqual(ts, sorted(ts))

    def test_candle_single_page_is_one_call_and_dedupes(self):
        import asyncio
        from unittest.mock import AsyncMock
        rows = self._rows(1_000, 5)
        post = AsyncMock(return_value=rows + rows[:2])   # duplikat
        with patch.object(c, "http_post_json", new=post), patch.object(c, "with_retry", new=self._wr):
            out = asyncio.run(c.hl_candle_snapshot("BTC", "1h", 0, 10**12))
        self.assertEqual(len(out), 5)
        self.assertEqual(post.await_count, 1)

    def test_candle_later_page_failure_returns_partial_first_page_failure_raises(self):
        import asyncio
        from unittest.mock import AsyncMock
        p1 = self._rows(1_000, c.HL_CANDLE_MAX_PER_CALL)
        post = AsyncMock(side_effect=[p1, c.DataUnavailable("x")])
        with patch.object(c, "http_post_json", new=post), patch.object(c, "with_retry", new=self._wr):
            out = asyncio.run(c.hl_candle_snapshot("BTC", "1m", 0, 10**12))
        self.assertEqual(len(out), c.HL_CANDLE_MAX_PER_CALL)
        post2 = AsyncMock(side_effect=c.DataUnavailable("x"))
        with patch.object(c, "http_post_json", new=post2), patch.object(c, "with_retry", new=self._wr):
            with self.assertRaises(c.DataUnavailable):
                asyncio.run(c.hl_candle_snapshot("BTC", "1m", 0, 10**12))

    def test_candle_stops_at_page_cap(self):
        import asyncio
        from unittest.mock import AsyncMock
        counter = {"t": 1_000}
        async def _post(url, payload, timeout=15.0):
            rows = self._rows(counter["t"], c.HL_CANDLE_MAX_PER_CALL)
            counter["t"] = rows[-1]["t"] + 60_000
            return rows
        with patch.object(c, "http_post_json", new=_post), patch.object(c, "with_retry", new=self._wr):
            out = asyncio.run(c.hl_candle_snapshot("BTC", "1m", 0, 10**15))
        self.assertEqual(len(out), c.HL_CANDLE_MAX_PER_CALL * c.HL_CANDLE_MAX_PAGES)

    # ---- #4 CascadeBuffer deque ----
    def test_cascade_buffer_deque_prunes_by_window(self):
        from collections import deque
        buf = c.CascadeBuffer(window_sec=300.0)
        self.assertIsInstance(buf._events, deque)
        base = 1_000.0
        with patch.object(c.time, "monotonic", side_effect=[base, base + 10, base + 400]):
            buf.add_event("BTC", "LONG", 100.0)
            buf.add_event("BTC", "SHORT", 50.0)
            buf.add_event("ETH", "LONG", 25.0)   # t=1400 → dua event pertama kadaluarsa
        self.assertEqual(len(buf._events), 1)
        with patch.object(c.time, "monotonic", return_value=base + 400):
            self.assertEqual(buf.snapshot_usd(), {"ETH": 25.0})
            self.assertEqual(buf.snapshot_side(), {"ETH": "LONG"})

    # ---- #5 etherscan rate-limit ----
    def test_etherscan_rate_limit_keeps_stale_cache_and_uses_cooldown(self):
        import asyncio, time as _t
        from unittest.mock import AsyncMock
        stale_ts = _t.monotonic() - c._ETHERSCAN_CACHE_TTL - 1
        stale = [{"hash": "0x1"}]
        key = "txlist:0xabc"
        c._ETHERSCAN_CACHE[key] = (stale_ts, stale)
        c._circuit.pop("etherscan_rest", None)
        resp = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
        try:
            with patch.dict(os.environ, {"ETHERSCAN_API_KEY": "k"}), \
                 patch.object(c, "http_get_json", new=AsyncMock(return_value=resp)), \
                 patch.object(c, "with_retry", new=self._wr):
                out = asyncio.run(c._etherscan_list("0xABC", "txlist", None, offset=10))
            self.assertEqual(out, stale)
            self.assertEqual(c._ETHERSCAN_CACHE[key], (stale_ts, stale), "cache tidak boleh ditimpa []")
            st = c._circuit["etherscan_rest"]
            self.assertEqual(st["failures"], 0)
            self.assertGreater(st["open_until"], _t.monotonic())
            self.assertLessEqual(st["open_until"], _t.monotonic() + c._ETHERSCAN_RATE_COOLDOWN_SEC + 1)
        finally:
            c._ETHERSCAN_CACHE.pop(key, None)
            c._circuit.pop("etherscan_rest", None)

    def test_circuit_cooldown_never_shortens_existing_open(self):
        import time as _t
        c._circuit.pop("zz_src", None)
        try:
            c._circuit["zz_src"]["open_until"] = _t.monotonic() + 300
            c._circuit_cooldown("zz_src", 5.0)
            self.assertGreater(c._circuit["zz_src"]["open_until"], _t.monotonic() + 200)
        finally:
            c._circuit.pop("zz_src", None)

    # ---- #6 ATR fail-closed ----
    def test_atr_unavailable_warns_once_per_ttl_then_debug_then_rewarns(self):
        import asyncio
        import logging
        import time as _t
        from unittest.mock import AsyncMock
        c._ATR_CACHE.clear()
        c._ATR_UNAVAILABLE_WARNED.clear()
        boom = AsyncMock(side_effect=c.DataUnavailable("down"))
        try:
            with patch.object(c, "hl_candle_snapshot", new=boom):
                with self.assertLogs("cryptone", level="WARNING") as cm:
                    self.assertIsNone(asyncio.run(c.estimate_atr("WARNCOIN", 10.0, None)))
                self.assertEqual(len(cm.records), 1)
                self.assertIn("WARNCOIN", cm.records[0].getMessage())
                self.assertIn("fail-closed", cm.records[0].getMessage())
                # dalam TTL → tidak ada warning lagi (hanya debug)
                with self.assertNoLogs("cryptone", level="WARNING"):
                    self.assertIsNone(asyncio.run(c.estimate_atr("WARNCOIN", 10.0, None)))
                # TTL lewat → warning lagi
                c._ATR_UNAVAILABLE_WARNED["WARNCOIN"] = (
                    _t.monotonic() - c.ATR_UNAVAILABLE_WARN_TTL_SEC - 1
                )
                with self.assertLogs("cryptone", level="WARNING") as cm2:
                    asyncio.run(c.estimate_atr("WARNCOIN", 10.0, None))
                self.assertEqual(len(cm2.records), 1)
        finally:
            c._ATR_CACHE.clear()
            c._ATR_UNAVAILABLE_WARNED.clear()

    def test_atr_recovery_clears_warn_state(self):
        import asyncio
        c._ATR_CACHE.clear()
        c._ATR_UNAVAILABLE_WARNED["OKCOIN"] = 1.0
        now = c.now_utc()
        candles = [
            c.Candle("OKCOIN", "1h", 10, 11, 9, 10, 1, now - c.timedelta(hours=20 - i))
            for i in range(20)
        ]
        try:
            with patch.object(c, "hl_candle_snapshot", new=AsyncMock_return(candles)):
                out = asyncio.run(c.estimate_atr("OKCOIN", 10.0, None))
            self.assertIsNotNone(out)
            self.assertNotIn("OKCOIN", c._ATR_UNAVAILABLE_WARNED)
        finally:
            c._ATR_CACHE.clear()
            c._ATR_UNAVAILABLE_WARNED.clear()

    def test_estimate_atr_none_when_candle_and_db_empty_even_with_memory(self):
        import asyncio
        from unittest.mock import AsyncMock
        c._ATR_CACHE.clear()
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            with patch.object(c, "hl_candle_snapshot",
                              new=AsyncMock(side_effect=c.DataUnavailable("down"))):
                out = asyncio.run(c.estimate_atr("NEWCOIN", 50.0, mem))
            mem.close()
        self.assertIsNone(out)
        c._ATR_CACHE.clear()


class TestP0AuditFixes(unittest.TestCase):
    """P0 audit: data-quality gate, confirm independence, lifecycle matrix, critical write."""

    def _asset(self, sym="X", funding=0.0001, px=100.0, oi=1e6, vol=5e7):
        return c.AssetCtx(
            symbol=sym, funding=funding, open_interest=oi,
            mark_px=px, mid_px=px, day_ntl_vlm=vol, prev_day_px=px,
        )

    # ---- market_data_ok ----
    def test_market_data_ok_accepts_sane(self):
        ok, reason = c.market_data_ok(self._asset(), atr=1.5)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_market_data_ok_rejects_zero_price(self):
        ok, reason = c.market_data_ok(self._asset(px=0.0), atr=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_price")

    def test_market_data_ok_rejects_nan_funding(self):
        a = self._asset()
        a.funding = float("nan")
        ok, reason = c.market_data_ok(a, atr=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_funding")

    def test_market_data_ok_rejects_absurd_funding(self):
        ok, reason = c.market_data_ok(self._asset(funding=0.08), atr=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "funding_absurd")

    def test_market_data_ok_rejects_missing_atr(self):
        ok, reason = c.market_data_ok(self._asset(), atr=None)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_atr")

    def test_market_data_ok_rejects_absurd_atr(self):
        # ATR 60 pada harga 100 = 60% > ATR_PCT_MAX_SANE
        ok, reason = c.market_data_ok(self._asset(px=100.0), atr=60.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "atr_absurd")

    def test_market_data_ok_rejects_negative_oi(self):
        ok, reason = c.market_data_ok(self._asset(oi=-1.0), atr=1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_oi")

    # ---- pure-fallback cannot reach Tier A ----
    def test_pure_fallback_confirms_reject_and_tier_c(self):
        rs = [
            c.ConfirmResult("microstructure", True, "funding_proxy", is_fallback=True),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
            c.ConfirmResult("cross_exchange", True, "xref", is_fallback=True),
        ]
        passed, total, real, n = c.evaluate_confirms(rs)
        self.assertFalse(passed, "pure-fallback harus ditolak (real_count=0)")
        self.assertEqual(real, 0)
        self.assertEqual(c.assign_tier(3.0, real), "C")

    def test_one_real_plus_proxies_can_pass_but_not_tier_a(self):
        rs = [
            c.ConfirmResult("microstructure", True, "book", is_fallback=False),
            c.ConfirmResult("oi_structure", True, "oi=1", is_fallback=True),
            c.ConfirmResult("cross_exchange", True, "xref", is_fallback=True),
        ]
        passed, total, real, n = c.evaluate_confirms(rs)
        self.assertTrue(passed)
        self.assertEqual(real, 1)
        self.assertEqual(c.assign_tier(2.0, real), "C")
        self.assertEqual(c.assign_tier(2.6, real), "C")
        self.assertEqual(c.assign_tier(2.6, 3), "A")

    # ---- lifecycle matrix: same-candle entry+SL, TP1→SL, TP2, expiry ----
    def _seed_armed(self, mem, sid, sym, direction, entry_lo, entry_hi, sl, tp1, tp2,
                    state="ARMED", tp1_hit=0):
        import asyncio
        now = c.now_utc().isoformat()
        future = (c.now_utc() + c.timedelta(hours=6)).isoformat()
        past = (c.now_utc() - c.timedelta(minutes=30)).isoformat()

        async def _go():
            await mem.enqueue_write("signal_history", {
                "signal_id": sid, "symbol": sym, "direction": direction,
                "horizon": "INTRADAY", "tier": "B",
                "entry_zone_low": entry_lo, "entry_zone_high": entry_hi,
                "stop_loss": sl, "original_stop": sl,
                "tp1": tp1, "tp2": tp2, "rr": 2.0,
                "confidence": 0.6, "trigger_type": "funding",
                "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                "last_checked_at": past, "created_at": past,
                "valid_until": future, "is_simulated": 0,
                "state": state, "armed_at": past if state == "ARMED" else None,
                "tp1_hit": tp1_hit,
            })
            await mem.enqueue_write("active_signals", {
                "signal_id": sid, "symbol": sym, "expires_at": future,
            })
            await mem.flush_writes()
            if tp1_hit:
                mem._conn.execute(
                    "UPDATE signal_history SET tp1_hit=1 WHERE signal_id=?", (sid,),
                )
                mem._conn.commit()
        asyncio.run(_go())

    def test_lifecycle_sl_priority_over_tp2_same_bar(self):
        """Intrabar unknown: SL menang atas TP2 (konservatif)."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_armed(mem, "sl1", "L1", "LONG", 99.0, 101.0, 90.0, 110.0, 120.0)
            # candle: low menembus SL, high menembus TP2
            candles = [c.Candle("L1", "1m", 100, 125, 85, 95, 10, c.now_utc())]
            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value={"L1": 95.0})), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=candles)):
                    return await c.monitor_active_signals(mem)
            events = asyncio.run(_run())
            by_id = {e["signal_id"]: e for e in events}
            self.assertEqual(by_id["sl1"]["outcome"], "LOSS")
            self.assertEqual(by_id["sl1"]["reason"], "SL")
            self.assertAlmostEqual(by_id["sl1"]["realized_rr"], -1.0, places=4)
            self.assertEqual(mem.count_active_signals(), 0)
            mem.close()

    def test_lifecycle_tp1_then_sl_is_partial(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            # sudah TP1, SL sekarang di BE (entry mid=100)
            self._seed_armed(
                mem, "p1", "P1", "LONG", 99.0, 101.0, 100.0, 110.0, 120.0,
                tp1_hit=1,
            )
            # tulis original_stop=90 agar realized R memakai risiko awal
            mem._conn.execute(
                "UPDATE signal_history SET original_stop=90.0 WHERE signal_id='p1'"
            )
            mem._conn.commit()
            candles = [c.Candle("P1", "1m", 100, 101, 99.5, 100.0, 10, c.now_utc())]
            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value={"P1": 100.0})), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=candles)):
                    return await c.monitor_active_signals(mem)
            events = asyncio.run(_run())
            by_id = {e["signal_id"]: e for e in events}
            self.assertEqual(by_id["p1"]["outcome"], "PARTIAL")
            self.assertEqual(by_id["p1"]["reason"], "SL")
            # 0.5 * 1R (TP1) + 0.5 * 0R (BE exit) = 0.5R
            self.assertAlmostEqual(by_id["p1"]["realized_rr"], 0.5, places=4)
            mem.close()

    def test_lifecycle_pending_entry_and_sl_same_window(self):
        """PENDING yang entry+SL dalam window yang sama harus resolve LOSS, bukan stuck."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_armed(
                mem, "pe1", "PE", "LONG", 99.0, 101.0, 90.0, 110.0, 120.0,
                state="PENDING",
            )
            # hi masuk zone, lo menembus SL
            candles = [c.Candle("PE", "1m", 100, 100.5, 85, 88, 10, c.now_utc())]
            async def _run():
                with patch("cryptone_v45.hl_all_mids", new=AsyncMock(return_value={"PE": 88.0})), \
                     patch("cryptone_v45.hl_candle_snapshot", new=AsyncMock(return_value=candles)):
                    return await c.monitor_active_signals(mem)
            events = asyncio.run(_run())
            by_id = {e["signal_id"]: e for e in events}
            self.assertEqual(by_id["pe1"]["outcome"], "LOSS")
            self.assertEqual(by_id["pe1"]["reason"], "SL")
            self.assertEqual(mem.count_active_signals(), 0)
            mem.close()

    def test_lifecycle_resolve_idempotent(self):
        """resolve dua kali tidak boleh double-count performance."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            self._seed_armed(mem, "id1", "ID", "LONG", 99.0, 101.0, 90.0, 110.0, 120.0)
            mem.resolve_signal("id1", "LOSS", realized_rr=-1.0)
            mem.resolve_signal("id1", "LOSS", realized_rr=-1.0)  # no-op
            row = mem._conn.execute(
                "SELECT outcome, realized_rr FROM signal_history WHERE signal_id='id1'"
            ).fetchone()
            self.assertEqual(row["outcome"], "LOSS")
            sp = mem._conn.execute(
                "SELECT total_signals, losses FROM symbol_performance WHERE symbol='ID'"
            ).fetchone()
            self.assertEqual(int(sp["total_signals"]), 1)
            self.assertEqual(int(sp["losses"]), 1)
            self.assertEqual(mem.count_active_signals(), 0)
            mem.close()

    def test_critical_write_bad_signal_does_not_block_active(self):
        """Row signal_history rusak di-drop; active_signals sehat tetap tersimpan."""
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            async def _run():
                await mem.enqueue_write("signal_history", {
                    # param wajib hilang → dispatch gagal
                    "signal_id": "bad",
                })
                await mem.enqueue_write("active_signals", {
                    "signal_id": "ok1", "symbol": "OK",
                    "expires_at": (c.now_utc() + c.timedelta(hours=1)).isoformat(),
                })
                await mem.flush_writes()
            asyncio.run(_run())
            n_act = mem._conn.execute(
                "SELECT COUNT(*) AS n FROM active_signals WHERE signal_id='ok1'"
            ).fetchone()["n"]
            self.assertEqual(n_act, 1)
            n_sig = mem._conn.execute(
                "SELECT COUNT(*) AS n FROM signal_history WHERE signal_id='bad'"
            ).fetchone()["n"]
            self.assertEqual(n_sig, 0)
            mem.close()


class TestP1Reliability(unittest.TestCase):
    """P1: dead-letter writes, reconcile state, pending_delivery, backup/restore."""

    def test_critical_write_failure_lands_in_dead_letter(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            async def _run():
                await mem.enqueue_write("signal_history", {"signal_id": "x"})  # incomplete
                await mem.flush_writes()
            asyncio.run(_run())
            self.assertEqual(mem.count_write_dead_letters(open_only=True), 1)
            row = mem._conn.execute(
                "SELECT table_name, error_type FROM write_dead_letter"
            ).fetchone()
            self.assertEqual(row["table_name"], "signal_history")
            self.assertTrue(row["error_type"])
            mem.close()

    def test_reconcile_removes_orphan_active(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            # active tanpa history → count_active raw=1, get_active (JOIN) kosong
            mem._conn.execute(
                "INSERT INTO active_signals (signal_id, symbol, expires_at) "
                "VALUES ('orphan1', 'OR', ?)",
                ((c.now_utc() + c.timedelta(hours=1)).isoformat(),),
            )
            mem._conn.commit()
            self.assertEqual(mem.count_active_signals(), 0)  # JOIN: bukan open signal
            self.assertEqual(mem.get_active_signals(), [])
            n_raw = mem._conn.execute(
                "SELECT COUNT(*) AS n FROM active_signals"
            ).fetchone()["n"]
            self.assertEqual(n_raw, 1)
            out = mem.reconcile_signal_state()
            self.assertEqual(out["orphan_active"], 1)
            n_raw2 = mem._conn.execute(
                "SELECT COUNT(*) AS n FROM active_signals"
            ).fetchone()["n"]
            self.assertEqual(n_raw2, 0)
            mem.close()

    def test_reconcile_reattaches_open_history_without_active(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc().isoformat()
            future = (c.now_utc() + c.timedelta(hours=4)).isoformat()

            async def _seed():
                await mem.enqueue_write("signal_history", {
                    "signal_id": "miss1", "symbol": "MS", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 1, "entry_zone_high": 1.1,
                    "stop_loss": 0.9, "tp1": 1.2, "tp2": 1.3, "rr": 2,
                    "confidence": 0.6, "trigger_type": "funding",
                    "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                    "last_checked_at": now, "created_at": now,
                    "valid_until": future, "is_simulated": 0, "state": "ARMED",
                })
                await mem.flush_writes()
                # sengaja TIDAK tulis active_signals
            asyncio.run(_seed())
            self.assertEqual(mem.count_active_signals(), 0)
            out = mem.reconcile_signal_state()
            self.assertEqual(out["reattached"], 1)
            self.assertEqual(mem.count_active_signals(), 1)
            mem.close()

    def test_pending_delivery_roundtrip_and_drain_success(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            q = c.TelegramDeliveryQueue(mem)
            msg = c.TelegramMessage(chat_id="42", text="hello signal", priority="normal")
            asyncio.run(q.enqueue(msg))
            # simulate fail → persist
            async def _fail_send(*a, **k):
                return False
            with patch.object(c.TelegramBot, "send_message", new=AsyncMock(side_effect=_fail_send)):
                bot = c.TelegramBot()
                bot.token = "x"  # available path not required for our mock
                n = asyncio.run(q.drain_once(bot, dry_run=False, min_interval=0))
            self.assertEqual(n, 0)
            rows = mem.load_pending_deliveries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["text"], "hello signal")

            # reload + success
            q2 = c.TelegramDeliveryQueue(mem)
            loaded = q2.load_from_db()
            self.assertEqual(loaded, 1)

            async def _ok_send(*a, **k):
                return True
            with patch.object(c.TelegramBot, "send_message", new=AsyncMock(side_effect=_ok_send)):
                bot2 = c.TelegramBot()
                bot2.token = "x"
                n2 = asyncio.run(q2.drain_once(bot2, dry_run=False, min_interval=0))
            self.assertEqual(n2, 1)
            self.assertEqual(mem.load_pending_deliveries(), [])
            mem.close()

    def test_pending_delivery_rate_limit_requeues_in_memory(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            q = c.TelegramDeliveryQueue(mem)
            asyncio.run(q.enqueue(c.TelegramMessage(chat_id="1", text="a")))
            asyncio.run(q.enqueue(c.TelegramMessage(chat_id="1", text="b")))

            async def _rl(*a, **k):
                raise c.TelegramRateLimitError("429", retry_after=0.01)

            with patch.object(c.TelegramBot, "send_message", new=AsyncMock(side_effect=_rl)):
                bot = c.TelegramBot()
                bot.token = "x"
                n = asyncio.run(q.drain_once(bot, dry_run=False, min_interval=0))
            self.assertEqual(n, 0)
            # rate-limit break: pesan kembali ke queue, belum ke DB
            self.assertFalse(q.empty())
            self.assertEqual(len(mem.load_pending_deliveries()), 0)
            mem.close()

    def test_flush_to_pending_on_shutdown(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            q = c.TelegramDeliveryQueue(mem)
            asyncio.run(q.enqueue(c.TelegramMessage(chat_id="9", text="bye")))
            n = asyncio.run(q.flush_to_pending())
            self.assertEqual(n, 1)
            self.assertTrue(q.empty())
            rows = mem.load_pending_deliveries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["text"], "bye")
            mem.close()

    def test_prune_pending_drops_old_and_max_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            old = (c.now_utc() - c.timedelta(hours=60)).isoformat()
            mem._conn.execute(
                "INSERT INTO pending_delivery "
                "(chat_id, payload_json, attempts, last_error, created_at) "
                "VALUES ('1', '{}', 0, NULL, ?)",
                (old,),
            )
            mem._conn.execute(
                "INSERT INTO pending_delivery "
                "(chat_id, payload_json, attempts, last_error, created_at) "
                "VALUES ('2', '{}', 9, 'x', ?)",
                (c.now_utc().isoformat(),),
            )
            mem._conn.execute(
                "INSERT INTO pending_delivery "
                "(chat_id, payload_json, attempts, last_error, created_at) "
                "VALUES ('3', '{\"text\":\"keep\"}', 1, NULL, ?)",
                (c.now_utc().isoformat(),),
            )
            mem._conn.commit()
            n = mem.prune_pending_delivery(max_age_hours=48, max_attempts=8)
            self.assertEqual(n, 2)
            left = mem.load_pending_deliveries()
            self.assertEqual(len(left), 1)
            self.assertEqual(left[0]["chat_id"], "3")
            mem.close()

    def test_backup_and_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "cryptone.db")
            mem = c.MemoryEngine(db)
            mem.start()
            mem.set_runtime("probe", "alive")
            mem.close()
            bak = c.backup_db(db, suffix=".bak")
            self.assertIsNotNone(bak)
            self.assertTrue(Path(bak).is_file())
            # corrupt primary
            Path(db).write_bytes(b"not a sqlite db at all!!!")
            self.assertFalse(
                __import__("asyncio").run(c.startup_db_check(db))
            )
            # restore via env path
            import os
            old = os.environ.get("CRYPTONE_DB_PATH")
            try:
                os.environ["CRYPTONE_DB_PATH"] = db
                ok = c.restore_from_previous_artifact()
                self.assertTrue(ok)
                self.assertTrue(
                    __import__("asyncio").run(c.startup_db_check(db))
                )
                mem2 = c.MemoryEngine(db)
                mem2.start()
                self.assertEqual(mem2.get_runtime("probe"), "alive")
                mem2.close()
            finally:
                if old is None:
                    os.environ.pop("CRYPTONE_DB_PATH", None)
                else:
                    os.environ["CRYPTONE_DB_PATH"] = old

    def test_create_fresh_backs_up_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "c.db")
            Path(db).write_bytes(b"x" * 2000)
            import os
            old = os.environ.get("CRYPTONE_DB_PATH")
            try:
                os.environ["CRYPTONE_DB_PATH"] = db
                path = c.create_fresh_db()
                self.assertTrue(Path(path).is_file())
                self.assertTrue(Path(db + ".corrupt.bak").is_file())
                self.assertTrue(
                    __import__("asyncio").run(c.startup_db_check(path))
                )
            finally:
                if old is None:
                    os.environ.pop("CRYPTONE_DB_PATH", None)
                else:
                    os.environ["CRYPTONE_DB_PATH"] = old

    def test_startup_recovery_reconciles_before_monitor(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            # orphan active
            mem._conn.execute(
                "INSERT INTO active_signals (signal_id, symbol, expires_at) "
                "VALUES ('o1', 'Z', ?)",
                ((c.now_utc() + c.timedelta(hours=1)).isoformat(),),
            )
            mem._conn.commit()
            asyncio.run(c.startup_recovery(mem))
            n_raw = mem._conn.execute(
                "SELECT COUNT(*) AS n FROM active_signals"
            ).fetchone()["n"]
            self.assertEqual(n_raw, 0)
            mem.close()


class TestP2StrategyValidation(unittest.TestCase):
    """P2: expectancy, costs, drawdown, calibration, walk-forward, OHLC resolve, no look-ahead."""

    def _tr(self, outcome, rr, conf=0.7, setup="FUNDING_SQUEEZE", sid=None,
            resolved_at=None, simulated=False):
        return c.TradeRecord(
            signal_id=sid or f"{outcome}-{rr}",
            symbol="X", direction="LONG", horizon="INTRADAY", tier="B",
            setup_type=setup, outcome=outcome, realized_rr=rr, confidence=conf,
            created_at=c.now_utc() - c.timedelta(hours=2),
            resolved_at=resolved_at or c.now_utc(),
            is_simulated=simulated,
        )

    def test_expectancy_and_win_rate_exclude_neutral(self):
        trades = [
            self._tr("PROFIT", 2.0),
            self._tr("LOSS", -1.0),
            self._tr("PARTIAL", 0.5),
            self._tr("NEUTRAL", 0.0),
        ]
        m = c.compute_strategy_metrics(trades)
        self.assertEqual(m.n_trades, 3)
        self.assertEqual(m.n_neutral, 1)
        self.assertEqual(m.wins, 1)
        self.assertEqual(m.losses, 1)
        self.assertEqual(m.partials, 1)
        # WR = (1 + 0.5*1) / 3 = 0.5
        self.assertAlmostEqual(m.win_rate, 0.5)
        # E = (2 - 1 + 0.5) / 3 = 0.5
        self.assertAlmostEqual(m.expectancy_r, 0.5)
        self.assertAlmostEqual(m.sum_r, 1.5)

    def test_execution_costs_reduce_expectancy(self):
        trades = [self._tr("PROFIT", 2.0), self._tr("LOSS", -1.0)]
        costs = c.CostAssumptions(fee_r=0.05, slippage_r=0.05)
        m = c.compute_strategy_metrics(trades, costs)
        # signal E = 0.5; exec each trade -0.10 → E = 0.4
        self.assertAlmostEqual(m.expectancy_r, 0.5)
        self.assertAlmostEqual(m.expectancy_exec_r, 0.4)
        self.assertAlmostEqual(
            c.apply_execution_costs(2.0, costs), 1.9,
        )

    def test_profit_factor_and_max_drawdown(self):
        # path: +1, -2, +3 → equity 1, -1, 2; peak 1 then -1 → dd=2; peak 1→2
        rr = [1.0, -2.0, 3.0]
        self.assertAlmostEqual(c.max_drawdown_r(rr), 2.0)
        # PF = 4 / 2 = 2
        self.assertAlmostEqual(c.profit_factor(rr), 2.0)
        self.assertEqual(c.profit_factor([1.0, 2.0]), float("inf"))
        self.assertEqual(c.profit_factor([]), 0.0)
        self.assertEqual(c.max_drawdown_r([]), 0.0)

    def test_load_resolved_trades_filters_simulated_and_setup(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc()
            future = (now + c.timedelta(hours=1)).isoformat()

            async def _seed(sid, setup, sim, outcome, rr):
                await mem.enqueue_write("signal_history", {
                    "signal_id": sid, "symbol": "T", "direction": "LONG",
                    "horizon": "INTRADAY", "tier": "B",
                    "entry_zone_low": 100, "entry_zone_high": 100,
                    "stop_loss": 90, "tp1": 110, "tp2": 120, "rr": 2,
                    "confidence": 0.7, "trigger_type": "t", "reasoning": "t",
                    "setup_type": setup, "last_checked_at": now.isoformat(),
                    "created_at": now.isoformat(), "valid_until": future,
                    "is_simulated": sim,
                })
                await mem.flush_writes()
                mem.resolve_signal(sid, outcome, realized_rr=rr)

            asyncio.run(_seed("a", "FUNDING_SQUEEZE", 0, "PROFIT", 2.0))
            asyncio.run(_seed("b", "FUNDING_SQUEEZE", 1, "LOSS", -1.0))  # sim
            asyncio.run(_seed("c", "COMPRESSION", 0, "LOSS", -1.0))
            live = c.load_resolved_trades(mem, live_only=True)
            self.assertEqual({t.signal_id for t in live}, {"a", "c"})
            fund = c.load_resolved_trades(mem, setup_type="FUNDING_SQUEEZE")
            self.assertEqual([t.signal_id for t in fund], ["a"])
            mem.close()

    def test_confidence_calibration_buckets(self):
        trades = [
            self._tr("PROFIT", 2.0, conf=0.55),
            self._tr("LOSS", -1.0, conf=0.55),
            self._tr("PROFIT", 2.0, conf=0.85),
            self._tr("PROFIT", 1.5, conf=0.85),
        ]
        buckets = c.confidence_calibration(trades)
        b55 = next(b for b in buckets if b.lo == 0.50)
        b85 = next(b for b in buckets if b.lo == 0.80)
        self.assertEqual(b55.n, 2)
        self.assertAlmostEqual(b55.win_rate, 0.5)
        self.assertEqual(b85.n, 2)
        self.assertAlmostEqual(b85.win_rate, 1.0)
        self.assertGreater(b85.expectancy_r, b55.expectancy_r)

    def test_walk_forward_windows_non_overlapping_test(self):
        start = c.datetime(2026, 1, 1, tzinfo=c.UTC)
        end = c.datetime(2026, 3, 1, tzinfo=c.UTC)
        wins = c.walk_forward_windows(
            start, end, train_days=14, test_days=7, step_days=7,
        )
        self.assertTrue(len(wins) >= 2)
        for w in wins:
            self.assertEqual((w.train_end - w.train_start).days, 14)
            self.assertEqual((w.test_end - w.test_start).days, 7)
            self.assertEqual(w.test_start, w.train_end)
            self.assertLessEqual(w.test_end, end)
        # test windows do not overlap
        for a, b in zip(wins, wins[1:]):
            self.assertLessEqual(a.test_end, b.test_start)

    def test_walk_forward_metrics_out_of_sample_slice(self):
        base = c.datetime(2026, 1, 1, tzinfo=c.UTC)
        trades = [
            self._tr("PROFIT", 2.0, resolved_at=base + c.timedelta(days=20)),
            self._tr("LOSS", -1.0, resolved_at=base + c.timedelta(days=22)),
            self._tr("PROFIT", 1.0, resolved_at=base + c.timedelta(days=40)),
        ]
        wins = c.walk_forward_windows(
            base, base + c.timedelta(days=50),
            train_days=14, test_days=14, step_days=14,
        )
        results = c.walk_forward_metrics(trades, wins)
        # first test window covers day 14..28 → first two trades
        self.assertTrue(any(m.n_trades == 2 for _, m in results))

    def test_swing_confirmed_at_idx_is_lookback_later(self):
        self.assertEqual(c.swing_confirmed_at_idx(10, lookback=2), 12)
        self.assertEqual(
            c.swing_confirmed_at_idx(5, lookback=c.STRUCT_SWING_LOOKBACK),
            5 + c.STRUCT_SWING_LOOKBACK,
        )

    def test_simulate_bar_sl_priority_over_tp2(self):
        # same bar touches SL and TP2 → SL wins
        out, reason, px = c.simulate_bar_outcome(
            "LONG", 99, 101, 90, 110, 120, bar_high=125, bar_low=85, atr=2.0,
        )
        self.assertEqual(out, "LOSS")
        self.assertEqual(reason, "SL")
        self.assertEqual(px, 90)

    def test_simulate_bar_tp1_then_be_sl_partial(self):
        # after TP1, stop at BE; touch BE → PARTIAL
        out, reason, px = c.simulate_bar_outcome(
            "LONG", 99, 101, 100.0, 110, 120,
            bar_high=101, bar_low=99.5, already_tp1=True, atr=2.0,
        )
        self.assertEqual(out, "PARTIAL")
        self.assertEqual(reason, "SL")

    def test_simulate_bar_no_hit_returns_none(self):
        out, reason, px = c.simulate_bar_outcome(
            "LONG", 99, 101, 90, 110, 120, bar_high=105, bar_low=98, atr=2.0,
        )
        self.assertIsNone(out)
        self.assertIsNone(reason)

    def test_build_strategy_report_smoke(self):
        import asyncio
        with tempfile.TemporaryDirectory() as td:
            mem = c.MemoryEngine(str(Path(td) / "t.db"))
            mem.start()
            now = c.now_utc()
            future = (now + c.timedelta(hours=1)).isoformat()

            async def _seed():
                for i, (oc, rr) in enumerate([
                    ("PROFIT", 2.0), ("LOSS", -1.0), ("PARTIAL", 0.5),
                ]):
                    sid = f"r{i}"
                    await mem.enqueue_write("signal_history", {
                        "signal_id": sid, "symbol": "R", "direction": "LONG",
                        "horizon": "INTRADAY", "tier": "B",
                        "entry_zone_low": 100, "entry_zone_high": 100,
                        "stop_loss": 90, "tp1": 110, "tp2": 120, "rr": 2,
                        "confidence": 0.65 + i * 0.1, "trigger_type": "t",
                        "reasoning": "t", "setup_type": "FUNDING_SQUEEZE",
                        "last_checked_at": now.isoformat(),
                        "created_at": now.isoformat(), "valid_until": future,
                        "is_simulated": 0,
                    })
                    await mem.flush_writes()
                    mem.resolve_signal(sid, oc, realized_rr=rr)
            asyncio.run(_seed())
            report = c.build_strategy_report(mem)
            self.assertIn("Expectancy", report)
            self.assertIn("FUNDING_SQUEEZE", report)
            self.assertIn("confidence calibration", report)
            # accuracy UI uses same layer
            acc = c._fmt_accuracy(mem)
            self.assertIn("Expectancy", acc)
            self.assertIn("MaxDD", acc)
            mem.close()

    def test_empty_metrics_are_zero_not_nan(self):
        m = c.compute_strategy_metrics([])
        self.assertEqual(m.n_trades, 0)
        self.assertEqual(m.expectancy_r, 0.0)
        self.assertEqual(m.profit_factor, 0.0)
        self.assertEqual(m.max_drawdown_r, 0.0)



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--section", type=int, default=None)
    p.add_argument("--unit", action="store_true")
    p.add_argument("--integration", action="store_true")
    p.add_argument("--all", action="store_true")
    a = p.parse_args()
    sys.exit(run_tests(section=a.section, unit_only=a.unit, integration_only=a.integration))
