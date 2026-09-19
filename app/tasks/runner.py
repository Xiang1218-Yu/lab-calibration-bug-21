"""Background task framework.

Features
--------
* **Durable queue** — jobs are rows in ``jobs``; enqueue is transactional.
* **Duplicate-enqueue protection** — an optional ``idempotency_key`` plus a
  partial unique index ensures the same logical work is queued once while it is
  still live.
* **Single-flight execution** — claiming a job is an atomic conditional
  ``UPDATE ... WHERE status IN ('pending','retrying')``; only one worker wins.
  An additional advisory lock (``app.tasks.locks``) guards task *types* that must
  never overlap (e.g. one import at a time).
* **Failure retry with backoff** — a task that raises is rescheduled
  ``retrying`` with exponential backoff until ``max_attempts``, then ``failed``.
* **Timeout recovery** — each running task must heartbeat. A worker that dies
  (or a task that hangs past its timeout) leaves a stale ``running`` row; the
  next sweep detects the stale heartbeat, marks the attempt ``timeout`` and
  retries/fails it. Recovery is safe to run from every worker because reclaim is
  conditional on the old lock token.

The worker runs in a daemon thread and is started lazily / via :mod:`app.main`.
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from datetime import timedelta
from typing import Any, Callable, Optional

from .. import config, db
from ..utils import from_json, iso, parse_ts, to_json, utcnow

# Task registry: task_name -> fn(payload: dict, ctx: JobContext) -> Any
TASKS: dict[str, Callable[[dict, "JobContext"], Any]] = {}

_BACKOFF_BASE_SECONDS = 5.0
_HEARTBEAT_INTERVAL = 2.0


class TaskError(RuntimeError):
    pass


class JobContext:
    """Handed to every task. Supports cooperative cancellation & heartbeat."""

    def __init__(self, job_id: int, attempt: int, token: str, payload: dict):
        self.job_id = job_id
        self.attempt = attempt
        self.token = token
        self.payload = payload
        self.cancel_event = threading.Event()
        self._last_heartbeat = utcnow()

    def heartbeat(self) -> bool:
        """Record progress. Returns False if the job was lost (timeout/reclaim)."""
        self._last_heartbeat = utcnow()
        cur = db.get_conn().execute(
            "UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=? AND run_lock=?",
            (iso(), iso(), self.job_id, self.token))
        db.commit()
        return cur.rowcount == 1

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def sleep(self, seconds: float) -> bool:
        """Cancellable sleep. Returns False if cancelled."""
        return not self.cancel_event.wait(seconds)


def task(name: str):
    """Decorator registering a task handler."""
    def deco(fn):
        TASKS[name] = fn
        return fn
    return deco


# -- enqueue ----------------------------------------------------------------

def enqueue(task_name: str, payload: Optional[dict] = None,
            idempotency_key: Optional[str] = None,
            max_attempts: Optional[int] = None,
            delay_seconds: float = 0.0) -> dict:
    """Create a job. If an idempotency key matches a live job, return that one."""
    if task_name not in TASKS:
        raise TaskError(f"no task registered named {task_name!r}")
    now = utcnow()
    run_at = now + timedelta(seconds=delay_seconds)
    max_attempts = config.TASK_MAX_RETRIES + 1 if max_attempts is None else max_attempts

    if idempotency_key:
        existing = db.query_one(
            "SELECT * FROM jobs WHERE idempotency_key=? AND status IN ('pending','running','retrying')",
            (idempotency_key,))
        if existing:
            return dict(existing)

    try:
        job_id = db.execute(
            """INSERT INTO jobs
               (task_name, idempotency_key, payload_json, status, attempts,
                max_attempts, scheduled_at, next_run_at, created_at, updated_at)
               VALUES (?,?,?, 'pending', 0, ?, ?, ?, ?, ?)""",
            (task_name, idempotency_key, to_json(payload or {}),
             max_attempts, iso(now), iso(run_at), iso(now), iso(now)))
    except Exception:
        # Unique-index race: another thread enqueued the same idempotency key.
        if idempotency_key:
            existing = db.query_one(
                "SELECT * FROM jobs WHERE idempotency_key=? AND status IN ('pending','running','retrying')",
                (idempotency_key,))
            if existing:
                return dict(existing)
        raise
    return dict(db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,)))


# -- claiming / recovery ----------------------------------------------------

def _recover_stale() -> int:
    """Reclaim jobs stuck in 'running' past the timeout (dead/hung worker).

    Returns number recovered. Safe to call repeatedly and from many workers.
    """
    now = utcnow()
    grace = config.TASK_TIMEOUT_SECONDS * 1.5
    recovered = 0
    stale = db.query(
        "SELECT * FROM jobs WHERE status='running' AND run_lock IS NOT NULL")
    for job in stale:
        hb = parse_ts(job["heartbeat_at"]) or parse_ts(job["run_started_at"])
        if hb is None:
            continue
        if (now - hb).total_seconds() < grace:
            continue
        # Atomically take over: only if the old token is still the holder.
        attempts = job["attempts"]
        if attempts >= job["max_attempts"]:
            new_status, next_run = config.TASK_TIMEOUT, iso(now)
            last_error = "task timed out repeatedly; no attempts left"
        else:
            new_status, next_run = config.TASK_RETRYING, iso(now)
            last_error = f"worker lost / task exceeded {config.TASK_TIMEOUT_SECONDS:.0f}s timeout"
        cur = db.get_conn().execute(
            """UPDATE jobs SET status=?, run_lock=NULL, next_run_at=?,
               last_error=?, updated_at=?
               WHERE id=? AND status='running' AND run_lock=?""",
            (new_status, next_run, last_error, iso(now), job["id"], job["run_lock"]))
        db.commit()
        if cur.rowcount == 1:
            recovered += 1
    return recovered


def _claim_next() -> Optional[dict]:
    """Atomically claim one due job. Returns the job row or None."""
    token = uuid.uuid4().hex
    now = utcnow()
    now_iso = iso(now)
    candidates = db.query(
        """SELECT id FROM jobs
           WHERE status IN ('pending','retrying') AND next_run_at <= ?
           ORDER BY next_run_at, id LIMIT 5""", (now_iso,))
    for cand in candidates:
        cur = db.get_conn().execute(
            """UPDATE jobs SET status='running', run_lock=?, run_started_at=?,
               heartbeat_at=?, attempts=attempts+1, updated_at=?
               WHERE id=? AND status IN ('pending','retrying') AND next_run_at<=?""",
            (token, now_iso, now_iso, now_iso, cand["id"], now_iso))
        db.commit()
        if cur.rowcount == 1:
            return dict(db.query_one("SELECT * FROM jobs WHERE id=?", (cand["id"],)))
    return None


def _schedule_retry(job: dict, error: str) -> None:
    attempts = job["attempts"]  # already incremented at claim
    now = utcnow()
    if attempts >= job["max_attempts"]:
        db.get_conn().execute(
            "UPDATE jobs SET status='failed', run_lock=NULL, last_error=?, updated_at=? WHERE id=?",
            (error, iso(now), job["id"]))
    else:
        backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
        nxt = now + timedelta(seconds=backoff)
        db.get_conn().execute(
            "UPDATE jobs SET status='retrying', run_lock=NULL, last_error=?, next_run_at=?, updated_at=? WHERE id=?",
            (error, iso(nxt), iso(now), job["id"]))
    db.commit()


def _mark_success(job: dict, result: Any) -> None:
    db.get_conn().execute(
        "UPDATE jobs SET status='success', run_lock=NULL, result_json=?, last_error=NULL, updated_at=? WHERE id=?",
        (to_json({"result": result}), iso(), job["id"]))
    db.commit()


# -- execution --------------------------------------------------------------

def _run_job(job: dict) -> None:
    name = job["task_name"]
    fn = TASKS.get(name)
    ctx = JobContext(job["id"], job["attempts"], job["run_lock"],
                     from_json(job["payload_json"], {}))
    if fn is None:
        _schedule_retry(job, f"unknown task {name!r}")
        return

    box: dict = {}

    def target():
        try:
            box["result"] = fn(ctx.payload, ctx)
        except Exception as e:  # captured, retried by the worker
            box["error"] = f"{type(e).__name__}: {e}"
            box["trace"] = traceback.format_exc()

    worker = threading.Thread(target=target, name=f"job-{job['id']}", daemon=True)
    worker.start()
    worker.join(config.TASK_TIMEOUT_SECONDS)

    if worker.is_alive():
        # Hard timeout: ask the task to stop and hand it to retry/fail path.
        ctx.cancel_event.set()
        _schedule_retry(job, f"task timed out after {config.TASK_TIMEOUT_SECONDS:.0f}s")
        return
    if "error" in box:
        _schedule_retry(job, box["error"])
        return
    _mark_success(job, box.get("result"))


# -- worker loop ------------------------------------------------------------

class Worker:
    def __init__(self, poll_interval: Optional[float] = None):
        self.poll_interval = poll_interval or config.TASK_POLL_INTERVAL
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="caltrack-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._stop.wait(self.poll_interval)

    def tick(self) -> int:
        """One recovery + drain cycle. Returns number of jobs processed."""
        _recover_stale()
        processed = 0
        while not self._stop.is_set():
            job = _claim_next()
            if job is None:
                break
            _run_job(job)
            processed += 1
        return processed


_default_worker: Optional[Worker] = None


def start_worker() -> Worker:
    global _default_worker
    if _default_worker is None:
        _default_worker = Worker()
    _default_worker.start()
    return _default_worker


def run_once() -> int:
    """Process everything due right now and return count (used by tests/CLI)."""
    return Worker().tick()
