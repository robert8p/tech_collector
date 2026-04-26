# Tech Collector — Handoff Briefing (v0.7.13, post–invariant-arithmetic-fix)

**For:** the next Claude chat continuing this work with Rob
**Last session:** Mid-flow on a feature-discovery HOLDOUT analysis (a deliberate restart from scratch). Rule 1 (Continuation: `atr_reach < 12.52` AND `momentum > 0`) was selected on TRAIN/TEST and queued for HOLDOUT evaluation. The v0.7.12 backtest engine raised a false-positive AssertionError on the first HOLDOUT attempt due to an arithmetic bug in the v0.7.12-added TIME-exit invariant. v0.7.13 fixes the arithmetic. Code is complete and 78/78 verified locally on the three offline smoke files; deployment + sector smoke verification + HOLDOUT re-run is the next step.
**Critical state:** v0.7.13 is NOT yet deployed. The HOLDOUT result for Rule 1 is unknown — the previous attempt aborted with an invariant error, no contaminated data was written.

---

## What you absolutely must read first

1. **The v0.7.12 invariant had an arithmetic bug.** It compared `gross_bps` (computed from raw `entry_price`) against `tp_level` and `-sl_level`, but the engine's TP/SL bar checks use `effective_entry` (post entry-side slippage). The two scales differ by the entry-slippage offset (`slippage_bps × entry_slippage_split`, e.g., 7.5 bps for slip=15, split=0.5). Result: legitimate timestop exits with raw gross slightly above `tp_level` but below the actual `tp_price` (not enough to fire TP) falsely triggered the invariant.

2. **The bug was caught in production by the v0.7.12 invariant itself.** The first HOLDOUT attempt failed with:
   ```
   gross_bps=51.94 outside [-100.0, +50.0]. entry_ts=2026-01-05T18:30:00+00:00,
   entry_price=186.77, timestop_bar_ts=2026-01-05T20:50:00+00:00,
   timestop_open=187.7400, et_hh:et_mm=15:50.
   ```
   This trade was legitimate: 13:30 ET regular-session entry, 15:50 ET regular-session timestop, full 2h20m hold. `effective_entry = 186.91`, `tp_price = 187.85`. The 15:50 bar opened at 187.74 — below `tp_price`, so TP correctly didn't fire — but raw gross was +51.94 bps, just above the v0.7.12 invariant's threshold of `tp_level + 1` = 51 bps. **Engine did the right thing by raising rather than writing a possibly-contaminated trade. The invariant was simply wrong.**

3. **Fix shipped in v0.7.13 (code complete, NOT yet deployed):** Both the main-loop and fallback TIME-exit invariants now compute gross from `effective_entry` (matching the basis the TP/SL price thresholds are derived from). This is a one-line semantic fix to each of the two invariants, with extensive comments preserving the v0.7.12 narrative. The v0.7.12 phantom-detection capability is fully preserved — the invariant still raises when an after-hours bar leaks through `_find_scan_bar_ts` (test `test_v0713_main_loop_TIME_invariant_still_catches_phantom` explicitly verifies this).

4. **What we're in the middle of (the actual project work):** A full feature-discovery restart, abandoning all prior placeholder rules. Brief recap:
   - Methodology gates: TRAIN (2024-04-22 → 2025-04-22) for feature scan, TEST (2025-05-01 → 2025-10-31) for rule selection, HOLDOUT (2025-11-03 → 2026-04-17) for one and only one final evaluation. No iteration after HOLDOUT.
   - Discovered `sector_relative_strength` is leaky (schema flagged this; `rs_leakfree` has no edge — confirms leak)
   - Discovered `target_peak_50bps` is mostly a volatility metric not a directional one — top predictors are vol/feasibility predictors, weak directional ones (`momentum`, `mom_vs_spy`) are honest but small
   - Built two parallel candidate rules (continuation vs mean-reversion), both with exactly 2 predicates, frozen at TRAIN quintile thresholds before peeking at TEST
   - **Rule 1 won on TEST** by lift margin (R1: +0.2252 vs R2: +0.2010, both n>1000). R1 carries to HOLDOUT.
   - HOLDOUT pre-committed criteria: net P&L > 0 AND mean per-trade > +0.5 bps AND max DD < |total net|. If fail → restart Step 3, do NOT tweak Rule 1.

5. **What v0.7.13 does NOT change:** the v0.7.11 storage-layer ET-date filter, the v0.7.12 `_find_scan_bar_ts` regular-session guard, the `/raw-bars/coverage` diagnostic endpoint, all earlier engine fixes. Only the two TIME-exit invariants in `backtest.py` and version bumps are modified.

---

## Rob's working style — preserve absolutely

- **"We only do things properly."** No half-measures. No hacks.
- **"Trial and error is not acceptable."** Reason a fix through to completion locally with reproducer tests BEFORE asking him to deploy.
- **"Never take the easy option — only ever the option that has the highest likelihood of achieving a successful outcome."** Stated explicitly this session. Critical context for any judgment call.
- **No manual work — automate as much as possible.**
- **Step-by-step instructions when he asks.**
- **Blunt evidence-driven assessment.** Tell him when results are bad. Don't sugar-coat.
- **Trust him when he says something's broken.** Investigate, don't reassure.
- **Render-only deployment. No SSH/SQL.** Diagnostics through HTTP endpoints.

---

## Current state of the deployed system

- **Render service:** `tech-collector.onrender.com`. Reports `0.7.12` via `/info` until v0.7.13 deployed.
- **DB:** SQLite at `/var/data/tech_collector.db`. Same shape as v0.7.12 — no schema changes.
- **`research_rows`:** ~95k rows for IT sector, range 2024-04-22 → 2026-04-17ish. Computed pre-v0.7.13. Untouched by v0.7.13 — no recompute needed.
- **`raw_bars`:** Same as v0.7.12 — populated for IT + SPY. Includes after-hours bars, including (symbol, date) pairs where regular-session bars are missing (handled correctly by v0.7.12 session guard).
- **`backtest_runs`:** Pre-v0.7.13 runs include the v0.7.12 baseline (`342c5664`), 12-symbol exclusion (`9ebbe454`), 5-symbol exclusion (`3491aeb5`). All clean of phantoms but discarded for current analysis (we're doing a feature-discovery restart).

---

## What's in v0.7.13 (code complete, awaiting deploy)

1. **Fixed main-loop TIME-exit invariant** in `_simulate_trade` (backtest.py ~lines 246-285). Now computes `gross_eff_bps = (bar.open - effective_entry) / effective_entry * 1e4` and compares to `[-sl_level, +tp_level]`. Same comparison basis as the bar-vs-`tp_price` check.
2. **Fixed fallback TIME-exit invariant** in `_simulate_trade` (backtest.py ~lines 362-395). Same basis fix; same correctness reasoning. Documentation in code preserves the v0.7.12 narrative and explains why the bug rarely fired here in practice.
3. **2 new regression tests** in `smoke_backtest_audit.py`:
   - `test_v0713_main_loop_TIME_invariant_allows_legit_above_TP_with_slip` — reproduces the production failure case (entry 186.77, timestop_open 187.74, raw gross 51.94, eff gross 44.4) and asserts no raise
   - `test_v0713_main_loop_TIME_invariant_still_catches_phantom` — re-runs the v0.7.12 phantom case (gross −800 bps) and asserts the invariant still raises
4. **All v0.7.11 + v0.7.12 fixes preserved** — storage-layer ET-date filter, `_find_scan_bar_ts` regular-session guard, `/raw-bars/coverage` endpoint, earlier engine fixes, compute optimization, jobs system.

**Local module hashes (compare against deployed `/source-version` after deploy):**
- `backtest.py`: `3060763ad24679f4fb8795a4778607f93b62f95bc99ea0e3bb27b56b14035822` ← changed
- `feature_computer.py`: `bf502226a72794a89a672b1945f853c97bb37b15f7c9f26ad6ae422c9245d286` (unchanged)
- `api.py`: `d4da08bd7cde44ba15f3ec41d7bf766a9463e0c9094e376d9ed948f559fdcd86` (unchanged from v0.7.12)
- `storage.py`: `518419943186470bc916aec016884acec86fc3eeb5daebc52e4d89e7e7fbaba7` (unchanged)
- `backtest_audit.py`: `0fa3d887951873cc7f91275ac897de5f3bd014b4dfb9ff45715f1df356f2592b` (unchanged)
- `jobs.py`: `ddc29f8ceba0f590c6bed4bdb4e8bac8f44657f34bd41175572692df732176b2` (unchanged)
- `__init__.py`: `50111bf9e10cbaf3239d6a64e5976748b5f8148baee41177bc50956a5108d0a2` ← changed (version)
- `config.py`: `0dce6beb13c0de96f2981cbc00074a737b858da29a4fe3738cbb53043181deac` ← changed (version)

**Smoke totals (verified locally on three offline files; smoke_sectors needs Rob's machine):**
- `smoke_backtest_audit`: **52/52 passed** (was 48; +4 new assertions across 2 v0.7.13 tests)
- `smoke_compute`: **10/10 passed**
- `smoke_jobs`: **16/16 passed**
- `smoke_sectors`: NOT yet re-run. Was 212/212 on v0.7.12; v0.7.13 changes don't touch sectors-pipeline; only version-string asserts bumped (3 places). Expect 212/212.

Expected total when smoke_sectors is re-run: **290/290.**

---

## Current state of work — RESUME HERE

**Immediately next step:** Deploy v0.7.13, verify hashes, re-run the HOLDOUT backtest for Rule 1.

### Pre-deploy checklist

1. Unzip v0.7.13 locally, run `python3 -m tests.smoke_sectors` → expect 212/212.
2. Run `python3 -m tests.smoke_backtest_audit` → expect 52/52.
3. Run `python3 -m tests.smoke_compute` → expect 10/10.
4. Run `python3 -m tests.smoke_jobs` → expect 16/16.

### Post-deploy verification

1. `GET /info` → `"version": "0.7.13"`.
2. `GET /source-version` → `backtest.py`, `__init__.py`, `config.py` hashes match the three "changed" hashes above. `api.py` hash unchanged from v0.7.12 (`d4da08bd...`).
3. `GET /backtest/engine-selftest` → `all_pass: true`.

### HOLDOUT (a) settings — Rule 1 unchanged from prior attempt

Rule JSON:
```json
{
  "id": "step6-holdout-r1-continuation",
  "sector": "Information Technology",
  "target": "target_peak_50bps",
  "predicates": [
    {"feature": "atr_reach", "op": "<", "value": 12.52},
    {"feature": "momentum", "op": ">", "value": 0}
  ]
}
```

Form fields:
- TP `50`, SL `100`, TIMESTOP `15:50`, SLIPPAGE `15`
- SPY REGIME FILTER: empty
- SYMBOL EXCLUDE: empty
- START `2025-11-01`, END `2026-04-17`
- Conditional exits: **leave EMPTY** (clean test, no branch dispatch)

Predicted ~4,500–6,000 trades. v0.7.13 should NOT raise on this run; if it does, that's a new bug in the invariant that needs investigation before any P&L is read off.

### HOLDOUT success criteria (locked, do not relitigate)

- **Pass:** size-weighted net P&L > 0 AND mean per-trade > +0.5 bps AND max DD < |total net|. Move to position-sizing/concurrency analysis.
- **Marginal pass:** net P&L > 0 but per-trade weak or DD large. Document and discuss.
- **Fail:** net P&L ≤ 0. **Per protocol, no tweaking Rule 1.** Restart Step 3 with different methodology — likely interaction features (`momentum × atr_reach` joint quintiles), or a directional target (`return_at_scan_plus_60m > 0` instead of peak-tagging).

### After HOLDOUT CSV uploads — validation checklist

Same as v0.7.12 + new v0.7.13 expectation:
1. `exit_time_et`: every value in 09:30-15:59 ET. Any 19:xx = engine still broken (STOP).
2. `minutes_held`: TIME exits > 0, max ~320. Zero-min TIMEs are phantom; OK only for same-bar TPs.
3. `gross_return_bps`: in `[-160, +90]`. (For TP=50, SL=100, simple non-conditional: max ~+57.5, min ~-92.5.)
4. NO_DATA count: small (single digits expected — depends on missing-bar (symbol,date) pairs in HOLDOUT range).
5. **Run did not abort with AssertionError** (the v0.7.13 fix delivered).

If spot-check passes → evaluate against locked HOLDOUT criteria above. **DO NOT re-introduce branch dispatch, exclusions, or regime gates** — that's iteration. The pre-committed Rule 1 + form is the test.

---

## Open / pending items (priority ordered)

1. **Pre-deploy: run smoke_sectors locally (212 expected).**
2. **Deploy v0.7.13 to Render.**
3. **Post-deploy hash verification.**
4. **Run HOLDOUT for Rule 1 on v0.7.13.**
5. **Apply locked HOLDOUT criteria to result.**
6. (If pass) Position-sizing & concurrency analysis on Rule 1's HOLDOUT trades.
7. (If fail) Restart Step 3 from `tech_scan_rows` data — try interaction features or a directional target. The rule selection process gets re-done from scratch on the same TRAIN/TEST/HOLDOUT split, but TEST and HOLDOUT remain held-out from feature scanning (TRAIN is the only thing we look at for discovery).
8. (Backlog) Re-audit historical runs if Rob wants. Audit infrastructure not v0.7.13-verified — `_simulate_trade_reference` in `backtest_audit.py` has its own `_make_time_exit` invariant which uses raw gross too. **It has the same arithmetic bug.** It rarely fires because `_signal_time_to_utc_iso` doesn't search the bar list (no AH-bar-selection vulnerability), but the invariant itself is technically wrong. Worth fixing in a future hardening pass — not blocking.
9. (Backlog) Daily signal cap (April 7 2025 = ~36 simultaneous signals on clean v0.7.12 data, much less than the contaminated-era "146" claim).
10. (Backlog) UI version display.

---

## What NOT to do

1. **Do not deploy v0.7.13 without running the offline smokes locally first** (see pre-deploy checklist).
2. **Do not skip post-deploy hash verification.** Every deploy gets verified.
3. **Do not iterate on Rule 1** based on HOLDOUT result. Pass/fail decision is locked. If fail, restart Step 3 — do not relax HOLDOUT criteria, do not adjust thresholds, do not add predicates.
4. **Do not add conditional exits / branch dispatch / symbol exclusions to the HOLDOUT run** — that's the next Claude prematurely "improving" Rule 1. The simplicity of two predicates and a single TP/SL pair is intentional.
5. **Do not reference any pre-restart findings** ("12-symbol exclusion list", "+4549 bps from baseline", "regime_ok improves H2") as truth. Those came from the placeholder rule and are not part of the current discovery process.

---

## Files in the handoff zip

- Source code FLAT layout matching prior versions, with the v0.7.13 changes
- `HANDOFF_BRIEFING.md` — this document
- `HANDOFF_DATA.json` — programmatic state (hashes, settings, locked criteria)
- `evidence_packs/` — kept the v0.7.11 contaminated baseline CSV as documented evidence; rest unchanged

---

## First message to send Rob in the new chat

> Continuing from prior chat. v0.7.13 is code-complete (TIME-exit invariant arithmetic fix; main-loop and fallback both corrected; phantom detection preserved). 78/78 on offline smokes. The HOLDOUT for Rule 1 (`atr_reach < 12.52, momentum > 0`, TP=50/SL=100, no exclusions or branch dispatch) is the immediately-next step — engine should now allow legitimate near-TP timestops through. Have you deployed v0.7.13 yet? If not, run the pre-deploy smokes; if yes, post-deploy hashes + HOLDOUT.

---

## Working notes for next Claude

- **Two TIME-exit invariants existed pre-v0.7.13. Both had the same bug; both are now fixed.** If you ever modify `_simulate_trade`, make sure both the main-loop timestop path AND the fallback path use `gross_eff_bps` for the invariant comparison. Don't regress the basis.

- **The v0.7.12 invariant fired BEFORE writing contaminated data.** That's the correct fail mode for an over-tight invariant. Better than silently writing wrong P&L. Honor that pattern in any new invariants you add.

- **The audit infrastructure (`backtest_audit.py`) was NOT modified in v0.7.13.** Its `_make_time_exit` uses raw gross too. That bug is technically present but rarely triggers (audit-side entry resolution doesn't search bars). Future hardening work, not blocking.

- **Methodological discipline for the feature-discovery work matters more than any specific rule.** The whole point of the restart was to do this cleanly, with HOLDOUT untouched until the very end. If HOLDOUT fails for Rule 1, the next attempt MUST come from a fresh TRAIN-only feature scan — not from inspecting what didn't work in HOLDOUT.

- **Be honest about uncertainty.** If you've fixed something, say "I think this fixes it; let's verify on production." When something works, "this is working." When something is broken, "this is broken." Don't bury bad news in qualifiers.
