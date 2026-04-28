# v0.7.36 Capital-Recycling Market Replay

Purpose: estimate the economics of the intended automated scanner more honestly than the conservative cap10 replay.

## What changed

- Added `capital_recycling_enabled` to market replay requests.
- Added `capital_slots` to control maximum concurrent positions.
- In capital-recycling mode, signals arrive chronologically at scan time + entry delay.
- If all slots are in use, signals queue.
- When a position exits by TP, SL, or timestop, its slot becomes available.
- The next queued valid signal opens at the next available minute-bar open if still before that rule's timestop.
- Duplicate signals can be treated as separate investments with `dedupe_policy = allow_all` and `max_trades_per_symbol_per_day = 0`.
- Outputs now include a compounded equity summary and `market_replay_capital_recycling_equity_curve.csv`.

## Sizing model

The capital model starts at 1.0 equity.

Each opened position uses:

```text
1 / capital_slots
```

of account equity at its open event.

Proceeds return to cash when the position closes. This is still a research simulator, not an execution engine: it excludes borrow constraints, partial fills, taxes, commissions beyond slippage, and broker/order-routing limits.

## Ready-to-run presets

Located in `presets/market_replay/`:

- `market_replay_all_current_rules_capital_recycling_slots5_allow_duplicates_25bps.json`
- `market_replay_all_current_rules_capital_recycling_slots10_allow_duplicates_25bps.json`
- `market_replay_all_current_rules_capital_recycling_slots20_allow_duplicates_25bps.json`

Recommended first run:

```text
market_replay_all_current_rules_capital_recycling_slots10_allow_duplicates_25bps.json
```

## Important interpretation

This version answers a different question from the earlier cap10 replay:

Earlier cap10 replay:
> If up to 10 selected positions per day are allowed, does the rule portfolio work?

Capital-recycling replay:
> If a finite number of capital slots can be reused intraday after exits, how much compounded equity growth does the rule portfolio generate?

Do not compare net trade-bps directly to compounded return. Use the `capital_recycling` section in the manifest summary.
