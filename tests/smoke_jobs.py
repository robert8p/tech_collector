"""Smoke test: SQLite-backed jobs registry (v0.7.5 fix for multi-worker polls).

The original in-memory JobRegistry broke under Render's multi-worker
deployment because jobs created by one worker were invisible to others.
This test verifies the SQLite-backed replacement:

  1. Jobs persist to disk
  2. A second "worker" (new JobRegistry instance, same DB) sees jobs created
     by the first worker — this is the exact multi-worker scenario
  3. run_async status transitions are visible to the second worker
  4. Errors in background threads persist to the error column

Usage:
    python -m tests.smoke_jobs
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback


RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, passed: bool, detail: str = ""):
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}{(': ' + detail) if detail else ''}")


def test_create_and_get_in_same_worker():
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        # Reload jobs module after setting DB_PATH (the module-level `registry`
        # uses config.DB_PATH at import time, so we instantiate a fresh one)
        from tech_collector.jobs import JobRegistry
        reg = JobRegistry(config.DB_PATH)
        job = reg.create("test_kind", {"foo": "bar"})
        _check("create returns a Job with job_id",
               bool(job.job_id) and len(job.job_id) == 36,
               f"got {job.job_id}")
        _check("create returns pending status",
               job.status == "pending", f"got {job.status}")
        fetched = reg.get(job.job_id)
        _check("get() by same registry returns the job",
               fetched is not None, "got None" if not fetched else "")
        if fetched:
            _check("get() returns same params",
                   fetched.params == {"foo": "bar"}, f"got {fetched.params}")
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_cross_worker_visibility():
    """The key regression test: a job created by one registry instance
    (simulating worker A) must be visible to a DIFFERENT instance
    (simulating worker B) backed by the same DB file."""
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        from tech_collector.jobs import JobRegistry
        worker_a = JobRegistry(config.DB_PATH)
        job = worker_a.create("cross_worker_test", {"msg": "hello"})

        worker_b = JobRegistry(config.DB_PATH)  # separate instance — simulates another process
        fetched = worker_b.get(job.job_id)
        _check("worker B sees worker A's job",
               fetched is not None,
               "got None (multi-worker broken)" if not fetched else "")
        if fetched:
            _check("worker B reads same params",
                   fetched.params == {"msg": "hello"},
                   f"got {fetched.params}")
            _check("worker B reads same kind",
                   fetched.kind == "cross_worker_test",
                   f"got {fetched.kind}")
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_run_async_status_transitions_cross_worker():
    """Run a background job in worker A. Poll its status from worker B.
    This is the exact audit-endpoint scenario."""
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        from tech_collector.jobs import JobRegistry
        worker_a = JobRegistry(config.DB_PATH)
        worker_b = JobRegistry(config.DB_PATH)

        def slow_work():
            time.sleep(0.3)
            return {"answer": 42}

        job = worker_a.create("slow", {})
        worker_a.run_async(job, slow_work)

        # Poll from worker B. Tolerate brief lag.
        start = time.time()
        final = None
        while time.time() - start < 5:
            fetched = worker_b.get(job.job_id)
            if fetched and fetched.status in ("succeeded", "failed"):
                final = fetched
                break
            time.sleep(0.05)
        _check("worker B sees job reach terminal status",
               final is not None, "job never completed within 5s")
        if final:
            _check("final status is succeeded",
                   final.status == "succeeded", f"got {final.status}")
            _check("worker B reads result from A's thread",
                   final.result == {"answer": 42}, f"got {final.result}")
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_run_async_error_persists():
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        from tech_collector.jobs import JobRegistry
        worker_a = JobRegistry(config.DB_PATH)
        worker_b = JobRegistry(config.DB_PATH)

        def boom():
            raise ValueError("intentional failure for test")

        job = worker_a.create("boom", {})
        worker_a.run_async(job, boom)

        # Wait for failed status
        start = time.time()
        final = None
        while time.time() - start < 3:
            fetched = worker_b.get(job.job_id)
            if fetched and fetched.status in ("succeeded", "failed"):
                final = fetched
                break
            time.sleep(0.05)
        _check("error job reaches failed status",
               final is not None and final.status == "failed",
               f"got {final.status if final else 'None'}")
        if final and final.status == "failed":
            _check("error message contains exception type",
                   "ValueError" in (final.error or ""),
                   f"got {final.error[:80] if final.error else None}")
            _check("error message contains our text",
                   "intentional failure" in (final.error or ""),
                   f"got {final.error[:80] if final.error else None}")
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_list_jobs_newest_first():
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        from tech_collector.jobs import JobRegistry
        reg = JobRegistry(config.DB_PATH)
        j1 = reg.create("first", {})
        time.sleep(0.01)
        j2 = reg.create("second", {})
        time.sleep(0.01)
        j3 = reg.create("third", {})
        jobs_list = reg.list()
        _check("list() returns all jobs",
               len(jobs_list) == 3, f"got {len(jobs_list)}")
        _check("list() orders newest first",
               [j.kind for j in jobs_list] == ["third", "second", "first"],
               f"got {[j.kind for j in jobs_list]}")
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def test_idempotent_schema_init():
    from tech_collector import config
    tmpdir = tempfile.mkdtemp(prefix="jobs_smoke_")
    orig = config.DB_PATH
    config.DB_PATH = os.path.join(tmpdir, "test.db")
    try:
        from tech_collector.jobs import JobRegistry, init_jobs_schema
        # Call schema init multiple times; should never raise
        for _ in range(5):
            init_jobs_schema(config.DB_PATH)
        reg = JobRegistry(config.DB_PATH)
        reg.create("ok", {})
        _check("init_jobs_schema is idempotent", True)
    except Exception as e:
        _check("init_jobs_schema is idempotent", False, str(e))
    finally:
        config.DB_PATH = orig
        import shutil; shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    print("SMOKE: jobs registry (SQLite-backed)")
    print("=" * 60)
    tests = [
        test_create_and_get_in_same_worker,
        test_cross_worker_visibility,
        test_run_async_status_transitions_cross_worker,
        test_run_async_error_persists,
        test_list_jobs_newest_first,
        test_idempotent_schema_init,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _check(t.__name__ + " (raised)", False, str(e))
            traceback.print_exc()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("=" * 60)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
