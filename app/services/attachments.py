"""Attachment service.

Attachments can hang off an import batch (``attachable_type='import_job'``) or a
single calibration (``attachable_type='calibration'``). Every batch-originated
attachment carries ``import_job_id``/``import_attempt_id`` so an undo can find
them regardless of what they are attached to.

Deleting an attachment is a destructive, irreversible action, so undo never
hard-deletes: it marks the row ``revoked`` and moves the underlying file into a
quarantine directory (recoverable). Manual attachments (``source='manual'``)
are never touched.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

from .. import config, db
from ..utils import iso

QUARANTINE_DIRNAME = "quarantine"


class AttachmentError(ValueError):
    pass


def register(*, attachable_type: str, attachable_id: int, filename: str,
             path: Optional[str] = None, content: Optional[bytes] = None,
             source: str = "manual", import_job_id: Optional[int] = None,
             import_attempt_id: Optional[int] = None,
             created_by: Optional[str] = None) -> dict:
    """Record an attachment. When ``content`` is given, persist it to the
    uploads dir and hash it. Returns the stored row as a dict.
    """
    if attachable_type not in ("import_job", "calibration"):
        raise AttachmentError(f"bad attachable_type {attachable_type!r}")

    digest = size = None
    stored_path: Optional[str] = path
    if content is not None:
        digest = hashlib.sha256(content).hexdigest()
        size = len(content)
        uploads = config.DATA_DIR / "attachments" / str(import_job_id or "manual")
        uploads.mkdir(parents=True, exist_ok=True)
        safe = os.path.basename(filename) or "attachment.bin"
        target = uploads / f"{digest[:12]}_{safe}"
        if not target.exists():
            target.write_bytes(content)
        stored_path = str(target)
    elif stored_path and os.path.exists(stored_path):
        try:
            size = os.path.getsize(stored_path)
        except OSError:
            size = None

    aid = db.execute(
        """INSERT INTO attachments
           (attachable_type, attachable_id, filename, path, content_hash,
            size_bytes, source, import_job_id, import_attempt_id, created_by,
            status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
        (attachable_type, attachable_id, os.path.basename(filename),
         stored_path, digest, size, source, import_job_id, import_attempt_id,
         created_by, iso()))
    return get(aid)


def get(attachment_id: int) -> Optional[dict]:
    row = db.query_one("SELECT * FROM attachments WHERE id=?", (attachment_id,))
    return dict(row) if row else None


def list_for_target(attachable_type: str, attachable_id: int) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM attachments WHERE attachable_type=? AND attachable_id=? "
        "ORDER BY id", (attachable_type, attachable_id))]


def list_for_batch(import_job_id: int, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM attachments WHERE import_job_id=?"
    if active_only:
        sql += " AND status='active'"
    return [dict(r) for r in db.query(sql + " ORDER BY id", (import_job_id,))]


def list_batch_originated(import_job_id: int) -> list[dict]:
    """All attachments a batch/its retries created (active ones)."""
    return [dict(r) for r in db.query(
        "SELECT * FROM attachments WHERE import_job_id=? AND status='active' "
        "AND source != 'manual' ORDER BY id", (import_job_id,))]


def quarantine_path(path: str) -> str:
    src = Path(path)
    qdir = config.DATA_DIR / QUARANTINE_DIRNAME
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / src.name
    # Never overwrite an existing quarantined file.
    i = 1
    while dest.exists():
        dest = qdir / f"{src.stem}.{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))
    return str(dest)


def revoke(a: dict) -> dict:
    """Mark revoked and move the file to quarantine. Idempotent per row.

    Must be called inside the undo transaction; the physical move happens at
    commit time by the caller (filesystem is not transactional), so this only
    flips DB state. Returns the updated row.
    """
    if a["status"] != "active":
        return a
    db.execute(
        "UPDATE attachments SET status='revoked', revoked_at=? WHERE id=? AND status='active'",
        (iso(), a["id"]))
    row = get(a["id"])
    return row
