"""
Rule tester — evaluates candidate pattern rules against stored scan rows.

Input rule format (JSON):
    {
      "rules": [
        {
          "id": "it-momentum-vol-1",        # stable identifier, used for tracking
          "sector": "Information Technology",
          "target": "target_peak_75bps",     # one of the stored target columns
          "predicates": [
            {"feature": "momentum", "op": ">", "value": 0.001955},
            {"feature": "realized_vol_so_far", "op": ">", "value": 0.002807},
            {"feature": "new_highs_in_sector", "op": ">", "value": 6}
          ],
          "notes": "optional human-readable description"
        },
        ...
      ]
    }

Predicate ops are the literal operators: '>', '>=', '<', '<=', '==', '!='.
Values are raw numeric thresholds, not quantile references — this matters,
because evaluating the same rule against a future dataset shouldn't silently
re-thresh at new quantiles. The quantile work is part of rule DISCOVERY (done
in analysis sessions); the tester consumes concrete thresholds.

Output: per-rule stats including precision, lift, support, day-concentration,
rolling-origin folds, plus an overall summary.

Design principles:
- The tester NEVER discovers rules. It only evaluates rules a human (or Claude
  in an analysis session) has handed it. Discovery and testing are separate
  responsibilities by design.
- All results are deterministic and reproducible: same rule + same data + same
  split policy = same output.
- The tester applies the same row-level filters the analysis pipeline used
  (drop null-target rows, drop 09:30 scans, thin-tape filter). These are
  parameters so they can be audited.
- Rolling-origin validation splits the date range into N contiguous folds and
  reports per-fold stats. This is the feature that makes the tester more
  useful than ad-hoc in-session analysis.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from . import config, storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Which target columns are valid — must match what feature_computer writes
# ---------------------------------------------------------------------------
VALID_TARGETS = (
    "target", "target_25bps", "target_peak_25bps",
    "target_50bps", "target_peak_50bps",
    "target_75bps", "target_peak_75bps",
)

VALID_OPS = (">", ">=", "<", "<=", "==", "!=")


# ---------------------------------------------------------------------------
# Rule data model
# ---------------------------------------------------------------------------
@dataclass
class Predicate:
    feature: str
    op: str
    value: float

    def to_dict(self) -> dict:
        return {"feature": self.feature, "op": self.op, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> "Predicate":
        if d.get("op") not in VALID_OPS:
            raise ValueError(f"invalid op {d.get('op')!r}; must be one of {VALID_OPS}")
        if not isinstance(d.get("feature"), str):
            raise ValueError(f"predicate feature must be str, got {type(d.get('feature'))}")
        return cls(feature=d["feature"], op=d["op"], value=float(d["value"]))


@dataclass
class Rule:
    id: str
    sector: str
    target: str
    predicates: list[Predicate]
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "sector": self.sector, "target": self.target,
            "predicates": [p.to_dict() for p in self.predicates],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        if d.get("target") not in VALID_TARGETS:
            raise ValueError(
                f"unknown target {d.get('target')!r}; must be one of {VALID_TARGETS}"
            )
        return cls(
            id=d["id"], sector=d["sector"], target=d["target"],
            predicates=[Predicate.from_dict(p) for p in d.get("predicates", [])],
            notes=d.get("notes", ""),
        )


def apply_predicate(s: pd.Series, op: str, value: float) -> pd.Series:
    """Boolean mask for a predicate against a pandas Series.

    Null values propagate as False — a predicate can't evaluate against a
    missing feature reading, and False is the conservative choice (rule
    doesn't fire on rows we don't have data for).
    """
    if op == ">":  m = s > value
    elif op == ">=": m = s >= value
    elif op == "<":  m = s < value
    elif op == "<=": m = s <= value
    elif op == "==": m = s == value
    elif op == "!=": m = s != value
    else:
        raise ValueError(f"unsupported op {op!r}")
    return m.fillna(False)


def rule_mask(df: pd.DataFrame, rule: Rule) -> pd.Series:
    """Return boolean mask where every predicate fires."""
    if not rule.predicates:
        # A rule with no predicates matches everything — this is usually a
        # client bug. Raise rather than silently returning "fires always".
        raise ValueError(f"rule {rule.id!r} has no predicates")
    mask = pd.Series(True, index=df.index)
    missing_features = [p.feature for p in rule.predicates if p.feature not in df.columns]
    if missing_features:
        raise ValueError(
            f"rule {rule.id!r} references features not in scan rows: {missing_features}"
        )
    for p in rule.predicates:
        mask &= apply_predicate(df[p.feature], p.op, p.value)
    return mask


# ---------------------------------------------------------------------------
# Row-level filters — match the analysis pipeline so testing and discovery
# use the same population
# ---------------------------------------------------------------------------
def apply_standard_filters(
    df: pd.DataFrame,
    target: str,
    drop_0930: bool = True,
    thin_tape_fraction: float = 0.25,
) -> tuple[pd.DataFrame, dict]:
    """Apply filters that make testing comparable to discovery.

    - Drop rows missing the target (scan_price null => target null).
    - Drop 09:30 scans (pre-scan features undefined at session open).
    - Drop thin-tape rows where >thin_tape_fraction of pre-scan minute bars
      are missing. Threshold is per-scan relative to expected minute count.

    Returns (filtered_df, diagnostics) where diagnostics is a dict with row
    counts at each stage. The null-target count is separated out from the
    null-scan-price count because a large null-target count is a strong
    signal that the user needs to recompute their research_rows against a
    newer schema (e.g. after adding target_50bps/75bps columns in v0.3.2).
    """
    n_input = len(df)
    # Null-target rows — computed BEFORE the null-scan-price drop so the two
    # counts don't overlap in a confusing way. A row with scan_price=None
    # already has target=None by definition in feature_computer.
    n_null_target_only = int(df[target].isna().sum())
    n_null_scan_price = int(df["scan_price"].isna().sum())

    df = df.dropna(subset=[target, "scan_price"]).copy()
    n_after_null_drop = len(df)
    logger.info(f"filter: null target/scan_price {n_input}->{n_after_null_drop}")

    n_0930_dropped = 0
    if drop_0930:
        before = len(df)
        df = df[df["scan_time_et"] != "09:30"].copy()
        n_0930_dropped = before - len(df)
        logger.info(f"filter: dropped 09:30 scans {before}->{len(df)}")

    n_thin_tape_dropped = 0
    if thin_tape_fraction is not None and "bars_missing_pre_scan" in df.columns:
        expected = {"10:30": 60, "11:30": 120, "12:30": 180, "13:30": 240, "14:30": 300}
        df["_expected_pre"] = df["scan_time_et"].map(expected)
        df["_missing_frac"] = df["bars_missing_pre_scan"].fillna(0) / df["_expected_pre"]
        before = len(df)
        df = df[df["_missing_frac"] < thin_tape_fraction].copy()
        n_thin_tape_dropped = before - len(df)
        df = df.drop(columns=["_expected_pre", "_missing_frac"])
        logger.info(f"filter: thin-tape {before}->{len(df)}")

    diagnostics = {
        "target_column": target,
        "rows_input": n_input,
        "rows_with_null_target": n_null_target_only,
        "rows_with_null_scan_price": n_null_scan_price,
        "rows_after_null_drop": n_after_null_drop,
        "rows_dropped_0930": n_0930_dropped,
        "rows_dropped_thin_tape": n_thin_tape_dropped,
        "rows_final": len(df),
    }
    # The warning condition: if >5% of input rows were dropped for null target
    # AND the target is one of the newer v0.3.2+ columns, this is almost
    # certainly a "need to recompute" situation, not an ordinary filter miss.
    # We flag it by setting a human-readable warning string; the API layer
    # surfaces it prominently.
    newer_targets = {"target_50bps", "target_peak_50bps", "target_75bps", "target_peak_75bps"}
    if n_input > 0 and n_null_target_only > 0:
        null_frac = n_null_target_only / n_input
        if target in newer_targets and null_frac > 0.05:
            diagnostics["warning"] = (
                f"{n_null_target_only:,} of {n_input:,} rows ({null_frac:.0%}) "
                f"have NULL {target}. This usually means research_rows were "
                f"computed before the v0.3.2 schema added 50bps/75bps target "
                f"columns. Run compute again on the affected date range to "
                f"populate them."
            )
        elif null_frac > 0.20:
            diagnostics["warning"] = (
                f"{n_null_target_only:,} of {n_input:,} rows ({null_frac:.0%}) "
                f"have NULL {target}. Unusually high — check compute coverage."
            )
    return df, diagnostics




def apply_target_only_filters(
    df: pd.DataFrame,
    target: str,
) -> tuple[pd.DataFrame, dict]:
    """Apply only the non-negotiable filters needed for safe evaluation.

    This keeps 09:30 rows and skips thin-tape filtering, but still removes rows
    where the target or scan_price is missing/non-finite. It is the correct
    tester population for opening-scan rules when standard filters are off.
    """
    n_input = len(df)
    target_numeric = pd.to_numeric(df[target], errors="coerce")
    scan_price_numeric = pd.to_numeric(df["scan_price"], errors="coerce")

    n_null_target = int(target_numeric.isna().sum())
    n_nonfinite_target = int((~np.isfinite(target_numeric.dropna())).sum())
    n_null_scan_price = int(scan_price_numeric.isna().sum())
    n_nonfinite_scan_price = int((~np.isfinite(scan_price_numeric.dropna())).sum())

    valid = (
        target_numeric.notna()
        & np.isfinite(target_numeric)
        & scan_price_numeric.notna()
        & np.isfinite(scan_price_numeric)
    )
    out = df.loc[valid].copy()
    if not out.empty:
        out[target] = pd.to_numeric(out[target], errors="coerce").astype(np.int8)

    diagnostics = {
        "target_column": target,
        "filter_mode": "target_only",
        "rows_input": n_input,
        "rows_with_null_target": n_null_target,
        "rows_with_nonfinite_target": n_nonfinite_target,
        "rows_with_null_scan_price": n_null_scan_price,
        "rows_with_nonfinite_scan_price": n_nonfinite_scan_price,
        "rows_after_null_drop": len(out),
        "rows_dropped_0930": 0,
        "rows_dropped_thin_tape": 0,
        "rows_final": len(out),
    }
    if n_input > 0 and n_null_target > 0:
        diagnostics["warning"] = (
            f"{n_null_target:,} of {n_input:,} rows have NULL {target}. "
            "They were excluded with target-only filtering; 09:30 rows were kept."
        )
    return out, diagnostics


# ---------------------------------------------------------------------------
# Per-fold evaluation
# ---------------------------------------------------------------------------
def evaluate_rule_on_slice(
    df: pd.DataFrame, rule: Rule, target: str,
) -> dict:
    """Compute support/precision/lift/day-concentration for a rule on a slice."""
    y = df[target].astype(np.int8).values
    base_rate = float(y.mean()) if len(y) else 0.0
    mask = rule_mask(df, rule).values
    n_total = int(len(df))
    n_fire = int(mask.sum())
    if n_fire == 0:
        return {
            "n_total": n_total, "base_rate": round(base_rate, 6),
            "support": 0, "precision": None, "lift": None,
            "days_firing": 0, "max_day_fraction": None,
            "p_y1_given_silent": None, "probability_shift": None,
            "specificity": None, "recall": None,
        }
    tp = int(((mask == 1) & (y == 1)).sum())
    fp = int(((mask == 1) & (y == 0)).sum())
    fn = int(((mask == 0) & (y == 1)).sum())
    tn = int(((mask == 0) & (y == 0)).sum())
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    specificity = tn / (tn + fp) if (tn + fp) else 0.0
    p_silent = fn / (fn + tn) if (fn + tn) else 0.0
    dates = df["date"].values[mask]
    uniq_dates, counts = np.unique(dates, return_counts=True)
    max_day_frac = float(counts.max() / n_fire)
    return {
        "n_total": n_total, "base_rate": round(base_rate, 6),
        "support": n_fire,
        "precision": round(precision, 6),
        "lift": round(precision / base_rate, 6) if base_rate > 0 else None,
        "days_firing": int(uniq_dates.size),
        "max_day_fraction": round(max_day_frac, 6),
        "p_y1_given_silent": round(p_silent, 6),
        "probability_shift": round(precision - p_silent, 6),
        "specificity": round(specificity, 6),
        "recall": round(recall, 6),
    }


# ---------------------------------------------------------------------------
# Rolling-origin validation — the real value-add over in-session analysis
# ---------------------------------------------------------------------------
def _fold_dates(dates_sorted: list[str], n_folds: int) -> list[tuple[str, str, str]]:
    """Return list of (train_end_exclusive, oos_start, oos_end) for each fold.

    With n_folds=5 and 250 distinct dates, the first fold trains on the first
    50 days and tests on days 50-100; second trains on 0-100, tests on 100-150;
    etc. This is expanding-window, not sliding-window — consistent with how
    you'd retrain in production (more history is always available).
    """
    n = len(dates_sorted)
    if n < n_folds + 1:
        raise ValueError(f"need at least {n_folds + 1} distinct dates, have {n}")
    fold_size = n // (n_folds + 1)  # first block is train-only, rest are test folds
    folds = []
    for i in range(n_folds):
        train_end_idx = fold_size * (i + 1)
        oos_end_idx = min(n, fold_size * (i + 2))
        if oos_end_idx <= train_end_idx:
            continue
        folds.append((
            dates_sorted[train_end_idx],        # train is [start, this date)
            dates_sorted[train_end_idx],        # oos starts at this date (inclusive)
            dates_sorted[oos_end_idx - 1],      # oos ends here (inclusive)
        ))
    return folds


def _year_based_folds(dates_sorted: list[str]) -> list[tuple[str, str, str, str]]:
    """Return year-based expanding-window folds for durability testing.

    Each fold trains on everything strictly before a given year and tests on
    that year. Answers "does the rule work in year Y given everything before
    Y as training?" — which is the honest durability question.

    Returns list of (train_start, train_end_exclusive, oos_start, oos_end).
    Note the different tuple shape vs. _fold_dates: we also return the
    explicit train_start so the fold label can show "trained on 2023-2024,
    tested on 2025" rather than inferring it.

    The first year found is skipped as an OOS fold (there's nothing before
    it to train on). Years with fewer than 30 trading days are also
    skipped — a partial year at the end of the data range doesn't give a
    meaningful precision estimate.

    Example with 3 years of data (2023-2025):
      - fold 1: train 2023-2023, oos 2024
      - fold 2: train 2023-2024, oos 2025
    """
    if not dates_sorted:
        return []
    from collections import defaultdict as _dd
    by_year: dict[int, list[str]] = _dd(list)
    for d in dates_sorted:
        y = int(d[:4])
        by_year[y].append(d)
    years_sorted = sorted(by_year.keys())
    if len(years_sorted) < 2:
        raise ValueError(
            f"year-based folds need ≥2 distinct years in data, have {years_sorted}"
        )
    first_year = years_sorted[0]
    folds: list[tuple[str, str, str, str]] = []
    for year in years_sorted[1:]:  # skip first year — nothing to train on
        year_dates = by_year[year]
        if len(year_dates) < 30:
            continue  # partial year, skip
        train_start = by_year[first_year][0]
        train_end_excl = year_dates[0]          # first day of OOS year
        oos_start = year_dates[0]
        oos_end = year_dates[-1]
        folds.append((train_start, train_end_excl, oos_start, oos_end))
    return folds


def rolling_origin_evaluate(
    df: pd.DataFrame, rule: Rule, n_folds: int = 5,
    fold_mode: Literal["expanding_window", "year_based"] = "expanding_window",
) -> list[dict]:
    """Return per-fold rule stats.

    fold_mode:
      - 'expanding_window' (default): n_folds contiguous OOS windows. Each
        fold's train window is [start, fold_start); OOS is [fold_start,
        fold_end]. Good for shorter data ranges (≤1 year) where calendar
        boundaries aren't meaningful.
      - 'year_based': one fold per distinct calendar year (after the first).
        Each fold trains on all years before year Y and tests on Y. Correct
        answer to "does this rule work across calendar regimes?" — the
        right question for multi-year data. n_folds is ignored in this mode;
        the fold count is determined by how many years the data spans.

    Rule evaluation is computed on the OOS slice; train stats are included
    for stability comparison.
    """
    df = df.sort_values("date").reset_index(drop=True)
    dates_sorted = sorted(df["date"].unique())

    if fold_mode == "year_based":
        year_folds = _year_based_folds(list(dates_sorted))
        out = []
        for i, (train_start, train_end_excl, oos_start, oos_end) in enumerate(year_folds):
            train = df[(df["date"] >= train_start) & (df["date"] < train_end_excl)]
            oos = df[(df["date"] >= oos_start) & (df["date"] <= oos_end)]
            train_stats = evaluate_rule_on_slice(train, rule, rule.target)
            oos_stats = evaluate_rule_on_slice(oos, rule, rule.target)
            out.append({
                "fold": i + 1,
                "fold_label": f"train {train_start[:4]}–{train_end_excl[:4]} / oos {oos_start[:4]}",
                "train_span": [train_start, train_end_excl],
                "oos_span": [oos_start, oos_end],
                "train": train_stats,
                "oos": oos_stats,
            })
        return out

    # expanding_window (the existing behavior)
    folds = _fold_dates(list(dates_sorted), n_folds)
    out = []
    for i, (train_end, oos_start, oos_end) in enumerate(folds):
        train = df[df["date"] < train_end]
        oos = df[(df["date"] >= oos_start) & (df["date"] <= oos_end)]
        train_stats = evaluate_rule_on_slice(train, rule, rule.target)
        oos_stats = evaluate_rule_on_slice(oos, rule, rule.target)
        out.append({
            "fold": i + 1,
            "train_span": [dates_sorted[0], train_end],
            "oos_span": [oos_start, oos_end],
            "train": train_stats,
            "oos": oos_stats,
        })
    return out


# ---------------------------------------------------------------------------
# Database loading — the tester reads from research_rows directly
# ---------------------------------------------------------------------------
def load_scan_rows_from_db(
    db_path: str,
    sector: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Load research_rows for the given sector and optional date range."""
    with storage.connect(db_path) as conn:
        query = "SELECT * FROM research_rows WHERE sector = ?"
        params: list = [sector]
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        query += " ORDER BY date, scan_time_et, symbol"
        df = pd.read_sql_query(query, conn, params=params)
    return df


def load_scan_rows_from_parquet(path: str | Path) -> pd.DataFrame:
    """Fallback ingest path — read a scan-rows parquet produced by the exporter.

    Needed when testing rules against a pack Claude produced in an analysis
    session, before the underlying data has been re-ingested by the collector.
    Lazy pyarrow import so the tester module loads fine without it.
    """
    import pyarrow.parquet as pq  # noqa: F401  (raises ImportError if missing)
    df = pd.read_parquet(path)
    return df


# ---------------------------------------------------------------------------
# Top-level entry point: test a rule bundle
# ---------------------------------------------------------------------------
def test_rule_bundle(
    rules: list[Rule],
    df: pd.DataFrame | None = None,
    db_path: str | None = None,
    sector: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    n_folds: int = 5,
    fold_mode: Literal["expanding_window", "year_based"] = "expanding_window",
    apply_filters: bool = True,
    regime_min_lift: float = 1.3,
) -> dict:
    """Test a list of rules and return per-rule + summary stats.

    Data source: pass `df` directly (e.g. loaded from parquet), OR pass
    db_path+sector to read from the collector's SQLite.

    n_folds: number of rolling-origin folds. Set to 0 to skip (report single
    train/OOS split at 70/30 instead).

    fold_mode: see rolling_origin_evaluate. 'year_based' is the right choice
    for multi-year data (≥2 full calendar years); 'expanding_window' is
    fine for shorter ranges.

    regime_min_lift: threshold for the regime_consistent flag. A rule is
    regime-consistent when its OOS lift >= this value in ≥80% of folds and
    OOS precision is positive in every fold. Default 1.3. Set to 1.0 to
    match the old behavior (not recommended — too permissive).
    """
    if df is None:
        if not db_path or not sector:
            raise ValueError("must pass either df or (db_path, sector)")
        df = load_scan_rows_from_db(db_path, sector, start_date, end_date)

    if df.empty:
        return {
            "error": "no scan rows for the given sector/date range",
            "rules": [], "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }

    results = []
    warnings_seen: list[str] = []
    for rule in rules:
        # Each rule is filtered to its own target's valid rows
        rdf = df
        diagnostics: dict = {}
        if apply_filters:
            rdf, diagnostics = apply_standard_filters(rdf, rule.target)
        else:
            # Keep 09:30 rows for opening-scan rules, but still remove NULL/non-finite
            # target and scan_price rows so evaluation can safely cast the target.
            rdf, diagnostics = apply_target_only_filters(rdf, rule.target)
        if "warning" in diagnostics and diagnostics["warning"] not in warnings_seen:
            warnings_seen.append(diagnostics["warning"])
        if rdf.empty:
            results.append({
                "rule_id": rule.id,
                "error": "no rows remain after filtering",
                "rule": rule.to_dict(),
                "filter_diagnostics": diagnostics,
            })
            continue
        overall = evaluate_rule_on_slice(rdf, rule, rule.target)
        entry = {
            "rule_id": rule.id,
            "rule": rule.to_dict(),
            "n_rows_evaluated": int(len(rdf)),
            "date_range": [str(rdf["date"].min()), str(rdf["date"].max())],
            "fold_mode": fold_mode,
            "overall": overall,
            "filter_diagnostics": diagnostics,
        }
        # Folds
        if n_folds and n_folds > 0 or fold_mode == "year_based":
            try:
                entry["folds"] = rolling_origin_evaluate(
                    rdf, rule, n_folds=n_folds, fold_mode=fold_mode,
                )
                entry["fold_summary"] = _summarize_folds(
                    entry["folds"], min_lift=regime_min_lift,
                )
            except ValueError as e:
                entry["fold_error"] = str(e)
        results.append(entry)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_rules_tested": len(rules),
        "n_rows_in_source": int(len(df)),
        "fold_mode": fold_mode,
        "warnings": warnings_seen,  # deduped, surfaced by the API/UI
        "rules": results,
    }


def _summarize_folds(folds: list[dict], min_lift: float = 1.3) -> dict:
    """Per-fold stability metrics across rolling-origin runs.

    Answers the question: "does this rule's precision hold up across time?"
    - oos_precision_min/median/max: range of per-fold OOS precision
    - oos_lift_min/median/max: same for lift
    - folds_with_support: how many folds had enough rows for the rule to fire
    - regime_consistent: a boolean heuristic — true if oos_precision is
      positive in every fold AND oos_lift >= min_lift in at least 80% of folds.

    Note on min_lift default: the previous version of this function used
    lift > 1.0, which let marginal rules (e.g. lift 1.14 on a 28% base rate)
    pass the bar indistinguishable from rules with lift 2+. The 1.3 default
    is still conservative — it corresponds to ~30% excess precision above
    base rate, which is a meaningful-but-not-heroic threshold. Set it lower
    if you want to catch weak signals too, or higher if you only want to
    flag the clear winners.
    """
    precisions = [f["oos"]["precision"] for f in folds if f["oos"]["precision"] is not None]
    lifts = [f["oos"]["lift"] for f in folds if f["oos"]["lift"] is not None]
    supports = [f["oos"]["support"] for f in folds]
    n_with_support = sum(1 for s in supports if s > 0)
    if not precisions:
        return {
            "folds_with_support": 0,
            "regime_consistent": False,
            "regime_consistent_min_lift": min_lift,
        }
    lift_above_threshold = sum(1 for l in lifts if l >= min_lift)
    regime_consistent = (
        all(p > 0 for p in precisions)
        and lift_above_threshold >= max(1, int(0.8 * len(lifts)))
    )
    return {
        "folds_with_support": int(n_with_support),
        "oos_precision_min": round(min(precisions), 6),
        "oos_precision_median": round(float(np.median(precisions)), 6),
        "oos_precision_max": round(max(precisions), 6),
        "oos_lift_min": round(min(lifts), 6) if lifts else None,
        "oos_lift_median": round(float(np.median(lifts)), 6) if lifts else None,
        "oos_lift_max": round(max(lifts), 6) if lifts else None,
        "oos_support_min": int(min(supports)),
        "oos_support_median": int(np.median(supports)),
        "regime_consistent": regime_consistent,
        "regime_consistent_min_lift": min_lift,
    }


# ---------------------------------------------------------------------------
# Persistence — tracked rules in SQLite so results accumulate across runs
# ---------------------------------------------------------------------------
TRACKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_rules (
    rule_id       TEXT    PRIMARY KEY,
    sector        TEXT    NOT NULL,
    target        TEXT    NOT NULL,
    predicates    TEXT    NOT NULL,        -- JSON-encoded list
    notes         TEXT,
    created_at_utc TEXT   NOT NULL,
    status        TEXT    DEFAULT 'active' -- 'active' | 'retired'
);

CREATE TABLE IF NOT EXISTS rule_test_runs (
    run_id        TEXT    PRIMARY KEY,
    rule_id       TEXT    NOT NULL,
    tested_at_utc TEXT    NOT NULL,
    data_start    TEXT,
    data_end      TEXT,
    n_rows        INTEGER,
    overall_precision REAL,
    overall_lift  REAL,
    overall_support INTEGER,
    fold_summary_json TEXT,  -- full _summarize_folds dict
    full_result_json  TEXT,  -- full per-rule entry
    FOREIGN KEY (rule_id) REFERENCES tracked_rules(rule_id)
);

CREATE INDEX IF NOT EXISTS idx_rule_test_runs_rule
    ON rule_test_runs(rule_id, tested_at_utc);
"""


def init_tracker_schema(db_path: str) -> None:
    with storage.connect(db_path) as conn:
        conn.executescript(TRACKER_SCHEMA)


def track_rule(db_path: str, rule: Rule) -> None:
    """Insert or update a tracked rule."""
    init_tracker_schema(db_path)
    with storage.connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tracked_rules
               (rule_id, sector, target, predicates, notes, created_at_utc, status)
               VALUES (?, ?, ?, ?, ?, COALESCE(
                    (SELECT created_at_utc FROM tracked_rules WHERE rule_id = ?),
                    ?
               ), 'active')""",
            (rule.id, rule.sector, rule.target,
             json.dumps([p.to_dict() for p in rule.predicates]),
             rule.notes,
             rule.id,
             datetime.now(timezone.utc).isoformat()),
        )


def retire_rule(db_path: str, rule_id: str) -> None:
    init_tracker_schema(db_path)
    with storage.connect(db_path) as conn:
        conn.execute(
            "UPDATE tracked_rules SET status = 'retired' WHERE rule_id = ?",
            (rule_id,),
        )


def list_tracked_rules(db_path: str, status: str | None = "active") -> list[dict]:
    init_tracker_schema(db_path)
    with storage.connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tracked_rules WHERE status = ? ORDER BY rule_id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tracked_rules ORDER BY rule_id"
            ).fetchall()
    out = []
    for r in rows:
        out.append({
            "rule_id": r["rule_id"],
            "sector": r["sector"],
            "target": r["target"],
            "predicates": json.loads(r["predicates"]),
            "notes": r["notes"] or "",
            "created_at_utc": r["created_at_utc"],
            "status": r["status"],
        })
    return out


def record_test_run(db_path: str, result: dict) -> None:
    """Persist the output of test_rule_bundle so rule history accumulates."""
    init_tracker_schema(db_path)
    with storage.connect(db_path) as conn:
        for entry in result.get("rules", []):
            if "error" in entry:
                continue
            rule_id = entry["rule_id"]
            overall = entry.get("overall", {})
            conn.execute(
                """INSERT INTO rule_test_runs
                   (run_id, rule_id, tested_at_utc, data_start, data_end,
                    n_rows, overall_precision, overall_lift, overall_support,
                    fold_summary_json, full_result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), rule_id,
                    result["generated_at_utc"],
                    entry["date_range"][0], entry["date_range"][1],
                    entry.get("n_rows_evaluated"),
                    overall.get("precision"),
                    overall.get("lift"),
                    overall.get("support"),
                    json.dumps(entry.get("fold_summary", {})),
                    json.dumps(entry),
                ),
            )


def rule_history(db_path: str, rule_id: str) -> list[dict]:
    init_tracker_schema(db_path)
    with storage.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT run_id, tested_at_utc, data_start, data_end, n_rows,
                      overall_precision, overall_lift, overall_support,
                      fold_summary_json
               FROM rule_test_runs
               WHERE rule_id = ?
               ORDER BY tested_at_utc""",
            (rule_id,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "run_id": r["run_id"],
            "tested_at_utc": r["tested_at_utc"],
            "data_span": [r["data_start"], r["data_end"]],
            "n_rows": r["n_rows"],
            "precision": r["overall_precision"],
            "lift": r["overall_lift"],
            "support": r["overall_support"],
            "fold_summary": json.loads(r["fold_summary_json"] or "{}"),
        })
    return out
