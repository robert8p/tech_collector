# v0.7.38 Rule040 live-shadow candidate promotion

Rule040 is promoted from candidate validation to `promoted_shadow_candidate` after clearing the full ladder:

- positive standalone path-dependent batch at 10/15/20 bps
- positive current-six market replay at 25 bps
- positive exact-settings capital-recycling replay at 25 bps with `allow_all`, `capital_slots=10`, `global_max_trades_per_day=0`, and `max_trades_per_symbol_per_day=0`

Key exact-settings capital-recycling result:

- baseline current six: 987 trades, +18,686.12 net bps, compounded return +20.28%, CAGR +9.76%, max drawdown -2.52%
- current six + Rule040: 1,025 trades, +19,758.67 net bps, compounded return +21.56%, CAGR +10.35%, max drawdown -2.52%

Promotion scope:

- include Rule040 in the replay default baseline (current seven)
- keep status as live-shadow candidate / promoted shadow candidate
- do not enable live trading
