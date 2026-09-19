"""Tests for the auditable import undo & compensation flow.

Covers: record classification (deletable / depended / issue-affected / missing /
conflict), protection of identical manual records, retry attempt history,
issue-state restore chains, notifications, upload artifacts, transactions +
idempotency + concurrency locks, permission confirmation, preview diff, and
post-recovery queries.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import threading
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CALTRACK_DB", os.path.join(_tmp, "test_caltrack.db"))
os.environ.setdefault("CALTRACK_TASK_TIMEOUT", "2")
os.environ.setdefault("CALTRACK_TASK_MAX_RETRIES", "2")

from app import config, db  # noqa: E402
from app.services import (  # noqa: E402
    calibrations as cal, devices, events, importer, issues,
    notifications as notify, undo)
from app.tasks import locks  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        db.init_db()
        for t in ("compensations", "undo_batches", "import_items", "import_effects",
                  "notifications", "issue_events", "issues", "calibrations", "events",
                  "import_jobs", "jobs", "task_locks", "devices"):
            db.execute(f"DELETE FROM {t}")
        os.environ.pop("CALTRACK_UNDO_OPERATORS", None)

    def mkdev(self, code="D-1"):
        return devices.create({"code": code, "name": "Dev " + code})

    def open_issue(self, d):
        """Three abnormal events -> one open issue for the device."""
        for i in range(3):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-08T0{i}:00:00",
                           "severity": "warning", "message": "w"})
        issue = issues.open_issue_for_device(d.id)
        assert issue is not None
        return issue

    def run_import(self, content, fmt="csv", auto_create=False, attempt=1):
        """Run an import synchronously, the way import_task does it."""
        rows = importer.parse_content(content, fmt)
        jid = importer.create_import_job("t.csv", len(rows))
        if attempt > 1:
            undo.clear_previous_attempt(jid, attempt)
        result = importer.process_rows(rows, jid, auto_create_devices=auto_create,
                                       attempt=attempt)
        return jid, result

    def undo_now(self, jid, operator="alice"):
        """Preview + confirm in one go; returns the terminal undo batch."""
        prev = undo.preview_undo(jid)
        return undo.confirm_undo(jid, prev["id"], prev["confirm_token"], operator)


class TestClassification(BaseCase):
    def test_clean_import_is_fully_deletable(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result,technician,measured_value\n"
            "D-1,2026-09-08 09:00,pass,A,1.0\n"
            "D-1,2026-09-08 10:00,pass,A,2.0\n"
            "D-1,2026-09-08 11:00,fail,A,3.0\n")

        prev = undo.preview_undo(jid)
        self.assertEqual(prev["status"], "preview")
        counts = prev["preview"]["counts"]
        self.assertEqual(counts["deletable"], 3)
        self.assertEqual(counts["compensations_expected"], 0)
        self.assertEqual(len(prev["preview"]["items"]), 3)

        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["operator"], "alice")
        self.assertEqual(batch["report"]["counts"]["deleted"], 3)
        self.assertEqual(batch["report"]["compensations"], [])
        # Rows really gone; lineage marked; import flagged as undone.
        self.assertEqual(cal.list_calibrations(), [])
        items = db.query("SELECT status FROM import_items WHERE import_job_id=?", (jid,))
        self.assertEqual({r["status"] for r in items}, {"undone"})
        job = importer.get_import_job(jid)
        self.assertEqual(job["status"], "undone")

    def test_manual_same_content_record_is_never_deleted(self):
        """The imported row is replaced by an identical manual record: undo must
        not match by content — the manual row survives and a compensation is raised."""
        d = self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result,technician,measured_value\n"
            "D-1,2026-09-08 09:00,pass,A,1.0\n")
        imported = cal.list_calibrations()[0]
        # Operator deletes the imported row and re-enters identical data manually.
        db.execute("DELETE FROM calibrations WHERE id=?", (imported["id"],))
        manual = cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T09:00:00",
                             "result": "pass", "technician": "A", "measured_value": 1.0})
        self.assertEqual(manual.content_hash, imported["content_hash"])

        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(batch["report"]["counts"]["deleted"], 0)
        comp = batch["report"]["compensations"][0]
        self.assertEqual(comp["kind"], "missing_row")
        # The identical manual record is untouched.
        remaining = cal.list_calibrations()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], manual.id)
        self.assertEqual(remaining[0]["source"], "manual")

    def test_depended_record_is_kept_with_compensation(self):
        """A later manual calibration builds on the state after the imported row:
        the row is not deleted; a compensation item is raised instead."""
        d = self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 09:00,pass\n")
        imported = cal.list_calibrations()[0]
        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T12:00:00",
                    "result": "pass"})  # later, non-batch state

        prev = undo.preview_undo(jid)
        self.assertEqual(prev["preview"]["counts"]["depended"], 1)
        self.assertEqual(prev["preview"]["counts"]["compensations_expected"], 1)

        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "partial")
        comp = batch["report"]["compensations"][0]
        self.assertEqual(comp["kind"], "depended")
        self.assertEqual(comp["ref_id"], imported["id"])
        # Both rows still present — nothing was silently deleted.
        self.assertEqual(len(cal.list_calibrations()), 2)

    def test_missing_and_source_conflict_are_compensated_not_deleted(self):
        d = self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 09:00,pass\n"
            "D-1,2026-09-08 10:00,pass\n")
        rows = cal.list_calibrations()
        # Row 1 deleted outside the undo flow; row 2's source relabelled.
        db.execute("DELETE FROM calibrations WHERE id=?", (rows[0]["id"],))
        db.execute("UPDATE calibrations SET source='manual' WHERE id=?", (rows[1]["id"],))

        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")
        kinds = sorted(c["kind"] for c in batch["report"]["compensations"])
        self.assertEqual(kinds, ["missing_row", "source_conflict"])
        self.assertEqual(len(cal.list_calibrations()), 1)  # relabelled row kept


class TestIssueEffects(BaseCase):
    def test_issue_restored_and_notification_revoked(self):
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,pass\n")
        self.assertEqual(issues.get(issue.id).status, "resolved")
        notif = notify.list_notifications(import_job_id=jid)[0]
        self.assertEqual(notif["status"], "pending")

        prev = undo.preview_undo(jid)
        counts = prev["preview"]["counts"]
        self.assertEqual(counts["restorable"], 1)
        self.assertEqual(counts["issues_to_restore"], 1)
        self.assertEqual(counts["notifications_to_revoke"], 1)

        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")
        after = issues.get(issue.id)
        self.assertEqual(after.status, "open")          # state restored
        self.assertIsNone(after.resolved_at)
        self.assertEqual(after.event_count, 3)
        self.assertEqual(notify.get(notif["id"])["status"], "revoked")
        self.assertEqual(cal.list_calibrations(), [])
        self.assertEqual(batch["report"]["restored_issues"],
                         [{"issue_id": issue.id, "from": "resolved", "to": "open"}])

    def test_issue_chain_restored_to_earliest_state(self):
        """Import drives open -> monitoring -> resolved with two rows; undo must
        walk the chain back to open, not just one step."""
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,conditional\n"
            "D-1,2026-09-08 11:00,pass\n")
        self.assertEqual(issues.get(issue.id).status, "resolved")

        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "done")
        self.assertEqual(issues.get(issue.id).status, "open")
        self.assertEqual(batch["report"]["counts"]["deleted"], 2)
        self.assertEqual(len(notify.list_notifications(status="revoked")), 2)

    def test_touched_issue_becomes_compensation_and_row_kept(self):
        """Issue changed after the import (manual re-resolve): no auto-restore,
        the imported row is kept, and a pending compensation is raised."""
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,pass\n")
        issues.resolve(issue.id, "人工确认已修复")  # touches updated_at/resolution

        prev = undo.preview_undo(jid)
        self.assertEqual(prev["preview"]["counts"]["issue_conflicted"], 1)
        self.assertEqual(prev["preview"]["counts"]["issues_compensated"], 1)

        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "partial")
        comp = batch["report"]["compensations"][0]
        self.assertEqual(comp["kind"], "issue_state")
        self.assertEqual(comp["ref_id"], issue.id)
        after = issues.get(issue.id)
        self.assertEqual(after.status, "resolved")           # left as the human set it
        self.assertEqual(after.resolution, "人工确认已修复")
        self.assertEqual(len(cal.list_calibrations()), 1)    # imported row kept

    def test_new_events_on_issue_break_auto_restore(self):
        """Events attached after the import change event_count -> depended state."""
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,conditional\n")
        events.create({"device_id": d.id, "occurred_at": "2026-09-08T12:00:00",
                       "severity": "critical", "message": "又坏了"})

        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")
        self.assertEqual(batch["report"]["compensations"][0]["kind"], "issue_state")
        self.assertEqual(issues.get(issue.id).status, "monitoring")  # untouched
        self.assertEqual(issues.get(issue.id).event_count, 4)

    def test_sent_notification_cannot_be_revoked(self):
        """A notification already delivered can't be unsent: issue is still
        restored and the row deleted, but a compensation flags the retraction."""
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,pass\n")
        notif = notify.list_notifications(import_job_id=jid)[0]
        self.assertTrue(notify.mark_sent(notif["id"]))

        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")
        comp = batch["report"]["compensations"][0]
        self.assertEqual(comp["kind"], "notification_sent")
        self.assertEqual(comp["ref_id"], notif["id"])
        self.assertEqual(notify.get(notif["id"])["status"], "sent")  # not revoked
        self.assertEqual(issues.get(issue.id).status, "open")        # still restored
        self.assertEqual(cal.list_calibrations(), [])


class TestRetryHistory(BaseCase):
    def test_retry_attempts_are_not_double_undone(self):
        """Attempt 1 wrote rows + effects, attempt 2 wiped and rewrote them.
        Undo deletes only attempt-2 rows, marks attempt-1 lineage as already
        cleared, and still restores the issue across the whole attempt chain."""
        d = self.mkdev("D-1")
        issue = self.open_issue(d)

        rows = importer.parse_content(
            "device_code,calibrated_at,result\nD-1,2026-09-08 10:00,conditional\n", "csv")
        jid = importer.create_import_job("t.csv", len(rows))
        importer.process_rows(rows, jid, attempt=1)          # issue -> monitoring
        cal_a1 = cal.list_calibrations()[0]

        undo.clear_previous_attempt(jid, 2)                  # retry wipes attempt 1
        self.assertEqual(cal.list_calibrations(), [])
        item = db.query_one("SELECT * FROM import_items WHERE calibration_id=?",
                            (cal_a1["id"],))
        self.assertEqual(item["status"], "cleared_by_retry")

        rows2 = importer.parse_content(
            "device_code,calibrated_at,result\nD-1,2026-09-08 10:00,pass\n", "csv")
        importer.process_rows(rows2, jid, attempt=2)         # issue -> resolved
        self.assertEqual(issues.get(issue.id).status, "resolved")

        prev = undo.preview_undo(jid)
        counts = prev["preview"]["counts"]
        self.assertEqual(counts["cleared_by_retry"], 1)      # history visible
        self.assertEqual(counts["restorable"], 1)

        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["report"]["counts"]["deleted"], 1)  # only attempt-2 row
        self.assertEqual(issues.get(issue.id).status, "open")      # full chain restored
        # Attempt-1 lineage stays 'cleared_by_retry' — not re-processed.
        item = db.query_one("SELECT * FROM import_items WHERE calibration_id=?",
                            (cal_a1["id"],))
        self.assertEqual(item["status"], "cleared_by_retry")


class TestArtifacts(BaseCase):
    def test_upload_artifact_removed_and_audited(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        uploads = config.DATA_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        artifact = uploads / f"import_{jid}.csv"
        artifact.write_text("device_code,calibrated_at,result\n", encoding="utf-8")
        try:
            prev = undo.preview_undo(jid)
            self.assertEqual(prev["preview"]["counts"]["artifacts"], 1)
            batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
            self.assertEqual(batch["status"], "done")
            self.assertFalse(artifact.exists())
            self.assertEqual(batch["report"]["artifacts"][0]["result"], "deleted")
        finally:
            if artifact.exists():
                artifact.unlink()

    def test_missing_artifact_is_not_an_error(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        batch = self.undo_now(jid)  # no upload file on disk
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["report"]["artifacts"], [])


class TestIdempotencyAndConcurrency(BaseCase):
    def test_confirm_is_idempotent(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        prev = undo.preview_undo(jid)
        first = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        second = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "done")
        self.assertEqual(first["report"], second["report"])  # replayed, not re-run
        self.assertEqual(cal.list_calibrations(), [])
        # No duplicate side effects from the second confirm.
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM compensations")["c"], 0)

    def test_confirm_requires_matching_token_and_operator(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        prev = undo.preview_undo(jid)
        with self.assertRaises(undo.UndoError) as ctx:
            undo.confirm_undo(jid, prev["id"], "wrong-token", "alice")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(undo.UndoError) as ctx:
            undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "")
        self.assertEqual(ctx.exception.status, 403)
        # Nothing executed: row still there, batch still in preview.
        self.assertEqual(len(cal.list_calibrations()), 1)
        self.assertEqual(undo.get_undo(prev["id"])["status"], "preview")

    def test_operator_allowlist(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        os.environ["CALTRACK_UNDO_OPERATORS"] = "alice,bob"
        prev = undo.preview_undo(jid)
        with self.assertRaises(undo.UndoError) as ctx:
            undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "carol")
        self.assertEqual(ctx.exception.status, 403)
        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "bob")
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["operator"], "bob")

    def test_preview_supersedes_unexecuted_preview(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        p1 = undo.preview_undo(jid)
        p2 = undo.preview_undo(jid)
        self.assertNotEqual(p1["id"], p2["id"])
        self.assertEqual(undo.get_undo(p1["id"])["status"], "superseded")
        with self.assertRaises(undo.UndoError) as ctx:
            undo.confirm_undo(jid, p1["id"], p1["confirm_token"], "alice")
        self.assertEqual(ctx.exception.status, 409)
        batch = undo.confirm_undo(jid, p2["id"], p2["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")

    def test_confirm_blocked_while_import_lock_held(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        prev = undo.preview_undo(jid)
        token = locks.acquire(config.IMPORT_LOCK_KEY, ttl_seconds=60)
        self.assertIsNotNone(token)
        try:
            with self.assertRaises(undo.UndoError) as ctx:
                undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            locks.release(config.IMPORT_LOCK_KEY, token)
        # After the lock is released the same confirm goes through.
        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")

    def test_concurrent_confirms_execute_exactly_once(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result,measured_value\n"
            + "".join(f"D-1,2026-09-08 09:{i:02d},pass,{i}.0\n" for i in range(10)))
        prev = undo.preview_undo(jid)
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def worker():
            try:
                barrier.wait(timeout=5)
                outcomes.append(
                    undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice"))
            except undo.UndoError as e:
                errors.append(e.status)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(len(outcomes) + len(errors), 2)
        # At most one loser, and only with a 409.
        self.assertTrue(all(s == 409 for s in errors))
        # Exactly one execution: all 10 rows deleted once, batch terminal.
        self.assertEqual(cal.list_calibrations(), [])
        final = undo.get_undo(prev["id"])
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["report"]["counts"]["deleted"], 10)

    def test_stale_running_batch_is_recovered(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        prev = undo.preview_undo(jid)
        # Simulate a crashed executor: batch stuck in 'running' long ago.
        db.execute(
            "UPDATE undo_batches SET status='running', confirmed_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", prev["id"]))
        p2 = undo.preview_undo(jid)  # triggers stale recovery
        self.assertEqual(undo.get_undo(prev["id"])["status"], "failed")
        batch = undo.confirm_undo(jid, p2["id"], p2["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")


class TestCompensationWorkflow(BaseCase):
    def test_compensation_resolve_and_query(self):
        d = self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T12:00:00",
                    "result": "pass"})  # makes the imported row 'depended'
        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")

        pending = undo.list_compensations(status="pending", import_job_id=jid)
        self.assertEqual(len(pending), 1)
        comp = undo.resolve_compensation(pending[0]["id"], "bob", "已人工核对并删除")
        self.assertEqual(comp["status"], "resolved")
        self.assertEqual(comp["resolved_by"], "bob")
        self.assertEqual(comp["resolution"], "已人工核对并删除")
        self.assertEqual(undo.list_compensations(status="pending"), [])
        # Resolving again is idempotent.
        again = undo.resolve_compensation(pending[0]["id"], "bob")
        self.assertEqual(again["status"], "resolved")
        # Operator is required.
        with self.assertRaises(undo.UndoError):
            undo.resolve_compensation(pending[0]["id"], None)

    def test_empty_import_undoes_cleanly(self):
        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nGHOST,2026-09-08 09:00,pass\n")
        prev = undo.preview_undo(jid)
        self.assertEqual(prev["preview"]["counts"]["compensations_expected"], 0)
        batch = undo.confirm_undo(jid, prev["id"], prev["confirm_token"], "alice")
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["report"]["counts"]["deleted"], 0)


class TestPostRecoveryQueries(BaseCase):
    def test_import_detail_and_undo_queries(self):
        d = self.mkdev("D-1")
        issue = self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08 10:00,conditional\n"
            "D-1,2026-09-08 11:00,pass\n")
        notify.mark_sent(notify.list_notifications(import_job_id=jid)[0]["id"])
        batch = self.undo_now(jid)
        self.assertEqual(batch["status"], "partial")  # sent notification -> compensation

        summary = undo.latest_undo_summary(jid)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["counts"]["compensations"], 1)
        self.assertEqual(undo.count_compensations(status="pending", import_job_id=jid), 1)

        latest = undo.latest_undo_for_import(jid)
        self.assertEqual(latest["id"], batch["id"])
        self.assertIsNotNone(latest["preview"])   # the diff shown before confirm
        self.assertIsNotNone(latest["report"])    # what actually happened
        self.assertEqual(undo.get_undo(batch["id"])["status"], "partial")

        # Domain state reflects the recovery.
        self.assertEqual(issues.get(issue.id).status, "open")
        self.assertEqual(cal.list_calibrations(), [])
        self.assertEqual(len(notify.list_notifications(status="revoked")), 1)
        self.assertEqual(len(notify.list_notifications(status="sent")), 1)


class TestUndoApi(BaseCase):
    """Smoke-test the HTTP route handlers (routing + error mapping)."""

    def _ctx(self, params=None, query=None, body=None):
        from app.api.server import Ctx
        q = {k: [v] for k, v in (query or {}).items()}
        raw = json.dumps(body).encode() if body is not None else b""
        return Ctx(None, params or {}, q, raw)

    def test_preview_confirm_query_flow(self):
        from app.api import server
        d = self.mkdev("D-1")
        self.open_issue(d)
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 10:00,pass\n")

        prev = server.undo_preview(self._ctx({"id": str(jid)}))
        self.assertEqual(prev["status"], "preview")
        self.assertEqual(prev["preview"]["counts"]["restorable"], 1)

        batch = server.undo_confirm(self._ctx(
            {"id": str(jid)},
            body={"undo_id": prev["id"], "confirm_token": prev["confirm_token"],
                  "operator": "alice"}))
        self.assertEqual(batch["status"], "done")

        detail = server.get_import(self._ctx({"id": str(jid)}))
        self.assertEqual(detail["status"], "undone")
        self.assertEqual(detail["undo"]["status"], "done")
        self.assertEqual(detail["pending_compensations"], 0)

        latest = server.latest_undo(self._ctx({"id": str(jid)}))
        self.assertEqual(latest["id"], prev["id"])
        fetched = server.get_undo(self._ctx({"id": str(prev["id"])}))
        self.assertEqual(fetched["report"]["counts"]["deleted"], 1)
        notifs = server.list_notifications(self._ctx(query={"status": "revoked"}))
        self.assertEqual(len(notifs), 1)

    def test_api_error_mapping(self):
        from app.api import server
        with self.assertRaises(server.ApiError) as ctx:
            server.undo_preview(self._ctx({"id": "999"}))
        self.assertEqual(ctx.exception.status, 404)

        self.mkdev("D-1")
        jid, _ = self.run_import(
            "device_code,calibrated_at,result\nD-1,2026-09-08 09:00,pass\n")
        prev = server.undo_preview(self._ctx({"id": str(jid)}))
        with self.assertRaises(server.ApiError) as ctx:
            server.undo_confirm(self._ctx(
                {"id": str(jid)},
                body={"undo_id": prev["id"], "confirm_token": "nope",
                      "operator": "alice"}))
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(server.ApiError) as ctx:
            server.get_undo(self._ctx({"id": "999"}))
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
