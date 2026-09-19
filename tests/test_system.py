"""End-to-end tests for the calibration tracker (stdlib unittest only).

Run:  CALTRACK_DB=$(mktemp -d)/t.db python3 -m unittest discover -s tests -v
The suite points the app at a throwaway database via CALTRACK_DB.
"""
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("CALTRACK_DB", os.path.join(_tmp, "test_caltrack.db"))
os.environ.setdefault("CALTRACK_TASK_TIMEOUT", "2")
os.environ.setdefault("CALTRACK_TASK_MAX_RETRIES", "2")

from app import db  # noqa: E402
from app.services import (  # noqa: E402
    calibrations as cal, devices, events, importer, issues)
from app.tasks import runner, locks  # noqa: E402
from app.tasks.runner import task  # noqa: E402
from app.utils import iso  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        # Fresh schema for full isolation: rebuild in-memory-ish by clearing rows.
        db.init_db()
        for t in ("compensations", "undo_batches", "import_items", "import_effects",
                  "notifications", "issue_events", "issues", "calibrations", "events",
                  "import_jobs", "jobs", "task_locks", "devices"):
            db.execute(f"DELETE FROM {t}")

    def mkdev(self, code="D-1"):
        return devices.create({"code": code, "name": "Dev " + code})


class TestAnomalyRule(BaseCase):
    def test_info_events_do_not_trigger(self):
        d = self.mkdev()
        for i in range(5):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-08T0{i}:00:00",
                           "severity": "info", "message": "ok"})
        self.assertIsNone(issues.open_issue_for_device(d.id))

    def test_three_abnormal_within_window_open_issue(self):
        d = self.mkdev()
        for i in range(2):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-08T0{i}:00:00",
                           "severity": "warning", "code": "E_X", "message": "w"})
        self.assertIsNone(issues.open_issue_for_device(d.id))  # threshold is 3
        events.create({"device_id": d.id, "occurred_at": "2026-09-08T05:00:00",
                       "severity": "critical", "code": "E_X", "message": "c"})
        issue = issues.open_issue_for_device(d.id)
        self.assertIsNotNone(issue)
        self.assertEqual(issue.status, "open")
        self.assertEqual(issue.severity, "critical")  # escalated to worst
        self.assertEqual(issue.event_count, 3)

    def test_events_outside_window_do_not_cluster(self):
        d = self.mkdev()
        events.create({"device_id": d.id, "occurred_at": "2026-09-01T00:00:00",
                       "severity": "warning", "message": "w"})
        events.create({"device_id": d.id, "occurred_at": "2026-09-03T00:00:00",
                       "severity": "warning", "message": "w"})
        events.create({"device_id": d.id, "occurred_at": "2026-09-08T00:00:00",
                       "severity": "warning", "message": "w"})
        self.assertIsNone(issues.open_issue_for_device(d.id))


class TestCalibrationTransitions(BaseCase):
    def _open_issue(self, d):
        for i in range(3):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-08T0{i}:00:00",
                           "severity": "warning", "message": "w"})
        return issues.open_issue_for_device(d.id)

    def test_conditional_then_pass_then_fail(self):
        d = self.mkdev()
        issue = self._open_issue(d)
        self.assertEqual(issue.status, "open")

        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T04:00:00",
                    "result": "conditional"})
        self.assertEqual(issues.get(issue.id).status, "monitoring")

        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T05:00:00",
                    "result": "pass"})
        self.assertEqual(issues.get(issue.id).status, "resolved")

        # New anomalies reopen a fresh issue; a fail holds it open.
        for i in range(3):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-09T0{i}:00:00",
                           "severity": "warning", "message": "w"})
        issue2 = issues.open_issue_for_device(d.id)
        self.assertIsNotNone(issue2)
        cal.create({"device_id": d.id, "calibrated_at": "2026-09-09T05:00:00",
                    "result": "fail"})
        self.assertEqual(issues.get(issue2.id).status, "open")

    def test_duplicate_calibration_rejected(self):
        d = self.mkdev()
        cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T05:00:00",
                    "result": "pass", "technician": "A", "measured_value": 1.0})
        with self.assertRaises(cal.CalibrationError):
            cal.create({"device_id": d.id, "calibrated_at": "2026-09-08T05:00:00",
                        "result": "pass", "technician": "A", "measured_value": 1.0})


class TestImport(BaseCase):
    def test_import_report(self):
        self.mkdev("D-1")
        rows = importer.parse_content(
            "device_code,calibrated_at,result,measured_value\n"
            "D-1,2026-09-08 09:00,pass,10\n"
            "D-1,2026-09-08 09:00,pass,10\n"   # in-file duplicate
            "GHOST,2026-09-08 09:00,pass,1\n"   # unknown device
            "D-1,not-a-date,pass,1\n", "csv")   # bad timestamp
        jid = importer.create_import_job("x.csv", len(rows))
        result = importer.process_rows(rows, jid, auto_create_devices=False)
        s = result["summary"]
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["accepted"], 1)
        self.assertEqual(s["duplicate"], 1)
        self.assertEqual(s["errors"], 2)

    def test_import_auto_create_and_issue_resolution(self):
        d = self.mkdev("D-9")
        for i in range(3):
            events.create({"device_id": d.id, "occurred_at": f"2026-09-08T0{i}:00:00",
                           "severity": "warning", "message": "w"})
        self.assertIsNotNone(issues.open_issue_for_device(d.id))
        rows = importer.parse_content(
            "device_code,calibrated_at,result\n"
            "D-9,2026-09-08 10:00,pass\n"
            "BRANDNEW,2026-09-08 11:00,pass\n", "csv")
        jid = importer.create_import_job("y.csv", len(rows))
        importer.process_rows(rows, jid, auto_create_devices=True)
        self.assertIsNone(issues.open_issue_for_device(d.id))  # pass resolved it
        self.assertIsNotNone(devices.get_by_code("BRANDNEW"))


class TestTaskFramework(BaseCase):
    def test_retry_then_success(self):
        calls = {"n": 0}

        @task("ut_flaky")
        def flaky(payload, ctx):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("boom")
            return {"ok": True}

        runner.enqueue("ut_flaky", {}, max_attempts=3)
        runner.run_once()
        self.assertEqual(calls["n"], 1)
        db.execute("UPDATE jobs SET next_run_at=? WHERE task_name='ut_flaky'",
                   ("2020-01-01T00:00:00+00:00",))
        runner.run_once()
        job = db.query_one("SELECT * FROM jobs WHERE task_name='ut_flaky'")
        self.assertEqual(job["status"], "success")
        self.assertEqual(calls["n"], 2)

    def test_idempotent_enqueue(self):
        @task("ut_idem")
        def t(payload, ctx):
            return True
        a = runner.enqueue("ut_idem", {"x": 1}, idempotency_key="k")
        b = runner.enqueue("ut_idem", {"x": 2}, idempotency_key="k")
        self.assertEqual(a["id"], b["id"])

    def test_timeout_marks_failed(self):
        @task("ut_slow")
        def slow(payload, ctx):
            ctx.sleep(30)
            return True
        runner.enqueue("ut_slow", {}, max_attempts=1)
        runner.run_once()
        job = db.query_one("SELECT * FROM jobs WHERE task_name='ut_slow'")
        self.assertEqual(job["status"], "failed")
        self.assertIn("timed out", job["last_error"])

    def test_stale_running_recovered(self):
        @task("ut_stale")
        def t(payload, ctx):
            return True
        from datetime import timedelta
        from app.utils import utcnow
        old = iso(utcnow() - timedelta(hours=2))
        db.execute(
            "INSERT INTO jobs (task_name,payload_json,status,attempts,max_attempts,"
            "scheduled_at,next_run_at,run_lock,run_started_at,heartbeat_at,created_at,updated_at)"
            " VALUES ('ut_stale','{}','running',1,3,?,?,?,?,?,?,?)",
            (old, old, "ghost", old, old, old, old))
        jid = db.query_one("SELECT id FROM jobs WHERE task_name='ut_stale'")["id"]
        runner.run_once()  # recovery first, then it runs to success
        job = db.query_one("SELECT * FROM jobs WHERE id=?", (jid,))
        self.assertIsNone(job["run_lock"])
        self.assertEqual(job["status"], "success")

    def test_single_flight_lock(self):
        t1 = locks.acquire("ut_resource", ttl_seconds=60)
        self.assertIsNotNone(t1)
        self.assertIsNone(locks.acquire("ut_resource", ttl_seconds=60))  # held
        locks.release("ut_resource", t1)
        self.assertIsNotNone(locks.acquire("ut_resource", ttl_seconds=60))


if __name__ == "__main__":
    unittest.main()
