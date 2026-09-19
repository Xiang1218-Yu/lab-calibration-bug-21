"""Background task: run a calibration import asynchronously.

The HTTP handler uploads the file, records a pending ``import_jobs`` row and
enqueues this task. The task:

* takes a single-flight advisory lock so two imports never run concurrently
  (duplicate-execution protection),
* opens an ``import_attempts`` row so every retry leaves durable history,
* makes a retry safe by cleaning *only* the previous attempt's writes through
  :mod:`attempt_cleanup` (state that has since moved on becomes a compensation
  item instead of being force-deleted),
* heartbeats while processing so a wedged import is detected as timed out,
* records the full report (accepted / duplicate / errors) on ``import_jobs``.
"""
from __future__ import annotations

import os

from .. import config, db
from ..services import importer
from ..services import attempt_cleanup
from ..utils import iso
from . import locks
from .runner import task

IMPORT_LOCK_KEY = "import:calibrations"
IMPORT_LOCK_TTL = max(600.0, config.TASK_TIMEOUT_SECONDS * 2)


@task("import_calibrations")
def run_import(payload: dict, ctx) -> dict:
    import_job_id = payload["import_job_id"]
    path = payload["path"]
    fmt = payload.get("fmt", "csv")
    auto_create = bool(payload.get("auto_create_devices", False))
    actor = payload.get("actor")

    # Single-flight: only one import at a time across workers/processes.
    token = locks.acquire(IMPORT_LOCK_KEY, ttl_seconds=IMPORT_LOCK_TTL)
    if token is None:
        # Another import is running; raise so this job retries shortly.
        raise RuntimeError("another import is already running; will retry")
    attempt_id = None
    try:
        attempt_id = importer.start_attempt(import_job_id, job_id=ctx.job_id)
        db.execute("UPDATE import_jobs SET status='processing' WHERE id=?",
                   (import_job_id,))

        # Safe retry: remove rows/notifications of earlier *failed* attempts,
        # generating compensation items where state can't be auto-rewound.
        prior = db.query(
            "SELECT id FROM import_attempts WHERE import_job_id=? AND id!=? "
            "AND status='failed'", (import_job_id, attempt_id))
        for p in prior:
            attempt_cleanup.cleanup_attempt(import_job_id, p["id"])

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            content = fh.read()
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")

        rows = importer.parse_content(content, fmt=fmt)
        db.execute("UPDATE import_jobs SET total_rows=? WHERE id=?",
                   (len(rows), import_job_id))

        result = importer.process_rows(
            rows, import_job_id, auto_create_devices=auto_create,
            import_attempt_id=attempt_id, actor=actor)
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")
        return result["summary"]
    except Exception as exc:
        importer.fail_attempt(attempt_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        locks.release(IMPORT_LOCK_KEY, token)
        # Best-effort cleanup of the uploaded scratch file.
        try:
            os.remove(path)
        except OSError:
            pass
