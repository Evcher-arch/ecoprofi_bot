"""Проверка доверенного канала и текущих прав участника.

Права читаются из БД при каждом обращении. Права оператора индексатора
сюда не входят и доступа участнику не дают.
"""
import hmac
import os
from dataclasses import dataclass


class Unauthorized(Exception):
    """Неверный служебный ключ (401)."""


class Forbidden(Exception):
    """Недопустимые группа, диалог или участник (403)."""


@dataclass(frozen=True)
class Access:
    group_id: int
    user_id: int
    role: str                    # public / club / unknown
    audiences: tuple[str, ...]   # аудитории материалов, доступные участнику


def check_service_key(presented: str | None, expected: str | None = None) -> None:
    expected = expected if expected is not None else os.environ.get("SERVICE_KEY", "")
    if not expected or not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise Unauthorized()


def audiences_for(role: str) -> tuple[str, ...]:
    """Неизвестная роль допущенного участника разрешает публичную часть."""
    return ("public", "club") if role == "club" else ("public",)


def resolve_access(conn, group_id: int, peer_id: int, user_id: int) -> Access:
    """Проверить группу, личный диалог и допуск; вернуть действующую роль."""
    if user_id <= 0 or peer_id != user_id:
        raise Forbidden("Поиск доступен только в личном диалоге.")
    row = conn.execute(
        "SELECT p.role FROM participant p JOIN test_group g USING (group_id) "
        "WHERE p.group_id = %s AND p.vk_id = %s AND p.tester",
        (group_id, user_id),
    ).fetchone()
    if row is None:
        raise Forbidden("Группа или участник не допущены к тесту.")
    return Access(group_id, user_id, row["role"], audiences_for(row["role"]))


# Общий фильтр обоих видов поиска: только действующие опубликованные материалы
# с известной аудиторией, разрешённой участнику. Параметр — массив аудиторий.
VISIBLE_MATERIALS = """
    SELECT m.id AS material_id, m.published_version_id AS version_id, m.lecture_id,
           m.title, m.description, m.url, m.owner_id, m.video_id, v.track_state
    FROM material m
    JOIN material_policy mp ON mp.owner_id = m.owner_id AND mp.video_id = m.video_id
    JOIN material_version v ON v.id = m.published_version_id
    WHERE m.state = 'active' AND mp.audience = ANY(%(audiences)s)
"""


def visible_versions(conn, audiences: tuple[str, ...]) -> dict[int, int]:
    """Материалы, доступные сейчас: material_id → опубликованная версия."""
    rows = conn.execute(VISIBLE_MATERIALS, {"audiences": list(audiences)}).fetchall()
    return {r["material_id"]: r["version_id"] for r in rows}
