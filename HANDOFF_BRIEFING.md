# Tech Collector — Handoff Briefing (v0.7.12, post-AH-bar-guard)

**For:** the next Claude chat continuing this work with Rob
**Last session:** v0.7.11 baseline (a) backtest produced 14 phantom rows out of 2058 (0.68%) — same `exit_time_et=19:00 / minutes_held=0` signature as the prior phantom bug. Root cause traced to `_find_scan_bar_ts` accepting after-hours bars when regular-session bars are missing from `raw_bars`. v0.7.12 fixes this with two complementary guards. Code is complete and 74/74 verified locally on the three network-independent smoke files; deployment + sector smoke verification + (a) re-run is the next step.
**Critical state:** v0.7.12 is NOT yet deployed. The (a) baseline needs to be re-run from scratch on v0.7.12. The v0.7.11 (a) CSV (`run_uuid 77416b6d`) is contaminated and discarded.

---

## What you absolutely must read first

1. **The v0.7.11 (a) baseline was contaminated.** 14 phantom trades out of 2058 (0.68%) carry the canonical phantom signature: `exit_time_et == "19:00"` (or 19:01), `exit_reason == "TIME"`, `minutes_held == 0`, and gross outside the invariant band ([-876.6, +864.2] vs. allowed [-157.5, +82.5]). Symbol concentration: SMCI ×7, PLTR ×5, LITE ×1, APP ×1. All on dates with known data anomalies (SMCI's Nov 2024 – Mar 2025 halt/delisting saga; PLTR Mar 4 2025 had phantoms on both 10:30 and 11:30 signals same day).

2. **The bug — and why v0.7.11 didn't catch it:** v0.7.11 correctly fixed `storage.get_raw_bars_for_day` to return bars for one ET trading date including same-day after-hours (ET 19:00–23:59). For most (symbol, date) pairs, regular-session bars sort earlier in UTC and `_find_scan_bar_ts` matches them first — fine. **But for (symbol, date) pairs where regular-session bars are MISSING from `raw_bars`** (likely cause: halted-symbol partial pulls or cleanup-then-repopulate races), only same-day AH bars remained. `_find_scan_bar_ts` then matched the first AH bar (et.hour=19 satisfies any reasonable scan_time, et.date matches signal_date), simulator anchored entry there, fired timestop on the first iteration (et.hour=19 > timestop_h=15), and recorded a phantom-TIME exit with bogus gross from the AH-vs-research-scan-price gap. **This bug is a sibling of the v0.7.11 bug, exposed by the v0.7.11 fix correctly including same-day AH bars that used to be silently dropped.**

3. **Fix shipped in v0.7.12 (code complete, NOT yet deployed):**
   - **Primary:** `_find_scan_bar_ts` now requires the matched bar to be in regular ET session (09:30–15:59). Returns None for AH-only or pre-market-only bar lists. `run_backtest`'s existing None-handler emits a `NO_DATA` trade for these signals — no contamination.
   - **Defense-in-depth:** `_simulate_trade`'s main-loop TIME exit (line ~250) now applies the same `[-sl_level, +tp_level]` invariant the fallback path on line ~352 already had. If anything ever lets an AH bar through `_find_scan_bar_ts` again, the simulator raises an AssertionError loudly instead of silently writing a contaminated trade. Message points at the session guard.
   - **New diagnostic:** `GET /raw-bars/coverage?symbol=X&date=YYYY-MM-DD` returns `{n_pre_market, n_regular_session, n_after_hours, first_bar_et, last_bar_et, phantom_risk}`. Lets us confirm/refute the missing-regular-session hypothesis directly on the deployed DB without a code change.

4. **Earlier "fixes" weren't wrong, just incomplete.** v0.7.1 (engine invariant fallback), v0.7.8 (zoneinfo / `_utc_hour_to_et` removal), v0.7.11 (storage UTC→ET range + `_find_scan_bar_ts` ET-date guard), v0.7.12 (`_find_scan_bar_ts` session guard + main-loop invariant). Each addressed a real defect; together they form the full closure of the phantom-TIME failure class. v0.7.12 was specifically required because the v0.7.11 fix legitimately included same-day AH bars in the bar list, exposing a bar-selection vulnerability that pre-v0.7.11 was masked by the storage layer dropping those bars.

5. **Diagnostic endpoints (v0.7.9+ kept; v0.7.12 added one).**
   - `GET /source-version` — SHA256 hashes of loaded modules + paths. Compare to `HANDOFF_DATA.json` after deploy.
   - `GET /backtest/engine-selftest` — three canonical scenarios hitting `_simulate_trade` directly. Still does NOT cover storage layer or `_find_scan_bar_ts` end-to-end. Don't trust as sole verification of the v0.7.12 fix; the `smoke_backtest_audit` E2E test (`test_v0712_run_backtest_AH_only_yields_NO_DATA`) is the authoritative regression coverage.
   - `GET /raw-bars/coverage?symbol=...&date=...` — v0.7.12, pinpoints which (symbol, date) pairs are at phantom risk.

---

## Rob's working style — preserve absolutely

- **"We only do things properly."** No half-measures. No hacks.
- **"Trial and error is not acceptable."** Reason a fix through to completion locally with reproducer tests BEFORE asking him to deploy.
- **No manual work — automate as much as possible.**
- **Step-by-step instructions when he asks.** Exact UI clicks and form values, not abstract advice.
- **Blunt evidence-driven assessment.** Tell him when results are bad. Don't sugar-coat.
- **Trust him when he says something's broken.** Investigate, don't reassure.
- **Render-only deployment.** Every fix requires a deploy. Verify locally with reproducer tests first.
- **No shell/SSH/SQL access** to Render beyond what the API exposes. Diagnostics come through HTTP endpoints.

---

## Current state of the deployed system

- **Render service:** `tech-collector.onrender.com`. Version reports `0.7.11` via `/info` until v0.7.12 is deployed.
- **DB:** SQLite at `/var/data/tech_collector.db`
- **Sector:** Information Technology, ~75 symbols, 2 years (Apr 2024 → Apr 2026)
- **`research_rows`:** Computed on v0.7.8/v0.7.10 with `regime_ok` populated. ~95k+ rows. Date range 2024-04-22 → 2026-04-17ish. Unaffected by v0.7.12 — no recompute needed.
- **`raw_bars`:** Populated for IT sector + SPY across the same range. Contains after-hours bars. **Some (symbol, date) pairs are missing regular-session bars** — that's the structural condition v0.7.12 now correctly handles. Use `/raw-bars/coverage` after deploy to enumerate which ones.
- **`backtest_runs`:** All pre-v0.7.12 runs are contaminated (including the v0.7.11-era `6ce332aa` single-day diagnostic, which happened to hit a non-affected day so produced clean output, and the (a) baseline `77416b6d` which carries 14 phantoms). Don't analyze any pre-v0.7.12 run.
- **`bt_jobs`:** SQLite-backed. Orphan-sweep on startup gated by `sweep_orphaned=True` (fires once per process restart).

---

## What's in v0.7.12 (code complete, awaiting deploy)

1. **`_find_scan_bar_ts` regular-session guard** — rejects `et.hour < 9`, `(et.hour == 9 and et.minute < 30)`, or `et.hour >= 16`. Returns None if no in-session bar matches.
2. **`_simulate_trade` main-loop TIME-exit invariant** — same `[-sl_level, +tp_level]` (1 bp tol) check the fallback path has. Raises AssertionError on violation.
3. **`GET /raw-bars/coverage`** — new diagnostic endpoint returning bar-coverage breakdown for (symbol, ET date).
4. **6 new regression tests** in `smoke_backtest_audit.py`:
   - `test_v0712_find_scan_bar_ts_rejects_after_hours`
   - `test_v0712_find_scan_bar_ts_rejects_pre_market`
   - `test_v0712_find_scan_bar_ts_accepts_regular_session`
   - `test_v0712_find_scan_bar_ts_skips_AH_finds_regular`
   - `test_v0712_simulate_trade_main_loop_invariant_fires`
   - `test_v0712_run_backtest_AH_only_yields_NO_DATA`
5. **All v0.7.11 engine fixes preserved** — phantom-TIME invariant on fallback (v0.7.1), zoneinfo-based ET conversion (v0.7.8), `_utc_hour_to_et` trap (v0.7.8), storage layer ET-date filtering (v0.7.11), `_find_scan_bar_ts` ET-date guard (v0.7.11).
6. **Compute optimization preserved** — bars cache + chunked compute (v0.7.8/v0.7.10).
7. **Job system fixes preserved** — SQLite-backed jobs (v0.7.6), gated orphan sweep (v0.7.10).

**Local module hashes (compare against deployed `/source-version` after deploy):**
- `backtest.py`: `488e94145778556de97f66775b58efcb43edcb6e4c171824ae514e0b14a8d4dc` ← changed
- `feature_computer.py`: `bf502226a72794a89a672b1945f853c97bb37b15f7c9f26ad6ae422c9245d286` (unchanged)
- `api.py`: `d4da08bd7cde44ba15f3ec41d7bf766a9463e0c9094e376d9ed948f559fdcd86` ← changed
- `storage.py`: `518419943186470bc916aec016884acec86fc3eeb5daebc52e4d89e7e7fbaba7` (unchanged)
- `backtest_audit.py`: `0fa3d887951873cc7f91275ac897de5f3bd014b4dfb9ff45715f1df356f2592b` (unchanged)
- `jobs.py`: `ddc29f8ceba0f590c6bed4bdb4e8bac8f44657f34bd41175572692df732176b2` (unchanged)
- `__init__.py`: `a2dd2bd25837990a9cd4ec12e5d219373c1069a4104b4edde69135bf6780a704` ← changed (version)
- `config.py`: `6eb20e64f9c2ce8ce32444fe905229df113104156c83bb17edd7a7588107c2f4` ← changed (version)

If `/source-version` returns different values for `backtest.py`, `api.py`, `__init__.py`, or `config.py` after deploy, the deploy mechanism failed silently. Debug that BEFORE running anything.

**Smoke totals (verified locally):**
- `smoke_backtest_audit`: **48/48 passed** (was 38; +10 new assertions across 6 v0.7.12 tests)
- `smoke_compute`: **10/10 passed**
- `smoke_jobs`: **16/16 passed**
- `smoke_sectors`: **NOT YET RE-RUN.** Was 212/212 on v0.7.11. v0.7.12 changes don't touch the sectors-pipeline code path; only the hardcoded version-string check in three places was bumped from 0.7.11 → 0.7.12. **Rob: please run `python3 -m tests.smoke_sectors` locally before deploying** to confirm. If fewer than 212 pass, stop and investigate.

Expected total: **286/286** when smoke_sectors is re-run.

---

## Current state of work — RESUME HERE

**Immediately next step:** Rob deploys v0.7.12, verifies `/source-version` matches the four changed hashes above, then re-runs the full-range (a) baseline. Same form settings as before.

### Pre-deploy checklist

1. Unzip v0.7.12 locally, run `python3 -m tests.smoke_sectors` → expect 212/212.
2. Run `python3 -m tests.smoke_backtest_audit` → expect 48/48.
3. Run `python3 -m tests.smoke_compute` → expect 10/10.
4. Run `python3 -m tests.smoke_jobs` → expect 16/16.
5. If all four green → deploy to Render.

### Post-deploy verification (before backtest)

1. `GET /info` → expect `"version": "0.7.12"`.
2. `GET /source-version` → confirm `backtest.py`, `api.py`, `__init__.py`, `config.py` hashes match the four "changed" hashes above.
3. `GET /backtest/engine-selftest` → expect `all_pass: true` (still doesn't exercise v0.7.12 path; just confirms v0.7.8 invariants intact).
4. `GET /raw-bars/coverage?symbol=SMCI&date=2024-11-14` → expect `phantom_risk: true`, `n_regular_session: 0`, `n_after_hours: >0`. **This confirms the v0.7.12 hypothesis on real production data.** If `n_regular_session > 0` for that pair, the missing-bars hypothesis is wrong and we need to investigate further before the (a) re-run.

### Backtest (a) settings — ready to paste (unchanged from v0.7.11)

**Rule JSON:**

```json
{
  "id": "it-50-mom-vol-2way-atr5-v0.7.12-baseline",
  "sector": "Information Technology",
  "target": "target_peak_75bps",
  "predicates": [
    {"feature": "momentum", "op": ">", "value": 0.002},
    {"feature": "rel_volume_r2k", "op": ">", "value": 1.2},
    {"feature": "atr_reach", "op": "<", "value": 5.0}
  ]
}
```

**Form fields:**
- TP (BPS): `50`
- SL (BPS): `150`
- TIMESTOP (ET): `15:50`
- SLIPPAGE (BPS): `15`
- SPY REGIME FILTER: empty
- SYMBOL EXCLUDE: empty
- START DATE: `24/04/2024` (2024-04-22 in ISO)
- END DATE: `17/04/2026`

**Conditional exits (C-scaled two-branch):**

| feature | op | value | TP_BPS | SL_BPS | SIZE | LABEL |
|---|---|---|---|---|---|---|
| `gap_filled` | `==` | `0` | `75` | `100` | `1` | `gap_open` |
| `gap_filled` | `==` | `1` | `50` | `150` | `0.5` | `gap_filled_half` |

### After Rob uploads (a) trades CSV — validation checklist

Spot-check IMMEDIATELY:

1. **`exit_time_et` column:** every value in `09:30`–`15:59` ET. **Any `19:xx` = engine still broken — STOP.**
2. **`minutes_held` column:** TIME exits should be nonzero (max ~320). Any TIME exit with `minutes_held == 0` is a phantom signature. (Note: legitimate TP exits CAN have `minutes_held == 0` when entry bar's high crosses TP within the entry minute — these are fine and concentrated at `exit_time_et == signal_time_et`.)
3. **`gross_return_bps` distribution:** within `[-160, +90]`. Branch `gap_open`: ~[-107, +82.5]. Branch `gap_filled_half`: ~[-157.5, +57.5].
4. **`exit_reason == NO_DATA` count:** expected to be ~14 (the previous phantoms now correctly skipped). Could be slightly higher or lower depending on which exact (symbol, date) pairs the new guard now catches. If NO_DATA count is dramatically larger (say >50), investigate — the guard may be too aggressive.
5. **Trade count:** should be ~2055 minus NO_DATA count. So roughly 2040 valid exits.
6. **Total NET P&L:** broadly in `[-3000, +12000]` bps (educated guess; could fall outside). The contaminated v0.7.11 raw sum of net was -129 bps; after removing the 14 phantoms it was +749 bps raw / +501 bps size-weighted. v0.7.12 should land near those latter figures (the 14 phantoms become NO_DATA = 0 P&L, mathematically equivalent to dropping them). **Do not anchor on this prediction — let the actual run be the baseline.**

If spot-check passes → run per-quarter, per-symbol, per-branch analysis. **Do NOT make filter recommendations yet.** First show Rob the clean baseline.

If spot-check fails → STOP. Re-check `/source-version`, `/raw-bars/coverage` for the offending symbol/date, look at the failing trade's `entry_price` vs `exit_price` to characterize the new failure mode.

---

## What we're trying to figure out (the actual project)

Rob is building a discretionary intraday trading strategy for the IT sector. Open questions:
- Does the C-scaled rule have positive expected value on clean data?
- Are there filters (regime, symbol exclusion, etc.) that meaningfully improve it?
- What position sizing makes sense given trade concurrency profile?
- Is the strategy robust across market regimes?

We still don't know — pre-v0.7.12 data is all contaminated. v0.7.12 (a) re-run is the first chance at an honest answer.

---

## Open / pending items (priority ordered)

1. **Re-run (a) full-range baseline on v0.7.12.** Pre-deploy checklist + post-deploy verification first.
2. **Validate (a) CSV** — spot-check + NO_DATA count.
3. **Analyze (a)** — per-quarter, per-symbol, per-branch, per-feature distributions on clean data.
4. **Run (b)** with `regime_ok == 1` predicate. Same form. Compare.
5. **Run (c)** with `regime_ok == 1` + no timestop (empty timestop_et). Compare.
6. **(Backlog) Daily signal cap.** April 7 2025 had ~146 simultaneous signals. Real portfolio-level tail risk per-trade analysis can't catch. Need a `max_signals_per_day` BacktestConfig field.
7. **(Backlog) Repair workflow.** `audit_run` / `repair_run` use `_simulate_trade_reference` in `backtest_audit.py`, which has the existing `_make_time_exit` invariant but does NOT have the v0.7.12 `_find_scan_bar_ts` session guard at its entry-resolution layer (it uses `_signal_time_to_utc_iso` instead, which doesn't search the bar list). Audit IS structurally protected by the invariant in `_make_time_exit` — would raise AssertionError on AH-only days rather than write contamination — but `audit_run`'s loop catches exceptions per trade, so the audit reports would mark these as `audit_error` rather than as clean re-simulation. Worth a follow-up if Rob wants to re-audit any historical run.
8. **(Backlog) UI version display ('v–' issue).** Diagnose if it recurs.

---

## What NOT to do

1. **Do not reference any pre-v0.7.12 backtest results as truth.** The v0.7.11 (a) baseline (`77416b6d`), the +12,613 bps "corrected baseline" from earlier sessions, the per-quarter breakdowns, regime/exclusion findings, MPWR/STX/CIEN losers, 1/N projections — all derived from contaminated data.

2. **Do not analyze the contaminated CSVs by "just removing the 14 phantoms".** Mathematically equivalent in some narrow senses but methodologically suspect — re-run on v0.7.12 and use that as the baseline.

3. **Do not skip the deploy verification.** `/source-version` hash check + `/raw-bars/coverage` confirmation on a known phantom-source symbol/date are both required before running (a). The deploy mechanism has failed silently before (flat zip layout v0.7.0/v0.7.10/v0.7.11 worked; nested layouts v0.7.7/v0.7.8 silently failed).

4. **Do not propose new code changes before establishing the clean v0.7.12 baseline.** Wait for honest baseline numbers, then iterate.

5. **Do not assume the rule predicates are right.** `momentum > 0.002, rel_volume_r2k > 1.2, atr_reach < 5.0` are placeholders that produce ~2,055 signals. If results look weak, the rule itself may need rebuilding.

---

## Files in the handoff zip

- `tech_collector_v0.7.12.zip` — current code, FLAT layout, all four smokes verified locally except `smoke_sectors` (network-blocked in dev sandbox; version-string assertions inside it bumped to 0.7.12)
- `HANDOFF_BRIEFING.md` — this document
- `HANDOFF_DATA.json` — programmatic state (hashes, v0.7.11 contamination summary, settings JSON)

---

## First message to send Rob in the new chat

Don't re-explain context. Confirm and prompt:

> Continuing from the prior chat. v0.7.12 is code-complete (AH-bar guard at `_find_scan_bar_ts` + main-loop invariant + `/raw-bars/coverage` diagnostic + 6 new regression tests). 74/74 verified on three smoke files locally; smoke_sectors needs to be run on Rob's machine before deploy. Have you deployed v0.7.12 yet? If not, run pre-deploy checklist; if yes, check `/source-version` hashes and `/raw-bars/coverage?symbol=SMCI&date=2024-11-14` before re-running (a).

---

## Working notes for next Claude

- **The phantom-TIME failure class is now fully closed at three layers:** storage (v0.7.11 ET-date range), bar selection (v0.7.12 session guard), and simulator (v0.7.12 main-loop invariant + v0.7.1 fallback invariant). If a new variant ever appears, look outside this class first — likely something further upstream (research_rows compute, signal generation, or feed integrity).

- **The audit infrastructure (`backtest_audit.py`) was NOT modified in v0.7.12.** It uses `_signal_time_to_utc_iso` (math, not bar-search) for entry-time resolution, so the AH-bar-selection vulnerability doesn't apply structurally. But its `_simulate_trade_reference` has not been re-verified against AH-only bar lists with the same rigour as `_simulate_trade`. If Rob requests re-audits of historical runs, treat audit output as needing its own validation pass.

- **`/raw-bars/coverage` is the new debug primitive.** Before guessing about data integrity, query it. It's the equivalent of "look at the data first" that we should have built earlier.

- **Trust your tests, not your hypotheses.** Got us through both v0.7.11 and v0.7.12. End-to-end reproducer tests with realistic-shape inputs are the only thing that's caught these phantom variants reliably.

- **Be honest about uncertainty.** "I think this fixes it; let's verify on production." When something works, "this is working." When something is broken, "this is broken." Don't bury bad news.
