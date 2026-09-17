"""Device profile service."""
from __future__ import annotations

from typing import Optional

from .. import db
from ..models import Device
from ..utils import iso


class DeviceError(ValueError):
    pass


def _validate(data: dict) -> dict:
    code = str(data.get("code", "")).strip()
    name = str(data.get("name", "")).strip()
    if not code:
        raise DeviceError("device code is required")
    if not name:
        raise DeviceError("device name is required")
    device_type = str(data.get("device_type", "generic")).strip() or "generic"
    return {
        "code": code,
        "name": name,
        "device_type": device_type,
        "manufacturer": (data.get("manufacturer") or None),
        "model": (data.get("model") or None),
        "serial_number": (data.get("serial_number") or None),
        "location": (data.get("location") or None),
        "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    }


def create(data: dict) -> Device:
    fields = _validate(data)
    now = iso()
    from ..utils import to_json
    try:
        new_id = db.execute(
            """INSERT INTO devices
               (code, name, device_type, manufacturer, model, serial_number,
                location, metadata_json, is_active, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
            (fields["code"], fields["name"], fields["device_type"],
             fields["manufacturer"], fields["model"], fields["serial_number"],
             fields["location"], to_json(fields["metadata"]), now, now),
        )
    except Exception as e:  # UNIQUE constraint
        raise DeviceError(f"device code {fields['code']!r} already exists") from e
    return get(new_id)


def update(device_id: int, data: dict) -> Device:
    dev = get(device_id)
    if dev is None:
        raise DeviceError(f"device {device_id} not found")
    merged = {
        "code": data.get("code", dev.code),
        "name": data.get("name", dev.name),
        "device_type": data.get("device_type", dev.device_type),
        "manufacturer": data.get("manufacturer", dev.manufacturer),
        "model": data.get("model", dev.model),
        "serial_number": data.get("serial_number", dev.serial_number),
        "location": data.get("location", dev.location),
        "metadata": data.get("metadata", dev.metadata),
    }
    fields = _validate(merged)
    from ..utils import to_json
    now = iso()
    db.execute(
        """UPDATE devices SET code=?, name=?, device_type=?, manufacturer=?,
           model=?, serial_number=?, location=?, metadata_json=?, updated_at=?
           WHERE id=?""",
        (fields["code"], fields["name"], fields["device_type"],
         fields["manufacturer"], fields["model"], fields["serial_number"],
         fields["location"], to_json(fields["metadata"]), now, device_id),
    )
    return get(device_id)


def set_active(device_id: int, active: bool) -> Device:
    if get(device_id) is None:
        raise DeviceError(f"device {device_id} not found")
    db.execute("UPDATE devices SET is_active=?, updated_at=? WHERE id=?",
               (1 if active else 0, iso(), device_id))
    return get(device_id)


def get(device_id: int) -> Optional[Device]:
    row = db.query_one("SELECT * FROM devices WHERE id=?", (device_id,))
    return Device.from_row(row) if row else None


def get_by_code(code: str) -> Optional[Device]:
    row = db.query_one("SELECT * FROM devices WHERE code=? ", (code.strip(),))
    return Device.from_row(row) if row else None


def list_devices(active_only: bool = False, q: Optional[str] = None) -> list[Device]:
    sql = "SELECT * FROM devices WHERE 1=1"
    params: list = []
    if active_only:
        sql += " AND is_active=1"
    if q:
        like = f"%{q.strip()}%"
        sql += " AND (code LIKE ? OR name LIKE ? OR location LIKE ?)"
        params += [like, like, like]
    sql += " ORDER BY code"
    return [Device.from_row(r) for r in db.query(sql, params)]


def device_summary(device_id: int) -> dict:
    """Counts used by the device detail page."""
    cal = db.query_one(
        "SELECT COUNT(*) c, MAX(calibrated_at) latest FROM calibrations WHERE device_id=?",
        (device_id,))
    evt = db.query_one(
        "SELECT COUNT(*) c, MAX(occurred_at) latest FROM events WHERE device_id=?",
        (device_id,))
    open_issues = db.query_one(
        "SELECT COUNT(*) c FROM issues WHERE device_id=? AND status != 'resolved'",
        (device_id,))
    last_cal = db.query_one(
        "SELECT result FROM calibrations WHERE device_id=? ORDER BY calibrated_at DESC, id DESC LIMIT 1",
        (device_id,))
    return {
        "calibration_count": cal["c"],
        "last_calibration_at": cal["latest"],
        "event_count": evt["c"],
        "last_event_at": evt["latest"],
        "open_issue_count": open_issues["c"],
        "last_calibration_result": last_cal["result"] if last_cal else None,
    }
