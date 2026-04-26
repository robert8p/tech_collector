"""
Evidence pack exporter.

Produces a zip containing both scan-row features and raw minute bars:

  tech_research_dataset.csv       — one row per (symbol, date, scan_time_et)
                                    with all 49 feature columns
  raw_bars.parquet                — 1-min bars for the full session of every
                                    day in [start-5 trading days, end], for
                                    every symbol in the universe plus SPY.
                                    Enables recomputing any feature downstream.
  tech_run_manifest.json          — provenance + schema contract
  tech_dataset_summary.json       — quick summary stats

Bars parquet schema:
  symbol         str
  date           str (YYYY-MM-DD, ET calendar)
  timestamp_et   datetime64[ns, America/New_York]
  open           float64
  high           float64
  low            float64
  close          float64
  volume         int64
  vwap           float64 (nullable)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, storage
from .universes import get_universe, sector_slug


RAW_BAR_PRIOR_DAYS: int = 5  # trading days of prior context included in each pack


def _query_rows(conn: sqlite3.Connection, start: str, end: str, sector: str) -> pd.DataFrame:
    sql = """
        SELECT *
        FROM research_rows
        WHERE date BETWEEN ? AND ?
          AND sector = ?
        ORDER BY date, scan_time_et, symbol
    """
    df = pd.read_sql_query(sql, conn, params=(start, end, sector))
    return df


def _query_raw_bars(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    prior_days: int,
    symbols: list[str],
) -> pd.DataFrame:
    """Load full-session 1-min bars for [start - prior_days trading days, end]
    restricted to `symbols` (the sector's universe plus SPY).

    We filter by explicit symbol list rather than by the `raw_bars.sector`
    column because SPY is pulled under every sector's backfill, and its
    stored sector label is whichever sector pulled it most recently.
    Filtering by symbol list is unambiguous regardless of pull history.

    Uses calendar days with a generous buffer so weekends/holidays don't
    shorten coverage. Filters to regular session (09:30-16:00 ET) in pandas
    after load.
    """
    calendar_buffer = prior_days * 2 + 3
    buffered_start = (date.fromisoformat(start) - timedelta(days=calendar_buffer)).isoformat()
    if not symbols:
        return pd.DataFrame(columns=[
            "symbol", "date", "timestamp_et", "open", "high", "low",
            "close", "volume", "vwap",
        ])
    placeholders = ",".join("?" * len(symbols))
    sql = f"""
        SELECT symbol, timestamp_utc, open, high, low, close, volume, vwap
        FROM raw_bars
        WHERE substr(timestamp_utc, 1, 10) BETWEEN ? AND ?
          AND symbol IN ({placeholders})
        ORDER BY symbol, timestamp_utc
    """
    params = [buffered_start, end, *symbols]
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return df
    ts_utc = pd.to_datetime(df["timestamp_utc"], utc=True)
    ts_et = ts_utc.dt.tz_convert("America/New_York")
    df["timestamp_et"] = ts_et
    df["date"] = ts_et.dt.strftime("%Y-%m-%d")
    # Regular session: 09:30 <= t < 16:00 ET
    mins_since_open = (ts_et.dt.hour - 9) * 60 + ts_et.dt.minute - 30
    in_session = (mins_since_open >= 0) & (mins_since_open < 390)
    df = df.loc[in_session].copy()
    df = df.drop(columns=["timestamp_utc"])
    return df[["symbol", "date", "timestamp_et", "open", "high", "low", "close", "volume", "vwap"]]


def _summary(df_rows: pd.DataFrame, df_bars: pd.DataFrame) -> dict:
    if df_rows.empty:
        return {"rows": 0}
    overall = float(df_rows["target"].dropna().mean())
    by_scan = (
        df_rows.dropna(subset=["target"])
          .groupby("scan_time_et")["target"].mean()
          .round(6).to_dict()
    )
    missing_pre = int(df_rows["bars_missing_pre_scan"].fillna(0).sum()) if "bars_missing_pre_scan" in df_rows else 0
    missing_post = int(df_rows["bars_missing_post_scan"].fillna(0).sum()) if "bars_missing_post_scan" in df_rows else 0
    null_targets = int(df_rows["target"].isna().sum())
    return {
        "rows": int(len(df_rows)),
        "symbols": int(df_rows["symbol"].nunique()),
        "days": int(df_rows["date"].nunique()),
        "scan_times": list(config.SCAN_TIMES_ET),
        "positive_rate_overall": round(overall, 6),
        "positive_rate_by_scan_time": by_scan,
        "raw_bars": {
            "rows": int(len(df_bars)),
            "days_covered": int(df_bars["date"].nunique()) if not df_bars.empty else 0,
            "symbols_covered": int(df_bars["symbol"].nunique()) if not df_bars.empty else 0,
            "date_range": [
                df_bars["date"].min() if not df_bars.empty else None,
                df_bars["date"].max() if not df_bars.empty else None,
            ],
        },
        "data_quality": {
            "bars_missing_pre_scan_total": missing_pre,
            "bars_missing_post_scan_total": missing_post,
            "null_target_rows": null_targets,
        },
    }


def _manifest(
    df_rows: pd.DataFrame,
    df_bars: pd.DataFrame,
    start: str,
    end: str,
    sector: str,
    universe: tuple[str, ...],
) -> dict:
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "sector": sector,
        "scan_times_et": list(config.SCAN_TIMES_ET),
        "cutoff_rule": "official_market_close_minus_30m",
        "target_definition": "1 if cutoff_price > scan_price else 0",
        "data_feed": config.ALPACA_FEED,
        "bar_adjustment": "split",
        "bar_timeframe": "1Min",
        "start_date": start,
        "end_date": end,
        "raw_bars_prior_days": RAW_BAR_PRIOR_DAYS,
        "symbol_source": f"static universe from universes.py (sector={sector})",
        "universe_note": (
            f"Survivorship-biased: S&P 500 {sector} constituents as of "
            "2026-04-19. Does not reflect point-in-time membership for any "
            "historical date. SPY is included in raw_bars for market-context "
            "features."
        ),
        "symbol_count": len(universe),
        "symbols": list(universe),
        "row_count": int(len(df_rows)),
        "raw_bars_row_count": int(len(df_bars)),
        "positive_rate": (
            round(float(df_rows["target"].dropna().mean()), 6)
            if not df_rows.empty and df_rows["target"].notna().any() else None
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_definitions": {
            "notes": "See tech_collector/feature_computer.py and tech_collector/r2k_features.py for exact implementations.",
            "columns": list(config.ALL_COLUMNS),
            "leakage_warning": (
                "sector_relative_strength uses post-cutoff cross-section "
                "information (for research-schema compatibility). Use "
                "rs_leakfree for pattern analysis."
            ),
        },
        "raw_bars_schema": {
            "description": (
                "1-minute OHLCV bars for the full regular session (09:30-16:00 ET) "
                f"of every trading day in [start_date - {RAW_BAR_PRIOR_DAYS} trading days, end_date]. "
                "Includes SPY for market-context features. Adjustment: split. Feed: sip."
            ),
            "file": "raw_bars.parquet",
            "columns": {
                "symbol": "str",
                "date": "str (YYYY-MM-DD, ET calendar)",
                "timestamp_et": "datetime64[ns, America/New_York]",
                "open": "float64",
                "high": "float64",
                "low": "float64",
                "close": "float64",
                "volume": "int64",
                "vwap": "float64 (may be null for some bars)",
            },
        },
        "output_files": {
            "dataset_csv": "tech_research_dataset.csv",
            "raw_bars_parquet": "raw_bars.parquet",
            "manifest_json": "tech_run_manifest.json",
            "summary_json": "tech_dataset_summary.json",
            "zip": "tech_research_export.zip",
        },
    }


def export_scan_rows(
    start_date: str,
    end_date: str,
    out_dir: str | Path = config.EVIDENCE_PACK_DIR,
    db_path: str = config.DB_PATH,
    sector: str | None = None,
) -> Path:
    """Export only the scan-row CSV (no raw bars) for a date range.

    This is the right shape for cross-month pattern analysis: small file,
    fits in one Claude upload for any reasonable date range, contains all
    49 computed features per scan-bar.

    `sector` picks which GICS sector's rows to export. When None, falls
    back to config.DEFAULT_SECTOR. The slugified sector name appears in
    the output filename:
        tech_scan_rows_information-technology_2025-10-20_to_2026-04-17.zip

    Returns the path to the zip containing:
      tech_scan_rows.csv       — 49-column scan-row features
      tech_scan_rows_manifest.json — provenance + schema
    """
    resolved_sector = sector or config.DEFAULT_SECTOR
    universe = get_universe(resolved_sector)
    slug = sector_slug(resolved_sector)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with storage.connect(db_path) as conn:
        df_rows = _query_rows(conn, start_date, end_date, resolved_sector)

    stem = f"tech_scan_rows_{slug}_{start_date}_to_{end_date}"
    pack_dir = out_dir / stem
    pack_dir.mkdir(exist_ok=True)

    csv_path = pack_dir / "tech_scan_rows.csv"
    manifest_path = pack_dir / "tech_scan_rows_manifest.json"
    zip_path = out_dir / f"{stem}.zip"

    desired_cols = [c for c in config.ALL_COLUMNS if c in df_rows.columns]
    df_rows = df_rows[desired_cols]
    df_rows.to_csv(csv_path, index=False)

    manifest = {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "sector": resolved_sector,
        "export_kind": "scan_rows_only",
        "purpose": (
            "Cross-month / long-range pattern analysis. Scan-row features "
            "only; no raw bars. Suitable for walkforward validation, rule "
            "search, and regime detection across the full date range in a "
            "single Claude upload."
        ),
        "scan_times_et": list(config.SCAN_TIMES_ET),
        "cutoff_rule": "official_market_close_minus_30m",
        "target_definition": "1 if cutoff_price > scan_price else 0",
        "data_feed": config.ALPACA_FEED,
        "bar_adjustment": "split",
        "start_date": start_date,
        "end_date": end_date,
        "symbol_source": f"static universe from universes.py (sector={resolved_sector})",
        "universe_note": (
            f"Survivorship-biased: S&P 500 {resolved_sector} constituents "
            "as of 2026-04-19. Does not reflect point-in-time membership "
            "for any historical date."
        ),
        "symbol_count": len(universe),
        "symbols": list(universe),
        "row_count": int(len(df_rows)),
        "positive_rate": (
            round(float(df_rows["target"].dropna().mean()), 6)
            if not df_rows.empty and df_rows["target"].notna().any() else None
        ),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_definitions": {
            "notes": "See tech_collector/feature_computer.py and tech_collector/r2k_features.py.",
            "columns": list(config.ALL_COLUMNS),
            "leakage_warning": (
                "sector_relative_strength uses post-cutoff cross-section "
                "information. Use rs_leakfree for pattern analysis."
            ),
        },
        "output_files": {
            "scan_rows_csv": "tech_scan_rows.csv",
            "manifest_json": "tech_scan_rows_manifest.json",
        },
    }
    with open(csv_path, "rb") as f:
        manifest["scan_rows_csv_sha256"] = hashlib.sha256(f.read()).hexdigest()

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(csv_path, arcname="tech_scan_rows.csv")
        zf.write(manifest_path, arcname="tech_scan_rows_manifest.json")

    return zip_path


def export_pack(
    start_date: str,
    end_date: str,
    out_dir: str | Path = config.EVIDENCE_PACK_DIR,
    db_path: str = config.DB_PATH,
    sector: str | None = None,
) -> Path:
    """Produce an evidence pack zip for the given date range.

    Includes the scan-row CSV and a raw-bars Parquet file with full-session
    1-min bars for the date range plus RAW_BAR_PRIOR_DAYS of prior context,
    restricted to the sector's universe plus SPY.

    `sector` picks which GICS sector to export. When None, falls back to
    config.DEFAULT_SECTOR. The slugified sector name appears in the filename:
        tech_research_export_information-technology_2025-10-20_to_2026-04-17.zip

    Returns the path to the zip file.
    """
    resolved_sector = sector or config.DEFAULT_SECTOR
    universe = get_universe(resolved_sector)
    slug = sector_slug(resolved_sector)
    # SPY is always included in raw_bars (for market-context features)
    bars_symbols = list(universe) + ["SPY"]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with storage.connect(db_path) as conn:
        df_rows = _query_rows(conn, start_date, end_date, resolved_sector)
        df_bars = _query_raw_bars(
            conn, start_date, end_date, RAW_BAR_PRIOR_DAYS,
            symbols=bars_symbols,
        )

    stem = f"tech_research_export_{slug}_{start_date}_to_{end_date}"
    pack_dir = out_dir / stem
    pack_dir.mkdir(exist_ok=True)

    csv_path = pack_dir / "tech_research_dataset.csv"
    bars_path = pack_dir / "raw_bars.parquet"
    manifest_path = pack_dir / "tech_run_manifest.json"
    summary_path = pack_dir / "tech_dataset_summary.json"
    zip_path = out_dir / f"{stem}.zip"

    # Reorder CSV columns to match schema order
    desired_cols = [c for c in config.ALL_COLUMNS if c in df_rows.columns]
    df_rows = df_rows[desired_cols]
    df_rows.to_csv(csv_path, index=False)

    # Parquet bars
    if not df_bars.empty:
        df_bars.to_parquet(bars_path, index=False, compression="snappy")
    else:
        empty = pd.DataFrame(columns=[
            "symbol", "date", "timestamp_et", "open", "high", "low", "close", "volume", "vwap"
        ])
        empty.to_parquet(bars_path, index=False, compression="snappy")

    manifest = _manifest(df_rows, df_bars, start_date, end_date, resolved_sector, universe)
    summary = _summary(df_rows, df_bars)

    with open(csv_path, "rb") as f:
        manifest["dataset_csv_sha256"] = hashlib.sha256(f.read()).hexdigest()
    with open(bars_path, "rb") as f:
        manifest["raw_bars_parquet_sha256"] = hashlib.sha256(f.read()).hexdigest()

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, sort_keys=False)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(csv_path, arcname="tech_research_dataset.csv")
        zf.write(bars_path, arcname="raw_bars.parquet")
        zf.write(manifest_path, arcname="tech_run_manifest.json")
        zf.write(summary_path, arcname="tech_dataset_summary.json")

    return zip_path


def export_scan_rows_parquet(
    start_date: str,
    end_date: str,
    out_dir: str | Path = config.EVIDENCE_PACK_DIR,
    db_path: str = config.DB_PATH,
    sector: str | None = None,
) -> Path:
    """Export scan rows as a single Parquet file (no zip wrapper).

    Same content as export_scan_rows but produces one `.parquet` instead
    of a zip of CSV+JSON. Parquet on this schema compresses ~10x vs CSV,
    so a full 2yr × 72-symbol sector export (~150K rows × 66 cols) lands
    in the ~8-15 MB range — small enough to upload to Claude as a single
    file without chunking.

    Filename shape:
        tech_scan_rows_information-technology_2024-04-19_to_2026-04-17.parquet

    The provenance metadata that would normally live in a sibling
    manifest.json is embedded in the Parquet file's key-value metadata
    instead, so a consumer that reads the parquet with pyarrow can
    recover app/version/sector/symbol_count/generated_at/etc. without a
    second file.
    """
    resolved_sector = sector or config.DEFAULT_SECTOR
    universe = get_universe(resolved_sector)
    slug = sector_slug(resolved_sector)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with storage.connect(db_path) as conn:
        df_rows = _query_rows(conn, start_date, end_date, resolved_sector)

    desired_cols = [c for c in config.ALL_COLUMNS if c in df_rows.columns]
    df_rows = df_rows[desired_cols]

    filename = f"tech_scan_rows_{slug}_{start_date}_to_{end_date}.parquet"
    parquet_path = out_dir / filename

    # Embed provenance directly in the parquet file metadata. This keeps
    # the deliverable to a single file while preserving everything a
    # manifest.json would contain.
    metadata = {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "sector": resolved_sector,
        "export_kind": "scan_rows_parquet",
        "start_date": start_date,
        "end_date": end_date,
        "scan_times_et": ",".join(config.SCAN_TIMES_ET),
        "cutoff_rule": "official_market_close_minus_30m",
        "target_definition": "1 if cutoff_price > scan_price else 0",
        "data_feed": config.ALPACA_FEED,
        "bar_adjustment": "split",
        "symbol_count": str(len(universe)),
        "symbols": ",".join(universe),
        "row_count": str(len(df_rows)),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "leakage_warning": (
            "sector_relative_strength uses post-cutoff info; "
            "use rs_leakfree for pattern analysis."
        ),
    }

    # Use pyarrow directly so we can attach schema-level metadata.
    # pandas.to_parquet's metadata kwarg only reaches the Arrow table
    # via a roundabout path and silently drops some keys on older
    # versions; going through pa.Table is the reliable route.
    import pyarrow as pa  # imported lazily so tests without pyarrow can
    import pyarrow.parquet as pq  # still import the module

    table = pa.Table.from_pandas(df_rows, preserve_index=False)
    # Parquet file metadata must be bytes-keyed and bytes-valued
    meta_bytes = {k.encode(): v.encode() for k, v in metadata.items()}
    table = table.replace_schema_metadata(meta_bytes)
    pq.write_table(table, parquet_path, compression="snappy")

    return parquet_path


# ---------------------------------------------------------------------------
# One-click orchestrator: backfill -> compute -> parquet export.
# Skips steps whose work is already complete for the requested range.
# ---------------------------------------------------------------------------
def _expected_trading_days(start_date: str, end_date: str) -> int:
    """Approximate US trading days in [start, end] inclusive.

    Counts weekdays and does NOT subtract US market holidays — those
    would require a calendar dependency. Undercount is fine because
    the skip heuristic uses a 95% threshold anyway.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    days = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def _backfill_coverage(
    db_path: str, symbols: tuple[str, ...], start_date: str, end_date: str
) -> tuple[int, int]:
    """Return (min_symbol_days_covered, expected_days) for the range.

    min_symbol_days_covered is the worst-covered symbol's day count. If
    that's close to expected_days, we can skip backfill.
    """
    expected = _expected_trading_days(start_date, end_date)
    if expected == 0:
        return (0, 0)
    with storage.connect(db_path) as conn:
        min_covered = expected  # ceiling
        for sym in symbols:
            row = conn.execute(
                """SELECT COUNT(DISTINCT substr(timestamp_utc, 1, 10)) AS n
                   FROM raw_bars
                   WHERE symbol = ?
                     AND substr(timestamp_utc, 1, 10) BETWEEN ? AND ?""",
                (sym, start_date, end_date),
            ).fetchone()
            n = int(row["n"]) if row else 0
            if n < min_covered:
                min_covered = n
    return (min_covered, expected)


def _compute_coverage(
    db_path: str, sector: str, start_date: str, end_date: str
) -> tuple[int, int]:
    """Return (distinct_dates_with_rows, expected_days) for the sector."""
    expected = _expected_trading_days(start_date, end_date)
    with storage.connect(db_path) as conn:
        row = conn.execute(
            """SELECT COUNT(DISTINCT date) AS n
               FROM research_rows
               WHERE sector = ? AND date BETWEEN ? AND ?""",
            (sector, start_date, end_date),
        ).fetchone()
        n = int(row["n"]) if row else 0
    return (n, expected)


def generate_research_pack(
    start_date: str,
    end_date: str,
    sector: str | None = None,
    db_path: str = config.DB_PATH,
    out_dir: str | Path = config.EVIDENCE_PACK_DIR,
    coverage_threshold: float = 0.95,
) -> dict:
    """One-click orchestrator: backfill -> compute -> parquet export.

    Skips backfill and compute if the DB already holds sufficient data
    for the requested (sector, date range). "Sufficient" = at least
    coverage_threshold (default 95%) of expected weekday trading days
    already present. Export always runs — it's cheap and produces the
    deliverable file.

    The skip heuristic is deliberately coarse. It's designed to avoid
    wasting 30+ minutes of Alpaca calls when you're just re-exporting,
    not to be a correctness guarantee. If the user suspects data is
    bad for a specific range, they should use POST /backfill directly.

    Returns a dict with per-step status and the parquet filename.
    """
    # Import inside the function to avoid import-time dependency on
    # collector.py for callers that only want the exporter bits.
    from . import collector, feature_computer

    resolved_sector = sector or config.DEFAULT_SECTOR
    universe = get_universe(resolved_sector)

    result: dict = {
        "sector": resolved_sector,
        "start_date": start_date,
        "end_date": end_date,
        "steps_run": [],
        "steps_skipped": [],
    }

    # Step 1: backfill unless coverage already >= threshold
    min_covered, expected = _backfill_coverage(
        db_path, universe, start_date, end_date
    )
    backfill_coverage_pct = (min_covered / expected) if expected else 1.0
    if backfill_coverage_pct >= coverage_threshold:
        result["steps_skipped"].append("backfill")
        result["backfill_coverage_pct"] = round(backfill_coverage_pct, 4)
    else:
        result["steps_run"].append("backfill")
        bf_result = collector.collect_range(
            start_date=start_date, end_date=end_date,
            db_path=db_path, sector=resolved_sector,
        )
        result["backfill"] = bf_result
        result["backfill_coverage_pct_before"] = round(backfill_coverage_pct, 4)

    # Step 2: compute unless research_rows for this sector cover the range
    compute_covered, _ = _compute_coverage(
        db_path, resolved_sector, start_date, end_date
    )
    compute_coverage_pct = (compute_covered / expected) if expected else 1.0
    if compute_coverage_pct >= coverage_threshold:
        result["steps_skipped"].append("compute")
        result["compute_coverage_pct"] = round(compute_coverage_pct, 4)
    else:
        result["steps_run"].append("compute")
        cp_result = feature_computer.compute_range(
            start_date=start_date, end_date=end_date,
            db_path=db_path, sector=resolved_sector,
        )
        result["compute"] = cp_result
        result["compute_coverage_pct_before"] = round(compute_coverage_pct, 4)

    # Step 3: always export (fast and produces the deliverable)
    result["steps_run"].append("export")
    parquet_path = export_scan_rows_parquet(
        start_date=start_date, end_date=end_date,
        out_dir=out_dir, db_path=db_path, sector=resolved_sector,
    )
    size = parquet_path.stat().st_size
    result["pack_path"] = str(parquet_path)
    result["pack_filename"] = parquet_path.name
    result["download_url"] = f"/packs/{parquet_path.name}"
    result["size_bytes"] = size
    result["size_mb"] = round(size / 1_000_000, 2)

    return result


def generate_research_pack_all_sectors(
    start_date: str,
    end_date: str,
    db_path: str = config.DB_PATH,
    out_dir: str | Path = config.EVIDENCE_PACK_DIR,
    coverage_threshold: float = 0.95,
) -> dict:
    """Run generate_research_pack for every GICS sector in sequence.

    Long-running: the full 11-sector × 2yr pipeline takes 6-10 hours on
    a cold DB. Per-sector failures don't stop the run; they're captured
    in the result dict and the next sector continues.

    Returns {'packs': [...], 'errors': [...], 'total_size_mb': float}.
    """
    from .universes import SECTOR_UNIVERSES  # local to avoid top-level cycle
    packs = []
    errors = []
    for sector in SECTOR_UNIVERSES.keys():
        try:
            r = generate_research_pack(
                start_date=start_date, end_date=end_date, sector=sector,
                db_path=db_path, out_dir=out_dir,
                coverage_threshold=coverage_threshold,
            )
            packs.append(r)
        except Exception as e:
            errors.append({
                "sector": sector,
                "error": f"{type(e).__name__}: {e}",
            })
    return {
        "packs": packs,
        "errors": errors,
        "total_packs": len(packs),
        "total_size_mb": round(
            sum(p.get("size_bytes", 0) for p in packs) / 1_000_000, 2
        ),
    }


# ---------------------------------------------------------------------------
# v0.5.0 — chained long-range backfill+compute with raw_bars discard
# ---------------------------------------------------------------------------
def _month_segments(
    start_date: str, end_date: str, months_per_segment: int = 6,
) -> list[tuple[str, str]]:
    """Split a date range into contiguous N-month segments.

    Rule: each segment is at most months_per_segment months long, ending on
    the last day of a month (or end_date, whichever is earlier). Segments
    are contiguous — segment N+1 begins the day after segment N ends.

    Returns list of (seg_start, seg_end) ISO-date tuples, inclusive on both
    ends, ordered chronologically.

    Example (months_per_segment=6):
      _month_segments("2023-04-19", "2026-04-19")
      => [("2023-04-19", "2023-10-31"),
          ("2023-11-01", "2024-04-30"),
          ("2024-05-01", "2024-10-31"),
          ...,
          ("2026-04-01", "2026-04-19")]
    """
    from datetime import date as _date, timedelta as _td
    start = _date.fromisoformat(start_date)
    end = _date.fromisoformat(end_date)
    if start > end:
        raise ValueError(f"start_date {start_date} > end_date {end_date}")
    segments = []
    cursor = start
    while cursor <= end:
        # Advance by months_per_segment months, then roll back to end-of-month
        y = cursor.year
        m = cursor.month + months_per_segment - 1  # inclusive month span
        while m > 12:
            m -= 12
            y += 1
        # Last day of that target month
        if m == 12:
            seg_end = _date(y, 12, 31)
        else:
            seg_end = _date(y, m + 1, 1) - _td(days=1)
        seg_end = min(seg_end, end)
        segments.append((cursor.isoformat(), seg_end.isoformat()))
        cursor = seg_end + _td(days=1)
    return segments


def chained_long_backfill(
    start_date: str,
    end_date: str,
    sector: str,
    db_path: str = config.DB_PATH,
    months_per_segment: int = 6,
    discard_raw_bars: bool = True,
    progress_cb: "callable | None" = None,
) -> dict:
    """Run backfill+compute in contiguous N-month segments.

    For each segment in order:
      1. Call collect_range(segment_start, segment_end, sector) → fills raw_bars
      2. Call compute_range(segment_start, segment_end, sector) → fills research_rows
      3. If discard_raw_bars: delete_raw_bars_in_range(segment_start, segment_end, sector)
         keeps DB size bounded. SPY bars in the segment are preserved.

    When progress_cb is provided, it's called after each segment with a dict
    describing the step just completed. This is how the job system streams
    incremental progress to the dashboard instead of waiting for the full
    chain to finish.

    Returns a summary dict with per-segment counts and totals. On failure,
    the exception propagates; segments that already completed have their
    research_rows persisted (SQLite commits per segment, not per chain),
    so a subsequent re-run with discard_raw_bars and the same parameters
    will skip the already-computed segments only if coverage heuristics
    decide so — which they don't, by default, for the long orchestrator.
    It's the caller's responsibility to narrow the date range if resuming.

    The function is deliberately simple: no segment-level parallelism
    (Alpaca rate limits mean sequential is actually faster in practice,
    since parallel pulls hit the same throttle and just thrash).
    """
    from . import collector, feature_computer

    segments = _month_segments(start_date, end_date, months_per_segment)
    if not segments:
        return {"segments_run": 0, "segments": [], "note": "no segments produced"}

    summary = {
        "sector": sector,
        "start_date": start_date,
        "end_date": end_date,
        "months_per_segment": months_per_segment,
        "discard_raw_bars": discard_raw_bars,
        "n_segments_total": len(segments),
        "segments_completed": 0,
        "segments": [],
        "total_bars_pulled": 0,
        "total_research_rows_written": 0,
        "total_raw_bars_deleted": 0,
    }

    for i, (seg_start, seg_end) in enumerate(segments, 1):
        seg_result: dict = {
            "segment_idx": i, "seg_start": seg_start, "seg_end": seg_end,
        }
        # Step 1: backfill
        bf = collector.collect_range(
            start_date=seg_start, end_date=seg_end,
            db_path=db_path, sector=sector,
        )
        seg_result["bars_pulled"] = bf.get("rows", 0)
        seg_result["symbols_done"] = bf.get("symbols_done", 0)
        if bf.get("errors"):
            seg_result["backfill_errors"] = bf["errors"]

        # Step 2: compute
        cp = feature_computer.compute_range(
            start_date=seg_start, end_date=seg_end,
            db_path=db_path, sector=sector,
        )
        seg_result["research_rows_written"] = cp.get("rows_written", 0)

        # Step 3: discard raw_bars
        if discard_raw_bars:
            with storage.connect(db_path) as conn:
                n_deleted = storage.delete_raw_bars_in_range(
                    conn, seg_start, seg_end, sector=sector,
                )
            seg_result["raw_bars_deleted"] = n_deleted
        else:
            seg_result["raw_bars_deleted"] = 0

        summary["segments"].append(seg_result)
        summary["segments_completed"] += 1
        summary["total_bars_pulled"] += seg_result.get("bars_pulled", 0)
        summary["total_research_rows_written"] += seg_result.get("research_rows_written", 0)
        summary["total_raw_bars_deleted"] += seg_result.get("raw_bars_deleted", 0)

        if progress_cb is not None:
            try:
                progress_cb({
                    "segment_idx": i,
                    "of": len(segments),
                    "seg_start": seg_start, "seg_end": seg_end,
                    "bars_pulled": seg_result.get("bars_pulled", 0),
                    "research_rows_written": seg_result.get("research_rows_written", 0),
                    "raw_bars_deleted": seg_result.get("raw_bars_deleted", 0),
                })
            except Exception:
                # Progress callback failures are non-fatal — the chain
                # continues regardless of UI wiring issues.
                pass

    return summary
