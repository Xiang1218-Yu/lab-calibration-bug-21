"""Run events service + querying."""
from __future__ import annotations

from typing import Optional

from .. import config, db
from ..models import Event
from ..utils import iso, parse_ts
from . import issues as issues_service


class EventError(ValueError):
    pass


def _normalize_severity(value: Optional[str]) -> str:
    sev = str(value or "info").strip().lower()
    aliases = {"warn": "warning", "error": "critical", "fatal": "critical",
               "critical": "critical", "warning": "warning", "info": "info"}
    sev = aliases.get(sev, sev)
    if sev not in config.SEVERITY_ORDER:
        raise EventError(f"invalid severity {value!r}; expected one of {config.SEVERITY_ORDER}")
    return sev


def create(data: dict, run_rules: bool = True) -> Event:
    device_id = data.get("device_id")
    if device_id is None:
        raise EventError("device_id is required")
    if not isinstance(device_id, int):
        raise EventError("device_id must be an integer")
    message = str(data.get("message", "")).strip()
    if not message:
        raise EventError("message is required")
    occurred = parse_ts(data.get("occurred_at"))
    if occurred is None:
        raise EventError(f"invalid occurred_at timestamp: {data.get('occurred_at')!r}")
    severity = _normalize_severity(data.get("severity"))
    code = data.get("code") or None
    source = data.get("source") or "manual"

    new_id = db.execute(
        """INSERT INTO events (device_id, occurred_at, severity, code, message, source, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (device_id, iso(occurred), severity, code, message, source, iso()),
    )
    event = Event(
        id=new_id, device_id=device_id, occurred_at=iso(occurred),
        severity=severity, message=message, code=code, source=source,
        created_at=iso(),
    )
    if run_rules:
        issues_service.evaluate_event(event)
    return event


def list_events(device_id: Optional[int] = None, severity: Optional[str] = None,
                start: Optional[str] = None, end: Optional[str] = None,
                limit: int = 200) -> list[dict]:
    sql = """SELECT e.*, d.code AS device_code, d.name AS device_name
             FROM events e JOIN devices d ON d.id = e.device_id WHERE 1=1"""
    params: list = []
    if device_id is not None:
        sql += " AND e.device_id=?"
        params.append(device_id)
    if severity:
        sql += " AND e.severity=?"
        params.append(severity)
    if start:
        sql += " AND e.occurred_at >= ?"
        params.append(start)
    if end:
        sql += " AND e.occurred_at <= ?"
        params.append(end)
    sql += " ORDER BY e.occurred_at DESC, e.id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in db.query(sql, params)]
