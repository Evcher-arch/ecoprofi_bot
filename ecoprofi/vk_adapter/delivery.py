"""Доставка ответа в исходный диалог.

Ответ уходит только в peer_id исходного события. При неопределённом исходе
отправки сохраняется прежний random_id, а перед повторной отправкой у bot
запрашивается актуальный ответ: права могли измениться.
"""
import time

MAX_ATTEMPTS = 3
DELAY = 2.0
MAX_MESSAGE = 4000


class BotUnavailable(Exception):
    """bot временно недоступен: тот же event_id нужно повторить."""


class SendUncertain(Exception):
    """Исход messages.send неизвестен (тайм-аут, обрыв)."""


class SendRejected(Exception):
    """VK отклонил отправку; повтор не поможет."""


def request_answer(bot, event: dict, *, attempts: int = MAX_ATTEMPTS, sleep=time.sleep) -> dict | None:
    """Получить ответ bot. None — событие отклонено (401/403/422), поиск не запускался."""
    for attempt in range(1, attempts + 1):
        try:
            return bot.search(event)
        except BotUnavailable:
            if attempt == attempts:
                raise
            sleep(DELAY * attempt)


def deliver(vk, bot, event: dict, *, attempts: int = MAX_ATTEMPTS, sleep=time.sleep) -> str:
    """Обработать событие целиком. Возвращает sent / skipped / mismatch / failed."""
    try:
        answer = request_answer(bot, event, attempts=attempts, sleep=sleep)
    except BotUnavailable:
        return "failed"
    for attempt in range(1, attempts + 1):
        if answer is None:
            return "skipped"
        if answer.get("peer_id") != event["peer_id"] or answer.get("event_id") != event["event_id"]:
            return "mismatch"
        try:
            vk.send(event["peer_id"], answer["random_id"], answer["text"][:MAX_MESSAGE])
        except SendRejected:
            return "failed"
        except SendUncertain:
            if attempt == attempts:
                return "failed"
            sleep(DELAY * attempt)
            try:
                # Тот же event_id: bot вернёт прежний random_id и ответ по действующим правам.
                answer = request_answer(bot, event, attempts=attempts, sleep=sleep)
            except BotUnavailable:
                return "failed"
            continue
        bot.delivered(event)
        return "sent"
    return "failed"
