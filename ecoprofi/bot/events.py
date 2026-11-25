"""Однократная обработка события и контекст уточнения.

Событие определяется составным ключом группы и события. Повтор события не
продвигает контекст: он возвращает прежние итоговый запрос и random_id, а поиск
выполняется заново с действующими правами. Контекст изолирован по группе,
peer_id и user_id.
"""
import secrets
from dataclasses import dataclass

from ecoprofi.bot.access import Forbidden

MAX_QUERY = 2000


@dataclass(frozen=True)
class EventState:
    effective_query: str   # запрос, по которому получен ответ (для повтора — сохранённый)
    random_id: int         # идентификатор отправки, один на событие
    first: bool            # False — повторная доставка того же события
    context: str | None = None   # прежний уточняемый запрос диалога (только для первой доставки)


def register(conn, group_id: int, event_id: str, peer_id: int, user_id: int, text: str) -> EventState:
    """Зарегистрировать событие или вернуть состояние уже обработанного."""
    with conn.transaction():
        pending = conn.execute(
            "SELECT pending_query FROM dialog_context "
            "WHERE group_id = %s AND peer_id = %s AND user_id = %s FOR UPDATE",
            (group_id, peer_id, user_id),
        ).fetchone()
        # Итоговый запрос уточняется после поиска (set_effective_query): новое сообщение
        # ищется само по себе, а контекст добавляется, только если без него не нашлось.
        effective = text[-MAX_QUERY:]
        random_id = secrets.randbelow(2**31 - 1) + 1
        inserted = conn.execute(
            "INSERT INTO event (group_id, event_id, peer_id, user_id, effective_query, random_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (group_id, event_id) DO NOTHING RETURNING 1",
            (group_id, event_id, peer_id, user_id, effective, random_id),
        ).fetchone()
        if inserted:
            return EventState(effective, random_id, True, pending["pending_query"] if pending else None)
        row = conn.execute(
            "SELECT peer_id, user_id, effective_query, random_id FROM event "
            "WHERE group_id = %s AND event_id = %s",
            (group_id, event_id),
        ).fetchone()
    if (row["peer_id"], row["user_id"]) != (peer_id, user_id):
        # Идентификатор события принадлежит другому диалогу.
        raise Forbidden("Событие относится к другому диалогу.")
    return EventState(row["effective_query"], row["random_id"], False)


def set_effective_query(conn, group_id: int, event_id: str, effective_query: str) -> None:
    """Запомнить запрос, по которому получен ответ: повтор события выполнит тот же поиск."""
    with conn.transaction():
        conn.execute("UPDATE event SET effective_query = %s WHERE group_id = %s AND event_id = %s",
                     (effective_query, group_id, event_id))


def update_context(conn, group_id: int, peer_id: int, user_id: int, outcome: str, effective_query: str) -> None:
    """После первого ответа: запомнить уточняемый запрос либо очистить контекст."""
    with conn.transaction():
        if outcome == "clarify":
            conn.execute(
                "INSERT INTO dialog_context (group_id, peer_id, user_id, pending_query) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (group_id, peer_id, user_id) DO UPDATE "
                "SET pending_query = excluded.pending_query, updated_at = now()",
                (group_id, peer_id, user_id, effective_query),
            )
        else:
            conn.execute(
                "DELETE FROM dialog_context WHERE group_id = %s AND peer_id = %s AND user_id = %s",
                (group_id, peer_id, user_id),
            )


def mark_delivered(conn, group_id: int, event_id: str) -> None:
    with conn.transaction():
        conn.execute("UPDATE event SET state = 'delivered' WHERE group_id = %s AND event_id = %s",
                     (group_id, event_id))
