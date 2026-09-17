"""Database-backed advisory lock for single-flight / duplicate-execution guard.

Works across threads and processes because it lives in SQLite. ``acquire`` is an
atomic insert-or-fail: only one holder ever wins for a given ``lock_key``.
Expired locks (a holder that crashed and never released) can be stolen so the
work is not blocked forever — this is the timeout/recovery path.
"""
from __future__ import annotations

import uuid
from typing import Optional

from .. import db
from ..utils import iso, parse_ts


def _now_dt():
    from ..utils import utcnow
    return utcnow()


def acquire(lock_key: str, ttl_seconds: float = 300.0,
            token: Optional[str] = None) -> Optional[str]:
    """Try to take ``lock_key``. Returns the owner token on success, else None.

    A held lock that has passed its expiry is considered abandoned and is
    atomically taken over by the caller.
    """
    token = token or uuid.uuid4().hex
    now = _now_dt()
    from datetime import timedelta
    expires = now + timedelta(seconds=ttl_seconds)

    with db.transaction():
        conn = db.get_conn()
        # Clean out expired lock first, but only if it isn't ours already.
        conn.execute(
            "DELETE FROM task_locks WHERE lock_key=? AND expires_at <= ?",
            (lock_key, iso(now)))
        try:
            conn.execute(
                "INSERT INTO task_locks (lock_key, lock_token, acquired_at, expires_at) "
                "VALUES (?,?,?,?)",
                (lock_key, token, iso(now), iso(expires)))
        except Exception:
            # UNIQUE collision -> someone else holds a live lock.
            return None
    return token


def refresh(lock_key: str, token: str, ttl_seconds: float = 300.0) -> bool:
    """Extend a held lock (heartbeat). Returns False if we no longer own it."""
    from datetime import timedelta
    expires = _now_dt() + timedelta(seconds=ttl_seconds)
    cur = db.get_conn().execute(
        "UPDATE task_locks SET expires_at=? WHERE lock_key=? AND lock_token=?",
        (iso(expires), lock_key, token))
    db.get_conn().commit()
    return cur.rowcount == 1


def release(lock_key: str, token: str) -> None:
    db.get_conn().execute(
        "DELETE FROM task_locks WHERE lock_key=? AND lock_token=?",
        (lock_key, token))
    db.get_conn().commit()


def is_held(lock_key: str) -> bool:
    row = db.query_one(
        "SELECT lock_token, expires_at FROM task_locks WHERE lock_key=?",
        (lock_key,))
    if row is None:
        return False
    if row["expires_at"] and parse_ts(row["expires_at"]) < _now_dt():
        return False
    return True
