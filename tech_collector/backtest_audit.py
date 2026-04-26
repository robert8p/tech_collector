"""Audit and repair tooling for completed backtest runs.

Purpose
=======
A completed backtest run is a set of stored trades. This module verifies
that every trade's stored exit outcome matches what a freshly-executed
reference simulator produces from the same bars. Any divergence indicates
a bug in the backtest engine at the time the run was executed.

The reference simulator (`_simulate_trade_reference`) is the canonical
implementation: it uses zoneinfo for ET, scans bars exhaustively for
TP/SL crossings, and asserts that every TIME-exit return is within the
configured TP/SL bounds. Divergences from stored results are flagged and
optionally written to a new run for comparison.

Usage (via API):
    POST /backtest/{run_uuid}/audit
         -> returns structured diff report + optional evidence pack

    POST /backtest/{run_uuid}/repair
         -> writes a new run_uuid with corrected trades

Both endpoints are idempotent: running audit multiple times produces the
same report; repair creates a new run each time (previous runs are never
mutated).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import backtest, collector, config, storage

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Reference simulator — canonical implementation used for audit
# ---------------------------------------------------------------------------
def _simulate_trade_reference(
    bars: list[dict],
    entry_ts_utc: str,
    entry_price: float,
    tp_level: float,
    sl_level: float,
    timestop_et_hhmm: str,
    slippage_bps: float,
    entry_slippage_split: float = 0.5,
) -> dict:
    """Canonical, invariant-checked trade simulator.

    Differs from backtest._simulate_trade only in that it is guaranteed
    never to change — this is the reference we audit against. It uses
    zoneinfo for ET conversion and explicitly validates TIME exits.

    Mechanics:
      1. Entry: apply entry-side slippage to entry_price → effective_entry.
         Compute tp_price, sl_price around effective_entry.
      2. Walk bars in time order from entry onwards.
      3. On each bar:
         (a) If bar's ET time >= timestop, exit TIME at bar['open'].
         (b) Else, check intra-bar TP / SL hits. If both, SL first.
      4. If we exhaust bars without any exit trigger, scan ALL bars once
         more for any TP/SL crossing we might have missed (belt-and-braces).
         If none found, return TIME at the last bar's close.
      5. Invariant: TIME-exit gross return must lie within
         [-sl_level, +tp_level] plus a 1 bp tolerance. Violation raises
         AssertionError.
    """
    entry_dt = _parse_utc(entry_ts_utc)
    # v0.7.7: timestop_et_hhmm may be None/empty to disable the timestop.
    if timestop_et_hhmm:
        timestop_h, timestop_m = map(int, timestop_et_hhmm.split(":"))
        timestop_enabled = True
    else:
        timestop_h, timestop_m = 99, 99
        timestop_enabled = False

    slip_entry_mult = 1.0 + (slippage_bps / 10_000.0) * entry_slippage_split
    slip_exit_mult = 1.0 - (slippage_bps / 10_000.0) * (1 - entry_slippage_split)
    effective_entry = entry_price * slip_entry_mult
    tp_price = effective_entry * (1 + tp_level / 10_000.0)
    sl_price = effective_entry * (1 - sl_level / 10_000.0)

    # Pre-parse post-entry bars with ET time attached.
    parsed: list[tuple[datetime, int, int, dict]] = []
    for bar in bars:
        bdt = _parse_utc(bar["timestamp_utc"])
        if bdt < entry_dt:
            continue
        et = bdt.astimezone(ET)
        parsed.append((bdt, et.hour, et.minute, bar))

    if not parsed:
        return _result_no_data(entry_price)

    # Main loop — first match (timestop OR TP/SL) wins.
    for bar_dt, et_hh, et_mm, bar in parsed:
        if timestop_enabled and ((et_hh > timestop_h) or (et_hh == timestop_h and et_mm >= timestop_m)):
            return _make_time_exit(
                bar, bar_dt, et_hh, et_mm, entry_dt, entry_price,
                effective_entry, slip_exit_mult, use_open=True,
                tp_level=tp_level, sl_level=sl_level,
            )
        hit_tp = bar["high"] >= tp_price
        hit_sl = bar["low"] <= sl_price
        if hit_tp and hit_sl:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                sl_price, effective_entry, slip_exit_mult, "SL",
            )
        if hit_tp:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                tp_price, effective_entry, slip_exit_mult, "TP",
            )
        if hit_sl:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                sl_price, effective_entry, slip_exit_mult, "SL",
            )

    # Fallback: exhaustive re-scan for any missed crossing.
    for bar_dt, et_hh, et_mm, bar in parsed:
        hit_tp = bar["high"] >= tp_price
        hit_sl = bar["low"] <= sl_price
        if hit_tp and hit_sl:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                sl_price, effective_entry, slip_exit_mult, "SL",
            )
        if hit_tp:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                tp_price, effective_entry, slip_exit_mult, "TP",
            )
        if hit_sl:
            return _make_level_exit(
                bar_dt, et_hh, et_mm, entry_dt, entry_price,
                sl_price, effective_entry, slip_exit_mult, "SL",
            )

    # Truly no TP/SL crossing; last-close TIME exit
    last_dt, et_hh, et_mm, last = parsed[-1]
    return _make_time_exit(
        last, last_dt, et_hh, et_mm, entry_dt, entry_price,
        effective_entry, slip_exit_mult, use_open=False,
        tp_level=tp_level, sl_level=sl_level,
    )


def _parse_utc(ts: str) -> datetime:
    s = ts.replace("Z", "+00:00") if isinstance(ts, str) and "Z" in ts else ts
    dt = datetime.fromisoformat(s) if isinstance(s, str) else s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _make_level_exit(
    bar_dt, et_hh, et_mm, entry_dt, entry_price,
    exit_price_raw, effective_entry, slip_exit_mult, reason,
) -> dict:
    exit_price = exit_price_raw * slip_exit_mult
    gross_bps = (exit_price_raw - entry_price) / entry_price * 10_000
    net_bps = (exit_price - effective_entry) / effective_entry * 10_000
    return {
        "exit_price": exit_price,
        "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
        "exit_reason": reason,
        "minutes_held": int((bar_dt - entry_dt).total_seconds() / 60),
        "gross_return_bps": gross_bps,
        "net_return_bps": net_bps,
    }


def _make_time_exit(
    bar, bar_dt, et_hh, et_mm, entry_dt, entry_price,
    effective_entry, slip_exit_mult, use_open: bool,
    tp_level: float, sl_level: float,
) -> dict:
    ref_price = bar["open"] if use_open else bar["close"]
    exit_price = ref_price * slip_exit_mult
    gross_bps = (ref_price - entry_price) / entry_price * 10_000
    net_bps = (exit_price - effective_entry) / effective_entry * 10_000
    # Invariant: TIME exits must not exceed TP/SL bounds (with small tolerance)
    tol = 1.0
    if gross_bps > tp_level + tol or gross_bps < -sl_level - tol:
        raise AssertionError(
            f"TIME exit invariant violation: gross_bps={gross_bps:.2f} outside "
            f"[-{sl_level}, +{tp_level}] at {bar_dt.isoformat()} "
            f"(entry={entry_price}, ref={ref_price}, use_open={use_open})"
        )
    return {
        "exit_price": exit_price,
        "exit_time_et": f"{et_hh:02d}:{et_mm:02d}",
        "exit_reason": "TIME",
        "minutes_held": int((bar_dt - entry_dt).total_seconds() / 60),
        "gross_return_bps": gross_bps,
        "net_return_bps": net_bps,
    }


def _result_no_data(entry_price: float) -> dict:
    return {
        "exit_price": entry_price,
        "exit_time_et": "NA",
        "exit_reason": "NO_DATA",
        "minutes_held": 0,
        "gross_return_bps": 0.0,
        "net_return_bps": 0.0,
    }


# ---------------------------------------------------------------------------
# Audit orchestrator
# ---------------------------------------------------------------------------
@dataclass
class TradeAuditResult:
    symbol: str
    signal_date: str
    signal_time_et: str
    entry_price: float
    tp_bps_used: float
    sl_bps_used: float
    # Stored outcome
    stored_exit_reason: str
    stored_minutes_held: int
    stored_net_bps: float
    stored_gross_bps: float
    # Reference outcome
    ref_exit_reason: str
    ref_minutes_held: int
    ref_net_bps: float
    ref_gross_bps: float
    # Divergence flags
    reason_matches: bool
    net_bps_delta: float  # ref - stored
    suspect_time_exit: bool  # stored is TIME but gross outside [-sl, +tp]
    bars_seen_post_entry: int
    audit_error: str | None  # None unless an assertion fired


def _signal_time_to_utc_iso(signal_date: str, signal_time_et: str) -> str:
    """Convert (YYYY-MM-DD, HH:MM ET) → ISO UTC timestamp."""
    h, m = map(int, signal_time_et.split(":"))
    y, mo, d = map(int, signal_date.split("-"))
    et_dt = datetime(y, mo, d, h, m, tzinfo=ET)
    utc_dt = et_dt.astimezone(timezone.utc)
    return utc_dt.isoformat().replace("+00:00", "Z")


def audit_run(
    db_path: str, run_uuid: str,
    jit_backfill: bool = True,
) -> dict:
    """Audit every trade in the run. Returns structured report.

    For each trade:
      1. Load the raw_bars for (symbol, signal_date) from the DB.
         If empty and jit_backfill=True, backfill just-in-time.
      2. Re-simulate using _simulate_trade_reference.
      3. Compare to the stored outcome.
    """
    with storage.connect(db_path) as conn:
        run = storage.get_backtest_run(conn, run_uuid)
        if run is None:
            raise ValueError(f"run {run_uuid} not found")
        slippage_bps = float(run["slippage_bps"])
        timestop_et = run["timestop_et"]
        trades = storage.get_backtest_trades(conn, run_uuid)

    logger.info(f"audit_run: {run_uuid}, {len(trades)} trades, slippage={slippage_bps}")

    results: list[TradeAuditResult] = []
    # Track bars fetches to minimize JIT requests
    fetched_days: set[tuple[str, str]] = set()
    # Bars cache per (symbol, date) — avoid re-querying for trades that
    # share the same signal_date at different scan_times. In a C-scaled
    # run there are typically 1-3 trades per (symbol,date), so caching
    # cuts the raw_bars query count roughly in half.
    bars_cache: dict[tuple[str, str], list[dict]] = {}

    # Open ONE connection for the entire audit. sqlite3 is perfectly happy
    # with long-lived connections; one-per-trade was the reason the HTTP
    # request exhausted workers under 502 on Render.
    with storage.connect(db_path) as conn:
        for i, t in enumerate(trades):
            if t["exit_reason"] == "NO_DATA":
                continue
            symbol = t["symbol"]
            signal_date = t["signal_date"]
            signal_time_et = t["signal_time_et"]
            entry_price = float(t["entry_price"])
            tp_level = float(t["tp_bps_used"])
            sl_level = float(t["sl_bps_used"])
            entry_ts_utc = _signal_time_to_utc_iso(signal_date, signal_time_et)

            # Load bars — cache within the run
            key = (symbol, signal_date)
            if key in bars_cache:
                bars = bars_cache[key]
            else:
                bars = storage.get_raw_bars_for_day(conn, symbol, signal_date)
                if not bars and jit_backfill and key not in fetched_days:
                    logger.info(f"JIT backfill: {symbol} {signal_date}")
                    try:
                        # Drop the connection for JIT (collector opens its own)
                        # and re-query after.
                        collector.collect_range(
                            symbols=[symbol], start_date=signal_date,
                            end_date=signal_date, db_path=db_path,
                        )
                        fetched_days.add(key)
                        bars = storage.get_raw_bars_for_day(
                            conn, symbol, signal_date,
                        )
                    except Exception as e:
                        logger.warning(
                            f"JIT backfill failed for {symbol} {signal_date}: {e}"
                        )
                bars_cache[key] = bars

            # Count post-entry bars (for diagnostic)
            entry_dt = _parse_utc(entry_ts_utc)
            bars_post = [b for b in bars if _parse_utc(b["timestamp_utc"]) >= entry_dt]

            # Run reference simulator
            audit_error = None
            try:
                ref = _simulate_trade_reference(
                    bars=bars, entry_ts_utc=entry_ts_utc, entry_price=entry_price,
                    tp_level=tp_level, sl_level=sl_level,
                    timestop_et_hhmm=timestop_et, slippage_bps=slippage_bps,
                )
            except AssertionError as e:
                audit_error = str(e)
                ref = {
                    "exit_reason": "ASSERT_FAIL", "minutes_held": 0,
                    "net_return_bps": 0.0, "gross_return_bps": 0.0,
                }

            stored_net = float(t["net_return_bps"])
            stored_gross = float(t["gross_return_bps"])
            suspect = (
                t["exit_reason"] == "TIME"
                and (stored_gross > tp_level + 1.0 or stored_gross < -sl_level - 1.0)
            )

            results.append(TradeAuditResult(
                symbol=symbol, signal_date=signal_date,
                signal_time_et=signal_time_et, entry_price=entry_price,
                tp_bps_used=tp_level, sl_bps_used=sl_level,
                stored_exit_reason=t["exit_reason"],
                stored_minutes_held=int(t["minutes_held"]),
                stored_net_bps=stored_net,
                stored_gross_bps=stored_gross,
                ref_exit_reason=ref["exit_reason"],
                ref_minutes_held=int(ref["minutes_held"]),
                ref_net_bps=float(ref["net_return_bps"]),
                ref_gross_bps=float(ref["gross_return_bps"]),
                reason_matches=(t["exit_reason"] == ref["exit_reason"]),
                net_bps_delta=float(ref["net_return_bps"]) - stored_net,
                suspect_time_exit=suspect,
                bars_seen_post_entry=len(bars_post),
                audit_error=audit_error,
            ))

            if (i + 1) % 100 == 0:
                logger.info(f"audited {i+1}/{len(trades)}")

    # Aggregate report
    n = len(results)
    n_mismatch = sum(1 for r in results if not r.reason_matches)
    n_suspect = sum(1 for r in results if r.suspect_time_exit)
    n_error = sum(1 for r in results if r.audit_error)
    suspect_pnl = sum(r.stored_net_bps for r in results if r.suspect_time_exit)
    mismatch_pnl = sum(r.net_bps_delta for r in results if not r.reason_matches)
    stored_total = sum(r.stored_net_bps for r in results)
    ref_total = sum(r.ref_net_bps for r in results if r.audit_error is None)

    # Group by exit-reason transitions
    transitions: dict[str, int] = {}
    for r in results:
        k = f"{r.stored_exit_reason}→{r.ref_exit_reason}"
        transitions[k] = transitions.get(k, 0) + 1

    return {
        "run_uuid": run_uuid,
        "n_trades": n,
        "n_reason_mismatch": n_mismatch,
        "n_suspect_time_exits": n_suspect,
        "n_invariant_violations": n_error,
        "stored_total_net_bps": round(stored_total, 2),
        "ref_total_net_bps": round(ref_total, 2),
        "pnl_delta_from_mismatches_bps": round(mismatch_pnl, 2),
        "suspect_stored_pnl_bps": round(suspect_pnl, 2),
        "exit_reason_transitions": transitions,
        "jit_backfills_triggered": len(fetched_days),
        "trade_results": [asdict(r) for r in results],
    }


def repair_run(
    db_path: str, source_run_uuid: str,
    jit_backfill: bool = True,
    notes_prefix: str = "repaired from",
) -> str:
    """Re-simulate all trades in source run with reference engine; write to new run.

    Returns the new run_uuid. The original run is untouched.
    """
    with storage.connect(db_path) as conn:
        src = storage.get_backtest_run(conn, source_run_uuid)
        if src is None:
            raise ValueError(f"run {source_run_uuid} not found")
        src_dict = src  # get_backtest_run already returns a dict
        trades = storage.get_backtest_trades(conn, source_run_uuid)

    new_uuid = str(uuid.uuid4())
    slippage_bps = float(src_dict["slippage_bps"])
    timestop_et = src_dict["timestop_et"]

    new_trades: list[dict] = []
    bars_cache: dict[tuple[str, str], list[dict]] = {}
    fetched_days: set[tuple[str, str]] = set()
    with storage.connect(db_path) as conn:
        for t in trades:
            if t["exit_reason"] == "NO_DATA":
                new_trades.append(dict(t))
                continue
            symbol = t["symbol"]
            signal_date = t["signal_date"]
            signal_time_et = t["signal_time_et"]
            entry_price = float(t["entry_price"])
            tp_level = float(t["tp_bps_used"])
            sl_level = float(t["sl_bps_used"])
            entry_ts_utc = _signal_time_to_utc_iso(signal_date, signal_time_et)

            key = (symbol, signal_date)
            if key in bars_cache:
                bars = bars_cache[key]
            else:
                bars = storage.get_raw_bars_for_day(conn, symbol, signal_date)
                if not bars and jit_backfill and key not in fetched_days:
                    try:
                        collector.collect_range(
                            symbols=[symbol], start_date=signal_date,
                            end_date=signal_date, db_path=db_path,
                        )
                        fetched_days.add(key)
                        bars = storage.get_raw_bars_for_day(
                            conn, symbol, signal_date,
                        )
                    except Exception as e:
                        logger.warning(f"JIT backfill failed: {e}")
                bars_cache[key] = bars

            try:
                ref = _simulate_trade_reference(
                    bars=bars, entry_ts_utc=entry_ts_utc, entry_price=entry_price,
                    tp_level=tp_level, sl_level=sl_level,
                    timestop_et_hhmm=timestop_et, slippage_bps=slippage_bps,
                )
                new_net = ref["net_return_bps"] * float(t.get("position_size") or 1.0)
            except AssertionError as e:
                logger.error(f"assertion in {symbol} {signal_date}: {e}")
                ref = {
                    "exit_price": entry_price, "exit_time_et": "NA",
                    "exit_reason": "NO_DATA", "minutes_held": 0,
                    "gross_return_bps": 0.0, "net_return_bps": 0.0,
                }
                new_net = 0.0

            new_trades.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "signal_time_et": signal_time_et,
                "entry_price": entry_price,
                "branch_label": t.get("branch_label"),
                "position_size": t.get("position_size"),
                "tp_bps_used": tp_level,
                "sl_bps_used": sl_level,
                "exit_price": ref["exit_price"],
                "exit_time_et": ref["exit_time_et"],
                "exit_reason": ref["exit_reason"],
                "minutes_held": ref["minutes_held"],
                "gross_return_bps": ref["gross_return_bps"],
                "net_return_bps": new_net,
            })

    # Save new run. Recompute aggregate P&L / win rate from the corrected trades.
    exits = [t for t in new_trades if t["exit_reason"] != "NO_DATA"]
    total_net_bps = sum(t["net_return_bps"] for t in exits)
    win_rate = (
        sum(1 for t in exits if t["net_return_bps"] > 0) / len(exits)
        if exits else None
    )
    new_run = {
        "run_uuid": new_uuid,
        "rule_json": src_dict["rule_json"],
        "tp_bps": src_dict["tp_bps"],
        "sl_bps": src_dict["sl_bps"],
        "timestop_et": timestop_et,
        "slippage_bps": slippage_bps,
        "spy_regime_filter": src_dict.get("spy_regime_filter"),
        "symbol_exclude": src_dict.get("symbol_exclude"),
        "start_date": src_dict.get("start_date"),
        "end_date": src_dict.get("end_date"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_signals_total": src_dict.get("n_signals_total"),
        "n_signals_skipped": src_dict.get("n_signals_skipped"),
        "n_trades": len(exits),
        "net_pnl_bps": total_net_bps,
        "win_rate": win_rate,
        "notes": f"{notes_prefix} {source_run_uuid} (repaired by backtest_audit v0.7.1)",
        "conditional_exits_json": src_dict.get("conditional_exits_json"),
    }
    with storage.connect(db_path) as conn:
        storage.record_backtest_run(conn, new_run)
        storage.insert_backtest_trades(conn, new_uuid, new_trades)

    return new_uuid


def export_audit_pack(report: dict, out_dir: Path) -> Path:
    """Write audit report as JSON under the evidence packs directory.

    Two files: summary.json (lightweight) and full.json (all trade results).
    Zipped together for single-file download.
    """
    import zipfile
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_uuid = report["run_uuid"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pack_path = out_dir / f"backtest_audit_{run_uuid[:8]}_{ts}.zip"

    summary = {k: v for k, v in report.items() if k != "trade_results"}
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps(summary, indent=2))
        z.writestr("full.json", json.dumps(report, indent=2))
    return pack_path
