"""Background task: run a calibration import asynchronously.

The HTTP handler uploads the file, records a pending ``import_jobs`` row and
enqueues this task. The task:

* takes a single-flight advisory lock so two imports never run concurrently
  (duplicate-execution protection) — the same lock the undo flow takes, so an
  import and an undo never overlap either,
* makes the run idempotent by clearing any rows from a previous attempt
  (retries then reproduce a correct result rather than double-counting); the
  cleared rows' lineage is marked ``cleared_by_retry`` so undo still sees the
  full attempt history,
* heartbeats while processing so a wedged import is detected as timed out,
* records the full report (accepted / duplicate / errors) on ``import_jobs``.

The uploaded file is kept after the run as the batch's source artifact; the
undo flow (or manual cleanup) removes it.
"""
from __future__ import annotations

from .. import config, db
from ..services import importer
from ..services import undo as undo_service
from . import locks
from .runner import task

IMPORT_LOCK_TTL = max(600.0, config.TASK_TIMEOUT_SECONDS * 2)


@task("import_calibrations")
def run_import(payload: dict, ctx) -> dict:
    import_job_id = payload["import_job_id"]
    path = payload["path"]
    fmt = payload.get("fmt", "csv")
    auto_create = bool(payload.get("auto_create_devices", False))
    attempt = getattr(ctx, "attempt", 1)

    # Single-flight: only one import/undo at a time across workers/processes.
    token = locks.acquire(config.IMPORT_LOCK_KEY, ttl_seconds=IMPORT_LOCK_TTL)
    if token is None:
        # Another import/undo is running; raise so this job retries shortly.
        raise RuntimeError("another import or undo is already running; will retry")
    try:
        # Idempotency: wipe rows from any earlier attempt (lineage kept as
        # 'cleared_by_retry') so a retry rebuilds the exact result instead of
        # counting them as duplicates.
        undo_service.clear_previous_attempt(import_job_id, attempt)

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            content = fh.read()
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")

        rows = importer.parse_content(content, fmt=fmt)
        db.execute("UPDATE import_jobs SET status='processing', total_rows=? WHERE id=?",
                   (len(rows), import_job_id))

        result = importer.process_rows(rows, import_job_id,
                                       auto_create_devices=auto_create,
                                       attempt=attempt)
        if not ctx.heartbeat():
            raise RuntimeError("lost job lock during import")
        return result["summary"]
    finally:
        locks.release(config.IMPORT_LOCK_KEY, token)
