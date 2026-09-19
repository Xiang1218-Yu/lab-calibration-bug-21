"""Domain models.

Dataclasses mirror the database rows and centralise the vocabulary the whole
system shares (result / severity / issue status). Services return these; the
API layer serialises them via ``to_dict``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from . import config
from .utils import from_json


@dataclass
class Device:
    code: str
    name: str
    device_type: str = "generic"
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    location: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    is_active: bool = True
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metadata"] = self.metadata
        d["is_active"] = bool(self.is_active)
        return d

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Device":
        return cls(
            id=r["id"],
            code=r["code"],
            name=r["name"],
            device_type=r["device_type"],
            manufacturer=r["manufacturer"],
            model=r["model"],
            serial_number=r["serial_number"],
            location=r["location"],
            metadata=from_json(r["metadata_json"], {}),
            is_active=bool(r["is_active"]),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )


@dataclass
class Calibration:
    device_id: int
    calibrated_at: str
    result: str
    technician: Optional[str] = None
    measured_value: Optional[float] = None
    nominal_value: Optional[float] = None
    tolerance: Optional[float] = None
    unit: Optional[str] = None
    notes: Optional[str] = None
    source: str = "manual"
    import_row: Optional[int] = None
    import_job_id: Optional[int] = None
    import_attempt_id: Optional[int] = None
    content_hash: str = ""
    id: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Calibration":
        return cls(
            id=r["id"], device_id=r["device_id"], calibrated_at=r["calibrated_at"],
            result=r["result"], technician=r["technician"],
            measured_value=r["measured_value"], nominal_value=r["nominal_value"],
            tolerance=r["tolerance"], unit=r["unit"], notes=r["notes"],
            source=r["source"], import_row=r["import_row"],
            import_job_id=r["import_job_id"] if "import_job_id" in r.keys() else None,
            import_attempt_id=(r["import_attempt_id"]
                               if "import_attempt_id" in r.keys() else None),
            content_hash=r["content_hash"], created_at=r["created_at"],
        )


@dataclass
class Event:
    device_id: int
    occurred_at: str
    severity: str
    message: str
    code: Optional[str] = None
    source: str = "manual"
    id: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Event":
        return cls(
            id=r["id"], device_id=r["device_id"], occurred_at=r["occurred_at"],
            severity=r["severity"], message=r["message"], code=r["code"],
            source=r["source"], created_at=r["created_at"],
        )


@dataclass
class Issue:
    device_id: int
    title: str
    rule: str
    status: str = config.ISSUE_OPEN
    severity: str = "warning"
    description: Optional[str] = None
    trigger_event_id: Optional[int] = None
    first_event_at: Optional[str] = None
    last_event_at: Optional[str] = None
    event_count: int = 0
    resolved_at: Optional[str] = None
    resolution: Optional[str] = None
    id: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Issue":
        return cls(
            id=r["id"], device_id=r["device_id"], status=r["status"],
            severity=r["severity"], title=r["title"], description=r["description"],
            rule=r["rule"], trigger_event_id=r["trigger_event_id"],
            first_event_at=r["first_event_at"], last_event_at=r["last_event_at"],
            event_count=r["event_count"], resolved_at=r["resolved_at"],
            resolution=r["resolution"], created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
