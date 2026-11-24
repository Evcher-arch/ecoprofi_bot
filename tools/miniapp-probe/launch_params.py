"""Изолированная проверка запуска Mini App. Не импортировать код bot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode

MAX_QUERY_BYTES = 16 * 1024


class LaunchError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Launch:
    app_id: int
    user_id: int
    timestamp: int
    canonical: str


def _canonical_query(pairs: list[tuple[str, str]], *, space_as_plus: bool) -> str:
    signed = sorted((key, value) for key, value in pairs if key.startswith("vk_"))
    if not signed:
        raise LaunchError("invalid_launch")
    if space_as_plus:
        return urlencode(signed)
    return urlencode(signed, quote_via=quote)


def verify_launch(
    query: str,
    *,
    secret: str,
    expected_app_id: int,
    now: int | None = None,
    check_age: bool = True,
) -> Launch:
    """Принять строгую исходную query; не принимать декодированный объект Bridge.

    Подпись проверяется до любой идентификации. Разница %20/+ в официальных
    примерах не ослабляет HMAC: принимается только полное совпадение sign.
    """
    if not secret or not query or query.startswith(("?", "http://", "https://")):
        raise LaunchError("invalid_launch")
    if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise LaunchError("invalid_launch")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in query):
        raise LaunchError("invalid_launch")
    index = 0
    while index < len(query):
        if query[index] == "%":
            if index + 2 >= len(query) or any(character not in "0123456789abcdefABCDEF" for character in query[index + 1:index + 3]):
                raise LaunchError("invalid_launch")
            index += 3
        else:
            index += 1
    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=128,
            separator="&",
        )
    except (ValueError, UnicodeError) as error:
        raise LaunchError("invalid_launch") from error
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)):
        raise LaunchError("invalid_launch")
    values = dict(pairs)
    required = {"sign", "vk_app_id", "vk_user_id", "vk_ts"}
    if not required.issubset(values) or not values["sign"]:
        raise LaunchError("invalid_launch")
    sign = values["sign"]
    if len(sign) != 43 or any(character not in "-_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" for character in sign):
        raise LaunchError("invalid_launch")
    try:
        app_id = int(values["vk_app_id"], 10)
        user_id = int(values["vk_user_id"], 10)
        timestamp = int(values["vk_ts"], 10)
    except ValueError as error:
        raise LaunchError("invalid_launch") from error
    if app_id <= 0 or user_id <= 0 or timestamp <= 0:
        raise LaunchError("invalid_launch")
    current = int(time.time()) if now is None else now
    if check_age and not current - 300 <= timestamp <= current + 60:
        raise LaunchError("launch_expired")

    canonical = _canonical_query(pairs, space_as_plus=True)
    candidates = (canonical, _canonical_query(pairs, space_as_plus=False))
    if not any(
        hmac.compare_digest(
            base64.urlsafe_b64encode(
                hmac.new(secret.encode("utf-8"), candidate.encode("utf-8"), hashlib.sha256).digest()
            ).rstrip(b"=").decode("ascii"),
            sign,
        )
        for candidate in candidates
    ):
        raise LaunchError("invalid_launch")
    if app_id != expected_app_id:
        raise LaunchError("invalid_launch")
    return Launch(app_id=app_id, user_id=user_id, timestamp=timestamp, canonical=canonical)
