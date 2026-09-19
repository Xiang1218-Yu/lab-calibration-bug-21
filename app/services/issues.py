"""Issue lifecycle and the anomaly rule engine.

Rules (extensible — register new callables in ``RULES``):

* ``consecutive_anomaly``: when a device accumulates ``ANOMALY_CONSECUTIVE_THRESHOLD``
  abnormal (warning/critical) events within ``ANOMALY_WINDOW_MINUTES``, and it has
  no open/monitoring issue, an ``open`` issue is created. Further abnormal events
  attach to the existing issue and bump its counter.

Issue state is also driven by calibration results (see :func:`apply_calibration`):
a *pass* resolves open/monitoring issues; a *fail* re-opens a monitoring issue;
a *conditional* result moves the issue to ``monitoring`` (watch, not yet fixed).
"""
from __future__ import annotations

from typing import Optional

from .. import config, db
from ..models import Issue
from ..utils import iso, parse_ts


class IssueError(ValueError):
    pass


# -- queries ----------------------------------------------------------------

def get(issue_id: int) -> Optional[Issue]:
    row = db.query_one("SELECT * FROM issues WHERE id=?", (issue_id,))
    return Issue.from_row(row) if row else None


def list_issues(device_id: Optional[int] = None, status: Optional[str] = None,
                severity: Optional[str] = None) -> list[Issue]:
    sql = "SELECT * FROM issues WHERE 1=1"
    params: list = []
    if device_id is not None:
        sql += " AND device_id=?"
        params.append(device_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    if severity:
        sql += " AND severity=?"
        params.append(severity)
    sql += " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'monitoring' THEN 1 ELSE 2 END, updated_at DESC"
    return [Issue.from_row(r) for r in db.query(sql, params)]


def open_issue_for_device(device_id: int) -> Optional[Issue]:
    """Newest non-resolved issue for a device (single active issue per device)."""
    row = db.query_one(
        "SELECT * FROM issues WHERE device_id=? AND status != 'resolved' "
        "ORDER BY id DESC LIMIT 1", (device_id,))
    return Issue.from_row(row) if row else None


def linked_events(issue_id: int) -> list[dict]:
    rows = db.query(
        """SELECT e.* FROM events e
           JOIN issue_events ie ON ie.event_id = e.id
           WHERE ie.issue_id=? ORDER BY e.occurred_at, e.id""", (issue_id,))
    return [dict(r) for r in rows]


# -- mutations --------------------------------------------------------------

def _create_issue(device_id: int, severity: str, title: str, description: str,
                  rule: str, trigger_event_id: int, first_at: str) -> int:
    now = iso()
    issue_id = db.execute(
        """INSERT INTO issues
           (device_id, status, severity, title, description, rule,
            trigger_event_id, first_event_at, last_event_at, event_count,
            created_at, updated_at)
           VALUES (?, 'open', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (device_id, severity, title, description, rule, trigger_event_id,
         first_at, first_at, now, now))
    db.execute("INSERT OR IGNORE INTO issue_events (issue_id, event_id) VALUES (?,?)",
               (issue_id, trigger_event_id))
    return issue_id


def _attach_event(issue_id: int, event_id: int, occurred_at: str,
                  severity: str) -> None:
    db.execute("INSERT OR IGNORE INTO issue_events (issue_id, event_id) VALUES (?,?)",
               (issue_id, event_id))
    # Escalate severity if the new event is worse.
    sev_rank = {s: i for i, s in enumerate(config.SEVERITY_ORDER)}
    issue = get(issue_id)
    new_sev = issue.severity
    if sev_rank.get(severity, 0) > sev_rank.get(issue.severity, 0):
        new_sev = severity
    db.execute(
        """UPDATE issues SET last_event_at=?, event_count=event_count+1,
           severity=?, updated_at=? WHERE id=?""",
        (occurred_at, new_sev, iso(), issue_id))


def evaluate_event(event) -> Optional[Issue]:
    """Run all rules against the device after a new event. Returns the active issue."""
    for rule_fn in RULES:
        issue = rule_fn(event)
        if issue is not None:
            return issue
    return open_issue_for_device(event.device_id)


def rule_consecutive_anomaly(event) -> Optional[Issue]:
    """Create/extend an issue when abnormal events cluster together."""
    if event.severity not in config.ABNORMAL_SEVERITIES:
        return None

    # Recent abnormal events for this device within the window.
    window_start = iso(_shift(event.occurred_at, -config.ANOMALY_WINDOW_MINUTES))
    rows = db.query(
        """SELECT * FROM events
           WHERE device_id=? AND severity IN ('warning','critical')
             AND occurred_at >= ? AND occurred_at <= ?
           ORDER BY occurred_at, id""",
        (event.device_id, window_start, event.occurred_at))
    recent = [dict(r) for r in rows]
    count = len(recent)

    existing = open_issue_for_device(event.device_id)
    if existing is not None:
        # Attach only if not already linked; keep the active issue current.
        _attach_event(existing.id, event.id, event.occurred_at, event.severity)
        return get(existing.id)

    if count >= config.ANOMALY_CONSECUTIVE_THRESHOLD:
        worst = "critical" if any(e["severity"] == "critical" for e in recent) else "warning"
        codes = sorted({e["code"] for e in recent if e["code"]})
        title = f"连续 {count} 次异常事件"
        desc = (f"设备在 {config.ANOMALY_WINDOW_MINUTES} 分钟内出现 {count} 次 "
                f"warning/critical 事件"
                + (f"，代码: {', '.join(codes)}" if codes else "")
                + "，需排查处理。")
        issue_id = _create_issue(
            event.device_id, worst, title, desc,
            "consecutive_anomaly", event.id, recent[0]["occurred_at"])
        # Link the rest of the clustered events.
        for e in recent:
            if e["id"] != event.id:
                db.execute(
                    "INSERT OR IGNORE INTO issue_events (issue_id, event_id) VALUES (?,?)",
                    (issue_id, e["id"]))
        # Recompute count to cover all linked events.
        db.execute("UPDATE issues SET event_count=? WHERE id=?",
                   (len(recent), issue_id))
        return get(issue_id)
    return None


def _shift(ts_iso: str, minutes: int) -> "datetime":
    from datetime import timedelta
    return parse_ts(ts_iso) + timedelta(minutes=minutes)


# -- calibration-driven transitions ----------------------------------------

def _log_transition(issue, from_status, to_status, action, *,
                    cause_type, cause_id=None, import_job_id=None,
                    import_attempt_id=None, actor=None,
                    reversal_of_id=None, snapshot=None) -> int:
    snap = snapshot if snapshot is not None else {}
    return db.execute(
        """INSERT INTO issue_transitions
           (issue_id, device_id, from_status, to_status, action, cause_type,
            cause_id, import_job_id, import_attempt_id, actor,
            from_resolved_at, from_resolution, reversal_of_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (issue.id, issue.device_id, from_status, to_status, action, cause_type,
         cause_id, import_job_id, import_attempt_id, actor,
         snap.get("resolved_at"), snap.get("resolution"), reversal_of_id, iso()))


def apply_calibration(calibration, *, import_job_id=None,
                      import_attempt_id=None, actor=None) -> dict:
    """Update issue state based on a calibration result.

    Returns a dict describing the transition taken (for import summaries/logs).
    Every effective change is appended to ``issue_transitions`` so it can later
    be inverted by an import undo.
    """
    issue = open_issue_for_device(calibration.device_id)
    if issue is None:
        return {"action": "none", "reason": "no active issue"}

    result = calibration.result
    transition = {"issue_id": issue.id, "from": issue.status, "result": result}
    cause_type = "calibration"
    job_id = import_job_id if import_job_id is not None else getattr(
        calibration, "import_job_id", None)
    attempt_id = import_attempt_id if import_attempt_id is not None else getattr(
        calibration, "import_attempt_id", None)
    snapshot = {"resolved_at": issue.resolved_at, "resolution": issue.resolution}

    if result == config.RESULT_PASS:
        db.execute(
            "UPDATE issues SET status='resolved', resolved_at=?, "
            "resolution=?, updated_at=? WHERE id=?",
            (iso(), f"校准通过（{calibration.calibrated_at}），自动关闭", iso(), issue.id))
        transition.update(to="resolved", action="resolved")
    elif result == config.RESULT_CONDITIONAL:
        db.execute(
            "UPDATE issues SET status='monitoring', resolved_at=NULL, updated_at=? WHERE id=?",
            (iso(), issue.id))
        transition.update(to="monitoring", action="monitoring")
    elif result == config.RESULT_FAIL:
        # A failed calibration keeps/reopens the issue as open.
        db.execute(
            "UPDATE issues SET status='open', resolved_at=NULL, updated_at=? WHERE id=?",
            (iso(), issue.id))
        transition.update(to="open", action="reopened" if issue.status == "monitoring" else "held_open")
    else:
        transition.update(to=issue.status, action="noop")
        return transition

    tid = _log_transition(
        issue, issue.status, transition["to"], transition["action"],
        cause_type=cause_type, cause_id=calibration.id,
        import_job_id=job_id, import_attempt_id=attempt_id, actor=actor,
        snapshot=snapshot)
    transition["transition_id"] = tid
    return transition


def resolve(issue_id: int, resolution: Optional[str] = None,
            actor: Optional[str] = None) -> Issue:
    issue = get(issue_id)
    if issue is None:
        raise IssueError(f"issue {issue_id} not found")
    snapshot = {"resolved_at": issue.resolved_at, "resolution": issue.resolution}
    db.execute(
        "UPDATE issues SET status='resolved', resolved_at=?, resolution=?, updated_at=? WHERE id=?",
        (iso(), resolution or "人工标记为已处理", iso(), issue_id))
    _log_transition(issue, issue.status, "resolved", "manual",
                    cause_type="manual", actor=actor, snapshot=snapshot)
    return get(issue_id)


def reopen(issue_id: int, reason: Optional[str] = None,
           actor: Optional[str] = None) -> Issue:
    issue = get(issue_id)
    if issue is None:
        raise IssueError(f"issue {issue_id} not found")
    snapshot = {"resolved_at": issue.resolved_at, "resolution": issue.resolution}
    db.execute(
        "UPDATE issues SET status='open', resolved_at=NULL, updated_at=? WHERE id=?",
        (iso(), issue_id))
    _log_transition(issue, issue.status, "open", "manual",
                    cause_type="manual", actor=actor, snapshot=snapshot)
    return get(issue_id)


RULES = [rule_consecutive_anomaly]
