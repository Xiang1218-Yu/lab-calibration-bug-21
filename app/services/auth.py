"""Minimal role/permission confirmation for destructive operations.

The product has no login session yet, so identity is carried by the
``X-Actor`` header and role by ``X-Role`` (an administrative caller is assumed
to have authenticated upstream). Undo is destructive and two-phase, so it
additionally requires an explicit acknowledgement flag and binds the request to
a preview fingerprint. Swapping this module for real auth later does not touch
the undo engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class AuthorizationError(PermissionError):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status
        self.message = message


# role -> granted permissions
ROLE_PERMISSIONS = {
    "viewer": {"read"},
    "operator": {"read", "import:create"},
    "admin": {"read", "import:create", "import:undo", "compensation:resolve"},
}
DEFAULT_ROLE = "operator"


@dataclass
class Principal:
    actor: str
    role: str

    @property
    def permissions(self) -> set[str]:
        return ROLE_PERMISSIONS.get(self.role, set())

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def principal(headers) -> Principal:
    """Build a principal from HTTP-like headers (case-insensitive mapping)."""
    actor = headers.get("X-Actor") or headers.get("x-actor") or "anonymous"
    role = (headers.get("X-Role") or headers.get("x-role")
            or DEFAULT_ROLE).lower()
    if role not in ROLE_PERMISSIONS:
        raise AuthorizationError(f"unknown role {role!r}", status=400)
    return Principal(actor=actor, role=role)


def require(princ: Principal, permission: str) -> None:
    if not princ.can(permission):
        raise AuthorizationError(
            f"role {princ.role!r} lacks permission {permission!r}")


def require_undo_confirmation(princ: Principal, body: dict,
                              previewed: bool) -> None:
    """Explicit acknowledgement guards the destructive commit."""
    require(princ, "import:undo")
    if not body.get("confirm") is True:
        raise AuthorizationError(
            "undo must be explicitly confirmed with \"confirm\": true")
    if not body.get("confirm_token"):
        raise AuthorizationError("confirm_token from preview is required")
