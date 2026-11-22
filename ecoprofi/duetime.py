"""Сроки автоматических запусков: не чаще двух раз в сутки, утром и вечером.

Чистые функции без БД и сети: ими пользуются сервис запусков индексатора и
проверка устаревания в bot.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
MIN_GAP = timedelta(hours=6)
MAX_SLOTS = 2


class ScheduleError(ValueError):
    """Недопустимое расписание запусков."""


def parse_slots(values: list[str]) -> tuple[time, time | None]:
    """Утреннее и вечернее время из строк ЧЧ:ММ. Больше двух значений и близкие сроки отклоняются."""
    if not 1 <= len(values) <= MAX_SLOTS:
        raise ScheduleError("Допустимо одно или два времени запуска в сутки: утреннее и вечернее.")
    try:
        slots = sorted(datetime.strptime(v, "%H:%M").time() for v in values)
    except ValueError:
        raise ScheduleError("Время задаётся в виде ЧЧ:ММ.") from None
    if len(slots) == 2:
        gap = timedelta(hours=slots[1].hour - slots[0].hour, minutes=slots[1].minute - slots[0].minute)
        if gap < MIN_GAP:
            raise ScheduleError("Между утренним и вечерним запуском должно быть не менее шести часов.")
        return slots[0], slots[1]
    return slots[0], None


def slots_of(now: datetime, morning: time, evening: time | None) -> list[datetime]:
    """Сроки текущих московских суток."""
    local = now.astimezone(MOSCOW)
    return [datetime.combine(local.date(), slot, MOSCOW) for slot in (morning, evening) if slot is not None]


def last_due(now: datetime, morning: time, evening: time | None) -> datetime | None:
    """Последний наступивший срок текущих московских суток, иначе None.

    Вчерашние пропущенные сроки не догоняются: иначе за сутки набралось бы
    больше двух автоматических запусков.
    """
    passed = [slot for slot in slots_of(now, morning, evening) if slot <= now]
    return max(passed) if passed else None


def next_due(now: datetime, morning: time, evening: time | None) -> datetime:
    """Ближайший будущий срок."""
    upcoming = [slot for slot in slots_of(now, morning, evening) if slot > now]
    if upcoming:
        return min(upcoming)
    tomorrow = now.astimezone(MOSCOW).date() + timedelta(days=1)
    return datetime.combine(tomorrow, morning, MOSCOW)


def is_due(now: datetime, morning: time, evening: time | None, last_due_at: datetime | None) -> datetime | None:
    """Срок, который пора выполнить: наступил сегодня и ещё не учтён."""
    due = last_due(now, morning, evening)
    if due is None or (last_due_at is not None and last_due_at >= due):
        return None
    return due
