# Rule036B market-replay integration — v0.7.32

Adds Rule036B cap10 as a market-replay registry candidate.

Rule logic:
- minutes_since_open == 240
- spy_vol < 0.0075
- spy_momentum >= -0.001
- momentum > 0.0008
- mom_vs_spy > 0
- distance_to_vwap > 0
- atr_reach <= 8.0
- rsi_14 >= 55
- range_tightness_30m >= 0.00253
- rank by momentum descending
- max_signals_per_day = 10
- TP100 / SL200 / entry delay 1 / min exit 1 / timestop 15:50

Standalone stress result uploaded from batch 2eae73ff:
- cap10 TP100/SL200 25 bps full span: 112 trades, +23.94 bps/trade
- Phase1: +24.71 bps/trade
- Forward: +23.29 bps/trade

Next required evidence before live-shadow: compare legacy active market replay versus all active + Rule036B using the included UI buttons or JSON request files.
