"""Auditable undo & compensation for import batches.

Undoing an import is a two-phase, fully audited operation:

1. **Preview** (:func:`preview_undo`) classifies every record the import wrote
   and returns the diff of what would happen, without changing any data:

   * ``deletable``         – imported row, untouched since, no later state
     depends on it, no issue effects → deleted directly.
   * ``restorable``        – the row drove an issue transition and the issue is
     still exactly where the import left it → row deleted, issue restored.
   * ``depended``          – later calibrations outside this batch build on the
     state that followed this row → kept, compensation item raised.
   * ``issue_conflicted``  – the issue it affected has since changed → kept,
     compensation item raised.
   * ``missing``/``conflict`` – the row vanished or no longer belongs to the
     batch (e.g. re-created manually with identical content) → never touched,
     compensation item raised.

   Deletes always match on the primary key *and* the batch source, never on
   the content hash, so a manual record with identical content is never
   removed by mistake.

2. **Confirm** (:func:`confirm_undo`) requires the preview's ``confirm_token``
   plus an operator name (optional ``CALTRACK_UNDO_OPERATORS`` allowlist), then
   executes inside a single transaction under the same advisory lock imports
   use. Every guard is re-checked at execution time; anything that cannot be
   reverted automatically becomes a *pending compensation* instead of silently
   succeeding. Side artifacts are handled too: pending notifications are
   revoked (already-sent ones raise compensations) and the uploaded source
   file is removed.

Retry safety: import retries wipe previous attempts' rows via
:func:`clear_previous_attempt`, which marks the lineage rows instead of losing
them, so undo sees the full history of attempts and never double-counts.
"""
from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Optional

from .. import config, db
from ..tasks import locks
from ..utils import from_json, iso, to_json, utcnow

# Undo execution is mutually exclusive with import execution: same lock key.
UNDO_LOCK_KEY = config.IMPORT_LOCK_KEY
UNDO_LOCK_TTL = max(600.0, config.TASK_TIMEOUT_SECONDS * 2)

# Item classifications (preview diff + execution dispositions).
DELETABLE = "deletable"
RESTORABLE = "restorable"
DEPENDED = "depended"
ISSUE_CONFLICTED = "issue_conflicted"
MISSING = "missing"
CONFLICT = "conflict"

_LIVE_STATUSES = ("preview", "confirmed", "running")
_TERMINAL_STATUSES = ("done", "partial")

# Fields the undo must be able to restore on an issue; compared as a whole to
# detect any post-import change (manual resolve/reopen, new events, ...).
_ISSUE_SNAPSHOT_FIELDS = ("status", "resolved_at", "resolution",
                          "event_count", "updated_at")


class UndoError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Lineage written by the importer
# ---------------------------------------------------------------------------

def snapshot_issue(issue) -> dict:
    """Snapshot of the issue fields undo restores / compares."""
    return {f: getattr(issue, f) for f in _ISSUE_SNAPSHOT_FIELDS}


def record_item(import_job_id: int, attempt: int, calibration_id: int,
                import_row: Optional[int], content_hash: str) -> None:
    db.execute(
        """INSERT INTO import_items
           (import_job_id, attempt, calibration_id, import_row, content_hash, created_at)
           VALUES (?,?,?,?,?,?)""",
        (import_job_id, attempt, calibration_id, import_row, content_hash, iso()))


def record_effect(import_job_id: int, calibration_id: int, issue_id: int,
                  before: Optional[dict], after: Optional[dict],
                  notification_id: Optional[int]) -> None:
    db.execute(
        """INSERT INTO import_effects
           (import_job_id, calibration_id, kind, issue_id, before_json, after_json,
            notification_id, created_at)
           VALUES (?,?, 'issue_transition', ?,?,?,?,?)""",
        (import_job_id, calibration_id, issue_id, to_json(before), to_json(after),
         notification_id, iso()))


def clear_previous_attempt(import_job_id: int, attempt: int) -> None:
    """Retry safety: wipe rows written by earlier attempts and mark their
    lineage ``cleared_by_retry`` (history kept), atomically."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM calibrations WHERE source=?",
                     (f"import:{import_job_id}",))
        conn.execute(
            """UPDATE import_items SET status='cleared_by_retry', cleared_at=?, clear_reason=?
               WHERE import_job_id=? AND status='active'""",
            (iso(), f"superseded by attempt {attempt}", import_job_id))


# ---------------------------------------------------------------------------
# Plan building (shared by preview and execution)
# ---------------------------------------------------------------------------

def _snapshot_from_row(row) -> dict:
    return {f: row[f] for f in _ISSUE_SNAPSHOT_FIELDS}


def _plan_issue_restore(conn, issue_id: int, effs: list) -> dict:
    """Decide whether one issue can be auto-restored.

    ``effs`` are the import's effects on this issue, oldest first. Auto-restore
    is only safe when the effects form an unbroken chain (each effect starts
    where the previous one ended) and the issue is *still* exactly where the
    import's last effect left it.
    """
    plan = {
        "issue_id": issue_id,
        "effect_ids": [e["id"] for e in effs],
        "calibration_ids": [e["calibration_id"] for e in effs],
        "restore_to": from_json(effs[0]["before_json"], None),
        "auto": False,
        "reason": "",
    }
    if plan["restore_to"] is None:
        plan["reason"] = "缺少导入前的问题快照，无法安全恢复"
        return plan
    row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if row is None:
        plan["reason"] = "问题已不存在"
        return plan
    current = _snapshot_from_row(row)
    plan["current"] = current
    chain_ok = all(
        from_json(a["after_json"], {}) == from_json(b["before_json"], {})
        for a, b in zip(effs, effs[1:]))
    last_after = from_json(effs[-1]["after_json"], {})
    if not chain_ok:
        plan["reason"] = "导入对该问题的影响链不完整（重试或交叉修改），不敢自动恢复"
    elif current != last_after:
        plan["reason"] = (f"问题在导入后已被后续操作改变"
                          f"（当前 {current['status']}，导入结束时 {last_after['status']}）")
    else:
        plan["auto"] = True
        plan["reason"] = "问题状态自导入后未被改变，可自动恢复"
    return plan


def _artifact_paths(import_job_id: int) -> list[str]:
    """Uploaded source file(s) kept as the batch's attachment."""
    uploads = config.DATA_DIR / "uploads"
    if not uploads.is_dir():
        return []
    return sorted(str(p) for p in uploads.glob(f"import_{import_job_id}.*")
                  if p.is_file())


def _build_plan(conn, import_job_id: int) -> dict:
    """Classify everything the import touched. Read-only; used by preview and
    re-derived at execution time (state may have changed since preview)."""
    source = f"import:{import_job_id}"
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM import_items WHERE import_job_id=? AND status='active' ORDER BY id",
        (import_job_id,)).fetchall()]
    cleared = conn.execute(
        "SELECT COUNT(*) c FROM import_items WHERE import_job_id=? AND status='cleared_by_retry'",
        (import_job_id,)).fetchone()["c"]

    # Robustness net: rows tagged with this import's source but missing from
    # the lineage (import crashed between insert and record_item).
    known = {it["calibration_id"] for it in items}
    for row in conn.execute(
            "SELECT * FROM calibrations WHERE source=?", (source,)).fetchall():
        if row["id"] not in known:
            items.append({"id": None, "calibration_id": row["id"],
                          "import_row": row["import_row"], "attempt": None,
                          "content_hash": row["content_hash"]})

    effects = [dict(r) for r in conn.execute(
        "SELECT * FROM import_effects WHERE import_job_id=? AND status='active' ORDER BY id",
        (import_job_id,)).fetchall()]
    effs_by_issue: dict[int, list] = {}
    for e in effects:
        effs_by_issue.setdefault(e["issue_id"], []).append(e)
    issue_plans = {iid: _plan_issue_restore(conn, iid, effs)
                   for iid, effs in effs_by_issue.items()}
    eff_by_cal: dict[int, list] = {}
    for e in effects:
        eff_by_cal.setdefault(e["calibration_id"], []).append(e)

    plan_items = []
    for item in items:
        cal_id = item["calibration_id"]
        row = conn.execute(
            """SELECT c.*, d.code AS device_code FROM calibrations c
               JOIN devices d ON d.id = c.device_id WHERE c.id=?""",
            (cal_id,)).fetchone()
        entry = {
            "item_id": item["id"], "calibration_id": cal_id,
            "import_row": item["import_row"], "attempt": item["attempt"],
            "content_hash": item["content_hash"],
            "issue_ids": sorted({e["issue_id"] for e in eff_by_cal.get(cal_id, [])}),
        }
        if row is None:
            entry.update(cls=MISSING,
                         reason="记录已不存在（人工删除或随设备删除）")
        else:
            entry.update(device_id=row["device_id"], device_code=row["device_code"],
                         calibrated_at=row["calibrated_at"], result=row["result"])
            if row["source"] != source:
                entry.update(
                    cls=CONFLICT,
                    reason=f"记录来源已变为 {row['source']!r}；为防止误删同内容手工记录，跳过")
            else:
                later = conn.execute(
                    """SELECT COUNT(*) c FROM calibrations
                       WHERE device_id=? AND source<>?
                         AND (calibrated_at>? OR (calibrated_at=? AND id>?))""",
                    (row["device_id"], source, row["calibrated_at"],
                     row["calibrated_at"], cal_id)).fetchone()["c"]
                if later:
                    entry.update(
                        cls=DEPENDED,
                        reason=f"该设备之后还有 {later} 条非本批次校准，删除会破坏后续状态")
                elif entry["issue_ids"]:
                    if all(issue_plans[iid]["auto"] for iid in entry["issue_ids"]):
                        entry.update(cls=RESTORABLE,
                                     reason="删除记录并恢复受影响的问题状态")
                    else:
                        entry.update(cls=ISSUE_CONFLICTED,
                                     reason="影响过的问题已被后续操作改变，无法自动恢复")
                else:
                    entry.update(cls=DELETABLE, reason="无后续依赖，可直接删除")
        plan_items.append(entry)

    notif_plan = [{
        "id": n["id"], "status": n["status"], "message": n["message"],
        "action": "revoke" if n["status"] == "pending" else "compensate",
    } for n in conn.execute(
        "SELECT * FROM notifications WHERE import_job_id=? AND status<>'revoked' ORDER BY id",
        (import_job_id,)).fetchall()]

    def n(cls):
        return sum(1 for i in plan_items if i["cls"] == cls)

    counts = {
        "deletable": n(DELETABLE),
        "restorable": n(RESTORABLE),
        "depended": n(DEPENDED),
        "issue_conflicted": n(ISSUE_CONFLICTED),
        "missing": n(MISSING),
        "conflict": n(CONFLICT),
        "issues_to_restore": sum(1 for p in issue_plans.values() if p["auto"]),
        "issues_compensated": sum(1 for p in issue_plans.values() if not p["auto"]),
        "notifications_to_revoke": sum(1 for x in notif_plan if x["action"] == "revoke"),
        "notifications_compensated": sum(1 for x in notif_plan if x["action"] == "compensate"),
        "cleared_by_retry": cleared,
        "artifacts": len(_artifact_paths(import_job_id)),
    }
    counts["compensations_expected"] = (
        counts["depended"] + counts["missing"] + counts["conflict"]
        + counts["issues_compensated"] + counts["notifications_compensated"])

    return {
        "import_job_id": import_job_id,
        "generated_at": iso(),
        "items": plan_items,
        "issues": list(issue_plans.values()),
        "notifications": notif_plan,
        "artifacts": [{"path": p} for p in _artifact_paths(import_job_id)],
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Phase 1: preview
# ---------------------------------------------------------------------------

def preview_undo(import_job_id: int) -> dict:
    """Create a preview (diff) of what an undo would do. Changes nothing.

    Supersedes any earlier un-executed preview for the same import; concurrent
    previews are folded onto the single live batch (partial unique index).
    """
    job = db.query_one("SELECT id FROM import_jobs WHERE id=?", (import_job_id,))
    if job is None:
        raise UndoError(f"import job {import_job_id} not found", 404)
    _recover_stale()
    token = secrets.token_hex(8)
    try:
        with db.transaction() as conn:
            running = conn.execute(
                "SELECT id FROM undo_batches WHERE import_job_id=? AND status='running'",
                (import_job_id,)).fetchone()
            if running:
                raise UndoError("an undo is currently running for this import", 409)
            conn.execute(
                """UPDATE undo_batches SET status='superseded'
                   WHERE import_job_id=? AND status IN ('preview','confirmed')""",
                (import_job_id,))
            plan = _build_plan(conn, import_job_id)
            cur = conn.execute(
                """INSERT INTO undo_batches (import_job_id, status, confirm_token, preview_json, created_at)
                   VALUES (?, 'preview', ?, ?, ?)""",
                (import_job_id, token, to_json(plan), iso()))
            undo_id = cur.lastrowid
    except UndoError:
        raise
    except Exception:
        # Unique-index race: a concurrent preview won — return the live batch.
        existing = _live_batch(import_job_id)
        if existing is not None:
            return _batch_dict(existing)
        raise
    return _batch_dict(db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_id,)))


# ---------------------------------------------------------------------------
# Phase 2: confirm + execute
# ---------------------------------------------------------------------------

def confirm_undo(import_job_id: int, undo_id: int, confirm_token: str,
                 operator: Optional[str]) -> dict:
    """Execute a previewed undo. Idempotent: confirming a terminal batch
    replays its stored report; concurrent confirms execute exactly once."""
    op = _check_operator(operator)
    batch = db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_id,))
    if batch is None:
        raise UndoError(f"undo batch {undo_id} not found", 404)
    if batch["import_job_id"] != import_job_id:
        raise UndoError(f"undo batch {undo_id} does not belong to import {import_job_id}", 400)
    if batch["confirm_token"] != (confirm_token or ""):
        raise UndoError("confirm_token does not match the preview", 403)
    if batch["status"] in _TERMINAL_STATUSES:
        return _batch_dict(batch)  # idempotent replay, no re-execution
    if batch["status"] == "superseded":
        raise UndoError("this preview was superseded by a newer one; preview again", 409)
    if batch["status"] == "running":
        raise UndoError("undo is already running", 409)

    lock = locks.acquire(UNDO_LOCK_KEY, ttl_seconds=UNDO_LOCK_TTL)
    if lock is None:
        raise UndoError("an import or another undo is currently running; try again later", 409)
    try:
        # Atomic claim: exactly one executor moves preview/confirmed/failed -> running.
        cur = db.get_conn().execute(
            """UPDATE undo_batches SET status='running', operator=?, confirmed_at=?, last_error=NULL
               WHERE id=? AND status IN ('preview','confirmed','failed')""",
            (op, iso(), undo_id))
        db.get_conn().commit()
        if cur.rowcount != 1:
            latest = db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_id,))
            if latest["status"] in _TERMINAL_STATUSES:
                return _batch_dict(latest)
            raise UndoError("undo is already running elsewhere", 409)
        try:
            with db.transaction() as conn:
                # Re-derive the plan now: preview was informational, the world
                # may have changed. Guards below re-check every assumption.
                plan = _build_plan(conn, import_job_id)
                report = _apply_plan(conn, undo_id, import_job_id, plan, op)
                report["status"] = "done" if not report["compensations"] else "partial"
                conn.execute(
                    "UPDATE undo_batches SET status=?, report_json=?, finished_at=? WHERE id=?",
                    (report["status"], to_json(report), iso(), undo_id))
                conn.execute(
                    "UPDATE import_jobs SET status=? WHERE id=?",
                    ("undone" if report["status"] == "done" else "undo_partial",
                     import_job_id))
        except Exception as e:
            db.execute(
                "UPDATE undo_batches SET status='failed', last_error=? WHERE id=?",
                (f"{type(e).__name__}: {e}", undo_id))
            raise
    finally:
        locks.release(UNDO_LOCK_KEY, lock)
    return _batch_dict(db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_id,)))


def _apply_plan(conn, undo_id: int, import_job_id: int, plan: dict,
                operator: str) -> dict:
    """Execute the plan inside the caller's transaction. Every mutation is
    guarded; a failed guard becomes a compensation, never a silent skip."""
    source = f"import:{import_job_id}"
    now = iso()
    deleted: list[int] = []
    restored: list[dict] = []
    revoked: list[int] = []
    compensations: list[dict] = []

    def compensate(kind: str, ref_id: Optional[int], detail: str) -> None:
        cur = conn.execute(
            """INSERT INTO compensations (undo_batch_id, import_job_id, kind, ref_id, detail, created_at)
               VALUES (?,?,?,?,?,?)""",
            (undo_id, import_job_id, kind, ref_id, detail, now))
        compensations.append({"id": cur.lastrowid, "kind": kind,
                              "ref_id": ref_id, "detail": detail})

    # 1) Restore issues whose state is verifiably untouched since the import.
    restored_issues: set[int] = set()
    for ip in plan["issues"]:
        if not ip["auto"]:
            continue
        snap, cur_state = ip["restore_to"], ip["current"]
        cur = conn.execute(
            """UPDATE issues SET status=?, resolved_at=?, resolution=?, event_count=?, updated_at=?
               WHERE id=? AND status=? AND updated_at=?""",
            (snap["status"], snap["resolved_at"], snap["resolution"],
             snap["event_count"], iso(),
             ip["issue_id"], cur_state["status"], cur_state["updated_at"]))
        if cur.rowcount == 1:
            restored_issues.add(ip["issue_id"])
            restored.append({"issue_id": ip["issue_id"],
                             "from": cur_state["status"], "to": snap["status"]})
            conn.execute(
                "UPDATE import_effects SET status='reverted' WHERE import_job_id=? AND issue_id=?",
                (import_job_id, ip["issue_id"]))
        else:  # lost a race against a concurrent manual change
            ip["auto"] = False
            ip["reason"] = "执行瞬间问题状态被并发修改"

    # 2) Issues that could not be restored -> one compensation each; their
    #    calibration rows are kept (listed in the detail for the human).
    for ip in plan["issues"]:
        if ip["issue_id"] in restored_issues:
            continue
        conn.execute(
            """UPDATE import_effects SET status='compensated'
               WHERE import_job_id=? AND issue_id=? AND status='active'""",
            (import_job_id, ip["issue_id"]))
        compensate("issue_state", ip["issue_id"],
                   f"问题 #{ip['issue_id']} 无法自动恢复（{ip['reason']}）；"
                   f"相关校准记录 {ip['calibration_ids']} 已保留，请人工核对问题状态与记录去留")

    # 3) Delete rows that are safe to delete; compensate the rest.
    for entry in plan["items"]:
        cls, cal_id = entry["cls"], entry["calibration_id"]
        if cls == RESTORABLE and not all(i in restored_issues for i in entry["issue_ids"]):
            cls = ISSUE_CONFLICTED  # its issue failed to restore at execution time
        if cls in (DELETABLE, RESTORABLE):
            cur = conn.execute(
                "DELETE FROM calibrations WHERE id=? AND source=?", (cal_id, source))
            if cur.rowcount == 1:
                if entry["item_id"] is not None:
                    conn.execute(
                        """UPDATE import_items SET status='undone', cleared_at=?, clear_reason=?
                           WHERE id=?""",
                        (now, f"undo batch {undo_id}", entry["item_id"]))
                deleted.append(cal_id)
            else:  # concurrent modification between plan and delete
                compensate("source_conflict", cal_id,
                           f"校准记录 #{cal_id} 在执行时被并发修改，已跳过以防误删")
        elif cls == MISSING:
            compensate("missing_row", cal_id,
                       f"校准记录 #{cal_id}（导入第 {entry['import_row']} 行）在撤销前已不存在；未执行删除")
        elif cls == CONFLICT:
            compensate("source_conflict", cal_id,
                       f"校准记录 #{cal_id} 已保留：{entry['reason']}")
        elif cls == DEPENDED:
            compensate("depended", cal_id,
                       f"校准记录 #{cal_id} 已保留：{entry['reason']}")
        # ISSUE_CONFLICTED rows are covered by the issue_state compensation.

    # 4) Notifications: revoke pending; already-sent ones need human follow-up.
    for n in plan["notifications"]:
        if n["action"] == "revoke":
            cur = conn.execute(
                "UPDATE notifications SET status='revoked', revoked_at=? WHERE id=? AND status='pending'",
                (now, n["id"]))
            if cur.rowcount == 1:
                revoked.append(n["id"])
            else:  # it was sent between plan and execution
                compensate("notification_sent", n["id"],
                           f"通知 #{n['id']} 已发送，无法撤回；请人工发送更正通知")
        else:
            compensate("notification_sent", n["id"],
                       f"通知 #{n['id']} 已发送，无法撤回；请人工发送更正通知")

    # 5) Artifacts: remove the uploaded source file(s). Best effort, audited;
    #    a filesystem failure becomes a compensation, not a crashed undo.
    artifacts = []
    for path in _artifact_paths(import_job_id):
        try:
            os.remove(path)
            artifacts.append({"path": path, "result": "deleted"})
        except FileNotFoundError:
            artifacts.append({"path": path, "result": "already_absent"})
        except OSError as e:
            artifacts.append({"path": path, "result": f"failed: {e}"})
            compensate("artifact", None,
                       f"附件 {path} 删除失败：{e}；请人工清理")

    return {
        "undo_id": undo_id,
        "import_job_id": import_job_id,
        "operator": operator,
        "finished_at": iso(),
        "deleted_calibration_ids": deleted,
        "restored_issues": restored,
        "revoked_notification_ids": revoked,
        "artifacts": artifacts,
        "compensations": compensations,
        "counts": {
            "deleted": len(deleted),
            "restored_issues": len(restored),
            "revoked_notifications": len(revoked),
            "compensations": len(compensations),
        },
    }


# ---------------------------------------------------------------------------
# Post-recovery queries + compensation workflow
# ---------------------------------------------------------------------------

def get_undo(undo_id: int) -> dict:
    row = db.query_one("SELECT * FROM undo_batches WHERE id=?", (undo_id,))
    if row is None:
        raise UndoError(f"undo batch {undo_id} not found", 404)
    return _batch_dict(row)


def latest_undo_for_import(import_job_id: int) -> Optional[dict]:
    row = db.query_one(
        "SELECT * FROM undo_batches WHERE import_job_id=? ORDER BY id DESC LIMIT 1",
        (import_job_id,))
    return _batch_dict(row) if row else None


def latest_undo_summary(import_job_id: int) -> Optional[dict]:
    """Compact undo state embedded in the import detail response."""
    row = db.query_one(
        "SELECT * FROM undo_batches WHERE import_job_id=? ORDER BY id DESC LIMIT 1",
        (import_job_id,))
    if row is None:
        return None
    report = from_json(row["report_json"], None)
    return {"id": row["id"], "status": row["status"], "operator": row["operator"],
            "created_at": row["created_at"], "finished_at": row["finished_at"],
            "counts": (report or {}).get("counts")}


def list_compensations(status: Optional[str] = None,
                       import_job_id: Optional[int] = None,
                       limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM compensations WHERE 1=1"
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


def count_compensations(status: Optional[str] = None,
                        import_job_id: Optional[int] = None) -> int:
    return len(list_compensations(status=status, import_job_id=import_job_id,
                                  limit=100000))


def resolve_compensation(comp_id: int, operator: Optional[str],
                         resolution: Optional[str] = None) -> dict:
    """Mark a compensation handled. Idempotent; requires a named operator."""
    op = _check_operator(operator)
    row = db.query_one("SELECT * FROM compensations WHERE id=?", (comp_id,))
    if row is None:
        raise UndoError(f"compensation {comp_id} not found", 404)
    if row["status"] == "resolved":
        return dict(row)  # idempotent
    db.execute(
        """UPDATE compensations SET status='resolved', resolved_at=?, resolved_by=?, resolution=?
           WHERE id=? AND status='pending'""",
        (iso(), op, resolution or "人工处理完成", comp_id))
    return dict(db.query_one("SELECT * FROM compensations WHERE id=?", (comp_id,)))


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _check_operator(operator: Optional[str]) -> str:
    """Permission confirmation: a named operator is required; if
    ``CALTRACK_UNDO_OPERATORS`` (comma-separated) is set, the operator must be
    on the allowlist."""
    op = (operator or "").strip()
    if not op:
        raise UndoError("operator is required (who is performing this undo?)", 403)
    allow = [s.strip()
             for s in os.environ.get("CALTRACK_UNDO_OPERATORS", "").split(",")
             if s.strip()]
    if allow and op not in allow:
        raise UndoError(f"operator {op!r} is not allowed to confirm undo operations", 403)
    return op


def _recover_stale() -> None:
    """Fail 'running' batches whose executor died (mirrors job recovery)."""
    cutoff = iso(utcnow() - timedelta(seconds=config.UNDO_STALE_SECONDS))
    db.execute(
        """UPDATE undo_batches SET status='failed',
           last_error='undo execution went stale (process crash?)'
           WHERE status='running' AND confirmed_at < ?""",
        (cutoff,))


def _live_batch(import_job_id: int):
    return db.query_one(
        """SELECT * FROM undo_batches WHERE import_job_id=?
           AND status IN ('preview','confirmed','running')
           ORDER BY id DESC LIMIT 1""",
        (import_job_id,))


def _batch_dict(row) -> dict:
    d = dict(row)
    d["preview"] = from_json(d.pop("preview_json", None), None)
    d["report"] = from_json(d.pop("report_json", None), None)
    return d
