"""SQLite database access.

A single helper (:class:`Database`) wraps a connection and exposes the small set
of primitives the rest of the app uses. SQLite is opened with WAL mode and a
busy timeout so the HTTP threads and the background worker can operate on the
same file safely. Each thread uses its own connection.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Devices -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT NOT NULL UNIQUE,          -- business identifier, e.g. BAL-001
    name          TEXT NOT NULL,
    device_type   TEXT NOT NULL DEFAULT 'generic',
    manufacturer  TEXT,
    model         TEXT,
    serial_number TEXT,
    location      TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Calibration records -----------------------------------------------------
CREATE TABLE IF NOT EXISTS calibrations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    calibrated_at TEXT NOT NULL,                  -- ISO-8601 timestamp
    result        TEXT NOT NULL,                  -- pass | fail | conditional
    technician    TEXT,
    measured_value REAL,
    nominal_value  REAL,
    tolerance      REAL,
    unit           TEXT,
    notes          TEXT,
    source         TEXT NOT NULL DEFAULT 'manual', -- manual | import:<job_id>
    import_row     INTEGER,
    content_hash   TEXT NOT NULL,                 -- duplicate detection key
    created_at     TEXT NOT NULL,
    UNIQUE(device_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_cal_device_time
    ON calibrations(device_id, calibrated_at);

-- Run events --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    severity    TEXT NOT NULL,                    -- info | warning | critical
    code        TEXT,                             -- machine code, e.g. E_OVERHEAT
    message     TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_device_time
    ON events(device_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_severity ON events(severity);

-- Issues (auto-generated pending problems) --------------------------------
CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | monitoring | resolved
    severity      TEXT NOT NULL DEFAULT 'warning',
    title         TEXT NOT NULL,
    description   TEXT,
    rule          TEXT NOT NULL,                  -- rule that created it
    trigger_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    first_event_at  TEXT,
    last_event_at   TEXT,
    event_count     INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT,
    resolution      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issue_device ON issues(device_id);
CREATE INDEX IF NOT EXISTS idx_issue_status ON issues(status);

-- Issue <-> event link (which events belong to which issue) ---------------
CREATE TABLE IF NOT EXISTS issue_events (
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (issue_id, event_id)
);

-- Background jobs ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name    TEXT NOT NULL,
    idempotency_key TEXT,                       -- duplicate-enqueue guard
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending', -- pending|running|success|failed|retrying|timeout
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_lock     TEXT,                            -- worker token while running
    run_started_at TEXT,
    scheduled_at TEXT NOT NULL,
    next_run_at  TEXT NOT NULL,
    heartbeat_at TEXT,
    last_error   TEXT,
    result_json  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_lock ON jobs(run_lock);
-- Only one live (non-terminal) job per idempotency key at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_idem
    ON jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL
      AND status IN ('pending','running','retrying');

-- Import jobs (structured calibration import) -----------------------------
CREATE TABLE IF NOT EXISTS import_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|processing|done|failed
    total_rows    INTEGER NOT NULL DEFAULT 0,
    accepted_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    error_rows    INTEGER NOT NULL DEFAULT 0,
    summary_json  TEXT,
    errors_json   TEXT,                           -- per-row error detail
    created_at    TEXT NOT NULL,
    finished_at   TEXT
);

-- Generic idempotency / single-flight lock (duplicate execution protection)
CREATE TABLE IF NOT EXISTS task_locks (
    lock_key   TEXT PRIMARY KEY,
    lock_token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """Return a thread-local connection."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(
            config.DB_PATH,
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    """Commit on success, rollback on error."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    """Create tables on first run."""
    conn = get_conn()
    conn.executescript(_SCHEMA)
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    return cur.fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    return cur.fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute within an implicit transaction; return lastrowid."""
    conn = get_conn()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.lastrowid


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
