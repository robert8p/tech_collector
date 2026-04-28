# v0.7.34 Market replay UI fix

Fixes the operator error path where market-replay JSON files could be uploaded to the single-rule backtest section by mistake.

## Changes
- Adds market-replay JSON upload inside the Historic multi-rule market replay panel.
- Adds `run uploaded replay JSON`.
- Adds Rule038-only and current-five-plus-Rule038 market replay buttons.
- Keeps Rule038 as replay candidate only; it is not added to DEFAULT_RULE_IDS.
- Does not change rule logic, thresholds, live scheduler, or existing rule behavior.
