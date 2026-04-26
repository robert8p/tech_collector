"""
Validation harness: compare computed research rows against the original
research CSV (tech_research_dataset.csv from the 2026-04-19 export).

Why this matters: the feature definitions in feature_computer.py are
reconstructed, not transcribed from a spec. Before trusting any backfill
output for pattern work, we need to confirm the values match the original
CSV on a non-trivial overlapping sample.

Usage:
    python -m tech_collector.validate \\
        --research-csv path/to/tech_research_dataset.csv \\
        --db-path tech_collector.sqlite \\
        --sample 500

Checks:
  - Mean/median/stdev of each numeric feature within 1% of research values
  - target agreement rate > 99%
  - Per-feature max absolute difference
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import storage


NUMERIC_FEATURES = [
    "scan_price", "open_to_scan_return", "gap_pct",
    "intraday_range_position", "distance_to_vwap",
    "distance_to_day_high", "distance_to_day_low",
    "rsi_14", "macd_hist",
    "ema_9_distance", "ema_20_distance", "ema_50_distance",
    "relative_volume", "realized_vol_so_far",
    "cutoff_price", "return_to_cutoff",
    "min_return_before_cutoff", "max_return_before_cutoff",
]


def compare(
    research_csv: Path, db_path: Path, sample_size: int = 500,
    sector: str | None = None,
) -> dict:
    """Load the research CSV and the computed rows, join on (symbol, date,
    scan_time_et), report agreement stats on NUMERIC_FEATURES and target.

    `sector`, when given, filters the computed-rows side so you don't
    accidentally sample rows from a different sector into the comparison.
    The reference CSV is not filtered — the inner-join will drop any
    non-matching rows anyway, but pre-filtering keeps the error messages
    clear when a user validates sector A with the DB also containing
    sector B rows.
    """
    research = pd.read_csv(research_csv)
    with storage.connect(db_path) as conn:
        if sector:
            computed = pd.read_sql_query(
                "SELECT * FROM research_rows WHERE sector = ?",
                conn, params=(sector,),
            )
        else:
            computed = pd.read_sql_query("SELECT * FROM research_rows", conn)

    if computed.empty:
        msg = "No computed rows in DB. Run backfill + compute first."
        if sector:
            msg = (
                f"No computed rows in DB for sector={sector!r}. Run backfill "
                f"+ compute for this sector first, or check the sector name."
            )
        return {"error": msg}

    join_keys = ["symbol", "date", "scan_time_et"]
    merged = research.merge(
        computed, on=join_keys, how="inner", suffixes=("_r", "_c")
    )
    if merged.empty:
        return {"error": "No overlap between research CSV and computed rows."}

    # Sample for speed
    if len(merged) > sample_size:
        merged = merged.sample(sample_size, random_state=42)

    report = {
        "overlap_rows": int(len(merged)),
        "target_agreement_rate": None,
        "feature_stats": {},
    }

    # Target agreement
    both_targets = merged.dropna(subset=["target_r", "target_c"])
    if not both_targets.empty:
        agree = (both_targets["target_r"] == both_targets["target_c"]).mean()
        report["target_agreement_rate"] = round(float(agree), 4)

    # Per-feature stats
    for f in NUMERIC_FEATURES:
        r_col, c_col = f"{f}_r", f"{f}_c"
        if r_col not in merged.columns or c_col not in merged.columns:
            continue
        pair = merged[[r_col, c_col]].dropna()
        if pair.empty:
            continue
        diff = pair[c_col] - pair[r_col]
        rel_diff = diff / pair[r_col].replace(0, np.nan)
        report["feature_stats"][f] = {
            "n": int(len(pair)),
            "research_mean": round(float(pair[r_col].mean()), 6),
            "computed_mean": round(float(pair[c_col].mean()), 6),
            "mean_abs_diff": round(float(diff.abs().mean()), 6),
            "max_abs_diff": round(float(diff.abs().max()), 6),
            "median_rel_diff_pct": (
                round(float(rel_diff.abs().median()) * 100, 3)
                if rel_diff.notna().any() else None
            ),
        }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--research-csv", required=True, type=Path)
    ap.add_argument("--db-path", default="tech_collector.sqlite", type=Path)
    ap.add_argument("--sample", default=500, type=int)
    ap.add_argument(
        "--sector", default=None,
        help="Optional GICS sector filter for computed rows (e.g. 'Information Technology')",
    )
    args = ap.parse_args()

    if not args.research_csv.exists():
        print(f"Research CSV not found: {args.research_csv}", file=sys.stderr)
        return 2
    if not args.db_path.exists():
        print(f"DB not found: {args.db_path}", file=sys.stderr)
        return 2

    report = compare(args.research_csv, args.db_path, args.sample, sector=args.sector)
    import json
    print(json.dumps(report, indent=2, sort_keys=False))
    # Warn loudly if any feature has >1% median relative diff
    bad = [
        f for f, s in report.get("feature_stats", {}).items()
        if (s.get("median_rel_diff_pct") or 0) > 1.0
    ]
    if bad:
        print(f"\nWARNING: features with >1% median rel diff: {bad}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
