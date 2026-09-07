from __future__ import annotations


class BokError(Exception):
    """Structured error safe to expose through the local API."""

    def __init__(self, code: str, message: str, *, status: int = 400, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict:
        payload = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ConflictError(BokError):
    def __init__(self, message: str, *, details=None):
        super().__init__("conflict", message, status=409, details=details)


class NotFoundError(BokError):
    def __init__(self, message: str, *, details=None):
        super().__init__("not_found", message, status=404, details=details)


class PermissionDeniedError(BokError):
    def __init__(self, message: str, *, details=None):
        super().__init__("permission_denied", message, status=403, details=details)
