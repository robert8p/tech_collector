"""Offline smoke checks for Rule034 conservative live-shadow candidate logic."""
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


def test_rule034_filter_and_rank() -> None:
    rows = [
        dict(date="2026-04-27", symbol="KEEP2", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=9.0, volume_acceleration=1.2, gap_filled=0, distance_to_day_low=0.02),
        dict(date="2026-04-27", symbol="KEEP1", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=8.0, volume_acceleration=1.2, gap_filled=0, distance_to_day_low=0.08),
        # fails: Rule034 conservative ATR reach cap
        dict(date="2026-04-27", symbol="FAILATR", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=11.0, volume_acceleration=1.2, gap_filled=0, distance_to_day_low=0.10),
        # fails: completed gap fill
        dict(date="2026-04-27", symbol="FAILGAP", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=8.0, volume_acceleration=1.2, gap_filled=1, distance_to_day_low=0.12),
        # fails: not enough volume acceleration
        dict(date="2026-04-27", symbol="FAILVOL", sector="Information Technology", scan_time_et="13:30", minutes_since_open=240,
             scan_price=100, spy_ret=-0.01, momentum=-0.002, atr_reach=8.0, volume_acceleration=0.9, gap_filled=0, distance_to_day_low=0.15),
    ]
    old_loader = api.rule_tester.load_scan_rows_from_db
    try:
        api.rule_tester.load_scan_rows_from_db = lambda *a, **k: pd.DataFrame(rows)
        out = api._rule034_candidates_for_date("Information Technology", "2026-04-27")
    finally:
        api.rule_tester.load_scan_rows_from_db = old_loader
    _check("Rule034 helper exists", callable(api._rule034_candidates_for_date))
    _check("Rule034 keeps exactly qualifying rows", list(out["symbol"]) == ["KEEP1", "KEEP2"], f"symbols={list(out.get('symbol', []))}")
    _check("Rule034 ranks desc by distance_to_day_low", list(out["rule034_rank_by_distance_to_day_low"]) == [1, 2])
    exported = api._rule034_shadow_export_rows(out, 1)
    _check("Rule034 export includes planned entry", exported[0]["planned_entry_time_et"] == "13:31")


def main() -> int:
    print("SMOKE: Rule034 conservative live-shadow candidate logic")
    print("=" * 64)
    test_rule034_filter_and_rank()
    print("All Rule034 smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
