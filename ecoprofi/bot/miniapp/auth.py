"""Строгая проверка исходных параметров запуска VK Mini App."""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode

from ecoprofi.bot.miniapp import config
from ecoprofi.bot.miniapp.config import LAUNCH_AGE_SECONDS, LAUNCH_FUTURE_SECONDS, LAUNCH_LIMIT


class LaunchError(ValueError):
    def __init__(self, code="invalid_launch"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Launch:
    app_id: int
    user_id: int
    timestamp: int
    digest: bytes


def canonical(pairs: list[tuple[str, str]], *, plus: bool = True) -> str:
    signed = sorted((key, value) for key, value in pairs if key.startswith("vk_"))
    if not signed:
        raise LaunchError()
    return urlencode(signed) if plus else urlencode(signed, quote_via=quote)


def verify_launch(query: str, *, secret: str, expected_app_id: int, now: datetime | None = None,
                  check_age: bool = True) -> Launch:
    if not secret or not query or query.startswith(("?", "http://", "https://")):
        raise LaunchError()
    if len(query.encode("utf-8")) > LAUNCH_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in query):
        raise LaunchError()
    index = 0
    while index < len(query):
        if query[index] == "%":
            if index + 2 >= len(query) or any(c not in "0123456789abcdefABCDEF" for c in query[index + 1:index + 3]):
                raise LaunchError()
            index += 3
        else:
            index += 1
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=128, separator="&")
    except (ValueError, UnicodeError) as error:
        raise LaunchError() from error
    names = [key for key, _ in pairs]
    if len(names) != len(set(names)):
        raise LaunchError()
    values = dict(pairs)
    if not {"sign", "vk_app_id", "vk_user_id", "vk_ts"}.issubset(values) or not values["sign"]:
        raise LaunchError()
    sign = values["sign"]
    if len(sign) != 43 or any(c not in "-_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for c in sign):
        raise LaunchError()
    try:
        app_id = int(values["vk_app_id"], 10)
        user_id = int(values["vk_user_id"], 10)
        timestamp = int(values["vk_ts"], 10)
    except ValueError as error:
        raise LaunchError() from error
    if min(app_id, user_id, timestamp) <= 0:
        raise LaunchError()
    current = int((now or config.utcnow()).timestamp())
    if check_age and not current - LAUNCH_AGE_SECONDS <= timestamp <= current + LAUNCH_FUTURE_SECONDS:
        raise LaunchError("launch_expired")
    base = canonical(pairs)
    candidates = (base, canonical(pairs, plus=False))
    if not any(hmac.compare_digest(base64.urlsafe_b64encode(
            hmac.new(secret.encode(), candidate.encode(), hashlib.sha256).digest()).rstrip(b"=").decode(), sign)
            for candidate in candidates):
        raise LaunchError()
    if app_id != expected_app_id:
        raise LaunchError()
    digest = hashlib.sha256(base.encode() + b"\x00" + sign.encode()).digest()
    return Launch(app_id, user_id, timestamp, digest)


def launch_time(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)
