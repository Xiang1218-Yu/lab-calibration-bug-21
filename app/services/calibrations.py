"""Calibration records service (manual creation + querying).

Bulk structured import lives in :mod:`app.services.importer`. Both paths share
:func:`insert_calibration`, which enforces the duplicate guard and triggers
issue state transitions.
"""
from __future__ import annotations

from typing import Optional

from .. import config, db
from ..models import Calibration
from ..utils import content_hash, iso, parse_ts, to_float
from . import issues as issues_service


class CalibrationError(ValueError):
    pass


def make_hash(device_id: int, calibrated_at: str, technician: Optional[str],
              measured: Optional[float]) -> str:
    return content_hash(device_id, calibrated_at, technician or "",
                        "" if measured is None else round(measured, 6))


def insert_calibration(device_id: int, calibrated_at: str, result: str,
                       technician: Optional[str] = None,
                       measured_value: Optional[float] = None,
                       nominal_value: Optional[float] = None,
                       tolerance: Optional[float] = None,
                       unit: Optional[str] = None, notes: Optional[str] = None,
                       source: str = "manual",
                       import_row: Optional[int] = None) -> Calibration:
    """Insert one calibration. Caller has validated the device exists.

    Raises :class:`CalibrationError` on a duplicate. Returns the stored record.
    """
    chash = make_hash(device_id, calibrated_at, technician, measured_value)
    dup = db.query_one(
        "SELECT id FROM calibrations WHERE device_id=? AND content_hash=?",
        (device_id, chash))
    if dup is not None:
        raise CalibrationError("duplicate calibration record")

    new_id = db.execute(
        """INSERT INTO calibrations
           (device_id, calibrated_at, result, technician, measured_value,
            nominal_value, tolerance, unit, notes, source, import_row,
            content_hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (device_id, calibrated_at, result, technician, measured_value,
         nominal_value, tolerance, unit, notes, source, import_row, chash, iso()),
    )
    return get(new_id)


def get(cal_id: int) -> Optional[Calibration]:
    row = db.query_one("SELECT * FROM calibrations WHERE id=?", (cal_id,))
    return Calibration.from_row(row) if row else None


def create(data: dict) -> Calibration:
    device_id = data.get("device_id")
    if not isinstance(device_id, int):
        raise CalibrationError("device_id must be an integer")
    from .devices import get as get_device
    if get_device(device_id) is None:
        raise CalibrationError(f"device {device_id} not found")

    occurred = parse_ts(data.get("calibrated_at"))
    if occurred is None:
        raise CalibrationError(f"invalid calibrated_at: {data.get('calibrated_at')!r}")
    result = str(data.get("result", "")).strip().lower()
    if result not in config.CALIBRATION_RESULTS:
        raise CalibrationError(f"result must be one of {config.CALIBRATION_RESULTS}")

    try:
        measured = to_float(data.get("measured_value"))
        nominal = to_float(data.get("nominal_value"))
        tol = to_float(data.get("tolerance"))
    except ValueError as e:
        raise CalibrationError(str(e)) from e

    try:
        cal = insert_calibration(
            device_id, iso(occurred), result,
            technician=data.get("technician") or None,
            measured_value=measured, nominal_value=nominal, tolerance=tol,
            unit=data.get("unit") or None, notes=data.get("notes") or None,
            source="manual")
    except CalibrationError as e:
        raise CalibrationError(str(e)) from e
    # A new calibration may resolve / monitor / reopen the device's issue.
    issues_service.apply_calibration(cal)
    return cal


def list_calibrations(device_id: Optional[int] = None,
                      result: Optional[str] = None,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: int = 200) -> list[dict]:
    sql = """SELECT c.*, d.code AS device_code, d.name AS device_name
             FROM calibrations c JOIN devices d ON d.id = c.device_id WHERE 1=1"""
    params: list = []
    if device_id is not None:
        sql += " AND c.device_id=?"
        params.append(device_id)
    if result:
        sql += " AND c.result=?"
        params.append(result)
    if start:
        sql += " AND c.calibrated_at >= ?"
        params.append(start)
    if end:
        sql += " AND c.calibrated_at <= ?"
        params.append(end)
    sql += " ORDER BY c.calibrated_at DESC, c.id DESC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in db.query(sql, params)]
