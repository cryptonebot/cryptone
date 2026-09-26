#!/usr/bin/env python3
"""
Orphan-checker v4 untuk CRYPTONE_V45_MASTER.md.

ast per-blok python. Membedakan:
  - Top-level calls: nama(  -- HARUS punya def di doc atau whitelist
  - Method calls:    .nama( -- whitelist method (didefinisikan di .py)
  - Exception classes di `except X:` / `except X as e:` -- HARUS punya
    def/class di doc atau whitelist (v3 tidak menangkap ini karena
    ast.ExceptHandler bukan ast.Call)

Blok yang bukan Python valid (pseudo-code, mis. "..." atau "[CHART: ...]")
di-skip dengan detail (nomor blok, error, baris pertama), bukan bikin
script crash.

Catatan filter privat: fungsi/method/exception berawalan "_" TIDAK
dilaporkan sebagai orphan (trade-off disengaja -- lihat --help).

Usage:
  python check_orphans.py CRYPTONE_V45_MASTER.md
  python check_orphans.py CRYPTONE_V45_MASTER.md --check-dead-defs
  python check_orphans.py CRYPTONE_V45_MASTER.md --audit-whitelist
  python check_orphans.py CRYPTONE_V45_MASTER.md --check-dead-defs --audit-whitelist

Exit code: 0 bersih, 1 ada orphan top-level (dead-defs/audit-whitelist tidak mempengaruhi exit code -- itu laporan informatif).
"""
import argparse
import ast
import re
import sys
from pathlib import Path

# Entry point yang wajar tidak pernah "dipanggil" di dalam doc itu sendiri
# (dipanggil dari CLI / GH Actions / __main__, bukan dari kode lain di doc)
ENTRY_POINTS = {
    "main", "run_live_service", "run_bootstrap", "build_parser",
    "run_orchestrator_cycle", "run_full_council_pipeline", "run_live_cycle",
}

# Fungsi/library yang aman disebut tanpa def (stdlib/3rd party/pseudo)
WHITELIST_STDLIB = {
    "print","len","range","str","int","float","list","dict","set","tuple","bool",
    "open","isinstance","getattr","setattr","hasattr","type","id","repr","abs",
    "min","max","sum","sorted","any","all","zip","enumerate","round",
    "clamp","percentile","compute_percentile",
    "asdict","dataclass","field","uuid4","sha256","to_thread",
    "utcnow","now","now_utc","now_wib","to_wib_iso","parse_dt",
    # builtin exceptions (masalah #3)
    "Exception","ValueError","TypeError","RuntimeError","KeyError",
    "AttributeError","IndexError","StopIteration","NotImplementedError",
    "AssertionError","TimeoutError","ConnectionError","OSError","IOError",
    "FileNotFoundError","ZeroDivisionError","ArithmeticError","LookupError",
}

WHITELIST_MODULES = {
    "plt","json","statistics","asyncio","sqlite3","logging","re","io","os","sys",
    "time","signal","math","random","hashlib","uuid","pandas","np","pd",
    "matplotlib","mplfinance",
}

# Method (dipanggil sebagai .x()) yang sengaja "kontrak .py", bukan orphan
WHITELIST_METHODS = {
    # MemoryEngine write/read
    "enqueue_write","flush_writes","start","checkpoint_wal",
    "get_active_signals","get_active_signal","remove_active_signal",
    "mark_tp1_hit","update_last_checked","set_signal_outcome",
    "record_skip","get_expired_signals","mark_signal_expired",
    "get_last_cycle","get_atr","get_atr_history",
    "extend_signal_validity",
    "set_flag","get_flag",
    "insert_blackswan_log",
    "get_last_anchor",
    # telegram
    "enqueue","send","run_sender_loop","start_sender_loop",
    # data fetch helpers (Bagian V, implementasi .py)
    "get_ohlcv","get_ohlcv_history","get_candles_since",
    "get_current_atr","get_current_volume_1h","get_median_volume_1h",
    "get_current_oi","get_oi_at","get_historical_oi_deltas",
    "get_volume_rank","get_volume_z_score","get_oi_z_score",
    "get_price_change_pct","get_funding_rate","get_funding_p95",
    "get_hl_price","get_binance_price",
    "get_baseline","save_baseline",
    "get_wallet_netflow","get_netflow_history",
    "get_market_anchor",
    # retention
    "downsample_ohlcv","purge_old_signals","purge_old_liquidations",
    "purge_old_health_logs","days_since_last_baseline_update",
    "update_all_volume_baselines",
    # db integrity
    "run_integrity_check",
    # notification
    "maybe_notify_resolution",
    # LLM
    "generate","record_call","get_cached","set_cached",
}

# Method stdlib umum -- dipisah dari WHITELIST_METHODS supaya method-orphan
# report tetap actionable (masalah #4), tapi tidak ikut dianggap "kontrak .py"
STDLIB_METHODS = {
    "execute","put","get","connect","sleep","Queue","correlation",
    "median","dumps","loads","signal","get_event_loop","create_task",
    "match","findall","finditer","sub","search","startswith","endswith",
    "isupper","islower","strip","lstrip","rstrip","lower","upper",
    "split","rsplit","join","append","extend","insert","pop","remove",
    "astimezone","add_argument","add_mutually_exclusive_group",
    "ArgumentParser","warning","info","debug","error","exception","critical",
    "total_seconds","isoformat","fromisoformat","keys","values","items",
    "update","copy","format","encode","decode","replace","fetchone",
    "fetchall","commit","rollback","close","cursor","read","write",
    "acquire","release","wait","cancel","result","done","add",
    "discard","clear","index","count","sort","reverse",
}

# Helper bebas yang dipakai lintas-bagian tapi boleh "kontrak .py"
WHITELIST_HELPERS = {
    "safe_json_loads","compute_realized_rr","hit_tp","hit_sl",
    "setup_logging","ensure_thread_pool","install_shutdown_handler",
    "parse_symbol_list","require","chunk","tiered_lookup",
    "ewma_reliability","classify_cap","get_session_spike",
    "build_war_room_messages",
}


def extract_from_ast(code: str, block_no: int):
    """
    Parse satu blok python. Return (defs, classes, top_calls, method_calls,
    except_names, ok).
    ok=False kalau blok gagal di-parse (pseudo-code) -- caller yang
    menangani skip.
    """
    defs, classes = set(), set()
    top_calls, method_calls = set(), set()
    except_names = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return defs, classes, top_calls, method_calls, except_names, False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                top_calls.add(func.id)
            elif isinstance(func, ast.Attribute):
                method_calls.add(func.attr)
        elif isinstance(node, ast.ExceptHandler) and node.type is not None:
            # except Foo: / except Foo as e: -> Foo
            # except (Foo, Bar): -> Foo, Bar
            candidates = (node.type.elts
                          if isinstance(node.type, ast.Tuple)
                          else [node.type])
            for c in candidates:
                if isinstance(c, ast.Name):
                    except_names.add(c.id)
                elif isinstance(c, ast.Attribute):
                    except_names.add(c.attr)
    return defs, classes, top_calls, method_calls, except_names, True


# Pola prosa: `tabel `nama_tabel`` atau "tabel nama_tabel" diikuti referensi
# bagian, mis. "tabel `bootstrap_meta` (Kelompok D — Mutable State, §VI.5)"
# atau "Tabel `news_volume_5m` (tambahan di §VI.2, ...)".
TABLE_REF_RE = re.compile(
    r"[Tt]abel\s+`?(\w+)`?\s*\([^)]*§[\w.]+[^)]*\)"
)
CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`?(\w+)`?", re.IGNORECASE
)


def check_table_refs(text: str):
    """
    Scan prosa untuk referensi 'tabel X (... §Y ...)' dan cek apakah X
    punya CREATE TABLE di suatu blok SQL manapun. Return sorted list of
    (nama_tabel, konteks_singkat) yang tidak ketemu definisinya.
    """
    referenced = {}
    for m in TABLE_REF_RE.finditer(text):
        name = m.group(1)
        if name not in referenced:
            start = max(0, m.start() - 20)
            referenced[name] = text[start:m.end()].replace("\n", " ").strip()

    sql_blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
    defined = set()
    for block in sql_blocks:
        defined |= set(CREATE_TABLE_RE.findall(block))

    missing = sorted(
        (name, ctx) for name, ctx in referenced.items() if name not in defined
    )
    return missing


def main():
    parser = argparse.ArgumentParser(
        description="Orphan-checker v4 untuk CRYPTONE_V45_MASTER.md (ast-based)."
    )
    parser.add_argument("master_md", help="Path ke file markdown master")
    parser.add_argument("--check-dead-defs", action="store_true",
                         help="Laporkan def yang tidak pernah dipanggil di doc (gap A)")
    parser.add_argument("--audit-whitelist", action="store_true",
                         help="Laporkan entry whitelist yang tidak pernah muncul di call manapun (gap D)")
    parser.add_argument("--check-table-refs", action="store_true",
                         help="Laporkan referensi tabel di prosa ('tabel X ... §Y') yang tidak punya CREATE TABLE (gap E)")
    args = parser.parse_args()

    path = Path(args.master_md)
    if not path.exists():
        print(f"ERROR: {path} tidak ditemukan")
        return 2

    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)

    defs, classes = set(), set()
    top_calls, method_calls, except_names = set(), set(), set()
    skipped = []

    for i, block in enumerate(blocks, 1):
        b_defs, b_classes, b_top, b_methods, b_except, ok = extract_from_ast(block, i)
        if not ok:
            try:
                ast.parse(block)
            except SyntaxError as e:
                first_line = next((ln for ln in block.splitlines() if ln.strip()), "")
                skipped.append((i, str(e), first_line.strip()[:60]))
            continue
        defs |= b_defs
        classes |= b_classes
        top_calls |= b_top
        method_calls |= b_methods
        except_names |= b_except

    safe = (WHITELIST_STDLIB | WHITELIST_MODULES | WHITELIST_HELPERS
            | WHITELIST_METHODS | STDLIB_METHODS | classes)

    # gap B: exception classes dipakai di `except X:` sekarang ikut dicek,
    # digabung ke top_orphans check (perlakuan sama seperti top-level call)
    all_referenced_names = top_calls | except_names

    top_orphans = sorted(
        c for c in (all_referenced_names - defs - safe)
        if not c.isupper() and not c.startswith("_")
    )
    method_orphans = sorted(
        m for m in (method_calls - defs - safe)
        if not m.startswith("_")
    )

    print(f"Blok python     : {len(blocks)} (parsed: {len(blocks) - len(skipped)}, skipped: {len(skipped)})")
    if skipped:
        print("  Blok di-skip (bukan Python valid):")
        for i, err, first_line in skipped:
            print(f"    - blok #{i}: {err} | baris pertama: {first_line!r}")
    print(f"Definisi (def)  : {len(defs)}")
    print(f"Definisi (class): {len(classes)}")
    print(f"Top-level calls : {len(top_calls)}")
    print(f"Method calls    : {len(method_calls)}")
    print(f"Exception refs  : {len(except_names)} (dari except clause)")
    print()
    print(f"Orphan top-level (termasuk exception class): {len(top_orphans)}")
    for name in top_orphans:
        print(f"  - {name}")
    print()
    print(f"Method orphan (cek: kontrak .py atau lupa whitelist): {len(method_orphans)}")
    for name in method_orphans:
        print(f"  - .{name}()")
    print()

    if args.check_dead_defs:
        called = top_calls | method_calls | except_names
        dead = sorted(d for d in defs if d not in called and d not in ENTRY_POINTS)
        print(f"Dead defs (def tapi tidak pernah dipanggil di doc, bukan entry point): {len(dead)}")
        for d in dead:
            print(f"  - {d}()")
        print("  Catatan: bisa jadi OK kalau pemanggil sebenarnya ada di .py, bukan di doc.")
        print()

    if args.audit_whitelist:
        all_calls_and_refs = top_calls | method_calls | except_names
        for label, wl in (
            ("WHITELIST_STDLIB", WHITELIST_STDLIB),
            ("WHITELIST_MODULES", WHITELIST_MODULES),
            ("WHITELIST_METHODS", WHITELIST_METHODS),
            ("WHITELIST_HELPERS", WHITELIST_HELPERS),
            ("STDLIB_METHODS", STDLIB_METHODS),
        ):
            unused = sorted(w for w in wl if w not in all_calls_and_refs)
            print(f"Whitelist audit -- {label}: {len(unused)}/{len(wl)} entry tidak pernah dipanggil")
            for u in unused:
                print(f"  - {u}")
        print()

    if args.check_table_refs:
        missing_tables = check_table_refs(text)
        print(f"Referensi tabel tanpa CREATE TABLE (gap E): {len(missing_tables)}")
        for name, ctx in missing_tables:
            print(f"  - {name}  |  konteks: ...{ctx}...")
        print("  Catatan: cek pola prosa 'tabel `X` (... §Y)'. Tidak menangkap semua gaya penulisan.")
        print()

    if top_orphans:
        print("AKSI: definisikan di dokumen, atau tambahkan ke WHITELIST_HELPERS/WHITELIST_STDLIB kalau memang kontrak .py / builtin.")
        return 1
    print("Bersih -- tidak ada orphan top-level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
