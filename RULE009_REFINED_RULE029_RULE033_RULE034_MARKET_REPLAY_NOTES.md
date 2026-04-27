# v0.7.30 market replay tranche

This build adds a historic multi-rule market replay layer on top of v0.7.29.

Preserved:
- Rule009 refined gap/range live-shadow monitor
- Rule029 live-shadow monitor
- Rule033 live-shadow monitor
- Rule034 conservative live-shadow monitor

Added:
- `tech_collector/market_replay.py`
- `GET /market-replay/rules`
- `POST /market-replay/run` async job endpoint
- `POST /market-replay/run-sync` short-range synchronous endpoint
- Dashboard section: Historic multi-rule market replay

Default replay behaviour:
- Rules: Rule009 refined top10, Rule029 top3, Rule033 top20, Rule034 conservative top20
- Slippage: 25 bps
- Entry delay: 1 minute
- Minimum exit delay: 1 minute
- Timestop: 15:50 ET
- Max trades per symbol per day: 1
- Global max trades per day: 10
- Dedupe policy: best_priority
- Default priority: Rule029 > Rule009 refined > Rule034 > Rule033

Evidence pack outputs:
- `market_replay_manifest.json`
- `market_replay_daily_summary.csv`
- `market_replay_all_signals.csv`
- `market_replay_selected_signals.csv`
- `market_replay_selected_trades.csv`
- `market_replay_rejected_signals.csv`
- `market_replay_rule_summary.csv`
- `market_replay_overlap_matrix.csv`
- `market_replay_symbol_concentration.csv`

Purpose:
This feature tests whether the combined future scanner improves on standalone rules after shared exposure controls, deduplication, overlap, and bad-day concentration are accounted for.
