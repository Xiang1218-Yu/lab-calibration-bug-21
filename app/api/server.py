"""HTTP API + static file server (Python standard library only).

Run with ``python -m app.main``. Serves the JSON API under ``/api`` and the
static frontend from ``web/`` at ``/``.
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Any, Callable, Optional

from .. import config, db
from ..services import (
    attachments as attachment_service,
    auth,
    calibrations as cal_service,
    compensation as comp_service,
    devices as device_service,
    events as event_service,
    importer,
    issues as issue_service,
    notifications as notification_service,
    undo as undo_service,
)
from ..tasks import import_task  # noqa: F401  (registers the import task)
from ..tasks import runner as task_runner
from ..utils import from_json, iso, to_json
from . import seed

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# -- routing -----------------------------------------------------------------

# Each route: (method, regex, handler)
_ROUTES: list[tuple[str, re.Pattern, Callable]] = []


def route(method: str, pattern: str):
    rx = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        _ROUTES.append((method.upper(), rx, fn))
        return fn
    return deco


class Ctx:
    def __init__(self, handler: "Handler", match: dict, query: dict, body: bytes):
        self.h = handler
        self.params = match
        self.query = query
        self._body = body
        self.headers = handler.headers

    def q(self, name: str, default: Optional[str] = None) -> Optional[str]:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def qint(self, name: str, default: Optional[int] = None) -> Optional[int]:
        v = self.q(name)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except ValueError:
            raise ApiError(400, f"query param {name} must be an integer")

    def qbool(self, name: str, default: bool = False) -> bool:
        v = self.q(name)
        if v is None:
            return default
        return v.lower() in ("1", "true", "yes", "on")

    def json(self) -> dict:
        if not self._body:
            return {}
        try:
            data = json.loads(self._body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ApiError(400, f"invalid JSON body: {e}")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    def raw_text(self) -> str:
        return self._body.decode("utf-8-sig", errors="replace")

    def int_param(self, name: str) -> int:
        try:
            return int(self.params[name])
        except (KeyError, ValueError):
            raise ApiError(400, f"invalid {name} in path")


# -- handlers -----------------------------------------------------------------

@route("GET", "/api/health")
def health(c: Ctx):
    return {"status": "ok", "version": config.__dict__.get("__version__", "1.0.0"),
            "time": iso()}


@route("GET", "/api/dashboard")
def dashboard(c: Ctx):
    def scalar(sql):
        return db.query_one(sql)[0]
    return {
        "devices": scalar("SELECT COUNT(*) FROM devices"),
        "active_devices": scalar("SELECT COUNT(*) FROM devices WHERE is_active=1"),
        "calibrations": scalar("SELECT COUNT(*) FROM calibrations"),
        "events": scalar("SELECT COUNT(*) FROM events"),
        "open_issues": scalar("SELECT COUNT(*) FROM issues WHERE status!='resolved'"),
        "failed_calibrations": scalar("SELECT COUNT(*) FROM calibrations WHERE result='fail'"),
        "critical_events": scalar("SELECT COUNT(*) FROM events WHERE severity='critical'"),
        "recent_issues": [i.to_dict() for i in issue_service.list_issues()][:10],
        "jobs_running": scalar("SELECT COUNT(*) FROM jobs WHERE status='running'"),
    }


# devices
@route("GET", "/api/devices")
def list_devices(c: Ctx):
    devs = device_service.list_devices(
        active_only=c.qbool("active_only"), q=c.q("q"))
    out = []
    for d in devs:
        dct = d.to_dict()
        dct["summary"] = device_service.device_summary(d.id)
        out.append(dct)
    return out


@route("POST", "/api/devices")
def create_device(c: Ctx):
    try:
        d = device_service.create(c.json())
    except device_service.DeviceError as e:
        raise ApiError(400, str(e))
    return d.to_dict()


@route("GET", "/api/devices/{id}")
def get_device(c: Ctx):
    dev = device_service.get(c.int_param("id"))
    if not dev:
        raise ApiError(404, "device not found")
    dct = dev.to_dict()
    dct["summary"] = device_service.device_summary(dev.id)
    return dct


@route("PATCH", "/api/devices/{id}")
def update_device(c: Ctx):
    try:
        d = device_service.update(c.int_param("id"), c.json())
    except device_service.DeviceError as e:
        raise ApiError(404 if "not found" in str(e) else 400, str(e))
    return d.to_dict()


@route("POST", "/api/devices/{id}/active")
def set_device_active(c: Ctx):
    body = c.json()
    try:
        d = device_service.set_active(c.int_param("id"), bool(body.get("is_active", True)))
    except device_service.DeviceError as e:
        raise ApiError(404, str(e))
    return d.to_dict()


# calibrations
@route("GET", "/api/calibrations")
def list_calibrations(c: Ctx):
    return cal_service.list_calibrations(
        device_id=c.qint("device_id"), result=c.q("result"),
        start=c.q("start"), end=c.q("end"), limit=c.qint("limit", 200))


@route("POST", "/api/calibrations")
def create_calibration(c: Ctx):
    try:
        cal = cal_service.create(c.json())
    except cal_service.CalibrationError as e:
        raise ApiError(400, str(e))
    return cal.to_dict()


# events
@route("GET", "/api/events")
def list_events(c: Ctx):
    return event_service.list_events(
        device_id=c.qint("device_id"), severity=c.q("severity"),
        start=c.q("start"), end=c.q("end"), limit=c.qint("limit", 200))


@route("POST", "/api/events")
def create_event(c: Ctx):
    try:
        ev = event_service.create(c.json())
    except event_service.EventError as e:
        raise ApiError(400, str(e))
    return ev.to_dict()


# issues
@route("GET", "/api/issues")
def list_issues(c: Ctx):
    issues = issue_service.list_issues(
        device_id=c.qint("device_id"), status=c.q("status"),
        severity=c.q("severity"))
    out = []
    for i in issues:
        dct = i.to_dict()
        dev = device_service.get(i.device_id)
        dct["device_code"] = dev.code if dev else None
        dct["device_name"] = dev.name if dev else None
        out.append(dct)
    return out


@route("GET", "/api/issues/{id}")
def get_issue(c: Ctx):
    issue = issue_service.get(c.int_param("id"))
    if not issue:
        raise ApiError(404, "issue not found")
    dct = issue.to_dict()
    dct["events"] = issue_service.linked_events(issue.id)
    dev = device_service.get(issue.device_id)
    dct["device_code"] = dev.code if dev else None
    return dct


@route("POST", "/api/issues/{id}/resolve")
def resolve_issue(c: Ctx):
    body = c.json()
    try:
        i = issue_service.resolve(c.int_param("id"), body.get("resolution"))
    except issue_service.IssueError as e:
        raise ApiError(404, str(e))
    return i.to_dict()


@route("POST", "/api/issues/{id}/reopen")
def reopen_issue(c: Ctx):
    body = c.json()
    try:
        i = issue_service.reopen(c.int_param("id"), body.get("reason"))
    except issue_service.IssueError as e:
        raise ApiError(404, str(e))
    return i.to_dict()


# timeline (merged events + calibrations for one device)
@route("GET", "/api/timeline/{device_id}")
def device_timeline(c: Ctx):
    device_id = c.int_param("device_id")
    if not device_service.get(device_id):
        raise ApiError(404, "device not found")
    items = []
    for e in event_service.list_events(device_id=device_id,
                                       severity=c.q("severity"),
                                       start=c.q("start"), end=c.q("end"), limit=1000):
        items.append({"kind": "event", "at": e["occurred_at"], **e})
    for cal in cal_service.list_calibrations(device_id=device_id,
                                             result=c.q("cal_result"),
                                             start=c.q("start"), end=c.q("end"), limit=1000):
        items.append({"kind": "calibration", "at": cal["calibrated_at"], **cal})
    items.sort(key=lambda x: (x["at"], x.get("id", 0)), reverse=True)
    return items[: c.qint("limit", 500)]


# imports
@route("POST", "/api/imports")
def create_import(c: Ctx):
    fmt = (c.q("fmt") or "csv").lower()
    if fmt not in ("csv", "json", "tsv"):
        raise ApiError(400, "fmt must be csv, tsv or json")
    auto_create = c.qbool("auto_create")
    filename = c.q("filename") or f"import.{fmt}"
    content = c.raw_text()
    if not content.strip():
        raise ApiError(400, "empty import body")

    # Validate it parses before enqueueing (cheap fail-fast).
    try:
        rows = importer.parse_content(content, fmt=fmt)
    except importer.ImportError_ as e:
        raise ApiError(400, str(e))

    job_id = importer.create_import_job(filename, len(rows))
    # Persist the upload to a scratch file the worker reads.
    data_dir = config.DATA_DIR / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"import_{job_id}.{fmt}"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)

    if c.qbool("sync"):
        # Process inline (handy for tests / no-worker setups).
        from ..tasks.import_task import run_import
        princ = auth.principal(c.headers)
        summary = run_import(
            {"import_job_id": job_id, "path": str(path), "fmt": fmt,
             "auto_create_devices": auto_create, "actor": princ.actor},
            _InlineCtx())
        return importer.get_import_job(job_id) | {"sync": True, "summary": summary}

    idem = f"import:{job_id}"
    task_runner.enqueue(
        "import_calibrations",
        {"import_job_id": job_id, "path": str(path), "fmt": fmt,
         "auto_create_devices": auto_create,
         "actor": (c.headers.get("X-Actor") or c.headers.get("x-actor"))},
        idempotency_key=idem)
    return importer.get_import_job(job_id)


@route("GET", "/api/imports")
def list_imports(c: Ctx):
    return importer.list_import_jobs(limit=c.qint("limit", 50))


@route("GET", "/api/imports/{id}")
def get_import(c: Ctx):
    job = importer.get_import_job(c.int_param("id"))
    if not job:
        raise ApiError(404, "import job not found")
    return job


# -- import undo / compensation ---------------------------------------------

@route("POST", "/api/imports/{id}/undo/preview")
def undo_preview(c: Ctx):
    princ = auth.principal(c.headers)
    auth.require(princ, "import:undo")
    job_id = c.int_param("id")
    body = c.json()
    try:
        plan = undo_service.preview(
            job_id, requested_by=princ.actor, reason=body.get("reason"),
            force=c.qbool("force"))
    except undo_service.UndoError as e:
        raise ApiError(400, str(e))
    except undo_service.UndoConflict as e:
        raise ApiError(409, str(e))
    return plan


@route("POST", "/api/imports/{id}/undo/commit")
def undo_commit(c: Ctx):
    princ = auth.principal(c.headers)
    auth.require(princ, "import:undo")
    job_id = c.int_param("id")
    body = c.json()
    if body.get("confirm") is not True:
        raise ApiError(400, 'must pass {"confirm": true} to execute an undo')
    token = body.get("confirm_token")
    if not token:
        raise ApiError(400, "confirm_token from preview is required")
    idem = c.headers.get("X-Idempotency-Key") or c.headers.get("x-idempotency-key")
    try:
        result = undo_service.commit(
            job_id, token, requested_by=princ.actor,
            expected_fingerprint=body.get("fingerprint"),
            idem_key=idem, force=body.get("force") is True)
    except undo_service.UndoError as e:
        raise ApiError(400, str(e))
    except undo_service.UndoConflict as e:
        raise ApiError(409, str(e))
    undo_service.set_confirmed_at(result["undo_batch_id"])
    return result


@route("GET", "/api/imports/{id}/undo")
def undo_status(c: Ctx):
    st = undo_service.status(c.int_param("id"))
    if st is None:
        raise ApiError(404, "no undo for this import batch")
    return st


@route("GET", "/api/compensations")
def list_compensations(c: Ctx):
    auth.require(auth.principal(c.headers), "read")
    return comp_service.list_items(
        status=c.q("status", "pending"),
        import_job_id=c.qint("import_job_id"), kind=c.q("kind"))


@route("POST", "/api/compensations/{id}/resolve")
def resolve_compensation(c: Ctx):
    princ = auth.principal(c.headers)
    auth.require(princ, "compensation:resolve")
    body = c.json()
    try:
        return comp_service.resolve(
            c.int_param("id"),
            resolution=body.get("resolution") or body.get("note") or "人工处理完成",
            resolved_by=princ.actor,
            new_status="ignored" if body.get("ignore") else "resolved")
    except comp_service.CompensationError as e:
        raise ApiError(404 if "not found" in str(e) else 400, str(e))


@route("GET", "/api/notifications")
def list_notifications(c: Ctx):
    return notification_service.list_notifications(
        status=c.q("status"), import_job_id=c.qint("import_job_id"),
        limit=c.qint("limit", 200))


@route("POST", "/api/notifications/{id}/read")
def read_notification(c: Ctx):
    n = notification_service.get(c.int_param("id"))
    if not n:
        raise ApiError(404, "notification not found")
    return notification_service.mark_read(n["id"])


@route("GET", "/api/issues/{id}/transitions")
def issue_transitions(c: Ctx):
    issue_id = c.int_param("id")
    if not issue_service.get(issue_id):
        raise ApiError(404, "issue not found")
    rows = db.query(
        "SELECT * FROM issue_transitions WHERE issue_id=? ORDER BY id", (issue_id,))
    return [dict(r) for r in rows]


@route("POST", "/api/imports/{id}/attachments")
def add_import_attachment(c: Ctx):
    princ = auth.principal(c.headers)
    auth.require(princ, "import:create")
    job_id = c.int_param("id")
    if not importer.get_import_job(job_id):
        raise ApiError(404, "import job not found")
    filename = c.headers.get("X-Filename") or c.headers.get("x-filename") or "attachment.bin"
    body = c._body
    if not body:
        raise ApiError(400, "empty attachment body")
    a = attachment_service.register(
        attachable_type="import_job", attachable_id=job_id, filename=filename,
        content=body, source=f"import:{job_id}", import_job_id=job_id,
        created_by=princ.actor)
    return a


@route("GET", "/api/imports/{id}/attachments")
def list_import_attachments(c: Ctx):
    if not importer.get_import_job(c.int_param("id")):
        raise ApiError(404, "import job not found")
    return attachment_service.list_for_batch(c.int_param("id"))


# background jobs
@route("GET", "/api/jobs")
def list_jobs(c: Ctx):
    rows = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ?",
                    (c.qint("limit", 100),))
    out = []
    for r in rows:
        d = dict(r)
        d.pop("payload_json", None)
        out.append(d)
    return out


@route("GET", "/api/jobs/{id}")
def get_job(c: Ctx):
    r = db.query_one("SELECT * FROM jobs WHERE id=?", (c.int_param("id"),))
    if not r:
        raise ApiError(404, "job not found")
    d = dict(r)
    d["payload"] = from_json(d.pop("payload_json"), {})
    d["result"] = from_json(d.pop("result_json"), None)
    return d


class _InlineCtx:
    """Minimal stand-in for JobContext for synchronous (sync=true) imports."""
    job_id = None

    def heartbeat(self):
        return True

    @property
    def cancelled(self):
        return False


# -- HTTP plumbing ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CalTrack/1.0"

    def log_message(self, fmt, *args):  # quiet by default
        pass

    def _send_json(self, status: int, payload: Any):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str):
        if path == "/" or path == "":
            path = "/index.html"
        # Prevent path traversal.
        rel = path.lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        try:
            target.relative_to(config.WEB_DIR.resolve())
        except ValueError:
            return self._send_json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._send_json(404, {"error": "not found"})
        ext = target.suffix.lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)

        if not path.startswith("/api/"):
            if method == "GET":
                return self._serve_static(path)
            return self._send_json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        for m, rx, fn in _ROUTES:
            if m != method:
                continue
            match = rx.match(path)
            if match:
                ctx = Ctx(self, match.groupdict(), query, body)
                try:
                    result = fn(ctx)
                except ApiError as e:
                    return self._send_json(e.status, {"error": e.message})
                except auth.AuthorizationError as e:
                    return self._send_json(e.status, {"error": e.message})
                except Exception as e:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    return self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
                return self._send_json(200, {"data": result})
        return self._send_json(404, {"error": f"no route for {method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")


def create_server(host: Optional[str] = None, port: Optional[int] = None,
                  start_worker: bool = True) -> ThreadingHTTPServer:
    db.init_db()
    seed.ensure_seed_data()
    if start_worker:
        task_runner.start_worker()
    httpd = ThreadingHTTPServer((host or config.HOST, port or config.PORT), Handler)
    return httpd
