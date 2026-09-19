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

-- Per-attempt history of an import batch (a retried job leaves a trail here) -
CREATE TABLE IF NOT EXISTS import_attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id  INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    job_id         INTEGER REFERENCES jobs(id),
    attempt_no     INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running', -- running|succeeded|failed|cleaned
    accepted_rows  INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    error_rows     INTEGER NOT NULL DEFAULT 0,
    summary_json   TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    UNIQUE(import_job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_import_attempt_job ON import_attempts(import_job_id);

-- Attachments linked to a batch or to a single imported calibration --------
CREATE TABLE IF NOT EXISTS attachments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    attachable_type   TEXT NOT NULL,   -- import_job | calibration
    attachable_id     INTEGER NOT NULL,
    filename          TEXT NOT NULL,
    path              TEXT,            -- NULL = external reference, nothing to quarantine
    content_hash      TEXT,
    size_bytes        INTEGER,
    source            TEXT NOT NULL DEFAULT 'manual', -- manual | import:<job_id>
    import_job_id     INTEGER,
    import_attempt_id INTEGER,
    created_by        TEXT,
    status            TEXT NOT NULL DEFAULT 'active', -- active|revoked
    created_at        TEXT NOT NULL,
    revoked_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_batch ON attachments(import_job_id);
CREATE INDEX IF NOT EXISTS idx_att_target ON attachments(attachable_type, attachable_id);

-- Notifications emitted when import-driven work changes issue state --------
CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    type              TEXT NOT NULL,   -- issue_state | batch_reverted
    device_id         INTEGER,
    issue_id          INTEGER,
    calibration_id    INTEGER,
    import_job_id     INTEGER,
    import_attempt_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'queued',
        -- queued | sent | read | acknowledged | cancelled | retracted
    title             TEXT NOT NULL,
    message           TEXT,
    channels_json     TEXT NOT NULL DEFAULT '[]',
    read_at           TEXT,
    retracted_at      TEXT,
    created_by        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_batch ON notifications(import_job_id);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);

-- Append-only audit trail of every issue status change (the inverse log) ---
CREATE TABLE IF NOT EXISTS issue_transitions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id          INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    device_id         INTEGER NOT NULL,
    from_status       TEXT,
    to_status         TEXT NOT NULL,
    action            TEXT NOT NULL,   -- resolved|monitoring|reopened|held_open|manual|restored
    cause_type        TEXT NOT NULL,   -- calibration | manual | import_undo
    cause_id          INTEGER,         -- calibration id when cause_type='calibration'
    import_job_id     INTEGER,
    import_attempt_id INTEGER,
    actor             TEXT,
    from_resolved_at  TEXT,            -- snapshot so a restore is byte-exact
    from_resolution   TEXT,
    reversal_of_id    INTEGER REFERENCES issue_transitions(id),
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_itrans_issue ON issue_transitions(issue_id, id);
CREATE INDEX IF NOT EXISTS idx_itrans_device ON issue_transitions(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_itrans_cause ON issue_transitions(cause_type, cause_id);
CREATE INDEX IF NOT EXISTS idx_itrans_batch ON issue_transitions(import_job_id);

-- Undo / revert orders for an import batch ---------------------------------
CREATE TABLE IF NOT EXISTS undo_batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_job_id   INTEGER NOT NULL REFERENCES import_jobs(id),
    idem_key        TEXT,              -- caller idempotency key (commit phase)
    status          TEXT NOT NULL DEFAULT 'previewed',
        -- previewed | superseded | running | completed | partial | failed
    requested_by    TEXT,
    reason          TEXT,
    fingerprint     TEXT NOT NULL,    -- state digest the confirmation is bound to
    confirm_token   TEXT NOT NULL,
    preview_json    TEXT NOT NULL,
    result_json     TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    started_at      TEXT,
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_undo_batch_job ON undo_batches(import_job_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_undo_idem ON undo_batches(idem_key)
    WHERE idem_key IS NOT NULL AND status IN ('running','completed','partial');

-- Manual compensation work list (never silently "succeed" an unsafe undo) --
CREATE TABLE IF NOT EXISTS compensation_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    undo_batch_id     INTEGER REFERENCES undo_batches(id),
    import_job_id     INTEGER NOT NULL,
    import_attempt_id INTEGER,
    kind              TEXT NOT NULL,
        -- dependent_record | issue_state_moved | manual_twin
        -- | notification_read | attachment_file | retry_artifact
    severity          TEXT NOT NULL DEFAULT 'warning', -- info|warning|critical
    status            TEXT NOT NULL DEFAULT 'pending', -- pending|resolved|ignored
    ref_type          TEXT,
    ref_id            INTEGER,
    summary           TEXT NOT NULL,
    detail_json       TEXT,
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    resolved_by       TEXT,
    resolution        TEXT
);
CREATE INDEX IF NOT EXISTS idx_comp_status ON compensation_items(status);
CREATE INDEX IF NOT EXISTS idx_comp_batch ON compensation_items(import_job_id);
-- Never queue the same pending compensation twice (idempotent compensation).
CREATE UNIQUE INDEX IF NOT EXISTS uq_comp_active
    ON compensation_items(kind, ref_type, ref_id, import_job_id)
    WHERE status='pending';
"""

# Columns added after the original schema; applied to pre-existing databases.
_MIGRATIONS = (
    ("calibrations", "import_job_id",
     "ALTER TABLE calibrations ADD COLUMN import_job_id INTEGER"),
    ("calibrations", "import_attempt_id",
     "ALTER TABLE calibrations ADD COLUMN import_attempt_id INTEGER"),
)

_local = threading.local()

# Tests may point the app at a dedicated connection factory; normally unset.
_conn_factory = None


def set_conn_factory(factory) -> None:
    """Override how connections are created (used for isolated test DBs)."""
    global _conn_factory
    _conn_factory = factory
    _local.conn = None


def _make_conn() -> sqlite3.Connection:
    if _conn_factory is not None:
        conn = _conn_factory()
    else:
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
    return conn


def get_conn() -> sqlite3.Connection:
    """Return a thread-local connection."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _make_conn()
        _local.conn = conn
    return conn


@contextmanager
def transaction(immediate: bool = False):
    """Commit on success, rollback on error.

    With ``immediate=True`` the write lock is taken up front (``BEGIN IMMEDIATE``)
    so multi-step mutations cannot deadlock against a concurrent writer that
    upgrades lazily. Nested usage joins the outer transaction; only the
    outermost block commits, so a service helper never commits halfway through a
    larger undo unit of work.
    """
    conn = get_conn()
    depth = getattr(_local, "tx_depth", 0)
    if depth == 0:
        _local.tx_depth = 1
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            _local.tx_depth = 0
    else:
        # Inner block: no independent commit/rollback.
        _local.tx_depth = depth + 1
        try:
            yield conn
        finally:
            _local.tx_depth = depth


def init_db() -> None:
    """Create tables and apply additive migrations."""
    conn = get_conn()
    conn.executescript(_SCHEMA)
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    return cur.fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    cur = get_conn().execute(sql, tuple(params))
    return cur.fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """Execute; return lastrowid.

    Commits immediately when called standalone, but joins an enclosing
    :func:`transaction` (no mid-transaction commit).
    """
    conn = get_conn()
    cur = conn.execute(sql, tuple(params))
    if getattr(_local, "tx_depth", 0) == 0:
        conn.commit()
    return cur.lastrowid


def commit() -> None:
    """Commit unless we are inside a managed transaction."""
    if getattr(_local, "tx_depth", 0) == 0:
        get_conn().commit()


def rollback() -> None:
    if getattr(_local, "tx_depth", 0) == 0:
        get_conn().rollback()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
