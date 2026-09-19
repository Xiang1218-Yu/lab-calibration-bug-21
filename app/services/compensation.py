"""Compensation work-list service.

Whenever an undo cannot safely restore something automatically, it records a
``compensation_items`` row instead of pretending success. Items are idempotent:
the partial unique index ``uq_comp_active`` collapses repeated attempts
(retry cleanup, re-running a partial undo) into one pending item.
"""
from __future__ import annotations

from typing import Optional

from .. import db
from ..utils import iso, to_json


class CompensationError(ValueError):
    pass


KINDS = (
    "dependent_record",     # a later calibration depends on this one
    "issue_state_moved",    # issue state moved after the import; human review
    "manual_twin",          # an identical-content manual record exists
    "notification_read",    # already-read notification cannot be retracted
    "attachment_file",      # file quarantine/removal failed
    "retry_artifact",       # leftover from an earlier import attempt
)


def queue(*, kind: str, summary: str, import_job_id: int,
          undo_batch_id: Optional[int] = None,
          import_attempt_id: Optional[int] = None,
          ref_type: Optional[str] = None,
          ref_id: Optional[int] = None,
          severity: str = "warning",
          detail: Optional[dict] = None) -> Optional[int]:
    """Insert a pending item unless an identical pending one exists.

    Returns the item id (existing or new), or None when a non-pending item
    already occupies the dedupe slot.
    """
    if kind not in KINDS:
        raise CompensationError(f"unknown kind {kind!r}")
    try:
        return db.execute(
            """INSERT INTO compensation_items
               (undo_batch_id, import_job_id, import_attempt_id, kind, severity,
                status, ref_type, ref_id, summary, detail_json, created_at)
               VALUES (?,?,?,?,?, 'pending', ?,?,?,?,?)""",
            (undo_batch_id, import_job_id, import_attempt_id, kind, severity,
             ref_type, ref_id, summary, to_json(detail or {}), iso()))
    except Exception:
        # Unique-index collision: an identical pending item exists already.
        row = db.query_one(
            """SELECT id FROM compensation_items
               WHERE kind=? AND COALESCE(ref_type,'')=COALESCE(?,'')
                 AND COALESCE(ref_id,-1)=COALESCE(?,-1)
                 AND import_job_id=? AND status='pending'""",
            (kind, ref_type, ref_id, import_job_id))
        return row["id"] if row else None


def get(item_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM compensation_items WHERE id=?", (item_id,))
    if not row:
        return None
    d = dict(row)
    from ..utils import from_json
    d["detail"] = from_json(d.pop("detail_json"), {})
    return d


def list_items(status: Optional[str] = "pending",
               import_job_id: Optional[int] = None,
               kind: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM compensation_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if import_job_id is not None:
        sql += " AND import_job_id=?"
        params.append(import_job_id)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id"
    return [get(r["id"]) for r in db.query(sql, params)]


def resolve(item_id: int, *, resolution: str, resolved_by: str,
            new_status: str = "resolved") -> dict:
    if new_status not in ("resolved", "ignored"):
        raise CompensationError("new_status must be resolved or ignored")
    row = db.query_one("SELECT * FROM compensation_items WHERE id=?", (item_id,))
    if row is None:
        raise CompensationError(f"compensation {item_id} not found")
    if row["status"] != "pending":
        return get(item_id)
    db.execute(
        """UPDATE compensation_items SET status=?, resolved_at=?, resolved_by=?,
           resolution=? WHERE id=? AND status='pending'""",
        (new_status, iso(), resolved_by, resolution, item_id))
    return get(item_id)
