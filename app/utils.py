"""Small shared helpers: timestamps, hashing, JSON, parsing."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    """ISO-8601 UTC string, second precision (consistent, sortable)."""
    dt = dt or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: Any) -> Optional[datetime]:
    """Parse a flexible timestamp into an aware UTC datetime, or None.

    Accepts ISO-8601 (with/without timezone, with/without seconds) and
    ``YYYY-MM-DD HH:MM[:SS]``. Naive values are assumed UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        # Allow "YYYY-MM-DD HH:MM:SS" by converting the space to "T"
        if len(s) > 10 and s[10] == " ":
            s = s[:10] + "T" + s[11:]
        dt = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M%z",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def content_hash(*parts: Any) -> str:
    """Stable hash used to detect duplicate calibrations."""
    h = hashlib.sha256()
    for p in parts:
        h.update(("" if p is None else str(p)).strip().lower().encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def from_json(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        raise ValueError(f"not a number: {value!r}")
