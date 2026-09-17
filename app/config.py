"""Central configuration. Values can be overridden with environment variables."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CALTRACK_DATA_DIR", BASE_DIR / "data"))
WEB_DIR = Path(os.environ.get("CALTRACK_WEB_DIR", BASE_DIR / "web"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("CALTRACK_DB", DATA_DIR / "caltrack.db"))

# HTTP server
HOST = os.environ.get("CALTRACK_HOST", "127.0.0.1")
PORT = int(os.environ.get("CALTRACK_PORT", "8000"))

# Background task framework
TASK_MAX_RETRIES = int(os.environ.get("CALTRACK_TASK_MAX_RETRIES", "3"))
TASK_TIMEOUT_SECONDS = float(os.environ.get("CALTRACK_TASK_TIMEOUT", "120"))
TASK_POLL_INTERVAL = float(os.environ.get("CALTRACK_TASK_POLL_INTERVAL", "2"))

# Anomaly rules.
# Consecutive abnormal (warning+ severity) events on one device within this
# window raise an OPEN issue.
ANOMALY_CONSECUTIVE_THRESHOLD = int(
    os.environ.get("CALTRACK_ANOMALY_THRESHOLD", "3")
)
ANOMALY_WINDOW_MINUTES = int(
    os.environ.get("CALTRACK_ANOMALY_WINDOW_MINUTES", "1440")
)

# Calibration result -> issue effect
RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_CONDITIONAL = "conditional"
CALIBRATION_RESULTS = (RESULT_PASS, RESULT_FAIL, RESULT_CONDITIONAL)

# Severity ordering, low -> high. Anything >= WARNING counts as "abnormal".
SEVERITY_ORDER = ("info", "warning", "critical")
ABNORMAL_SEVERITIES = ("warning", "critical")

# Issue lifecycle
ISSUE_OPEN = "open"
ISSUE_MONITORING = "monitoring"
ISSUE_RESOLVED = "resolved"
ISSUE_STATUSES = (ISSUE_OPEN, ISSUE_MONITORING, ISSUE_RESOLVED)

# Task lifecycle
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"
TASK_RETRYING = "retrying"
TASK_TIMEOUT = "timeout"
TASK_STATUSES = (
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SUCCESS,
    TASK_FAILED,
    TASK_RETRYING,
    TASK_TIMEOUT,
)
