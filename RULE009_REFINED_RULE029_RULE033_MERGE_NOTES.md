# v0.7.28 merge notes — Rule009 refined + Rule029 + Rule033

Base lineage:
- Started from `tech_collector_v0_7_27_RULE033_PLUS_RULE029_LIVE_SHADOW.zip` so Rule029 and Rule033 live-shadow support remained intact.
- Merged the updated Rule009 refined logic from `tech_collector_v0_7_26_RULE009_REFINED_GAP_RANGE_FILTER.zip`.

Rule009 refined changes included:
- Rule009 shadow monitor now requires `gap_pct <= 0` and `range_expansion >= 1.0` in addition to `scan_time_et == 10:30`, `spy_vol >= 0.005`, and `spy_momentum >= 0`.
- Rule009 shadow exports include `range_expansion`.
- Rule009 strategy id/manifest wording updated to the refined gap/range variant.
- Dashboard Rule009 preset buttons now load the refined predicates.
- Added `presets/rule009_refined/*` from the uploaded refined Rule009 package.

Preserved:
- Rule029 live-shadow endpoint and UI: `/rule029/shadow/run`.
- Rule033 live-shadow endpoint and UI: `/rule033/shadow/run`.
- Rule009 live-shadow endpoint path remains `/rule009/shadow/run`, but its logic is now refined.

All rule-shadow endpoints remain read-only. No live trading is enabled.
