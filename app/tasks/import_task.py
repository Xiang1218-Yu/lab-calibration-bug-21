"""Background task: run a calibration import asynchronously.

The HTTP handler uploads the file, records a pending ``import_jobs`` row and
enqueues this task. The task:

* takes a single-flight advisory lock so two imports never run concurrently
  (duplicate-execution protection),
* makes the run idempotent by clearing any rows from a previous attempt
  (retries then reproduce a correct result rather than double-counting),
* heartbeats while processing so a wedged import is detected as timed out,
* records the full report (accepted / duplicate / errors) on ``import_jobs``.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import config, db
from ..services import importer
from ..utils import iso, to_json
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

    # Single-flight: only one import at a time across workers/processes.
    token = locks.acquire(IMPORT_LOCK_KEY, ttl_seconds=IMPORT_LOCK_TTL)
    if token is None:
        # Another import is running; raise so this job retries shortly.
        raise RuntimeError("another import is already running; will retry")
    try:
        # Idempotency: wipe rows from any earlier attempt so a retry rebuilds
        # the exact result instead of counting them as duplicates.
        db.execute(
            "DELETE FROM calibrations WHERE source=?",
            (f"import:{import_job_id}",))

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            content = fh.read()
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")

        rows = importer.parse_content(content, fmt=fmt)
        db.execute("UPDATE import_jobs SET status='processing', total_rows=? WHERE id=?",
                   (len(rows), import_job_id))

        result = importer.process_rows(rows, import_job_id,
                                       auto_create_devices=auto_create)
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")
        return result["summary"]
    finally:
        locks.release(IMPORT_LOCK_KEY, token)
        # Best-effort cleanup of the uploaded scratch file.
        try:
            os.remove(path)
        except OSError:
            pass
