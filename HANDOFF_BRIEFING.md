# Tech Collector — Handoff Briefing (as of v0.7.7)

**For:** new Claude chat continuing this work with Rob
**Purpose:** preserve complete context so development continues seamlessly
**Last session ended:** v0.7.7 packaged and delivered, awaiting deployment

---

## Who Rob is and how he works

Rob is a solo developer building personal discretionary trading tools, primarily in Python. His workflow pairs ChatGPT (main dev partner) with Claude (independent external reviewer and occasional direct implementer). He feeds outputs between the two.

**Rob's explicit working style directives — do not forget these:**
- **"We only do things properly."** No half-measures. No hacks.
- **"This trial and error is not acceptable."** Ship fixes that have been reasoned through, not iterated through deploys. Verify locally before packaging.
- **No manual work — automate as much as possible.** If something can be tested in smoke, add the test. If something can be a one-click UI operation, make it one.
- **Thorough review before next artifact.** When he pushes back with this phrase, stop shipping and investigate properly.
- **Blunt evidence-driven assessment.** He's comfortable with being told the strategy is weaker than hoped. Don't hedge.

**Skepticism vectors he already has, unprompted:**
- Infrastructure-over-signal patterns (diagnostic code growing faster than the core)
- Data contamination masking real behaviour
- Process progress mistaken for product progress
- Stories written for results that don't survive cleaning

**When Rob says something is broken:** trust him, investigate, don't defend. Multiple times this session I wasted his time with reassurances; the pattern now is always "open browser devtools / check logs / examine data, don't speculate."

---

## The project: tech-collector

A FastAPI web application deployed on Render that:
1. Backfills 1-minute bar data from Alpaca for S&P 500 sector-restricted universes
2. Computes scan-time features and writes `research_rows` (one per symbol/date/scan_time)
3. Lets Rob define filter rules (predicate lists over features) and tests them
4. Runs backtests that simulate trades based on rule-matched signals and stores results
5. Audits/repairs buggy runs (added in v0.7.1 after a critical phantom-TIME bug was caught)

**Sector focus:** Primarily Information Technology (~75 symbols, 2 years of data Apr 2024 — Apr 2026). Also exists for Financials and R2K but those aren't our focus right now.

**Local workspace:** `/home/claude/tech_collector_work/`
**Deployed on:** Render Pro Plus
**DB:** SQLite at `/var/data/tech_collector.db` on Render, `config.DB_PATH` locally

---

## The bug that shaped recent sessions (critical context)

In backtest engine v0.7.0, `_simulate_trade` had a **phantom-TIME-exit bug**. When a trade's bar iteration ended without a TP/SL match, the fallback took the last bar's close as a "TIME" exit *without re-scanning for missed crossings*. On big intraday moves, this produced stored returns like +3037 bps (30%) that were marked as TIME, well outside the TP/SL bounds.

**The specific production case we uncovered:** IT on 2026-02-03, entry 10:30 ET at $157.165, stock rallied to $201.84 (+30%) by 15:30, stored as TIME/+3037 bps. This was impossible — the stock crossed TP (+75 bps) within minutes of entry.

**Impact of the bug on the C-scaled baseline (run_uuid `51db4f14`):**
- 1,760 trades; 586 reason-mismatches (33%); 481 suspect TIME exits
- **Stored total P&L: +44,641 bps. Reference-computed total: +16,649 bps. Contamination: -31,923 bps (78% of baseline was phantom.)**
- Zero invariant violations after fix
- Transitions: TP→TP 903, SL→SL 263, TIME→TP 390, TIME→SL 149, TIME→TIME 8, TIME→NO_DATA 38, TP↔SL swaps 9

**The fix (v0.7.1+):**
- Replaced approximate `_utc_hour_to_et` with `zoneinfo`-backed conversion (handles DST)
- End-of-bars fallback scans the full bar list for TP/SL crossings before declaring TIME
- Added invariant: TIME-exit gross must be within `[-sl_level, +tp_level]`. Violation raises `AssertionError` loudly.
- Built audit/repair pipeline so existing stored runs could be verified against the canonical reference simulator.

**Status today:** the bug is fixed in the engine, the audit has run successfully on the baseline, and the results above are the clean numbers to work with.

---

## The clean baseline (what we actually know works)

**C-scaled strategy**, ~2 years of IT signal data, corrected outcomes:

| metric | value |
|---|---|
| total P&L | +12,613 bps |
| avg per trade | +7.3 bps |
| win rate | 75.5% |
| trades usable | 1,722 of 1,760 (38 NO_DATA excluded) |

**Per-branch (C-scaled is a two-branch strategy):**
- `gap_open` (TP75/SL100 at position_size=1.0): n=827, +8,577 bps, avg +10.4 bps/trade
- `gap_filled_half` (TP50/SL150 at position_size=0.5): n=895, +4,036 bps, avg +4.5 bps/trade

**Per-quarter (reveals significant instability):**
| quarter | period | total | avg/trade | notes |
|---|---|---|---|---|
| Q1 | Apr 2024 – Feb 2025 | +3,158 | +7.2 | ok |
| Q2 | Feb – Apr 2025 | +6,270 | +14.9 | strong |
| Q3 | Apr – Oct 2025 | +217 | +0.5 | **near-zero for 6 months** |
| Q4 | Feb – Apr 2026 | +2,967 | +6.9 | recovered |

**Branch-level fragility:** `gap_open` is negative in Q3 (-201 bps); `gap_filled_half` is negative in Q4 (-824 bps). Neither branch is consistently positive across all 4 quarters. Aggregate edge comes from decorrelated branches, not two independently robust edges.

**Sobering implications:**
- Year 2 edge (+3.5 bps/trade) is materially weaker than Year 1 (+11.3 bps/trade)
- Most prior "predictive" filters from buggy-data analysis collapsed when outcomes were corrected — they were detecting the phantom-TIME pattern itself, not real edge
- Published backtest numbers from earlier versions are inflated by the phantom bug

---

## Rule analysis — what survived on clean data

**Filters that turned out to be artifacts:**
- `dist_to_prev_close_bps > 100` (the old "best filter"): drops 37% of train, reduces P&L both train and test
- `gap_pct > 0`: reduces P&L both
- `intraday_range_position > 0.8`: marginal drag
- `new_highs_in_sector >= 5`: near-neutral

**Filters that survived on clean data:**

| filter | train delta | test delta | notes |
|---|---|---|---|
| `spy_momentum <= 0` | -1,044 | +1,437 | regime-specific |
| `spy_mom<=0 AND dist_prev_close<0` | -60 | +2,063 | strongest combo |
| Exclude MPWR | +332 | — | one persistently-negative symbol (neg both years) |
| Exclude MPWR+STX+CIEN | +1,300 | — | STX/CIEN one-year negative each |

**Recommended filter stack (stable across 3 of 4 quarters):**
`KEEP if ((symbol != MPWR) AND (spy_momentum > 0 OR dist_to_prev_close_bps >= 0))`

- Drops 14.3% of trades
- P&L improvement: +12,613 → +14,497 bps (+15%)
- Per-quarter delta: Q1 +306, Q2 -457, Q3 +1,663, Q4 +372

**Alternative (regime-only, simpler):**
Just `KEEP if (spy_momentum > 0 OR dist_to_prev_close_bps >= 0)`.
- Drops 12.5%
- P&L: +12,613 → +14,616 bps (+16%)

Slightly better than the combined version because symbol-exclusion overlaps with regime-exclusion (MPWR's bad trades mostly occur in bad regimes too).

---

## 1/N compounded returns (what Rob asked about last)

Using the **Filter A+B** (MPWR exclusion + regime filter) on corrected data:

| N (concurrent slots) | per-trade size | 2-year compounded | annualized | max drawdown |
|---|---|---|---|---|
| 5 | 20% capital | +33.5% | +15.5% | -4.3% |
| 10 | 10% capital | +15.6% | +7.5% | -2.2% |
| 15 | 6.7% capital | +10.1% | +4.9% | -1.5% |
| 20 | 5% capital | +7.5% | +3.7% | -1.1% |

**Baseline (no filter, corrected):**
| N | 2-year | annualized |
|---|---|---|
| 5 | +28.5% | +13.4% |
| 10 | +13.4% | +6.5% |
| 20 | +6.5% | +3.2% |

**Trade concurrency distribution:** median 2/day, mean 4.4/day, p95 13/day, **max 146** (April 7 2025 tariff shock — the rule fired on basically the entire IT sector simultaneously).

**Known tail-risk issue:** the April 7 outlier means even N=20 sizing sees ~7× leverage on tariff day. **A daily signal cap is needed** (e.g. drop any signal beyond the 10th of the day, prioritised by some criterion). Not yet implemented.

**Caveat on compounding math:** trades on the same day don't actually compound — they all draw from the same equity snapshot. The per-trade compounding I computed slightly overstates reality. A daily-compounded calculation would give ~90-95% of those numbers. Still positive, still meaningful.

---

## What's in v0.7.7 (the current zip)

### 1. `regime_ok` feature
New column in `research_rows`:
- Formula: `int((spy_momentum > 0) or (dist_to_prev_close_bps >= 0))`
- Handles partial-input cases (only one of the two populated)
- Computed in `feature_computer.compute_range` Pass-2
- Migration added to `storage.init_schema` (existing DBs pick up the column on next startup)
- Rule-usable via predicate `{"feature": "regime_ok", "op": "==", "value": 1}`

**IMPORTANT:** the new column is NULL for historical rows until recompute runs. Rob needs to recompute the IT sector over the full range before the filter is testable end-to-end.

### 2. Timestop default 15:30 → 15:50
- All three locations updated: `BacktestConfig`, API Pydantic model, UI input
- `CUTOFF_TIME_ET=15:30` for research rows is UNCHANGED (moving it would invalidate all historical research data)

### 3. No-timestop mode
- `BacktestConfig.timestop_et: str | None`
- UI: empty input → `null` → timestop disabled
- Empty string `""` also treated as disabled (forward/backward compat)
- Engine: `_simulate_trade` and `_simulate_trade_reference` both honour the `timestop_enabled` flag
- If neither TP nor SL hit and bars run out: legitimate TIME fallback at last close, invariant enforced
- Storage: `None` coerced to `""` at write time (`backtest_runs.timestop_et` is `TEXT NOT NULL` on the live DB and migrating that constraint is risky)

### Test coverage
**205 + 29 + 16 = 250/250 passing** before packaging.
- `tests/smoke_sectors.py` (205 checks): end-to-end compute, schema migration, backtest engine paths, storage, rule evaluation
- `tests/smoke_backtest_audit.py` (29 checks): reference simulator, invariant enforcement, DST transitions, audit_run/repair_run E2E, no-timestop cases, 15:50 timestop behaviour
- `tests/smoke_jobs.py` (16 checks): SQLite-backed JobRegistry, cross-worker visibility, error persistence

---

## Bug/incident history this session (context for cautious deploys)

**v0.7.1** — initial phantom-TIME fix. Shipped with endpoint crashes in prod (used sqlite3.connect directly instead of storage.connect; storage functions returned raw tuples that failed `dict(row)`).

**v0.7.2** — fixed storage-shape bugs. Also caught `storage.insert_backtest_run` didn't exist (correct: `record_backtest_run`), wrong field names (`spy_regime_min` vs `spy_regime_filter`, etc.), `collector.backfill_range` → `collect_range`. Added 22-test smoke including end-to-end DB tests. Rob said "trial and error is not acceptable." — valid pushback, lesson applied.

**v0.7.3** — UI error messages improved to show server response body instead of generic "Unexpected token '<'".

**v0.7.4** — discovered audit endpoint was 502'ing because 1,760 trades took minutes and the sync HTTP handler exhausted Render's worker timeout. Made audit/repair async via jobs.registry. Audit itself worked fine on server (93 seconds); UI couldn't see results.

**v0.7.5** — added tolerance for transient 404s during poll; better error diagnostics.

**v0.7.6** — root-caused the UI polls as multi-worker issue. In-memory `JobRegistry` stored jobs in one worker's Python dict; polls routing to a different worker saw 404. **Rewrote `jobs.py` as SQLite-backed** (`bt_jobs` table). This was the actual proper fix; everything before was working around symptoms.

**v0.7.7** — regime_ok + 15:50 timestop + no-timestop mode (current zip).

**Lesson for future work:** the "transient 404 tolerance" in v0.7.5 is still in the UI. It's belt-and-braces and not harmful, but the underlying fix is the SQLite jobs table. If there's ever a JobRegistry issue again, start by checking `/var/data/tech_collector.db` bt_jobs table, not the UI.

---

## What Rob should do after deploying v0.7.7

1. **Verify version shows `0.7.7` in `/info`**
2. **Recompute the IT sector** over the full range (Apr 2024 → present) so `regime_ok` populates for historical rows
3. **Run 3 backtests** to validate the filter stack:
   - (a) Baseline: existing rule, `timestop_et=15:50` (replicate the corrected baseline cleanly)
   - (b) +Regime: new rule with `regime_ok == 1` predicate added, `timestop_et=15:50`
   - (c) +Regime +No-timestop: same rule, leave timestop empty

**Expected results (based on clean-data analysis):**
- (a) should produce ~+12,600 bps (matching the corrected baseline)
- (b) should produce ~+14,600 bps (+2,000 gain, drop ~12.5% of trades)
- (c) is speculative — hypothesis is slightly higher avg per trade (fewer forced flats cap upside) with somewhat higher variance. The invariant tests guarantee no phantom blow-ups.

4. **After validating: address the daily signal cap tail-risk.** The April 7 2025 behaviour (146 simultaneous signals) is a real portfolio-level risk regardless of filter. Needs a daily-cap feature: drop signals beyond the Nth of a given day, prioritised by some criterion (most recent? highest-momentum? smallest gap? TBD).

---

## Open questions / design decisions pending

**A. Daily signal cap implementation.** Where to enforce it: (1) in `run_backtest` as a BacktestConfig field (`max_signals_per_day: int | None`), (2) as a post-hoc filter in the rule engine, or (3) as a derived feature like `rank_in_day` computed in feature_computer? Option 1 is simplest and keeps backtest decisions explicit. Option 3 is cleanest semantically (rule predicate `rank_in_day <= 10`). Recommend option 1 for pragmatism.

**B. 38 NO_DATA trades.** Stored run has 38 trades the reference can't resolve (all have `bars_seen_post_entry = 0`). Concentrated on specific dates (2025-03-06 has 4, 2025-02-21 has 3, 2025-02-28 has 3, 2025-03-04 has 3). Likely partial bar-data issues. Could be recovered by re-backfilling those specific dates. Minor impact (2% of dataset); low priority but worth doing eventually.

**C. Q3 2025 edge collapse.** Six months (Apr–Oct 2025) with +0.5 bps/trade avg. Nothing obvious in features predicts it. Could be a regime this strategy doesn't work in (low-VIX drifting tape? sector rotation out of tech?). If this represents a real structural change, post-deploy monitoring needs to catch similar conditions quickly.

**D. Rule variants haven't been re-validated on clean data.** Earlier sessions compared C-scaled vs C-simple vs 75bps-alone vs 50bps-alone using buggy data. All those rankings are suspect. Should re-run on corrected data before making deployment decisions.

**E. Backtest on repaired run vs. new run.** v0.7.7 repair is available, but Rob hasn't clicked it yet. The corrected trades exist only in my analysis (`/home/claude/corrected_trades_51db4f14.csv` locally). Running repair makes them durable in the production DB. Recommend doing this so the corrected baseline is the "official" reference for future comparisons.

---

## Artifacts on disk (next-session environment)

### In `/home/claude/`
- `corrected_trades_51db4f14.csv` — the clean trade dataset from the audit (1,760 rows; `ref_*` columns are the canonical outcomes; use for any further pattern analysis)
- `audit_pack1/full.json` — 1,145KB audit report with per-trade records (identical to audit_pack2)
- `audit_pack1/summary.json` — aggregate audit summary
- `tech_rows/tech_scan_rows.csv` — full IT research_rows (feature-engineered) for joining with trades

### In `/mnt/user-data/uploads/`
- `backtest_audit_51db4f14_20260424T002756Z.zip` — first audit pack from production
- `backtest_audit_51db4f14_20260424T003233Z.zip` — second audit pack (identical)
- `backtest_51db4f14_trades.csv` — original (contaminated) trades CSV
- `backtest_5d21a81a_trades.csv` — original trades CSV for the `dist_prev_close<=100` variant (contaminated)
- `tech_scan_rows_information-technology_2024-04-22_to_2026-04-17.zip/.parquet` — 67MB research_rows dump
- `tech_research_export_*` — monthly exports (likely less useful than the full parquet)

### Code workspace in `/home/claude/tech_collector_work/`
- Current local version of the whole app, matches v0.7.7 zip contents
- Run `python3 -m tests.smoke_sectors`, `tests.smoke_backtest_audit`, `tests.smoke_jobs` to verify state

### Transcript pointers
- `/mnt/transcripts/2026-04-23-22-52-08-tech-collector-v07-backtest.txt`
- `/mnt/transcripts/2026-04-23-23-44-07-tech-collector-v07-option-c-and-bug.txt`
- `/mnt/transcripts/2026-04-24-00-44-27-tech-collector-v075-audit-repair.txt`
- `/mnt/transcripts/journal.txt` — catalog of all prior transcripts

---

## Production DB state (as of last session)

- 20+ backtest runs in `backtest_runs`, most from before the phantom-TIME fix → **their P&L numbers should not be trusted without audit+repair**
- `51db4f14` is the canonical C-scaled baseline; audit evidence pack exists at `/packs/backtest_audit_51db4f14_20260424T002754Z.zip`
- `research_rows` table populated for IT, 2024-04-22 through approximately 2026-04-20
- `regime_ok` column does NOT exist yet on Render (will be added when v0.7.7 starts; existing rows will be NULL until recompute)

---

## Key constitutional reminders for the next Claude

1. **Rob uses Render, not local.** Every "fix" requires deploy. Test hypotheses locally with simulated data BEFORE asking Rob to deploy. Don't ask him to deploy a diagnostic version.

2. **UI errors must surface the real error.** Never wrap server responses in generic JSON-parse failure messages. Always log response status + first 500 chars of body.

3. **Smoke tests catch what unit tests miss.** Every bug we've shipped this session was caught, after the fact, by a smoke test we should have had already. When adding any new code path, also add the smoke test for it BEFORE packaging.

4. **Sqlite-connection-per-row is a worker-killer on Render.** If you're looping over trades/bars/rows doing DB queries, use one long-lived connection + an in-memory cache for repeated lookups. 1,760 `sqlite3.connect()` calls in one HTTP request will time out Render workers.

5. **Multi-worker is not optional on Render.** Any in-memory singleton state (job queues, caches, rate-limiters) has to be SQLite-backed or the UI polling story will break.

6. **The audit infrastructure is your friend.** If any future backtest engine change touches `_simulate_trade`, run the audit against a real baseline run afterwards to catch semantic regressions. The reference simulator in `backtest_audit.py` is the canonical authority.

7. **Rob will correct you if you get trivial details wrong.** His time is not free. Triple-check function names against the actual codebase before using them (`record_backtest_run` not `insert_backtest_run`; `collect_range` not `backfill_range`).

---

## Session-to-session continuity marker

When the new Claude picks this up, first actions should be:
1. Read this briefing
2. Confirm the v0.7.7 zip exists at `/mnt/user-data/outputs/tech_collector_v0.7.7.zip`
3. Ask Rob: **"Have you deployed v0.7.7 and recomputed IT? If yes, the next step is the 3-backtest validation. If not, what's blocking?"**
4. Wait for his answer before assuming state.

Don't volunteer opinions on work he didn't ask about. Continue his train of thought.
