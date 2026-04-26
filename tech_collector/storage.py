"""
SQLite storage for the Tech Collector.

Two tables:
- raw_bars: 1-minute OHLCV bars from Alpaca, indexed by (symbol, timestamp_utc)
- research_rows: computed feature rows matching the research CSV schema,
  indexed by (symbol, date, scan_time_et)

Both tables use INSERT OR REPLACE semantics so the collector can be re-run
safely over a date range without creating duplicates.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_bars (
    symbol         TEXT    NOT NULL,
    timestamp_utc  TEXT    NOT NULL,          -- ISO 8601 UTC
    open           REAL    NOT NULL,
    high           REAL    NOT NULL,
    low            REAL    NOT NULL,
    close          REAL    NOT NULL,
    volume         INTEGER NOT NULL,
    vwap           REAL,                       -- per-bar VWAP from Alpaca
    trade_count    INTEGER,
    feed_source    TEXT    NOT NULL DEFAULT 'sip',
    pulled_at_utc  TEXT    NOT NULL,
    sector         TEXT,                       -- GICS sector label at pull time (nullable for legacy rows)
    PRIMARY KEY (symbol, timestamp_utc)
);

CREATE INDEX IF NOT EXISTS idx_raw_bars_symbol_date
    ON raw_bars(symbol, substr(timestamp_utc, 1, 10));

CREATE TABLE IF NOT EXISTS research_rows (
    symbol                    TEXT    NOT NULL,
    date                      TEXT    NOT NULL,  -- YYYY-MM-DD
    scan_time_et              TEXT    NOT NULL,
    sector                    TEXT    NOT NULL,
    minutes_since_open        INTEGER NOT NULL,
    scan_price                REAL,
    open_to_scan_return       REAL,
    gap_pct                   REAL,
    intraday_range_position   REAL,
    distance_to_vwap          REAL,
    distance_to_day_high      REAL,
    distance_to_day_low       REAL,
    rsi_14                    REAL,
    macd_hist                 REAL,
    ema_9_distance            REAL,
    ema_20_distance           REAL,
    ema_50_distance           REAL,
    relative_volume           REAL,
    realized_vol_so_far       REAL,
    sector_relative_strength  REAL,             -- computed same as research (leak-prone)
    rs_leakfree               REAL,             -- extension: leak-free version
    day_of_week               TEXT,
    cutoff_time_et            TEXT,
    cutoff_price              REAL,
    return_to_cutoff          REAL,
    target                    INTEGER,
    min_return_before_cutoff  REAL,
    max_return_before_cutoff  REAL,
    return_at_scan_plus_30m   REAL,
    return_at_scan_plus_60m   REAL,
    return_at_scan_plus_90m   REAL,
    return_at_scan_plus_120m  REAL,
    bars_missing_pre_scan     INTEGER,
    bars_missing_post_scan    INTEGER,
    feed_source               TEXT,
    pulled_at_utc             TEXT,
    momentum                  REAL,
    rel_volume_r2k            REAL,
    vwap_slope                REAL,
    orb_strength              REAL,
    atr_reach                 REAL,
    trend_str                 REAL,
    range_expansion           REAL,
    spy_ret                   REAL,
    ret_vs_spy                REAL,
    spy_momentum              REAL,
    mom_vs_spy                REAL,
    spy_vol                   REAL,
    gap_filled                INTEGER,
    range_tightness_30m       REAL,
    bars_in_range_20bps       INTEGER,
    is_nr7                    INTEGER,
    dist_to_day_high_bps      REAL,
    broke_day_high_this_bar   INTEGER,
    broke_opening_range_high  INTEGER,
    bars_since_day_high       INTEGER,
    dist_to_prev_close_bps    REAL,
    dist_to_5d_high_bps       REAL,
    dist_to_20d_high_bps      REAL,
    days_since_20d_high       INTEGER,
    volume_acceleration       REAL,
    cumulative_volume_vs_typical REAL,
    sector_breadth_up         REAL,
    new_highs_in_sector       INTEGER,
    target_25bps              INTEGER,
    target_peak_25bps         INTEGER,
    target_50bps              INTEGER,
    target_peak_50bps         INTEGER,
    target_75bps              INTEGER,
    target_peak_75bps         INTEGER,
    regime_ok                 INTEGER,
    PRIMARY KEY (symbol, date, scan_time_et)
);

CREATE INDEX IF NOT EXISTS idx_research_rows_date
    ON research_rows(date, scan_time_et);

CREATE TABLE IF NOT EXISTS run_log (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    mode           TEXT NOT NULL,               -- 'backfill' | 'pack'
    start_date     TEXT,
    end_date       TEXT,
    symbols_n      INTEGER,
    rows_written   INTEGER,
    errors_n       INTEGER,
    notes          TEXT
);
"""


@contextmanager
def connect(db_path: str | Path = config.DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(db_path: str | Path = config.DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Migration: add sector column to existing raw_bars tables that
        # predate v0.3.0. Always safe — non-PK nullable column.
        raw_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(raw_bars)").fetchall()
        }
        if "sector" not in raw_cols:
            conn.execute("ALTER TABLE raw_bars ADD COLUMN sector TEXT")
        # Migration: add R2K columns to existing research_rows tables that
        # predate this schema. Uses ALTER TABLE ADD COLUMN IF NOT EXISTS
        # via PRAGMA table_info; SQLite has no native IF NOT EXISTS on ADD.
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(research_rows)").fetchall()
        }
        r2k_additions = [
            ("momentum", "REAL"), ("rel_volume_r2k", "REAL"),
            ("vwap_slope", "REAL"), ("orb_strength", "REAL"),
            ("atr_reach", "REAL"), ("trend_str", "REAL"),
            ("range_expansion", "REAL"), ("spy_ret", "REAL"),
            ("ret_vs_spy", "REAL"), ("spy_momentum", "REAL"),
            ("mom_vs_spy", "REAL"), ("spy_vol", "REAL"),
            ("gap_filled", "INTEGER"),
        ]
        for col, typ in r2k_additions:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE research_rows ADD COLUMN {col} {typ}")
        # Migration: structural features + new targets
        structural_additions = [
            ("range_tightness_30m", "REAL"),
            ("bars_in_range_20bps", "INTEGER"),
            ("is_nr7", "INTEGER"),
            ("dist_to_day_high_bps", "REAL"),
            ("broke_day_high_this_bar", "INTEGER"),
            ("broke_opening_range_high", "INTEGER"),
            ("bars_since_day_high", "INTEGER"),
            ("dist_to_prev_close_bps", "REAL"),
            ("dist_to_5d_high_bps", "REAL"),
            ("dist_to_20d_high_bps", "REAL"),
            ("days_since_20d_high", "INTEGER"),
            ("volume_acceleration", "REAL"),
            ("cumulative_volume_vs_typical", "REAL"),
            ("sector_breadth_up", "REAL"),
            ("new_highs_in_sector", "INTEGER"),
            ("target_25bps", "INTEGER"),
            ("target_peak_25bps", "INTEGER"),
            ("target_50bps", "INTEGER"),
            ("target_peak_50bps", "INTEGER"),
            ("target_75bps", "INTEGER"),
            ("target_peak_75bps", "INTEGER"),
            # v0.7.7: regime-gating feature
            ("regime_ok", "INTEGER"),
        ]
        # Re-read existing_cols after the R2K additions
        existing_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(research_rows)").fetchall()
        }
        for col, typ in structural_additions:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE research_rows ADD COLUMN {col} {typ}")


# ---------------------------------------------------------------------------
# v0.6.0: backtest tables
# ---------------------------------------------------------------------------
_BACKTEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT UNIQUE NOT NULL,
    rule_json TEXT NOT NULL,       -- full rule spec as JSON
    tp_bps REAL NOT NULL,          -- take-profit in bps (e.g. 50.0 = 0.5%)
    sl_bps REAL NOT NULL,          -- stop-loss in bps
    timestop_et TEXT NOT NULL,     -- HH:MM at/after which we flatten unconditionally; empty string "" = no timestop (v0.7.7)
    slippage_bps REAL NOT NULL,    -- round-trip slippage in bps
    spy_regime_filter REAL,        -- skip signals when spy_ret_since_open < this (null = off)
    symbol_exclude TEXT,           -- comma-separated symbols to skip
    start_date TEXT,
    end_date TEXT,
    generated_at_utc TEXT NOT NULL,
    n_signals_total INTEGER,
    n_signals_skipped INTEGER,
    n_trades INTEGER,
    net_pnl_bps REAL,
    win_rate REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    run_uuid TEXT NOT NULL,
    symbol TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    signal_time_et TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    exit_time_et TEXT NOT NULL,
    exit_reason TEXT NOT NULL,     -- 'TP' | 'SL' | 'TIME' | 'NO_DATA'
    minutes_held INTEGER,
    gross_return_bps REAL,         -- (exit-entry)/entry * 10000
    net_return_bps REAL,           -- gross minus slippage
    PRIMARY KEY (run_uuid, symbol, signal_date, signal_time_et),
    FOREIGN KEY (run_uuid) REFERENCES backtest_runs(run_uuid)
);

CREATE INDEX IF NOT EXISTS ix_backtest_trades_run ON backtest_trades(run_uuid);
CREATE INDEX IF NOT EXISTS ix_backtest_trades_symdate ON backtest_trades(symbol, signal_date);
"""


def init_backtest_schema(db_path: str | Path = config.DB_PATH) -> None:
    """Create backtest_runs and backtest_trades if they don't already exist.
    Safe to call repeatedly."""
    with connect(db_path) as conn:
        conn.executescript(_BACKTEST_SCHEMA_SQL)
        # v0.7.0 migration: conditional-exit trade fields
        trade_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(backtest_trades)").fetchall()
        }
        for col, typ in [
            ("branch_label", "TEXT"),
            ("position_size", "REAL"),
            ("tp_bps_used", "REAL"),
            ("sl_bps_used", "REAL"),
        ]:
            if col not in trade_cols:
                conn.execute(f"ALTER TABLE backtest_trades ADD COLUMN {col} {typ}")
        # v0.7.0 migration: conditional-exit spec on backtest_runs
        run_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()
        }
        if "conditional_exits_json" not in run_cols:
            conn.execute(
                "ALTER TABLE backtest_runs ADD COLUMN conditional_exits_json TEXT"
            )


def get_raw_bars_for_day(
    conn: sqlite3.Connection, symbol: str, date: str,
) -> list[dict]:
    """Return all minute bars for (symbol, date) in ascending timestamp order.

    Each bar: {'timestamp_utc', 'open', 'high', 'low', 'close', 'volume'}
    """
    rows = conn.execute(
        """SELECT timestamp_utc, open, high, low, close, volume
           FROM raw_bars
           WHERE symbol = ? AND date(timestamp_utc) = ?
           ORDER BY timestamp_utc""",
        (symbol, date),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_raw_bars_for_day(
    conn: sqlite3.Connection, symbol: str, date: str, preserve_spy: bool = True,
) -> int:
    """Delete raw_bars for a single (symbol, date). Returns rows deleted.

    By default preserves SPY — the SPY-relative features in other analyses
    rely on SPY being available; the backtest also needs SPY bars itself.
    """
    if preserve_spy and symbol == "SPY":
        return 0
    cur = conn.execute(
        "DELETE FROM raw_bars WHERE symbol = ? AND date(timestamp_utc) = ?",
        (symbol, date),
    )
    return cur.rowcount or 0


def record_backtest_run(
    conn: sqlite3.Connection, run_info: dict,
) -> int:
    """Insert a backtest_runs row. Returns the auto-increment id."""
    # v0.7.0: provide default for conditional_exits_json for pre-v0.7 callers
    run_info = dict(run_info)
    run_info.setdefault("conditional_exits_json", None)
    # v0.7.7: timestop_et can be None (= disabled). Schema is NOT NULL TEXT,
    # so serialize None as empty string. The backtest engine treats both
    # None and "" as "timestop disabled" — see _simulate_trade.
    if run_info.get("timestop_et") is None:
        run_info["timestop_et"] = ""
    cur = conn.execute(
        """INSERT INTO backtest_runs (
            run_uuid, rule_json, tp_bps, sl_bps, timestop_et, slippage_bps,
            spy_regime_filter, symbol_exclude, start_date, end_date,
            generated_at_utc, n_signals_total, n_signals_skipped,
            n_trades, net_pnl_bps, win_rate, notes, conditional_exits_json
        ) VALUES (
            :run_uuid, :rule_json, :tp_bps, :sl_bps, :timestop_et,
            :slippage_bps, :spy_regime_filter, :symbol_exclude,
            :start_date, :end_date, :generated_at_utc,
            :n_signals_total, :n_signals_skipped,
            :n_trades, :net_pnl_bps, :win_rate, :notes,
            :conditional_exits_json
        )""",
        run_info,
    )
    return cur.lastrowid


def insert_backtest_trades(
    conn: sqlite3.Connection, run_uuid: str, trades: list[dict],
) -> int:
    """Bulk-insert per-trade results. Returns rows inserted."""
    if not trades:
        return 0
    sql = """
        INSERT OR REPLACE INTO backtest_trades (
            run_uuid, symbol, signal_date, signal_time_et,
            entry_price, exit_price, exit_time_et, exit_reason,
            minutes_held, gross_return_bps, net_return_bps,
            branch_label, position_size, tp_bps_used, sl_bps_used
        ) VALUES (
            :run_uuid, :symbol, :signal_date, :signal_time_et,
            :entry_price, :exit_price, :exit_time_et, :exit_reason,
            :minutes_held, :gross_return_bps, :net_return_bps,
            :branch_label, :position_size, :tp_bps_used, :sl_bps_used
        )
    """
    for t in trades:
        t["run_uuid"] = run_uuid
        # v0.7.0: provide safe defaults for older trade dicts that don't have
        # conditional-exit fields. Ensures pre-v0.7.0 callers still work.
        t.setdefault("branch_label", "")
        t.setdefault("position_size", 1.0)
        t.setdefault("tp_bps_used", None)
        t.setdefault("sl_bps_used", None)
    conn.executemany(sql, trades)
    return len(trades)


def get_backtest_run(conn: sqlite3.Connection, run_uuid: str) -> dict | None:
    """Fetch a single run by uuid."""
    row = conn.execute(
        "SELECT * FROM backtest_runs WHERE run_uuid = ?", (run_uuid,),
    ).fetchone()
    return dict(row) if row else None


def list_backtest_runs(
    conn: sqlite3.Connection, limit: int = 50,
) -> list[dict]:
    """List recent backtest runs, newest first."""
    rows = conn.execute(
        """SELECT id, run_uuid, rule_json, tp_bps, sl_bps, timestop_et,
                  slippage_bps, spy_regime_filter, generated_at_utc,
                  n_trades, net_pnl_bps, win_rate
           FROM backtest_runs
           ORDER BY id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_backtest_trades(
    conn: sqlite3.Connection, run_uuid: str,
) -> list[dict]:
    """Return all trades for a run."""
    rows = conn.execute(
        """SELECT * FROM backtest_trades WHERE run_uuid = ?
           ORDER BY signal_date, signal_time_et, symbol""",
        (run_uuid,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_bars(
    conn: sqlite3.Connection,
    rows: list[dict],
    feed: str,
    pulled_at_utc: str,
    sector: str | None = None,
) -> int:
    """Bulk-insert raw bars. `rows` is a list of dicts with keys
    symbol, timestamp_utc, open, high, low, close, volume, vwap, trade_count.

    `sector` is the GICS sector label being collected for. Stored on each
    row so callers can later distinguish bars pulled under different sector
    backfills (SPY bars in particular may be pulled multiple times under
    different sector labels; the most recent label wins via INSERT OR
    REPLACE, which is fine since sector on raw_bars is descriptive
    metadata, not a constraint).
    """
    if not rows:
        return 0
    sql = """
        INSERT OR REPLACE INTO raw_bars
        (symbol, timestamp_utc, open, high, low, close, volume, vwap,
         trade_count, feed_source, pulled_at_utc, sector)
        VALUES (:symbol, :timestamp_utc, :open, :high, :low, :close,
                :volume, :vwap, :trade_count, :feed_source, :pulled_at_utc,
                :sector)
    """
    for r in rows:
        r.setdefault("feed_source", feed)
        r.setdefault("pulled_at_utc", pulled_at_utc)
        r.setdefault("vwap", None)
        r.setdefault("trade_count", None)
        r.setdefault("sector", sector)
    conn.executemany(sql, rows)
    return len(rows)


def delete_raw_bars_in_range(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
    sector: str | None = None,
) -> int:
    """Delete raw_bars in a date range. Returns number of rows removed.

    Used by the chained long-backfill orchestrator to bound DB size: once
    a 6-month segment has been compute'd into research_rows, the raw minute
    bars that produced it can be discarded. research_rows is small (tens of
    thousands of rows per sector per year); raw_bars is large (~21M rows
    per sector per year at 1-min resolution).

    Date filter uses substr(timestamp_utc, 1, 10) to match the existing
    `idx_raw_bars_symbol_date` index structure.

    When sector is provided, SPY bars are preserved (SPY is shared across
    sectors and deleting it under one sector's cleanup would break other
    sectors' computes). Callers that want to delete SPY must pass
    sector=None.
    """
    if sector is None:
        cur = conn.execute(
            "DELETE FROM raw_bars "
            "WHERE substr(timestamp_utc, 1, 10) >= ? "
            "  AND substr(timestamp_utc, 1, 10) <= ?",
            (start_date, end_date),
        )
    else:
        cur = conn.execute(
            "DELETE FROM raw_bars "
            "WHERE substr(timestamp_utc, 1, 10) >= ? "
            "  AND substr(timestamp_utc, 1, 10) <= ? "
            "  AND sector = ? "
            "  AND symbol != 'SPY'",
            (start_date, end_date, sector),
        )
    return cur.rowcount


def insert_research_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = [
        "symbol", "date", "scan_time_et", "sector", "minutes_since_open",
        "scan_price", "open_to_scan_return", "gap_pct",
        "intraday_range_position", "distance_to_vwap",
        "distance_to_day_high", "distance_to_day_low",
        "rsi_14", "macd_hist",
        "ema_9_distance", "ema_20_distance", "ema_50_distance",
        "relative_volume", "realized_vol_so_far",
        "sector_relative_strength", "rs_leakfree",
        "day_of_week", "cutoff_time_et", "cutoff_price",
        "return_to_cutoff", "target",
        "min_return_before_cutoff", "max_return_before_cutoff",
        "return_at_scan_plus_30m", "return_at_scan_plus_60m",
        "return_at_scan_plus_90m", "return_at_scan_plus_120m",
        "bars_missing_pre_scan", "bars_missing_post_scan",
        "feed_source", "pulled_at_utc",
        "momentum", "rel_volume_r2k", "vwap_slope", "orb_strength",
        "atr_reach", "trend_str", "range_expansion",
        "spy_ret", "ret_vs_spy", "spy_momentum", "mom_vs_spy", "spy_vol",
        "gap_filled",
        # structural features
        "range_tightness_30m", "bars_in_range_20bps", "is_nr7",
        "dist_to_day_high_bps", "broke_day_high_this_bar",
        "broke_opening_range_high", "bars_since_day_high",
        "dist_to_prev_close_bps", "dist_to_5d_high_bps",
        "dist_to_20d_high_bps", "days_since_20d_high",
        "volume_acceleration", "cumulative_volume_vs_typical",
        "sector_breadth_up", "new_highs_in_sector",
        # target variants
        "target_25bps", "target_peak_25bps",
        "target_50bps", "target_peak_50bps",
        "target_75bps", "target_peak_75bps",
        # v0.7.7: regime gate
        "regime_ok",
    ]
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO research_rows ({col_list}) VALUES ({placeholders})"
    # Fill missing keys with None for safety
    for r in rows:
        for c in cols:
            r.setdefault(c, None)
    conn.executemany(sql, rows)
    return len(rows)


def log_run_start(
    conn: sqlite3.Connection,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    symbols_n: int,
    started_at_utc: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO run_log (started_at_utc, mode, start_date, end_date,
               symbols_n, rows_written, errors_n)
           VALUES (?, ?, ?, ?, ?, 0, 0)""",
        (started_at_utc, mode, start_date, end_date, symbols_n),
    )
    return cur.lastrowid


def log_run_finish(
    conn: sqlite3.Connection,
    run_id: int,
    finished_at_utc: str,
    rows_written: int,
    errors_n: int,
    notes: str | None = None,
) -> None:
    conn.execute(
        """UPDATE run_log
           SET finished_at_utc = ?, rows_written = ?, errors_n = ?, notes = ?
           WHERE run_id = ?""",
        (finished_at_utc, rows_written, errors_n, notes, run_id),
    )


def sector_status(conn: sqlite3.Connection) -> list[dict]:
    """Return per-sector status summary from research_rows.

    One row per sector that has at least one research_row. Sectors with
    no data are NOT returned — the API layer merges this with the full
    sector list so untouched sectors show "no data" cleanly.

    Returns list of {sector, earliest_date, latest_date, row_count,
    null_target_peak_50bps, null_target_peak_75bps}.

    The null-target counts are included to catch a footgun introduced in
    v0.3.2: research_rows computed before the 50bps/75bps target columns
    were added will have NULL for those columns, and the rule tester
    silently drops such rows. A non-zero count here means "recompute
    needed on the affected date range".
    """
    rows = conn.execute(
        """SELECT sector,
                  MIN(date) AS earliest_date,
                  MAX(date) AS latest_date,
                  COUNT(*) AS row_count,
                  SUM(CASE WHEN target_peak_50bps IS NULL THEN 1 ELSE 0 END)
                      AS null_target_peak_50bps,
                  SUM(CASE WHEN target_peak_75bps IS NULL THEN 1 ELSE 0 END)
                      AS null_target_peak_75bps
           FROM research_rows
           WHERE sector IS NOT NULL
           GROUP BY sector
           ORDER BY sector"""
    ).fetchall()
    return [
        {
            "sector": r["sector"],
            "earliest_date": r["earliest_date"],
            "latest_date": r["latest_date"],
            "row_count": int(r["row_count"]),
            "null_target_peak_50bps": int(r["null_target_peak_50bps"] or 0),
            "null_target_peak_75bps": int(r["null_target_peak_75bps"] or 0),
        }
        for r in rows
    ]
