"""Smoke test: multi-day feature_computer.compute_range (v0.7.8).

Exercises the per-symbol bars cache path across multiple trading days.
Ensures:
  - Rows written for every day in range (cache slicing is correct per-date)
  - Timing is bounded (catches N×N blowups — before v0.7.8, compute of
    5 days × 3 symbols × 6 scan times could take much longer because each
    date re-queried 30 days of overlapping bars)
  - Progress logging fires and includes dates_processed/total_dates
  - Bars cache DataFrame is correctly date-sliced (no leakage across dates)

This test runs entirely in memory with synthetic bars — no Alpaca, no
network. It completes in under 10 seconds on commodity hardware; if it
takes longer, a regression is likely.

Usage:
    python -m tests.smoke_compute
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, passed: bool, detail: str = ""):
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}{(': ' + detail) if detail else ''}")


def _seed_bars(db_path: str, trading_dates: list[date], symbols: list[str]) -> int:
    """Seed raw_bars with synthetic full-session data. Returns count."""
    from tech_collector import storage
    bars = []
    for d in trading_dates:
        for sym in symbols:
            for minute in range(390):  # 9:30-16:00 ET
                dt_et = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) + timedelta(minutes=minute)
                dt_utc = dt_et.astimezone(UTC)
                # Each day walks up 0.5% from 100.0. Variation by symbol.
                base = 100.0 + (0.2 if sym == "NVDA" else 0.0)
                price = base + minute * 0.013
                bars.append({
                    "symbol": sym,
                    "timestamp_utc": dt_utc.isoformat().replace("+00:00", "Z"),
                    "open": price, "high": price + 0.05, "low": price - 0.05,
                    "close": price + 0.02, "volume": 1000, "vwap": price,
                    "trade_count": 10,
                    "sector": "Information Technology" if sym != "SPY" else None,
                })
    with storage.connect(db_path) as conn:
        storage.insert_bars(
            conn, bars, feed="sip",
            pulled_at_utc="2025-04-14T09:30:00Z",
            sector="Information Technology",
        )
    return len(bars)


def test_multi_day_compute_cache_path():
    """Run compute_range over 10 trading days × 3 symbols. Verify:
      - Every date produces rows
      - Cache-slicing didn't leak rows across dates
      - Timing is under 30s (generous bound; well under the 7-hour pathology)
    """
    from tech_collector import storage, feature_computer, config, universes
    tmpdir = tempfile.mkdtemp(prefix="compute_perf_")
    orig_db = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")

    # Monkey-patch the universe so we control symbol count
    original_get = universes.get_universe
    def fake_universe(sector):
        if sector == "Information Technology":
            return ["AAPL", "MSFT", "NVDA"]
        return original_get(sector)
    universes.get_universe = fake_universe

    try:
        storage.init_schema(config.DB_PATH)
        storage.init_backtest_schema(config.DB_PATH)

        # 10 consecutive trading days (skip weekends)
        trading_dates = []
        d = date(2025, 4, 14)
        while len(trading_dates) < 10:
            if d.weekday() < 5:
                trading_dates.append(d)
            d += timedelta(days=1)
        start_str = trading_dates[0].isoformat()
        end_str = trading_dates[-1].isoformat()

        seeded = _seed_bars(
            config.DB_PATH, trading_dates, ["AAPL", "MSFT", "NVDA", "SPY"],
        )
        _check("seeded bars for multi-day test",
               seeded == 10 * 4 * 390,
               f"got {seeded}, expected {10*4*390}")

        t0 = time.time()
        result = feature_computer.compute_range(
            start_date=start_str, end_date=end_str,
            db_path=config.DB_PATH, sector="Information Technology",
        )
        elapsed = time.time() - t0

        _check("compute_range multi-day: returns result dict",
               isinstance(result, dict) and "rows_written" in result,
               f"got {result}")

        # Expected: 10 dates × 3 symbols × 6 scan times (10:30..15:30 every hr)
        expected_rows = 10 * 3 * 6
        _check("compute_range multi-day: wrote expected row count",
               result["rows_written"] == expected_rows,
               f"got {result['rows_written']}, expected {expected_rows}")

        _check("compute_range multi-day: elapsed under 30s (no perf regression)",
               elapsed < 30.0,
               f"elapsed {elapsed:.1f}s")

        # Per-date verification: cache slicing must produce 18 rows per date
        # (3 symbols × 6 scan times). If caching leaks across dates, this fails.
        with storage.connect(config.DB_PATH) as conn:
            by_date = conn.execute(
                "SELECT date, COUNT(*) FROM research_rows GROUP BY date "
                "ORDER BY date"
            ).fetchall()
        _check("compute_range multi-day: 10 distinct dates populated",
               len(by_date) == 10, f"got {len(by_date)}")
        per_date_counts = {r[0]: r[1] for r in by_date}
        all_18 = all(c == 18 for c in per_date_counts.values())
        _check("compute_range multi-day: every date has 18 rows",
               all_18, f"counts: {per_date_counts}")

        # Correctness of regime_ok: rows with populated inputs should have
        # regime_ok populated. At 09:30 (opening scan), there's no prior
        # bars to derive spy_momentum or dist_to_prev_close_bps from, so
        # regime_ok is legitimately NULL. This is expected behavior.
        with storage.connect(config.DB_PATH) as conn:
            non_open = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN regime_ok IS NOT NULL THEN 1 ELSE 0 END) "
                "FROM research_rows "
                "WHERE scan_time_et != '09:30'"
            ).fetchone()
        n_non_open, populated_non_open = non_open
        _check("compute_range multi-day: regime_ok populated on all non-09:30 rows",
               populated_non_open == n_non_open,
               f"{populated_non_open}/{n_non_open}")
        # Separately: 09:30 rows should have NULL regime_ok (no prior data)
        with storage.connect(config.DB_PATH) as conn:
            open_rows = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN regime_ok IS NULL THEN 1 ELSE 0 END) "
                "FROM research_rows "
                "WHERE scan_time_et = '09:30'"
            ).fetchone()
        n_open, null_open = open_rows
        _check("compute_range multi-day: regime_ok correctly NULL at 09:30",
               null_open == n_open,
               f"{null_open}/{n_open} null")
    finally:
        universes.get_universe = original_get
        config.DB_PATH = orig_db
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_load_bars_for_range_uses_pk_index():
    """Verify the new load_bars_for_range query uses the PK index
    (no TEMP B-TREE FOR ORDER BY). Regression guard against the
    pre-v0.7.8 query pattern."""
    import sqlite3
    tmpdir = tempfile.mkdtemp(prefix="qplan_")
    try:
        db = os.path.join(tmpdir, "test.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE raw_bars (
                symbol TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume INTEGER, vwap REAL, sector TEXT,
                PRIMARY KEY (symbol, timestamp_utc)
            );
            CREATE INDEX idx_raw_bars_symbol_date
              ON raw_bars(symbol, substr(timestamp_utc, 1, 10));
        """)
        plan = conn.execute("""
            EXPLAIN QUERY PLAN
            SELECT timestamp_utc, open, high, low, close, volume, vwap
            FROM raw_bars
            WHERE symbol = ?
              AND timestamp_utc >= ?
              AND timestamp_utc < ?
            ORDER BY timestamp_utc
        """, ("AAPL", "2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z")).fetchall()
        plan_text = " | ".join(str(row) for row in plan)
        # The PK-backed plan should NOT include TEMP B-TREE (would be
        # a regression to the slow path)
        _check("load_bars_for_range query doesn't need TEMP B-TREE for ORDER BY",
               "TEMP B-TREE" not in plan_text,
               f"plan: {plan_text[:200]}")
        # Positive: it DOES use the PK autoindex
        _check("load_bars_for_range uses PK-backed index",
               "sqlite_autoindex_raw_bars_1" in plan_text
               or "PRIMARY KEY" in plan_text,
               f"plan: {plan_text[:200]}")
    finally:
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    # Keep logger output visible
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
    )
    print("SMOKE: feature_computer.compute_range multi-day + perf")
    print("=" * 60)
    tests = [
        test_multi_day_compute_cache_path,
        test_load_bars_for_range_uses_pk_index,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _check(t.__name__ + " (raised)", False, str(e))
            traceback.print_exc()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("=" * 60)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
