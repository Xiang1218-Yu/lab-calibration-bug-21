"""Auditable undo / compensation engine for import batches.

Records imported by a batch are never treated uniformly. Each calibration is
classified before anything is touched:

* ``deletable`` (A) — the record caused no issue-state change and nothing later
  depends on it. It can be removed outright.
* ``state_dependent`` (B) — a later state transition on the device happened
  *after* the record's effect, so deleting it and rewinding the issue would
  corrupt history. The record is retained and a manual compensation item is
  queued.
* ``state_driving`` (C) — the record's own transition is still the *latest*
  word on the issue. The record is deleted and that transition is inverted
  (state restored to the exact snapshot taken beforehand); a compensating
  ``restored`` audit row is written.

Safety properties enforced here:

* an identical-content **manual** record is never deleted (protected by
  ``source`` + content_hash; a twin is reported, not removed),
* every import **attempt** (retry history) is covered, not just the last,
* batch **attachments** are revoked/quarantined and **notifications** retracted,
* anything that cannot be undone automatically becomes a pending
  **compensation item** — an undo with blockers finishes ``partial``, never a
  silent ``completed``.

Two phases: :func:`preview` (dry run, fingerprinted) then :func:`commit` (the
confirmation must carry the token and match the fingerprint). Idempotent,
guarded by a per-batch advisory lock and an IMMEDIATE transaction.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional

from .. import db
from ..tasks import locks
from ..utils import from_json, iso, to_json
from . import (attachments as attachment_service,
               compensation as comp_service,
               notifications as notification_service)

UNDO_LOCK_TTL = 300.0


class UndoError(ValueError):
    """User-facing error (bad request / state conflict)."""


class UndoConflict(RuntimeError):
    """A concurrent undo or batch mutation interfered."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _batch(import_job_id: int) -> dict:
    row = db.query_one("SELECT * FROM import_jobs WHERE id=?", (import_job_id,))
    if row is None:
        raise UndoError(f"import job {import_job_id} not found")
    d = dict(row)
    d["summary"] = from_json(d.pop("summary_json", None), None)
    d["errors"] = from_json(d.pop("errors_json", None), [])
    return d


def _assert_revocable(batch: dict) -> None:
    status = batch["status"]
    if status == "revoked":
        raise UndoError(f"batch {batch['id']} is already revoked")
    if status in ("pending", "processing"):
        raise UndoConflict(
            f"batch {batch['id']} is {status}; wait for it to finish before undo")
    if status not in ("done", "failed", "partial"):
        raise UndoError(f"batch status {status!r} cannot be undone")


def imported_calibrations(import_job_id: int) -> list[dict]:
    """All calibrations currently present that a batch's attempts wrote.

    Prefers the explicit ``import_job_id`` column; falls back to the legacy
    ``source='import:<id>'`` marker for rows written before the column existed.
    """
    rows = db.query(
        """SELECT * FROM calibrations
           WHERE import_job_id=?
              OR (source=? AND import_job_id IS NULL)
           ORDER BY id""",
        (import_job_id, f"import:{import_job_id}"))
    return [dict(r) for r in rows]


def _manual_twin(cal: dict) -> Optional[dict]:
    """An existing manual record with the same dedup content hash."""
    row = db.query_one(
        """SELECT * FROM calibrations
           WHERE device_id=? AND content_hash=? AND source='manual'
           ORDER BY id LIMIT 1""",
        (cal["device_id"], cal["content_hash"]))
    return dict(row) if row else None


def _transitions_for_cal(cal_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM issue_transitions WHERE cause_type='calibration' "
        "AND cause_id=? ORDER BY id", (cal_id,))]


def _latest_transition_before(device_id: int, ts_iso: str) -> Optional[dict]:
    row = db.query_one(
        """SELECT * FROM issue_transitions WHERE device_id=? AND created_at < ?
           ORDER BY created_at DESC, id DESC LIMIT 1""",
        (device_id, ts_iso))
    return dict(row) if row else None


def _later_external_transition(device_id: int, ts_iso: str) -> Optional[dict]:
    """Any non-import transition strictly after the batch record (later state
    the device has since moved through — makes a blind rewind unsafe)."""
    row = db.query_one(
        """SELECT * FROM issue_transitions
           WHERE device_id=? AND created_at > ?
             AND (cause_type != 'calibration'
                  OR cause_id NOT IN (SELECT id FROM calibrations
                                      WHERE import_job_id IS NOT NULL))
           ORDER BY created_at ASC, id ASC LIMIT 1""",
        (device_id, ts_iso))
    return dict(row) if row else None


def _device_terminal_transition(device_id: int) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM issue_transitions WHERE device_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (device_id,))
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(import_job_id: int) -> dict:
    """Bucket every live batch calibration and gather side effects."""
    deletable, state_dependent, state_driving = [], [], []

    for cal in imported_calibrations(import_job_id):
        twin = _manual_twin(cal)
        trans = _transitions_for_cal(cal["id"])
        # A held_open (open->open) changes no state; it has nothing to invert.
        effective = [t for t in trans if t["from_status"] != t["to_status"]]
        terminal = _device_terminal_transition(cal["device_id"])
        later = _later_external_transition(cal["device_id"], cal["created_at"])

        entry = {
            "calibration_id": cal["id"],
            "device_id": cal["device_id"],
            "calibrated_at": cal["calibrated_at"],
            "result": cal["result"],
            "import_attempt_id": cal["import_attempt_id"],
            "transitions": trans,
            "manual_twin_id": twin["id"] if twin else None,
        }

        if twin is not None:
            # Identical manual content exists: this import row is a duplicate
            # of a manual record. Never delete; queue a twin compensation.
            entry["reason"] = "manual_twin"
            state_dependent.append(entry)
        elif effective and later is not None:
            # The record changed state, but the device has since moved on.
            entry["reason"] = "later_state_transition"
            entry["later_transition_id"] = later["id"]
            entry["later_action"] = later["action"]
            state_dependent.append(entry)
        elif effective and terminal is not None and terminal["id"] == effective[-1]["id"]:
            # This record's transition is still the latest word -> invertible.
            prev = _latest_transition_before(cal["device_id"], cal["created_at"])
            entry["inverse"] = _plan_inverse(effective, prev, cal)
            state_driving.append(entry)
        else:
            if effective:
                # Changed state but superseded by another batch/unknown change.
                entry["reason"] = "superseded_within_batch"
                state_dependent.append(entry)
            else:
                entry["reason"] = "no_state_effect"
                deletable.append(entry)

    return {
        "deletable": deletable,
        "state_dependent": state_dependent,
        "state_driving": state_driving,
    }


def _plan_inverse(transitions: list[dict], prev: Optional[dict],
                  cal: dict) -> dict:
    """Describe how to undo the (terminal) transition a record caused."""
    t = transitions[-1]
    restore_status = t["from_status"] or "open"
    return {
        "transition_id": t["id"],
        "issue_id": t["issue_id"],
        "from_status": t["to_status"],      # status before undo (current)
        "to_status": restore_status,        # status after undo
        "restore_resolved_at": (t["from_resolved_at"]
                                if restore_status == "resolved" else None),
        "restore_resolution": (t["from_resolution"]
                               if restore_status == "resolved" else None),
        "action": t["action"],
    }


# ---------------------------------------------------------------------------
# Side effects
# ---------------------------------------------------------------------------

def _side_effects(import_job_id: int) -> dict:
    atts = attachment_service.list_batch_originated(import_job_id)
    notifs = [n for n in notification_service.list_notifications(
        import_job_id=import_job_id, limit=10000)
        if n["type"] != "batch_reverted"]
    return {
        "attachments": [
            {"id": a["id"], "filename": a["filename"],
             "attachable_type": a["attachable_type"],
             "attachable_id": a["attachable_id"], "path": a["path"],
             "import_attempt_id": a["import_attempt_id"]}
            for a in atts],
        "notifications": [
            {"id": n["id"], "status": n["status"], "title": n["title"],
             "issue_id": n["issue_id"], "calibration_id": n["calibration_id"],
             "import_attempt_id": n["import_attempt_id"]}
            for n in notifs],
    }


def _attempts(import_job_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT id, attempt_no, status, accepted_rows, duplicate_rows, "
        "error_rows, started_at, finished_at FROM import_attempts "
        "WHERE import_job_id=? ORDER BY attempt_no", (import_job_id,))]


def _issues_touched(import_job_id: int) -> list[dict]:
    rows = db.query(
        """SELECT DISTINCT issue_id FROM issue_transitions
           WHERE import_job_id=?""", (import_job_id,))
    out = []
    for r in rows:
        i = db.query_one("SELECT id, device_id, status, resolved_at, resolution "
                         "FROM issues WHERE id=?", (r["issue_id"],))
        if i:
            out.append(dict(i))
    return out


# ---------------------------------------------------------------------------
# Fingerprint & preview
# ---------------------------------------------------------------------------

def _fingerprint(buckets: dict, sides: dict) -> str:
    h = hashlib.sha256()
    for name in ("deletable", "state_driving", "state_dependent"):
        ids = sorted(e["calibration_id"] for e in buckets[name])
        h.update(name.encode()); h.update(repr(ids).encode())
    h.update(repr(sorted(a["id"] for a in sides["attachments"])).encode())
    h.update(repr(sorted(n["id"] for n in sides["notifications"])).encode())
    return h.hexdigest()


def preview(import_job_id: int, *, requested_by: Optional[str] = None,
            reason: Optional[str] = None, force: bool = False) -> dict:
    """Build (and persist) a dry-run undo plan with a confirmation token."""
    batch = _batch(import_job_id)
    _assert_revocable(batch)

    buckets = classify(import_job_id)
    sides = _side_effects(import_job_id)
    fingerprint = _fingerprint(buckets, sides)

    n_delete = len(buckets["deletable"])
    n_drive = len(buckets["state_driving"])
    n_depend = len(buckets["state_dependent"])
    n_twin = sum(1 for e in buckets["state_dependent"]
                 if e.get("reason") == "manual_twin")
    read_notifs = [n for n in sides["notifications"]
                   if n["status"] in ("read", "acknowledged")]
    blockers = n_depend + len(read_notifs)

    plan = {
        "import_job": {"id": batch["id"], "filename": batch["filename"],
                       "status": batch["status"]},
        "attempts": _attempts(import_job_id),
        "counts": {
            "deletable": n_delete,
            "state_driving": n_drive,
            "state_dependent": n_depend,
            "manual_twins": n_twin,
            "attachments": len(sides["attachments"]),
            "notifications": len(sides["notifications"]),
            "notifications_read": len(read_notifs),
        },
        "buckets": buckets,
        "attachments": sides["attachments"],
        "notifications": sides["notifications"],
        "issues_before": _issues_touched(import_job_id),
        "blockers": blockers,
        "auto_recoverable": blockers == 0,
        "outcome_if_confirmed": "completed" if blockers == 0 else "partial",
        "fingerprint": fingerprint,
        "requested_by": requested_by,
        "reason": reason,
        "generated_at": iso(),
    }

    token = uuid.uuid4().hex
    with db.transaction(immediate=True):
        # Invalidate any prior unconfirmed preview for the same batch.
        db.execute(
            "UPDATE undo_batches SET status='superseded', finished_at=? "
            "WHERE import_job_id=? AND status='previewed'",
            (iso(), import_job_id))
        undo_id = db.execute(
            """INSERT INTO undo_batches
               (import_job_id, status, requested_by, reason, fingerprint,
                confirm_token, preview_json, created_at)
               VALUES (?, 'previewed', ?, ?, ?, ?, ?, ?)""",
            (import_job_id, requested_by, reason, fingerprint, token,
             to_json(plan), iso()))
    plan["undo_batch_id"] = undo_id
    plan["confirm_token"] = token
    # Convenience: expose the three buckets at the top level too.
    plan.update(buckets)
    return plan


def get_preview(undo_batch_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_batch_id,))
    if not row:
        return None
    plan = from_json(row["preview_json"], {})
    plan["undo_batch_id"] = row["id"]
    plan["status"] = row["status"]
    plan["confirm_token"] = row["confirm_token"]
    plan["fingerprint_stored"] = row["fingerprint"]
    for bucket in ("deletable", "state_dependent", "state_driving"):
        plan.setdefault(bucket, plan.get("buckets", {}).get(bucket, []))
    return plan


# ---------------------------------------------------------------------------
# Commit (execution)
# ---------------------------------------------------------------------------

def commit(import_job_id: int, confirm_token: str, *,
           requested_by: Optional[str] = None,
           expected_fingerprint: Optional[str] = None,
           idem_key: Optional[str] = None,
           force: bool = False) -> dict:
    """Execute a previewed undo. Returns the persisted result."""
    # Idempotency: a repeated commit with the same key returns the prior run.
    # Checked before the revocable-state guard so replaying against an already
    # revoked batch still yields the original (identical) result.
    if idem_key:
        prior = db.query_one(
            "SELECT * FROM undo_batches WHERE idem_key=? AND status IN "
            "('running','completed','partial') ORDER BY id DESC LIMIT 1",
            (idem_key,))
        if prior is not None:
            return _result_dict(prior, replayed=True)

    batch = _batch(import_job_id)
    _assert_revocable(batch)

    # Per-batch single-flight lock (cross thread/process).
    lock_key = f"undo:import:{import_job_id}"
    token = locks.acquire(lock_key, ttl_seconds=UNDO_LOCK_TTL)
    if token is None:
        raise UndoConflict(
            f"an undo for batch {import_job_id} is already running")

    files_to_quarantine: list[str] = []
    try:
        with db.transaction(immediate=True):
            row = db.query_one(
                "SELECT * FROM undo_batches WHERE import_job_id=? AND status='previewed' "
                "ORDER BY id DESC LIMIT 1", (import_job_id,))
            if row is None:
                raise UndoError("no valid preview; call preview first")
            if row["confirm_token"] != confirm_token:
                raise UndoError("confirm token does not match the latest preview")
            undo_id = row["id"]

            # Recompute the plan inside the lock and compare fingerprints so a
            # stale confirmation cannot undo a changed world.
            buckets = classify(import_job_id)
            sides = _side_effects(import_job_id)
            current_fp = _fingerprint(buckets, sides)
            if expected_fingerprint and expected_fingerprint != current_fp:
                raise UndoError(
                    "the batch changed since preview; re-run preview to get a "
                    "fresh diff")
            if row["fingerprint"] != current_fp and not force:
                raise UndoError(
                    "state drift detected (fingerprint mismatch); re-run preview "
                    "or confirm with force=true")

            db.execute("UPDATE undo_batches SET status='running', started_at=?, "
                       "requested_by=COALESCE(?, requested_by), idem_key=? WHERE id=?",
                       (iso(), requested_by, idem_key, undo_id))

            result = _execute(undo_id, import_job_id, buckets, sides, requested_by)
            files_to_quarantine = result.pop("_quarantine_files", [])

            final_status = "partial" if result["compensation_created"] else "completed"
            result["status"] = final_status
            db.execute(
                "UPDATE undo_batches SET status=?, result_json=?, finished_at=? WHERE id=?",
                (final_status, to_json({k: v for k, v in result.items()}),
                 iso(), undo_id))
            db.execute(
                "UPDATE import_jobs SET status='revoked', finished_at=? WHERE id=?",
                (iso(), import_job_id))

        # Transaction committed. Quarantine physical files now (non-transactional).
        result = _reconcile_files(undo_id, import_job_id, result,
                                  files_to_quarantine, requested_by)
        result["undo_batch_id"] = undo_id
        return result
    finally:
        locks.release(lock_key, token)


def _execute(undo_id: int, import_job_id: int, buckets: dict, sides: dict,
             actor: Optional[str]) -> dict:
    deleted_ids, retained_ids, reversed_issue_ids = [], [], []
    compensations = 0

    # --- A: plain deletable ------------------------------------------------
    for e in buckets["deletable"]:
        cur = db.get_conn().execute(
            "DELETE FROM calibrations WHERE id=? AND source!='manual' "
            "AND (import_job_id=? OR source=?)",
            (e["calibration_id"], import_job_id, f"import:{import_job_id}"))
        if cur.rowcount == 1:
            deleted_ids.append(e["calibration_id"])
        else:
            # Vanished under us -> compensate rather than silently succeed.
            compensations += 1
            comp_service.queue(
                kind="retry_artifact", import_job_id=import_job_id,
                undo_batch_id=undo_id, ref_type="calibration",
                ref_id=e["calibration_id"],
                summary=f"校准记录 {e['calibration_id']} 在撤销时已不存在，需人工核对")

    # --- C: state-driving, invert transitions then delete ------------------
    for e in buckets["state_driving"]:
        inv = e["inverse"]
        issue_id = inv["issue_id"]
        # Guarded restore: only if the issue is still in the expected state.
        cur = db.get_conn().execute(
            "UPDATE issues SET status=?, resolved_at=?, resolution=?, updated_at=? "
            "WHERE id=? AND status=?",
            (inv["to_status"], inv["restore_resolved_at"],
             inv["restore_resolution"], iso(), issue_id, inv["from_status"]))
        if cur.rowcount == 1:
            db.execute(
                """INSERT INTO issue_transitions
                   (issue_id, device_id, from_status, to_status, action,
                    cause_type, cause_id, import_job_id, actor,
                    reversal_of_id, created_at)
                   VALUES (?,?,?,?, 'restored', 'import_undo', ?, ?, ?, ?, ?)""",
                (issue_id, e["device_id"], inv["from_status"], inv["to_status"],
                 e["calibration_id"], import_job_id, actor,
                 inv["transition_id"], iso()))
            dcur = db.get_conn().execute(
                "DELETE FROM calibrations WHERE id=? AND source!='manual'",
                (e["calibration_id"],))
            if dcur.rowcount == 1:
                deleted_ids.append(e["calibration_id"])
                reversed_issue_ids.append(issue_id)
            else:
                compensations += 1
                comp_service.queue(
                    kind="retry_artifact", import_job_id=import_job_id,
                    undo_batch_id=undo_id, ref_type="calibration",
                    ref_id=e["calibration_id"],
                    summary=f"状态已回滚但校准 {e['calibration_id']} 已缺失，需核对")
        else:
            # State moved since fingerprint; retain + compensate.
            retained_ids.append(e["calibration_id"])
            compensations += 1
            comp_service.queue(
                kind="issue_state_moved", import_job_id=import_job_id,
                undo_batch_id=undo_id, ref_type="issue", ref_id=issue_id,
                severity="critical",
                summary=f"问题 {issue_id} 状态在撤销前发生变化，校准 "
                        f"{e['calibration_id']} 已保留待人工处理",
                detail={"expected_status": inv["from_status"],
                        "calibration_id": e["calibration_id"]})

    # --- B: state-dependent -> retain + manual compensation ---------------
    for e in buckets["state_dependent"]:
        retained_ids.append(e["calibration_id"])
        compensations += 1
        if e.get("reason") == "manual_twin":
            kind, summary = "manual_twin", (
                f"校准 {e['calibration_id']} 与手工记录 "
                f"{e['manual_twin_id']} 内容相同，已保留导入记录，需人工确认去留")
        else:
            kind, summary = "dependent_record", (
                f"校准 {e['calibration_id']} 已被后续状态依赖"
                f"（{e.get('later_action', '后续转换')}），记录已保留，需人工评估")
        comp_service.queue(
            kind=kind, import_job_id=import_job_id, undo_batch_id=undo_id,
            import_attempt_id=e.get("import_attempt_id"),
            ref_type="calibration", ref_id=e["calibration_id"],
            severity="critical" if kind == "dependent_record" else "warning",
            summary=summary)

    # --- attachments: revoke + quarantine (after commit) -------------------
    quarantine_files = []
    for a in sides["attachments"]:
        row = attachment_service.revoke(
            attachment_service.get(a["id"]))
        if row["path"]:
            quarantine_files.append(row["path"])

    # --- notifications: retract unread, compensate read --------------------
    for n in sides["notifications"]:
        full = notification_service.get(n["id"])
        if full["status"] in ("read", "acknowledged"):
            compensations += 1
            comp_service.queue(
                kind="notification_read", import_job_id=import_job_id,
                undo_batch_id=undo_id, ref_type="notification", ref_id=n["id"],
                summary=f"通知 {n['id']}（{n['title']}）已被阅读，无法撤回，需人工说明",
                detail={"issue_id": n["issue_id"]})
        else:
            notification_service.retract(full)

    db.execute("UPDATE import_attempts SET status='cleaned', finished_at=? "
               "WHERE import_job_id=?", (iso(), import_job_id))

    # Audit notification that the batch was reverted.
    notification_service.emit(
        type_="batch_reverted", title=f"导入批次 {import_job_id} 已撤销",
        message=f"撤销单 {undo_id}：删除 {len(deleted_ids)} 条，保留 "
                f"{len(retained_ids)} 条待处理，由 {actor or 'system'} 执行",
        import_job_id=import_job_id, created_by=actor, status="sent")

    return {
        "deleted_calibration_ids": deleted_ids,
        "retained_calibration_ids": retained_ids,
        "reversed_issue_ids": sorted(set(reversed_issue_ids)),
        "attachments_revoked": len(sides["attachments"]),
        "notifications_retracted": sum(
            1 for n in sides["notifications"]
            if notification_service.get(n["id"])["status"] == "retracted"),
        "compensation_created": compensations > 0,
        "compensation_count": compensations,
        "executed_by": actor,
        "executed_at": iso(),
        "_quarantine_files": quarantine_files,
    }


def _reconcile_files(undo_id: int, import_job_id: int, result: dict,
                     paths: list[str], actor: Optional[str]) -> dict:
    """Move revoked attachment files to quarantine after commit.

    A filesystem failure can't roll the DB back, so it surfaces as a
    compensation item rather than disappearing.
    """
    moved, failed = [], []
    for p in paths:
        try:
            moved.append(attachment_service.quarantine_path(p))
        except OSError as exc:
            failed.append({"path": p, "error": str(exc)})
    if failed:
        with db.transaction(immediate=True):
            for f in failed:
                cid = comp_service.queue(
                    kind="attachment_file", import_job_id=import_job_id,
                    undo_batch_id=undo_id, ref_type="file",
                    ref_id=None, severity="warning",
                    summary=f"附件无法移入隔离区：{f['path']}（{f['error']}）",
                    detail=f)
            if cid:
                result["compensation_created"] = True
                result["compensation_count"] = result.get("compensation_count", 0) + 1
                db.execute(
                    "UPDATE undo_batches SET status='partial', "
                    "result_json=? WHERE id=?", (to_json(result), undo_id))
    result["files_quarantined"] = moved
    result["files_failed"] = failed
    return result


def _result_dict(row, replayed: bool = False) -> dict:
    d = from_json(row["result_json"], {}) or {}
    d["undo_batch_id"] = row["id"]
    d["import_job_id"] = row["import_job_id"]
    d["status"] = row["status"]
    d["replayed"] = replayed
    return d


def status(import_job_id: int) -> Optional[dict]:
    """Latest undo order for a batch."""
    row = db.query_one(
        "SELECT * FROM undo_batches WHERE import_job_id=? ORDER BY id DESC LIMIT 1",
        (import_job_id,))
    if row is None:
        return None
    d = _result_dict(row)
    d["requested_by"] = row["requested_by"]
    d["reason"] = row["reason"]
    d["created_at"] = row["created_at"]
    d["confirmed_at"] = row["confirmed_at"]
    return d


def set_confirmed_at(undo_id: int) -> None:
    db.execute("UPDATE undo_batches SET confirmed_at=? WHERE id=?", (iso(), undo_id))
