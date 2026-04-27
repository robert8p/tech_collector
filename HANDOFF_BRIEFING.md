# Tech Collector — Handoff Briefing (v0.7.14, post-invariant-redesign)

**For:** the next Claude chat continuing this work with Rob
**Last session:** Mid-flow on a feature-discovery HOLDOUT analysis. v0.7.12 added a gross-based TIME-exit invariant that was **fundamentally unsound** — fired false-positives on legitimate trades. v0.7.13 attempted an arithmetic fix that addressed one false-positive class but missed the structural one (gap-through-TP at the timestop bar). **v0.7.14 removes the gross-based main-loop invariant entirely and replaces it with an entry-time-in-regular-session check** at the top of `_simulate_trade`. The check targets the actual phantom signature directly (entry bar in AH) rather than a downstream proxy (gross outside [-SL, +TP]). The HOLDOUT for Rule 1 has hit invariant-raises on two separate v0.7.12 / v0.7.13 attempts and not yet completed.
**Critical state:** v0.7.14 is NOT yet deployed. Code complete with 90/90 verified locally on three offline smoke files (smoke_sectors needs Rob's machine). HOLDOUT pending.

---

## What you absolutely must read first

1. **The v0.7.12 main-loop TIME-exit invariant was unsound, not just arithmetically off.** It claimed timestop TIME exits must have gross within `[-sl_level, +tp_level]`. That claim is FALSE: legitimate timestop exits CAN have gross outside this range when an inter-bar gap immediately precedes the timestop bar (auctions, halts, news pops, lunch slowdowns). The bar JUST BEFORE timestop has high < tp_price (so TP correctly didn't fire), but the timestop bar's open is above tp_price due to the gap. Engine's documented design is to fire timestop on the timestop bar even if TP would otherwise hit — the comment in code is `"Check BEFORE processing TP/SL so we don't award a post-timestop TP exit"`.

2. **Two production failures exposed this:**
   - **First (v0.7.12):** entry 186.77, timestop_open 187.74, raw_gross +51.94. tp_price was 187.85 — timestop_open BELOW tp_price, but raw_gross > tp_level=50 due to entry-slippage offset. v0.7.13 "fixed" this by switching basis from raw_entry to effective_entry.
   - **Second (v0.7.13):** entry 280.75, timestop_open 282.51, raw_gross +62.69, eff_gross +55.15. tp_price was 282.37 — timestop_open ABOVE tp_price. eff_gross > tp_level=50 — false-positive even after v0.7.13's fix. Reason: gap from prior bar's close (~282.30) to timestop bar's open (282.51) put the open above tp_price without any prior bar's high crossing tp_price.

3. **v0.7.14 fix (code complete, NOT yet deployed):**
   - **Removed** the v0.7.12/v0.7.13 main-loop gross-based invariant entirely. It cannot be made sound.
   - **Added** an entry-time-in-regular-session check at the top of `_simulate_trade`: if `entry_ts_utc` resolves to ET time outside 09:30-15:59, raise. This is the actual phantom signature; replaces the gross-based proxy with a direct structural check.
   - **Kept** the fallback-path invariant (with v0.7.13's effective-entry-based math). The fallback is only reached when no bar's high reached tp_price AND no bar's low reached sl_price across the entire `parsed` list — which mathematically guarantees last_close ∈ (sl_price, tp_price), so the invariant is sound there.

4. **Verification level (blunt):** I verified v0.7.14 directly against BOTH production failures plus 6 other edge cases (phantom AH entry, pre-market entry, gap-down-through-SL at timestop, session boundaries 09:30/15:59/16:00). All 8 cases give correct outcomes. 90/90 on offline smokes. The previous two zip-and-ship cycles did not catch the v0.7.13 bug because the v0.7.13 test only covered the "near-TP" case (open below tp_price), not the "gap-through-TP" case (open above tp_price). The v0.7.14 test set covers both.

5. **Rob's stated demand this session:** "fix it and test it properly before providing another zip. We don't have time to waste." I take this seriously. The v0.7.14 testing is more thorough than v0.7.13's was — direct production-failure replay plus 6 edge cases plus the existing smoke regression suite, all run and passing.

---

## Rob's working style — preserve absolutely

- **"We only do things properly."** No half-measures. No hacks.
- **"Trial and error is not acceptable."** Reason a fix through to completion locally with reproducer tests BEFORE asking him to deploy.
- **"Never take the easy option — only ever the option that has the highest likelihood of achieving a successful outcome."** Stated this session.
- **"PLEASE fix it and test it properly before providing another zip."** Stated this session, after the v0.7.13 false-fix. Verify EVERY claimed fix against the actual production failure data before shipping.
- **No manual work — automate as much as possible.**
- **Step-by-step instructions when he asks.**
- **Render-only deployment. No SSH/SQL.** Diagnostics through HTTP endpoints.

---

## What's in v0.7.14 (code complete, awaiting deploy)

1. **Removed unsound main-loop TIME-exit invariant** in `_simulate_trade` (backtest.py, in the timestop-fires branch around line 246-282). The block has a long comment explaining why the invariant was unsound and why the entry-time check replaces it. The TIME exit return statement is unchanged.

2. **Added entry-time-in-regular-session check** at top of `_simulate_trade` (backtest.py, after `entry_dt` is parsed, around line 203-228). Raises with a clear message if `entry_dt` resolves to ET time outside 09:30-15:59. Direct defense-in-depth for the AH-bar phantom case.

3. **Kept fallback-path invariant** with v0.7.13's effective-entry math (backtest.py, around line 362-395). This invariant IS sound by construction (no bar's high ≥ tp_price ⟹ last_close < tp_price; symmetric for SL).

4. **3 new regression tests** in `smoke_backtest_audit.py`:
   - `test_v0714_legit_timestop_above_eff_TP_via_gap_does_not_raise` — replays the v0.7.13 production failure (entry 280.75 → timestop_open 282.51 above tp_price)
   - `test_v0714_entry_time_invariant_fires_on_AH_entry` — phantom case still caught
   - `test_v0714_entry_time_invariant_passes_for_regular_session` — sweeps all 6 standard scan times + 15:59 boundary

5. **All v0.7.11/v0.7.12/v0.7.13 fixes preserved** — storage-layer ET-date filter, `_find_scan_bar_ts` regular-session guard, `/raw-bars/coverage` endpoint, fallback-path invariant correctness, earlier engine fixes.

**Local module hashes (compare against deployed `/source-version` after deploy):**
- `backtest.py`: `e55262b4713c02f11a19628be2eaa3e74b0aacbe67db3aaa18d49f5ee598a6f2` ← changed
- `feature_computer.py`: `bf502226a72794a89a672b1945f853c97bb37b15f7c9f26ad6ae422c9245d286` (unchanged)
- `api.py`: `d4da08bd7cde44ba15f3ec41d7bf766a9463e0c9094e376d9ed948f559fdcd86` (unchanged from v0.7.12)
- `storage.py`: `518419943186470bc916aec016884acec86fc3eeb5daebc52e4d89e7e7fbaba7` (unchanged)
- `backtest_audit.py`: `0fa3d887951873cc7f91275ac897de5f3bd014b4dfb9ff45715f1df356f2592b` (unchanged)
- `jobs.py`: `ddc29f8ceba0f590c6bed4bdb4e8bac8f44657f34bd41175572692df732176b2` (unchanged)
- `__init__.py`: `a5a319fbaad5863f11cd38f2284c9ff38a302e943f75df203bad088672ccca89` ← changed (version)
- `config.py`: `29d96bc7f9bd9ec481f3e8096ed833fd20db69cfc5b9ad64137801b7d433777c` ← changed (version)

**Smoke totals (verified locally on three offline files; smoke_sectors needs Rob's machine):**
- `smoke_backtest_audit`: **64/64 passed** (was 52; +12 new assertions across 3 v0.7.14 tests; v0.7.12/v0.7.13 tests still pass — the entry-time check raises before the old gross check would have)
- `smoke_compute`: **10/10 passed**
- `smoke_jobs`: **16/16 passed**
- `smoke_sectors`: NOT yet re-run. Was 212/212 on prior versions. v0.7.14 changes don't touch the sectors-pipeline code path; only the hardcoded version-string check in three places was bumped.

Expected total after smoke_sectors run: **302/302**.

**Direct production-failure verification beyond the smoke suite (eight cases, all pass):**
1. v0.7.12 prod failure (entry 186.77, timestop_open 187.74) → no raise, exit_reason=TIME ✓
2. v0.7.13 prod failure (entry 280.75, timestop_open 282.51) → no raise, exit_reason=TIME ✓
3. AH-bar phantom (entry at ET 19:00) → raises with entry-time message ✓
4. Symmetric gap-DOWN-through-SL at timestop → no raise, exit_reason=TIME ✓
5. Pre-market entry (08:00 ET) → raises ✓
6. 09:30 boundary → passes ✓
7. 15:59 boundary → passes ✓
8. 16:00 boundary → raises ✓

---

## Current state of work — RESUME HERE

**Immediately next step:** Deploy v0.7.14, verify hashes, re-run the HOLDOUT backtest for Rule 1 (third attempt).

### Pre-deploy checklist

1. Unzip v0.7.14 locally, run `python3 -m tests.smoke_sectors` → expect 212/212.
2. Run `python3 -m tests.smoke_backtest_audit` → expect 64/64.
3. Run `python3 -m tests.smoke_compute` → expect 10/10.
4. Run `python3 -m tests.smoke_jobs` → expect 16/16.

### Post-deploy verification

1. `GET /info` → `"version": "0.7.14"`.
2. `GET /source-version` → `backtest.py`, `__init__.py`, `config.py` hashes match the three "changed" hashes above. `api.py` unchanged from v0.7.12 (`d4da08bd...`).
3. `GET /backtest/engine-selftest` → `all_pass: true`.

### HOLDOUT settings — Rule 1 unchanged

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

Form fields (unchanged from v0.7.12 / v0.7.13 attempts):
- TP `50`, SL `100`, TIMESTOP `15:50`, SLIPPAGE `15`
- SPY REGIME FILTER: empty
- SYMBOL EXCLUDE: empty
- START `2025-11-01`, END `2026-04-17`
- Conditional exits: **leave EMPTY** (clean test, no branch dispatch)

Predicted ~4,500–6,000 trades. v0.7.14 should NOT raise on this run; if it does, that's a new bug not anticipated by the v0.7.14 test set, and we stop and investigate.

### HOLDOUT success criteria (locked, do not relitigate)

- **Pass:** size-weighted net P&L > 0 AND mean per-trade > +0.5 bps AND max DD < |total net|. Move to position-sizing/concurrency analysis.
- **Marginal pass:** net P&L > 0 but per-trade weak or DD large. Discuss.
- **Fail:** net P&L ≤ 0. **Per protocol, no tweaking Rule 1.** Restart Step 3 with different methodology (interaction features or directional target like `return_at_scan_plus_60m > 0`).

### After HOLDOUT CSV uploads — validation checklist

1. `exit_time_et`: every value in 09:30-15:59 ET. Any `19:xx` = engine still broken (STOP).
2. `minutes_held`: TIME exits > 0, max ~320. Zero-min TIMEs are phantom; OK only for same-bar TPs.
3. `gross_return_bps`: typically in `[-100, +60]` for TP=50/SL=100 with slip, but **may exceed those bounds when the timestop bar opens above tp_price or below sl_price due to inter-bar gaps** — that's a legitimate occurrence in v0.7.14, not a phantom signature. The bounds are not a hard invariant; only the entry-time check is.
4. NO_DATA count: small (single digits expected).
5. Run completed without AssertionError.

If spot-check passes → evaluate against locked HOLDOUT criteria above. **DO NOT re-introduce branch dispatch, exclusions, or regime gates** — that's iteration. The pre-committed Rule 1 + form is the test.

---

## Open / pending items (priority ordered)

1. **Pre-deploy: run smoke_sectors locally (212 expected).**
2. **Deploy v0.7.14 to Render.**
3. **Post-deploy hash verification.**
4. **Run HOLDOUT for Rule 1 on v0.7.14.**
5. **Apply locked HOLDOUT criteria.**
6. (If pass) Position-sizing & concurrency analysis on Rule 1's HOLDOUT trades.
7. (If fail) Restart Step 3 from `tech_scan_rows` data with different methodology.
8. (Backlog) Audit infrastructure (`backtest_audit.py::_make_time_exit`) has the same unsound gross-based invariant as v0.7.12/v0.7.13 had. Should be replaced with the same entry-time-in-session approach. Audit isn't currently used in the production path; deferred.
9. (Backlog) Daily signal cap (April 7 2025 = 36 simultaneous on clean data, manageable).
10. (Backlog) UI version display.

---

## What NOT to do

1. **Do not deploy v0.7.14 without running the offline smokes locally first.**
2. **Do not skip post-deploy hash verification.**
3. **Do not iterate on Rule 1** based on HOLDOUT result. Pass/fail decision is locked.
4. **Do not re-add a gross-based TIME-exit invariant on the main-loop path** — it cannot be made sound. The entry-time-in-session check is the correct phantom defense.
5. **Do not reference any pre-restart findings** ("12-symbol exclusion list", "+4549 bps from baseline", "regime_ok improves H2") as truth.

---

## Files in the handoff zip

- Source code FLAT layout matching prior versions, with v0.7.14 changes.
- `HANDOFF_BRIEFING.md` — this document.
- `HANDOFF_DATA.json` — programmatic state (hashes, settings, locked criteria).
- `evidence_packs/` — kept the v0.7.11 contaminated baseline CSV plus original 3 reference packs.

---

## First message to send Rob in the new chat

> Continuing from prior chat. v0.7.14 is code-complete. The v0.7.12 main-loop TIME-exit invariant was unsound (false-positives on inter-bar gaps); v0.7.13's arithmetic patch addressed one class but not the gap-through-TP class. v0.7.14 removes the gross-based main-loop invariant entirely and replaces it with an entry-time-in-regular-session check — direct defense against the actual phantom signature with no false-positives. Verified against both production failures plus 6 edge cases beyond the smoke suite. 90/90 on three offline smoke files. Have you deployed v0.7.14 yet?

---

## Working notes for next Claude

- **Invariants on derived quantities are dangerous.** The gross-based invariant looked clean in isolation but missed the design intent (timestop fires before TP/SL on the timestop bar, so gap-through-TP is allowed). When adding defense-in-depth, prefer checking *structural* conditions (entry bar in session, bar list sorted, etc.) over checking *derived* outcomes (gross within bounds, P&L positive, etc.).

- **The fallback-path invariant IS sound by construction.** It's reached only after a full re-scan that confirms no bar's high reached tp_price and no bar's low reached sl_price. Last close is mathematically bounded. Don't remove this one.

- **Test against actual production failures, not just constructed cases.** The v0.7.13 zip shipped because its tests covered "near-TP timestop" but not "gap-through-TP timestop". When fixing a production failure, the test that REPRODUCES that exact failure in code should be the first thing written. The eight-case verification block in `/home/claude/v0714/test_v0714_verify.py`-style direct script (run interactively, results above) is a model — replicate this discipline for any future invariant work.

- **Audit infrastructure (`_make_time_exit` in `backtest_audit.py`) has the same kind of bug.** It's not in the production path so this isn't urgent, but it should be fixed in the same way (entry-time check instead of gross-based invariant) before any audit-based rerun is trusted.

- **Be honest about uncertainty.** When you've fixed something, say "I think this fixes it; let's verify on production." The v0.7.13 ship-and-fail cycle happened because I claimed correctness based on insufficient testing. Don't repeat it.

## v0.7.29 Rule034 merged monitor

Merged Rule034 conservative monitor into the latest combined app. Preserve all four rule tracks: Rule009 refined, Rule029, Rule033, and Rule034. Rule034 is live-shadow/monitoring only and should not replace Rule033 by default.
