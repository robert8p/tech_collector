# Tech Collector

Alpaca-backed backfill and evidence-pack exporter for S&P 500 IT intraday
research. Deployed as a FastAPI web service on Render with persistent disk
storage, matching the pattern of the S&P 500 Intraday and Coinbase Crypto
scanners.

## Scope

- **Universe:** 72 static S&P 500 Information Technology constituents,
  frozen as of 2026-04-19. Survivorship-biased by design.
- **Mode:** backfill-only. No live collection, no scheduler.
- **Deployment:** Render web service with 5 GB persistent disk for SQLite
  database and evidence packs.
- **Auth:** shared-secret (`X-API-Key` header) on all non-health endpoints.
- **Output:** evidence-pack zip matching the structure of the original
  `tech_research_export.zip`, plus extensions (leak-free sector relative
  strength, path points at 30/60/90/120 min post-scan, per-row data-quality
  markers).

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | no | Dashboard UI (HTML) |
| `GET` | `/health` | no | Liveness check (Render uses this) |
| `GET` | `/info` | no | Service info JSON |
| `POST` | `/backfill` | yes | Kick off Alpaca bar backfill (async) |
| `POST` | `/compute` | yes | Kick off feature compute (async) |
| `POST` | `/pack` | yes | Build evidence pack zip (sync) |
| `POST` | `/validate` | yes | Compare computed rows to reference CSV |
| `POST` | `/upload-reference` | yes | Upload the reference research CSV to disk |
| `GET` | `/jobs/{id}` | yes | Poll async job status |
| `GET` | `/jobs` | yes | List all jobs in memory |
| `GET` | `/packs` | yes | List pack zips on disk |
| `GET` | `/packs/{filename}` | yes | Download a pack zip |
| `POST` | `/rule009/shadow/run` | yes | Build Rule009 refined read-only live-shadow evidence pack |
| `POST` | `/rule029/shadow/run` | yes | Build Rule029 read-only live-shadow evidence pack with Rule009 overlap comparison |
| `POST` | `/rule033/shadow/run` | yes | Build Rule033 read-only live-shadow evidence pack |

All authenticated endpoints require an `X-API-Key` header matching the
`API_KEY` environment variable set in the Render dashboard.

## Deployment

See `DEPLOYMENT.md` for the step-by-step walkthrough.

## Feature definitions

See inline docstrings in `feature_computer.py`. Notes:

- **RSI** seeded at 50 at session open to match original research.
- **VWAP** session-anchored at 09:30 ET.
- **`relative_volume`** is a reconstruction. If `/validate` flags >1%
  median diff, this definition needs adjustment.
- **`sector_relative_strength`** preserves the original leak-prone
  definition for schema compatibility. **Use `rs_leakfree` for patterns.**

## Promoted / shadow rules

- Rule009 refined: 10:30 high-volatility Technology momentum benchmark with `gap_pct <= 0` and `range_expansion >= 1.0` filters.
- Rule029: 10:30 Technology pullback/reclaim live-shadow candidate; primary profile is top3 by lowest ATR reach, TP100/SL200, 25 bps evaluation slippage.
- Rule033: 13:30 Technology selloff-rebound live-shadow monitor with volume confirmation.

All rule shadow endpoints are read-only; no live trading is enabled.

## Known limitations

- Universe is frozen and survivorship-biased.
- Alpaca SIP historical minute bars may have occasional gaps; these are
  surfaced in the pack summary.
- In-memory job registry: if the service restarts mid-job, the job
  record is lost (SQLite writes are durable; re-send the request).

## v0.7.29 Rule034 merged monitor

Adds Rule034 conservative monitoring on top of the combined Rule009 refined + Rule029 + Rule033 build. Rule034 is a conservative Rule033 miss-exclusion variant: Rule033 plus `gap_filled == 0` and `atr_reach <= 10`, with UI buttons, `/rule034/shadow/run`, and promoted presets.
