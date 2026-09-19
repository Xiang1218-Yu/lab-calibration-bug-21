"""Entry point.

Usage:
    python -m app.main                 # start API server + background worker
    python -m app.main serve           # same
    python -m app.main worker          # run only the background worker (drain once/loop)
    python -m app.main once            # process due jobs once and exit
    python -m app.main reseed          # wipe & recreate demo data
    python -m app.main demo-import     # enqueue a demo import job

Environment overrides: CALTRACK_HOST, CALTRACK_PORT, CALTRACK_DB, ...
"""
from __future__ import annotations

import sys
import time

from . import config, db
from .api import seed
from .api.server import create_server
from .tasks import runner as task_runner


def cmd_serve():
    httpd = create_server()
    host, port = httpd.server_address[:2]
    print(f"CalTrack running at http://{host}:{port}")
    print(f"  API:   http://{host}:{port}/api/dashboard")
    print(f"  UI:    http://{host}:{port}/")
    print(f"  DB:    {config.DB_PATH}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.shutdown()


def cmd_worker():
    db.init_db()
    task_runner.start_worker()
    print("Background worker started. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def cmd_once():
    db.init_db()
    n = task_runner.run_once()
    print(f"Processed {n} job(s).")


def cmd_reseed():
    db.init_db()
    # Wipe domain data but keep jobs/locks.
    for table in ("compensations", "undo_batches", "import_items", "import_effects",
                  "notifications", "issue_events", "issues", "calibrations", "events",
                  "import_jobs", "devices"):
        db.execute(f"DELETE FROM {table}")
    seed.ensure_seed_data(force=True)
    print("Demo data recreated.")


def cmd_demo_import():
    """Enqueue an import of a sample CSV bundled under data/."""
    from pathlib import Path
    from .tasks import runner
    from .services import importer
    sample = config.DATA_DIR / "sample_calibrations.csv"
    if not sample.exists():
        _write_sample(sample)
    content = sample.read_text(encoding="utf-8")
    rows = importer.parse_content(content, "csv")
    job_id = importer.create_import_job(sample.name, len(rows))
    runner.enqueue(
        "import_calibrations",
        {"import_job_id": job_id, "path": str(sample), "fmt": "csv",
         "auto_create_devices": True},
        idempotency_key=f"import:{job_id}")
    print(f"Enqueued demo import job {job_id} ({len(rows)} rows). "
          f"Poll GET /api/imports/{job_id}")


def _write_sample(path):
    path.write_text(
        "device_code,calibrated_at,result,technician,measured_value,nominal_value,tolerance,unit,notes\n"
        "BAL-001,2026-09-08 09:00,pass,张工,200.000,200.0,0.005,g,常规校准\n"
        "PH-014,2026-09-08 10:00,pass,王工,7.01,7.00,0.05,pH,更换电极后复测通过\n"
        "NEW-100,2026-09-08 11:00,pass,李工,10.0,10.0,0.1,mL,新设备自动建档\n"
        "BAL-001,2026-09-08 09:00,pass,张工,200.000,200.0,0.005,g,重复行应被判重\n"
        "OVN-007,bad-date,pass,赵工,,,,,错误时间示例\n"
        "PH-014,2026-09-08 12:00,maybe,王工,7.0,7.0,0.05,pH,非法结果示例\n",
        encoding="utf-8")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "serve"
    return {
        "serve": cmd_serve,
        "worker": cmd_worker,
        "once": cmd_once,
        "reseed": cmd_reseed,
        "demo-import": cmd_demo_import,
    }.get(cmd, cmd_serve)()


if __name__ == "__main__":
    main()
