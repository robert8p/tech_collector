"""Path-dependent backtest harness (v0.6.0).

Simulates realized P&L for a tracked rule against historical minute bars.
Unlike the rule tester (which measures whether the target was *touched*),
this module asks "would the trade have been profitable" by simulating
take-profit / stop-loss / time-stop exits on minute-bar paths.

Key behaviors
=============
1. For each signal the rule produces, walk forward from the signal minute
   using raw minute bars, applying TP / SL / time-stop whichever triggers
   first. When both TP and SL levels fall inside the same minute bar's
   high-low range, we assume SL hit first (conservative).

2. If raw_bars are not in the DB for a (symbol, signal_date), we pull
   them just-in-time via collector.backfill_range and optionally delete
   them after processing the day. This keeps storage proportional to one
   trading day, matching v0.5.0's chained-backfill discard semantics.

3. Slippage is applied symmetrically: entry price *= (1 + slip/2), exit
   price *= (1 - slip/2) where slip is the round-trip bps. This models
   the "real fill is worse than the midpoint" assumption typical for
   market orders.

4. Optional filters:
   - spy_regime_filter: skip signals when SPY's return since market open
     is below the threshold. Captures "rule breaks down when market is
     weak" — one of the findings from the miss-set analysis.
   - symbol_exclude: skip signals on named symbols. Useful for testing
     whether dropping high-miss names (SMCI, LITE, etc.) improves P&L.

What this module does NOT do
============================
- Concurrent signal / position sizing: every signal gets a full unit
  simulated independently. A full portfolio sim would require weighting.
- Order queue realism: assumes you can get filled at the signal bar's
  close price (plus slippage). Real market orders against fast-moving
  stocks can slip much more than fixed bps.
- Overnight positions: time-stop always forces flatten before session
  close (default 15:30 ET).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import collector, config, rule_tester, storage

logger = logging.getLogger(__name__)

# Canonical ET timezone — handles DST transitions correctly.
ET = ZoneInfo("America/New_York")


def _bar_ts_to_et(bar_dt_utc: datetime) -> datetime:
    """Convert a UTC-aware datetime to ET via zoneinfo.

    Handles EDT/EST transitions correctly. Replaces the old
    _utc_hour_to_et approximation which was month-boundary-based and
    off-by-one-hour on DST transition days. This function is the single
    source of truth for ET in the backtest engine.
    """
    if bar_dt_utc.tzinfo is None:
        bar_dt_utc = bar_dt_utc.replace(tzinfo=timezone.utc)
    return bar_dt_utc.astimezone(ET)


# ---------------------------------------------------------------------------
# v0.7.0: conditional exits — different TP/SL per signal based on a feature
# value. Used to test "Option C": different exit profiles for gap-open vs
# gap-filled signals of the same rule.
# ---------------------------------------------------------------------------
@dataclass
class ConditionalExitBranch:
    """One branch of a conditional-exit spec.

    A branch activates when `feature` compares `op` against `value` for a
    given signal. When it activates, trade uses tp_bps/sl_bps overrides and
    the resulting net_return_bps is multiplied by position_size (for
    size-weighted variants).

    op is one of: '==', '!=', '<', '<=', '>', '>='.
    """
    feature: str
    op: str
    value: float
    tp_bps: float
    sl_bps: float
    position_size: float = 1.0   # 1.0 = full size, 0.5 = half size
    label: str = ""              # optional human label for reporting


def _branch_matches(sig: dict, branch: ConditionalExitBranch) -> bool:
    """Test whether a signal row matches this branch's predicate."""
    val = sig.get(branch.feature)
    if val is None:
        return False
    try:
        val = float(val)
    except (TypeError, ValueError):
        return False
    ops = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<":  lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        ">":  lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
    }
    op_fn = ops.get(branch.op)
    if op_fn is None:
        raise ValueError(f"Unknown op {branch.op!r} in ConditionalExitBranch")
    return op_fn(val, branch.value)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    """Parameters controlling a single backtest run."""

    rule: rule_tester.Rule          # the rule whose signals we simulate
    tp_bps: float                   # take-profit in bps (e.g. 50.0 = 0.5%)
    sl_bps: float                   # stop-loss in bps. Positive = drawdown pct.
    timestop_et: str | None = "15:50"  # flatten at or after this ET time. None = no timestop (v0.7.7)
    slippage_bps: float = 10.0      # round-trip slippage (applied half/half)
    spy_regime_filter: float | None = None  # e.g. -0.002 to skip if SPY < -0.2%
    symbol_exclude: list[str] = field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    # Behavior tuning
    just_in_time_backfill: bool = True  # pull raw_bars per (symbol,date) if missing
    delete_raw_bars_after: bool = False  # clean up to save space after each day
    entry_slippage_split: float = 0.5    # fraction of slip applied on entry side
    # v0.7.0: conditional-exit branches. If non-empty, each signal is matched
    # against branches in order and the FIRST match wins. If no branch
    # matches, fall back to the top-level tp_bps/sl_bps and position_size=1.
    conditional_exits: list[ConditionalExitBranch] = field(default_factory=list)
    # v0.7.16: signal-row filtering mode.
    # "standard" preserves legacy behavior: drop 09:30 and thin-tape rows.
    # "target_only" keeps 09:30 rows but still drops rows with null target/scan_price.
    # This is required for opening-scan rule backtests without reintroducing NULL-target crashes.
    filter_mode: str = "standard"
    # v0.7.18: minimum minutes after the entry timestamp before TP/SL exits are allowed.
    # Use 1 for a conservative audit of 09:30 rules so same-minute bar highs/lows
    # cannot dominate results. Entry price remains the configured entry reference; only exit
    # eligibility is delayed. Legacy default is 0.
    min_exit_minutes: int = 0
    # v0.7.20: delay the simulated entry itself after the scan timestamp.
    # 0 = legacy: fill at research_rows.scan_price at the scan timestamp.
    # 1 = conservative opening audit: if a 09:30 signal fires, enter at the 09:31
    # raw bar open instead of assuming a fill at the 09:30 scan_price.
    entry_delay_minutes: int = 0
    # v0.7.23: optional per-day signal cap/ranking for shortlist realism.
    # When max_signals_per_day is set, after the rule fires and after any
    # regime filter, keep only the top/bottom N signals per signal date using
    # rank_feature. This is intended for event-day rules that otherwise fire
    # on most of the sector at once. Ranking uses scan-time signal-row fields
    # only; outcome columns must never be used here.
    max_signals_per_day: int | None = None
    rank_feature: str | None = None
    rank_direction: str = "desc"  # "desc" = largest values first; "asc" = smallest first.


# v0.7.21: diagnostic feature export for execution-aligned rule discovery.
# These columns are copied from the signal row into the downloadable trades CSV
# with a `sig_` prefix. This does NOT change simulation behaviour or DB schema;
# it only makes uploaded backtest outputs useful for finding why actual
# executable trades won/lost.
BACKTEST_SIGNAL_EXPORT_COLUMNS = [
    # outcomes / audit fields (diagnostic only; never use as predictors)
    "target", "return_to_cutoff", "min_return_before_cutoff", "max_return_before_cutoff",
    "return_at_scan_plus_30m", "return_at_scan_plus_60m",
    "return_at_scan_plus_90m", "return_at_scan_plus_120m",
    "target_25bps", "target_50bps", "target_75bps",
    "target_peak_25bps", "target_peak_50bps", "target_peak_75bps",
    "bars_missing_pre_scan", "bars_missing_post_scan",
    # scan-time candidate predictors
    "minutes_since_open", "day_of_week", "open_to_scan_return", "gap_pct",
    "intraday_range_position", "distance_to_vwap", "distance_to_day_high",
    "distance_to_day_low", "rsi_14", "macd_hist", "ema_9_distance",
    "ema_20_distance", "ema_50_distance", "relative_volume",
    "realized_vol_so_far", "rs_leakfree", "momentum", "rel_volume_r2k",
    "vwap_slope", "orb_strength", "atr_reach", "trend_str", "range_expansion",
    "spy_ret", "ret_vs_spy", "spy_momentum", "mom_vs_spy", "spy_vol",
    "gap_filled", "range_tightness_30m", "bars_in_range_20bps", "is_nr7",
    "dist_to_day_high_bps", "broke_day_high_this_bar",
    "broke_opening_range_high", "bars_since_day_high",
    "dist_to_prev_close_bps", "dist_to_5d_high_bps", "dist_to_20d_high_bps",
    "days_since_20d_high", "volume_acceleration",
    "cumulative_volume_vs_typical", "sector_breadth_up",
    "new_highs_in_sector", "regime_ok",
]


def _clean_export_value(v):
    """Return a CSV-safe scalar value for a signal-row field."""
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _signal_export_fields(sig: pd.Series) -> dict:
    out: dict = {}
    for col in BACKTEST_SIGNAL_EXPORT_COLUMNS:
        if col in sig.index:
            out[f"sig_{col}"] = _clean_export_value(sig.get(col))
    return out


def _entry_vs_scan_bps(sig: pd.Series, entry_price_used: float) -> float | None:
    try:
        scan_px = float(sig.get("scan_price"))
        if not math.isfinite(scan_px) or scan_px <= 0:
            return None
        return (float(entry_price_used) / scan_px - 1.0) * 10_000.0
    except Exception:
        return None



# ---------------------------------------------------------------------------
# Simulation — core path-dependent logic
# ---------------------------------------------------------------------------
def _simulate_trade(
    bars: list[dict],
    entry_ts_utc: str,
    entry_price: float,
    tp_level: float,
    sl_level: float,
    timestop_et_hhmm: str,
    slippage_bps: float,
    entry_slippage_split: float,
    min_exit_minutes: int = 0,
) -> dict:
    """Walk forward through minute bars from entry_ts onwards.

    Returns a dict with exit_price, exit_time_et, exit_reason, minutes_held,
    gross_return_bps, net_return_bps. exit_reason in {'TP','SL','TIME','NO_DATA'}.

    Assumes bars are sorted ascending by timestamp. Entry is at the given
    entry_price (already the scan_price from research_rows, set by the
    caller). We do NOT re-entry from a particular bar's open — the scan
    price is the "fill" reference, and slippage is applied on top.

    Intra-bar TP/SL resolution: if both levels fall within a bar's low-high
    range, we treat SL as triggering first (conservative assumption). This
    is material because winning-trade drawdown is the single biggest risk
    we identified: real minute bars frequently dip below SL before high
    reaches TP.

    v0.7.1 fix: previously, if the main loop terminated without finding a
    TP/SL/timestop exit (e.g. due to timezone-conversion issues that made
    the timestop check never fire, or sparse bars around the scan time),
    the fallback path at the bottom would take the LAST bar's close as a
    TIME exit — WITHOUT re-scanning the bars for TP/SL crossings. On big
    intraday moves this produced phantom TIME exits with returns of
    +20%+ on trades that should have been TP exits at +0.75%. This fix:
      1. Uses zoneinfo for correct UTC→ET conversion (handles DST edges)
      2. Before returning TIME, scans ALL post-entry bars for TP/SL hits
      3. Adds an invariant check: TIME returns must be within the TP/SL
         bounds (plus slippage tolerance); violation raises.
    """
    # Walk bars starting from entry timestamp (inclusive of bars AFTER entry)
    try:
        entry_dt = datetime.fromisoformat(entry_ts_utc.replace("Z", "+00:00"))
    except ValueError:
        entry_dt = datetime.fromisoformat(entry_ts_utc)
    # Ensure entry_dt is timezone-aware in UTC for safe comparisons later
    if entry_dt.tzinfo is None:
        entry_dt = entry_dt.replace(tzinfo=timezone.utc)

    # v0.7.14: entry-time-in-regular-session invariant. This is the correct
    # defense-in-depth against the AH-bar phantom case (originally targeted by
    # the v0.7.12 main-loop gross-based invariant, which had to be removed in
    # v0.7.14 — see comment block where the timestop exit fires).
    #
    # The phantom signature is: _find_scan_bar_ts (or any future caller) hands
    # _simulate_trade an entry_ts_utc whose ET time is in after-hours (>= 16:00
    # ET) or pre-market (< 09:30 ET). The simulator then anchors on that AH
    # bar and produces gross values driven by overnight gaps. _find_scan_bar_ts
    # has had a regular-session guard since v0.7.12, so this invariant exists
    # solely to catch any future regression at the bar-selection layer.
    #
    # This check is on entry_ts ONLY — not on bar prices, gross, or any
    # downstream outcome. That's the key change from v0.7.12/v0.7.13: we
    # check the actual structural condition (entry was AH) rather than a
    # downstream proxy (gross was large), because the gross-based proxy has
    # legitimate-case false positives we cannot eliminate without making the
    # invariant useless.
    _entry_et = _bar_ts_to_et(entry_dt)
    if (_entry_et.hour < 9 or _entry_et.hour >= 16
            or (_entry_et.hour == 9 and _entry_et.minute < 30)):
        raise AssertionError(
            f"Entry-time invariant violated: entry_ts={entry_ts_utc} "
            f"resolves to {_entry_et.strftime('%Y-%m-%d %H:%M ET')}, which "
            f"is outside regular ET trading session 09:30-15:59. "
            f"_find_scan_bar_ts session guard should have prevented this. "
            f"Caller passed entry_ts={entry_ts_utc}, entry_price={entry_price}."
        )

    # Convert timestop to a UTC datetime on the same day as entry
    # (SPY session close is 20:00 UTC in summer, 21:00 UTC in winter.)
    # We just check the ET portion of each bar's timestamp.
    # v0.7.7: timestop_et_hhmm may be None / empty to disable the timestop
    # entirely. When disabled, trades only exit on TP or SL (or on the
    # last available bar at end-of-session as a legitimate TIME fallback).
    if timestop_et_hhmm:
        timestop_h, timestop_m = map(int, timestop_et_hhmm.split(":"))
        timestop_enabled = True
    else:
        timestop_h, timestop_m = 99, 99  # sentinel — never triggers
        timestop_enabled = False

    slip_entry_mult = 1.0 + (slippage_bps / 10_000.0) * entry_slippage_split
    slip_exit_mult = 1.0 - (slippage_bps / 10_000.0) * (1 - entry_slippage_split)

    # Apply entry slippage to the reference fill price
    effective_entry = entry_price * slip_entry_mult

    tp_price = effective_entry * (1 + tp_level / 10_000.0)
    sl_price = effective_entry * (1 - sl_level / 10_000.0)

    # v0.7.18: conservative same-minute-exit audit control.
    # If min_exit_minutes=1, a 09:30 signal cannot exit from the 09:30 bar's
    # high/low. This does not claim to be final execution realism; it is a
    # deliberate bias check for opening-scan rules.
    try:
        min_exit_minutes_int = max(0, int(min_exit_minutes or 0))
    except (TypeError, ValueError):
        min_exit_minutes_int = 0

    # Pre-parse all relevant bars once, in ET, for the main loop + audit.
    # This eliminates re-parsing mistakes and gives us a guaranteed-correct
    # timeline to scan during the fallback.
    parsed = []  # (bar_dt_utc, et_hh, et_mm, bar)
    for bar in bars:
        ts = bar["timestamp_utc"]
        try:
            bar_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            bar_dt = datetime.fromisoformat(ts)
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=timezone.utc)
        if bar_dt < entry_dt:
            continue
        et = _bar_ts_to_et(bar_dt)
        parsed.append((bar_dt, et.hour, et.minute, bar))

    # Main loop: iterate post-entry bars once, check timestop -> TP/SL
    for bar_dt, et_hh, et_mm, bar in parsed:
        minutes_from_entry = int((bar_dt - entry_dt).total_seconds() / 60)
        exit_eligible = minutes_from_entry >= min_exit_minutes_int
        # Has timestop triggered? Check BEFORE processing TP/SL so we don't
        # award a post-timestop TP exit.
        if timestop_enabled and ((et_hh > timestop_h) or (et_hh == timestop_h and et_mm >= timestop_m)):
            exit_price = bar["open"] * slip_exit_mult
            gross_bps = (bar["open"] - entry_price) / entry_price * 10_000
            net_bps = (exit_price - effective_entry) / effective_entry * 10_000
            # v0.7.14: the v0.7.12-introduced gross-based invariant on this
            # path was REMOVED. It was unsound: it claimed timestop TIME
            # exits must have gross within [-sl_level, +tp_level] (later
            # corrected to use effective_entry in v0.7.13), but legitimate
            # timestop exits CAN have gross outside that range when there
            # is an inter-bar gap immediately preceding the timestop bar
            # (auctions, halts, news pops). In that case the bar JUST
            # BEFORE timestop had high < tp_price (so TP correctly didn't
            # fire) but the timestop bar's open is ABOVE tp_price due to
            # the gap. The engine intentionally exits at TIME (not TP) on
            # the timestop bar — see the docstring at the top of this
            # function and the comment above this block — so the gross
            # CAN exceed tp_level legitimately.
            #
            # The original purpose of the v0.7.12 invariant was defense-in-
            # depth against AH-bar entries leaking past _find_scan_bar_ts.
            # That defense is now provided by the entry-time-in-session
            # check at the top of this function (v0.7.14), which targets
            # the actual phantom condition rather than a downstream proxy.
            #
            # Production failure that exposed v0.7.13's still-unsound
            # invariant: 2026-03-05 IT, entry 13:30 ET (regular session)
            # at 280.75, timestop_open 282.51, tp_price 282.37. The gap
            # from prior bar's close (~282.30) to timestop bar's open
            # (282.51) put the timestop_open above tp_price even though
            # no prior bar's high reached tp_price. That trade is a
            # legitimate TIME exit with raw gross +62.7 bps and effective
            # gross +55.2 bps — both above tp_level=50, but neither is a
            # phantom signature.
            return {
                "exit_price": exit_price,
                "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
                "exit_reason": "TIME",
                "minutes_held": int((bar_dt - entry_dt).total_seconds() / 60),
                "gross_return_bps": gross_bps,
                "net_return_bps": net_bps,
            }

        # Check intra-bar TP and SL only after the configured minimum exit delay.
        if not exit_eligible:
            continue
        hit_tp = bar["high"] >= tp_price
        hit_sl = bar["low"] <= sl_price
        if hit_tp and hit_sl:
            # Both in same bar: assume SL fired first (conservative)
            exit_price_raw = sl_price
            exit_reason = "SL"
        elif hit_tp:
            exit_price_raw = tp_price
            exit_reason = "TP"
        elif hit_sl:
            exit_price_raw = sl_price
            exit_reason = "SL"
        else:
            continue

        exit_price = exit_price_raw * slip_exit_mult
        gross_bps = (exit_price_raw - entry_price) / entry_price * 10_000
        net_bps = (exit_price - effective_entry) / effective_entry * 10_000
        return {
            "exit_price": exit_price,
            "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
            "exit_reason": exit_reason,
            "minutes_held": int((bar_dt - entry_dt).total_seconds() / 60),
            "gross_return_bps": gross_bps,
            "net_return_bps": net_bps,
        }

    # Ran out of bars without hitting TP/SL or timestop.
    # v0.7.1 fix: before returning TIME, audit the full bar list to ensure
    # we didn't miss a TP/SL crossing. This catches bugs in the main loop
    # (e.g. timezone-conversion errors causing bars to be misinterpreted).
    if parsed:
        for bar_dt, et_hh, et_mm, bar in parsed:
            minutes_from_entry = int((bar_dt - entry_dt).total_seconds() / 60)
            if minutes_from_entry < min_exit_minutes_int:
                continue
            if bar["high"] >= tp_price and bar["low"] <= sl_price:
                # Ambiguous: SL first (conservative)
                exit_price_raw = sl_price
                exit_reason = "SL"
            elif bar["high"] >= tp_price:
                exit_price_raw = tp_price
                exit_reason = "TP"
            elif bar["low"] <= sl_price:
                exit_price_raw = sl_price
                exit_reason = "SL"
            else:
                continue
            exit_price = exit_price_raw * slip_exit_mult
            gross_bps = (exit_price_raw - entry_price) / entry_price * 10_000
            net_bps = (exit_price - effective_entry) / effective_entry * 10_000
            return {
                "exit_price": exit_price,
                "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
                "exit_reason": exit_reason,
                "minutes_held": int((bar_dt - entry_dt).total_seconds() / 60),
                "gross_return_bps": gross_bps,
                "net_return_bps": net_bps,
            }

        # Truly no TP/SL crossing: legitimate TIME exit at last bar's close
        last_dt, et_hh, et_mm, last = parsed[-1]
        exit_price = last["close"] * slip_exit_mult
        gross_bps = (last["close"] - entry_price) / entry_price * 10_000
        net_bps = (exit_price - effective_entry) / effective_entry * 10_000
        # Invariant check: a TIME exit means neither TP nor SL fired across
        # the entire held bar range. The price at exit (last bar's close)
        # must therefore lie within [-sl_level, +tp_level] of effective_entry
        # — the same basis used by the bar-vs-level checks for TP/SL inside
        # the main loop.
        #
        # v0.7.13: fixed to use effective_entry, matching the main-loop TIME
        # exit invariant. v0.7.1 introduced this check using raw entry_price
        # which had the same arithmetic bug as the v0.7.12 main-loop
        # invariant — see backtest.py main-loop comment block for full
        # explanation. The bug rarely fired here in practice because the
        # fallback path is only taken with timestop disabled AND no TP/SL
        # in any bar, but correcting it is essential for honesty.
        gross_eff_bps = (last["close"] - effective_entry) / effective_entry * 10_000
        tol = 1.0  # 1 bp tolerance
        if gross_eff_bps > tp_level + tol or gross_eff_bps < -sl_level - tol:
            raise AssertionError(
                f"TIME exit (fallback) invariant violated: "
                f"gross_eff_bps={gross_eff_bps:.2f} outside [-{sl_level}, +{tp_level}] "
                f"(gross_bps_raw={gross_bps:.2f}). Check bar data integrity "
                f"for entry_ts={entry_ts_utc}, entry_price={entry_price}, "
                f"effective_entry={effective_entry:.4f}, "
                f"tp_price={tp_price:.4f}, sl_price={sl_price:.4f}, "
                f"last_close={last['close']:.4f}."
            )
        return {
            "exit_price": exit_price,
            "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
            "exit_reason": "TIME",
            "minutes_held": int((last_dt - entry_dt).total_seconds() / 60),
            "gross_return_bps": gross_bps,
            "net_return_bps": net_bps,
        }
    return {
        "exit_price": entry_price,
        "exit_time_et": "NA",
        "exit_reason": "NO_DATA",
        "minutes_held": 0,
        "gross_return_bps": 0.0,
        "net_return_bps": 0.0,
    }


def _dead_removed_utc_hour_to_et_noop(*args, **kwargs):
    """Intentionally removed in v0.7.8. The approximate month-boundary DST
    converter `_utc_hour_to_et` was replaced by zoneinfo-based `_bar_ts_to_et`.
    All callers have been migrated. This stub exists so that any stale
    reference fails loudly at call time rather than silently producing
    wrong answers for ~4% of trading days per year (first week of March,
    first days of November).
    """
    raise RuntimeError(
        "_utc_hour_to_et was removed in v0.7.8 — use _bar_ts_to_et (zoneinfo) "
        "instead. The approximation was wrong on DST transition weeks. "
        "See backtest.py::_bar_ts_to_et."
    )


# Preserve the old name as an alias to the guard so reimports fail loudly.
_utc_hour_to_et = _dead_removed_utc_hour_to_et_noop


# ---------------------------------------------------------------------------
# Signal generation & per-day orchestration
# ---------------------------------------------------------------------------
def _load_signals(
    db_path: str, bt: BacktestConfig,
) -> pd.DataFrame:
    """Load research_rows for the rule's sector + date range, apply the rule,
    return a DataFrame of signals (one row per fire)."""
    df = rule_tester.load_scan_rows_from_db(
        db_path, bt.rule.sector,
        start_date=bt.start_date, end_date=bt.end_date,
    )
    if df.empty:
        return df
    filter_mode = (bt.filter_mode or "standard").strip().lower()
    if filter_mode in {"target_only", "target-only", "targetonly"}:
        df, _diag = rule_tester.apply_standard_filters(
            df, bt.rule.target, drop_0930=False, thin_tape_fraction=None
        )
    elif filter_mode == "standard":
        df, _diag = rule_tester.apply_standard_filters(df, bt.rule.target)
    else:
        raise ValueError(
            f"Unsupported backtest filter_mode {bt.filter_mode!r}. "
            "Use 'standard' or 'target_only'."
        )
    mask = rule_tester.rule_mask(df, bt.rule)
    signals = df[mask].copy()
    # Drop excluded symbols
    if bt.symbol_exclude:
        excl = set(s.strip().upper() for s in bt.symbol_exclude)
        before = len(signals)
        signals = signals[~signals["symbol"].str.upper().isin(excl)].copy()
        logger.info(f"symbol_exclude: {before - len(signals)} signals dropped")
    return signals


def _apply_spy_regime_filter(
    signals: pd.DataFrame, db_path: str, threshold: float,
) -> tuple[pd.DataFrame, int]:
    """Compute SPY return from market open to each signal's scan time.

    Filter out signals where SPY's return since open is below `threshold`.
    Threshold is a decimal (e.g. -0.002 for -0.2%). Returns (filtered_signals,
    n_dropped).
    """
    # For each unique date, pull SPY's 09:30 open and each scan time's bar
    # from raw_bars. If SPY raw_bars aren't present for a given date, we
    # cannot apply the filter — keep the signal (safer than dropping).
    dates = sorted(signals["date"].unique())
    spy_open_by_date: dict[str, float] = {}
    spy_price_by_scan: dict[tuple[str, str], float] = {}
    with storage.connect(db_path) as conn:
        for d in dates:
            spy_bars = storage.get_raw_bars_for_day(conn, "SPY", d)
            if not spy_bars:
                continue
            # First bar of the day at/after 09:30 ET is the open proxy
            # v0.7.8: zoneinfo via _bar_ts_to_et (previously used
            # _utc_hour_to_et approximation; wrong in first week of March
            # and first days of November)
            for b in spy_bars:
                bdt = datetime.fromisoformat(b["timestamp_utc"].replace("Z", "+00:00"))
                if bdt.tzinfo is None:
                    bdt = bdt.replace(tzinfo=timezone.utc)
                if _bar_ts_to_et(bdt).hour >= 9:
                    spy_open_by_date[d] = b["open"]
                    break
            # Snapshot SPY price at each scan time (e.g. 10:30, 11:30, etc.)
            for scan_time in ("10:30", "11:30", "12:30", "13:30", "14:30"):
                target_hh, target_mm = map(int, scan_time.split(":"))
                for b in spy_bars:
                    bdt = datetime.fromisoformat(b["timestamp_utc"].replace("Z", "+00:00"))
                    if bdt.tzinfo is None:
                        bdt = bdt.replace(tzinfo=timezone.utc)
                    et = _bar_ts_to_et(bdt)
                    if et.hour > target_hh or (et.hour == target_hh and et.minute >= target_mm):
                        spy_price_by_scan[(d, scan_time)] = b["close"]
                        break
    keep = []
    dropped = 0
    for _, sig in signals.iterrows():
        d = sig["date"]; st = sig["scan_time_et"]
        open_px = spy_open_by_date.get(d)
        scan_px = spy_price_by_scan.get((d, st))
        if open_px is None or scan_px is None:
            keep.append(True)
            continue
        spy_ret = (scan_px - open_px) / open_px
        if spy_ret < threshold:
            dropped += 1
            keep.append(False)
        else:
            keep.append(True)
    return signals[keep].copy(), dropped


def _ensure_raw_bars(
    conn: sqlite3.Connection, db_path: str, symbol: str, date: str,
    pull_if_missing: bool,
) -> list[dict]:
    """Return minute bars for (symbol, date), pulling from Alpaca if missing.

    IMPORTANT: the collector module exposes ``collect_range`` (not
    ``backfill_range`` — the latter was a hallucinated name in the v0.6.0
    build that failed silently via AttributeError → empty bars on every
    call). This is why v0.6.0/v0.6.1/v0.6.2 runs showed 932 NO_DATA
    entries reproducibly on every retry: the JIT path never actually
    hit Alpaca.
    """
    bars = storage.get_raw_bars_for_day(conn, symbol, date)
    if bars or not pull_if_missing:
        return bars
    logger.info(f"just-in-time backfill: {symbol} {date}")
    try:
        collector.collect_range(
            start_date=date, end_date=date,
            symbols=[symbol], db_path=db_path,
            sector=None,
        )
    except Exception as e:
        logger.error(f"just-in-time backfill failed for {symbol} {date}: {e}")
        return []
    # Re-query
    return storage.get_raw_bars_for_day(conn, symbol, date)



def _apply_signal_rank_cap(signals: pd.DataFrame, bt: BacktestConfig) -> tuple[pd.DataFrame, int]:
    """v0.7.23: keep only the top/bottom N signals per signal date.

    Returns (filtered_signals, n_dropped). This runs after the rule predicates
    and optional SPY regime filter, before raw-bar simulation. It is deliberately
    simple and transparent so the trades CSV remains a faithful record of the
    subset actually backtested.

    Guardrails:
    - max_signals_per_day <= 0 means disabled.
    - rank_feature accepts either raw names ("relative_volume") or exported
      names ("sig_relative_volume") and maps to the signal DataFrame column.
    - Missing/non-finite rank values are pushed to the back of the ranking.
    """
    try:
        n = int(bt.max_signals_per_day or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return signals, 0
    if not bt.rank_feature:
        raise ValueError("max_signals_per_day requires rank_feature")

    feature = str(bt.rank_feature).strip()
    if feature.startswith("sig_"):
        feature = feature[4:]
    if feature not in signals.columns:
        raise ValueError(f"rank_feature {bt.rank_feature!r} not found in signal rows")

    direction = (bt.rank_direction or "desc").strip().lower()
    if direction not in ("desc", "asc"):
        raise ValueError("rank_direction must be 'desc' or 'asc'")

    ranked = signals.copy()
    ranked["_rank_value"] = pd.to_numeric(ranked[feature], errors="coerce")
    # Missing/non-finite values should not win the shortlist.
    if direction == "desc":
        ranked["_rank_sort"] = ranked["_rank_value"].replace([np.inf, -np.inf], np.nan).fillna(-np.inf)
        ascending = False
    else:
        ranked["_rank_sort"] = ranked["_rank_value"].replace([np.inf, -np.inf], np.nan).fillna(np.inf)
        ascending = True

    sort_cols = ["date", "_rank_sort", "symbol", "scan_time_et"]
    ranked = ranked.sort_values(sort_cols, ascending=[True, ascending, True, True])
    capped = ranked.groupby("date", sort=False, group_keys=False).head(n).copy()
    dropped = len(signals) - len(capped)
    capped = capped.drop(columns=["_rank_value", "_rank_sort"], errors="ignore")
    return capped, dropped


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_backtest(
    bt: BacktestConfig, db_path: str = config.DB_PATH,
) -> dict:
    """Execute a backtest and return the summary + store results."""
    storage.init_backtest_schema(db_path)
    run_uuid = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()

    signals = _load_signals(db_path, bt)
    n_signals_total = len(signals)
    if n_signals_total == 0:
        return {
            "run_uuid": run_uuid, "n_signals_total": 0, "n_trades": 0,
            "error": "no signals produced by rule in given date range",
        }

    n_skipped_regime = 0
    if bt.spy_regime_filter is not None:
        signals, n_skipped_regime = _apply_spy_regime_filter(
            signals, db_path, bt.spy_regime_filter,
        )

    n_skipped_rank_cap = 0
    signals, n_skipped_rank_cap = _apply_signal_rank_cap(signals, bt)

    # Group by (symbol, date) so we can amortize the raw_bars fetch
    signals = signals.sort_values(["date", "symbol", "scan_time_et"]).copy()
    trades: list[dict] = []

    with storage.connect(db_path) as conn:
        for (symbol, date), group in signals.groupby(["symbol", "date"], sort=False):
            bars = _ensure_raw_bars(
                conn, db_path, symbol, date, bt.just_in_time_backfill,
            )
            if not bars:
                # record a NO_DATA trade per signal so the run's coverage
                # is transparent; net_bps = 0 so it doesn't skew P&L stats
                for _, sig in group.iterrows():
                    trades.append({
                        "symbol": symbol, "signal_date": date,
                        "signal_time_et": sig["scan_time_et"],
                        "entry_price": float(sig["scan_price"] or 0),
                        "exit_price": float(sig["scan_price"] or 0),
                        "exit_time_et": "NA",
                        "exit_reason": "NO_DATA",
                        "minutes_held": 0,
                        "gross_return_bps": 0.0,
                        "net_return_bps": 0.0,
                        "branch_label": "",
                        "position_size": 1.0,
                        "tp_bps_used": bt.tp_bps,
                        "sl_bps_used": bt.sl_bps,
                        "entry_vs_scan_bps": 0.0,
                        **_signal_export_fields(sig),
                    })
                continue
            for _, sig in group.iterrows():
                # Find the entry bar.
                #
                # v0.7.20: optional delayed-entry audit. With entry_delay_minutes=0
                # we preserve legacy behavior: entry timestamp is the scan bar and
                # entry price is research_rows.scan_price. With entry_delay_minutes>0
                # we intentionally simulate a stricter live-executable path: wait N
                # minutes after the scan and enter at the delayed raw bar's OPEN.
                #
                # For the 09:30 rules this lets us test a conservative sequence:
                #   signal known at/after 09:30 -> enter at 09:31 open -> optionally
                #   require min_exit_minutes=1 so TP/SL cannot fire until 09:32.
                try:
                    entry_delay_int = max(0, int(bt.entry_delay_minutes or 0))
                except (TypeError, ValueError):
                    entry_delay_int = 0

                entry_time_et = (
                    _hhmm_plus_minutes(sig["scan_time_et"], entry_delay_int)
                    if entry_delay_int > 0 else sig["scan_time_et"]
                )

                # v0.7.11: pass `date` so we don't accidentally match prior-
                # day after-hours bars that leak in via the UTC-date query.
                entry_bar_ts = _find_scan_bar_ts(
                    bars, entry_time_et, signal_date_et=date,
                )
                entry_price_source = "scan_price"
                entry_price_used = float(sig["scan_price"])

                if entry_bar_ts is not None and entry_delay_int > 0:
                    entry_bar = _find_bar_by_ts(bars, entry_bar_ts)
                    if entry_bar is None:
                        entry_bar_ts = None
                    else:
                        entry_price_used = float(entry_bar["open"])
                        entry_price_source = f"delayed_bar_open_plus_{entry_delay_int}m"

                if entry_bar_ts is None:
                    trades.append({
                        "symbol": symbol, "signal_date": date,
                        "signal_time_et": sig["scan_time_et"],
                        "scan_price_ref": float(sig["scan_price"]),
                        "entry_time_et": entry_time_et,
                        "entry_ts_utc": "NA",
                        "entry_delay_minutes": entry_delay_int,
                        "entry_price_source": entry_price_source,
                        "entry_price": float(sig["scan_price"]),
                        "exit_price": float(sig["scan_price"]),
                        "exit_time_et": "NA",
                        "exit_reason": "NO_DATA",
                        "minutes_held": 0,
                        "gross_return_bps": 0.0,
                        "net_return_bps": 0.0,
                        "branch_label": "",
                        "position_size": 1.0,
                        "tp_bps_used": bt.tp_bps,
                        "sl_bps_used": bt.sl_bps,
                        "entry_vs_scan_bps": 0.0,
                        **_signal_export_fields(sig),
                    })
                    continue

                # v0.7.0: resolve TP/SL via conditional branches if configured.
                # First-match wins. If no branch matches, use top-level tp/sl.
                tp_eff = bt.tp_bps
                sl_eff = bt.sl_bps
                size_mult = 1.0
                branch_label = ""
                if bt.conditional_exits:
                    sig_dict = sig.to_dict()
                    for branch in bt.conditional_exits:
                        if _branch_matches(sig_dict, branch):
                            tp_eff = branch.tp_bps
                            sl_eff = branch.sl_bps
                            size_mult = branch.position_size
                            branch_label = branch.label or (
                                f"{branch.feature}{branch.op}{branch.value}"
                            )
                            break

                result = _simulate_trade(
                    bars=bars,
                    entry_ts_utc=entry_bar_ts,
                    entry_price=entry_price_used,
                    tp_level=tp_eff,
                    sl_level=sl_eff,
                    timestop_et_hhmm=bt.timestop_et,
                    slippage_bps=bt.slippage_bps,
                    entry_slippage_split=bt.entry_slippage_split,
                    min_exit_minutes=bt.min_exit_minutes,
                )
                # Apply position_size multiplier to net_return_bps only —
                # gross is the true pre-sizing outcome, net reflects realized
                # P&L contribution at the branch's size weighting.
                if size_mult != 1.0:
                    result = dict(result)
                    result["net_return_bps"] = result["net_return_bps"] * size_mult
                trades.append({
                    "symbol": symbol, "signal_date": date,
                    "signal_time_et": sig["scan_time_et"],
                    "scan_price_ref": float(sig["scan_price"]),
                    "entry_time_et": entry_time_et,
                    "entry_ts_utc": entry_bar_ts,
                    "entry_delay_minutes": entry_delay_int,
                    "entry_price_source": entry_price_source,
                    "entry_price": entry_price_used,
                    "branch_label": branch_label,
                    "position_size": size_mult,
                    "tp_bps_used": tp_eff,
                    "sl_bps_used": sl_eff,
                    "entry_vs_scan_bps": _entry_vs_scan_bps(sig, entry_price_used),
                    **_signal_export_fields(sig),
                    **result,
                })
            if bt.delete_raw_bars_after:
                storage.delete_raw_bars_for_day(conn, symbol, date, preserve_spy=True)

        # Aggregate stats
        exits = [t for t in trades if t["exit_reason"] != "NO_DATA"]
        n_trades = len(exits)
        n_nodata = len(trades) - n_trades
        if n_trades > 0:
            net_bps_arr = np.array([t["net_return_bps"] for t in exits])
            win_rate = float((net_bps_arr > 0).mean())
            net_pnl_bps = float(net_bps_arr.sum())
        else:
            win_rate = 0.0
            net_pnl_bps = 0.0

        # Persist
        cond_exits_serialized = None
        if bt.conditional_exits:
            cond_exits_serialized = json.dumps([
                {"feature": b.feature, "op": b.op, "value": b.value,
                 "tp_bps": b.tp_bps, "sl_bps": b.sl_bps,
                 "position_size": b.position_size, "label": b.label}
                for b in bt.conditional_exits
            ])
        storage.record_backtest_run(conn, {
            "run_uuid": run_uuid,
            "rule_json": json.dumps(bt.rule.to_dict()),
            "tp_bps": bt.tp_bps, "sl_bps": bt.sl_bps,
            "timestop_et": bt.timestop_et,
            "slippage_bps": bt.slippage_bps,
            "spy_regime_filter": bt.spy_regime_filter,
            "symbol_exclude": ",".join(bt.symbol_exclude) if bt.symbol_exclude else None,
            "start_date": bt.start_date, "end_date": bt.end_date,
            "generated_at_utc": generated_at,
            "n_signals_total": n_signals_total,
            "n_signals_skipped": n_skipped_regime + n_skipped_rank_cap + n_nodata,
            "n_trades": n_trades,
            "net_pnl_bps": net_pnl_bps,
            "win_rate": win_rate,
            "notes": (
                f"filter_mode={bt.filter_mode}; min_exit_minutes={bt.min_exit_minutes}; "
                f"entry_delay_minutes={bt.entry_delay_minutes}; "
                f"max_signals_per_day={bt.max_signals_per_day}; "
                f"rank_feature={bt.rank_feature}; rank_direction={bt.rank_direction}"
            ),
            "conditional_exits_json": cond_exits_serialized,
        })
        storage.insert_backtest_trades(conn, run_uuid, trades)

    # v0.6.1: write trades CSV eagerly to evidence_packs so it surfaces in
    # the packs list and /packs/{filename} download flow. Previously this
    # was written lazily on first call to /backtest/{uuid}/trades.csv,
    # which meant it didn't appear in the packs list and the auth-less
    # href link in the UI returned 401.
    import csv as _csv
    csv_name = f"backtest_{run_uuid[:8]}_trades.csv"
    csv_path = Path(config.EVIDENCE_PACK_DIR) / csv_name
    try:
        Path(config.EVIDENCE_PACK_DIR).mkdir(parents=True, exist_ok=True)
        if trades:
            with open(csv_path, "w", newline="") as f:
                w = _csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(trades)
    except Exception as e:
        logger.warning(f"failed to write trades CSV {csv_path}: {e}")
        csv_name = None

    return {
        "run_uuid": run_uuid,
        "generated_at_utc": generated_at,
        "rule_id": bt.rule.id,
        "tp_bps": bt.tp_bps, "sl_bps": bt.sl_bps,
        "slippage_bps": bt.slippage_bps,
        "timestop_et": bt.timestop_et,
        "spy_regime_filter": bt.spy_regime_filter,
        "filter_mode": bt.filter_mode,
        "min_exit_minutes": bt.min_exit_minutes,
        "entry_delay_minutes": bt.entry_delay_minutes,
        "symbol_exclude": bt.symbol_exclude,
        "n_signals_total": n_signals_total,
        "n_signals_skipped_regime": n_skipped_regime,
        "n_signals_skipped_rank_cap": n_skipped_rank_cap,
        "n_signals_no_data": n_nodata,
        "max_signals_per_day": bt.max_signals_per_day,
        "rank_feature": bt.rank_feature,
        "rank_direction": bt.rank_direction,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "net_pnl_bps": net_pnl_bps,
        "avg_net_bps_per_trade": (net_pnl_bps / n_trades) if n_trades else 0.0,
        "exit_reason_mix": _count_exits(trades),
        "trades_csv_filename": csv_name,  # v0.6.1: downloadable via /packs
        "no_data_diagnosis": _summarize_no_data(trades),
        "per_branch_stats": _summarize_branches(trades),
    }


def _summarize_branches(trades: list[dict]) -> dict:
    """v0.7.0: break down outcomes by conditional-exit branch_label so the
    caller can see how each branch of a conditional-exit spec performed.
    Only includes non-NO_DATA trades. Returns {} if all trades used the
    top-level TP/SL (no branch matched)."""
    exits = [t for t in trades if t["exit_reason"] != "NO_DATA"]
    # Group by branch_label ("" = fell through to top-level tp/sl)
    from collections import defaultdict
    grouped = defaultdict(list)
    for t in exits:
        grouped[t.get("branch_label", "")].append(t)
    if len(grouped) <= 1 and "" in grouped:
        return {}  # no conditional branching happened
    out = {}
    for label, ts in grouped.items():
        key = label if label else "(fallthrough)"
        net = [t["net_return_bps"] for t in ts]
        wins = sum(1 for v in net if v > 0)
        out[key] = {
            "n": len(ts),
            "total_bps": float(sum(net)),
            "avg_bps": float(sum(net) / len(ts)) if ts else 0.0,
            "win_rate": float(wins / len(ts)) if ts else 0.0,
            "tp_count": sum(1 for t in ts if t["exit_reason"] == "TP"),
            "sl_count": sum(1 for t in ts if t["exit_reason"] == "SL"),
            "time_count": sum(1 for t in ts if t["exit_reason"] == "TIME"),
            "tp_bps_used": ts[0].get("tp_bps_used"),
            "sl_bps_used": ts[0].get("sl_bps_used"),
            "position_size": ts[0].get("position_size", 1.0),
        }
    return out


def _summarize_no_data(trades: list[dict]) -> dict:
    """Break down NO_DATA trades by date and symbol to help diagnose why
    the JIT backfill didn't get data. Returns counts for the top offenders
    in each dimension so the caller can spot patterns like 'all Jan 2024' or
    'all SMCI'."""
    from collections import Counter
    nd = [t for t in trades if t["exit_reason"] == "NO_DATA"]
    if not nd:
        return {"n_no_data": 0}
    by_date = Counter(t["signal_date"] for t in nd)
    by_symbol = Counter(t["symbol"] for t in nd)
    by_month = Counter(t["signal_date"][:7] for t in nd)
    return {
        "n_no_data": len(nd),
        "top_dates": dict(by_date.most_common(10)),
        "top_symbols": dict(by_symbol.most_common(10)),
        "by_month": dict(sorted(by_month.items())),
    }


def _count_exits(trades: list[dict]) -> dict:
    """Summarize trade outcomes by exit reason."""
    from collections import Counter
    c = Counter(t["exit_reason"] for t in trades)
    return dict(c)



def _hhmm_plus_minutes(hhmm: str, minutes: int) -> str:
    """Return HH:MM shifted forward by `minutes` within a trading day.

    v0.7.20: used for delayed-entry execution audits. This is intentionally
    simple: scan times are intraday ET HH:MM strings, and we only support
    non-negative minute delays for same-day testing.
    """
    hh, mm = map(int, hhmm.split(":"))
    total = hh * 60 + mm + max(0, int(minutes or 0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _find_bar_by_ts(bars: list[dict], timestamp_utc: str) -> dict | None:
    """Return the raw bar matching timestamp_utc, or None."""
    for b in bars:
        if b.get("timestamp_utc") == timestamp_utc:
            return b
    return None


def _find_scan_bar_ts(
    bars: list[dict],
    scan_time_et: str,
    signal_date_et: str | None = None,
) -> str | None:
    """Return the timestamp_utc of the first **regular-session** bar at/after
    scan_time_et on the requested ET trading date.

    v0.7.12: now also requires the matched bar to be in the regular ET
    trading session (09:30-15:59 ET). Returns None if no such bar exists.

    Why this guard exists: v0.7.11 fixed the storage layer so that
    get_raw_bars_for_day returns bars where ET date matches — including
    same-day after-hours bars at ET 19:00-23:59. On most (symbol, date)
    pairs this is fine because regular-session bars are present and sort
    earlier in UTC, so they're matched first. But on (symbol, date) pairs
    where regular-session bars are MISSING from raw_bars (halted symbols,
    partial backfills, cleanup-then-repopulate races), only the same-day
    after-hours bars remain. Without an in-session guard, this function
    would match the first AH bar (et.hour=19 satisfies any reasonable
    scan_time target), the simulator would anchor entry to that bar, fire
    the timestop on the next iteration (et.hour=19 > timestop_h=15), and
    record a phantom-TIME exit with bogus gross P&L from after-hours price
    differentials versus the research_rows-derived scan_price.

    The fix is conservative: an entry bar must be in the regular ET
    session, OR the signal is treated as NO_DATA. Pre-market and after-
    hours bars never become entry candidates regardless of how late the
    requested scan_time is.

    v0.7.11: requires the bar's ET date to equal `signal_date_et` (defense
    against the prior-day-AH leak that storage layer also fixed).

    `signal_date_et` defaults to None for backward compatibility with old
    callers/tests; in that mode we fall back to the legacy "first bar
    at/after target hour anywhere in the list" behaviour BUT still apply
    the regular-session guard. Production `run_backtest` always passes
    the date.

    v0.7.8: switched from the month-boundary _utc_hour_to_et approximation
    to zoneinfo via _bar_ts_to_et.
    """
    target_hh, target_mm = map(int, scan_time_et.split(":"))
    target_date = None
    if signal_date_et:
        target_date = datetime.fromisoformat(signal_date_et).date()
    for b in bars:
        bdt = datetime.fromisoformat(b["timestamp_utc"].replace("Z", "+00:00"))
        if bdt.tzinfo is None:
            bdt = bdt.replace(tzinfo=timezone.utc)
        et = _bar_ts_to_et(bdt)
        if target_date is not None and et.date() != target_date:
            continue
        # v0.7.12: regular-session guard. Reject any bar outside ET 09:30-
        # 15:59. Pre-market (< 09:30) and after-hours (>= 16:00) bars are
        # never valid entry candidates for a regular-session scan signal.
        if et.hour < 9 or et.hour >= 16:
            continue
        if et.hour == 9 and et.minute < 30:
            continue
        if et.hour > target_hh or (et.hour == target_hh and et.minute >= target_mm):
            return b["timestamp_utc"]
    return None


# ---------------------------------------------------------------------------
# Aggregate statistics — for the results dashboard panel
# ---------------------------------------------------------------------------
def compute_aggregates(trades: list[dict]) -> dict:
    """Compute equity curve, max drawdown, and by-exit-reason breakdown.

    Assumes trades are sorted by (signal_date, signal_time_et).
    """
    if not trades:
        return {"equity_curve_bps": [], "max_drawdown_bps": 0.0, "by_reason": {}}
    exits = [t for t in trades if t["exit_reason"] != "NO_DATA"]
    net_bps = [t["net_return_bps"] for t in exits]
    equity = np.cumsum(net_bps).tolist()
    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd
    # By reason
    by_reason = {}
    for reason in ("TP", "SL", "TIME", "NO_DATA"):
        subset = [t for t in trades if t["exit_reason"] == reason]
        if subset and reason != "NO_DATA":
            arr = np.array([t["net_return_bps"] for t in subset])
            by_reason[reason] = {
                "n": len(subset),
                "mean_bps": float(arr.mean()),
                "median_bps": float(np.median(arr)),
                "sum_bps": float(arr.sum()),
            }
        elif subset:
            by_reason[reason] = {"n": len(subset)}
    return {
        "equity_curve_bps": equity,
        "max_drawdown_bps": float(max_dd),
        "by_reason": by_reason,
    }
