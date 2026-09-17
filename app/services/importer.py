"""Structured calibration import.

Accepts rows (from CSV or JSON) and, for each row, validates fields, resolves the
device, detects duplicates, and inserts the calibration. Every outcome is
recorded so the caller gets a full report: accepted / duplicate / error counts
and a per-row error list. Bad rows never abort the whole import.

Expected columns (case-insensitive, aliases accepted):
    device_code, calibrated_at, result, technician,
    measured_value, nominal_value, tolerance, unit, notes
"""
from __future__ import annotations

import csv
import io
from typing import Any, Optional

from .. import config, db
from ..utils import iso, parse_ts, to_float, to_json
from . import calibrations as cal_service
from . import devices as device_service

RESULT_ALIASES = {
    "pass": "pass", "passed": "pass", "ok": "pass", "通过": "pass",
    "合格": "pass", "normal": "pass",
    "fail": "fail", "failed": "fail", "failure": "fail",
    "失败": "fail", "不合格": "fail", "error": "fail",
    "conditional": "conditional", "cond": "conditional",
    "条件通过": "conditional", "限制使用": "conditional", "有条件": "conditional",
    "conditional pass": "conditional",
}

COLUMN_ALIASES = {
    "device": "device_code", "device_code": "device_code", "devicecode": "device_code",
    "设备编号": "device_code", "设备": "device_code", "设备编码": "device_code",
    "calibrated_at": "calibrated_at", "calibration_date": "calibrated_at",
    "date": "calibrated_at", "time": "calibrated_at", "校准时间": "calibrated_at",
    "校准日期": "calibrated_at",
    "result": "result", "结果": "result", "校准结果": "result",
    "technician": "technician", "tech": "technician", "技术员": "technician",
    "校准员": "technician",
    "measured_value": "measured_value", "measured": "measured_value",
    "测量值": "measured_value", "实测值": "measured_value",
    "nominal_value": "nominal_value", "nominal": "nominal_value",
    "标称值": "nominal_value", "标准值": "nominal_value",
    "tolerance": "tolerance", "tol": "tolerance", "容差": "tolerance", "允差": "tolerance",
    "unit": "unit", "单位": "unit",
    "notes": "notes", "note": "notes", "备注": "notes", "说明": "notes",
}


class ImportError_(ValueError):
    pass


def parse_content(content: str, fmt: str = "csv") -> list[dict]:
    """Parse raw CSV/JSON text into a list of raw row dicts."""
    fmt = fmt.lower()
    if fmt == "json":
        import json
        data = json.loads(content)
        if isinstance(data, dict):
            data = data.get("rows", data.get("records", []))
        if not isinstance(data, list):
            raise ImportError_("JSON must be a list of rows or {'rows': [...]}")
        return [dict(r) for r in data]
    if fmt in ("csv", "tsv"):
        delimiter = "\t" if fmt == "tsv" else ","
        reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
        rows = []
        for r in reader:
            # Normalise keys via alias map; drop None keys (trailing columns).
            norm = {}
            for k, v in r.items():
                if k is None:
                    continue
                key = COLUMN_ALIASES.get(str(k).strip().lower(),
                                        COLUMN_ALIASES.get(str(k).strip(), str(k).strip()))
                norm[key] = v
            rows.append(norm)
        return rows
    raise ImportError_(f"unsupported format {fmt!r}; use csv or json")


def _norm_result(value: Any) -> str:
    key = str(value or "").strip().lower()
    return RESULT_ALIASES.get(key, key)


def validate_row(row: dict) -> tuple[Optional[dict], list[str]]:
    """Return (clean_row, errors). errors empty => valid."""
    errors: list[str] = []
    code = str(row.get("device_code", "")).strip()
    if not code:
        errors.append("device_code is required")

    ts_raw = row.get("calibrated_at")
    occurred = parse_ts(ts_raw)
    if occurred is None:
        errors.append(f"invalid or missing calibrated_at: {ts_raw!r}")

    result = _norm_result(row.get("result"))
    if result not in config.CALIBRATION_RESULTS:
        errors.append(f"result must be one of {config.CALIBRATION_RESULTS}, got {row.get('result')!r}")

    measured = nominal = tol = None
    for field, target in (("measured_value", "measured"), ("nominal_value", "nominal"),
                          ("tolerance", "tol")):
        try:
            val = to_float(row.get(field))
        except ValueError:
            errors.append(f"{field} must be a number, got {row.get(field)!r}")
            val = None
        if target == "measured":
            measured = val
        elif target == "nominal":
            nominal = val
        else:
            tol = val

    if not errors:
        clean = {
            "device_code": code,
            "calibrated_at": iso(occurred),
            "result": result,
            "technician": (str(row["technician"]).strip() if row.get("technician") else None),
            "measured_value": measured,
            "nominal_value": nominal,
            "tolerance": tol,
            "unit": (str(row["unit"]).strip() if row.get("unit") else None),
            "notes": (str(row["notes"]).strip() if row.get("notes") else None),
        }
        return clean, []
    return None, errors


def create_import_job(filename: Optional[str], total_rows: int) -> int:
    return db.execute(
        """INSERT INTO import_jobs (filename, status, total_rows, created_at)
           VALUES (?, 'processing', ?, ?)""",
        (filename, total_rows, iso()))


def finish_import_job(job_id: int, status: str, summary: dict, errors: list) -> None:
    db.execute(
        """UPDATE import_jobs SET status=?, accepted_rows=?, duplicate_rows=?,
           error_rows=?, summary_json=?, errors_json=?, finished_at=? WHERE id=?""",
        (status, summary["accepted"], summary["duplicate"], summary["errors"],
         to_json(summary), to_json(errors), iso(), job_id))


def process_rows(rows: list[dict], import_job_id: int,
                 auto_create_devices: bool = False) -> dict:
    """Validate + insert rows. Returns a summary dict and writes per-row errors.

    Each accepted calibration triggers issue state transitions via the shared
    calibration service.
    """
    source = f"import:{import_job_id}"
    errors: list[dict] = []
    accepted = duplicate = error_count = 0
    transitions: list[dict] = []
    # Intra-file duplicate guard: same normalized row twice in one file.
    seen_in_file: set = set()

    for idx, raw in enumerate(rows, start=1):
        clean, row_errors = validate_row(raw)
        if row_errors:
            error_count += 1
            errors.append({"row": idx, "errors": row_errors, "raw": _safe_raw(raw)})
            continue

        device = device_service.get_by_code(clean["device_code"])
        if device is None:
            if auto_create_devices:
                device = device_service.create({
                    "code": clean["device_code"],
                    "name": clean["device_code"],
                })
            else:
                error_count += 1
                errors.append({
                    "row": idx,
                    "errors": [f"unknown device_code {clean['device_code']!r} "
                               f"(create the device first or enable auto-create)"],
                    "raw": _safe_raw(raw),
                })
                continue

        dedup_key = (device.id, clean["calibrated_at"],
                     clean["technician"] or "",
                     "" if clean["measured_value"] is None else round(clean["measured_value"], 6))
        if dedup_key in seen_in_file:
            duplicate += 1
            errors.append({"row": idx, "duplicate": True,
                           "errors": ["duplicate row within the same file"],
                           "raw": _safe_raw(raw)})
            continue
        seen_in_file.add(dedup_key)

        try:
            cal = cal_service.insert_calibration(
                device.id, clean["calibrated_at"], clean["result"],
                technician=clean["technician"],
                measured_value=clean["measured_value"],
                nominal_value=clean["nominal_value"],
                tolerance=clean["tolerance"],
                unit=clean["unit"], notes=clean["notes"],
                source=source, import_row=idx)
        except cal_service.CalibrationError as e:
            duplicate += 1
            errors.append({"row": idx, "duplicate": True,
                           "errors": [str(e)], "raw": _safe_raw(raw)})
            continue

        accepted += 1
        # Record the issue transition this calibration caused.
        from . import issues as issues_service
        trans = issues_service.apply_calibration(cal)
        if trans.get("action") not in (None, "none", "noop"):
            transitions.append({"row": idx, **trans})

    summary = {
        "total": len(rows),
        "accepted": accepted,
        "duplicate": duplicate,
        "errors": error_count,
        "transitions": transitions,
    }
    finish_import_job(import_job_id, "done", summary, errors)
    return {"summary": summary, "errors": errors, "import_job_id": import_job_id}


def _safe_raw(raw: dict) -> dict:
    return {k: (v if v is None else str(v)) for k, v in raw.items()}


def get_import_job(job_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM import_jobs WHERE id=?", (job_id,))
    if not row:
        return None
    d = dict(row)
    from ..utils import from_json
    d["summary"] = from_json(d.pop("summary_json", None), None)
    d["errors"] = from_json(d.pop("errors_json", None), [])
    return d


def list_import_jobs(limit: int = 50) -> list[dict]:
    rows = db.query("SELECT * FROM import_jobs ORDER BY id DESC LIMIT ?", (int(limit),))
    out = []
    for r in rows:
        d = dict(r)
        from ..utils import from_json
        d.pop("summary_json", None)
        d.pop("errors_json", None)
        out.append(d)
    return out
