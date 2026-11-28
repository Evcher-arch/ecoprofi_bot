"""Ключи распознавателя (research R2a): выпуск, хеш, проверка, отзыв.

Ключ показывается один раз при выпуске. В БД хранится только sha256 ключа.
Ключ не пишется в журналы и не возвращается ни одной командой.
"""
import hashlib
import hmac
import secrets

from psycopg.types.json import Jsonb


class WorkerExists(ValueError):
    pass


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue_key(conn, name: str) -> str:
    """Выпустить ключ нового распознавателя (256 бит). Вернуть значение ключа."""
    key = secrets.token_urlsafe(32)
    with conn.transaction():
        if conn.execute("SELECT 1 FROM asr_worker WHERE name = %s", (name,)).fetchone():
            raise WorkerExists(f"Распознаватель {name} уже зарегистрирован. Отзовите его ключ и выпустите новый "
                               "под другим именем.")
        conn.execute("INSERT INTO asr_worker (name, token_hash) VALUES (%s, %s)", (name, _hash(key)))
    return key


def revoke(conn, name: str) -> bool:
    """Отозвать ключ. Действующие аренды распознавателя возвращаются в очередь."""
    with conn.transaction():
        row = conn.execute("UPDATE asr_worker SET revoked_at = now() WHERE name = %s AND revoked_at IS NULL "
                           "RETURNING id", (name,)).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE asr_job SET state = 'queued', lease_id = NULL, lease_worker_id = NULL, "
            "lease_expires_at = NULL, deadline_at = NULL, audio_path = NULL, updated_at = now() "
            "WHERE lease_worker_id = %s AND state IN ('preparing', 'leased')", (row["id"],))
    return True


def authenticate(conn, bearer: str | None) -> dict | None:
    """Распознаватель по ключу из заголовка Authorization или None."""
    if not bearer or not bearer.startswith("Bearer "):
        return None
    key = bearer[len("Bearer "):].strip()
    if not key:
        return None
    digest = _hash(key)
    row = conn.execute("SELECT id, name, token_hash FROM asr_worker WHERE token_hash = %s AND revoked_at IS NULL",
                       (digest,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None
    return {"id": row["id"], "name": row["name"]}


def touch(conn, worker_id: int, version: str | None = None, device: dict | None = None) -> None:
    """Отметить связь. Версия и устройство обновляются, когда клиент их сообщил."""
    conn.execute(
        "UPDATE asr_worker SET last_seen_at = now(), version = coalesce(%s, version), "
        "device = coalesce(%s, device) WHERE id = %s",
        (version, Jsonb(device) if device is not None else None, worker_id))


def list_workers(conn) -> list[dict]:
    return conn.execute("SELECT name, created_at, revoked_at, last_seen_at, version, device "
                        "FROM asr_worker ORDER BY id").fetchall()
