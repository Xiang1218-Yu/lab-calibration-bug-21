"""Seed a small but realistic demo dataset on first run.

Idempotent: only runs when the devices table is empty. Uses the service layer so
that issue-generation and calibration-driven state transitions are exercised the
same way real writes are.
"""
from __future__ import annotations

from .. import db
from ..services import devices as device_service
from ..services import events as event_service
from ..services import calibrations as cal_service
from ..utils import iso, parse_ts


def _has_data() -> bool:
    return db.query_one("SELECT COUNT(*) c FROM devices")["c"] > 0


def ensure_seed_data(force: bool = False) -> bool:
    if not force and _has_data():
        return False

    bal = device_service.create({
        "code": "BAL-001", "name": "分析天平 #1", "device_type": "balance",
        "manufacturer": "Mettler", "model": "XS205", "location": "称量室 A",
    })
    ph = device_service.create({
        "code": "PH-014", "name": "pH 计 #14", "device_type": "ph_meter",
        "manufacturer": "Hanna", "model": "HI5522", "location": "理化室",
    })
    oven = device_service.create({
        "code": "OVN-007", "name": "干燥箱 #7", "device_type": "oven",
        "manufacturer": "Memmert", "model": "UF110", "location": "前处理室",
    })
    inactive = device_service.create({
        "code": "BAL-002", "name": "分析天平 #2 (停用)", "device_type": "balance",
        "manufacturer": "Sartorius", "model": "CPA225D", "location": "称量室 A",
    })
    device_service.set_active(inactive.id, False)

    # BAL-001: healthy history, recent pass.
    cal_service.create({"device_id": bal.id, "calibrated_at": "2026-08-01 09:00",
                        "result": "pass", "technician": "张工",
                        "measured_value": 200.001, "nominal_value": 200.0,
                        "tolerance": 0.005, "unit": "g"})
    cal_service.create({"device_id": bal.id, "calibrated_at": "2026-08-15 09:30",
                        "result": "pass", "technician": "张工",
                        "measured_value": 200.002, "nominal_value": 200.0,
                        "tolerance": 0.005, "unit": "g"})
    cal_service.create({"device_id": bal.id, "calibrated_at": "2026-09-01 10:00",
                        "result": "conditional", "technician": "李工",
                        "measured_value": 200.004, "nominal_value": 200.0,
                        "tolerance": 0.005, "unit": "g",
                        "notes": "接近允差上限，限制使用并复测"})

    # PH-014: a cluster of abnormal events within 24h -> auto issue, then a
    # failing calibration keeps the issue open.
    event_service.create({"device_id": ph.id, "occurred_at": "2026-09-05 08:10",
                          "severity": "info", "code": "E_BOOT",
                          "message": "设备开机自检通过"})
    event_service.create({"device_id": ph.id, "occurred_at": "2026-09-06 10:00",
                          "severity": "warning", "code": "E_DRIFT",
                          "message": "电极漂移超阈值 (0.12 pH)"})
    event_service.create({"device_id": ph.id, "occurred_at": "2026-09-06 16:00",
                          "severity": "warning", "code": "E_DRIFT",
                          "message": "电极漂移持续 (0.15 pH)"})
    event_service.create({"device_id": ph.id, "occurred_at": "2026-09-07 09:05",
                          "severity": "critical", "code": "E_SLOPE",
                          "message": "斜率异常，校准失败风险高"})
    cal_service.create({"device_id": ph.id, "calibrated_at": "2026-09-07 10:00",
                        "result": "fail", "technician": "王工",
                        "measured_value": 7.21, "nominal_value": 7.00,
                        "tolerance": 0.05, "unit": "pH",
                        "notes": "三点校准失败，问题保持打开"})

    # OVN-007: three warnings within 24h open an issue; a conditional result
    # moves it to monitoring and a later pass resolves it.
    event_service.create({"device_id": oven.id, "occurred_at": "2026-08-22 08:00",
                          "severity": "warning", "code": "E_OVERHEAT",
                          "message": "腔体温差 +6°C"})
    event_service.create({"device_id": oven.id, "occurred_at": "2026-08-22 14:00",
                          "severity": "warning", "code": "E_OVERHEAT",
                          "message": "腔体温差 +5°C"})
    event_service.create({"device_id": oven.id, "occurred_at": "2026-08-23 08:00",
                          "severity": "warning", "code": "E_OVERHEAT",
                          "message": "腔体温差 +4°C"})
    cal_service.create({"device_id": oven.id, "calibrated_at": "2026-08-23 09:00",
                        "result": "conditional", "technician": "赵工",
                        "measured_value": 104.0, "nominal_value": 105.0,
                        "tolerance": 2.0, "unit": "°C",
                        "notes": "温控偏差改善，转入观察"})
    cal_service.create({"device_id": oven.id, "calibrated_at": "2026-08-28 09:00",
                        "result": "pass", "technician": "赵工",
                        "measured_value": 105.1, "nominal_value": 105.0,
                        "tolerance": 2.0, "unit": "°C",
                        "notes": "复测通过，问题自动关闭"})

    return True
