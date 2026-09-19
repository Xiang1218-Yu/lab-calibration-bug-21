"""Cleanup of a single import attempt before a retry.

The old import task did ``DELETE FROM calibrations WHERE source='import:...'``
blindly. That double-deleted manual-content twins and left issue state changed
without compensation. This module removes only what the *attempt* wrote,
inverts still-terminal transitions exactly like the user-facing undo, and
queues compensation items when the state has since moved on. It is deliberately
scoped per-attempt so repeated retries converge deterministically.
"""
from __future__ import annotations

from .. import db
from ..utils import iso
from . import (attachments as attachment_service,
               compensation as comp_service,
               notifications as notification_service)


def cleanup_attempt(import_job_id: int, attempt_id: int) -> dict:
    """Remove rows/notifications/attachments of one attempt. Returns stats."""
    deleted = compensated = retracted = revoked = 0
    cals = db.query(
        "SELECT * FROM calibrations WHERE import_attempt_id=?", (attempt_id,))

    with db.transaction(immediate=True):
        for r in cals:
            cal = dict(r)
            if cal["source"] == "manual":
                continue
            trans = db.query(
                "SELECT * FROM issue_transitions WHERE cause_type='calibration' "
                "AND cause_id=? AND from_status != to_status ORDER BY id",
                (cal["id"],))
            trans = [dict(t) for t in trans]
            terminal_row = db.query_one(
                "SELECT id FROM issue_transitions WHERE device_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (cal["device_id"],))
            terminal_id = terminal_row["id"] if terminal_row else None

            if trans and trans[-1]["id"] == terminal_id:
                # Still the latest word: invert, then delete.
                t = trans[-1]
                cur = db.get_conn().execute(
                    "UPDATE issues SET status=?, resolved_at=?, resolution=?, updated_at=? "
                    "WHERE id=? AND status=?",
                    (t["from_status"],
                     t["from_resolved_at"] if t["from_status"] == "resolved" else None,
                     t["from_resolution"] if t["from_status"] == "resolved" else None,
                     iso(), t["issue_id"], t["to_status"]))
                if cur.rowcount == 1:
                    db.get_conn().execute(
                        "DELETE FROM calibrations WHERE id=? AND source!='manual'",
                        (cal["id"],))
                    deleted += 1
                    continue
            if trans:
                # State moved on: retain + compensate.
                comp_service.queue(
                    kind="retry_artifact", import_job_id=import_job_id,
                    import_attempt_id=attempt_id,
                    ref_type="calibration", ref_id=cal["id"], severity="critical",
                    summary=f"重试清理：校准 {cal['id']} 的状态已被后续变化依赖，"
                            "记录已保留，需人工处理")
                compensated += 1
            else:
                db.get_conn().execute(
                    "DELETE FROM calibrations WHERE id=? AND source!='manual'",
                    (cal["id"],))
                deleted += 1

        # Retract notifications this attempt emitted.
        for n in notification_service.list_notifications(
                import_job_id=import_job_id, limit=10000):
            if n["import_attempt_id"] != attempt_id:
                continue
            if n["type"] == "batch_reverted":
                continue
            if n["status"] in ("read", "acknowledged"):
                comp_service.queue(
                    kind="notification_read", import_job_id=import_job_id,
                    import_attempt_id=attempt_id, ref_type="notification",
                    ref_id=n["id"],
                    summary=f"重试清理：通知 {n['id']} 已读，无法撤回")
                compensated += 1
            else:
                notification_service.retract(n)
                retracted += 1

        # Revoke this attempt's active attachments.
        for a in attachment_service.list_for_batch(import_job_id):
            if a["import_attempt_id"] == attempt_id and a["status"] == "active":
                attachment_service.revoke(a)
                revoked += 1

        db.execute(
            "UPDATE import_attempts SET status='cleaned', finished_at=? WHERE id=?",
            (iso(), attempt_id))

    return {"deleted": deleted, "compensated": compensated,
            "notifications_retracted": retracted, "attachments_revoked": revoked}
