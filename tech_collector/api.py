"""
FastAPI layer for the Tech Collector.

Endpoints:
    GET  /health                      — liveness check (Render uses this)
    GET  /                             — service info
    GET  /sectors                      — list of supported GICS sectors
    GET  /sector-status                — per-sector data coverage summary
    POST /backfill                     — kick off bar backfill (async)
    POST /compute                      — kick off feature compute (async)
    POST /pack                         — build evidence pack (sync, < 30s)
    POST /export-scan-rows             — scan-rows CSV zip (sync)
    POST /export-scan-rows-parquet     — scan-rows Parquet single file (sync)
    POST /generate-research-pack       — one-click backfill+compute+export (async)
    POST /validate                     — validate computed rows (sync)
    GET  /jobs/{job_id}                — poll job status
    GET  /jobs                         — list all jobs
    GET  /packs                        — list available evidence packs
    GET  /packs/{filename}             — download an evidence pack zip

Auth: all endpoints except /health, /, /info, and /sectors require an
X-API-Key header matching the API_KEY environment variable.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Depends, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
import pandas as pd

from . import (
    backtest, collector, config, exporter, feature_computer, jobs,
    rule_tester, storage, validate,
)
from .universes import SECTOR_UNIVERSES

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Tech Collector",
    version=config.APP_VERSION,
    description=(
        "Backfill-only evidence-pack generator for S&P 500 IT intraday "
        "research. Pulls Alpaca SIP bars, computes research-schema features, "
        "exports zipped evidence packs."
    ),
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _require_api_key(x_api_key: str | None = Header(default=None)):
    expected = os.environ.get(config.API_KEY_ENV)
    if not expected:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Server not configured: {config.API_KEY_ENV} env var not set. "
                "Set it in Render dashboard."
            ),
        )
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class DateRange(BaseModel):
    start: str = Field(..., description="YYYY-MM-DD")
    end: str = Field(..., description="YYYY-MM-DD")
    sector: str | None = Field(
        default=None,
        description=(
            "GICS sector name (e.g. 'Information Technology'). "
            "If omitted, server falls back to DEFAULT_SECTOR env var."
        ),
    )


class ValidateRequest(BaseModel):
    research_csv_path: str = Field(
        ..., description="Path on Render disk to reference tech_research_dataset.csv"
    )
    sample: int = 500
    sector: str | None = Field(
        default=None,
        description=(
            "GICS sector filter applied to computed rows before comparison. "
            "If omitted, server falls back to DEFAULT_SECTOR env var."
        ),
    )


class ResearchPackRequest(BaseModel):
    start: str = Field(..., description="YYYY-MM-DD")
    end: str = Field(..., description="YYYY-MM-DD")
    sector: str | None = Field(
        default=None,
        description=(
            "GICS sector. Ignored when run_all_sectors=True."
        ),
    )
    run_all_sectors: bool = Field(
        default=False,
        description=(
            "If True, iterate all 11 GICS sectors in sequence. Long-running "
            "(hours). One pack per sector is produced."
        ),
    )


# -- Rule tester request schemas --
class PredicateSchema(BaseModel):
    feature: str
    op: str = Field(..., description="one of: >, >=, <, <=, ==, !=")
    value: float


class RuleSchema(BaseModel):
    id: str
    sector: str
    target: str
    predicates: list[PredicateSchema]
    notes: str = ""


class TestRulesRequest(BaseModel):
    rules: list[RuleSchema]
    start: str | None = Field(
        default=None,
        description="YYYY-MM-DD. If omitted, uses all available data for each rule's sector.",
    )
    end: str | None = Field(default=None, description="YYYY-MM-DD")
    n_folds: int = Field(
        default=5,
        description="Number of rolling-origin folds. 0 to skip fold analysis.",
    )
    fold_mode: str = Field(
        default="expanding_window",
        description=(
            "Fold generation strategy. 'expanding_window' uses n_folds "
            "contiguous time slices. 'year_based' uses one fold per calendar "
            "year (ignores n_folds); correct for multi-year data."
        ),
    )
    apply_filters: bool = Field(
        default=True,
        description="Apply the standard row-level filters (drop 09:30, thin-tape).",
    )
    track: bool = Field(
        default=False,
        description="If True, persist each rule to tracked_rules and record this run in rule_test_runs.",
    )
    save_csv: bool = Field(
        default=True,
        description="If True, save a flat per-rule CSV to evidence_packs for download.",
    )
    regime_min_lift: float = Field(
        default=1.3,
        description=(
            "Lift threshold for the regime_consistent flag in the fold summary. "
            "A rule is regime-consistent when its OOS lift >= this value in "
            "≥80% of folds. Default 1.3 (was 1.0 in v0.5.0 — too permissive; "
            "a rule barely beating the base rate shouldn't be flagged "
            "indistinguishably from a strong rule)."
        ),
    )


class ChainedBackfillRequest(BaseModel):
    start: str = Field(..., description="YYYY-MM-DD, inclusive")
    end: str = Field(..., description="YYYY-MM-DD, inclusive")
    sector: str = Field(..., description="GICS sector name")
    months_per_segment: int = Field(
        default=6,
        description=(
            "Segment width. 6 is the Render-safe default (matches /backfill "
            "for single-segment parity); smaller values give more granular "
            "progress updates at the cost of more orchestration overhead."
        ),
    )
    discard_raw_bars: bool = Field(
        default=True,
        description=(
            "If True, delete raw_bars for the segment's date range after "
            "compute succeeds. Keeps DB size bounded. SPY bars are preserved. "
            "Re-computing features on discarded data requires re-backfilling."
        ),
    )


class TrackRuleRequest(BaseModel):
    rules: list[RuleSchema]


class RetireRuleRequest(BaseModel):
    rule_id: str


def _resolve_sector(requested: str | None) -> str:
    """Resolve a sector name to a validated, server-known sector.

    Falls back to config.DEFAULT_SECTOR if none provided. Raises HTTP 400
    if the requested sector isn't one of the 11 GICS sectors on record.
    """
    resolved = requested or config.DEFAULT_SECTOR
    if resolved not in SECTOR_UNIVERSES:
        known = sorted(SECTOR_UNIVERSES.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sector {resolved!r}. Known sectors: {known}",
        )
    return resolved


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Liveness check — always returns 200 if the process is up."""
    return {"status": "ok", "version": config.APP_VERSION}


@app.get("/", response_class=HTMLResponse)
def root():
    """Dashboard UI — served when a browser hits the root."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        # Fallback if the static file was lost during deploy
        return HTMLResponse(
            "<h1>Tech Collector</h1><p>Dashboard HTML missing. "
            "See <a href='/info'>/info</a> for service status.</p>",
            status_code=200,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/source-version")
def source_version():
    """v0.7.9: report SHA256 of loaded module source files. Diagnostic for
    "is the deploy actually serving the new code?" If the deploy mechanism
    didn't replace backtest.py or feature_computer.py, this endpoint will
    return a stale hash that doesn't match the local development copy.

    Returns hashes for the modules that have had material engine changes:
    backtest.py, feature_computer.py, api.py, storage.py, backtest_audit.py.
    Also returns the file path each module was actually loaded from, so a
    deploy mismatch is visible.

    Auth: public; the SHA256 of source code is not sensitive.
    """
    import hashlib
    import inspect
    from . import backtest as _bt
    from . import feature_computer as _fc
    from . import storage as _st
    from . import backtest_audit as _ba
    from . import rule_tester as _rt

    out = {
        "version": config.APP_VERSION,
        "modules": {},
    }
    for name, mod in [
        ("backtest", _bt),
        ("feature_computer", _fc),
        ("api", __import__("tech_collector.api", fromlist=["api"])),
        ("storage", _st),
        ("backtest_audit", _ba),
        ("rule_tester", _rt),
    ]:
        try:
            src_path = inspect.getsourcefile(mod) or "<unknown>"
            with open(src_path, "rb") as f:
                src = f.read()
            out["modules"][name] = {
                "path": src_path,
                "size_bytes": len(src),
                "sha256": hashlib.sha256(src).hexdigest(),
            }
        except Exception as e:
            out["modules"][name] = {"error": str(e)}
    # Also report what _utc_hour_to_et resolves to — if it's the noop
    # trap, v0.7.8+ source is loaded; if it's the live function, older.
    try:
        fn = getattr(_bt, "_utc_hour_to_et", None)
        out["utc_hour_to_et_check"] = {
            "exists": fn is not None,
            "name": getattr(fn, "__name__", None) if fn else None,
            "is_v078_trap": (
                getattr(fn, "__name__", "") == "_dead_removed_utc_hour_to_et_noop"
                if fn else False
            ),
        }
    except Exception as e:
        out["utc_hour_to_et_check"] = {"error": str(e)}
    return out


@app.get("/info")
def info():
    """Service info as JSON (moved from / to make room for the dashboard)."""
    return {
        "app": config.APP_NAME,
        "version": config.APP_VERSION,
        "default_sector": config.DEFAULT_SECTOR,
        "default_universe_size": len(config.UNIVERSE),
        "supported_sectors": list(config.SUPPORTED_SECTORS),
        "mode": "backfill-only",
        "data_dir": os.environ.get("DATA_DIR", "."),
        "endpoints": {
            "dashboard": "GET /",
            "health": "GET /health",
            "info": "GET /info",
            "source-version": "GET /source-version  (v0.7.9, deploy verification)",
            "raw-bars-coverage": "GET /raw-bars/coverage?symbol=X&date=YYYY-MM-DD  (v0.7.12, missing-bars diagnostic)",
            "sectors": "GET /sectors",
            "sector-status": "GET /sector-status",
            "backfill": "POST /backfill  (async, returns job_id)",
            "backfill-chained": "POST /backfill-chained  (v0.5.0, async, multi-year chained)",
            "compute":  "POST /compute   (async, returns job_id)",
            "pack":     "POST /pack      (sync)",
            "export-scan-rows": "POST /export-scan-rows  (sync, zip)",
            "export-scan-rows-parquet": "POST /export-scan-rows-parquet  (sync, single file)",
            "generate-research-pack": "POST /generate-research-pack  (async, one-click)",
            "validate": "POST /validate  (sync)",
            "upload-reference": "POST /upload-reference  (multipart)",
            "rules-test":    "POST /rules/test           (v0.4.0, sync, returns JSON + CSV download)",
            "rules-track":   "POST /rules/track          (v0.4.0, persist without testing)",
            "rules-retire":  "POST /rules/retire         (v0.4.0)",
            "rules-tracked": "GET  /rules/tracked        (v0.4.0, list active rules)",
            "rules-history": "GET  /rules/{id}/history   (v0.4.0, decay tracking)",
            "backtest-batch-run": "POST /backtest/batch/run  (v0.7.22, async sequential preset batch)",
            "jobs":     "GET /jobs/{id}  or  GET /jobs",
            "packs":    "GET /packs  or  GET /packs/{filename}",
        },
    }


@app.get("/sectors")
def sectors():
    """List the 11 supported GICS sectors with their universe sizes.

    Public (no auth) so the dashboard dropdown can populate itself without
    requiring an API key; the sector names themselves are not sensitive.
    """
    return {
        "default_sector": config.DEFAULT_SECTOR,
        "sectors": [
            {"name": name, "symbol_count": len(tickers)}
            for name, tickers in SECTOR_UNIVERSES.items()
        ],
    }


# ---------------------------------------------------------------------------
# Authenticated endpoints
# ---------------------------------------------------------------------------
@app.post("/backfill", dependencies=[Depends(_require_api_key)])
def backfill(rng: DateRange):
    """Start a backfill job. Returns immediately with job_id; poll /jobs/{id}."""
    resolved_sector = _resolve_sector(rng.sector)
    job = jobs.registry.create(
        "backfill",
        params={"start": rng.start, "end": rng.end, "sector": resolved_sector},
    )
    jobs.registry.run_async(
        job, collector.collect_range,
        start_date=rng.start, end_date=rng.end, db_path=config.DB_PATH,
        sector=resolved_sector,
    )
    return {"job_id": job.job_id, "status": "started",
            "sector": resolved_sector,
            "poll": f"/jobs/{job.job_id}"}


@app.post("/backfill-chained", dependencies=[Depends(_require_api_key)])
def backfill_chained(req: ChainedBackfillRequest):
    """Chained multi-year backfill: loop backfill → compute in contiguous segments.

    Designed for Render Pro Plus tier, which can run multi-hour jobs without
    idle-kill. Each segment runs backfill+compute, then optionally discards
    raw_bars to keep DB size bounded. Research rows are committed per
    segment, so a partial run leaves valid data for completed segments.

    Returns immediately with a job_id. The job object's params track the
    total segment count; progress shows up in /jobs/{id} via the underlying
    collector/feature_computer return values captured per segment.
    """
    resolved_sector = _resolve_sector(req.sector)
    # Validate the date range up front — gives a 400 instead of an async
    # failure if the user typo'd the dates.
    try:
        segments = exporter._month_segments(
            req.start, req.end, months_per_segment=req.months_per_segment,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not segments:
        raise HTTPException(
            status_code=400,
            detail=f"no segments produced for range {req.start}..{req.end}",
        )

    job = jobs.registry.create(
        "backfill-chained",
        params={
            "start": req.start, "end": req.end, "sector": resolved_sector,
            "months_per_segment": req.months_per_segment,
            "discard_raw_bars": req.discard_raw_bars,
            "n_segments": len(segments),
            "segment_boundaries": segments,
        },
    )
    jobs.registry.run_async(
        job, exporter.chained_long_backfill,
        start_date=req.start, end_date=req.end, sector=resolved_sector,
        db_path=config.DB_PATH,
        months_per_segment=req.months_per_segment,
        discard_raw_bars=req.discard_raw_bars,
    )
    return {
        "job_id": job.job_id, "status": "started",
        "sector": resolved_sector,
        "n_segments": len(segments),
        "first_segment": list(segments[0]),
        "last_segment": list(segments[-1]),
        "discard_raw_bars": req.discard_raw_bars,
        "poll": f"/jobs/{job.job_id}",
        "estimated_wall_minutes": len(segments) * 30,  # rough, 30 min/segment
    }


@app.post("/compute", dependencies=[Depends(_require_api_key)])
def compute(rng: DateRange):
    """Start a compute job (raw bars -> research rows). Returns job_id."""
    resolved_sector = _resolve_sector(rng.sector)
    job = jobs.registry.create(
        "compute",
        params={"start": rng.start, "end": rng.end, "sector": resolved_sector},
    )
    jobs.registry.run_async(
        job, feature_computer.compute_range,
        start_date=rng.start, end_date=rng.end, db_path=config.DB_PATH,
        sector=resolved_sector,
    )
    return {"job_id": job.job_id, "status": "started",
            "sector": resolved_sector,
            "poll": f"/jobs/{job.job_id}"}


@app.post("/pack", dependencies=[Depends(_require_api_key)])
def pack(rng: DateRange):
    """Export an evidence pack zip for a date range. Synchronous (fast)."""
    resolved_sector = _resolve_sector(rng.sector)
    try:
        path = exporter.export_pack(
            start_date=rng.start, end_date=rng.end,
            out_dir=config.EVIDENCE_PACK_DIR, db_path=config.DB_PATH,
            sector=resolved_sector,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pack failed: {e}")
    return {
        "pack_path": str(path),
        "pack_filename": path.name,
        "sector": resolved_sector,
        "download_url": f"/packs/{path.name}",
    }


@app.post("/export-scan-rows", dependencies=[Depends(_require_api_key)])
def export_scan_rows_endpoint(rng: DateRange):
    """Export a small zip containing only the scan-row CSV (no raw bars).

    Purpose: cross-month pattern analysis. Fits in one Claude upload for
    any realistic date range — ~1 MB per month of data.
    """
    resolved_sector = _resolve_sector(rng.sector)
    try:
        path = exporter.export_scan_rows(
            start_date=rng.start, end_date=rng.end,
            out_dir=config.EVIDENCE_PACK_DIR, db_path=config.DB_PATH,
            sector=resolved_sector,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    size_bytes = path.stat().st_size
    return {
        "pack_path": str(path),
        "pack_filename": path.name,
        "sector": resolved_sector,
        "download_url": f"/packs/{path.name}",
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_000_000, 2),
    }


@app.post("/export-scan-rows-parquet", dependencies=[Depends(_require_api_key)])
def export_scan_rows_parquet_endpoint(rng: DateRange):
    """Export scan rows as a single Parquet file (no zip wrapper).

    Use this instead of /export-scan-rows when you want one small file
    suitable for direct upload to Claude. Parquet compresses this schema
    ~10x vs CSV so a full 2yr sector export typically lands in 8-15 MB.
    """
    resolved_sector = _resolve_sector(rng.sector)
    try:
        path = exporter.export_scan_rows_parquet(
            start_date=rng.start, end_date=rng.end,
            out_dir=config.EVIDENCE_PACK_DIR, db_path=config.DB_PATH,
            sector=resolved_sector,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    size_bytes = path.stat().st_size
    return {
        "pack_path": str(path),
        "pack_filename": path.name,
        "sector": resolved_sector,
        "download_url": f"/packs/{path.name}",
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / 1_000_000, 2),
    }


@app.post("/generate-research-pack", dependencies=[Depends(_require_api_key)])
def generate_research_pack_endpoint(req: ResearchPackRequest):
    """One-click: backfill -> compute -> parquet export, for a sector.

    Async — returns a job_id to poll. The underlying orchestrator skips
    backfill and compute when the DB already has >=95% coverage for the
    requested (sector, date range), so repeated clicks are cheap once
    the data is in place.

    If run_all_sectors=True, iterates all 11 GICS sectors serially.
    Expect 6-10 hours for a cold-DB, full-range, all-sectors run.
    """
    if req.run_all_sectors:
        job = jobs.registry.create(
            "generate-all-sectors",
            params={"start": req.start, "end": req.end,
                    "run_all_sectors": True},
        )
        jobs.registry.run_async(
            job, exporter.generate_research_pack_all_sectors,
            start_date=req.start, end_date=req.end,
            db_path=config.DB_PATH, out_dir=config.EVIDENCE_PACK_DIR,
        )
        return {"job_id": job.job_id, "status": "started",
                "scope": "all_sectors",
                "poll": f"/jobs/{job.job_id}"}

    resolved_sector = _resolve_sector(req.sector)
    job = jobs.registry.create(
        "generate-research-pack",
        params={"start": req.start, "end": req.end,
                "sector": resolved_sector},
    )
    jobs.registry.run_async(
        job, exporter.generate_research_pack,
        start_date=req.start, end_date=req.end,
        sector=resolved_sector,
        db_path=config.DB_PATH, out_dir=config.EVIDENCE_PACK_DIR,
    )
    return {"job_id": job.job_id, "status": "started",
            "sector": resolved_sector,
            "poll": f"/jobs/{job.job_id}"}


@app.get("/sector-status")
def sector_status():
    """Per-sector data coverage summary.

    Public (no auth) so the dashboard can render the sector dropdown with
    "backfilled through <date>" labels without requiring the API key.
    Returns one entry per GICS sector. Sectors with no data get
    row_count=0 and null dates.
    """
    from .universes import SECTOR_UNIVERSES
    with storage.connect(config.DB_PATH) as conn:
        present = {s["sector"]: s for s in storage.sector_status(conn)}
    out = []
    for name, tickers in SECTOR_UNIVERSES.items():
        if name in present:
            entry = dict(present[name])
            entry["symbol_count"] = len(tickers)
            entry["has_data"] = True
        else:
            entry = {
                "sector": name, "earliest_date": None, "latest_date": None,
                "row_count": 0, "symbol_count": len(tickers), "has_data": False,
                "null_target_peak_50bps": 0, "null_target_peak_75bps": 0,
            }
        out.append(entry)
    return {"sectors": out}


# ---------------------------------------------------------------------------
# Rule tester endpoints (v0.4.0)
# ---------------------------------------------------------------------------
@app.post("/rules/test", dependencies=[Depends(_require_api_key)])
def rules_test(req: TestRulesRequest):
    """Evaluate a rule bundle against stored scan rows.

    Each rule specifies its own (sector, target), so a single call can test
    rules across multiple sectors. Reads from the collector's SQLite
    directly — no need to upload a pack.

    Returns the full JSON result. When `save_csv=True` (default), also
    writes a flat per-rule CSV to the evidence_packs directory and
    includes a download_url in the response. The CSV is the right format
    to share back into a Claude analysis session.
    """
    try:
        rules = [rule_tester.Rule.from_dict(r.model_dump()) for r in req.rules]
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=f"Bad rule: {e}")

    # Rules may span multiple sectors; load scan rows per-sector and pass the
    # combined frame to test_rule_bundle, which re-filters per rule anyway.
    sectors_needed = sorted({r.sector for r in rules})
    frames = []
    for sector in sectors_needed:
        try:
            df_s = rule_tester.load_scan_rows_from_db(
                config.DB_PATH, sector,
                start_date=req.start, end_date=req.end,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Load failed for {sector!r}: {e}")
        if df_s.empty:
            logger.warning(f"no scan rows for sector={sector!r} in range")
        frames.append(df_s)
    import pandas as pd
    df_all = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    try:
        result = rule_tester.test_rule_bundle(
            rules=rules,
            df=df_all,
            n_folds=req.n_folds,
            fold_mode=req.fold_mode,
            apply_filters=req.apply_filters,
            regime_min_lift=req.regime_min_lift,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Test failed: {e}")

    if req.track:
        try:
            for rule in rules:
                rule_tester.track_rule(config.DB_PATH, rule)
            rule_tester.record_test_run(config.DB_PATH, result)
        except Exception as e:
            # Don't fail the response just because tracking failed; flag it.
            result["tracking_error"] = f"{type(e).__name__}: {e}"

    if req.save_csv:
        try:
            import pandas as pd
            # Build a flat per-rule summary row: one row per rule per fold,
            # plus an "overall" row per rule. This is the CSV that gets
            # shared back into Claude.
            rows = []
            for entry in result.get("rules", []):
                if "error" in entry:
                    rows.append({
                        "rule_id": entry["rule_id"], "scope": "overall",
                        "error": entry["error"],
                    })
                    continue
                base = {
                    "rule_id": entry["rule_id"],
                    "sector": entry["rule"]["sector"],
                    "target": entry["rule"]["target"],
                    "predicates": "; ".join(
                        f"{p['feature']}{p['op']}{p['value']}"
                        for p in entry["rule"]["predicates"]
                    ),
                    "n_rows_evaluated": entry.get("n_rows_evaluated"),
                    "data_start": entry.get("date_range", [None, None])[0],
                    "data_end": entry.get("date_range", [None, None])[1],
                }
                ov = entry.get("overall", {})
                fd = entry.get("filter_diagnostics", {}) or {}
                rows.append({**base, "scope": "overall",
                             "support": ov.get("support"),
                             "precision": ov.get("precision"),
                             "lift": ov.get("lift"),
                             "base_rate": ov.get("base_rate"),
                             "days_firing": ov.get("days_firing"),
                             "max_day_fraction": ov.get("max_day_fraction"),
                             "specificity": ov.get("specificity"),
                             "probability_shift": ov.get("probability_shift"),
                             "recall": ov.get("recall"),
                             # v0.5.1: surface filter diagnostics on the
                             # overall row so the CSV has everything needed
                             # to diagnose a silent-drop issue without
                             # re-running.
                             "rows_input": fd.get("rows_input"),
                             "rows_with_null_target": fd.get("rows_with_null_target"),
                             "rows_dropped_0930": fd.get("rows_dropped_0930"),
                             "rows_dropped_thin_tape": fd.get("rows_dropped_thin_tape"),
                             "rows_final": fd.get("rows_final"),
                             "warning": fd.get("warning", ""),
                             })
                for f in entry.get("folds", []):
                    ofold = f.get("oos", {})
                    rows.append({**base,
                                 "scope": f"fold_{f['fold']}_oos",
                                 "fold_label": f.get("fold_label", ""),
                                 "fold_train_span": f"{f['train_span'][0]}..{f['train_span'][1]}",
                                 "fold_oos_span": f"{f['oos_span'][0]}..{f['oos_span'][1]}",
                                 "support": ofold.get("support"),
                                 "precision": ofold.get("precision"),
                                 "lift": ofold.get("lift"),
                                 "base_rate": ofold.get("base_rate"),
                                 "days_firing": ofold.get("days_firing"),
                                 "max_day_fraction": ofold.get("max_day_fraction"),
                                 "specificity": ofold.get("specificity"),
                                 "probability_shift": ofold.get("probability_shift"),
                                 "recall": ofold.get("recall"),
                                 })
                fs = entry.get("fold_summary")
                if fs:
                    rows.append({**base, "scope": "fold_summary",
                                 "precision": fs.get("oos_precision_median"),
                                 "lift": fs.get("oos_lift_median"),
                                 "support": fs.get("oos_support_median"),
                                 "precision_min": fs.get("oos_precision_min"),
                                 "precision_max": fs.get("oos_precision_max"),
                                 "lift_min": fs.get("oos_lift_min"),
                                 "lift_max": fs.get("oos_lift_max"),
                                 "regime_consistent": fs.get("regime_consistent"),
                                 })
            df_out = pd.DataFrame(rows)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            fname = f"rule_test_{ts}.csv"
            out_dir = Path(config.EVIDENCE_PACK_DIR)
            out_dir.mkdir(parents=True, exist_ok=True)
            fpath = out_dir / fname
            df_out.to_csv(fpath, index=False)
            # Also save the full JSON for reproducibility
            jname = f"rule_test_{ts}.json"
            jpath = out_dir / jname
            import json as _json
            with open(jpath, "w") as f:
                _json.dump(result, f, indent=2, default=str)
            result["csv_filename"] = fname
            result["csv_download_url"] = f"/packs/{fname}"
            result["json_filename"] = jname
            result["json_download_url"] = f"/packs/{jname}"
        except Exception as e:
            result["csv_error"] = f"{type(e).__name__}: {e}"

    return result


@app.post("/rules/track", dependencies=[Depends(_require_api_key)])
def rules_track(req: TrackRuleRequest):
    """Persist one or more rules to tracked_rules without running a test."""
    try:
        rules = [rule_tester.Rule.from_dict(r.model_dump()) for r in req.rules]
        for rule in rules:
            rule_tester.track_rule(config.DB_PATH, rule)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Track failed: {e}")
    return {"tracked": len(rules), "rule_ids": [r.id for r in rules]}


@app.post("/rules/retire", dependencies=[Depends(_require_api_key)])
def rules_retire(req: RetireRuleRequest):
    """Mark a tracked rule as retired. Test history is preserved."""
    try:
        rule_tester.retire_rule(config.DB_PATH, req.rule_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Retire failed: {e}")
    return {"retired": req.rule_id}


@app.get("/rules/tracked", dependencies=[Depends(_require_api_key)])
def rules_tracked(status: str | None = "active"):
    """List tracked rules. Pass status=null to list retired too."""
    return {
        "rules": rule_tester.list_tracked_rules(config.DB_PATH, status=status),
    }


@app.get("/rules/{rule_id}/history", dependencies=[Depends(_require_api_key)])
def rules_history(rule_id: str):
    """Return the list of test runs recorded for a rule, oldest first.

    Each run has its overall precision/lift/support and the fold summary.
    Use this to watch for rule decay over time.
    """
    runs = rule_tester.rule_history(config.DB_PATH, rule_id)
    if not runs:
        return {"rule_id": rule_id, "runs": [], "note": "no runs recorded"}
    return {"rule_id": rule_id, "runs": runs}


@app.post("/pack-monthly", dependencies=[Depends(_require_api_key)])
def pack_monthly(rng: DateRange):
    """Export one evidence pack per calendar month within [start, end].

    Useful when a full-range pack would exceed Claude's 30MB upload limit.
    Each monthly pack covers the 1st through the last day of that month,
    clipped to the requested range.

    Returns a list of pack filenames with their download URLs.
    """
    from datetime import date as _date
    from calendar import monthrange
    resolved_sector = _resolve_sector(rng.sector)
    try:
        start = _date.fromisoformat(rng.start)
        end = _date.fromisoformat(rng.end)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Bad date: {e}")
    if start > end:
        raise HTTPException(status_code=400, detail="start after end")

    results = []
    errors = []
    # Iterate by month — year/month tuples
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        month_first = _date(y, m, 1)
        month_last = _date(y, m, monthrange(y, m)[1])
        chunk_start = max(start, month_first)
        chunk_end = min(end, month_last)
        try:
            path = exporter.export_pack(
                start_date=chunk_start.isoformat(),
                end_date=chunk_end.isoformat(),
                out_dir=config.EVIDENCE_PACK_DIR,
                db_path=config.DB_PATH,
                sector=resolved_sector,
            )
            size_bytes = path.stat().st_size
            results.append({
                "month": f"{y:04d}-{m:02d}",
                "pack_filename": path.name,
                "download_url": f"/packs/{path.name}",
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / 1_000_000, 2),
                "date_range": [chunk_start.isoformat(), chunk_end.isoformat()],
            })
        except Exception as e:
            errors.append({
                "month": f"{y:04d}-{m:02d}",
                "error": f"{type(e).__name__}: {e}",
            })
        # Advance to next month
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1

    return {
        "sector": resolved_sector,
        "packs": results,
        "errors": errors,
        "total_packs": len(results),
        "total_size_mb": round(sum(r["size_bytes"] for r in results) / 1_000_000, 2),
    }


@app.post("/validate", dependencies=[Depends(_require_api_key)])
def validate_endpoint(req: ValidateRequest):
    """Compare computed rows to a reference research CSV on the disk."""
    resolved_sector = _resolve_sector(req.sector)
    csv_path = Path(req.research_csv_path)
    if not csv_path.exists():
        raise HTTPException(status_code=400, detail=f"CSV not found: {csv_path}")
    try:
        report = validate.compare(
            csv_path, Path(config.DB_PATH), req.sample,
            sector=resolved_sector,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validate failed: {e}")
    bad = [
        f for f, s in report.get("feature_stats", {}).items()
        if (s.get("median_rel_diff_pct") or 0) > 1.0
    ]
    return {"sector": resolved_sector, "report": report,
            "features_above_1pct_median_diff": bad,
            "passed": len(bad) == 0}


@app.post("/reset-db", dependencies=[Depends(_require_api_key)])
def reset_db():
    """DESTRUCTIVE: delete the SQLite database and re-initialize it.
    Use when feature definitions have changed and you need to force a
    fresh backfill. Evidence packs on disk are NOT deleted.
    """
    db_path = Path(config.DB_PATH)
    deleted = []
    for p in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if p.exists():
            p.unlink()
            deleted.append(str(p))
    storage.init_schema(config.DB_PATH)
    return {"deleted": deleted, "reinitialized": str(db_path)}


@app.post("/upload-reference", dependencies=[Depends(_require_api_key)])
async def upload_reference(file: UploadFile = File(...)):
    """Upload the reference research CSV to the persistent disk so
    /validate can find it. Single-step replacement for the 'commit to
    GitHub, then copy in Render Shell' workaround.

    Usage:
        curl -X POST -H "X-API-Key: ..." \\
             -F "file=@tech_research_dataset.csv" \\
             https://YOUR-SERVICE.onrender.com/upload-reference
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400,
                            detail="Only .csv files accepted")
    # Always save under a fixed name; /validate reads from this path
    dest = Path(os.environ.get("DATA_DIR", ".")) / "tech_research_dataset.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Stream to disk in chunks so large files don't blow up memory
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)  # 1 MB chunks
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)

    return {
        "saved_to": str(dest),
        "size_bytes": total,
        "original_filename": file.filename,
        "note": "Use this path in the /validate endpoint: research_csv_path",
    }


@app.get("/jobs/{job_id}", dependencies=[Depends(_require_api_key)])
def get_job(job_id: str):
    job = jobs.registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.get("/jobs", dependencies=[Depends(_require_api_key)])
def list_jobs():
    return [j.to_dict() for j in jobs.registry.list()]


@app.get("/packs", dependencies=[Depends(_require_api_key)])
def list_packs():
    pack_dir = Path(config.EVIDENCE_PACK_DIR)
    if not pack_dir.exists():
        return {"packs": []}
    # Include zip (full packs), parquet (scan-rows single-file),
    # csv + json (rule test artifacts)
    files = sorted(
        list(pack_dir.glob("*.zip"))
        + list(pack_dir.glob("*.parquet"))
        + list(pack_dir.glob("*.csv"))
        + list(pack_dir.glob("*.json"))
    )
    return {
        "packs": [
            {
                "filename": z.name,
                "size_bytes": z.stat().st_size,
                "modified_at_utc": datetime.fromtimestamp(
                    z.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
                "download_url": f"/packs/{z.name}",
            }
            for z in files
        ]
    }


@app.get("/packs/{filename}", dependencies=[Depends(_require_api_key)])
def download_pack(filename: str):
    # Prevent directory traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = Path(config.EVIDENCE_PACK_DIR) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Pack not found")
    media_type = (
        "application/vnd.apache.parquet"
        if filename.endswith(".parquet")
        else "text/csv" if filename.endswith(".csv")
        else "application/json" if filename.endswith(".json")
        else "application/zip"
    )
    return FileResponse(
        path, media_type=media_type, filename=filename,
    )


# ---------------------------------------------------------------------------
# Backtest endpoints (v0.6.0)
# ---------------------------------------------------------------------------
class BacktestPredicate(BaseModel):
    feature: str
    op: str
    value: float


class BacktestRuleInline(BaseModel):
    id: str
    sector: str
    target: str
    predicates: list[BacktestPredicate]


class ConditionalExitSpec(BaseModel):
    """v0.7.0: branch of a conditional-exit spec.

    When `feature` compares `op` against `value`, this branch's
    tp_bps/sl_bps/position_size overrides apply. Branches are evaluated in
    request order; first match wins. Unmatched signals fall through to the
    top-level tp_bps/sl_bps at full size.
    """
    feature: str = Field(..., description="Feature name to test (e.g. 'gap_filled')")
    op: str = Field(..., description="Comparison op: == != < <= > >=")
    value: float = Field(..., description="Value to compare against")
    tp_bps: float = Field(..., ge=1.0, le=500.0)
    sl_bps: float = Field(..., ge=1.0, le=1000.0)
    position_size: float = Field(
        default=1.0, ge=0.0, le=5.0,
        description="Size multiplier applied to net_return_bps (1.0 = full).",
    )
    label: str = Field(
        default="",
        description="Human-readable branch label for reporting (e.g. 'gap_open').",
    )


class BacktestRunRequest(BaseModel):
    rule: BacktestRuleInline = Field(
        ...,
        description=(
            "Inline rule spec. Use the same format as the rule tester — id, "
            "sector, target, predicates. Can be copied from a tracked rule "
            "or constructed fresh."
        ),
    )
    tp_bps: float = Field(
        ..., ge=1.0, le=500.0,
        description="Take-profit in bps. 50 = 0.5% above entry.",
    )
    sl_bps: float = Field(
        ..., ge=1.0, le=1000.0,
        description=(
            "Stop-loss in bps. 50 = 0.5% drawdown from entry. Wide stops "
            "(100-300 bps) are intentionally supported — the miss-set analysis "
            "showed median winning-trade drawdown is 100 bps, so tight stops "
            "blow out winners."
        ),
    )
    timestop_et: str | None = Field(
        default="15:50",
        description=(
            "HH:MM in ET at/after which we flatten unconditionally. "
            "v0.7.7: default is now 15:50 (was 15:30) to give late trades "
            "more room. Pass null (or empty string) to disable the timestop "
            "entirely — trade only exits on TP or SL. When disabled, if "
            "neither level is hit by end-of-session bars, the trade exits "
            "at the last available bar's close as a TIME exit."
        ),
    )
    slippage_bps: float = Field(
        default=10.0, ge=0.0, le=100.0,
        description="Round-trip slippage in bps (half applied at entry, half at exit).",
    )
    spy_regime_filter: float | None = Field(
        default=None,
        description=(
            "Skip signals when SPY's return since market open is below this "
            "decimal threshold (e.g. -0.002 for -0.2%). Null = filter off. "
            "Motivation: miss-set analysis showed misses cluster on SPY-weak "
            "days; excluding these may reduce drawdown."
        ),
    )
    symbol_exclude: list[str] = Field(
        default_factory=list,
        description="List of symbols to exclude entirely (e.g. ['SMCI','LITE']).",
    )
    start_date: str | None = Field(default=None, description="YYYY-MM-DD. Defaults to all available.")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD. Defaults to all available.")
    filter_mode: str = Field(
        default="standard",
        description=(
            "Backtest signal-row filter mode. 'standard' keeps legacy behavior "
            "and drops 09:30 plus thin-tape rows. 'target_only' keeps 09:30 rows "
            "but still drops rows with NULL target/scan_price. Use target_only for "
            "opening-scan rules."
        ),
    )
    min_exit_minutes: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "Minimum minutes after the entry timestamp before TP/SL exits are allowed. "
            "Use 1 to audit 09:30 rules without same-minute exits. Default 0 preserves legacy behavior."
        ),
    )
    entry_delay_minutes: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "Delay entry after the scan timestamp by N minutes. 0 = legacy scan_price entry. "
            "1 = enter at the next minute bar open, useful for conservative 09:30 execution audits."
        ),
    )
    max_signals_per_day: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "v0.7.23: optional shortlist cap. If set, keep only top/bottom N signals "
            "per signal date using rank_feature after the rule fires."
        ),
    )
    rank_feature: str | None = Field(
        default=None,
        description=(
            "v0.7.23: scan-time feature used for max_signals_per_day ranking, e.g. "
            "'relative_volume', 'ret_vs_spy', 'momentum', or 'intraday_range_position'. "
            "Do not use outcome/leaky columns."
        ),
    )
    rank_direction: str = Field(
        default="desc",
        description="v0.7.23: 'desc' keeps largest rank_feature values; 'asc' keeps smallest.",
    )
    just_in_time_backfill: bool = Field(
        default=True,
        description=(
            "If true, pull raw minute bars from Alpaca for any (symbol,date) "
            "that has a signal but no raw_bars in the DB. Required for most "
            "historical backtests since raw_bars are typically discarded."
        ),
    )
    delete_raw_bars_after: bool = Field(
        default=False,
        description=(
            "If true, delete raw_bars for each (symbol,date) after simulation. "
            "Useful for keeping storage proportional to one trading day."
        ),
    )
    conditional_exits: list[ConditionalExitSpec] = Field(
        default_factory=list,
        description=(
            "v0.7.0: conditional TP/SL branches. If non-empty, each signal is "
            "matched against branches in order; first match wins. "
            "Example for 'Option C': [{'feature':'gap_filled','op':'==','value':0,"
            "'tp_bps':75,'sl_bps':100,'position_size':1.0,'label':'gap_open'}] — "
            "unmatched signals fall through to top-level tp_bps/sl_bps. "
            "position_size scales net_return_bps (0.5 = half-size trade, 1.0 = full)."
        ),
    )


class BacktestBatchRunRequest(BaseModel):
    """v0.7.22: a batch of backtest presets to run sequentially.

    The UI normalises uploaded presets before posting, but the server also
    accepts common preset aliases so a batch JSON can contain the same objects
    users download from the dashboard.
    """
    label: str | None = Field(default=None, description="Optional human-readable batch label.")
    presets: list[dict] = Field(..., description="List of backtest preset objects.")
    stop_on_error: bool = Field(default=False, description="Stop after the first failed preset when true.")


@app.get("/backtest/engine-selftest")
def backtest_engine_selftest():
    """v0.7.9: directly invoke _simulate_trade with three canonical scenarios
    and report the results. Independent of stored data, deploy state, or
    request payload. Detects whether the engine that's loaded is producing
    phantom-TIME exits.

    Scenario A (TP-then-moon): bars cross TP early; expect TP exit ~75 bps.
    Scenario B (never-TP, last-close-extreme): expect AssertionError invariant.
    Scenario C (DST week, March 5 EST): scan_time=10:30 should land on 15:30 UTC bar.

    If A returns TP and B raises AssertionError and C reports correct ET hour,
    the engine is v0.7.8+. If any scenario fails, deployed code is older.

    Auth: public; runs in-memory only, no DB writes, no risk.
    """
    out = {"version": config.APP_VERSION, "scenarios": {}}

    # --- Scenario A: TP early, then moon
    try:
        bars_a = [
            {"timestamp_utc": "2026-02-03T17:30:00Z", "open": 154.7, "high": 154.8, "low": 154.6, "close": 154.7, "volume": 1000},
            {"timestamp_utc": "2026-02-03T17:31:00Z", "open": 154.7, "high": 156.0, "low": 154.6, "close": 155.5, "volume": 2000},
            {"timestamp_utc": "2026-02-03T20:00:00Z", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.84, "volume": 5000},
        ]
        result = backtest._simulate_trade(
            bars=bars_a, entry_ts_utc="2026-02-03T17:30:00Z",
            entry_price=154.7, tp_level=75.0, sl_level=100.0,
            timestop_et_hhmm="15:50", slippage_bps=15.0, entry_slippage_split=0.5,
        )
        out["scenarios"]["A_tp_then_moon"] = {
            "exit_reason": result["exit_reason"],
            "gross_bps": round(result["gross_return_bps"], 2),
            "exit_time_et": result["exit_time_et"],
            "minutes_held": result["minutes_held"],
            "expected": "TP exit, ~75 bps gross, exit_time near 12:31 ET",
            "passes": (
                result["exit_reason"] == "TP"
                and 60 <= result["gross_return_bps"] <= 100
                and result["exit_time_et"].startswith("12:")
            ),
        }
    except Exception as e:
        out["scenarios"]["A_tp_then_moon"] = {"error": f"{type(e).__name__}: {e}"}

    # --- Scenario B: never crosses TP/SL intra-bar, last close way above
    # NOTE: this requires bars where intra-bar high<tp_price and low>sl_price
    # but final close is far away. Real bars wouldn't do that, but we construct it.
    try:
        bars_b = [
            {"timestamp_utc": "2026-02-03T17:30:00Z", "open": 154.7, "high": 154.8, "low": 154.6, "close": 154.75, "volume": 1000},
            {"timestamp_utc": "2026-02-03T17:31:00Z", "open": 154.75, "high": 154.85, "low": 154.7, "close": 162.45, "volume": 1000},
        ]
        try:
            result_b = backtest._simulate_trade(
                bars=bars_b, entry_ts_utc="2026-02-03T17:30:00Z",
                entry_price=154.7, tp_level=75.0, sl_level=100.0,
                timestop_et_hhmm="15:50", slippage_bps=15.0, entry_slippage_split=0.5,
            )
            out["scenarios"]["B_invariant"] = {
                "exit_reason": result_b["exit_reason"],
                "gross_bps": round(result_b["gross_return_bps"], 2),
                "expected": "AssertionError raised, OR TP exit (if engine re-scans bar high)",
                "passes": (
                    result_b["exit_reason"] == "TP"
                    and result_b["gross_return_bps"] <= 80
                ),
                "phantom_detected": (
                    result_b["exit_reason"] == "TIME"
                    and result_b["gross_return_bps"] > 80
                ),
            }
        except AssertionError as ae:
            out["scenarios"]["B_invariant"] = {
                "exit_reason": "AssertionError",
                "message": str(ae)[:200],
                "expected": "AssertionError raised",
                "passes": True,
            }
    except Exception as e:
        out["scenarios"]["B_invariant"] = {"error": f"{type(e).__name__}: {e}"}

    # --- Scenario C: DST week (March 5 2026 = pre-DST EST)
    try:
        bars_c = [
            {"timestamp_utc": f"2026-03-05T{h:02d}:{m:02d}:00Z",
             "open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1000}
            for h, m in [(14, 30), (14, 45), (15, 0), (15, 15), (15, 30)]
        ]
        ts_c = backtest._find_scan_bar_ts(bars_c, "10:30")
        out["scenarios"]["C_dst_march5"] = {
            "scan_bar_ts": ts_c,
            "expected": "2026-03-05T15:30:00Z (10:30 ET in EST)",
            "passes": ts_c == "2026-03-05T15:30:00Z",
        }
    except Exception as e:
        out["scenarios"]["C_dst_march5"] = {"error": f"{type(e).__name__}: {e}"}

    out["all_pass"] = all(
        s.get("passes", False) for s in out["scenarios"].values()
    )
    return out


@app.get("/raw-bars/coverage")
def raw_bars_coverage(symbol: str, date: str):
    """v0.7.12: report bar-coverage breakdown for one (symbol, ET trading
    date) pair from the deployed raw_bars table.

    Returns counts of bars in pre-market (before 09:30 ET), regular session
    (09:30-15:59 ET), and after-hours (16:00 onward), plus the first/last
    bar timestamps in ET. Designed to diagnose the "regular session bars
    missing from raw_bars" condition that drove the v0.7.12 phantom hits:
    a (symbol, date) where n_regular_session == 0 but n_after_hours > 0
    is the exact failure mode, and the v0.7.12 _find_scan_bar_ts session
    guard now converts those signals to NO_DATA instead of phantom-TIME.

    Query: ?symbol=SMCI&date=2024-11-14

    Auth: public; this is read-only metadata, no PII, no risk.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    # Validate date format
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"date must be YYYY-MM-DD, got {date!r}",
        )
    if not symbol or not isinstance(symbol, str):
        raise HTTPException(status_code=400, detail="symbol is required")
    sym = symbol.strip().upper()

    with storage.connect(config.DB_PATH) as conn:
        bars = storage.get_raw_bars_for_day(conn, sym, date)

    n_pre_market = 0
    n_regular_session = 0
    n_after_hours = 0
    first_et = None
    last_et = None
    for b in bars:
        ts = b["timestamp_utc"]
        try:
            bdt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        et = bdt.astimezone(ET)
        et_str = et.strftime("%Y-%m-%d %H:%M ET")
        if first_et is None:
            first_et = et_str
        last_et = et_str
        if et.hour < 9 or (et.hour == 9 and et.minute < 30):
            n_pre_market += 1
        elif et.hour < 16:
            n_regular_session += 1
        else:
            n_after_hours += 1

    return {
        "symbol": sym,
        "date_et": date,
        "n_total": len(bars),
        "n_pre_market": n_pre_market,
        "n_regular_session": n_regular_session,
        "n_after_hours": n_after_hours,
        "first_bar_et": first_et,
        "last_bar_et": last_et,
        "phantom_risk": (
            n_regular_session == 0 and n_after_hours > 0
        ),
        "version": config.APP_VERSION,
    }


def _backtest_request_to_config(req: BacktestRunRequest) -> backtest.BacktestConfig:
    """Convert an API request into the engine config. Shared by single and batch runs."""
    try:
        rule = rule_tester.Rule.from_dict({
            "id": req.rule.id,
            "sector": req.rule.sector,
            "target": req.rule.target,
            "predicates": [p.model_dump() for p in req.rule.predicates],
        })
    except (ValueError, KeyError) as e:
        raise ValueError(f"Bad rule: {e}")

    cond_branches = [
        backtest.ConditionalExitBranch(
            feature=c.feature, op=c.op, value=c.value,
            tp_bps=c.tp_bps, sl_bps=c.sl_bps,
            position_size=c.position_size, label=c.label,
        )
        for c in req.conditional_exits
    ]
    return backtest.BacktestConfig(
        rule=rule,
        tp_bps=req.tp_bps, sl_bps=req.sl_bps,
        timestop_et=req.timestop_et,
        slippage_bps=req.slippage_bps,
        spy_regime_filter=req.spy_regime_filter,
        symbol_exclude=req.symbol_exclude,
        start_date=req.start_date, end_date=req.end_date,
        filter_mode=req.filter_mode,
        min_exit_minutes=req.min_exit_minutes,
        entry_delay_minutes=req.entry_delay_minutes,
        max_signals_per_day=req.max_signals_per_day,
        rank_feature=req.rank_feature,
        rank_direction=req.rank_direction,
        just_in_time_backfill=req.just_in_time_backfill,
        delete_raw_bars_after=req.delete_raw_bars_after,
        conditional_exits=cond_branches,
    )


def _execute_backtest_request(req: BacktestRunRequest) -> dict:
    bt_config = _backtest_request_to_config(req)
    return backtest.run_backtest(bt_config, db_path=config.DB_PATH)


def _first_present(d: dict, keys: list[str], default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalise_backtest_preset_dict(raw: dict) -> dict:
    """Accept dashboard preset aliases and return a BacktestRunRequest-compatible dict."""
    if not isinstance(raw, dict):
        raise ValueError("Each batch item must be a JSON object")
    cfg = dict(raw.get("backtest") or raw)
    rule = cfg.get("rule")
    if rule is None and isinstance(cfg.get("rules"), list) and cfg["rules"]:
        rule = cfg["rules"][0]
    if rule is None:
        raise ValueError("Preset must contain rule or rules[0]")

    out = {
        "rule": rule,
        "tp_bps": _first_present(cfg, ["tp_bps", "tp", "take_profit_bps"], 50),
        "sl_bps": _first_present(cfg, ["sl_bps", "sl", "stop_loss_bps"], 100),
        "timestop_et": _first_present(cfg, ["timestop_et", "timestop"], "15:50"),
        "slippage_bps": _first_present(cfg, ["slippage_bps", "slippage"], 10),
        "spy_regime_filter": _first_present(cfg, ["spy_regime_filter", "regime_filter"], None),
        "symbol_exclude": _first_present(cfg, ["symbol_exclude", "exclude_symbols", "symbols_exclude"], []),
        "start_date": _first_present(cfg, ["start_date", "start"], None),
        "end_date": _first_present(cfg, ["end_date", "end"], None),
        "filter_mode": _first_present(cfg, ["filter_mode", "signal_filter_mode"], "standard"),
        "entry_delay_minutes": _first_present(cfg, ["entry_delay_minutes", "entry_delay", "entry_delay_min"], 0),
        "min_exit_minutes": _first_present(cfg, ["min_exit_minutes", "min_exit_delay", "min_exit_delay_minutes"], 0),
        "max_signals_per_day": _first_present(cfg, ["max_signals_per_day", "max_trades_per_day", "top_k_per_day", "top_k"], None),
        "rank_feature": _first_present(cfg, ["rank_feature", "rank_by", "sort_feature"], None),
        "rank_direction": _first_present(cfg, ["rank_direction", "rank_order", "sort_direction"], "desc"),
        "just_in_time_backfill": _first_present(cfg, ["just_in_time_backfill", "jit_backfill", "jit"], True),
        "delete_raw_bars_after": _first_present(cfg, ["delete_raw_bars_after"], False),
        "conditional_exits": _first_present(cfg, ["conditional_exits", "conditional_exit_branches"], []),
    }
    if out["timestop_et"] == "":
        out["timestop_et"] = None
    if isinstance(out["symbol_exclude"], str):
        out["symbol_exclude"] = [s.strip().upper() for s in out["symbol_exclude"].split(",") if s.strip()]
    return out


def _batch_progress(job_id: str, *, label: str | None, total: int, completed: int, current_index: int | None, items: list[dict], zip_filename: str | None = None, manifest_filename: str | None = None) -> None:
    jobs.registry._update_status(
        job_id, "running",
        result={
            "kind": "backtest_batch",
            "label": label,
            "total": total,
            "completed": completed,
            "current_index": current_index,
            "items": items,
            "batch_zip_filename": zip_filename,
            "batch_manifest_filename": manifest_filename,
        },
    )


def _run_backtest_batch_job(job_id: str, presets: list[dict], label: str | None = None, stop_on_error: bool = False) -> dict:
    """Run batch presets sequentially and package all CSVs into one evidence ZIP."""
    total = len(presets)
    items: list[dict] = []
    _batch_progress(job_id, label=label, total=total, completed=0, current_index=None, items=items)

    for idx, raw in enumerate(presets, start=1):
        item = {
            "index": idx,
            "status": "running",
            "label": raw.get("label") or raw.get("name") or raw.get("id"),
        }
        try:
            normalised = _normalise_backtest_preset_dict(raw)
            req = BacktestRunRequest.model_validate(normalised)
            item["rule_id"] = req.rule.id
            item["tp_bps"] = req.tp_bps
            item["sl_bps"] = req.sl_bps
            item["filter_mode"] = req.filter_mode
            item["entry_delay_minutes"] = req.entry_delay_minutes
            item["min_exit_minutes"] = req.min_exit_minutes
            item["status"] = "running"
            if len(items) < idx:
                items.append(item)
            else:
                items[idx - 1] = item
            _batch_progress(job_id, label=label, total=total, completed=idx-1, current_index=idx, items=items)

            summary = _execute_backtest_request(req)
            item.update({
                "status": "succeeded",
                "run_uuid": summary.get("run_uuid"),
                "trades_csv_filename": summary.get("trades_csv_filename"),
                "n_signals_total": summary.get("n_signals_total"),
                "n_trades": summary.get("n_trades"),
                "win_rate": summary.get("win_rate"),
                "net_pnl_bps": summary.get("net_pnl_bps"),
                "avg_net_bps_per_trade": summary.get("avg_net_bps_per_trade"),
                "exit_reason_mix": summary.get("exit_reason_mix"),
            })
        except Exception as e:
            logger.exception("Batch backtest item %s failed", idx)
            item.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
            if len(items) < idx:
                items.append(item)
            else:
                items[idx - 1] = item
            _batch_progress(job_id, label=label, total=total, completed=idx, current_index=None, items=items)
            if stop_on_error:
                break

        if len(items) < idx:
            items.append(item)
        else:
            items[idx - 1] = item
        _batch_progress(job_id, label=label, total=total, completed=idx, current_index=None, items=items)

    generated_at = datetime.now(timezone.utc).isoformat()
    safe_id = job_id[:8]
    manifest = {
        "batch_job_id": job_id,
        "label": label,
        "generated_at_utc": generated_at,
        "total": total,
        "succeeded": sum(1 for i in items if i.get("status") == "succeeded"),
        "failed": sum(1 for i in items if i.get("status") == "failed"),
        "items": items,
    }
    pack_dir = Path(config.EVIDENCE_PACK_DIR)
    pack_dir.mkdir(parents=True, exist_ok=True)
    manifest_filename = f"backtest_batch_{safe_id}_manifest.json"
    manifest_path = pack_dir / manifest_filename
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    zip_filename = f"backtest_batch_{safe_id}_outputs.zip"
    zip_path = pack_dir / zip_filename
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_path, arcname=manifest_filename)
        for item in items:
            csv_name = item.get("trades_csv_filename")
            if not csv_name:
                continue
            csv_path = pack_dir / csv_name
            if csv_path.exists():
                zf.write(csv_path, arcname=csv_name)
    manifest["batch_zip_filename"] = zip_filename
    manifest["batch_zip_url"] = f"/packs/{zip_filename}"
    manifest["batch_manifest_filename"] = manifest_filename
    manifest["batch_manifest_url"] = f"/packs/{manifest_filename}"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


@app.post("/backtest/run", dependencies=[Depends(_require_api_key)])
def backtest_run(req: BacktestRunRequest):
    """Execute a path-dependent backtest and return the summary.

    This is a synchronous endpoint. For typical runs (a few hundred signals,
    just-in-time backfill disabled because raw_bars are already there) it
    returns in seconds. For runs that require just-in-time backfill across
    many (symbol, date) pairs, it can take minutes — each unique day is one
    Alpaca call.
    """
    try:
        return _execute_backtest_request(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Backtest run failed")
        raise HTTPException(status_code=500, detail=f"Backtest failed: {e}")


def _extract_presets_from_uploaded_batch(filename: str, content: bytes) -> list[dict]:
    """Parse a .json batch/single preset or a .zip containing JSON presets."""
    name = (filename or "uploaded").lower()

    def _from_json_bytes(data: bytes, source_name: str) -> list[dict]:
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"{source_name}: invalid JSON: {e}")
        if isinstance(obj, dict) and isinstance(obj.get("presets"), list):
            return obj["presets"]
        if isinstance(obj, dict) and isinstance(obj.get("backtests"), list):
            return obj["backtests"]
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            return [obj]
        raise ValueError(f"{source_name}: expected JSON object, array, or object with presets[]")

    if name.endswith(".zip"):
        out: list[dict] = []
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                json_names = [n for n in zf.namelist() if n.lower().endswith(".json") and not n.endswith("/")]
                if not json_names:
                    raise ValueError("zip contains no .json preset files")
                for n in sorted(json_names):
                    out.extend(_from_json_bytes(zf.read(n), n))
        except zipfile.BadZipFile:
            raise ValueError("uploaded file is not a valid ZIP")
        return out

    return _from_json_bytes(content, filename or "uploaded.json")


def _start_backtest_batch_job(label: str | None, presets: list[dict], stop_on_error: bool = False) -> dict:
    if not presets:
        raise HTTPException(status_code=400, detail="Batch contains no presets")
    if len(presets) > 25:
        raise HTTPException(status_code=400, detail="Batch limit is 25 presets per run")

    # Validate up-front enough to fail fast on malformed files before starting a job.
    for idx, raw in enumerate(presets, start=1):
        try:
            BacktestRunRequest.model_validate(_normalise_backtest_preset_dict(raw))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Preset {idx} is invalid: {e}")

    job = jobs.registry.create(
        "backtest_batch",
        params={"label": label, "n_presets": len(presets), "stop_on_error": stop_on_error},
    )
    jobs.registry.run_async(
        job, _run_backtest_batch_job,
        job_id=job.job_id, presets=presets, label=label, stop_on_error=stop_on_error,
    )
    return {
        "job_id": job.job_id,
        "status": "started",
        "kind": "backtest_batch",
        "n_presets": len(presets),
        "poll": f"/jobs/{job.job_id}",
    }


@app.post("/backtest/batch/run", dependencies=[Depends(_require_api_key)])
def backtest_batch_run(req: BacktestBatchRunRequest):
    """v0.7.22: start a sequential batch from a JSON request body."""
    return _start_backtest_batch_job(req.label, req.presets, req.stop_on_error)


@app.post("/backtest/batch/upload-run", dependencies=[Depends(_require_api_key)])
async def backtest_batch_upload_run(file: UploadFile = File(...), label: str | None = None, stop_on_error: bool = False):
    """v0.7.22: upload a .json batch, one .json preset, or a .zip of JSON presets and run sequentially."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    try:
        presets = _extract_presets_from_uploaded_batch(file.filename or "uploaded", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _start_backtest_batch_job(label or file.filename, presets, stop_on_error)


@app.get("/backtest/{run_uuid}", dependencies=[Depends(_require_api_key)])
def backtest_get(run_uuid: str):
    """Fetch a completed backtest run's summary + aggregate stats."""
    with storage.connect(config.DB_PATH) as conn:
        run = storage.get_backtest_run(conn, run_uuid)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No backtest run with uuid {run_uuid!r}")
        trades = storage.get_backtest_trades(conn, run_uuid)
    return {
        "run": run,
        "n_trades": len(trades),
        "aggregates": backtest.compute_aggregates(trades),
    }


@app.get("/backtest/{run_uuid}/diagnose", dependencies=[Depends(_require_api_key)])
def backtest_diagnose(run_uuid: str):
    """Return no_data breakdown + exit-reason mix for an existing run.

    Useful when a run's trades CSV wasn't written at runtime (pre-v0.6.1)
    or when the user just wants a quick look at why signals didn't simulate.
    """
    with storage.connect(config.DB_PATH) as conn:
        run = storage.get_backtest_run(conn, run_uuid)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No backtest run with uuid {run_uuid!r}")
        trades = storage.get_backtest_trades(conn, run_uuid)
    return {
        "run_uuid": run_uuid,
        "n_trades_total": len(trades),
        "exit_reason_mix": backtest._count_exits(trades),
        "no_data_diagnosis": backtest._summarize_no_data(trades),
    }


@app.get("/backtest/{run_uuid}/trades.csv", dependencies=[Depends(_require_api_key)])
def backtest_trades_csv(run_uuid: str):
    """Download per-trade CSV for a completed backtest run."""
    import csv, io
    with storage.connect(config.DB_PATH) as conn:
        run = storage.get_backtest_run(conn, run_uuid)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No backtest run with uuid {run_uuid!r}")
        trades = storage.get_backtest_trades(conn, run_uuid)
    if not trades:
        raise HTTPException(status_code=404, detail="No trades for this run")
    out_path = Path(config.EVIDENCE_PACK_DIR) / f"backtest_{run_uuid[:8]}_trades.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
        w.writeheader(); w.writerows(trades)
    return FileResponse(
        out_path, media_type="text/csv", filename=out_path.name,
    )


@app.get("/backtest", dependencies=[Depends(_require_api_key)])
def backtest_list(limit: int = 50):
    """List recent backtest runs (newest first)."""
    with storage.connect(config.DB_PATH) as conn:
        runs = storage.list_backtest_runs(conn, limit=limit)
    return {"runs": runs, "count": len(runs)}


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Rule009 live-shadow monitor (v0.7.25)
# ---------------------------------------------------------------------------
class Rule009ShadowRequest(BaseModel):
    """Run the promoted Rule009 candidate in shadow/monitor mode.

    This is deliberately read-only and decision-support only. It surfaces the
    10:30 high-volatility Technology candidates and, when historical/outcome
    bars are available, also runs the same path-dependent TP/SL audit used by
    the backtest engine.
    """
    date: str | None = Field(
        default=None,
        description="YYYY-MM-DD. If omitted, uses the latest available 10:30 Information Technology scan date.",
    )
    sector: str = Field(default="Information Technology")
    top_ks: list[int] = Field(default_factory=lambda: [10, 20])
    evaluate: bool = Field(
        default=True,
        description="If True, attempt path-dependent outcome evaluation using available raw bars.",
    )
    just_in_time_backfill: bool = Field(default=True)
    slippage_bps: float = Field(default=10.0, ge=0.0, le=100.0)


def _rule009_rule_dict() -> dict:
    return {
        "id": "tech_rule_009_1030_spyvol005_spymom_pos",
        "sector": "Information Technology",
        "target": "target",
        "predicates": [
            {"feature": "minutes_since_open", "op": "==", "value": 60},
            {"feature": "spy_vol", "op": ">=", "value": 0.005},
            {"feature": "spy_momentum", "op": ">=", "value": 0},
        ],
        "notes": "Promoted high-volatility 10:30 Technology opportunity mode.",
    }


def _latest_rule009_scan_date(sector: str) -> str | None:
    with storage.connect(config.DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT MAX(date) AS latest_date
            FROM research_rows
            WHERE sector = ? AND scan_time_et = '10:30'
            """,
            (sector,),
        ).fetchone()
    if not row:
        return None
    try:
        return row["latest_date"]
    except Exception:
        return row[0]


def _rule009_candidates_for_date(sector: str, date: str) -> pd.DataFrame:
    df = rule_tester.load_scan_rows_from_db(
        config.DB_PATH, sector, start_date=date, end_date=date,
    )
    if df.empty:
        return df
    # Use only scan-time-available fields. Do NOT require target to be non-null;
    # live shadow rows may be unresolved intraday.
    for col in ["spy_vol", "spy_momentum", "momentum"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    mask = (
        (df.get("scan_time_et") == "10:30")
        & (df.get("spy_vol") >= 0.005)
        & (df.get("spy_momentum") >= 0)
    )
    out = df[mask].copy()
    if out.empty:
        return out
    out["_rank_value"] = pd.to_numeric(out.get("momentum"), errors="coerce").fillna(float("-inf"))
    out = out.sort_values(["_rank_value", "symbol"], ascending=[False, True]).copy()
    out["rule009_rank_by_momentum"] = range(1, len(out) + 1)
    out = out.drop(columns=["_rank_value"], errors="ignore")
    return out


def _shadow_export_rows(df: pd.DataFrame, top_k: int | None = None) -> list[dict]:
    if df.empty:
        return []
    if top_k is not None:
        df = df.head(int(top_k)).copy()
    keep_cols = [
        "date", "symbol", "scan_time_et", "minutes_since_open", "scan_price",
        "spy_vol", "spy_momentum", "momentum", "ret_vs_spy", "mom_vs_spy",
        "relative_volume", "distance_to_vwap", "intraday_range_position",
        "open_to_scan_return", "gap_pct", "rsi_14", "ema_20_distance",
        "sector_breadth_up", "new_highs_in_sector", "rule009_rank_by_momentum",
        "target", "return_to_cutoff", "target_50bps", "target_peak_50bps",
    ]
    rows = []
    for _, r in df.iterrows():
        row = {}
        for c in keep_cols:
            if c not in df.columns:
                continue
            v = r.get(c)
            try:
                if pd.isna(v):
                    v = None
            except Exception:
                pass
            row[c] = v
        row["planned_entry_time_et"] = "10:31"
        row["strategy_id"] = "tech_rule_009_ranked_momentum_top10_or_top20"
        rows.append(row)
    return rows


def _write_dict_rows_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("empty\n", encoding="utf-8")
        return
    keys = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                keys.append(k); seen.add(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


@app.post("/rule009/shadow/run", dependencies=[Depends(_require_api_key)])
def rule009_shadow_run(req: Rule009ShadowRequest):
    """Run promoted Rule009 in shadow mode and export a daily evidence ZIP.

    This endpoint is intentionally safe: it does not place orders, it only
    reads scan rows and optionally runs the existing historical outcome engine
    when sufficient bars/outcomes are available.
    """
    sector = _resolve_sector(req.sector)
    date = req.date or _latest_rule009_scan_date(sector)
    if not date:
        raise HTTPException(status_code=404, detail="No 10:30 scan rows found for Rule009 shadow monitor")

    candidates = _rule009_candidates_for_date(sector, date)
    active = not candidates.empty
    total_candidates = int(len(candidates))
    top_ks = sorted({max(1, min(100, int(k))) for k in (req.top_ks or [10, 20])})

    safe_date = str(date).replace("/", "-")
    generated_at = datetime.now(timezone.utc).isoformat()
    pack_stem = f"rule009_shadow_{safe_date}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    pack_dir = Path(config.EVIDENCE_PACK_DIR)
    work_dir = pack_dir / pack_stem
    work_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "kind": "rule009_shadow_monitor",
        "version": config.APP_VERSION,
        "generated_at_utc": generated_at,
        "strategy_id": "tech_rule_009_ranked_momentum",
        "date": date,
        "sector": sector,
        "active": active,
        "status": "active" if active else "inactive",
        "reason": (
            "Rule009 conditions met: 10:30 Technology rows with spy_vol >= 0.005 and spy_momentum >= 0."
            if active else
            "Rule009 inactive for this date: no 10:30 Technology rows met spy_vol >= 0.005 and spy_momentum >= 0."
        ),
        "total_candidates_before_rank_cap": total_candidates,
        "selection": {
            "rank_feature": "momentum",
            "rank_direction": "desc",
            "top_ks": top_ks,
        },
        "execution_reference": {
            "entry_delay_minutes": 1,
            "planned_entry_time_et": "10:31",
            "tp_bps": 100,
            "sl_bps": 200,
            "slippage_bps": req.slippage_bps,
            "min_exit_minutes": 1,
            "timestop_et": "15:50",
        },
        "outputs": [],
        "evaluations": [],
    }

    all_rows = _shadow_export_rows(candidates, None)
    all_csv = work_dir / "rule009_all_candidates.csv"
    _write_dict_rows_csv(all_csv, all_rows)
    manifest["outputs"].append(all_csv.name)

    for k in top_ks:
        rows = _shadow_export_rows(candidates, k)
        fn = work_dir / f"rule009_top{k}_candidates.csv"
        _write_dict_rows_csv(fn, rows)
        manifest["outputs"].append(fn.name)

        if active and req.evaluate:
            try:
                normalised = {
                    "rule": _rule009_rule_dict(),
                    "tp_bps": 100,
                    "sl_bps": 200,
                    "timestop_et": "15:50",
                    "slippage_bps": req.slippage_bps,
                    "start_date": date,
                    "end_date": date,
                    "filter_mode": "standard",
                    "entry_delay_minutes": 1,
                    "min_exit_minutes": 1,
                    "max_signals_per_day": k,
                    "rank_feature": "momentum",
                    "rank_direction": "desc",
                    "just_in_time_backfill": req.just_in_time_backfill,
                    "conditional_exits": [],
                }
                bt_req = BacktestRunRequest.model_validate(normalised)
                summary = _execute_backtest_request(bt_req)
                eval_item = {
                    "top_k": k,
                    "status": "succeeded" if not summary.get("error") else "no_signals_or_unresolved",
                    "summary": summary,
                }
                trades_name = summary.get("trades_csv_filename")
                if trades_name:
                    src = Path(config.EVIDENCE_PACK_DIR) / trades_name
                    if src.exists():
                        dst = work_dir / f"rule009_top{k}_shadow_trades.csv"
                        dst.write_bytes(src.read_bytes())
                        eval_item["trades_csv"] = dst.name
                        manifest["outputs"].append(dst.name)
                manifest["evaluations"].append(eval_item)
            except Exception as e:
                manifest["evaluations"].append({
                    "top_k": k,
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}",
                    "note": "This can be normal for unresolved live same-day rows; candidate CSVs are still valid.",
                })

    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    manifest["outputs"].append(manifest_path.name)

    zip_filename = f"{pack_stem}.zip"
    zip_path = pack_dir / zip_filename
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for child in sorted(work_dir.iterdir()):
            if child.is_file():
                zf.write(child, arcname=child.name)

    manifest.update({
        "pack_filename": zip_filename,
        "pack_url": f"/packs/{zip_filename}",
        "top10_preview": _shadow_export_rows(candidates, 10),
    })
    return manifest
# Audit + repair (v0.7.1): verify stored backtest outcomes against the
# canonical reference simulator. Produces evidence pack on divergence.
# ---------------------------------------------------------------------------
from . import backtest_audit  # noqa: E402


def _audit_job_wrapper(db_path: str, run_uuid: str, jit_backfill: bool) -> dict:
    """Runs audit and writes the evidence pack; job result holds the summary
    plus pack filename."""
    report = backtest_audit.audit_run(
        db_path=db_path, run_uuid=run_uuid, jit_backfill=jit_backfill,
    )
    pack_path = backtest_audit.export_audit_pack(
        report, Path(config.EVIDENCE_PACK_DIR),
    )
    summary = {k: v for k, v in report.items() if k != "trade_results"}
    summary["pack_filename"] = pack_path.name
    summary["pack_download_url"] = f"/packs/{pack_path.name}"
    return summary


def _repair_job_wrapper(db_path: str, source_run_uuid: str,
                        jit_backfill: bool) -> dict:
    new_uuid = backtest_audit.repair_run(
        db_path=db_path, source_run_uuid=source_run_uuid,
        jit_backfill=jit_backfill,
    )
    return {
        "source_run_uuid": source_run_uuid,
        "new_run_uuid": new_uuid,
        "trades_csv_url": f"/backtest/{new_uuid}/trades.csv",
    }


@app.post("/backtest/{run_uuid}/audit", dependencies=[Depends(_require_api_key)])
def backtest_audit_endpoint(run_uuid: str, jit_backfill: bool = True):
    """Start an async audit of a completed backtest run.

    Auditing 1,000+ trades with JIT backfills can take minutes. The
    endpoint returns immediately with a job_id; poll /jobs/{id} for
    status and result. When the job finishes, the result field holds
    the same summary the old sync endpoint returned.
    """
    # Validate run exists BEFORE kicking off the background thread, so a
    # bad UUID returns a proper 404 instead of a failed job.
    with storage.connect(config.DB_PATH) as conn:
        if storage.get_backtest_run(conn, run_uuid) is None:
            raise HTTPException(status_code=404, detail=f"run {run_uuid} not found")

    job = jobs.registry.create(
        "backtest_audit",
        params={"run_uuid": run_uuid, "jit_backfill": jit_backfill},
    )
    jobs.registry.run_async(
        job, _audit_job_wrapper,
        db_path=config.DB_PATH, run_uuid=run_uuid, jit_backfill=jit_backfill,
    )
    return {"job_id": job.job_id, "status": "started",
            "run_uuid": run_uuid, "poll": f"/jobs/{job.job_id}"}


@app.post("/backtest/{run_uuid}/repair", dependencies=[Depends(_require_api_key)])
def backtest_repair_endpoint(run_uuid: str, jit_backfill: bool = True):
    """Start an async repair. Returns job_id; poll /jobs/{id}.

    Repair re-simulates every trade in the source run using the canonical
    reference engine and writes a NEW run_uuid with corrected trades. The
    original run is never mutated. When the job completes, result.new_run_uuid
    is the corrected run; download its trades.csv for analysis.
    """
    with storage.connect(config.DB_PATH) as conn:
        if storage.get_backtest_run(conn, run_uuid) is None:
            raise HTTPException(status_code=404, detail=f"run {run_uuid} not found")

    job = jobs.registry.create(
        "backtest_repair",
        params={"source_run_uuid": run_uuid, "jit_backfill": jit_backfill},
    )
    jobs.registry.run_async(
        job, _repair_job_wrapper,
        db_path=config.DB_PATH, source_run_uuid=run_uuid,
        jit_backfill=jit_backfill,
    )
    return {"job_id": job.job_id, "status": "started",
            "source_run_uuid": run_uuid, "poll": f"/jobs/{job.job_id}"}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
    )
    Path(config.EVIDENCE_PACK_DIR).mkdir(parents=True, exist_ok=True)
    storage.init_schema(config.DB_PATH)
    storage.init_backtest_schema(config.DB_PATH)
    jobs.init_jobs_schema(config.DB_PATH, sweep_orphaned=True)
    logger.info(
        f"Tech Collector starting. Version={config.APP_VERSION}, "
        f"default_sector={config.DEFAULT_SECTOR}, "
        f"DB={config.DB_PATH}, packs={config.EVIDENCE_PACK_DIR}"
    )
