# Rule034 promotion notes — v0.7.27

Rule034 is the conservative Rule033 miss-exclusion variant.

Promoted logic:
- sector == Information Technology
- scan_time_et == 13:30 / minutes_since_open == 240
- spy_ret <= -0.0086881596898112
- momentum <= -0.00135528523765575
- atr_reach <= 10
- volume_acceleration >= 1.0
- gap_filled == 0
- rank by distance_to_day_low descending
- primary cap: top20 per day
- execution reference: entry delay 1 minute, min exit delay 1 minute, TP100 / SL200, 20 bps slippage, timestop 15:50, standard filter mode.

Evidence summary from Batch11:
- Full span top20, 20 bps slippage: 90 trades, 88.9% win rate, +61.5 bps/trade.
- Phase1 top20, 20 bps slippage: 49 trades, 91.8% win rate, +70.9 bps/trade.
- Forward top20, 20 bps slippage: 41 trades, 85.4% win rate, +50.4 bps/trade.

Operating note:
Rule034 should not replace Rule033 immediately. Use Rule033 as the broader event monitor and Rule034 as the conservative quality-filtered view. Both are read-only decision-support monitors; no auto-trading.
