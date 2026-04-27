"""Offline smoke checks for Rule029 + Rule033 live-shadow candidate logic.

These tests avoid API auth and network calls. They monkeypatch the scan-row
loader with synthetic rows and call the candidate-selection helpers directly.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tech_collector import api  # noqa: E402


def _check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        raise AssertionError(f"{label}: {detail}")


def test_rule029_filter_and_rank() -> None:
    rows = [
        # qualifies; lower atr_reach should rank first
        dict(date="2026-04-27", symbol="BBB", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, distance_to_vwap=-0.0010, vwap_slope=0.0002, atr_reach=7.0),
        dict(date="2026-04-27", symbol="AAA", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, distance_to_vwap=-0.0020, vwap_slope=0.0002, atr_reach=3.0),
        # fails: too far below VWAP
        dict(date="2026-04-27", symbol="CCC", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, distance_to_vwap=-0.0060, vwap_slope=0.0002, atr_reach=1.0),
        # fails: not below VWAP
        dict(date="2026-04-27", symbol="DDD", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, distance_to_vwap=0.0001, vwap_slope=0.0002, atr_reach=2.0),
        # fails: negative vwap slope
        dict(date="2026-04-27", symbol="EEE", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, distance_to_vwap=-0.0010, vwap_slope=-0.0001, atr_reach=2.0),
    ]
    old_loader = api.rule_tester.load_scan_rows_from_db
    try:
        api.rule_tester.load_scan_rows_from_db = lambda *a, **k: pd.DataFrame(rows)
        out = api._rule029_candidates_for_date("Information Technology", "2026-04-27")
    finally:
        api.rule_tester.load_scan_rows_from_db = old_loader
    _check("Rule029 keeps exactly qualifying rows", list(out["symbol"]) == ["AAA", "BBB"], f"symbols={list(out.get('symbol', []))}")
    _check("Rule029 ranks by lowest atr_reach", list(out["rule029_rank_by_atr_reach_low"]) == [1, 2])
    exported = api._rule029_shadow_export_rows(out, 1)
    _check("Rule029 export includes planned entry", exported[0]["planned_entry_time_et"] == "10:31")


def test_rule033_still_available_and_ranked() -> None:
    rows = [
        dict(date="2026-04-27", symbol="LOWER", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=10.0, volume_acceleration=1.2, distance_to_day_low=-0.05),
        dict(date="2026-04-27", symbol="HIGHER", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=10.0, volume_acceleration=1.2, distance_to_day_low=0.03),
        dict(date="2026-04-27", symbol="FAILVOL", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=10.0, volume_acceleration=0.5, distance_to_day_low=0.10),
    ]
    old_loader = api.rule_tester.load_scan_rows_from_db
    try:
        api.rule_tester.load_scan_rows_from_db = lambda *a, **k: pd.DataFrame(rows)
        out = api._rule033_candidates_for_date("Information Technology", "2026-04-27")
    finally:
        api.rule_tester.load_scan_rows_from_db = old_loader
    _check("Rule033 helper still exists", callable(api._rule033_candidates_for_date))
    _check("Rule033 keeps qualifying rows and ranks desc by distance_to_day_low", list(out["symbol"]) == ["HIGHER", "LOWER"], f"symbols={list(out.get('symbol', []))}")


def main() -> int:
    print("SMOKE: Rule029 + Rule033 live-shadow candidate logic")
    print("=" * 64)
    test_rule029_filter_and_rank()
    test_rule033_still_available_and_ranked()
    print("All Rule029/Rule033 smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
