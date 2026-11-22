"""Измерения по идентификатору операции.

Запись содержит версию индекса, использованные материалы, исход, длительности
и расход токенов. Ключи, токены, адреса возврата и подписанные URL в запись
не попадают: значения проходят очистку перед сохранением.
"""
import json
import logging
import re
import uuid

from psycopg.types.json import Jsonb

log = logging.getLogger("ecoprofi.telemetry")

_SECRET_KEYS = re.compile(r"(^|_)(token|secret|password|key|authorization|verifier|redirect\w*|callback\w*|"
                          r"track_url|sign\w*)$", re.I)
_URL = re.compile(r"https?://\S+")
_STABLE_URL = re.compile(r"^https://vkvideo\.ru/video-?\d+_\d+(\?t=\d+s)?$")


def new_operation_id() -> str:
    return uuid.uuid4().hex


def _clean_text(value: str) -> str:
    # Сохраняются только стабильные ссылки на записи; прочие адреса вырезаются.
    return _URL.sub(lambda m: m.group(0) if _STABLE_URL.match(m.group(0)) else "[url]", value)


def sanitize(value):
    """Убрать секреты и временные адреса из произвольной структуры."""
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if not _SECRET_KEYS.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return _clean_text(value)
    return value


def record(conn, operation_id: str, component: str, outcome: str, **details) -> dict:
    """Сохранить измерение в operation_log и продублировать в журнал процесса."""
    clean = sanitize(details)
    conn.execute(
        "INSERT INTO operation_log (operation_id, component, outcome, details) VALUES (%s, %s, %s, %s)",
        (operation_id, component, outcome, Jsonb(clean)),
    )
    log.info(json.dumps({"operation_id": operation_id, "component": component,
                         "outcome": outcome, **clean}, ensure_ascii=False))
    return clean
