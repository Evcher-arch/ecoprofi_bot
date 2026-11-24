"""Allowlist технических событий Mini App: без текстов, query, токенов и traceback."""
from __future__ import annotations

from ecoprofi import telemetry

ALLOWED_DETAILS = {
    "channel", "event", "attempt_no", "duration_ms", "materials", "versions",
    "error_code", "outcome", "session_id", "dialog_id", "request_id", "queue_depth",
}


def record(conn, operation_id: str, outcome: str, **details):
    safe = {key: value for key, value in details.items() if key in ALLOWED_DETAILS}
    safe["channel"] = "miniapp"
    return telemetry.record(conn, operation_id, "bot", outcome, **safe)
