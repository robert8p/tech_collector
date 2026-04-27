# Rule033 promotion notes — v0.7.26

Rule033 adds a read-only event-day live-shadow monitor for a 13:30 Technology selloff-rebound setup with volume confirmation.

Primary logic:
- sector: Information Technology
- minutes_since_open == 240 / 13:30 ET
- spy_ret <= -0.0086881596898112
- momentum <= -0.00135528523765575
- atr_reach <= 13.0
- volume_acceleration >= 1.0
- rank by distance_to_day_low descending
- primary cap: top20 per day
- execution reference: entry delay 1 minute, min exit delay 1 minute, TP100 / SL200, timestop 15:50, 20 bps slippage

Evidence summary from Batch09:
- full span top20 at 20 bps slippage: 191 trades, 78.0% win rate, +41.98 bps/trade
- full span top20 at 15 bps slippage: 191 trades, 79.1% win rate, +45.58 bps/trade
- full span top20 at 10 bps slippage: 191 trades, 80.1% win rate, +49.83 bps/trade

Caveat:
Rule033 is not a generic daily scanner. It is an event-day selloff-rebound monitor and should be evaluated as such in live shadow.
