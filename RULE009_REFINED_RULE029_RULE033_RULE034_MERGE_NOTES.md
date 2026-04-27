# v0.7.29 merge notes — Rule009 refined + Rule029 + Rule033 + Rule034

This build uses the v0.7.28 combined app as the base and adds the v0.7.27 Rule034 conservative monitor changes.

Included rule tracks:

- Rule009 refined gap/range live-shadow/backtest presets.
- Rule029 live-shadow candidate and candidate presets.
- Rule033 live-shadow monitor and promoted presets.
- Rule034 conservative monitor and promoted presets.

Rule034 logic:

- `minutes_since_open == 240` / 13:30 ET
- `spy_ret <= -0.0086881596898112`
- `momentum <= -0.00135528523765575`
- `atr_reach <= 10.0`
- `volume_acceleration >= 1.0`
- `gap_filled == 0`
- rank by `distance_to_day_low` descending
- primary monitor: top 20/day

Operational endpoints expected:

- `/rule009/shadow/run`
- `/rule029/shadow/run`
- `/rule033/shadow/run`
- `/rule034/shadow/run`

No auto-trading is added. These are monitoring/backtest/evidence-pack workflows only.
