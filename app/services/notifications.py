"""Minimal notification outbox.

Imports emit one notification per issue transition they cause. The undo flow
revokes notifications that are still ``pending``; ones already handed to the
outside world (``sent``) cannot be unsent, so undo raises a compensation item
instead of pretending they were withdrawn.
"""
from __future__ import annotations

from typing import Optional

from .. import db
from ..utils import iso


def create(kind: str, message: str, import_job_id: Optional[int] = None,
           issue_id: Optional[int] = None) -> int:
    return db.execute(
        """INSERT INTO notifications (kind, import_job_id, issue_id, message, status, created_at)
           VALUES (?,?,?,?, 'pending', ?)""",
        (kind, import_job_id, issue_id, message, iso()))


def get(notification_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM notifications WHERE id=?", (notification_id,))
    return dict(row) if row else None


def list_notifications(status: Optional[str] = None,
                       import_job_id: Optional[int] = None,
                       limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM notifications WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if import_job_id is not None:
        sql += " AND import_job_id=?"
        params.append(import_job_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in db.query(sql, params)]


def mark_sent(notification_id: int) -> bool:
    """Mark a pending notification as handed to the outside world.

    Stands in for the delivery gateway (and is used by tests to exercise the
    'cannot unsend' compensation path). Returns False if it was not pending.
    """
    cur = db.get_conn().execute(
        "UPDATE notifications SET status='sent', sent_at=? WHERE id=? AND status='pending'",
        (iso(), notification_id))
    db.get_conn().commit()
    return cur.rowcount == 1
