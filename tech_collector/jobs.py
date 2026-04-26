"""
Job registry: SQLite-backed so state is visible across worker processes.

Why SQLite
==========
Render (and any multi-process ASGI deployment) runs multiple worker
processes. The original in-memory JobRegistry stored jobs in a dict
inside each worker's Python memory — a POST to worker A created the
job, but a GET from worker B couldn't see it. Result: polls returned
404 even though the job was running successfully.

This module persists job state to `bt_jobs` in the main SQLite DB. Any
worker reading from the DB sees the same state. The worker that created
the job runs the background thread; other workers just read progress.

Failure modes
=============
- If the worker running the thread dies mid-job, status stays "running"
  forever. Clients should interpret "running" with a stale
  updated_at_utc as "probably dead; check logs or restart." A future
  improvement could add a heartbeat column.

API compatibility
=================
`registry` exposes the same methods as the old in-memory version:
create(), get(), list(), run_async(). Existing callers do not change.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Literal

from . import config

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "failed"]


_JOBS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bt_jobs (
    job_id           TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    result_json      TEXT,
    error            TEXT,
    created_at_utc   TEXT NOT NULL,
    started_at_utc   TEXT,
    finished_at_utc  TEXT,
    updated_at_utc   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_bt_jobs_created ON bt_jobs(created_at_utc DESC);
CREATE INDEX IF NOT EXISTS ix_bt_jobs_status ON bt_jobs(status);
"""


def init_jobs_schema(db_path: str = config.DB_PATH, *, sweep_orphaned: bool = False) -> None:
    """Create bt_jobs if it doesn't already exist. Safe to call repeatedly.

    v0.7.10: orphan cleanup is opt-in via sweep_orphaned=True. Schema
    creation happens both at module import and when helper registries are
    constructed, so unconditional sweeping can falsely mark a legitimately
    running job as failed. The FastAPI startup hook is the only place that
    should sweep, because a process restart really did kill old worker
    threads.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_JOBS_SCHEMA_SQL)
        n_swept = 0
        if sweep_orphaned:
            # Sweep orphaned running jobs (their thread is dead). Safe only
            # during process startup, before this worker accepts requests.
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "UPDATE bt_jobs SET status='failed', "
                "error='orphaned by service restart (worker thread died)', "
                "finished_at_utc=?, updated_at_utc=? "
                "WHERE status='running'",
                (now, now),
            )
            n_swept = cur.rowcount
        conn.commit()
        if n_swept > 0:
            logging.getLogger(__name__).info(
                f"init_jobs_schema: swept {n_swept} orphaned 'running' jobs"
            )
    finally:
        conn.close()


@dataclass
class Job:
    job_id: str
    kind: str
    status: JobStatus = "pending"
    started_at_utc: str | None = None
    finished_at_utc: str | None = None
    params: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        kind=row["kind"],
        status=row["status"],
        started_at_utc=row["started_at_utc"],
        finished_at_utc=row["finished_at_utc"],
        params=json.loads(row["params_json"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error=row["error"],
    )


class JobRegistry:
    """SQLite-backed job registry — multi-worker safe.

    Per-op short-lived connections. No app-level locking — SQLite's
    WAL journal handles concurrency.
    """

    def __init__(self, db_path: str = config.DB_PATH):
        self._db_path = db_path
        init_jobs_schema(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def create(self, kind: str, params: dict) -> Job:
        job = Job(
            job_id=str(uuid.uuid4()), kind=kind,
            status="pending", params=params,
        )
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO bt_jobs (
                    job_id, kind, status, params_json, result_json, error,
                    created_at_utc, started_at_utc, finished_at_utc,
                    updated_at_utc
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, ?)""",
                (job.job_id, kind, "pending", json.dumps(params), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return job

    def get(self, job_id: str) -> Job | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bt_jobs WHERE job_id = ?", (job_id,),
            ).fetchone()
            return _row_to_job(row) if row else None
        finally:
            conn.close()

    def list(self, limit: int = 100) -> list[Job]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM bt_jobs ORDER BY created_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row_to_job(r) for r in rows]
        finally:
            conn.close()

    def _update_status(
        self, job_id: str, status: JobStatus, *,
        result: dict | None = None, error: str | None = None,
        started_at_utc: str | None = None, finished_at_utc: str | None = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            fields = ["status = ?", "updated_at_utc = ?"]
            vals: list = [status, now]
            if result is not None:
                fields.append("result_json = ?"); vals.append(json.dumps(result))
            if error is not None:
                fields.append("error = ?"); vals.append(error)
            if started_at_utc is not None:
                fields.append("started_at_utc = ?"); vals.append(started_at_utc)
            if finished_at_utc is not None:
                fields.append("finished_at_utc = ?"); vals.append(finished_at_utc)
            vals.append(job_id)
            conn.execute(
                f"UPDATE bt_jobs SET {', '.join(fields)} WHERE job_id = ?",
                vals,
            )
            conn.commit()
        finally:
            conn.close()

    def run_async(self, job: Job, func: Callable[..., dict], **kwargs):
        """Start `func(**kwargs)` in a background thread; persist status
        transitions to SQLite so other workers can read them."""
        def _runner():
            started = datetime.now(timezone.utc).isoformat()
            self._update_status(
                job.job_id, "running", started_at_utc=started,
            )
            try:
                result = func(**kwargs)
                finished = datetime.now(timezone.utc).isoformat()
                self._update_status(
                    job.job_id, "succeeded", result=result,
                    finished_at_utc=finished,
                )
            except Exception as e:
                err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                finished = datetime.now(timezone.utc).isoformat()
                self._update_status(
                    job.job_id, "failed", error=err,
                    finished_at_utc=finished,
                )
                logger.exception(f"job {job.job_id} failed")

        t = threading.Thread(target=_runner, daemon=True)
        t.start()


# Module-level singleton. Lazily initialises the bt_jobs table on import.
registry = JobRegistry()
