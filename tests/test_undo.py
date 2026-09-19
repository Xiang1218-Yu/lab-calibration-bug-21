"""Tests for the auditable import undo / compensation subsystem.

Covers the three-record-classification, manual-twin protection, retry-attempt
history, attachments/notifications, issue-state restoration, idempotency,
concurrency locking, permission confirmation, preview diffs, post-undo
queries and the two-phase HTTP API. Stdlib unittest only.
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CALTRACK_DB", os.path.join(_tmp, "test_undo.db"))
os.environ.setdefault("CALTRACK_TASK_TIMEOUT", "2")
os.environ.setdefault("CALTRACK_TASK_MAX_RETRIES", "2")

from app import db  # noqa: E402
from app.services import (  # noqa: E402
    attachments, attempt_cleanup, auth, calibrations as cal,
    compensation, devices, events, importer, issues, notifications, undo)

TABLES = ("issue_events", "issues", "calibrations", "events", "import_jobs",
          "jobs", "task_locks", "devices", "import_attempts", "attachments",
          "notifications", "issue_transitions", "undo_batches",
          "compensation_items")


class UndoBase(unittest.TestCase):
    def setUp(self):
        db.init_db()
        conn = db.get_conn()
        conn.execute("PRAGMA foreign_keys = OFF;")
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON;")

    # -- fixtures -----------------------------------------------------------
    def mkdev(self, code="D-1"):
        return devices.create({"code": code, "name": "Dev " + code})

    def open_issue(self, d, day="2026-09-08"):
        for i in range(3):
            events.create({
                "device_id": d.id, "occurred_at": f"{day}T0{i}:00:00",
                "severity": "warning", "message": "w"})
        return issues.open_issue_for_device(d.id)

    def import_csv(self, csv_text, *, job="f.csv", auto=False, notify=True):
        rows = importer.parse_content(csv_text, "csv")
        jid = importer.create_import_job(job, len(rows))
        aid = importer.start_attempt(jid)
        res = importer.process_rows(
            rows, jid, auto_create_devices=auto,
            import_attempt_id=aid, emit_notifications=notify)
        return jid, aid, res

    def preview_commit(self, jid, *, actor="admin", idem=None, force=False):
        plan = undo.preview(jid, requested_by=actor)
        result = undo.commit(jid, plan["confirm_token"], requested_by=actor,
                             idem_key=idem, force=force)
        return plan, result


class TestClassification(UndoBase):
    def test_plain_record_is_deletable_and_removed(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T05:00:00,pass\n")  # no issue -> no state effect
        plan = undo.preview(jid)
        self.assertEqual(plan["counts"]["deletable"], 1)
        self.assertTrue(plan["auto_recoverable"])
        _, r = self.preview_commit(jid)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(undo.imported_calibrations(jid)), 0)
        self.assertEqual(importer.get_import_job(jid)["status"], "revoked")

    def test_state_driving_record_inverts_issue_state(self):
        d = self.mkdev()
        issue = self.open_issue(d)
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,pass\n")  # resolves the issue
        self.assertEqual(issues.get(issue.id).status, "resolved")

        plan = undo.preview(jid)
        self.assertEqual(plan["counts"]["state_driving"], 1)
        inv = plan["state_driving"][0]["inverse"]
        self.assertEqual(inv["to_status"], "open")

        _, r = self.preview_commit(jid)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(issues.get(issue.id).status, "open")
        # A compensating "restored" audit row exists.
        rows = db.query(
            "SELECT * FROM issue_transitions WHERE issue_id=? AND action='restored'",
            (issue.id,))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cause_type"], "import_undo")

    def test_state_dependent_when_later_manual_transition(self):
        d = self.mkdev()
        issue = self.open_issue(d)
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,conditional\n")  # -> monitoring
        self.assertEqual(issues.get(issue.id).status, "monitoring")
        # Later human action resolves it: the import record now has a dependent
        # later state and must NOT be auto-rewound.
        time.sleep(1.01)  # transition timestamps are second-precision
        issues.resolve(issue.id, "人工修好", actor="tech")

        plan = undo.preview(jid)
        self.assertEqual(plan["counts"]["state_dependent"], 1)
        self.assertFalse(plan["auto_recoverable"])
        _, r = self.preview_commit(jid)
        self.assertEqual(r["status"], "partial")
        # Record retained, issue left resolved, compensation queued.
        self.assertEqual(len(undo.imported_calibrations(jid)), 1)
        self.assertEqual(issues.get(issue.id).status, "resolved")
        kinds = [c["kind"] for c in compensation.list_items(import_job_id=jid)]
        self.assertIn("dependent_record", kinds)


class TestManualTwinProtection(UndoBase):
    def test_delete_predicate_never_matches_manual_source(self):
        # Even if a manual row shared every column shape, the delete is guarded
        # by source!='manual'; assert the SQL predicate explicitly.
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result,technician,measured_value\n"
            "D-1,2026-09-08T05:00:00,pass,A,1.0\n")
        # A genuinely separate manual record (different content) stays intact.
        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T07:00:00",
                    "result": "fail", "technician": "M", "measured_value": 9.0})
        self.preview_commit(jid)
        manual = db.query(
            "SELECT * FROM calibrations WHERE source='manual'")
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(undo.imported_calibrations(jid)), 0)

    def test_manual_twin_branch_buckets_as_state_dependent(self):
        # Exercise the defensive twin classifier directly (the same-content
        # twin is normally precluded by the UNIQUE(device_id, content_hash)
        # constraint; this covers the legacy/partial-index escape hatch).
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T05:00:00,pass\n")
        imported = undo.imported_calibrations(jid)[0]
        # Monkeypatch the twin lookup to claim a manual twin exists.
        orig = undo._manual_twin
        undo._manual_twin = lambda c: {"id": 777}
        try:
            buckets = undo.classify(jid)
        finally:
            undo._manual_twin = orig
        dep = [e for e in buckets["state_dependent"]
               if e["calibration_id"] == imported["id"]]
        self.assertEqual(len(dep), 1)
        self.assertEqual(dep[0]["reason"], "manual_twin")
        self.assertEqual(dep[0]["manual_twin_id"], 777)


class TestRetryAttempts(UndoBase):
    def test_cleanup_attempt_reverses_and_retracts_without_double_count(self):
        d = self.mkdev()
        issue = self.open_issue(d)
        rows = importer.parse_content(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,pass\n", "csv")
        jid = importer.create_import_job("r.csv", len(rows))
        aid1 = importer.start_attempt(jid)
        importer.process_rows(rows, jid, import_attempt_id=aid1)
        self.assertEqual(issues.get(issue.id).status, "resolved")
        # Attempt 1 fails and is cleaned before a retry.
        importer.fail_attempt(aid1, "boom")
        stats = attempt_cleanup.cleanup_attempt(jid, aid1)
        self.assertGreaterEqual(stats["deleted"], 1)
        self.assertEqual(issues.get(issue.id).status, "open")
        # Retry reproduces the same outcome - no duplicate rows.
        aid2 = importer.start_attempt(jid)
        res = importer.process_rows(rows, jid, import_attempt_id=aid2)
        self.assertEqual(res["summary"]["accepted"], 1)
        self.assertEqual(issues.get(issue.id).status, "resolved")
        self.assertEqual(len(undo.imported_calibrations(jid)), 1)
        # Both attempts are visible in history.
        attempts = importer.list_attempts(jid)
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["status"], "cleaned")
        self.assertEqual(attempts[1]["status"], "succeeded")

    def test_retry_cleanup_compensates_when_state_moved(self):
        d = self.mkdev()
        issue = self.open_issue(d)
        rows = importer.parse_content(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,conditional\n", "csv")
        jid = importer.create_import_job("r2.csv", len(rows))
        aid1 = importer.start_attempt(jid)
        importer.process_rows(rows, jid, import_attempt_id=aid1)
        importer.fail_attempt(aid1, "boom")
        time.sleep(1.01)
        issues.resolve(issue.id, "later manual", actor="tech")
        stats = attempt_cleanup.cleanup_attempt(jid, aid1)
        self.assertEqual(stats["compensated"], 1)
        self.assertEqual(len(undo.imported_calibrations(jid)), 1)  # retained


class TestAttachmentsAndNotifications(UndoBase):
    def test_attachments_revoked_and_quarantined(self):
        d = self.mkdev()
        jid, aid, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        a = attachments.register(
            attachable_type="import_job", attachable_id=jid,
            filename="cert.pdf", content=b"%PDF-1.4 x",
            source=f"import:{jid}", import_job_id=jid, import_attempt_id=aid)
        self.assertTrue(os.path.exists(a["path"]))
        _, r = self.preview_commit(jid)
        self.assertEqual(r["attachments_revoked"], 1)
        self.assertEqual(attachments.get(a["id"])["status"], "revoked")
        self.assertTrue(os.path.exists(r["files_quarantined"][0]))
        self.assertFalse(os.path.exists(a["path"]))

    def test_unread_notification_retracted_read_notification_compensated(self):
        d = self.mkdev()
        self.open_issue(d)
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,pass\n")
        ns = [n for n in notifications.list_notifications(import_job_id=jid)
              if n["type"] == "issue_state"]
        self.assertEqual(len(ns), 1)
        notifications.mark_read(ns[0]["id"])
        _, r = self.preview_commit(jid)
        # Read notification can't be retracted -> compensation, batch partial.
        self.assertEqual(r["notifications_retracted"], 0)
        self.assertTrue(any(
            c["kind"] == "notification_read"
            for c in compensation.list_items(import_job_id=jid)))


class TestIdempotencyConcurrency(UndoBase):
    def test_commit_idempotent_replay(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        plan = undo.preview(jid)
        r1 = undo.commit(jid, plan["confirm_token"], idem_key="abc")
        r2 = undo.commit(jid, plan["confirm_token"], idem_key="abc")
        self.assertTrue(r2["replayed"])
        self.assertEqual(r1["undo_batch_id"], r2["undo_batch_id"])

    def test_concurrent_undo_single_flight(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        plan = undo.preview(jid)
        holder = __import__("app.tasks.locks", fromlist=["locks"]).acquire(
            f"undo:import:{jid}", ttl_seconds=60)
        self.assertIsNotNone(holder)
        try:
            with self.assertRaises(undo.UndoConflict):
                undo.commit(jid, plan["confirm_token"])
        finally:
            __import__("app.tasks.locks", fromlist=["locks"]).release(
                f"undo:import:{jid}", holder)

    def test_stale_confirm_token_rejected(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        p1 = undo.preview(jid)
        p2 = undo.preview(jid)  # supersedes p1
        with self.assertRaises(undo.UndoError):
            undo.commit(jid, p1["confirm_token"])
        # Newer token works.
        undo.commit(jid, p2["confirm_token"])

    def test_fingerprint_drift_blocks_commit(self):
        d = self.mkdev()
        self.open_issue(d)
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,pass\n")
        plan = undo.preview(jid)
        # Insert an out-of-band extra import calibration -> fingerprint changes.
        from app.utils import content_hash, iso as isots
        ch = content_hash(d.id, "2027-01-01T00:00:00+00:00", "x", "3.0")
        db.execute(
            "INSERT INTO calibrations (device_id,calibrated_at,result,technician,"
            "measured_value,source,import_job_id,content_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (d.id, "2027-01-01T00:00:00+00:00", "pass", "x", 3.0,
             f"import:{jid}", jid, ch, isots()))
        with self.assertRaises(undo.UndoError):
            undo.commit(jid, plan["confirm_token"])

    def test_processing_batch_cannot_be_undone(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        db.execute("UPDATE import_jobs SET status='processing' WHERE id=?", (jid,))
        with self.assertRaises(undo.UndoConflict):
            undo.preview(jid)


class TestPermissions(UndoBase):
    def _princ(self, role, actor="u"):
        return auth.Principal(actor=actor, role=role)

    def test_role_permissions(self):
        self.assertFalse(self._princ("viewer").can("import:undo"))
        self.assertFalse(self._princ("operator").can("import:undo"))
        self.assertTrue(self._princ("admin").can("import:undo"))
        with self.assertRaises(auth.AuthorizationError):
            auth.require(self._princ("operator"), "import:undo")
        # No exception for admin.
        auth.require(self._princ("admin"), "import:undo")

    def test_confirmation_requires_flag_and_token(self):
        p = self._princ("admin")
        with self.assertRaises(auth.AuthorizationError):
            auth.require_undo_confirmation(p, {"confirm": False}, True)
        with self.assertRaises(auth.AuthorizationError):
            auth.require_undo_confirmation(p, {"confirm": True}, True)
        auth.require_undo_confirmation(
            p, {"confirm": True, "confirm_token": "t"}, True)


class TestCompensationWorkflow(UndoBase):
    def test_queue_dedup_and_resolve(self):
        d = self.mkdev()
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\nD-1,2026-09-08T05:00:00,pass\n")
        a = compensation.queue(kind="retry_artifact", summary="x",
                               import_job_id=jid, ref_type="calibration",
                               ref_id=99)
        b = compensation.queue(kind="retry_artifact", summary="x",
                               import_job_id=jid, ref_type="calibration",
                               ref_id=99)
        self.assertEqual(a, b)  # collapsed into one pending item
        item = compensation.resolve(a, resolution="done", resolved_by="admin")
        self.assertEqual(item["status"], "resolved")
        # Resolving again is a no-op.
        again = compensation.resolve(a, resolution="again", resolved_by="admin")
        self.assertEqual(again["resolution"], "done")


class TestPostUndoQuery(UndoBase):
    def test_status_and_recovery_queries(self):
        d = self.mkdev()
        issue = self.open_issue(d)
        jid, _, _ = self.import_csv(
            "device_code,calibrated_at,result\n"
            "D-1,2026-09-08T09:00:00,pass\n")
        _, r = self.preview_commit(jid)
        st = undo.status(jid)
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["executed_by"], "admin")
        # Audit transitions queryable per issue.
        rows = db.query(
            "SELECT action, from_status, to_status FROM issue_transitions "
            "WHERE issue_id=? ORDER BY id", (issue.id,))
        actions = [x["action"] for x in rows]
        self.assertIn("resolved", actions)
        self.assertIn("restored", actions)
        self.assertEqual(issues.get(issue.id).status, "open")


class TestHttpApi(UndoBase):
    """Spin the real stdlib HTTP server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        from app.api import server
        cls.server = server.create_server(
            host="127.0.0.1", port=0, start_worker=False)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        if body is not None:
            data = (json.dumps(body).encode()
                    if hdrs["Content-Type"] == "application/json"
                    else body.encode())
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_two_phase_undo_over_http(self):
        d = self.mkdev("HTTP-1")
        self.open_issue(d)
        # Import synchronously as operator.
        csv_body = ("device_code,calibrated_at,result\n"
                    "HTTP-1,2026-09-08T09:00:00,pass\n")
        st, resp = self._req(
            "POST", "/api/imports?fmt=csv&sync=true",
            body=csv_body,
            headers={"Content-Type": "text/csv", "X-Role": "operator",
                     "X-Actor": "op1"})
        self.assertEqual(st, 200, resp)
        jid = resp["data"]["id"]

        # Operator cannot undo (403).
        st, resp = self._req("POST", f"/api/imports/{jid}/undo/preview",
                             body={}, headers={"X-Role": "operator"})
        self.assertEqual(st, 403)

        # Admin previews.
        st, plan = self._req("POST", f"/api/imports/{jid}/undo/preview",
                             body={"reason": "wrong file"},
                             headers={"X-Role": "admin", "X-Actor": "root"})
        self.assertEqual(st, 200)
        self.assertEqual(plan["data"]["counts"]["state_driving"], 1)
        token = plan["data"]["confirm_token"]

        # Commit without confirm flag -> 400.
        st, resp = self._req("POST", f"/api/imports/{jid}/undo/commit",
                             body={"confirm_token": token},
                             headers={"X-Role": "admin"})
        self.assertEqual(st, 400)

        # Proper commit.
        st, done = self._req(
            "POST", f"/api/imports/{jid}/undo/commit",
            body={"confirm": True, "confirm_token": token},
            headers={"X-Role": "admin", "X-Actor": "root",
                     "X-Idempotency-Key": "http-1"})
        self.assertEqual(st, 200, done)
        self.assertEqual(done["data"]["status"], "completed")

        # Idempotent replay over HTTP returns same undo batch.
        st, again = self._req(
            "POST", f"/api/imports/{jid}/undo/commit",
            body={"confirm": True, "confirm_token": token},
            headers={"X-Role": "admin", "X-Idempotency-Key": "http-1"})
        self.assertEqual(st, 200)
        self.assertTrue(again["data"]["replayed"])

        # Status query.
        st, stt = self._req("GET", f"/api/imports/{jid}/undo")
        self.assertEqual(st, 200)
        self.assertEqual(stt["data"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
