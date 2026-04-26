"""
Structural features — breakout, consolidation, multi-day position,
volume thrust, sector context.

These are designed for precision-focused rule search: find setups where
a stock tends to rise by ≥ 25 bps over the scan-to-cutoff window, even
if the rule fires infrequently.

Computed at the scan bar from:
  - `pre` — session bars up to and including scan bar (same as fc/r2k)
  - `bars` — the full bar window including prior N days
  - `the_date` — target session
  - `cross_section` — optional dict of {symbol: {feature: value}} for
                     sector-wide features (computed two-pass in caller)

All distance/return features expressed in basis points (1 bp = 0.01%).
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


ET = ZoneInfo("America/New_York")


def compute_structural_features(
    pre: pd.DataFrame,
    bars: pd.DataFrame,
    scan_price: float,
    the_date: date,
    prior_session_close: float | None,
    scan_time_et: str,
) -> dict:
    """Compute the per-symbol structural features at scan time.

    `pre` is the current session's bars up to and including the scan bar.
    `bars` is the full DataFrame including prior days.
    """
    n = len(pre)
    if n < 3:
        return _empty()

    result = {}

    # ─── Consolidation / tight-range features ─────────────────────────
    # range_tightness_30m: std of last 30 bars' closes / scan_price
    last30 = pre.iloc[-30:] if n >= 30 else pre
    result["range_tightness_30m"] = (
        float(last30["close"].std() / scan_price)
        if scan_price > 0 and len(last30) >= 2 else None
    )

    # bars_in_range_20bps: count of last 30 bars whose close is within 20 bps of scan_price
    tol = 0.002  # 20 basis points
    in_range = (
        ((last30["close"] - scan_price).abs() / scan_price) <= tol
    ).sum()
    result["bars_in_range_20bps"] = int(in_range)

    # is_nr7: 1 if current bar range (high-low) is smallest over last 7 bars
    last7 = pre.iloc[-7:] if n >= 7 else pre
    current_range = float(pre.iloc[-1]["high"] - pre.iloc[-1]["low"])
    if len(last7) >= 7:
        all_ranges = (last7["high"] - last7["low"]).values
        result["is_nr7"] = int(current_range == all_ranges.min())
    else:
        result["is_nr7"] = 0

    # ─── Intraday position / breakout features ────────────────────────
    day_high = float(pre["high"].max())
    day_low = float(pre["low"].min())

    result["dist_to_day_high_bps"] = (
        float((day_high - scan_price) / scan_price * 10000)
        if scan_price > 0 else None
    )

    # broke_day_high_this_bar: 1 if current bar's high exceeds the high of
    # any previous bar in the session
    if n >= 2:
        prior_high = float(pre.iloc[:-1]["high"].max())
        result["broke_day_high_this_bar"] = int(
            float(pre.iloc[-1]["high"]) > prior_high
        )
    else:
        result["broke_day_high_this_bar"] = 0

    # broke_opening_range_high: 1 if scan_price > high of opening 30 min (09:30-09:59 bars)
    # Identify opening range: first 30 bars of session (9:30 through 9:59 inclusive)
    or_bars = pre.iloc[:30] if n >= 30 else pre
    or_high = float(or_bars["high"].max()) if len(or_bars) > 0 else day_high
    result["broke_opening_range_high"] = int(scan_price > or_high)

    # bars_since_day_high: count of bars since the bar containing the day high
    highs = pre["high"].values
    if len(highs) > 0:
        high_idx = int(highs.argmax())
        result["bars_since_day_high"] = int(len(pre) - 1 - high_idx)
    else:
        result["bars_since_day_high"] = None

    # ─── Prior-day / multi-day structure ──────────────────────────────
    if prior_session_close and prior_session_close > 0:
        result["dist_to_prev_close_bps"] = float(
            (scan_price - prior_session_close) / prior_session_close * 10000
        )
    else:
        result["dist_to_prev_close_bps"] = None

    # 5-day and 20-day high: walk back through `bars` to find
    # the highest high in prior N trading days (regular session only).
    hi_5d = _prior_n_day_high(bars, the_date, n_days=5)
    hi_20d = _prior_n_day_high(bars, the_date, n_days=20)
    result["dist_to_5d_high_bps"] = (
        float((hi_5d - scan_price) / scan_price * 10000)
        if hi_5d is not None and scan_price > 0 else None
    )
    result["dist_to_20d_high_bps"] = (
        float((hi_20d - scan_price) / scan_price * 10000)
        if hi_20d is not None and scan_price > 0 else None
    )

    # days_since_20d_high: how many trading days ago was the 20d high touched
    result["days_since_20d_high"] = _days_since_n_day_high(bars, the_date, n_days=20)

    # ─── Volume features ──────────────────────────────────────────────
    # volume_acceleration: sum(last 5 bars' vol) vs 5-bar-equivalent of preceding 20
    if n >= 25:
        last5_vol = float(pre.iloc[-5:]["volume"].sum())
        prior20 = pre.iloc[-25:-5]
        prior20_mean_per_bar = float(prior20["volume"].mean())
        denom = prior20_mean_per_bar * 5
        result["volume_acceleration"] = (
            float(last5_vol / denom) if denom > 0 else None
        )
    else:
        result["volume_acceleration"] = None

    # cumulative_volume_vs_typical: cum vol to scan / avg cum vol at same minute over prior 20 sessions
    result["cumulative_volume_vs_typical"] = _cum_vol_vs_typical(
        bars, the_date, scan_time_et, current_cum_vol=float(pre["volume"].sum()),
        lookback_days=20,
    )

    # ─── Sector-wide features filled in the cross-section pass ────────
    # Placeholders; overwritten by caller
    result["sector_breadth_up"] = None
    result["new_highs_in_sector"] = None

    return result


def _empty() -> dict:
    return {k: None for k in STRUCTURAL_COLUMNS}


# ---------------------------------------------------------------------------
# Helpers for multi-day lookups
# ---------------------------------------------------------------------------
def _prior_n_day_high(
    bars: pd.DataFrame, the_date: date, n_days: int
) -> float | None:
    """Return the highest regular-session `high` across the previous n_days
    trading days. Walks back by calendar day, skipping days with no bars."""
    highs = []
    cur = the_date - timedelta(days=1)
    tries = 0
    max_tries = n_days * 3  # allow ~3x for weekends / holidays
    while len(highs) < n_days and tries < max_tries:
        start = datetime.combine(cur, dtime(9, 30), tzinfo=ET)
        end = datetime.combine(cur, dtime(16, 0), tzinfo=ET)
        day_bars = bars.loc[(bars.index >= start) & (bars.index < end)]
        if not day_bars.empty:
            highs.append(float(day_bars["high"].max()))
        cur -= timedelta(days=1)
        tries += 1
    if not highs:
        return None
    return max(highs)


def _days_since_n_day_high(
    bars: pd.DataFrame, the_date: date, n_days: int
) -> int | None:
    """Trading-days count since the n_days high was touched.
    Returns 0 if touched today (up to scan time bars in `bars`)."""
    # First, find the n_day high and the date on which it was hit.
    highest_val = -np.inf
    highest_date = None
    cur = the_date - timedelta(days=1)
    tries = 0
    max_tries = n_days * 3
    trading_days_seen = 0
    while trading_days_seen < n_days and tries < max_tries:
        start = datetime.combine(cur, dtime(9, 30), tzinfo=ET)
        end = datetime.combine(cur, dtime(16, 0), tzinfo=ET)
        day_bars = bars.loc[(bars.index >= start) & (bars.index < end)]
        if not day_bars.empty:
            h = float(day_bars["high"].max())
            if h > highest_val:
                highest_val = h
                highest_date = cur
            trading_days_seen += 1
        cur -= timedelta(days=1)
        tries += 1
    if highest_date is None:
        return None
    # Count trading days between highest_date and the_date (exclusive of highest_date day).
    days_between = 0
    walk = highest_date + timedelta(days=1)
    while walk < the_date:
        if walk.weekday() < 5:  # rough — treats holidays as trading, off by <=5
            days_between += 1
        walk += timedelta(days=1)
    return days_between


def _cum_vol_vs_typical(
    bars: pd.DataFrame,
    the_date: date,
    scan_time_et: str,
    current_cum_vol: float,
    lookback_days: int,
) -> float | None:
    """Ratio of today's cumulative volume at scan to the average cumulative
    volume at the same scan minute across the prior `lookback_days` sessions."""
    hh, mm = map(int, scan_time_et.split(":"))
    historic_cum_vols = []
    cur = the_date - timedelta(days=1)
    tries = 0
    max_tries = lookback_days * 3
    while len(historic_cum_vols) < lookback_days and tries < max_tries:
        start = datetime.combine(cur, dtime(9, 30), tzinfo=ET)
        end_bar = datetime.combine(cur, dtime(hh, mm), tzinfo=ET)
        day_bars = bars.loc[(bars.index >= start) & (bars.index <= end_bar)]
        if not day_bars.empty:
            historic_cum_vols.append(float(day_bars["volume"].sum()))
        cur -= timedelta(days=1)
        tries += 1
    if not historic_cum_vols:
        return None
    typical = float(np.mean(historic_cum_vols))
    if typical <= 0:
        return None
    return float(current_cum_vol / typical)


# ---------------------------------------------------------------------------
# Column list (exported for schema/config)
# ---------------------------------------------------------------------------
STRUCTURAL_COLUMNS = [
    "range_tightness_30m",
    "bars_in_range_20bps",
    "is_nr7",
    "dist_to_day_high_bps",
    "broke_day_high_this_bar",
    "broke_opening_range_high",
    "bars_since_day_high",
    "dist_to_prev_close_bps",
    "dist_to_5d_high_bps",
    "dist_to_20d_high_bps",
    "days_since_20d_high",
    "volume_acceleration",
    "cumulative_volume_vs_typical",
    "sector_breadth_up",
    "new_highs_in_sector",
]
