"""Offline smoke test: backtest_audit + _simulate_trade fix.

Validates the v0.7.1 fix for the phantom-TIME-exit bug.

Critical regression test: a bar series where a stock moves +30% after
crossing TP MUST return exit_reason=TP, not TIME. This is the exact
failure mode we observed in production (IT 2026-02-03 and 475 others),
where the v0.7.0 engine returned phantom TIME exits with returns far
outside TP/SL bounds.

Usage:
    python -m tests.smoke_backtest_audit
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _bar(ts_utc: str, o: float, h: float, l: float, c: float, v: int = 1_000_000) -> dict:
    return {"timestamp_utc": ts_utc, "open": o, "high": h,
            "low": l, "close": c, "volume": v}


RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, passed: bool, detail: str = ""):
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}{(': ' + detail) if detail else ''}")


def test_regression_big_move_must_be_TP():
    """Exact reproduction of the production bug.

    Entry at 10:30 ET on 2026-02-03 (EST, UTC-5). Stock crosses TP at
    10:40 ET and rockets to +30% by close. Old engine returned TIME
    with gross=+3037 bps. New engine must return TP.
    """
    from tech_collector.backtest_audit import _simulate_trade_reference

    result = _simulate_trade_reference(
        bars=[
            _bar("2026-02-03T15:30:00Z", 157.165, 157.30, 157.00, 157.20),
            _bar("2026-02-03T15:31:00Z", 157.20, 157.50, 157.15, 157.40),
            _bar("2026-02-03T15:40:00Z", 157.40, 158.50, 157.30, 158.40),
            _bar("2026-02-03T15:45:00Z", 158.40, 180.00, 158.20, 179.00),
            _bar("2026-02-03T20:00:00Z", 199.00, 202.00, 198.00, 201.84),
        ],
        entry_ts_utc="2026-02-03T15:30:00Z",
        entry_price=157.165, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:30", slippage_bps=15.0,
    )
    _check("regression: big move → TP not TIME",
           result["exit_reason"] == "TP",
           f"got exit_reason={result['exit_reason']}, gross={result['gross_return_bps']:.1f}")
    _check("regression: gross return within TP range",
           abs(result["gross_return_bps"] - 75.0) < 30,
           f"got gross={result['gross_return_bps']:.1f}, expected ~75")


def test_time_exit_invariant():
    """If bars contain no TP/SL crossings but the last close is far
    outside bounds (data-integrity issue), the invariant raises."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    try:
        _simulate_trade_reference(
            bars=[{"timestamp_utc": "2026-02-03T15:35:00Z",
                   "open": 100.0, "high": 100.3, "low": 99.8,
                   "close": 130.0, "volume": 1000}],
            entry_ts_utc="2026-02-03T15:30:00Z",
            entry_price=100.0, tp_level=75.0, sl_level=100.0,
            timestop_et_hhmm="15:30", slippage_bps=15.0,
        )
        _check("invariant raises on bad data", False,
               "expected AssertionError but got clean result")
    except AssertionError as e:
        _check("invariant raises on bad data", "TIME exit invariant" in str(e),
               f"raised: {str(e)[:80]}")


def test_clean_TP_exit():
    from tech_collector.backtest_audit import _simulate_trade_reference
    result = _simulate_trade_reference(
        bars=[
            _bar("2025-06-03T14:30:00Z", 100.0, 100.3, 99.9, 100.1),
            _bar("2025-06-03T14:31:00Z", 100.1, 100.9, 100.0, 100.85),
        ],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:30", slippage_bps=15.0,
    )
    _check("clean TP exit", result["exit_reason"] == "TP",
           f"got {result['exit_reason']}")


def test_clean_SL_exit():
    from tech_collector.backtest_audit import _simulate_trade_reference
    result = _simulate_trade_reference(
        bars=[
            _bar("2025-06-03T14:30:00Z", 100.0, 100.1, 99.5, 99.7),
            _bar("2025-06-03T14:31:00Z", 99.7, 99.8, 98.9, 99.0),
        ],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:30", slippage_bps=15.0,
    )
    _check("clean SL exit", result["exit_reason"] == "SL",
           f"got {result['exit_reason']}")


def test_clean_TIME_exit_within_bounds():
    """Legitimate TIME exit (stock ranges tight all session) has gross
    within [-SL, +TP] and passes the invariant."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    result = _simulate_trade_reference(
        bars=[
            _bar("2025-06-03T14:30:00Z", 100.0, 100.2, 99.9, 100.0),
            _bar("2025-06-03T15:00:00Z", 100.0, 100.3, 99.9, 100.1),
            _bar("2025-06-03T18:00:00Z", 100.1, 100.4, 99.8, 100.2),
            _bar("2025-06-03T19:30:00Z", 100.2, 100.3, 99.9, 100.15),  # 15:30 EDT
        ],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:30", slippage_bps=15.0,
    )
    _check("TIME exit legit",
           result["exit_reason"] == "TIME",
           f"got {result['exit_reason']}")
    _check("TIME exit gross within bounds",
           -101.0 <= result["gross_return_bps"] <= 76.0,
           f"got gross={result['gross_return_bps']:.2f}")


def test_tp_sl_same_bar_resolves_SL():
    from tech_collector.backtest_audit import _simulate_trade_reference
    result = _simulate_trade_reference(
        bars=[_bar("2025-06-03T14:30:00Z", 100.0, 101.0, 98.9, 100.2)],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:30", slippage_bps=15.0,
    )
    _check("same-bar TP+SL resolves to SL",
           result["exit_reason"] == "SL",
           f"got {result['exit_reason']}")


def test_timezone_EST_Feb():
    """February is EST. 10:30 ET = 15:30 UTC."""
    from tech_collector.backtest_audit import _signal_time_to_utc_iso
    iso = _signal_time_to_utc_iso("2026-02-03", "10:30")
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    _check("Feb 10:30 ET = 15:30 UTC (EST)",
           dt.hour == 15 and dt.minute == 30,
           f"got {dt.hour}:{dt.minute:02d}")


def test_timezone_EDT_Jun():
    """June is EDT. 10:30 ET = 14:30 UTC."""
    from tech_collector.backtest_audit import _signal_time_to_utc_iso
    iso = _signal_time_to_utc_iso("2025-06-03", "10:30")
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    _check("Jun 10:30 ET = 14:30 UTC (EDT)",
           dt.hour == 14 and dt.minute == 30,
           f"got {dt.hour}:{dt.minute:02d}")


def test_timezone_DST_Mar_boundary():
    """2025 DST starts Mar 9. Mar 5 = EST, Mar 15 = EDT."""
    from tech_collector.backtest_audit import _signal_time_to_utc_iso
    iso_est = _signal_time_to_utc_iso("2025-03-05", "10:30")
    iso_edt = _signal_time_to_utc_iso("2025-03-15", "10:30")
    dt_est = datetime.fromisoformat(iso_est.replace("Z", "+00:00"))
    dt_edt = datetime.fromisoformat(iso_edt.replace("Z", "+00:00"))
    _check("Mar 5 2025 pre-DST (EST, UTC-5)",
           dt_est.hour == 15, f"got {dt_est.hour}")
    _check("Mar 15 2025 post-DST (EDT, UTC-4)",
           dt_edt.hour == 14, f"got {dt_edt.hour}")


def test_timezone_DST_Nov_boundary():
    """2025 DST ends Nov 2. Oct 28 = EDT, Nov 5 = EST."""
    from tech_collector.backtest_audit import _signal_time_to_utc_iso
    iso_edt = _signal_time_to_utc_iso("2025-10-28", "10:30")
    iso_est = _signal_time_to_utc_iso("2025-11-05", "10:30")
    dt_edt = datetime.fromisoformat(iso_edt.replace("Z", "+00:00"))
    dt_est = datetime.fromisoformat(iso_est.replace("Z", "+00:00"))
    _check("Oct 28 2025 pre-DST-end (EDT, UTC-4)",
           dt_edt.hour == 14, f"got {dt_edt.hour}")
    _check("Nov 5 2025 post-DST-end (EST, UTC-5)",
           dt_est.hour == 15, f"got {dt_est.hour}")


# ---------------------------------------------------------------------------
# End-to-end DB test: audit + repair against a synthetic backtest DB.
# Catches the "length 25; 2 is required" class of bug (storage-shape
# mismatches, wrong function names, missing row_factory, etc.).
# ---------------------------------------------------------------------------
def _setup_synthetic_db():
    """Create a minimal backtest DB with one run, one phantom-TIME trade,
    and raw_bars that show the stock actually crossed TP."""
    import tempfile, os
    from tech_collector import storage, config as _config

    tmpdir = tempfile.mkdtemp(prefix="audit_smoke_")
    db = os.path.join(tmpdir, "test.db")
    # Point config at our test DB for the duration of the test
    _orig_db_path = _config.DB_PATH
    _config.DB_PATH = db

    storage.init_schema(db)
    storage.init_backtest_schema(db)

    # Insert a run + one trade that was stored as "TIME" with huge gross.
    # Matches the production bug: entry at 10:30 ET 2026-02-03, stock goes
    # +30% but stored as TIME (rather than TP).
    run_uuid = "test-run-abc123"
    with storage.connect(db) as conn:
        storage.record_backtest_run(conn, {
            "run_uuid": run_uuid,
            "rule_json": '{"id":"test","sector":"Information Technology"}',
            "tp_bps": 75.0, "sl_bps": 100.0,
            "timestop_et": "15:30", "slippage_bps": 15.0,
            "spy_regime_filter": None, "symbol_exclude": None,
            "start_date": "2026-02-03", "end_date": "2026-02-03",
            "generated_at_utc": "2026-02-03T20:00:00Z",
            "n_signals_total": 1, "n_signals_skipped": 0, "n_trades": 1,
            "net_pnl_bps": 3000.0, "win_rate": 1.0,
            "notes": "test run with phantom TIME",
            "conditional_exits_json": None,
        })
        # The phantom trade: exit_reason=TIME with gross=+3000 bps
        storage.insert_backtest_trades(conn, run_uuid, [{
            "symbol": "IT", "signal_date": "2026-02-03", "signal_time_et": "10:30",
            "entry_price": 100.0, "exit_price": 130.0, "exit_time_et": "19:04",
            "exit_reason": "TIME", "minutes_held": 0,
            "gross_return_bps": 3000.0, "net_return_bps": 3000.0,
            "branch_label": "gap_open", "position_size": 1.0,
            "tp_bps_used": 75.0, "sl_bps_used": 100.0,
        }])
        # Raw bars that show the stock ACTUALLY crossed TP within minutes
        # of entry. Entry at 10:30 ET = 15:30 UTC (EST).
        bars_to_insert = [
            # entry bar: 15:30 UTC, price stays flat
            {"symbol": "IT", "timestamp_utc": "2026-02-03T15:30:00Z",
             "open": 100.0, "high": 100.2, "low": 99.9, "close": 100.1,
             "volume": 1000, "vwap": 100.0, "trade_count": 10, "sector": "IT"},
            # 15:35: crosses TP (100.75)
            {"symbol": "IT", "timestamp_utc": "2026-02-03T15:35:00Z",
             "open": 100.1, "high": 101.0, "low": 100.0, "close": 100.8,
             "volume": 2000, "vwap": 100.5, "trade_count": 20, "sector": "IT"},
            # 15:40: runs higher
            {"symbol": "IT", "timestamp_utc": "2026-02-03T15:40:00Z",
             "open": 100.8, "high": 110.0, "low": 100.7, "close": 109.5,
             "volume": 5000, "vwap": 105.0, "trade_count": 50, "sector": "IT"},
            # 20:00 UTC = 15:00 ET (just before timestop)
            {"symbol": "IT", "timestamp_utc": "2026-02-03T20:00:00Z",
             "open": 129.0, "high": 131.0, "low": 128.5, "close": 130.0,
             "volume": 3000, "vwap": 130.0, "trade_count": 40, "sector": "IT"},
        ]
        storage.insert_bars(conn, bars_to_insert,
                            feed="sip", pulled_at_utc="2026-02-03T20:05:00Z",
                            sector="Information Technology")

    return db, run_uuid, tmpdir, _orig_db_path


def _teardown_db(tmpdir: str, orig_db_path: str):
    import shutil
    from tech_collector import config as _config
    _config.DB_PATH = orig_db_path
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_audit_run_end_to_end():
    """audit_run must read the phantom trade, compare to reference, and
    flag it as suspect (stored TIME, reference would produce TP)."""
    from tech_collector import backtest_audit
    db, run_uuid, tmpdir, orig = _setup_synthetic_db()
    try:
        report = backtest_audit.audit_run(
            db_path=db, run_uuid=run_uuid, jit_backfill=False,
        )
        _check("audit_run: completes without error",
               isinstance(report, dict) and "n_trades" in report,
               f"got {list(report.keys())[:5]}")
        _check("audit_run: detects 1 suspect TIME exit",
               report["n_suspect_time_exits"] == 1,
               f"got {report['n_suspect_time_exits']}")
        _check("audit_run: detects reason mismatch",
               report["n_reason_mismatch"] == 1,
               f"got {report['n_reason_mismatch']}")
        _check("audit_run: records TIME→TP transition",
               report["exit_reason_transitions"].get("TIME→TP") == 1,
               f"got {report['exit_reason_transitions']}")
    finally:
        _teardown_db(tmpdir, orig)


def test_repair_run_end_to_end():
    """repair_run must write a new run with corrected trades (TP instead
    of TIME) and leave the original untouched."""
    from tech_collector import backtest_audit, storage as st
    db, run_uuid, tmpdir, orig = _setup_synthetic_db()
    try:
        new_uuid = backtest_audit.repair_run(
            db_path=db, source_run_uuid=run_uuid, jit_backfill=False,
        )
        _check("repair_run: returns new uuid", bool(new_uuid) and new_uuid != run_uuid,
               f"got {new_uuid}")

        # Verify original is unchanged
        with st.connect(db) as conn:
            orig_trades = st.get_backtest_trades(conn, run_uuid)
            new_trades = st.get_backtest_trades(conn, new_uuid)
        _check("repair_run: original trade reason unchanged",
               orig_trades[0]["exit_reason"] == "TIME",
               f"got {orig_trades[0]['exit_reason']}")
        _check("repair_run: new trade reason is TP",
               new_trades[0]["exit_reason"] == "TP",
               f"got {new_trades[0]['exit_reason']}")
        _check("repair_run: new trade net within TP range",
               abs(new_trades[0]["net_return_bps"] - 75.0) < 30,
               f"got {new_trades[0]['net_return_bps']:.2f}")
    finally:
        _teardown_db(tmpdir, orig)


# ---------------------------------------------------------------------------
# v0.7.7: no-timestop mode tests
# ---------------------------------------------------------------------------
def test_no_timestop_tp_still_works():
    """With timestop=None, a TP crossing still exits on TP (never triggers
    phantom TIME). Entry 10:30 ET; TP hit at 11:00 ET; timestop disabled."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    result = _simulate_trade_reference(
        bars=[
            _bar("2025-06-03T14:30:00Z", 100.0, 100.3, 99.9, 100.1),
            _bar("2025-06-03T15:00:00Z", 100.1, 101.0, 100.0, 100.9),
        ],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm=None, slippage_bps=15.0,
    )
    _check("no_timestop + TP hit → TP exit",
           result["exit_reason"] == "TP",
           f"got {result['exit_reason']}, gross={result['gross_return_bps']:.2f}")


def test_no_timestop_trade_never_hits_exits_as_TIME_within_bounds():
    """With timestop=None, a trade that never hits TP/SL should still exit
    as TIME at last bar's close, AND the invariant must hold (last close
    within [-SL, +TP] bounds). This is the critical edge case: we disabled
    the timestop; the fallback path MUST still produce a legitimate outcome."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    # Stock ranges tight all session, well within bounds. Bars extend past
    # what the old 15:30 timestop would have caught.
    bars = [
        _bar("2025-06-03T14:30:00Z", 100.0, 100.2, 99.9, 100.0),
        _bar("2025-06-03T15:00:00Z", 100.0, 100.3, 99.9, 100.1),
        _bar("2025-06-03T18:00:00Z", 100.1, 100.4, 99.8, 100.2),
        _bar("2025-06-03T19:30:00Z", 100.2, 100.3, 99.9, 100.15),  # 15:30 ET
        _bar("2025-06-03T19:50:00Z", 100.15, 100.3, 99.9, 100.2),  # 15:50 ET
        _bar("2025-06-03T19:59:00Z", 100.2, 100.35, 99.95, 100.25),  # 15:59 ET
    ]
    result = _simulate_trade_reference(
        bars=bars, entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm=None, slippage_bps=15.0,
    )
    _check("no_timestop + no level hit → TIME exit",
           result["exit_reason"] == "TIME",
           f"got {result['exit_reason']}")
    _check("no_timestop TIME exit respects invariant",
           -101.0 <= result["gross_return_bps"] <= 76.0,
           f"got gross={result['gross_return_bps']:.2f}")


def test_no_timestop_runs_past_old_timestop_into_TP():
    """Verify the new mode lets trades run past 15:30 ET. Tight range until
    15:55 ET, then TP hits at 15:58 ET. The old 15:30 timestop would have
    exited flat; with no timestop, we capture the TP."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    bars = [
        _bar("2025-06-03T14:30:00Z", 100.0, 100.2, 99.9, 100.05),
        _bar("2025-06-03T19:30:00Z", 100.05, 100.3, 99.9, 100.1),   # 15:30 ET — old timestop would fire here
        _bar("2025-06-03T19:40:00Z", 100.1, 100.3, 99.9, 100.15),
        _bar("2025-06-03T19:55:00Z", 100.15, 100.4, 100.1, 100.3),
        _bar("2025-06-03T19:58:00Z", 100.3, 101.0, 100.3, 100.9),  # TP = 100.83ish → HIT
    ]
    result = _simulate_trade_reference(
        bars=bars, entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm=None, slippage_bps=15.0,
    )
    _check("no_timestop: trade continues past old 15:30 window",
           result["exit_reason"] == "TP",
           f"got {result['exit_reason']}, gross={result['gross_return_bps']:.2f}")


def test_no_timestop_empty_string_is_treated_as_disabled():
    """Empty string timestop should behave identically to None — the API
    may pass "" for legacy reasons."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    # Same as test_no_timestop_tp_still_works but with "" instead of None
    result = _simulate_trade_reference(
        bars=[
            _bar("2025-06-03T14:30:00Z", 100.0, 100.3, 99.9, 100.1),
            _bar("2025-06-03T15:00:00Z", 100.1, 101.0, 100.0, 100.9),
        ],
        entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="", slippage_bps=15.0,
    )
    _check('empty-string timestop = disabled',
           result["exit_reason"] == "TP",
           f"got {result['exit_reason']}")


def test_timestop_1550_fires_after_1530():
    """Verify the new default (15:50) actually triggers at 15:50 and not
    before. A trade flat through 15:49 should hold; at 15:50 it exits TIME."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    bars = [
        _bar("2025-06-03T14:30:00Z", 100.0, 100.2, 99.9, 100.05),
        _bar("2025-06-03T19:30:00Z", 100.05, 100.2, 99.95, 100.1),  # 15:30 — old default
        _bar("2025-06-03T19:49:00Z", 100.1, 100.2, 99.95, 100.15),  # 15:49
        _bar("2025-06-03T19:50:00Z", 100.15, 100.3, 100.0, 100.2),  # 15:50 — new default
    ]
    result = _simulate_trade_reference(
        bars=bars, entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:50", slippage_bps=15.0,
    )
    _check("timestop 15:50 fires at 15:50 bar",
           result["exit_reason"] == "TIME" and result["exit_time_et"] == "15:50",
           f"got reason={result['exit_reason']}, time={result['exit_time_et']}")


def test_timestop_1550_does_not_fire_at_1530():
    """Regression check: with timestop=15:50, the 15:30 bar must NOT trigger
    TIME. It would under the old default. Trade continues to 15:50."""
    from tech_collector.backtest_audit import _simulate_trade_reference
    bars = [
        _bar("2025-06-03T14:30:00Z", 100.0, 100.2, 99.9, 100.05),
        _bar("2025-06-03T19:30:00Z", 100.05, 100.2, 99.95, 100.1),   # 15:30 ET
        _bar("2025-06-03T19:35:00Z", 100.1, 100.9, 100.05, 100.85),  # TP hit inside this bar (TP=100.83)
    ]
    result = _simulate_trade_reference(
        bars=bars, entry_ts_utc="2025-06-03T14:30:00Z",
        entry_price=100.0, tp_level=75.0, sl_level=100.0,
        timestop_et_hhmm="15:50", slippage_bps=15.0,
    )
    _check("15:50 timestop: 15:30 bar doesn't force-exit",
           result["exit_reason"] == "TP",
           f"got {result['exit_reason']}")


def test_v0711_phantom_bug_regression():
    """v0.7.11 regression test for the canonical phantom-TIME bug.

    The bug: get_raw_bars_for_day used `date(timestamp_utc)` (UTC date),
    which leaked in prior-day after-hours bars (UTC 00:00-05:00 = ET 19:00-
    00:00 of the previous day). _find_scan_bar_ts then matched the FIRST
    bar with ET hour >= target — which on a typical day was the previous
    day's 19:00 ET after-hours bar appearing first in the UTC-sorted list.
    The simulator anchored "scan time" to that bogus bar, fired the timestop
    on the next iteration, and wrote phantom-TIME exits with bogus gross
    P&L from after-hours price gaps.

    This test injects the exact production condition: bars for an ET
    trading date (Feb 3 2026) PLUS prior-day after-hours bars whose UTC
    date equals the requested date. Pre-fix, the engine produces an
    exit_time_et like "19:00" with minutes_held=0. Post-fix, it produces
    exit_time_et "15:50" with minutes_held=320.
    """
    import tempfile, shutil, os
    from datetime import date as _date
    from tech_collector import config, storage

    tmpdir = tempfile.mkdtemp(prefix="phantom_regr_")
    db = os.path.join(tmpdir, "test.db")
    orig_db = config.DB_PATH
    config.DB_PATH = db
    try:
        storage.init_schema(db)
        storage.init_backtest_schema(db)

        bars = []
        # Feb 2 2026 evening after-hours: ET 19:00-19:59 = UTC 00:00-00:59 Feb 3
        # These bars have UTC date = "2026-02-03" but ET date = "2026-02-02"
        for utc_min in range(0, 60, 5):
            bars.append({
                "symbol": "IT",
                "timestamp_utc": f"2026-02-03T00:{utc_min:02d}:00Z",
                "open": 200.0, "high": 201.0, "low": 199.5, "close": 200.5,
                "volume": 100, "vwap": 200.0, "trade_count": 5,
                "sector": "Information Technology",
            })
        # Feb 3 regular session: ET 09:30-15:59 = UTC 14:30-20:59 (winter)
        # Stock walks flat — no TP/SL hit. Timestop at 15:50 should fire.
        for utc_h in range(14, 21):
            for utc_m in range(0, 60):
                if utc_h == 14 and utc_m < 30:
                    continue
                if utc_h == 21 and utc_m > 0:
                    continue
                bars.append({
                    "symbol": "IT",
                    "timestamp_utc": f"2026-02-03T{utc_h:02d}:{utc_m:02d}:00Z",
                    "open": 154.7, "high": 154.85, "low": 154.6, "close": 154.7,
                    "volume": 1000, "vwap": 154.7, "trade_count": 50,
                    "sector": "Information Technology",
                })
        # SPY for regime features
        for utc_h in range(14, 21):
            for utc_m in range(0, 60, 30):
                if utc_h == 14 and utc_m < 30:
                    continue
                bars.append({
                    "symbol": "SPY",
                    "timestamp_utc": f"2026-02-03T{utc_h:02d}:{utc_m:02d}:00Z",
                    "open": 500.0, "high": 500.5, "low": 499.5, "close": 500.0,
                    "volume": 1000000, "vwap": 500.0, "trade_count": 1000,
                    "sector": None,
                })

        with storage.connect(db) as conn:
            storage.insert_bars(
                conn, bars, feed="sip",
                pulled_at_utc="2026-02-03T22:00:00Z",
                sector="Information Technology",
            )

        # Insert minimal research_row so the rule fires
        rr = {
            "symbol": "IT", "date": "2026-02-03", "scan_time_et": "10:30",
            "sector": "Information Technology",
            "minutes_since_open": 60, "scan_price": 154.7,
            "open_to_scan_return": 0.001, "gap_pct": 0.0,
            "intraday_range_position": 0.5,
            "distance_to_vwap": 0.0, "distance_to_day_high": 0.0,
            "distance_to_day_low": 0.0,
            "rsi_14": 60.0, "macd_hist": 0.1,
            "ema_9_distance": 0.001, "ema_20_distance": 0.001,
            "ema_50_distance": 0.001,
            "relative_volume": 1.5, "realized_vol_so_far": 0.005,
            "sector_relative_strength": 0.0,
            "day_of_week": "Tue", "cutoff_time_et": "15:30",
            "cutoff_price": 154.7,
            "return_to_cutoff": 0.0, "min_return_before_cutoff": -0.001,
            "max_return_before_cutoff": 0.001, "rs_leakfree": 0.0,
            "return_at_scan_plus_30m": 0.0, "return_at_scan_plus_60m": 0.0,
            "return_at_scan_plus_90m": 0.0, "return_at_scan_plus_120m": 0.0,
            "bars_missing_pre_scan": 0, "bars_missing_post_scan": 0,
            "feed_source": "sip", "pulled_at_utc": "2026-02-03T22:00:00Z",
            "momentum": 0.005, "rel_volume_r2k": 1.5, "vwap_slope": 0.001,
            "orb_strength": 0.0,
            "atr_reach": 1.0, "spy_ret": 0.001, "ret_vs_spy": 0.0,
            "spy_momentum": 0.001, "mom_vs_spy": 0.001, "spy_vol": 0.005,
            "gap_filled": 0, "range_tightness_30m": 0.005,
            "bars_in_range_20bps": 5, "is_nr7": 0,
            "dist_to_day_high_bps": 50, "broke_day_high_this_bar": 0,
            "broke_opening_range_high": 0, "bars_since_day_high": 30,
            "dist_to_prev_close_bps": 50, "dist_to_5d_high_bps": 100,
            "dist_to_20d_high_bps": 200, "days_since_20d_high": 5,
            "volume_acceleration": 1.0, "cumulative_volume_vs_typical": 1.0,
            "sector_breadth_up": 0.5, "new_highs_in_sector": 5,
            "target_25bps": 1, "target_peak_25bps": 1,
            "target_50bps": 1, "target_peak_50bps": 1,
            "target_75bps": 1, "target_peak_75bps": 1, "target": 1,
            "regime_ok": 1,
        }
        with storage.connect(db) as conn:
            storage.insert_research_rows(conn, [rr])

        from tech_collector import rule_tester, backtest as _bt
        rule = rule_tester.Rule.from_dict({
            "id": "phantom-regr-test",
            "sector": "Information Technology",
            "target": "target_peak_75bps",
            "predicates": [{"feature": "momentum", "op": ">", "value": 0.002}],
        })
        bt_config = _bt.BacktestConfig(
            rule=rule, tp_bps=75.0, sl_bps=100.0, timestop_et="15:50",
            slippage_bps=15.0, just_in_time_backfill=False,
            conditional_exits=[],
        )
        result = _bt.run_backtest(bt_config, db_path=db)
        with storage.connect(db) as conn:
            trades = storage.get_backtest_trades(conn, result["run_uuid"])

        _check("phantom-regr: produced exactly 1 trade",
               len(trades) == 1, f"got {len(trades)}")
        if not trades:
            return
        t = trades[0]
        _check("phantom-regr: exit_time_et is 15:50 (timestop), NOT 19:xx",
               t["exit_time_et"] == "15:50",
               f"got exit_time_et={t['exit_time_et']!r}")
        _check("phantom-regr: TIME exit has nonzero minutes_held",
               t["minutes_held"] > 0,
               f"got minutes_held={t['minutes_held']}")
        _check("phantom-regr: minutes_held is ~320 (10:30 → 15:50)",
               300 <= t["minutes_held"] <= 340,
               f"got minutes_held={t['minutes_held']}")
        _check("phantom-regr: gross within invariant bounds",
               -101 <= t["gross_return_bps"] <= 76,
               f"got gross={t['gross_return_bps']:.2f}")
        _check("phantom-regr: exit_reason is TIME",
               t["exit_reason"] == "TIME",
               f"got exit_reason={t['exit_reason']!r}")
    finally:
        config.DB_PATH = orig_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_v0711_get_raw_bars_filters_by_et_date():
    """Direct test of storage.get_raw_bars_for_day's ET-date filter.

    Pre-v0.7.11 used `date(timestamp_utc) = ?` which is UTC. Post-v0.7.11
    uses a UTC range that exactly covers one ET trading date.
    """
    import tempfile, shutil, os
    from tech_collector import storage
    tmpdir = tempfile.mkdtemp(prefix="raw_bars_filter_")
    db = os.path.join(tmpdir, "test.db")
    try:
        storage.init_schema(db)
        # Insert bars at three boundary times:
        bars = [
            # ET 19:00 Feb 2 = UTC 00:00 Feb 3 (winter EST). UTC date = Feb 3,
            # ET date = Feb 2. Should NOT be returned for date="2026-02-02".
            {
                "symbol": "TEST",
                "timestamp_utc": "2026-02-03T00:00:00Z",
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "volume": 1, "vwap": 1.0, "trade_count": 1,
                "sector": None,
            },
            # ET 10:30 Feb 3 = UTC 15:30 Feb 3. Both Feb 3.
            {
                "symbol": "TEST",
                "timestamp_utc": "2026-02-03T15:30:00Z",
                "open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0,
                "volume": 1, "vwap": 2.0, "trade_count": 1,
                "sector": None,
            },
            # ET 19:00 Feb 3 = UTC 00:00 Feb 4. UTC date = Feb 4, ET date = Feb 3.
            # SHOULD be returned for date="2026-02-03".
            {
                "symbol": "TEST",
                "timestamp_utc": "2026-02-04T00:00:00Z",
                "open": 3.0, "high": 3.0, "low": 3.0, "close": 3.0,
                "volume": 1, "vwap": 3.0, "trade_count": 1,
                "sector": None,
            },
        ]
        with storage.connect(db) as conn:
            storage.insert_bars(conn, bars, feed="sip",
                                pulled_at_utc="2026-02-03T22:00:00Z",
                                sector=None)
            feb2_bars = storage.get_raw_bars_for_day(conn, "TEST", "2026-02-02")
            feb3_bars = storage.get_raw_bars_for_day(conn, "TEST", "2026-02-03")
            feb4_bars = storage.get_raw_bars_for_day(conn, "TEST", "2026-02-04")

        # Feb 2 ET: only the first bar (UTC 00:00 Feb 3 = ET 19:00 Feb 2)
        _check("raw_bars filter: Feb 2 ET returns 1 bar (the prior-day AH)",
               len(feb2_bars) == 1,
               f"got {len(feb2_bars)} bars")
        # Feb 3 ET: middle and last bar
        _check("raw_bars filter: Feb 3 ET returns 2 bars (regular + AH)",
               len(feb3_bars) == 2,
               f"got {len(feb3_bars)} bars")
        # Feb 4 ET: nothing
        _check("raw_bars filter: Feb 4 ET returns 0 bars",
               len(feb4_bars) == 0,
               f"got {len(feb4_bars)} bars")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    print("SMOKE: backtest_audit + _simulate_trade fix")
    print("=" * 60)
    tests = [
        test_regression_big_move_must_be_TP,
        test_time_exit_invariant,
        test_clean_TP_exit,
        test_clean_SL_exit,
        test_clean_TIME_exit_within_bounds,
        test_tp_sl_same_bar_resolves_SL,
        test_timezone_EST_Feb,
        test_timezone_EDT_Jun,
        test_timezone_DST_Mar_boundary,
        test_timezone_DST_Nov_boundary,
        test_audit_run_end_to_end,
        test_repair_run_end_to_end,
        # v0.7.7
        test_no_timestop_tp_still_works,
        test_no_timestop_trade_never_hits_exits_as_TIME_within_bounds,
        test_no_timestop_runs_past_old_timestop_into_TP,
        test_no_timestop_empty_string_is_treated_as_disabled,
        test_timestop_1550_fires_after_1530,
        test_timestop_1550_does_not_fire_at_1530,
        # v0.7.11
        test_v0711_phantom_bug_regression,
        test_v0711_get_raw_bars_filters_by_et_date,
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
