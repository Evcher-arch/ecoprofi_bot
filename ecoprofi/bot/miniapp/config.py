"""Настройки и конечные политики Mini App; секреты только серверные."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

BODY_LIMIT = 32 * 1024
LAUNCH_LIMIT = 16 * 1024
QUESTION_LIMIT = 2000
SESSION_SECONDS = 8 * 60 * 60
LAUNCH_AGE_SECONDS = 300
LAUNCH_FUTURE_SECONDS = 60
QUEUE_LIMIT = 32
DIALOG_LIMIT = 20
REQUEST_LIMIT = 200
ATTEMPT_LIMIT = 3
BUDGET_SECONDS = 180
POLL_SECONDS = 2
HTTP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class MiniAppConfig:
    enabled: bool
    app_id: int
    secret: str
    origin: str
    club_url: str

    @classmethod
    def from_env(cls) -> "MiniAppConfig":
        enabled = os.environ.get("MINIAPP_ENABLED", "false").lower() == "true"
        raw_id = os.environ.get("MINIAPP_APP_ID", "")
        secret = os.environ.get("MINIAPP_APP_SECRET", "")
        origin = os.environ.get("MINIAPP_PUBLIC_ORIGIN", "").rstrip("/")
        if not enabled or not raw_id.isdigit() or int(raw_id) <= 0 or not secret or not origin.startswith("https://"):
            return cls(False, 0, "", "", "")
        return cls(True, int(raw_id), secret, origin, os.environ.get("CLUB_URL", ""))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def monotonic() -> float:
    return time.monotonic()
