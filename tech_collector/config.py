"""
Static configuration for the Tech Collector app.

Universe, scan schedule, and feature list are fixed to match the original
research dataset (tech_run_manifest.json from 2026-04-19). Do NOT modify
these values without re-running the research pipeline — they are contract,
not preference.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Universe — selected at request time from one of the 11 GICS sectors
# defined in universes.py. The module-level constants below preserve
# backward compatibility for callers that still reference config.UNIVERSE
# and config.SECTOR (e.g. CLI one-offs, older manifests).
# ---------------------------------------------------------------------------
import os as _os
from .universes import SECTOR_UNIVERSES, SECTOR_LISTS_SNAPSHOT_DATE  # noqa: E402

# Default sector when none specified — configurable via DEFAULT_SECTOR env var
DEFAULT_SECTOR: str = _os.environ.get("DEFAULT_SECTOR", "Information Technology")

if DEFAULT_SECTOR not in SECTOR_UNIVERSES:
    # Fall back gracefully if env var is misconfigured
    DEFAULT_SECTOR = "Information Technology"

SECTOR: str = DEFAULT_SECTOR
UNIVERSE: tuple[str, ...] = SECTOR_UNIVERSES[DEFAULT_SECTOR]

# Convenience: list of all supported sectors (for API enumeration)
SUPPORTED_SECTORS: tuple[str, ...] = tuple(SECTOR_UNIVERSES.keys())

# ---------------------------------------------------------------------------
# Scan schedule — six bars per trading day (ET)
# minutes_since_open is relative to 09:30 ET
# ---------------------------------------------------------------------------
SCAN_TIMES_ET: tuple[str, ...] = (
    "09:30", "10:30", "11:30", "12:30", "13:30", "14:30",
)

CUTOFF_TIME_ET: str = "15:30"  # close minus 30 minutes (regular session ends 16:00 ET)
MARKET_OPEN_ET: str = "09:30"
MARKET_CLOSE_ET: str = "16:00"

# ---------------------------------------------------------------------------
# Feature list — matches original research CSV columns.
# Extensions (path points at 5-min intervals, leak-free sector feature,
# data-quality markers) are added by the feature computer, not here.
# ---------------------------------------------------------------------------
RESEARCH_COLUMNS: tuple[str, ...] = (
    "symbol", "date", "sector", "scan_time_et", "minutes_since_open",
    "scan_price", "open_to_scan_return", "gap_pct",
    "intraday_range_position", "distance_to_vwap",
    "distance_to_day_high", "distance_to_day_low",
    "rsi_14", "macd_hist",
    "ema_9_distance", "ema_20_distance", "ema_50_distance",
    "relative_volume", "realized_vol_so_far",
    "sector_relative_strength",  # WARNING: original was leak-prone; see feature_computer
    "day_of_week",
    "cutoff_time_et", "cutoff_price", "return_to_cutoff", "target",
    "min_return_before_cutoff", "max_return_before_cutoff",
)

# Extension columns (added by feature computer, not in original research)
EXTENSION_COLUMNS: tuple[str, ...] = (
    "rs_leakfree",                   # leak-free sector rel strength (computed properly)
    "return_at_scan_plus_30m",       # path point: t+30min from scan
    "return_at_scan_plus_60m",       # path point: t+60min from scan
    "return_at_scan_plus_90m",       # path point: t+90min from scan
    "return_at_scan_plus_120m",      # path point: t+120min from scan
    "bars_missing_pre_scan",         # data quality: count of missing 1m bars
    "bars_missing_post_scan",        # data quality: count of missing 1m bars to cutoff
    "feed_source",                   # "sip" — logged for audit trail
    "pulled_at_utc",                 # when this row was fetched
    # R2K-style features (ported from R2K intraday scanner)
    "momentum",                      # short-horizon momentum
    "rel_volume_r2k",                # ADV-normalized relative volume
    "vwap_slope",                    # VWAP trajectory
    "orb_strength",                  # opening-range breakout strength
    "atr_reach",                     # % move distance in ATR units to 1% target
    "trend_str",                     # late-half vs early-half close ratio
    "range_expansion",               # last-bar range vs avg recent
    "spy_ret",                       # SPY return from open
    "ret_vs_spy",                    # stock ret minus SPY ret
    "spy_momentum",                  # SPY short-horizon momentum
    "mom_vs_spy",                    # stock momentum minus SPY momentum
    "spy_vol",                       # SPY realized vol
    "gap_filled",                    # 1 if price revisited prev-close level (R2K binary)
    # Structural features (for precision-focused rule search)
    "range_tightness_30m",           # std of last 30 bar closes / scan_price
    "bars_in_range_20bps",           # # of last 30 bars within 20bps of scan_price
    "is_nr7",                        # 1 if current bar has smallest range in last 7
    "dist_to_day_high_bps",          # distance to session high, basis points
    "broke_day_high_this_bar",       # 1 if current bar's high exceeds prior intraday high
    "broke_opening_range_high",      # 1 if scan_price > opening 30min high
    "bars_since_day_high",           # bars since day high was printed
    "dist_to_prev_close_bps",        # distance to yesterday's close, basis points
    "dist_to_5d_high_bps",           # distance to 5-day high, basis points
    "dist_to_20d_high_bps",          # distance to 20-day high, basis points
    "days_since_20d_high",           # trading days since 20-day high was touched
    "volume_acceleration",           # last-5-bars vol vs 5-bar-equiv of prior 20
    "cumulative_volume_vs_typical",  # today's cum-vol at scan / 20-day avg at same minute
    "sector_breadth_up",             # fraction of sector with positive open_to_scan
    "new_highs_in_sector",           # count of sector peers with recent intraday high
    # v0.7.7: regime-gating feature — KEEP-condition for the recommended filter.
    # regime_ok = 1 iff (spy_momentum > 0) OR (dist_to_prev_close_bps >= 0).
    # Used as a rule predicate (predicate: regime_ok == 1) to skip signals when
    # BOTH the market is weak AND the stock is already down vs prev close.
    "regime_ok",
    # Target variants
    "target_25bps",                  # 1 if return_to_cutoff > 0.0025 (precision target)
    "target_peak_25bps",             # 1 if max_return_before_cutoff > 0.0025 (touch variant)
    "target_50bps",                  # 1 if return_to_cutoff > 0.0050
    "target_peak_50bps",             # 1 if max_return_before_cutoff > 0.0050
    "target_75bps",                  # 1 if return_to_cutoff > 0.0075
    "target_peak_75bps",             # 1 if max_return_before_cutoff > 0.0075
)

ALL_COLUMNS: tuple[str, ...] = RESEARCH_COLUMNS + EXTENSION_COLUMNS

# ---------------------------------------------------------------------------
# Alpaca configuration (credentials from environment)
# ---------------------------------------------------------------------------
ALPACA_API_KEY_ENV: str = "ALPACA_API_KEY"
ALPACA_API_SECRET_ENV: str = "ALPACA_API_SECRET"
ALPACA_FEED: str = "sip"  # confirmed active on user subscription
ALPACA_BARS_TIMEFRAME: str = "1Min"

# Rate limiting: Alpaca Algo Trader Plus allows 10,000 req/min on market data.
# We batch symbols per request and stay well below this.
MAX_SYMBOLS_PER_REQUEST: int = 50
REQUESTS_PER_SECOND: int = 10  # conservative; raise if needed

# ---------------------------------------------------------------------------
# Storage — paths resolve from env vars so the app can run locally (defaults)
# or on Render (with DATA_DIR pointed at the persistent disk mount).
# ---------------------------------------------------------------------------
import os as _os

_DATA_DIR: str = _os.environ.get("DATA_DIR", ".")

DB_PATH: str = _os.environ.get(
    "DB_PATH", _os.path.join(_DATA_DIR, "tech_collector.sqlite")
)
EVIDENCE_PACK_DIR: str = _os.environ.get(
    "EVIDENCE_PACK_DIR", _os.path.join(_DATA_DIR, "evidence_packs")
)
APP_NAME: str = "tech-collector"
APP_VERSION: str = "0.7.30"

# ---------------------------------------------------------------------------
# API auth — set API_KEY env var in Render dashboard. Requests without a
# matching X-API-Key header will be rejected.
# ---------------------------------------------------------------------------
API_KEY_ENV: str = "API_KEY"
