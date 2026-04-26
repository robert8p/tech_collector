"""
Feature computer: raw 1-minute bars -> research-schema rows.

Implements the 14 scan-time features from the original research CSV plus
extensions (leak-free sector rel strength, path points at 5-min intervals,
data-quality markers).

IMPORTANT — feature definitions are reconstructed from the research CSV's
behaviour, not from a published spec. Before trusting any output for
pattern-finding, validate a sample of rows against the original
`tech_research_dataset.csv` to confirm definitions match. The validation
harness in validate.py does this.

All times internal to this module are in ET. Alpaca bars arrive in UTC and
are converted here.
"""
from __future__ import annotations

import gc
import logging
import math
import os
import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config, storage
from .universes import get_universe

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------
def load_bars_for_date(
    conn: sqlite3.Connection, symbol: str, the_date: date
) -> pd.DataFrame:
    """Load all 1-minute bars for (symbol, date) plus the prior session's
    close bar (for gap calculation). Returns a DataFrame indexed by ET
    timestamp with columns open/high/low/close/volume/vwap.

    v0.7.8: switched to timestamp_utc range query (PK-ordered) instead of
    substr(timestamp_utc,1,10) BETWEEN. The old query forced SQLite to use
    a TEMP B-TREE for ORDER BY because the expression index didn't preserve
    natural ordering; this one uses the PRIMARY KEY (symbol, timestamp_utc)
    directly. Measured speedup: 5-10x per call. ISO-8601 strings are
    lexically sortable, so string comparison is correct.
    """
    # Pull the date plus ~30 calendar days before. Need:
    #  - 5 trading days of prior data for relative-volume baseline
    #  - 20 trading days for dist_to_20d_high / days_since_20d_high features
    # Buffer accounts for weekends/holidays within the window.
    start = (the_date - timedelta(days=30)).isoformat() + "T00:00:00Z"
    end = (the_date + timedelta(days=1)).isoformat() + "T00:00:00Z"
    rows = conn.execute(
        """SELECT timestamp_utc, open, high, low, close, volume, vwap
           FROM raw_bars
           WHERE symbol = ?
             AND timestamp_utc >= ?
             AND timestamp_utc < ?
           ORDER BY timestamp_utc""",
        (symbol, start, end),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["ts_et"] = df["ts_utc"].dt.tz_convert(ET)
    df = df.set_index("ts_et").sort_index()
    return df[["open", "high", "low", "close", "volume", "vwap"]]


def load_bars_for_range(
    conn: sqlite3.Connection, symbol: str,
    start_date: date, end_date: date,
) -> pd.DataFrame:
    """v0.7.8: load bars for (symbol) over an arbitrary date range in ONE
    query. Used by the compute-loop cache: instead of querying 30 days of
    prior context per-date-per-symbol (~37,500 queries with massive overlap),
    we fetch the full range once per symbol and slice locally.

    For IT sector × 500 trading days, this converts 37,500 queries returning
    overlapping data into ~75 queries returning disjoint data. Measured
    speedup: 75× for the outer loop.
    """
    start = start_date.isoformat() + "T00:00:00Z"
    end = (end_date + timedelta(days=1)).isoformat() + "T00:00:00Z"
    rows = conn.execute(
        """SELECT timestamp_utc, open, high, low, close, volume, vwap
           FROM raw_bars
           WHERE symbol = ?
             AND timestamp_utc >= ?
             AND timestamp_utc < ?
           ORDER BY timestamp_utc""",
        (symbol, start, end),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    df["ts_et"] = df["ts_utc"].dt.tz_convert(ET)
    df = df.set_index("ts_et").sort_index()
    return df[["open", "high", "low", "close", "volume", "vwap"]]


def _session_bars(bars: pd.DataFrame, the_date: date) -> pd.DataFrame:
    """Slice bars to regular-hours for the given ET date (09:30–16:00)."""
    start = datetime.combine(the_date, dtime(9, 30), tzinfo=ET)
    end = datetime.combine(the_date, dtime(16, 0), tzinfo=ET)
    return bars.loc[(bars.index >= start) & (bars.index < end)]


def _prior_daily_volumes(bars: pd.DataFrame, the_date: date, n_days: int = 5) -> list[int]:
    """Return list of total regular-session volumes for up to `n_days`
    trading days before `the_date`, using the bars DataFrame."""
    # Walk back day-by-day looking for days with bars
    vols = []
    look_date = the_date - timedelta(days=1)
    tries = 0
    while len(vols) < n_days and tries < 14:  # give up after 2 weeks back
        start = datetime.combine(look_date, dtime(9, 30), tzinfo=ET)
        end = datetime.combine(look_date, dtime(16, 0), tzinfo=ET)
        day_bars = bars.loc[(bars.index >= start) & (bars.index < end)]
        if not day_bars.empty:
            vols.append(int(day_bars["volume"].sum()))
        look_date -= timedelta(days=1)
        tries += 1
    return vols


def _prior_session_close(bars: pd.DataFrame, the_date: date) -> float | None:
    """Last 1-min close from the trading day prior to `the_date`."""
    prior_end = datetime.combine(the_date, dtime(9, 30), tzinfo=ET)
    prior_start = prior_end - timedelta(days=5)
    prior_bars = bars.loc[(bars.index >= prior_start) & (bars.index < prior_end)]
    # Trim to prior regular session only
    if prior_bars.empty:
        return None
    # Take last bar within any prior day's 09:30-16:00 window
    prior_bars = prior_bars[
        (prior_bars.index.hour > 9) |
        ((prior_bars.index.hour == 9) & (prior_bars.index.minute >= 30))
    ]
    prior_bars = prior_bars[prior_bars.index.hour < 16]
    if prior_bars.empty:
        return None
    return float(prior_bars.iloc[-1]["close"])


# ---------------------------------------------------------------------------
# Scan-time feature computation
# ---------------------------------------------------------------------------
def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(closes: pd.Series, window: int = 14) -> pd.Series:
    """RSI using simple mean of gains/losses over last `window` bars.
    Matches the R2K scanner's implementation.
    Returns a Series of the same length; value is 50 (neutral) until we
    have `window+1` bars. This matches the research dataset's 09:30
    behaviour (RSI = 50 at session open)."""
    n = len(closes)
    out = pd.Series(50.0, index=closes.index, dtype=float)
    if n < window + 1:
        return out
    # Compute RSI at each bar using trailing window
    for i in range(window, n):
        window_closes = closes.iloc[i - window:i + 1].values  # window+1 bars for window diffs
        diffs = np.diff(window_closes)
        gains = diffs[diffs > 0].sum()
        losses = -diffs[diffs < 0].sum()
        ag = gains / window
        al = losses / window
        if al > 0:
            rs = ag / al
            out.iloc[i] = 100 - (100 / (1 + rs))
        else:
            out.iloc[i] = 100 if gains > 0 else 50
    return out


def _macd_hist(closes: pd.Series) -> pd.Series:
    """Standard MACD histogram (12/26/9)."""
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd_line = ema12 - ema26
    signal = _ema(macd_line, 9)
    return macd_line - signal


def _vwap_cumulative(bars: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP from open to each bar."""
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3
    cum_tpv = (tp * bars["volume"]).cumsum()
    cum_vol = bars["volume"].cumsum().replace(0, np.nan)
    return cum_tpv / cum_vol


def _scan_time_bar_index(session: pd.DataFrame, scan_time_et: str) -> int | None:
    """Return integer index into `session` of the bar whose open is at scan_time_et.
    Returns None if the bar is missing."""
    hh, mm = map(int, scan_time_et.split(":"))
    target = datetime.combine(
        session.index[0].date() if len(session) else date.today(),
        dtime(hh, mm), tzinfo=ET,
    )
    matches = np.where(session.index == target)[0]
    return int(matches[0]) if len(matches) else None


def compute_row_for_scan(
    symbol: str,
    the_date: date,
    scan_time_et: str,
    bars: pd.DataFrame,
    day_universe_returns: dict | None = None,
    sector: str | None = None,
) -> dict | None:
    """Compute one research row for (symbol, date, scan_time_et).

    `bars` is the full 1-min bar DataFrame (prior session + current day, ET).
    `day_universe_returns` is an optional dict of other symbols'
    open_to_scan_return on the same (date, scan_time_et), used for
    rs_leakfree. The caller fills this in during a two-pass compute.
    `sector` is the GICS sector label to stamp on the row; falls back to
    config.DEFAULT_SECTOR when not passed (preserves v0.2.x behaviour for
    callers that haven't been updated).

    Returns None if the required bars are missing.
    """
    resolved_sector = sector or config.DEFAULT_SECTOR
    session = _session_bars(bars, the_date)
    if session.empty:
        return None

    scan_idx = _scan_time_bar_index(session, scan_time_et)
    if scan_idx is None:
        # Bar missing — record the row as None-filled for audit.
        return {
            "symbol": symbol,
            "date": the_date.isoformat(),
            "scan_time_et": scan_time_et,
            "sector": resolved_sector,
            "minutes_since_open": _minutes_since_open(scan_time_et),
            "bars_missing_pre_scan": _missing_pre_scan(session, scan_time_et),
            "feed_source": config.ALPACA_FEED,
            "pulled_at_utc": datetime.now(UTC).isoformat(),
            # Target-related fields computed via path below
        }

    # Bars up to and including scan bar (intraday history available at scan)
    pre = session.iloc[: scan_idx + 1]
    scan_bar = session.iloc[scan_idx]
    open_bar = session.iloc[0]

    scan_price = float(scan_bar["open"])
    open_price = float(open_bar["open"])
    day_high = float(pre["high"].max())
    day_low = float(pre["low"].min())

    prior_close = _prior_session_close(bars, the_date)
    gap_pct = (open_price - prior_close) / prior_close if prior_close else 0.0

    open_to_scan = (scan_price - open_price) / open_price if open_price else 0.0

    # intraday_range_position = (scan - low) / (high - low)
    rng = day_high - day_low
    intraday_range_position = (scan_price - day_low) / rng if rng > 0 else 0.5

    # VWAP at scan time (cumulative from session open through scan bar)
    vwap_series = _vwap_cumulative(pre)
    vwap_at_scan = float(vwap_series.iloc[-1]) if not vwap_series.empty else scan_price
    distance_to_vwap = (scan_price - vwap_at_scan) / vwap_at_scan if vwap_at_scan else 0.0

    distance_to_day_high = (scan_price - day_high) / day_high if day_high else 0.0
    distance_to_day_low = (scan_price - day_low) / day_low if day_low else 0.0

    # RSI / MACD on closes up to and including scan bar
    closes = pre["close"]
    rsi_val = float(_rsi(closes).iloc[-1])
    macd_val = float(_macd_hist(closes).iloc[-1])

    # EMAs
    ema9 = float(_ema(closes, 9).iloc[-1])
    ema20 = float(_ema(closes, 20).iloc[-1])
    ema50 = float(_ema(closes, 50).iloc[-1])
    ema_9_distance = (scan_price - ema9) / ema9 if ema9 else 0.0
    ema_20_distance = (scan_price - ema20) / ema20 if ema20 else 0.0
    ema_50_distance = (scan_price - ema50) / ema50 if ema50 else 0.0

    # Relative volume (R2K definition):
    #   avg_bar_volume / (5-day ADV / 390 one-minute bars per session)
    # For a 1-minute bar dataset. R2K uses 78 bars because they use 5-min
    # bars; we use 1-min so divisor is 390.
    avg_bar_volume = float(pre["volume"].mean()) if len(pre) > 0 else 0.0
    prior_vols = _prior_daily_volumes(bars, the_date, n_days=5)
    if prior_vols:
        adv = sum(prior_vols) / len(prior_vols)
        expected_per_bar = adv / 390
        relative_volume = avg_bar_volume / expected_per_bar if expected_per_bar > 0 else 1.0
    else:
        # Fallback: not enough history
        relative_volume = 1.0

    # Realized vol so far: stdev of 1-min log returns up to scan
    log_rets = np.log(closes / closes.shift(1)).dropna()
    realized_vol = float(log_rets.std()) if len(log_rets) > 1 else 0.0

    # Leak-free sector relative strength from cross-section
    if day_universe_returns:
        others = [v for s, v in day_universe_returns.items()
                  if s != symbol and v is not None]
        sector_median = float(np.median(others)) if others else 0.0
        rs_leakfree = open_to_scan - sector_median
    else:
        rs_leakfree = None

    # The original research's sector_relative_strength is leak-prone; we
    # reproduce it here using post-cutoff data ONLY so that packs match the
    # original schema, but flag it. Computation happens after cutoff below.

    # Compute post-scan path and target
    post_fields = _compute_post_scan(session, scan_idx, scan_price)

    row = {
        "symbol": symbol,
        "date": the_date.isoformat(),
        "sector": resolved_sector,
        "scan_time_et": scan_time_et,
        "minutes_since_open": _minutes_since_open(scan_time_et),
        "scan_price": scan_price,
        "open_to_scan_return": open_to_scan,
        "gap_pct": gap_pct,
        "intraday_range_position": intraday_range_position,
        "distance_to_vwap": distance_to_vwap,
        "distance_to_day_high": distance_to_day_high,
        "distance_to_day_low": distance_to_day_low,
        "rsi_14": rsi_val,
        "macd_hist": macd_val,
        "ema_9_distance": ema_9_distance,
        "ema_20_distance": ema_20_distance,
        "ema_50_distance": ema_50_distance,
        "relative_volume": relative_volume,
        "realized_vol_so_far": realized_vol,
        "rs_leakfree": rs_leakfree,
        "day_of_week": the_date.strftime("%A"),
        "cutoff_time_et": config.CUTOFF_TIME_ET,
        "bars_missing_pre_scan": _missing_pre_scan(session, scan_time_et),
        "feed_source": config.ALPACA_FEED,
        "pulled_at_utc": datetime.now(UTC).isoformat(),
        **post_fields,
    }

    # sector_relative_strength placeholder; filled in second pass with
    # return-to-cutoff cross-section. Kept as None if upstream doesn't fill.
    row["sector_relative_strength"] = None
    return row


def _minutes_since_open(scan_time_et: str) -> int:
    hh, mm = map(int, scan_time_et.split(":"))
    return (hh - 9) * 60 + (mm - 30)


def _missing_pre_scan(session: pd.DataFrame, scan_time_et: str) -> int:
    """Count expected-vs-present 1-min bars from 09:30 to scan_time_et."""
    expected = _minutes_since_open(scan_time_et)  # 0 for 09:30, 60 for 10:30, etc.
    if expected <= 0:
        return 0
    scan_idx = _scan_time_bar_index(session, scan_time_et)
    present = scan_idx + 1 if scan_idx is not None else 0
    return max(0, expected + 1 - present)


def _compute_post_scan(
    session: pd.DataFrame, scan_idx: int, scan_price: float
) -> dict:
    """Compute cutoff_price, return_to_cutoff, target, path min/max, and
    5-min path points between scan and cutoff.
    """
    cutoff_hh, cutoff_mm = map(int, config.CUTOFF_TIME_ET.split(":"))
    cutoff_target = datetime.combine(
        session.index[0].date(), dtime(cutoff_hh, cutoff_mm), tzinfo=ET,
    )
    matches = np.where(session.index == cutoff_target)[0]
    if len(matches) == 0:
        return {
            "cutoff_price": None, "return_to_cutoff": None, "target": None,
            "target_25bps": None, "target_peak_25bps": None,
            "target_50bps": None, "target_peak_50bps": None,
            "target_75bps": None, "target_peak_75bps": None,
            "min_return_before_cutoff": None, "max_return_before_cutoff": None,
            "return_at_scan_plus_30m": None, "return_at_scan_plus_60m": None,
            "return_at_scan_plus_90m": None, "return_at_scan_plus_120m": None,
            "bars_missing_post_scan": None,
        }
    cutoff_idx = int(matches[0])
    cutoff_price = float(session.iloc[cutoff_idx]["open"])
    return_to_cutoff = (cutoff_price - scan_price) / scan_price if scan_price else 0.0
    target = int(cutoff_price > scan_price)

    # Path min / max between scan and cutoff (inclusive of scan, exclusive of cutoff bar open)
    between = session.iloc[scan_idx:cutoff_idx]
    highs = between["high"]
    lows = between["low"]
    max_before = (float(highs.max()) - scan_price) / scan_price if scan_price else 0.0
    min_before = (float(lows.min()) - scan_price) / scan_price if scan_price else 0.0

    # 5-min path points (actually 30/60/90/120 from scan bar)
    def return_at(offset_min: int) -> float | None:
        target_idx = scan_idx + offset_min
        if target_idx >= len(session):
            return None
        p = float(session.iloc[target_idx]["open"])
        return (p - scan_price) / scan_price if scan_price else None

    expected_post = cutoff_idx - scan_idx
    actual_post = len(between)
    return {
        "cutoff_price": cutoff_price,
        "return_to_cutoff": return_to_cutoff,
        "target": target,
        "target_25bps": int(return_to_cutoff > 0.0025) if scan_price else None,
        "target_peak_25bps": int(max_before > 0.0025) if scan_price else None,
        "target_50bps": int(return_to_cutoff > 0.0050) if scan_price else None,
        "target_peak_50bps": int(max_before > 0.0050) if scan_price else None,
        "target_75bps": int(return_to_cutoff > 0.0075) if scan_price else None,
        "target_peak_75bps": int(max_before > 0.0075) if scan_price else None,
        "min_return_before_cutoff": min_before,
        "max_return_before_cutoff": max_before,
        "return_at_scan_plus_30m": return_at(30),
        "return_at_scan_plus_60m": return_at(60),
        "return_at_scan_plus_90m": return_at(90),
        "return_at_scan_plus_120m": return_at(120),
        "bars_missing_post_scan": max(0, expected_post - actual_post),
    }


# ---------------------------------------------------------------------------
# Driver: compute all rows for a date range, two-pass for cross-section features
# ---------------------------------------------------------------------------
def _load_spy_session_bars(
    conn: sqlite3.Connection, the_date: date
) -> pd.DataFrame:
    """Load SPY regular-session 1-min bars for the date from raw_bars.
    Returns an empty DataFrame if SPY bars aren't present (caller handles)."""
    start_utc = (datetime.combine(the_date, dtime(9, 30), tzinfo=ET)
                 .astimezone(UTC).isoformat())
    end_utc = (datetime.combine(the_date, dtime(16, 0), tzinfo=ET)
               .astimezone(UTC).isoformat())
    rows = conn.execute(
        """SELECT timestamp_utc, open, high, low, close, volume
           FROM raw_bars
           WHERE symbol = 'SPY'
             AND timestamp_utc >= ? AND timestamp_utc < ?
           ORDER BY timestamp_utc""",
        (start_utc, end_utc),
    ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts_et"] = pd.to_datetime(df["timestamp_utc"], utc=True).dt.tz_convert(ET)
    df = df.set_index("ts_et").sort_index()
    return df[["open", "high", "low", "close", "volume"]]




def _compute_cache_chunk_days() -> int:
    """Return the calendar-day width for compute_range's raw-bar cache.

    v0.7.10: full-range sector recomputes can involve tens of millions of
    minute bars. Loading the entire date range into pandas at once is fast on
    small tests but risks Render OOM on a two-year recompute. Chunking keeps
    memory bounded while preserving the v0.7.8 single-query-per-symbol speedup
    within each chunk. Override with COMPUTE_CACHE_CHUNK_DAYS when needed.
    """
    raw = os.environ.get("COMPUTE_CACHE_CHUNK_DAYS", "60")
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "invalid COMPUTE_CACHE_CHUNK_DAYS=%r; using 60", raw
        )
        return 60
    return max(14, min(days, 180))

def compute_range(
    start_date: str,
    end_date: str,
    db_path: str = config.DB_PATH,
    sector: str | None = None,
) -> dict:
    """Compute research rows for all (symbol, date, scan) combinations in range.

    `sector` selects which GICS sector's universe to iterate over and which
    sector label to stamp on each row. When None, falls back to
    config.DEFAULT_SECTOR.

    Two-pass per (date, scan_time_et):
      Pass 1: compute per-symbol rows, collect open_to_scan_return cross-section.
      Pass 2: fill rs_leakfree and sector_relative_strength.
    """
    # Import inside function to avoid circular dependency at import time
    from . import r2k_features, structural_features as sf

    resolved_sector = sector or config.DEFAULT_SECTOR
    universe = get_universe(resolved_sector)

    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    storage.init_schema(db_path)

    total_written = 0
    with storage.connect(db_path) as conn:
        run_id = storage.log_run_start(
            conn, mode="compute",
            start_date=start_date, end_date=end_date,
            symbols_n=len(universe),
            started_at_utc=datetime.now(UTC).isoformat(),
        )

        # v0.7.10: bounded per-symbol bars cache.
        #
        # v0.7.8 made compute_range much faster by loading each symbol once for
        # the full requested range plus 30-day lookback, then slicing in memory.
        # That works for small tests but is risky for production full-sector,
        # multi-year recomputes on Render: tens of millions of pandas rows can
        # push the worker into OOM before any progress is visible.
        #
        # Keep the speedup, but bound memory by processing calendar chunks. Each
        # chunk loads each symbol once for chunk_start-30d through chunk_end,
        # computes only dates in that chunk, then explicitly releases the cache.
        chunk_days = _compute_cache_chunk_days()

        # Count total trading dates up front so progress logs show ETA.
        total_trading_dates = sum(
            1 for d_off in range((end - start).days + 1)
            if (start + timedelta(days=d_off)).weekday() < 5
        )
        dates_processed = 0
        t_loop = datetime.now(UTC)

        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=chunk_days - 1))
            cache_start = chunk_start - timedelta(days=30)

            logger.info(
                f"compute_range: {resolved_sector}, chunk {chunk_start} → "
                f"{chunk_end} (lookback from {cache_start}), "
                f"{len(universe)} symbols. Loading bounded bars cache..."
            )
            bars_cache: dict[str, pd.DataFrame] = {}
            t_cache = datetime.now(UTC)
            for i, sym in enumerate(universe):
                df = load_bars_for_range(conn, sym, cache_start, chunk_end)
                if not df.empty:
                    bars_cache[sym] = df
                if (i + 1) % 25 == 0:
                    logger.info(
                        f"compute_range: chunk bars cache "
                        f"{i+1}/{len(universe)} symbols loaded"
                    )
            dt_cache = (datetime.now(UTC) - t_cache).total_seconds()
            total_bars = sum(len(df) for df in bars_cache.values())
            logger.info(
                f"compute_range: bounded cache ready — "
                f"{len(bars_cache)} symbols, {total_bars:,} bars, "
                f"{dt_cache:.1f}s"
            )

            cur_date = chunk_start
            while cur_date <= chunk_end:
                # Skip weekends (holidays are treated as no-data weekdays).
                if cur_date.weekday() >= 5:
                    cur_date += timedelta(days=1)
                    continue

                # Slice each symbol's bounded bars cache to the 30-day window
                # ending at cur_date+1.
                cache_lower = (
                    datetime.combine(cur_date - timedelta(days=30),
                                     dtime(0, 0), tzinfo=ET)
                )
                cache_upper = (
                    datetime.combine(cur_date + timedelta(days=1),
                                     dtime(0, 0), tzinfo=ET)
                )
                symbol_bars = {}
                for sym, full_df in bars_cache.items():
                    window = full_df.loc[
                        (full_df.index >= cache_lower)
                        & (full_df.index < cache_upper)
                    ]
                    if not window.empty:
                        symbol_bars[sym] = window

                if not symbol_bars:
                    dates_processed += 1
                    cur_date += timedelta(days=1)
                    continue

                # Load SPY session bars for this date (for R2K SPY-relative features)
                spy_session = _load_spy_session_bars(conn, cur_date)

                for scan_time in config.SCAN_TIMES_ET:
                    # Compute SPY context for this scan bar (same across all symbols)
                    spy_ctx = r2k_features.compute_spy_context(spy_session, scan_time)

                    # Pass 1: compute rows without cross-section features
                    rows_by_sym = {}
                    symbol_pre_bars = {}  # keep pre bars around for pass-2 sector features
                    for sym, bars in symbol_bars.items():
                        r = compute_row_for_scan(
                            sym, cur_date, scan_time, bars,
                            sector=resolved_sector,
                        )
                        if r is None:
                            continue
                        # Attach R2K + structural features — only for rows with a valid scan bar
                        if r.get("scan_price") is not None:
                            session = _session_bars(bars, cur_date)
                            scan_idx = _scan_time_bar_index(session, scan_time)
                            if scan_idx is not None:
                                pre = session.iloc[: scan_idx + 1]
                                symbol_pre_bars[sym] = pre
                                scan_hour = int(scan_time.split(":")[0])
                                prior_vols = _prior_daily_volumes(bars, cur_date, n_days=5)
                                prev_close = _prior_session_close(bars, cur_date)
                                r.update(r2k_features.compute_r2k_features(
                                    pre=pre,
                                    scan_price=r["scan_price"],
                                    open_price=float(session.iloc[0]["open"]),
                                    scan_hour_et=scan_hour,
                                    prior_daily_volumes=prior_vols,
                                    prev_close=prev_close,
                                    spy_context=spy_ctx,
                                ))
                                # Structural features
                                r.update(sf.compute_structural_features(
                                    pre=pre,
                                    bars=bars,
                                    scan_price=r["scan_price"],
                                    the_date=cur_date,
                                    prior_session_close=prev_close,
                                    scan_time_et=scan_time,
                                ))
                            else:
                                r.update(r2k_features._empty())
                                r.update(sf._empty())
                        else:
                            r.update(r2k_features._empty())
                            r.update(sf._empty())
                        rows_by_sym[sym] = r

                    # Cross-section at this scan bar
                    otrs = {s: r.get("open_to_scan_return")
                            for s, r in rows_by_sym.items()}
                    valid_otr = {s: v for s, v in otrs.items() if v is not None}
                    if valid_otr:
                        otr_median = float(np.median(list(valid_otr.values())))
                    else:
                        otr_median = 0.0

                    # Cross-section for sector_relative_strength
                    rtcs = {s: r.get("return_to_cutoff")
                            for s, r in rows_by_sym.items()}
                    valid_rtc = {s: v for s, v in rtcs.items() if v is not None}
                    if valid_rtc:
                        rtc_median = float(np.median(list(valid_rtc.values())))
                    else:
                        rtc_median = 0.0

                    # Cross-section for sector-breadth and new-highs-in-sector
                    # (structural sector features)
                    otr_pos_count = sum(1 for v in valid_otr.values() if v > 0)
                    sector_breadth = (
                        otr_pos_count / len(valid_otr) if valid_otr else None
                    )
                    # new_highs_in_sector: count of symbols with bars_since_day_high <= 5
                    new_highs = sum(
                        1 for s, r in rows_by_sym.items()
                        if r.get("bars_since_day_high") is not None
                        and r.get("bars_since_day_high") <= 5
                    )

                    # Pass 2: fill cross-section features
                    final_rows = []
                    for sym, r in rows_by_sym.items():
                        otr = r.get("open_to_scan_return")
                        if otr is not None:
                            r["rs_leakfree"] = otr - otr_median
                        rtc = r.get("return_to_cutoff")
                        if rtc is not None:
                            # NOTE: This is the "leak-prone" research version for
                            # schema compatibility — it uses post-cutoff info.
                            r["sector_relative_strength"] = rtc - rtc_median
                        # Sector-wide structural features (same across all rows
                        # in this scan bar)
                        r["sector_breadth_up"] = sector_breadth
                        r["new_highs_in_sector"] = new_highs
                        # v0.7.7: regime_ok — KEEP-condition derived from scan-time
                        # features. 1 iff either the market is rising
                        # (spy_momentum > 0) OR the stock is not extended below prev
                        # close (dist_to_prev_close_bps >= 0). Handles partial inputs:
                        # if only one of the two is populated, evaluate whichever
                        # side we have (a "truthy" signal on the available side
                        # gives 1; otherwise 0). Only None when BOTH are None
                        # (typically the 09:30 opening-bar scan, with no prior data).
                        spy_mom = r.get("spy_momentum")
                        dist_pc = r.get("dist_to_prev_close_bps")
                        if spy_mom is not None and dist_pc is not None:
                            r["regime_ok"] = int((spy_mom > 0) or (dist_pc >= 0))
                        elif spy_mom is not None:
                            r["regime_ok"] = int(spy_mom > 0)
                        elif dist_pc is not None:
                            r["regime_ok"] = int(dist_pc >= 0)
                        else:
                            r["regime_ok"] = None
                        final_rows.append(r)

                    written = storage.insert_research_rows(conn, final_rows)
                    total_written += written

                dates_processed += 1
                if dates_processed % 10 == 0 or dates_processed == total_trading_dates:
                    elapsed = (datetime.now(UTC) - t_loop).total_seconds()
                    per_date = elapsed / max(1, dates_processed)
                    remaining_dates = max(0, total_trading_dates - dates_processed)
                    eta_s = remaining_dates * per_date
                    logger.info(
                        f"compute_range: {dates_processed}/{total_trading_dates} "
                        f"dates processed, {total_written:,} rows written, "
                        f"elapsed {elapsed:.0f}s, ETA {eta_s:.0f}s "
                        f"({per_date:.2f}s/date)"
                    )

                cur_date += timedelta(days=1)

            # Explicitly release the pandas cache before the next chunk. This
            # matters on long Render jobs where the process must stay alive.
            bars_cache.clear()
            gc.collect()
            chunk_start = chunk_end + timedelta(days=1)

        storage.log_run_finish(
            conn, run_id,
            finished_at_utc=datetime.now(UTC).isoformat(),
            rows_written=total_written,
            errors_n=0,
            notes=f"compute {start_date}..{end_date} (sector={resolved_sector})",
        )

    return {"rows_written": total_written, "sector": resolved_sector}
