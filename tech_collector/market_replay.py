"""Multi-rule historical market replay engine (v0.7.32).

This module answers a different question from standalone backtests:
    "If the live scanner had evaluated all active rules each historical day,
    which trades would have been selected after shared caps/deduplication, and
    what would the combined scanner have earned?"

It intentionally reuses the canonical rule_tester + backtest execution logic
so rule predicates, delayed entry, TP/SL, timestop, slippage, and NO_DATA
handling stay aligned with the rest of the app.
"""
from __future__ import annotations

import csv
import json
import math
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from . import backtest, config, rule_tester, storage


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayRuleSpec:
    rule_id: str
    display_name: str
    status: str
    priority: int
    sector: str
    rule: rule_tester.Rule
    scan_time_et: str
    rank_feature: str
    rank_direction: str
    max_signals_per_day: int
    tp_bps: float
    sl_bps: float
    default_slippage_bps: float
    entry_delay_minutes: int = 1
    min_exit_minutes: int = 1
    timestop_et: str = "15:50"
    notes: str = ""

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d["rule"] = self.rule.to_dict()
        return d


def _rule(rule_id: str, sector: str, predicates: list[dict], notes: str) -> rule_tester.Rule:
    return rule_tester.Rule.from_dict({
        "id": rule_id,
        "sector": sector,
        "target": "target",
        "predicates": predicates,
        "notes": notes,
    })


def registry(sector: str = "Information Technology") -> dict[str, ReplayRuleSpec]:
    """Return the canonical v0.7.32 multi-rule replay registry."""
    return {
        "rule029_top3": ReplayRuleSpec(
            rule_id="rule029_top3",
            display_name="Rule029 top3 ATR-low pullback/reclaim",
            status="live_shadow",
            priority=10,
            sector=sector,
            rule=_rule(
                "tech_rule_029_live_shadow_primary_top3_atrreach_low",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 60},
                    {"feature": "spy_vol", "op": ">=", "value": 0.005},
                    {"feature": "spy_momentum", "op": ">=", "value": 0},
                    {"feature": "distance_to_vwap", "op": "<", "value": 0},
                    {"feature": "distance_to_vwap", "op": ">=", "value": -0.0042402661},
                    {"feature": "vwap_slope", "op": ">", "value": 0},
                ],
                "10:30 Technology pullback/reclaim candidate; rank by lowest ATR reach; top3 primary live-shadow.",
            ),
            scan_time_et="10:30",
            rank_feature="atr_reach",
            rank_direction="asc",
            max_signals_per_day=3,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Primary complementary Rule009 challenger; live-shadow only.",
        ),
        "rule009_refined_top10": ReplayRuleSpec(
            rule_id="rule009_refined_top10",
            display_name="Rule009 refined top10 momentum",
            status="promoted_shadow",
            priority=20,
            sector=sector,
            rule=_rule(
                "tech_rule_009_refined_gap_range_top10",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 60},
                    {"feature": "spy_vol", "op": ">=", "value": 0.005},
                    {"feature": "spy_momentum", "op": ">=", "value": 0},
                    {"feature": "gap_pct", "op": "<=", "value": 0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                ],
                "Rule009 refined 10:30 high-vol Technology opportunity mode with gap/range filter.",
            ),
            scan_time_et="10:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Current Rule009 refined benchmark.",
        ),

        "rule036B_cap10": ReplayRuleSpec(
            rule_id="rule036B_cap10",
            display_name="Rule036B cap10 positive continuation",
            status="promoted_shadow_candidate",
            priority=35,
            sector=sector,
            rule=_rule(
                "tech_rule_036B_refined_1330_posContinuation_lowATR8_rangeLoose",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_vol", "op": "<", "value": 0.0075},
                    {"feature": "spy_momentum", "op": ">=", "value": -0.001},
                    {"feature": "momentum", "op": ">", "value": 0.0008},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0},
                    {"feature": "distance_to_vwap", "op": ">", "value": 0},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "rsi_14", "op": ">=", "value": 55},
                    {"feature": "range_tightness_30m", "op": ">=", "value": 0.00253},
                ],
                "13:30 Technology positive-continuation rule; low ATR reach and range looseness; rank by momentum desc; cap10 operating candidate.",
            ),
            scan_time_et="13:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validated in standalone path-dependent stress at 20/25/30 bps; run combined replay before live-shadow use.",
        ),
        "rule033_top20": ReplayRuleSpec(
            rule_id="rule033_top20",
            display_name="Rule033 top20 selloff rebound",
            status="live_shadow",
            priority=40,
            sector=sector,
            rule=_rule(
                "tech_rule_033_1330_selloff_rebound_volume_accel",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_ret", "op": "<=", "value": -0.0086881596898112},
                    {"feature": "momentum", "op": "<=", "value": -0.00135528523765575},
                    {"feature": "atr_reach", "op": "<=", "value": 13.0},
                    {"feature": "volume_acceleration", "op": ">=", "value": 1.0},
                ],
                "13:30 Technology selloff-event rebound monitor; rank by distance_to_day_low desc.",
            ),
            scan_time_et="13:30",
            rank_feature="distance_to_day_low",
            rank_direction="desc",
            max_signals_per_day=20,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=20,
            notes="Event-day rebound monitor; watch concentration.",
        ),
        "rule034_conservative_top20": ReplayRuleSpec(
            rule_id="rule034_conservative_top20",
            display_name="Rule034 conservative top20",
            status="live_shadow",
            priority=30,
            sector=sector,
            rule=_rule(
                "tech_rule_034_rule033_gapfilled0_atrreach10_conservative",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_ret", "op": "<=", "value": -0.0086881596898112},
                    {"feature": "momentum", "op": "<=", "value": -0.00135528523765575},
                    {"feature": "atr_reach", "op": "<=", "value": 10.0},
                    {"feature": "volume_acceleration", "op": ">=", "value": 1.0},
                    {"feature": "gap_filled", "op": "==", "value": 0},
                ],
                "Conservative Rule033 variant with gap_filled == 0 and ATR reach <= 10.",
            ),
            scan_time_et="13:30",
            rank_feature="distance_to_day_low",
            rank_direction="desc",
            max_signals_per_day=20,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=20,
            notes="Conservative Rule033 miss-exclusion monitor.",
        ),
    }


DEFAULT_RULE_IDS = ["rule009_refined_top10", "rule029_top3", "rule036B_cap10", "rule033_top20", "rule034_conservative_top20"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_float(v, default: float | None = None) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _clean(v):
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


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k); fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)


def _load_filtered_scan_rows(db_path: str, sector: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = rule_tester.load_scan_rows_from_db(db_path, sector, start_date=start_date, end_date=end_date)
    if df.empty:
        return df
    df, _diag = rule_tester.apply_standard_filters(df, "target", drop_0930=True, thin_tape_fraction=0.25)
    return df


def _apply_rank_cap(signals: pd.DataFrame, spec: ReplayRuleSpec) -> tuple[pd.DataFrame, int]:
    if signals.empty:
        return signals, 0
    n = int(spec.max_signals_per_day or 0)
    if n <= 0:
        return signals, 0
    feature = spec.rank_feature
    if feature not in signals.columns:
        raise ValueError(f"rank_feature {feature!r} not found for {spec.rule_id}")
    ranked = signals.copy()
    ranked["_rank_value"] = pd.to_numeric(ranked[feature], errors="coerce")
    if spec.rank_direction == "asc":
        ranked["_rank_sort"] = ranked["_rank_value"].replace([np.inf, -np.inf], np.nan).fillna(np.inf)
        ascending = True
    else:
        ranked["_rank_sort"] = ranked["_rank_value"].replace([np.inf, -np.inf], np.nan).fillna(-np.inf)
        ascending = False
    ranked = ranked.sort_values(["date", "_rank_sort", "symbol", "scan_time_et"], ascending=[True, ascending, True, True])
    ranked["rule_rank"] = ranked.groupby("date").cumcount() + 1
    capped = ranked.groupby("date", group_keys=False, sort=False).head(n).copy()
    dropped = len(signals) - len(capped)
    return capped.drop(columns=["_rank_value", "_rank_sort"], errors="ignore"), int(dropped)


def _candidate_rows_for_rule(df: pd.DataFrame, spec: ReplayRuleSpec) -> tuple[pd.DataFrame, dict]:
    mask = rule_tester.rule_mask(df, spec.rule)
    raw = df[mask].copy()
    raw_count = int(len(raw))
    capped, dropped = _apply_rank_cap(raw, spec)
    if capped.empty:
        return capped, {"rule_id": spec.rule_id, "raw_signals": raw_count, "selected_after_rule_cap": 0, "dropped_by_rule_cap": dropped}
    capped["rule_id"] = spec.rule_id
    capped["rule_display_name"] = spec.display_name
    capped["rule_priority"] = spec.priority
    capped["rank_feature"] = spec.rank_feature
    capped["rank_direction"] = spec.rank_direction
    capped["rank_value"] = pd.to_numeric(capped[spec.rank_feature], errors="coerce")
    capped["tp_bps_used"] = spec.tp_bps
    capped["sl_bps_used"] = spec.sl_bps
    capped["timestop_et"] = spec.timestop_et
    capped["entry_delay_minutes"] = spec.entry_delay_minutes
    capped["min_exit_minutes"] = spec.min_exit_minutes
    return capped, {"rule_id": spec.rule_id, "raw_signals": raw_count, "selected_after_rule_cap": int(len(capped)), "dropped_by_rule_cap": dropped}


def _select_portfolio_candidates(
    all_candidates: pd.DataFrame,
    *,
    global_max_trades_per_day: int,
    max_trades_per_symbol_per_day: int,
    dedupe_policy: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    if all_candidates.empty:
        return all_candidates.copy(), all_candidates.copy(), {"selected": 0, "rejected": 0}

    dedupe_policy = (dedupe_policy or "best_priority").lower()
    df = all_candidates.copy()
    df["selected"] = False
    df["rejection_reason"] = ""

    selected_idx: list[int] = []
    rejected: dict[int, str] = {}
    sym_day_counts: Counter[tuple[str, str]] = Counter()
    day_counts: Counter[str] = Counter()

    # Candidate ordering approximates live scanner order: date, scan time, priority,
    # then within-rule rank. For best_score, rank_value is used as tie-break but only
    # after scan/priority because rule scores are not commensurate across rules.
    sort_cols = ["date", "scan_time_et", "rule_priority", "rule_rank", "symbol"]
    df = df.sort_values(sort_cols, ascending=[True, True, True, True, True]).copy()

    if dedupe_policy == "allow_all":
        # Still apply the daily global cap if provided.
        for idx, row in df.iterrows():
            d = str(row["date"])
            if global_max_trades_per_day and day_counts[d] >= global_max_trades_per_day:
                rejected[idx] = "global_daily_cap"
                continue
            selected_idx.append(idx); day_counts[d] += 1
    else:
        for idx, row in df.iterrows():
            d = str(row["date"]); sym = str(row["symbol"])
            key = (d, sym)
            if max_trades_per_symbol_per_day and sym_day_counts[key] >= max_trades_per_symbol_per_day:
                rejected[idx] = "symbol_day_dedupe_or_cap"
                continue
            if global_max_trades_per_day and day_counts[d] >= global_max_trades_per_day:
                rejected[idx] = "global_daily_cap"
                continue
            selected_idx.append(idx)
            sym_day_counts[key] += 1
            day_counts[d] += 1

    df.loc[selected_idx, "selected"] = True
    for idx, reason in rejected.items():
        df.loc[idx, "rejection_reason"] = reason
    selected = df[df["selected"]].copy()
    rejected_df = df[~df["selected"]].copy()
    reason_counts = rejected_df["rejection_reason"].replace("", "not_selected").value_counts().to_dict() if not rejected_df.empty else {}
    return selected, rejected_df, {"selected": int(len(selected)), "rejected": int(len(rejected_df)), "rejection_reasons": reason_counts}


def _signal_export(sig: pd.Series) -> dict:
    cols = [
        "date", "symbol", "scan_time_et", "sector", "minutes_since_open", "scan_price",
        "rule_id", "rule_display_name", "rule_priority", "rule_rank", "rank_feature", "rank_direction", "rank_value",
        "open_to_scan_return", "gap_pct", "intraday_range_position", "distance_to_vwap",
        "distance_to_day_high", "distance_to_day_low", "rsi_14", "macd_hist", "ema_9_distance",
        "ema_20_distance", "ema_50_distance", "relative_volume", "realized_vol_so_far",
        "rs_leakfree", "momentum", "vwap_slope", "atr_reach", "trend_str", "range_expansion",
        "spy_ret", "ret_vs_spy", "spy_momentum", "mom_vs_spy", "spy_vol", "gap_filled",
        "range_tightness_30m", "bars_in_range_20bps", "is_nr7", "dist_to_day_high_bps",
        "broke_day_high_this_bar", "broke_opening_range_high", "bars_since_day_high",
        "dist_to_prev_close_bps", "dist_to_5d_high_bps", "dist_to_20d_high_bps",
        "days_since_20d_high", "volume_acceleration", "cumulative_volume_vs_typical",
        "sector_breadth_up", "new_highs_in_sector", "regime_ok", "target", "return_to_cutoff",
        "min_return_before_cutoff", "max_return_before_cutoff", "target_50bps", "target_peak_50bps",
    ]
    return {c: _clean(sig.get(c)) for c in cols if c in sig.index}


def _simulate_selected(selected: pd.DataFrame, specs_by_id: dict[str, ReplayRuleSpec], db_path: str, *, slippage_bps: float, just_in_time_backfill: bool, delete_raw_bars_after: bool) -> list[dict]:
    if selected.empty:
        return []
    trades: list[dict] = []
    selected = selected.sort_values(["date", "scan_time_et", "rule_priority", "symbol"]).copy()
    with storage.connect(db_path) as conn:
        for (symbol, date), group in selected.groupby(["symbol", "date"], sort=False):
            bars = backtest._ensure_raw_bars(conn, db_path, str(symbol), str(date), just_in_time_backfill)
            for _, sig in group.iterrows():
                spec = specs_by_id[str(sig["rule_id"])]
                entry_delay = max(0, int(spec.entry_delay_minutes or 0))
                entry_time_et = backtest._hhmm_plus_minutes(str(sig["scan_time_et"]), entry_delay) if entry_delay else str(sig["scan_time_et"])
                scan_price = _safe_float(sig.get("scan_price"), 0.0) or 0.0
                base = {
                    "rule_id": spec.rule_id,
                    "rule_display_name": spec.display_name,
                    "symbol": str(symbol),
                    "signal_date": str(date),
                    "signal_time_et": str(sig.get("scan_time_et")),
                    "rule_rank": _clean(sig.get("rule_rank")),
                    "rank_feature": spec.rank_feature,
                    "rank_direction": spec.rank_direction,
                    "rank_value": _clean(sig.get("rank_value")),
                    "scan_price_ref": scan_price,
                    "entry_time_et": entry_time_et,
                    "entry_delay_minutes": entry_delay,
                    "tp_bps_used": spec.tp_bps,
                    "sl_bps_used": spec.sl_bps,
                    "slippage_bps": slippage_bps,
                    "min_exit_minutes": spec.min_exit_minutes,
                    **{f"sig_{k}": v for k, v in _signal_export(sig).items() if k not in {"symbol", "date"}},
                }
                if not bars:
                    trades.append({**base, "entry_ts_utc": "NA", "entry_price": scan_price, "exit_price": scan_price, "exit_time_et": "NA", "exit_reason": "NO_DATA", "minutes_held": 0, "gross_return_bps": 0.0, "net_return_bps": 0.0})
                    continue
                entry_bar_ts = backtest._find_scan_bar_ts(bars, entry_time_et, signal_date_et=str(date))
                entry_price = scan_price
                entry_source = "scan_price"
                if entry_bar_ts is not None and entry_delay > 0:
                    entry_bar = backtest._find_bar_by_ts(bars, entry_bar_ts)
                    if entry_bar is None:
                        entry_bar_ts = None
                    else:
                        entry_price = float(entry_bar["open"])
                        entry_source = f"delayed_bar_open_plus_{entry_delay}m"
                if entry_bar_ts is None:
                    trades.append({**base, "entry_ts_utc": "NA", "entry_price_source": entry_source, "entry_price": scan_price, "exit_price": scan_price, "exit_time_et": "NA", "exit_reason": "NO_DATA", "minutes_held": 0, "gross_return_bps": 0.0, "net_return_bps": 0.0})
                    continue
                res = backtest._simulate_trade(
                    bars=bars,
                    entry_ts_utc=entry_bar_ts,
                    entry_price=entry_price,
                    tp_level=spec.tp_bps,
                    sl_level=spec.sl_bps,
                    timestop_et_hhmm=spec.timestop_et,
                    slippage_bps=slippage_bps,
                    entry_slippage_split=0.5,
                    min_exit_minutes=spec.min_exit_minutes,
                )
                trades.append({**base, "entry_ts_utc": entry_bar_ts, "entry_price_source": entry_source, "entry_price": entry_price, **res})
            if delete_raw_bars_after:
                storage.delete_raw_bars_for_day(conn, str(symbol), str(date), preserve_spy=True)
    return trades


def _summarise_trades(trades: list[dict], *, total_candidate_rows: int, selected_rows: int, rejected_rows: int) -> dict:
    exits = [t for t in trades if t.get("exit_reason") != "NO_DATA"]
    net = np.array([float(t.get("net_return_bps") or 0.0) for t in exits], dtype=float)
    by_rule: dict[str, dict] = {}
    for rid in sorted({t.get("rule_id") for t in trades}):
        rt = [t for t in exits if t.get("rule_id") == rid]
        rnet = np.array([float(t.get("net_return_bps") or 0.0) for t in rt], dtype=float)
        by_rule[str(rid)] = {
            "n_trades": int(len(rt)),
            "win_rate": float((rnet > 0).mean()) if len(rnet) else 0.0,
            "net_pnl_bps": float(rnet.sum()) if len(rnet) else 0.0,
            "avg_net_bps_per_trade": float(rnet.mean()) if len(rnet) else 0.0,
        }
    by_date = defaultdict(float)
    by_date_n = Counter()
    for t in exits:
        by_date[t["signal_date"]] += float(t.get("net_return_bps") or 0.0)
        by_date_n[t["signal_date"]] += 1
    daily_vals = np.array(list(by_date.values()), dtype=float) if by_date else np.array([], dtype=float)
    worst_day = None
    if by_date:
        d = min(by_date, key=lambda k: by_date[k])
        worst_day = {"date": d, "net_pnl_bps": float(by_date[d]), "n_trades": int(by_date_n[d])}
    return {
        "total_candidate_rows_after_rule_caps": int(total_candidate_rows),
        "selected_rows_after_portfolio_controls": int(selected_rows),
        "rejected_rows_after_portfolio_controls": int(rejected_rows),
        "n_trades": int(len(exits)),
        "n_no_data": int(len(trades) - len(exits)),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "net_pnl_bps": float(net.sum()) if len(net) else 0.0,
        "avg_net_bps_per_trade": float(net.mean()) if len(net) else 0.0,
        "exit_reason_mix": dict(Counter(t.get("exit_reason") for t in trades)),
        "firing_days": int(len(by_date)),
        "avg_trades_per_firing_day": float(sum(by_date_n.values()) / len(by_date_n)) if by_date_n else 0.0,
        "worst_day": worst_day,
        "p05_daily_net_bps": float(np.percentile(daily_vals, 5)) if len(daily_vals) else 0.0,
        "best_day_net_bps": float(np.max(daily_vals)) if len(daily_vals) else 0.0,
        "rule_summary": by_rule,
    }


def _overlap_matrix(all_candidates: pd.DataFrame) -> list[dict]:
    if all_candidates.empty:
        return []
    grouped = all_candidates.groupby(["date", "symbol"])["rule_id"].apply(lambda x: sorted(set(map(str, x)))).reset_index()
    pairs = Counter()
    for rules in grouped["rule_id"]:
        for i, a in enumerate(rules):
            for b in rules[i + 1:]:
                pairs[(a, b)] += 1
    return [{"rule_a": a, "rule_b": b, "overlap_symbol_days": int(n)} for (a, b), n in sorted(pairs.items())]


def _top_symbol_concentration(trades: list[dict]) -> list[dict]:
    exits = [t for t in trades if t.get("exit_reason") != "NO_DATA"]
    count = Counter(t["symbol"] for t in exits)
    pnl = defaultdict(float)
    for t in exits:
        pnl[t["symbol"]] += float(t.get("net_return_bps") or 0.0)
    return [{"symbol": s, "n_trades": int(n), "net_pnl_bps": float(pnl[s])} for s, n in count.most_common(25)]


def _daily_summary_rows(trades: list[dict]) -> list[dict]:
    exits = [t for t in trades if t.get("exit_reason") != "NO_DATA"]
    by = defaultdict(list)
    for t in exits:
        by[t["signal_date"]].append(t)
    rows = []
    cumulative = 0.0
    for d in sorted(by):
        ts = by[d]
        net = [float(t.get("net_return_bps") or 0.0) for t in ts]
        cumulative += sum(net)
        rows.append({
            "date": d,
            "n_trades": len(ts),
            "net_pnl_bps": float(sum(net)),
            "avg_net_bps_per_trade": float(sum(net) / len(net)) if net else 0.0,
            "win_rate": float(sum(1 for v in net if v > 0) / len(net)) if net else 0.0,
            "cumulative_net_pnl_bps": float(cumulative),
            "rules_fired": ";".join(sorted(set(t["rule_id"] for t in ts))),
        })
    return rows


def run_market_replay(
    *,
    start_date: str,
    end_date: str,
    sector: str = "Information Technology",
    rule_ids: list[str] | None = None,
    slippage_bps: float = 25.0,
    global_max_trades_per_day: int = 10,
    max_trades_per_symbol_per_day: int = 1,
    dedupe_policy: str = "best_priority",
    capital_mode: str = "equal_weight",
    include_virtual_trades: bool = True,
    just_in_time_backfill: bool = True,
    delete_raw_bars_after: bool = False,
    db_path: str = config.DB_PATH,
    evidence_dir: str = config.EVIDENCE_PACK_DIR,
) -> dict:
    """Run a multi-rule historical daily replay and write a ZIP evidence pack."""
    generated_at = datetime.now(timezone.utc)
    reg = registry(sector)
    ids = rule_ids or DEFAULT_RULE_IDS
    unknown = [r for r in ids if r not in reg]
    if unknown:
        raise ValueError(f"Unknown replay rule ids: {unknown}. Known: {sorted(reg)}")
    specs = [reg[r] for r in ids]
    specs_by_id = {s.rule_id: s for s in specs}

    df = _load_filtered_scan_rows(db_path, sector, start_date, end_date)
    if df.empty:
        raise ValueError("No historical scan rows found after standard filters for requested replay range")

    all_parts = []
    rule_diag = []
    for spec in specs:
        part, diag = _candidate_rows_for_rule(df, spec)
        rule_diag.append(diag)
        if not part.empty:
            all_parts.append(part)
    all_candidates = pd.concat(all_parts, ignore_index=True) if all_parts else pd.DataFrame()
    selected, rejected, select_diag = _select_portfolio_candidates(
        all_candidates,
        global_max_trades_per_day=int(global_max_trades_per_day or 0),
        max_trades_per_symbol_per_day=int(max_trades_per_symbol_per_day or 0),
        dedupe_policy=dedupe_policy,
    )

    trades = _simulate_selected(
        selected,
        specs_by_id,
        db_path,
        slippage_bps=float(slippage_bps),
        just_in_time_backfill=bool(just_in_time_backfill),
        delete_raw_bars_after=bool(delete_raw_bars_after),
    ) if include_virtual_trades else []

    summary = _summarise_trades(
        trades,
        total_candidate_rows=len(all_candidates),
        selected_rows=len(selected),
        rejected_rows=len(rejected),
    ) if include_virtual_trades else {
        "total_candidate_rows_after_rule_caps": int(len(all_candidates)),
        "selected_rows_after_portfolio_controls": int(len(selected)),
        "rejected_rows_after_portfolio_controls": int(len(rejected)),
        "n_trades": 0,
        "note": "include_virtual_trades=false; candidate selection only",
    }

    stem = f"market_replay_{start_date}_to_{end_date}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}".replace(":", "")
    pack_dir = Path(evidence_dir)
    work_dir = pack_dir / stem
    work_dir.mkdir(parents=True, exist_ok=True)

    # CSV outputs
    all_signal_rows = [_signal_export(r) for _, r in all_candidates.iterrows()] if not all_candidates.empty else []
    selected_rows = [_signal_export(r) for _, r in selected.iterrows()] if not selected.empty else []
    rejected_rows = []
    if not rejected.empty:
        for _, r in rejected.iterrows():
            row = _signal_export(r)
            row["rejection_reason"] = _clean(r.get("rejection_reason"))
            rejected_rows.append(row)

    _write_csv(work_dir / "market_replay_all_signals.csv", all_signal_rows)
    _write_csv(work_dir / "market_replay_selected_signals.csv", selected_rows)
    _write_csv(work_dir / "market_replay_rejected_signals.csv", rejected_rows)
    _write_csv(work_dir / "market_replay_selected_trades.csv", trades)
    _write_csv(work_dir / "market_replay_daily_summary.csv", _daily_summary_rows(trades))
    _write_csv(work_dir / "market_replay_overlap_matrix.csv", _overlap_matrix(all_candidates))
    _write_csv(work_dir / "market_replay_symbol_concentration.csv", _top_symbol_concentration(trades))

    rule_summary_rows = []
    for spec in specs:
        rs = (summary.get("rule_summary") or {}).get(spec.rule_id, {})
        diag = next((d for d in rule_diag if d["rule_id"] == spec.rule_id), {})
        rule_summary_rows.append({
            "rule_id": spec.rule_id,
            "display_name": spec.display_name,
            "priority": spec.priority,
            "status": spec.status,
            "raw_signals": diag.get("raw_signals", 0),
            "selected_after_rule_cap": diag.get("selected_after_rule_cap", 0),
            "dropped_by_rule_cap": diag.get("dropped_by_rule_cap", 0),
            **rs,
        })
    _write_csv(work_dir / "market_replay_rule_summary.csv", rule_summary_rows)

    manifest = {
        "kind": "multi_rule_market_replay",
        "version": config.APP_VERSION,
        "generated_at_utc": generated_at.isoformat(),
        "request": {
            "start_date": start_date,
            "end_date": end_date,
            "sector": sector,
            "rules": ids,
            "slippage_bps": slippage_bps,
            "global_max_trades_per_day": global_max_trades_per_day,
            "max_trades_per_symbol_per_day": max_trades_per_symbol_per_day,
            "dedupe_policy": dedupe_policy,
            "capital_mode": capital_mode,
            "include_virtual_trades": include_virtual_trades,
            "just_in_time_backfill": just_in_time_backfill,
        },
        "rule_registry": [s.to_public_dict() for s in specs],
        "rule_signal_diagnostics": rule_diag,
        "selection_diagnostics": select_diag,
        "summary": summary,
        "outputs": [
            "market_replay_manifest.json",
            "market_replay_daily_summary.csv",
            "market_replay_all_signals.csv",
            "market_replay_selected_signals.csv",
            "market_replay_selected_trades.csv",
            "market_replay_rejected_signals.csv",
            "market_replay_rule_summary.csv",
            "market_replay_overlap_matrix.csv",
            "market_replay_symbol_concentration.csv",
        ],
    }
    (work_dir / "market_replay_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    zip_path = pack_dir / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for child in sorted(work_dir.iterdir()):
            if child.is_file():
                zf.write(child, arcname=child.name)

    return {
        **manifest,
        "pack_filename": zip_path.name,
        "pack_download_url": f"/packs/{zip_path.name}",
    }
