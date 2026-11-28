"""Оповещения оператору (specs/003-autonomous-indexer/contracts/alerts.md).

Индексатор и bot только записывают причины в БД; отправляет их vk-adapter.
Одна открытая запись на причину: повторное событие обновляет текст.

Текст — причина одной строкой, числа, ключи материалов, номера публикаций и
название команды. Названий и текста материалов, адресов и значений доступа в
нём нет: тексты собираются только функциями этого модуля.
"""
from datetime import datetime

from ecoprofi.duetime import MOSCOW

REMIND_AFTER_HOURS = 24


def _now() -> str:
    return datetime.now(MOSCOW).strftime("%d.%m %H:%M")


def open_alert(conn, cause_key: str, body: str) -> None:
    conn.execute(
        "INSERT INTO alert (cause_key, kind, body) VALUES (%s, 'action', %s) "
        "ON CONFLICT (cause_key) WHERE state = 'open' DO UPDATE SET body = excluded.body",
        (cause_key, body))


def close_alert(conn, cause_key: str) -> None:
    """Закрыть причину. О восстановлении сообщается, только если о причине уже сообщали."""
    conn.execute(
        "UPDATE alert SET state = 'closed', closed_at = now(), recovery_sent = (sent_count = 0) "
        "WHERE cause_key = %s AND state = 'open'", (cause_key,))


def close_prefix(conn, prefix: str, keep: list[str]) -> None:
    """Закрыть причины с общим началом ключа, кроме перечисленных."""
    conn.execute(
        "UPDATE alert SET state = 'closed', closed_at = now(), recovery_sent = (sent_count = 0) "
        "WHERE state = 'open' AND cause_key LIKE %s AND NOT (cause_key = ANY(%s))", (prefix + "%", keep))


def info(conn, cause_key: str, body: str) -> None:
    """Разовое сообщение: отправляется один раз и закрывается."""
    conn.execute("INSERT INTO alert (cause_key, kind, body) VALUES (%s, 'info', %s) "
                 "ON CONFLICT (cause_key) WHERE state = 'open' DO UPDATE SET body = excluded.body",
                 (cause_key, body))


# --- тексты ---

def text_access() -> str:
    return (f"Индексатору нужен вход оператора ({_now()}).\n"
            "Доступ к источникам VK отозван, просрочен или не сохранён; обновление остановлено, "
            "поиск работает по опубликованным материалам.\nДействие: access login")


def text_run_failed(run_id: int, reason: str) -> str:
    return (f"Обновление не завершено ({_now()}).\nПроход {run_id}: {reason}.\n"
            "Действие: status")


def text_schedule_failed(run_id: int) -> str:
    return (f"Расписание лекций не прочитано ({_now()}).\nПроход {run_id}: перечень лекций не изменён, "
            "отложенные материалы повторены по прежнему перечню.\nДействие: status")


def text_permanent(key: str, post_id, reason: str | None) -> str:
    return (f"Материал не читается ({_now()}).\n{key}, публикация {post_id}: постоянный сбой"
            f"{f' ({reason})' if reason else ''}, автоматически больше не повторяется.\n"
            f"Действие: проверить материал в публикации; requeue {key}")


def text_post_missing(post_id: int, missed: int) -> str:
    return (f"Публикация не возвращена лентой ({_now()}).\nПубликация {post_id}: пропусков подряд {missed}. "
            "Материалы остаются в поиске до подтверждённого удаления.\nДействие: проверить публикацию")


def text_editor(count: int) -> str:
    return (f"Материалы ждут решения редактора ({_now()}).\nБез подтверждённой аудитории: {count}. "
            "До решения они никому не выдаются.\nДействие: передать отчёт редактору (--report)")


def text_ai_limit(run_id: int) -> str:
    return (f"Обновление остановлено: внешняя обработка текста недоступна ({_now()}).\n"
            f"Проход {run_id}: исчерпан лимит или отклонён ключ. Опубликованное сохранено.\n"
            "Действие: проверить лимит и ключ, затем run update")


def text_paid_feed(count: int | None, previous: int | None) -> str:
    seen = "не читается" if count is None else f"записей {count}"
    before = f", в прошлом успешном проходе {previous}" if previous else ""
    return (f"Платная лента не читается доступом оператора ({_now()}).\nПлатная лента: {seen}{before}. "
            "Обновление остановлено, каталог не изменён.\n"
            "Действие: проверить подписку на клуб и права учётной записи оператора в сообществе, затем run update")


def text_club() -> str:
    return (f"Бот не может проверить подписку участников на клуб ({_now()}).\n"
            "Поиск закрыт для всех, пока проверка не восстановится.\n"
            "Действие: проверить ключ сообщества Экопрофи (CLUB_GROUP_TOKEN) и перезапустить bot")


def text_stale(hours: int | None) -> str:
    detail = f"Последнее успешное обновление — {hours} ч назад." if hours is not None \
        else "Плановое обновление не началось в назначенный срок."
    return (f"Индекс не обновляется ({_now()}).\n{detail}\n"
            "Действие: проверить, работает ли indexer-daemon; status")


def text_run_summary(run_id: int, outcomes: dict) -> str:
    return (f"Полный проход {run_id} завершён ({_now()}).\n"
            f"Опубликовано: {outcomes.get('published', 0)}; ждёт источника: {outcomes.get('waiting_source', 0)}; "
            f"ждёт редактора: {outcomes.get('editor', 0)}; формат не поддерживается: "
            f"{outcomes.get('unsupported', 0)}; постоянный сбой: {outcomes.get('permanent', 0)}; "
            f"отложено: {outcomes.get('deferred', 0)}.")


# --- распознавание записей без субтитров (006) ---

def text_asr_permanent(key: str, reason: str | None) -> str:
    return (f"Запись не распознаётся ({_now()}).\n{key}: постоянный сбой распознавания"
            f"{f' ({reason})' if reason else ''}, автоматически больше не берётся.\n"
            f"Действие: python -m ecoprofi.asr jobs --state permanent; requeue {key}")


def text_asr_silent(days: int) -> str:
    return (f"Распознаватель на ПК не выходил на связь {days} сут. ({_now()}).\n"
            "Записи без субтитров ждут распознавания; поиск по остальным материалам работает.\n"
            "Действие: включить ПК с Docker Desktop; python -m ecoprofi.asr status")
