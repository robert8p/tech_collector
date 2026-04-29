"""Multi-rule historical market replay engine (v0.7.39).

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
import heapq
import zipfile
from collections import Counter, defaultdict, deque
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
    """Return the canonical v0.7.39 multi-rule replay registry."""
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

        "rule038_top15": ReplayRuleSpec(
            rule_id="rule038_top15",
            display_name="Rule038 top15 high-vol breadth ATR-low",
            status="promoted_shadow_candidate",
            priority=36,
            sector=sector,
            rule=_rule(
                "tech_rule_038_1330_highvol_breadth_atrlow_top15",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_vol", "op": ">=", "value": 0.004},
                    {"feature": "spy_momentum", "op": ">=", "value": 0.0},
                    {"feature": "gap_pct", "op": "<=", "value": 0.0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.4},
                ],
                "13:30 Technology high-volatility controlled momentum with sector-breadth confirmation; rank by rs_leakfree desc; top15 live-shadow candidate.",
            ),
            scan_time_et="13:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=15,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Rule038 v4 standalone batch and six-rule 25 bps market replay were strongly positive; promote to live-shadow evidence collection, not live capital.",
        ),
"rule039_top10": ReplayRuleSpec(
    rule_id="rule039_top10",
    display_name="Rule039 top10 10:30 ATR-breadth momentum",
    status="candidate_validation",
    priority=21,
    sector=sector,
    rule=_rule(
        "tech_rule_039_1030_highvol_atr_breadth_top10_momspy",
        sector,
        [
            {"feature": "minutes_since_open", "op": "==", "value": 60},
            {"feature": "spy_vol", "op": ">=", "value": 0.005},
            {"feature": "spy_momentum", "op": ">=", "value": 0.0},
            {"feature": "atr_reach", "op": "<=", "value": 8.0},
            {"feature": "sector_breadth_up", "op": ">=", "value": 0.4},
        ],
        "Claude Rule039 candidate: 10:30 high-vol momentum companion to Rule009, constrained by ATR reach and sector breadth confirmation; rank by mom_vs_spy desc; top10 validation candidate only.",
    ),
    scan_time_et="10:30",
    rank_feature="mom_vs_spy",
    rank_direction="desc",
    max_signals_per_day=10,
    tp_bps=100,
    sl_bps=200,
    default_slippage_bps=25,
    notes="Candidate only. Combined replay decides whether it adds value over the real six-rule baseline.",
),

"rule040_top15": ReplayRuleSpec(
    rule_id="rule040_top15",
    display_name="Rule040 top15 11:30 day-high breakout",
    status="promoted_shadow_candidate",
    priority=25,
    sector=sector,
    rule=_rule(
        "tech_rule_040_1130_dayhigh_break_top15_rsleakfree",
        sector,
        [
            {"feature": "minutes_since_open", "op": "==", "value": 120},
            {"feature": "broke_day_high_this_bar", "op": "==", "value": 1},
            {"feature": "spy_vol", "op": ">=", "value": 0.004},
            {"feature": "sector_breadth_up", "op": ">=", "value": 0.5},
            {"feature": "atr_reach", "op": "<=", "value": 8.0},
        ],
        "Claude Rule040 candidate: 11:30 day-high breakout in a confirmed regime; rank by rs_leakfree desc; top15 validation candidate only.",
    ),
    scan_time_et="11:30",
    rank_feature="rs_leakfree",
    rank_direction="desc",
    max_signals_per_day=15,
    tp_bps=100,
    sl_bps=200,
    default_slippage_bps=25,
    notes="Validated at 20 bps standalone, positive in current-six combined replay at 25 bps, and positive again in exact-settings capital-recycling replay with allow_all duplicates and 10 slots. Promote to live-shadow candidate, not live capital.",
),

"rule041_top15": ReplayRuleSpec(
    rule_id="rule041_top15",
    display_name="Rule041 top15 13:30 ORB sustained",
    status="candidate_validation",
    priority=37,
    sector=sector,
    rule=_rule(
        "tech_rule_041_1330_orb_sustained_top15",
        sector,
        [
            {"feature": "minutes_since_open", "op": "==", "value": 240},
            {"feature": "broke_opening_range_high", "op": "==", "value": 1},
            {"feature": "orb_strength", "op": ">=", "value": 1.0},
            {"feature": "atr_reach", "op": "<=", "value": 6.0},
            {"feature": "sector_breadth_up", "op": ">=", "value": 0.3},
        ],
        "Claude Rule041 candidate: 13:30 sustained opening-range breakout; rank by orb_strength desc; top15 validation candidate only.",
    ),
    scan_time_et="13:30",
    rank_feature="orb_strength",
    rank_direction="desc",
    max_signals_per_day=15,
    tp_bps=100,
    sl_bps=200,
    default_slippage_bps=25,
    notes="Candidate only. Claude stress tests flagged 2025Q2 P1 concentration and did not measure overlap versus Rule036B; treat as unproven until combined replay confirms incremental value.",
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
        "Rule009_FILT_momspy_gt_0p0004": ReplayRuleSpec(
            rule_id="Rule009_FILT_momspy_gt_0p0004",
            display_name="Rule009 filter mom_vs_spy > 0.0004",
            status="validation_filter",
            priority=20,
            sector=sector,
            rule=_rule(
                "tech_rule_009_refined_gap_range_top10_momspy_gt_0p0004",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 60},
                    {"feature": "spy_vol", "op": ">=", "value": 0.005},
                    {"feature": "spy_momentum", "op": ">=", "value": 0},
                    {"feature": "gap_pct", "op": "<=", "value": 0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0004},
                ],
                "Validation filter on Rule009 refined: require stronger mom_vs_spy to remove weak morning chases.",
            ),
            scan_time_et="10:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule009 filter ladder: stronger mom_vs_spy > 0.0004.",
        ),
        "Rule009_FILT_momspy_gt_0p0006": ReplayRuleSpec(
            rule_id="Rule009_FILT_momspy_gt_0p0006",
            display_name="Rule009 filter mom_vs_spy > 0.0006",
            status="validation_filter",
            priority=20,
            sector=sector,
            rule=_rule(
                "tech_rule_009_refined_gap_range_top10_momspy_gt_0p0006",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 60},
                    {"feature": "spy_vol", "op": ">=", "value": 0.005},
                    {"feature": "spy_momentum", "op": ">=", "value": 0},
                    {"feature": "gap_pct", "op": "<=", "value": 0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0006},
                ],
                "Validation filter on Rule009 refined: require stronger mom_vs_spy to remove weak morning chases.",
            ),
            scan_time_et="10:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule009 filter ladder: stronger mom_vs_spy > 0.0006.",
        ),
        "Rule009_FILT_momspy_gt_0p0008": ReplayRuleSpec(
            rule_id="Rule009_FILT_momspy_gt_0p0008",
            display_name="Rule009 filter mom_vs_spy > 0.0008",
            status="validation_filter",
            priority=20,
            sector=sector,
            rule=_rule(
                "tech_rule_009_refined_gap_range_top10_momspy_gt_0p0008",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 60},
                    {"feature": "spy_vol", "op": ">=", "value": 0.005},
                    {"feature": "spy_momentum", "op": ">=", "value": 0},
                    {"feature": "gap_pct", "op": "<=", "value": 0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0008},
                ],
                "Validation filter on Rule009 refined: require stronger mom_vs_spy to remove weak morning chases.",
            ),
            scan_time_et="10:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule009 filter ladder: stronger mom_vs_spy > 0.0008.",
        ),
        "Rule033_FILT_rangepos_lt_0p40": ReplayRuleSpec(
            rule_id="Rule033_FILT_rangepos_lt_0p40",
            display_name="Rule033 filter range position < 0.40",
            status="validation_filter",
            priority=40,
            sector=sector,
            rule=_rule(
                "tech_rule_033_1330_selloff_rebound_volume_accel_rangepos_lt_0p40",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_ret", "op": "<=", "value": -0.0086881596898112},
                    {"feature": "momentum", "op": "<=", "value": -0.00135528523765575},
                    {"feature": "atr_reach", "op": "<=", "value": 13.0},
                    {"feature": "volume_acceleration", "op": ">=", "value": 1.0},
                    {"feature": "intraday_range_position", "op": "<", "value": 0.40},
                ],
                "Validation filter on Rule033: avoid already-extended afternoon names via lower intraday range position.",
            ),
            scan_time_et="13:30",
            rank_feature="distance_to_day_low",
            rank_direction="desc",
            max_signals_per_day=20,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=20,
            notes="Validation-only Rule033 filter ladder: intraday_range_position < 0.40.",
        ),
        "Rule033_FILT_rangepos_lt_0p35": ReplayRuleSpec(
            rule_id="Rule033_FILT_rangepos_lt_0p35",
            display_name="Rule033 filter range position < 0.35",
            status="validation_filter",
            priority=40,
            sector=sector,
            rule=_rule(
                "tech_rule_033_1330_selloff_rebound_volume_accel_rangepos_lt_0p35",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_ret", "op": "<=", "value": -0.0086881596898112},
                    {"feature": "momentum", "op": "<=", "value": -0.00135528523765575},
                    {"feature": "atr_reach", "op": "<=", "value": 13.0},
                    {"feature": "volume_acceleration", "op": ">=", "value": 1.0},
                    {"feature": "intraday_range_position", "op": "<", "value": 0.35},
                ],
                "Validation filter on Rule033: avoid already-extended afternoon names via lower intraday range position.",
            ),
            scan_time_et="13:30",
            rank_feature="distance_to_day_low",
            rank_direction="desc",
            max_signals_per_day=20,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=20,
            notes="Validation-only Rule033 filter ladder: intraday_range_position < 0.35.",
        ),
        "Rule033_FILT_rangepos_lt_0p30": ReplayRuleSpec(
            rule_id="Rule033_FILT_rangepos_lt_0p30",
            display_name="Rule033 filter range position < 0.30",
            status="validation_filter",
            priority=40,
            sector=sector,
            rule=_rule(
                "tech_rule_033_1330_selloff_rebound_volume_accel_rangepos_lt_0p30",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_ret", "op": "<=", "value": -0.0086881596898112},
                    {"feature": "momentum", "op": "<=", "value": -0.00135528523765575},
                    {"feature": "atr_reach", "op": "<=", "value": 13.0},
                    {"feature": "volume_acceleration", "op": ">=", "value": 1.0},
                    {"feature": "intraday_range_position", "op": "<", "value": 0.30},
                ],
                "Validation filter on Rule033: avoid already-extended afternoon names via lower intraday range position.",
            ),
            scan_time_et="13:30",
            rank_feature="distance_to_day_low",
            rank_direction="desc",
            max_signals_per_day=20,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=20,
            notes="Validation-only Rule033 filter ladder: intraday_range_position < 0.30.",
        ),
        "Rule038_FILT_breadth_lt_0p85": ReplayRuleSpec(
            rule_id="Rule038_FILT_breadth_lt_0p85",
            display_name="Rule038 filter breadth < 0.85",
            status="validation_filter",
            priority=36,
            sector=sector,
            rule=_rule(
                "tech_rule_038_1330_highvol_breadth_atrlow_top15_breadth_lt_0p85",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_vol", "op": ">=", "value": 0.004},
                    {"feature": "spy_momentum", "op": ">=", "value": 0.0},
                    {"feature": "gap_pct", "op": "<=", "value": 0.0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.4},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.85},
                ],
                "Validation filter on Rule038: avoid euphoric breadth afternoons.",
            ),
            scan_time_et="13:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=15,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule038 filter ladder: sector_breadth_up < 0.85.",
        ),
        "Rule038_FILT_breadth_lt_0p83": ReplayRuleSpec(
            rule_id="Rule038_FILT_breadth_lt_0p83",
            display_name="Rule038 filter breadth < 0.83",
            status="validation_filter",
            priority=36,
            sector=sector,
            rule=_rule(
                "tech_rule_038_1330_highvol_breadth_atrlow_top15_breadth_lt_0p83",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_vol", "op": ">=", "value": 0.004},
                    {"feature": "spy_momentum", "op": ">=", "value": 0.0},
                    {"feature": "gap_pct", "op": "<=", "value": 0.0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.4},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.83},
                ],
                "Validation filter on Rule038: avoid euphoric breadth afternoons.",
            ),
            scan_time_et="13:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=15,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule038 filter ladder: sector_breadth_up < 0.83.",
        ),
        "Rule038_FILT_breadth_lt_0p80": ReplayRuleSpec(
            rule_id="Rule038_FILT_breadth_lt_0p80",
            display_name="Rule038 filter breadth < 0.80",
            status="validation_filter",
            priority=36,
            sector=sector,
            rule=_rule(
                "tech_rule_038_1330_highvol_breadth_atrlow_top15_breadth_lt_0p80",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "spy_vol", "op": ">=", "value": 0.004},
                    {"feature": "spy_momentum", "op": ">=", "value": 0.0},
                    {"feature": "gap_pct", "op": "<=", "value": 0.0},
                    {"feature": "range_expansion", "op": ">=", "value": 1.0},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.4},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.80},
                ],
                "Validation filter on Rule038: avoid euphoric breadth afternoons.",
            ),
            scan_time_et="13:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=15,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule038 filter ladder: sector_breadth_up < 0.80.",
        ),
        "Rule036B_FILT_breadth_gt_0p40": ReplayRuleSpec(
            rule_id="Rule036B_FILT_breadth_gt_0p40",
            display_name="Rule036B filter breadth > 0.40",
            status="validation_filter",
            priority=35,
            sector=sector,
            rule=_rule(
                "tech_rule_036B_refined_1330_posContinuation_lowATR8_rangeLoose_breadth_gt_0p40",
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
                    {"feature": "sector_breadth_up", "op": ">", "value": 0.40},
                ],
                "Validation filter on Rule036B: require moderate sector participation.",
            ),
            scan_time_et="13:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule036B filter ladder: sector_breadth_up > 0.40.",
        ),
        "Rule036B_FILT_breadth_gt_0p43": ReplayRuleSpec(
            rule_id="Rule036B_FILT_breadth_gt_0p43",
            display_name="Rule036B filter breadth > 0.43",
            status="validation_filter",
            priority=35,
            sector=sector,
            rule=_rule(
                "tech_rule_036B_refined_1330_posContinuation_lowATR8_rangeLoose_breadth_gt_0p43",
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
                    {"feature": "sector_breadth_up", "op": ">", "value": 0.43},
                ],
                "Validation filter on Rule036B: require moderate sector participation.",
            ),
            scan_time_et="13:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule036B filter ladder: sector_breadth_up > 0.43.",
        ),
        "Rule036B_FILT_breadth_gt_0p46": ReplayRuleSpec(
            rule_id="Rule036B_FILT_breadth_gt_0p46",
            display_name="Rule036B filter breadth > 0.46",
            status="validation_filter",
            priority=35,
            sector=sector,
            rule=_rule(
                "tech_rule_036B_refined_1330_posContinuation_lowATR8_rangeLoose_breadth_gt_0p46",
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
                    {"feature": "sector_breadth_up", "op": ">", "value": 0.46},
                ],
                "Validation filter on Rule036B: require moderate sector participation.",
            ),
            scan_time_et="13:30",
            rank_feature="momentum",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Validation-only Rule036B filter ladder: sector_breadth_up > 0.46.",
        ),
        "Rule042_1130_pullback_reclaim_A": ReplayRuleSpec(
            rule_id="Rule042_1130_pullback_reclaim_A",
            display_name="Rule042 11:30 pullback reclaim A",
            status="validation_candidate",
            priority=24,
            sector=sector,
            rule=_rule(
                "tech_rule_042_1130_pullback_reclaim_A",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 120},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.004},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.03},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.55},
                    {"feature": "intraday_range_position", "op": ">=", "value": 0.20},
                    {"feature": "atr_reach", "op": "<=", "value": 6.5},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.45},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0},
                    {"feature": "distance_to_vwap", "op": ">=", "value": -0.004},
                    {"feature": "vwap_slope", "op": ">", "value": -0.0005},
                ],
                "11:30 pullback-reclaim candidate intended to differ from Rule040 day-high breakout logic.",
            ),
            scan_time_et="11:30",
            rank_feature="mom_vs_spy",
            rank_direction="desc",
            max_signals_per_day=5,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 11:30 pullback-reclaim A.",
        ),
        "Rule042_1130_pullback_reclaim_B": ReplayRuleSpec(
            rule_id="Rule042_1130_pullback_reclaim_B",
            display_name="Rule042 11:30 pullback reclaim B",
            status="validation_candidate",
            priority=24,
            sector=sector,
            rule=_rule(
                "tech_rule_042_1130_pullback_reclaim_B",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 120},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.006},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.025},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.50},
                    {"feature": "intraday_range_position", "op": ">=", "value": 0.15},
                    {"feature": "atr_reach", "op": "<=", "value": 7.5},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.40},
                    {"feature": "momentum", "op": ">", "value": 0.0},
                    {"feature": "distance_to_vwap", "op": ">=", "value": -0.003},
                    {"feature": "vwap_slope", "op": ">", "value": 0.0},
                ],
                "11:30 pullback-reclaim candidate intended to differ from Rule040 day-high breakout logic.",
            ),
            scan_time_et="11:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 11:30 pullback-reclaim B.",
        ),
        "Rule042_1130_pullback_reclaim_C": ReplayRuleSpec(
            rule_id="Rule042_1130_pullback_reclaim_C",
            display_name="Rule042 11:30 pullback reclaim C",
            status="validation_candidate",
            priority=24,
            sector=sector,
            rule=_rule(
                "tech_rule_042_1130_pullback_reclaim_C",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 120},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.008},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.04},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.45},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.50},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0005},
                    {"feature": "distance_to_vwap", "op": ">", "value": -0.002},
                ],
                "11:30 pullback-reclaim candidate intended to differ from Rule040 day-high breakout logic.",
            ),
            scan_time_et="11:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 11:30 pullback-reclaim C.",
        ),
        "Rule043_1430_compression_break_A": ReplayRuleSpec(
            rule_id="Rule043_1430_compression_break_A",
            display_name="Rule043 14:30 compression breakout A",
            status="validation_candidate",
            priority=50,
            sector=sector,
            rule=_rule(
                "tech_rule_043_1430_compression_break_A",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 300},
                    {"feature": "broke_day_high_this_bar", "op": "==", "value": 1},
                    {"feature": "bars_in_range_20bps", "op": ">=", "value": 6},
                    {"feature": "atr_reach", "op": "<=", "value": 7.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.35},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.80},
                    {"feature": "spy_momentum", "op": ">=", "value": 0.0},
                ],
                "14:30 compression breakout candidate designed to cover a late-day zone the current portfolio barely touches.",
            ),
            scan_time_et="14:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 14:30 compression breakout A.",
        ),
        "Rule043_1430_compression_break_B": ReplayRuleSpec(
            rule_id="Rule043_1430_compression_break_B",
            display_name="Rule043 14:30 compression breakout B",
            status="validation_candidate",
            priority=50,
            sector=sector,
            rule=_rule(
                "tech_rule_043_1430_compression_break_B",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 300},
                    {"feature": "broke_day_high_this_bar", "op": "==", "value": 1},
                    {"feature": "bars_in_range_20bps", "op": ">=", "value": 8},
                    {"feature": "open_to_scan_return", "op": ">=", "value": 0.002},
                    {"feature": "open_to_scan_return", "op": "<=", "value": 0.035},
                    {"feature": "atr_reach", "op": "<=", "value": 8.5},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.40},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.83},
                ],
                "14:30 compression breakout candidate designed to cover a late-day zone the current portfolio barely touches.",
            ),
            scan_time_et="14:30",
            rank_feature="mom_vs_spy",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 14:30 compression breakout B.",
        ),
        "Rule043_1430_compression_break_C": ReplayRuleSpec(
            rule_id="Rule043_1430_compression_break_C",
            display_name="Rule043 14:30 compression breakout C",
            status="validation_candidate",
            priority=50,
            sector=sector,
            rule=_rule(
                "tech_rule_043_1430_compression_break_C",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 300},
                    {"feature": "broke_day_high_this_bar", "op": "==", "value": 1},
                    {"feature": "is_nr7", "op": "==", "value": 1},
                    {"feature": "atr_reach", "op": "<=", "value": 8.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.35},
                    {"feature": "sector_breadth_up", "op": "<", "value": 0.80},
                    {"feature": "distance_to_vwap", "op": ">=", "value": 0.0},
                ],
                "14:30 compression breakout candidate designed to cover a late-day zone the current portfolio barely touches.",
            ),
            scan_time_et="14:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=5,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 14:30 compression breakout C.",
        ),
        "Rule044_1330_lower_range_recovery_A": ReplayRuleSpec(
            rule_id="Rule044_1330_lower_range_recovery_A",
            display_name="Rule044 13:30 lower-range recovery A",
            status="validation_candidate",
            priority=45,
            sector=sector,
            rule=_rule(
                "tech_rule_044_1330_lower_range_recovery_A",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.005},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.04},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.35},
                    {"feature": "atr_reach", "op": "<=", "value": 9.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.40},
                    {"feature": "momentum", "op": ">", "value": 0.0},
                    {"feature": "distance_to_vwap", "op": ">=", "value": -0.003},
                ],
                "13:30 lower-range recovery candidate targeting afternoon stabilisation instead of continuation-chase behaviour.",
            ),
            scan_time_et="13:30",
            rank_feature="mom_vs_spy",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 13:30 lower-range recovery A.",
        ),
        "Rule044_1330_lower_range_recovery_B": ReplayRuleSpec(
            rule_id="Rule044_1330_lower_range_recovery_B",
            display_name="Rule044 13:30 lower-range recovery B",
            status="validation_candidate",
            priority=45,
            sector=sector,
            rule=_rule(
                "tech_rule_044_1330_lower_range_recovery_B",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.01},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.05},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.30},
                    {"feature": "vwap_slope", "op": ">", "value": 0.0},
                    {"feature": "mom_vs_spy", "op": ">", "value": 0.0},
                    {"feature": "atr_reach", "op": "<=", "value": 10.0},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.35},
                ],
                "13:30 lower-range recovery candidate targeting afternoon stabilisation instead of continuation-chase behaviour.",
            ),
            scan_time_et="13:30",
            rank_feature="rs_leakfree",
            rank_direction="desc",
            max_signals_per_day=10,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 13:30 lower-range recovery B.",
        ),
        "Rule044_1330_lower_range_recovery_C": ReplayRuleSpec(
            rule_id="Rule044_1330_lower_range_recovery_C",
            display_name="Rule044 13:30 lower-range recovery C",
            status="validation_candidate",
            priority=45,
            sector=sector,
            rule=_rule(
                "tech_rule_044_1330_lower_range_recovery_C",
                sector,
                [
                    {"feature": "minutes_since_open", "op": "==", "value": 240},
                    {"feature": "open_to_scan_return", "op": "<=", "value": -0.008},
                    {"feature": "open_to_scan_return", "op": ">=", "value": -0.03},
                    {"feature": "intraday_range_position", "op": "<=", "value": 0.40},
                    {"feature": "distance_to_vwap", "op": ">", "value": -0.001},
                    {"feature": "range_expansion", "op": ">=", "value": 0.6},
                    {"feature": "sector_breadth_up", "op": ">=", "value": 0.45},
                    {"feature": "momentum", "op": ">", "value": 0.0005},
                ],
                "13:30 lower-range recovery candidate targeting afternoon stabilisation instead of continuation-chase behaviour.",
            ),
            scan_time_et="13:30",
            rank_feature="mom_vs_spy",
            rank_direction="desc",
            max_signals_per_day=5,
            tp_bps=100,
            sl_bps=200,
            default_slippage_bps=25,
            notes="Orthogonal validation family: 13:30 lower-range recovery C.",
        ),
    }


DEFAULT_RULE_IDS = ["rule009_refined_top10", "rule029_top3", "rule036B_cap10", "rule033_top20", "rule034_conservative_top20", "rule038_top15", "rule040_top15"]


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




# ---------------------------------------------------------------------------
# Capital-recycling replay helpers (v0.7.37)
# ---------------------------------------------------------------------------
def _hhmm_to_minutes(hhmm: str | None, default: int | None = None) -> int | None:
    """Convert HH:MM to minutes since midnight ET."""
    if not hhmm or str(hhmm).upper() == "NA":
        return default
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return default


def _minutes_to_hhmm(minutes: int) -> str:
    minutes = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _trade_sort_key(t: dict, *, key: str = "exit") -> tuple:
    d = str(t.get("signal_date") or "")
    hhmm = str(t.get("exit_time_et") if key == "exit" else t.get("entry_time_et") or "23:59")
    return (d, _hhmm_to_minutes(hhmm, 24 * 60 + 59) or 24 * 60 + 59, str(t.get("rule_id") or ""), str(t.get("symbol") or ""))


def _simulate_one_signal_at_entry(
    sig: pd.Series,
    spec: ReplayRuleSpec,
    bars: list[dict],
    *,
    entry_time_et: str,
    slippage_bps: float,
    queue_wait_minutes: int,
    portfolio_slot_id: int | None,
    portfolio_entry_reason: str,
) -> dict:
    """Simulate one candidate using a portfolio-scheduled entry time.

    Unlike the standard replay path, this can enter after the rule's normal
    scan+delay time when a capital slot frees up and a queued signal is still
    eligible. Entry price is the open of the scheduled entry minute bar.
    """
    signal_date = str(sig.get("date"))
    symbol = str(sig.get("symbol"))
    scan_price = _safe_float(sig.get("scan_price"), 0.0) or 0.0
    base = {
        "rule_id": spec.rule_id,
        "rule_display_name": spec.display_name,
        "symbol": symbol,
        "signal_date": signal_date,
        "signal_time_et": str(sig.get("scan_time_et")),
        "rule_rank": _clean(sig.get("rule_rank")),
        "rank_feature": spec.rank_feature,
        "rank_direction": spec.rank_direction,
        "rank_value": _clean(sig.get("rank_value")),
        "scan_price_ref": scan_price,
        "entry_time_et": entry_time_et,
        "scheduled_entry_time_et": entry_time_et,
        "portfolio_entry_reason": portfolio_entry_reason,
        "portfolio_slot_id": portfolio_slot_id,
        "queue_wait_minutes": int(max(0, queue_wait_minutes)),
        "entry_delay_minutes": spec.entry_delay_minutes,
        "tp_bps_used": spec.tp_bps,
        "sl_bps_used": spec.sl_bps,
        "slippage_bps": slippage_bps,
        "min_exit_minutes": spec.min_exit_minutes,
        **{f"sig_{k}": v for k, v in _signal_export(sig).items() if k not in {"symbol", "date"}},
    }
    if not bars:
        return {
            **base,
            "entry_ts_utc": "NA",
            "entry_price_source": "no_raw_bars",
            "entry_price": scan_price,
            "exit_price": scan_price,
            "exit_time_et": "NA",
            "exit_reason": "NO_DATA",
            "minutes_held": 0,
            "gross_return_bps": 0.0,
            "net_return_bps": 0.0,
        }

    entry_bar_ts = backtest._find_scan_bar_ts(bars, entry_time_et, signal_date_et=signal_date)
    if entry_bar_ts is None:
        return {
            **base,
            "entry_ts_utc": "NA",
            "entry_price_source": "no_entry_bar_at_scheduled_time",
            "entry_price": scan_price,
            "exit_price": scan_price,
            "exit_time_et": "NA",
            "exit_reason": "NO_DATA",
            "minutes_held": 0,
            "gross_return_bps": 0.0,
            "net_return_bps": 0.0,
        }

    entry_bar = backtest._find_bar_by_ts(bars, entry_bar_ts)
    if entry_bar is None:
        return {
            **base,
            "entry_ts_utc": "NA",
            "entry_price_source": "entry_bar_not_found",
            "entry_price": scan_price,
            "exit_price": scan_price,
            "exit_time_et": "NA",
            "exit_reason": "NO_DATA",
            "minutes_held": 0,
            "gross_return_bps": 0.0,
            "net_return_bps": 0.0,
        }

    entry_price = float(entry_bar["open"])
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
    return {
        **base,
        "entry_ts_utc": entry_bar_ts,
        "entry_price_source": "portfolio_scheduled_bar_open",
        "entry_price": entry_price,
        **res,
    }


def _capital_recycling_equity(trades: list[dict], *, capital_slots: int) -> tuple[list[dict], dict]:
    """Attach a simple closed-trade compounded equity curve.

    Sizing model:
      - account starts at 1.0
      - each opened position uses 1/capital_slots of equity at its open event
      - proceeds return to cash at close
      - no mark-to-market before close, no borrow/leverage beyond slot count
    """
    slots = max(1, int(capital_slots or 1))
    exits = [t for t in trades if t.get("exit_reason") != "NO_DATA"]
    if not exits:
        return [], {
            "starting_equity": 1.0,
            "ending_equity": 1.0,
            "compounded_return_pct": 0.0,
            "capital_multiple": 1.0,
            "max_drawdown_pct": 0.0,
            "capital_slots": slots,
            "position_fraction_per_slot": 1.0 / slots,
            "n_compounded_trades": 0,
        }

    # Open/close accounting. Store open notional by a stable synthetic id.
    events: list[tuple] = []
    for i, t in enumerate(exits):
        t["_equity_trade_id"] = i
        entry_m = _hhmm_to_minutes(str(t.get("entry_time_et")), 24 * 60) or 24 * 60
        exit_m = _hhmm_to_minutes(str(t.get("exit_time_et")), 24 * 60 + 59) or 24 * 60 + 59
        d = str(t.get("signal_date"))
        events.append((d, entry_m, 0, i, "open", t))
        events.append((d, exit_m, 1, i, "close", t))
    events.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

    cash = 1.0
    open_notional: dict[int, float] = {}
    high_water = 1.0
    max_dd = 0.0
    curve: list[dict] = []

    def current_equity() -> float:
        return cash + sum(open_notional.values())

    for d, minute, _ord, trade_id, event_type, t in events:
        if event_type == "open":
            equity_before = current_equity()
            notional = equity_before / slots
            # Guard against tiny rounding errors; slot scheduler should prevent over-allocation.
            notional = min(notional, max(cash, 0.0))
            cash -= notional
            open_notional[trade_id] = notional
            t["equity_before_open"] = float(equity_before)
            t["position_notional"] = float(notional)
            t["position_fraction_of_equity"] = float((notional / equity_before) if equity_before else 0.0)
            t["cash_after_open"] = float(cash)
            continue

        notional = open_notional.pop(trade_id, 0.0)
        r = float(t.get("net_return_bps") or 0.0) / 10_000.0
        proceeds = notional * (1.0 + r)
        cash += proceeds
        equity_after = current_equity()
        high_water = max(high_water, equity_after)
        dd = (equity_after / high_water - 1.0) if high_water else 0.0
        max_dd = min(max_dd, dd)
        t["position_proceeds"] = float(proceeds)
        t["equity_after_close"] = float(equity_after)
        t["cash_after_close"] = float(cash)
        t["compounded_trade_contribution_pct_of_start"] = float((proceeds - notional) * 100.0)
        curve.append({
            "date": d,
            "time_et": _minutes_to_hhmm(minute),
            "event": "close",
            "symbol": t.get("symbol"),
            "rule_id": t.get("rule_id"),
            "exit_reason": t.get("exit_reason"),
            "net_return_bps": float(t.get("net_return_bps") or 0.0),
            "position_notional": float(notional),
            "cash": float(cash),
            "open_notional": float(sum(open_notional.values())),
            "equity": float(equity_after),
            "drawdown_pct": float(dd * 100.0),
        })

    ending = cash + sum(open_notional.values())
    start_date = min(str(t.get("signal_date")) for t in exits)
    end_date = max(str(t.get("signal_date")) for t in exits)
    try:
        days = max(1, (datetime.fromisoformat(end_date) - datetime.fromisoformat(start_date)).days)
    except Exception:
        days = 365
    cagr = (ending ** (365.25 / days) - 1.0) if ending > 0 and days > 0 else 0.0
    return curve, {
        "starting_equity": 1.0,
        "ending_equity": float(ending),
        "capital_multiple": float(ending),
        "compounded_return_pct": float((ending - 1.0) * 100.0),
        "approx_cagr_pct": float(cagr * 100.0),
        "max_drawdown_pct": float(max_dd * 100.0),
        "capital_slots": slots,
        "position_fraction_per_slot": float(1.0 / slots),
        "n_compounded_trades": int(len(exits)),
        "first_trade_date": start_date,
        "last_trade_date": end_date,
        "calendar_days": int(days),
        "sizing_note": "Each opened position uses 1/capital_slots of account equity at open; proceeds return to cash at close.",
    }


def _simulate_capital_recycling(
    all_candidates: pd.DataFrame,
    specs_by_id: dict[str, ReplayRuleSpec],
    db_path: str,
    *,
    slippage_bps: float,
    capital_slots: int,
    max_trades_per_symbol_per_day: int,
    dedupe_policy: str,
    max_queue_wait_minutes: int,
    rule_ids_immediate_only: list[str] | None,
    just_in_time_backfill: bool,
    delete_raw_bars_after: bool,
) -> tuple[list[dict], pd.DataFrame, pd.DataFrame, dict, list[dict]]:
    """Chronological market replay with finite capital slots and intraday recycling.

    Signals arrive at rule scan_time + entry_delay. If no capital slot is free,
    they queue. Whenever a position closes, the next queued valid signal opens
    at that minute's bar open, provided it is still before its rule timestop.
    """
    empty = all_candidates.copy()
    if all_candidates.empty:
        return [], empty, empty, {"selected": 0, "rejected": 0, "capital_recycling_enabled": True}, []

    slots = max(1, int(capital_slots or 1))
    dedupe_policy = (dedupe_policy or "best_priority").lower()
    immediate_only_rules = {str(x) for x in (rule_ids_immediate_only or []) if str(x)}
    max_queue_wait_minutes = int(max_queue_wait_minutes or 0)
    df = all_candidates.copy()
    df["selected"] = False
    df["rejection_reason"] = ""
    df["arrival_time_et"] = df.apply(
        lambda r: backtest._hhmm_plus_minutes(str(r.get("scan_time_et")), int(r.get("entry_delay_minutes") or 0)),
        axis=1,
    )
    df["_arrival_minutes"] = df["arrival_time_et"].map(lambda v: _hhmm_to_minutes(str(v), 24 * 60) or 24 * 60)
    df = df.sort_values(["date", "_arrival_minutes", "rule_priority", "rule_rank", "symbol"], ascending=[True, True, True, True, True]).copy()

    bars_cache: dict[tuple[str, str], list[dict]] = {}
    selected_indices: list[int] = []
    rejected_reasons: dict[int, str] = {}
    trades: list[dict] = []
    queue_audit: list[dict] = []
    slot_trade_counts: Counter[int] = Counter()
    sym_day_counts: Counter[tuple[str, str]] = Counter()

    def get_bars(conn, symbol: str, date: str) -> list[dict]:
        key = (symbol, date)
        if key not in bars_cache:
            bars_cache[key] = backtest._ensure_raw_bars(conn, db_path, symbol, date, just_in_time_backfill)
        return bars_cache[key]

    def mark_rejected(idx: int, reason: str, row: pd.Series | None = None, event_time: str | None = None):
        rejected_reasons[idx] = reason
        if row is not None:
            queue_audit.append({
                "date": str(row.get("date")),
                "symbol": str(row.get("symbol")),
                "rule_id": str(row.get("rule_id")),
                "signal_time_et": str(row.get("scan_time_et")),
                "arrival_time_et": str(row.get("arrival_time_et")),
                "event_time_et": event_time,
                "action": "rejected",
                "reason": reason,
            })

    with storage.connect(db_path) as conn:
        for day, day_df in df.groupby("date", sort=True):
            day_str = str(day)
            available_slots = list(range(slots))
            open_heap: list[tuple[int, int, int]] = []  # (exit_minute, sequence, slot_id)
            waiting: deque[tuple[int, pd.Series, int]] = deque()
            sequence = 0

            def open_candidate(idx: int, row: pd.Series, slot_id: int, entry_minute: int, reason: str) -> bool:
                nonlocal sequence
                spec = specs_by_id[str(row["rule_id"])]
                timestop_min = _hhmm_to_minutes(spec.timestop_et, 15 * 60 + 50) or (15 * 60 + 50)
                if entry_minute >= timestop_min:
                    mark_rejected(idx, "queued_entry_after_or_at_timestop", row, _minutes_to_hhmm(entry_minute))
                    return False
                symbol = str(row["symbol"])
                date = str(row["date"])
                bars = get_bars(conn, symbol, date)
                arrival_min = int(row.get("_arrival_minutes") or entry_minute)
                trade = _simulate_one_signal_at_entry(
                    row,
                    spec,
                    bars,
                    entry_time_et=_minutes_to_hhmm(entry_minute),
                    slippage_bps=float(slippage_bps),
                    queue_wait_minutes=max(0, entry_minute - arrival_min),
                    portfolio_slot_id=slot_id,
                    portfolio_entry_reason=reason,
                )
                trades.append(trade)
                selected_indices.append(idx)
                slot_trade_counts[slot_id] += 1
                if trade.get("exit_reason") == "NO_DATA":
                    # No capital was actually tied up; make the slot immediately reusable.
                    available_slots.append(slot_id)
                    queue_audit.append({
                        "date": date,
                        "symbol": symbol,
                        "rule_id": spec.rule_id,
                        "signal_time_et": str(row.get("scan_time_et")),
                        "arrival_time_et": str(row.get("arrival_time_et")),
                        "event_time_et": _minutes_to_hhmm(entry_minute),
                        "action": "opened_no_data_slot_released",
                        "reason": reason,
                        "slot_id": slot_id,
                    })
                    return True
                exit_min = _hhmm_to_minutes(str(trade.get("exit_time_et")), entry_minute) or entry_minute
                sequence += 1
                heapq.heappush(open_heap, (exit_min, sequence, slot_id))
                queue_audit.append({
                    "date": date,
                    "symbol": symbol,
                    "rule_id": spec.rule_id,
                    "signal_time_et": str(row.get("scan_time_et")),
                    "arrival_time_et": str(row.get("arrival_time_et")),
                    "event_time_et": _minutes_to_hhmm(entry_minute),
                    "action": "opened",
                    "reason": reason,
                    "slot_id": slot_id,
                    "exit_time_et": trade.get("exit_time_et"),
                    "exit_reason": trade.get("exit_reason"),
                    "queue_wait_minutes": max(0, entry_minute - arrival_min),
                })
                return True

            def drain_queue_at(event_minute: int):
                # Free all positions that closed by this minute, then open queued candidates.
                while open_heap and open_heap[0][0] <= event_minute:
                    exit_min, _seq, slot_id = heapq.heappop(open_heap)
                    available_slots.append(slot_id)
                    # Immediately recycle newly available slots into waiting signals.
                    while waiting and available_slots:
                        q_idx, q_row, q_arrival = waiting.popleft()
                        q_entry_minute = max(exit_min, q_arrival)
                        q_wait = max(0, q_entry_minute - q_arrival)
                        if max_queue_wait_minutes and q_wait > max_queue_wait_minutes:
                            mark_rejected(q_idx, "queue_wait_exceeded_max_minutes", q_row, _minutes_to_hhmm(q_entry_minute))
                            continue
                        q_slot = available_slots.pop(0)
                        opened = open_candidate(q_idx, q_row, q_slot, q_entry_minute, "queued_after_slot_release")
                        if not opened:
                            # Slot remains free if candidate expired/rejected.
                            available_slots.append(q_slot)

            for idx, row in day_df.iterrows():
                arrival = int(row.get("_arrival_minutes") or 24 * 60)
                drain_queue_at(arrival)

                sym_key = (day_str, str(row.get("symbol")))
                if dedupe_policy != "allow_all" and max_trades_per_symbol_per_day and sym_day_counts[sym_key] >= max_trades_per_symbol_per_day:
                    mark_rejected(idx, "symbol_day_dedupe_or_cap", row, _minutes_to_hhmm(arrival))
                    continue
                if dedupe_policy != "allow_all" and max_trades_per_symbol_per_day:
                    sym_day_counts[sym_key] += 1

                if available_slots and not waiting:
                    slot_id = available_slots.pop(0)
                    opened = open_candidate(idx, row, slot_id, arrival, "at_signal_arrival")
                    if not opened:
                        available_slots.append(slot_id)
                else:
                    if str(row.get("rule_id")) in immediate_only_rules:
                        mark_rejected(idx, "rule_immediate_only_queue_blocked", row, _minutes_to_hhmm(arrival))
                        continue
                    waiting.append((idx, row, arrival))
                    queue_audit.append({
                        "date": day_str,
                        "symbol": str(row.get("symbol")),
                        "rule_id": str(row.get("rule_id")),
                        "signal_time_et": str(row.get("scan_time_et")),
                        "arrival_time_et": str(row.get("arrival_time_et")),
                        "event_time_et": _minutes_to_hhmm(arrival),
                        "action": "queued",
                        "reason": "all_capital_slots_in_use",
                    })

            # After last new signal of the day, continue recycling queued signals as slots close.
            while waiting and open_heap:
                next_exit = open_heap[0][0]
                drain_queue_at(next_exit)

            # Anything still waiting cannot be opened before all positions finished/expired.
            while waiting:
                q_idx, q_row, _q_arrival = waiting.popleft()
                mark_rejected(q_idx, "queue_not_opened_before_end_of_day", q_row, None)

            if delete_raw_bars_after:
                for symbol in sorted({str(x) for x in day_df["symbol"].dropna().unique()}):
                    storage.delete_raw_bars_for_day(conn, symbol, day_str, preserve_spy=True)

    if selected_indices:
        df.loc[selected_indices, "selected"] = True
    for idx, reason in rejected_reasons.items():
        df.loc[idx, "rejection_reason"] = reason
    selected = df[df["selected"]].drop(columns=["_arrival_minutes"], errors="ignore").copy()
    rejected = df[~df["selected"]].drop(columns=["_arrival_minutes"], errors="ignore").copy()
    reason_counts = rejected["rejection_reason"].replace("", "not_selected").value_counts().to_dict() if not rejected.empty else {}
    equity_curve, equity_summary = _capital_recycling_equity(trades, capital_slots=slots)
    diag = {
        "capital_recycling_enabled": True,
        "capital_slots": slots,
        "selected": int(len(selected)),
        "rejected": int(len(rejected)),
        "rejection_reasons": reason_counts,
        "slot_trade_counts": {str(k): int(v) for k, v in sorted(slot_trade_counts.items())},
        "queue_events": int(len(queue_audit)),
        "max_queue_wait_minutes": int(max_queue_wait_minutes or 0),
        "rule_ids_immediate_only": sorted(immediate_only_rules),
        "compounded_equity": equity_summary,
        "queue_audit_rows": queue_audit,
    }
    return trades, selected, rejected, diag, equity_curve

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
    capital_recycling_enabled: bool = False,
    capital_slots: int = 10,
    max_queue_wait_minutes: int = 0,
    rule_ids_immediate_only: list[str] | None = None,
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
    equity_curve: list[dict] = []
    queue_audit_rows: list[dict] = []
    if bool(capital_recycling_enabled) and include_virtual_trades:
        trades, selected, rejected, select_diag, equity_curve = _simulate_capital_recycling(
            all_candidates,
            specs_by_id,
            db_path,
            slippage_bps=float(slippage_bps),
            capital_slots=int(capital_slots or 10),
            max_trades_per_symbol_per_day=int(max_trades_per_symbol_per_day or 0),
            dedupe_policy=dedupe_policy,
            max_queue_wait_minutes=int(max_queue_wait_minutes or 0),
            rule_ids_immediate_only=list(rule_ids_immediate_only or []),
            just_in_time_backfill=bool(just_in_time_backfill),
            delete_raw_bars_after=bool(delete_raw_bars_after),
        )
        queue_audit_rows = select_diag.pop("queue_audit_rows", [])
    else:
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
        if include_virtual_trades:
            equity_curve, equity_summary = _capital_recycling_equity(
                trades,
                capital_slots=int(global_max_trades_per_day or capital_slots or 10),
            )
            select_diag["compounded_fixed_slot_reference"] = equity_summary

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
    if include_virtual_trades:
        if bool(capital_recycling_enabled):
            summary["capital_recycling"] = select_diag.get("compounded_equity", {})
        else:
            summary["compounded_fixed_slot_reference"] = select_diag.get("compounded_fixed_slot_reference", {})

    stem_prefix = "market_replay_capital_recycling" if bool(capital_recycling_enabled) else "market_replay"
    stem = f"{stem_prefix}_{start_date}_to_{end_date}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}".replace(":", "")
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
    _write_csv(work_dir / "market_replay_capital_recycling_equity_curve.csv", equity_curve)
    _write_csv(work_dir / "market_replay_capital_recycling_queue_audit.csv", queue_audit_rows)
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
            "capital_recycling_enabled": bool(capital_recycling_enabled),
            "capital_slots": int(capital_slots or 10),
            "max_queue_wait_minutes": int(max_queue_wait_minutes or 0),
            "rule_ids_immediate_only": list(rule_ids_immediate_only or []),
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
            "market_replay_capital_recycling_equity_curve.csv",
            "market_replay_capital_recycling_queue_audit.csv",
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
