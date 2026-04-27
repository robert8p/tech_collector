"""Offline smoke check for Rule009 refined live-shadow candidate logic."""
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


def test_rule009_refined_filter_and_rank() -> None:
    rows = [
        # qualifies
        dict(date="2026-04-27", symbol="AAA", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, momentum=0.010, gap_pct=-0.001, range_expansion=1.2),
        dict(date="2026-04-27", symbol="BBB", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, momentum=0.020, gap_pct=0.0, range_expansion=1.0),
        # fails refined gap filter
        dict(date="2026-04-27", symbol="GAPUP", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, momentum=0.030, gap_pct=0.002, range_expansion=1.4),
        # fails refined range expansion filter
        dict(date="2026-04-27", symbol="TIGHT", sector="Information Technology", scan_time_et="10:30", minutes_since_open=60,
             scan_price=100, spy_vol=0.006, spy_momentum=0.001, momentum=0.030, gap_pct=-0.001, range_expansion=0.9),
    ]
    old_loader = api.rule_tester.load_scan_rows_from_db
    try:
        api.rule_tester.load_scan_rows_from_db = lambda *a, **k: pd.DataFrame(rows)
        out = api._rule009_candidates_for_date("Information Technology", "2026-04-27")
    finally:
        api.rule_tester.load_scan_rows_from_db = old_loader
    _check("Rule009 refined keeps only gap/range qualified rows", list(out["symbol"]) == ["BBB", "AAA"], f"symbols={list(out.get('symbol', []))}")
    _check("Rule009 refined ranks by momentum descending", list(out["rule009_rank_by_momentum"]) == [1, 2])
    exported = api._shadow_export_rows(out, 1)
    _check("Rule009 export includes range_expansion", "range_expansion" in exported[0])


def main() -> int:
    print("SMOKE: Rule009 refined live-shadow candidate logic")
    print("=" * 64)
    test_rule009_refined_filter_and_rank()
    print("All Rule009 refined smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
