"""
R2K-scanner feature computer.

Ports the feature definitions from the R2K Intraday Scanner
(server.py::compute_features and compute_spy_context) so the evidence pack
carries both the tech-research schema AND R2K-schema features side-by-side.

This module does NOT replace feature_computer.py — it augments it. The
research-schema features remain as before; R2K features are added with
different names (momentum, rel_volume_r2k, vwap_slope, orb_strength,
atr_reach, trend_str, range_expansion, spy_ret, ret_vs_spy, spy_momentum,
mom_vs_spy, spy_vol, gap_filled).

SPY context:
  SPY is not in our universe, so on first use we fetch SPY 1-min bars for
  the full compute range into the same SQLite. compute_range() handles this.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import config, feature_computer as fc


ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# R2K feature computer — operates on the same bar DataFrame shape as
# feature_computer.py. Takes pre-scan bars (session slice), scan_price,
# open_price, scan_hour_et, prior daily-volume list, and optional SPY context.
# ---------------------------------------------------------------------------
def compute_r2k_features(
    pre: pd.DataFrame,
    scan_price: float,
    open_price: float,
    scan_hour_et: int,
    prior_daily_volumes: list[int],
    prev_close: float | None,
    spy_context: dict | None = None,
) -> dict:
    """Return the R2K feature dict for one scan-bar."""
    n = len(pre)
    if n < 3:
        return _empty()

    bars_list = pre.to_dict("records")  # list of dicts with open/high/low/close/volume

    # ─── Bar-data features (direct port of R2K compute_features) ─────
    hours_left = 16 - scan_hour_et

    tail = bars_list[-3:]
    tail_open = tail[0]["open"]
    momentum = (tail[-1]["close"] - tail_open) / tail_open if tail_open > 0 else 0.0
    ret_from_open = (scan_price - open_price) / open_price if open_price > 0 else 0.0

    # rel_volume_r2k = avg_bar_volume / (5-day ADV / 390 1-min bars per day)
    avg_bv = np.mean([b["volume"] for b in bars_list])
    rel_volume_r2k = 1.0
    if prior_daily_volumes:
        adv = sum(prior_daily_volumes) / len(prior_daily_volumes)
        exp_per_bar = adv / 390  # 1-min bars per regular session
        if exp_per_bar > 0:
            rel_volume_r2k = float(avg_bv / exp_per_bar)

    # VWAP slope: compare VWAP over first 1/3 of bars vs first 2/3
    vwap_slope = 0.0
    if n >= 6:
        t = n // 3
        first_third = bars_list[:t]
        first_two_thirds = bars_list[:t * 2]
        n1 = sum(((b["high"] + b["low"] + b["close"]) / 3) * b["volume"] for b in first_third)
        d1 = sum(b["volume"] for b in first_third)
        n2 = sum(((b["high"] + b["low"] + b["close"]) / 3) * b["volume"] for b in first_two_thirds)
        d2 = sum(b["volume"] for b in first_two_thirds)
        v1 = n1 / d1 if d1 > 0 else scan_price
        v2 = n2 / d2 if d2 > 0 else scan_price
        vwap_slope = (v2 - v1) / v1 if v1 > 0 else 0.0

    # Opening Range Breakout: first min(6, n) bars
    orb_n = min(6, n)
    orb_slice = bars_list[:orb_n]
    orb_h = max(b["high"] for b in orb_slice)
    orb_l = min(b["low"] for b in orb_slice)
    orb_range = orb_h - orb_l
    orb_strength = float((scan_price - orb_h) / orb_range) if orb_range > 0 else 0.0

    # ATR reach — simplified because we don't have 5-day daily bars handy;
    # estimate ATR from today's intraday range so far
    if n >= 5:
        trs = []
        for i in range(1, n):
            prev_close_bar = bars_list[i - 1]["close"]
            tr = max(
                bars_list[i]["high"] - bars_list[i]["low"],
                abs(bars_list[i]["high"] - prev_close_bar),
                abs(bars_list[i]["low"] - prev_close_bar),
            )
            trs.append(tr)
        atr = float(np.mean(trs[-14:])) if trs else scan_price * 0.015
    else:
        atr = scan_price * 0.015
    # R2K's reach: target = scan_price * 0.01 (1% move); scaled by sqrt(hours_left / 6.5)
    TP_PCT = 0.01
    target = scan_price * TP_PCT
    atr_scaled = atr * math.sqrt(max(hours_left, 0) / 6.5) if hours_left > 0 else atr * 0.1
    atr_reach = float(target / atr_scaled) if atr_scaled > 0 else 2.0

    # Trend strength — ratio of last-half mean close vs first-half mean close
    trend_str = 0.0
    if n >= 10:
        half = n // 2
        late_mean = np.mean([b["close"] for b in bars_list[-half:]])
        early_mean = np.mean([b["close"] for b in bars_list[:half]])
        if early_mean > 0:
            trend_str = float(late_mean / early_mean - 1)

    # Range expansion: last bar range / avg of last-10 bar ranges
    last_bar = bars_list[-1]
    last_range = (last_bar["high"] - last_bar["low"]) / last_bar["close"] if last_bar["close"] > 0 else 0.0
    last_10 = bars_list[-10:] if n >= 10 else bars_list
    ranges = [(b["high"] - b["low"]) / b["close"] for b in last_10 if b["close"] > 0]
    avg_range = np.mean(ranges) if ranges else 1.0
    range_expansion = float(last_range / avg_range) if avg_range > 0 else 1.0

    # ─── SPY-relative ────────────────────────────────────────────────
    sc = spy_context or {"spy_ret": 0.0, "spy_momentum": 0.0, "spy_vol": 0.0}
    spy_ret = float(sc["spy_ret"])
    ret_vs_spy = float(ret_from_open - spy_ret)
    spy_momentum = float(sc["spy_momentum"])
    mom_vs_spy = float(momentum - spy_momentum)
    spy_vol = float(sc["spy_vol"])

    # ─── Gap filled ──────────────────────────────────────────────────
    gap_filled = 0
    if prev_close and prev_close > 0:
        gap = open_price - prev_close
        session_low = min(b["low"] for b in bars_list)
        session_high = max(b["high"] for b in bars_list)
        if gap > 0:  # gapped up
            gap_filled = 1 if session_low <= prev_close else 0
        elif gap < 0:  # gapped down
            gap_filled = 1 if session_high >= prev_close else 0

    return {
        "momentum": float(momentum),
        "rel_volume_r2k": rel_volume_r2k,
        "vwap_slope": float(vwap_slope),
        "orb_strength": orb_strength,
        "atr_reach": atr_reach,
        "trend_str": trend_str,
        "range_expansion": range_expansion,
        "spy_ret": spy_ret,
        "ret_vs_spy": ret_vs_spy,
        "spy_momentum": spy_momentum,
        "mom_vs_spy": mom_vs_spy,
        "spy_vol": spy_vol,
        "gap_filled": int(gap_filled),
    }


def _empty() -> dict:
    return {
        "momentum": None, "rel_volume_r2k": None, "vwap_slope": None,
        "orb_strength": None, "atr_reach": None, "trend_str": None,
        "range_expansion": None, "spy_ret": None, "ret_vs_spy": None,
        "spy_momentum": None, "mom_vs_spy": None, "spy_vol": None,
        "gap_filled": None,
    }


# ---------------------------------------------------------------------------
# SPY context computation — from SPY 1-min bars for the session, at scan time
# ---------------------------------------------------------------------------
def compute_spy_context(
    spy_bars_session: pd.DataFrame, scan_time_et: str
) -> dict:
    """Compute SPY-level features from SPY bars up to scan_time_et.
    Returns {"spy_ret": ..., "spy_momentum": ..., "spy_vol": ...}.
    """
    if spy_bars_session.empty:
        return {"spy_ret": 0.0, "spy_momentum": 0.0, "spy_vol": 0.0}
    scan_idx = fc._scan_time_bar_index(spy_bars_session, scan_time_et)
    if scan_idx is None:
        return {"spy_ret": 0.0, "spy_momentum": 0.0, "spy_vol": 0.0}
    pre = spy_bars_session.iloc[: scan_idx + 1]
    if len(pre) < 3:
        return {"spy_ret": 0.0, "spy_momentum": 0.0, "spy_vol": 0.0}

    spy_open = float(pre.iloc[0]["open"])
    spy_current = float(pre.iloc[-1]["close"])
    spy_ret = (spy_current - spy_open) / spy_open if spy_open > 0 else 0.0

    tail = pre.iloc[-3:]
    tail_open = float(tail.iloc[0]["open"])
    spy_momentum = (float(tail.iloc[-1]["close"]) - tail_open) / tail_open if tail_open > 0 else 0.0

    closes = pre["close"].values
    spy_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    spy_vol = float(np.std(spy_rets) * math.sqrt(78)) if len(spy_rets) > 1 else 0.0

    return {"spy_ret": float(spy_ret), "spy_momentum": float(spy_momentum), "spy_vol": spy_vol}
