# Rule029 Final Decision — Technology Rule Discovery

## Decision

Rule029 is live-shadow ready, not production-proven.

Primary live-shadow profile:
- Rule: Rule029 top-3 ATR-low
- Scan time: 10:30 ET
- TP/SL: 100 / 200 bps
- Evaluation slippage: 25 bps
- Ranking: atr_reach ascending
- Max signals/day: 3

Why TP100/SL200 is primary:
- It is positive in both chronological periods at 25 bps.
- It is more balanced than TP125/SL250.
- It has lower worst-day exposure than TP125/SL250.
- It is cleaner for first live-shadow.

Secondary watch-only profile:
- Rule029 top-10 ATR-low, TP125/SL250, 25 bps.
- Strong historical profile but wider event-day exposure.
- Track it, but do not promote ahead of top-3 without live evidence.

Blocked:
- Rule030 top-10 volume acceleration remains blocked from primary live-shadow because of bad-day concentration.
- Rule030 top-5 remains watchlist only.
- 13:30 family remains rejected.


## v0.7.27 app implementation

This build starts from v0.7.26 Rule033 live-shadow and adds Rule029 alongside Rule009 and Rule033. It does not remove or weaken Rule033.

Added endpoint: `POST /rule029/shadow/run`.

Added UI controls: Rule029 backtest preset loader and Rule029 live-shadow evidence-pack button.

Evidence pack contents include Rule029 all/top3/top10 candidates, Rule009 same-date comparison candidates, date-symbol overlaps, optional path-dependent virtual trades, and a manifest with version/config.
