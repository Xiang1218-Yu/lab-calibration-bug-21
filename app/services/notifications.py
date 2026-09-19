"""Notification service.

Notifications are produced when import-driven calibrations change an issue
(state notifications) and when an undo reverts a batch (``batch_reverted``).
Undo retracts the state notifications it had emitted, but it can never take
back one the user has already read: such rows are left in place and escalated
to a manual :mod:`compensation` item instead ("can't un-send a read message").
"""
from __future__ import annotations

from typing import Optional

from .. import db
from ..utils import iso, to_json


class NotificationError(ValueError):
    pass


def emit(*, type_: str, title: str, message: Optional[str] = None,
         device_id: Optional[int] = None, issue_id: Optional[int] = None,
         calibration_id: Optional[int] = None,
         import_job_id: Optional[int] = None,
         import_attempt_id: Optional[int] = None,
         channels: Optional[list] = None,
         created_by: Optional[str] = None,
         status: str = "sent") -> dict:
    nid = db.execute(
        """INSERT INTO notifications
           (type, device_id, issue_id, calibration_id, import_job_id,
            import_attempt_id, status, title, message, channels_json,
            created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (type_, device_id, issue_id, calibration_id, import_job_id,
         import_attempt_id, status, title, message,
         to_json(channels or []), created_by, iso(), iso()))
    return get(nid)


def get(notification_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM notifications WHERE id=?", (notification_id,))
    if not row:
        return None
    d = dict(row)
    d["channels"] = __import__("json").loads(d.pop("channels_json") or "[]")
    return d


def mark_read(notification_id: int) -> dict:
    db.execute(
        "UPDATE notifications SET status='read', read_at=COALESCE(read_at,?), "
        "updated_at=? WHERE id=?", (iso(), iso(), notification_id))
    return get(notification_id)


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
    return [get(r["id"]) for r in db.query(sql, params)]


def batch_state_notifications(import_job_id: int) -> list[dict]:
    """State-change notifications a batch (any attempt) emitted."""
    return list_notifications(import_job_id=import_job_id)


def retract(n: dict) -> dict:
    """Flip to ``retracted``. Idempotent. Does not retract read/acknowledged
    rows — the caller decides to compensate those instead.
    """
    if n["status"] in ("read", "acknowledged"):
        return n
    if n["status"] == "retracted":
        return n
    db.execute(
        "UPDATE notifications SET status='retracted', retracted_at=?, updated_at=? "
        "WHERE id=?", (iso(), iso(), n["id"]))
    return get(n["id"])
