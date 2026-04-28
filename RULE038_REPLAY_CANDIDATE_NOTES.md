# Rule038 Replay Candidate Notes — v0.7.33

This tranche registers Rule038 as a **market-replay candidate only**. It does not make Rule038 part of the default active replay set and does not alter existing Rule009, Rule029, Rule033, Rule034, or Rule036B definitions.

## Rule038 logic

- scan time: 13:30 ET (`minutes_since_open == 240`)
- `spy_vol >= 0.004`
- `spy_momentum >= 0`
- `gap_pct <= 0`
- `range_expansion >= 1.0`
- `atr_reach <= 8.0`
- `sector_breadth_up >= 0.4`
- rank: `rs_leakfree` descending
- cap: top15
- TP/SL: 100/200 bps
- default replay slippage: 25 bps

## Why it is included

Standalone app batch `rule038_discovery_batch_v4.zip` ran 19/19 presets successfully. The primary Rule038 top15 rs_leakfree variant produced:

- 124 trades
- 80.6% win rate
- +49.98 bps/trade at 10 bps slippage
- +46.95 bps/trade at 15 bps slippage
- +43.29 bps/trade at 20 bps slippage

This justifies a combined market-replay test against the current five-rule set, not live use.

## Presets added

- `market_replay_current_five_25bps_cap10.json`
- `market_replay_rule038_only_25bps_cap10.json`
- `market_replay_current_five_plus_rule038_25bps_cap10.json`
- `market_replay_rule036B_plus_rule038_25bps_cap10.json`

## Promotion gate

Rule038 should advance only if the current-five-plus-Rule038 replay improves the current-five replay after dedupe/global caps at 25 bps slippage, without unacceptable symbol/quarter/day concentration.
