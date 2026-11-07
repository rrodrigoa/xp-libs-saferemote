"""Canonical public errors with bounded, sanitized interpolation."""

import json
import re
from importlib.resources import files
from string import Formatter
from typing import Any
from uuid import uuid4

from .codes import ErrorCode as ErrorCode

_CATALOG = json.loads(
    files(__package__).joinpath("catalog.json").read_text(encoding="utf-8")
)
_SAFE = re.compile(r"[a-zA-Z0-9_.@\[\] -]{1,180}")


def get_error_info(code: str) -> dict[str, Any]:
    """Read a copy of a public error record."""
    if code not in _CATALOG:
        raise IntentError("ERROR_CODE_UNKNOWN", code_value=code)
    return dict(_CATALOG[code])


class IntentError(Exception):
    """An error code and safe public context."""

    def __init__(self, code: str, *, path: str = "workflow", **context: str) -> None:
        record = _CATALOG[code]
        self.code = code
        self.path = path if _SAFE.fullmatch(path) else "workflow"
        values = dict(
            context, path=self.path, code=context.get("code_value", "unknown")
        )
        fields = {
            key: value
            if _SAFE.fullmatch(value := values.get(key, "unknown"))
            else "unknown"
            for _, key, _, _ in Formatter().parse(record["message"])
            if key is not None
        }
        super().__init__(record["message"].format_map(fields))

    def envelope(self) -> dict[str, Any]:
        """Render the public response without the original exception context."""
        record = _CATALOG[self.code]
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": str(self),
                "path": self.path,
                "category": record["category"],
                "retryable": record["retryable"],
                "correlation_id": uuid4().hex,
                "suggested_fix": record["remediation"],
                "example": record["example"],
            },
        }
