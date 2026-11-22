"""Проверка доверенного канала и текущих прав участника.

Допуск — только подписка на клуб Экопрофи (VK Donut), она проверяется при
каждом обращении (ecoprofi.bot.club). Отдельного списка участников в БД нет.
Права оператора индексатора сюда не входят и доступа участнику не дают.
"""
import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass


class Unauthorized(Exception):
    """Неверный служебный ключ (401)."""


class Forbidden(Exception):
    """Недопустимые группа, диалог или участник (403)."""


class NotSubscribed(Forbidden):
    """Личный диалог допустим, но у автора нет подписки на клуб: ему отвечают отказом про клуб."""


@dataclass(frozen=True)
class Access:
    group_id: int
    user_id: int
    role: str                    # club: подписчик клуба
    audiences: tuple[str, ...]   # аудитории материалов, доступные участнику


CLUB_AUDIENCES = ("public", "club")


def check_service_key(presented: str | None, expected: str | None = None) -> None:
    expected = expected if expected is not None else os.environ.get("SERVICE_KEY", "")
    if not expected or not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise Unauthorized()


def resolve_access(conn, group_id: int, peer_id: int, user_id: int, is_member: Callable[[int], bool]) -> Access:
    """Проверить группу, личный диалог и подписку на клуб; вернуть действующую роль.

    is_member — проверка подписки (ClubMembers); её сбой (ClubUnavailable) пробрасывается:
    без подтверждённой подписки доступ не выдаётся.
    """
    if user_id <= 0 or peer_id != user_id:
        raise Forbidden("Поиск доступен только в личном диалоге.")
    if conn.execute("SELECT 1 FROM test_group WHERE group_id = %s", (group_id,)).fetchone() is None:
        raise Forbidden("Сообщество не подключено к поиску.")
    if not is_member(user_id):
        raise NotSubscribed("Поиск доступен подписчикам клуба Экопрофи.")
    return Access(group_id, user_id, "club", CLUB_AUDIENCES)


# Сигнал расписания для материала m: club, если хотя бы одна его публикация
# помечена «Клуб» в расписании или принадлежит платной ленте; public — если
# публикации есть в расписании, ни одна не закрыта и лента каждой проверена;
# NULL — материал не связан с расписанием либо лента публикации ещё не проверена
# (тогда действует только решение редактора, как в функции 001).
SIGNAL_SQL = """
    SELECT CASE WHEN count(*) = 0 THEN NULL
                WHEN bool_or(lp.schedule_club OR coalesce(ml.paid_feed, false)) THEN 'club'
                WHEN bool_or(ml.paid_feed IS NULL) THEN NULL
                ELSE 'public' END
    FROM material_link ml
    JOIN lecture_post lp ON lp.lecture_id = ml.lecture_id AND lp.post_id = ml.post_id
    WHERE ml.material_id = m.id
"""

# Действующая аудитория: решение редактора (mp) действует, пока сигнал расписания
# тот же, что он видел. Иначе — более строгая из двух (unknown > club > public).
# Без решения действует сигнал; без решения и сигнала материал закрыт (NULL).
EFFECTIVE_SQL = """
    CASE WHEN mp.audience IS NULL THEN s.signal
         WHEN s.signal IS NULL OR mp.seen_signal IS NOT DISTINCT FROM s.signal THEN mp.audience
         WHEN mp.audience = 'unknown' THEN 'unknown'
         WHEN 'club' IN (mp.audience, s.signal) THEN 'club'
         ELSE 'public' END
"""

# Материалы с решением, сигналом и действующей аудиторией — для поиска и отчёта.
MATERIAL_AUDIENCE = f"""
    SELECT m.*, mp.audience AS decision, mp.seen_signal, s.signal, eff.audience
    FROM material m
    LEFT JOIN material_policy mp
           ON mp.item_type = m.item_type AND mp.owner_id = m.owner_id AND mp.item_id = m.item_id
    LEFT JOIN LATERAL ({SIGNAL_SQL}) s(signal) ON true
    CROSS JOIN LATERAL (SELECT {EFFECTIVE_SQL} AS audience) eff
"""

# Общий фильтр обоих видов поиска: только действующие опубликованные материалы,
# чья действующая аудитория разрешена участнику. Параметр — массив аудиторий.
# Материал на проверке (review) и удалённый не выдаются никому.
VISIBLE_MATERIALS = f"""
    SELECT m.id AS material_id, m.published_version_id AS version_id,
           m.title, m.description, m.url, m.kind AS material_kind,
           m.item_type, m.owner_id, m.item_id, m.doc_numbers,
           m.req_date, m.req_number, m.req_sender, m.req_state,
           v.text_state, v.pages_total,
           lk.lecture_id, lk.lecture_title, lk.lecture_date
    FROM ({MATERIAL_AUDIENCE}) m
    JOIN material_version v ON v.id = m.published_version_id
    LEFT JOIN LATERAL (
        SELECT l.id AS lecture_id, l.title AS lecture_title, l.lecture_date
        FROM material_link ml JOIN lecture l ON l.id = ml.lecture_id
        WHERE ml.material_id = m.id
        ORDER BY (ml.link_type = 'attachment') DESC, ml.id
        LIMIT 1
    ) lk ON true
    WHERE m.state = 'active' AND m.audience = ANY(%(audiences)s)
"""


def visible_versions(conn, audiences: tuple[str, ...]) -> dict[int, int]:
    """Материалы, доступные сейчас: material_id → опубликованная версия."""
    rows = conn.execute(VISIBLE_MATERIALS, {"audiences": list(audiences)}).fetchall()
    return {r["material_id"]: r["version_id"] for r in rows}
