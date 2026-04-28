# v0.7.35 Rule038 live-shadow candidate promotion

## Decision
Rule038 is promoted from replay candidate to promoted shadow candidate / live-shadow evidence collection.

This is not a live-trading promotion. It is a market-replay-to-live-shadow promotion only.

## Evidence basis
Standalone Rule038 v4 batch:
- 19/19 presets succeeded.
- Primary Rule038 top15 rs_leakfree remained strongly positive at 10, 15 and 20 bps slippage.

Six-rule market replay at 25 bps, cap10:
- Rules: Rule009, Rule029, Rule033, Rule034, Rule036B, Rule038.
- Total trades: 701.
- Win rate: 72.04%.
- Net PnL: +17,880.62 bps.
- Avg net: +25.51 bps/trade.
- Firing days: 134.
- Rule038 contribution after portfolio controls: 47 trades, 82.98% win rate, +2,645.66 bps, +56.29 bps/trade.

Increment versus current five-rule baseline:
- +47 trades.
- +2,645.66 net bps.
- +0.79 percentage-point win-rate improvement.
- +2.21 avg bps/trade improvement.
- +4 firing days.

## Caveats
- This remains historical replay evidence; collect live-shadow evidence before any capital use.
- Rule038 overlaps with Rule009 and Rule036B but still contributes positively after dedupe and global cap controls.
- Continue monitoring symbol concentration and worst-day behavior.

## Operator next step
Run the all-current-rules replay periodically and collect live-shadow evidence for Rule009, Rule029, Rule033, Rule034, Rule036B and Rule038.
