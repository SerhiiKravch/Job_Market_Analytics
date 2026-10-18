"""Privacy-preserving JSON logs, with fixed events and numeric metadata only."""

import json
import logging
import math
from datetime import UTC, datetime
from typing import TextIO

_EVENTS = frozenset(
    {"application_started", "application_stopped", "configuration_validated", "operation_failed"}
)
_ERROR_CATEGORIES = frozenset({"configuration", "database", "telegram", "parsing", "internal"})
_NUMERIC_FIELDS = ("source_id", "message_id", "count", "duration_ms")


class SafeEventFormatter(logging.Formatter):
    """Drop arbitrary text, exception details, and non-allowlisted extra fields.

    Substring redaction cannot reliably recognize private message text. Only
    explicitly supported event names and typed metadata cross this boundary.
    Add new event names here when their emitting feature is implemented.
    """

    def format(self, record: logging.LogRecord) -> str:
        event = record.msg if isinstance(record.msg, str) and record.msg in _EVENTS else None
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": logging.getLevelName(record.levelno),
            "event": event or "unstructured_log_redacted",
        }
        if event:
            for name in _NUMERIC_FIELDS:
                value = getattr(record, name, None)
                valid_integer = type(value) is int and -(2**63) <= value < 2**63
                valid_duration = (
                    name == "duration_ms"
                    and type(value) is float
                    and math.isfinite(value)
                    and value >= 0
                )
                if valid_integer or valid_duration:
                    payload[name] = value
            category = getattr(record, "error_category", None)
            if isinstance(category, str) and category in _ERROR_CATEGORIES:
                payload["error_category"] = category
        return json.dumps(payload, ensure_ascii=True, allow_nan=False)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Configure stderr (or a supplied stream); never open files on import."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeEventFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
