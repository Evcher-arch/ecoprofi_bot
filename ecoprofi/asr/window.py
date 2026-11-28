"""Открыто ли окно распознавания (data-model.md, «Окно открыто, если»).

Чистые функции без БД: на вход — строка asr_schedule в виде словаря и текущий
момент. График распознавания независим от run_schedule индексатора.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ecoprofi.duetime import MOSCOW

# Запуск в плановом окне, закрытый по этим причинам, можно открыть снова в том же окне:
# ПК перезагрузили или видеокарта была занята.
RESUMABLE = ("worker_lost", "resource_unavailable", "error")
CATCH_UP_GRACE = timedelta(hours=24)


@dataclass(frozen=True)
class Window:
    kind: str                       # initial | scheduled | catch_up | manual
    until: datetime | None          # конец окна; None у первичного прохода
    due: datetime | None = None     # начало планового окна (для last_due_at)
    only: tuple[str, ...] | None = None   # запуск по списку записей


def _starts(schedule: dict, around: datetime, days_back: int = 8, days_ahead: int = 8):
    """Начала плановых окон по МСК в окрестности момента, по возрастанию."""
    local = around.astimezone(MOSCOW)
    start_at: time = schedule["start_at"]
    weekdays = set(schedule["weekdays"])
    for offset in range(-days_back, days_ahead + 1):
        day = local.date() + timedelta(days=offset)
        if day.isoweekday() in weekdays:
            yield datetime.combine(day, start_at, MOSCOW)


def current_start(schedule: dict, now: datetime) -> datetime | None:
    """Начало планового окна, в которое попадает now, иначе None."""
    length = schedule["window_length"]
    inside = [s for s in _starts(schedule, now) if s <= now < s + length]
    return max(inside) if inside else None


def missed_start(schedule: dict, now: datetime) -> datetime | None:
    """Последнее окно, которое уже закончилось не больше суток назад."""
    length = schedule["window_length"]
    passed = [s for s in _starts(schedule, now) if s + length <= now < s + length + CATCH_UP_GRACE]
    return max(passed) if passed else None


def next_due_at(schedule: dict, now: datetime) -> datetime:
    """Начало ближайшего будущего планового окна."""
    return min(s for s in _starts(schedule, now, days_back=0) if s > now)


def is_open(schedule: dict, now: datetime, *, last_outcome: str | None = None) -> Window | None:
    """Окно распознавания или None.

    last_outcome — исход последнего запуска планового окна: после потери связи
    с ПК окно открывается снова, пока оно не закончилось.
    """
    length = schedule["window_length"]
    only = schedule.get("manual_only")
    if only:
        # Явное действие оператора для пробы: работает и при паузе, берёт только список.
        return Window("manual", now + length, only=tuple(only))
    if schedule["paused"]:
        return None
    if schedule["initial_pass"] != "done":
        return Window("initial", None)
    if schedule.get("manual_requested_at"):
        return Window("manual", now + length)
    last_due = schedule.get("last_due_at")
    resumable = last_outcome in RESUMABLE
    start = current_start(schedule, now)
    if start is not None and (last_due is None or last_due < start or (resumable and last_due == start)):
        return Window("scheduled", start + length, due=start)
    if schedule["catch_up"]:
        missed = missed_start(schedule, now)
        if missed is not None and (last_due is None or last_due < missed):
            return Window("catch_up", now + length, due=missed)
    return None


def parse_window(text: str) -> timedelta:
    """«8h», «90m», «1h30m» → интервал от 30 минут до 24 часов."""
    import re
    match = re.fullmatch(r"\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*", text.lower())
    if not match or not any(match.groups()):
        raise ValueError("Окно задаётся как 8h, 90m или 1h30m.")
    value = timedelta(hours=int(match.group(1) or 0), minutes=int(match.group(2) or 0))
    if not timedelta(minutes=30) <= value <= timedelta(hours=24):
        raise ValueError("Окно — от 30 минут до 24 часов.")
    return value


def parse_days(text: str) -> list[int]:
    """«7» или «6,7» → ISO-дни 1–7, непустой список."""
    try:
        days = sorted({int(part) for part in text.split(",") if part.strip()})
    except ValueError:
        raise ValueError("Дни недели — числа 1–7 через запятую (7 — воскресенье).") from None
    if not days or any(d < 1 or d > 7 for d in days):
        raise ValueError("Дни недели — числа 1–7 через запятую (7 — воскресенье).")
    return days


def parse_at(text: str) -> time:
    try:
        return datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError("Время задаётся в виде ЧЧ:ММ.") from None
