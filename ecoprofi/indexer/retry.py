"""Различение сбоев чтения источника и повтор временных ошибок.

Тайм-аут, пустой ответ, отказ доступа и ошибка разбора получают разные статусы.
Повторяется только временная ошибка — и только затронутая операция.
"""
import time

MAX_ATTEMPTS = 3
BASE_DELAY = 2.0


class SourceError(Exception):
    """Базовая ошибка источника. reason совпадает с index_job.failure_reason."""
    reason = "parse"
    temporary = False


class SourceTimeout(SourceError):
    """Сеть, тайм-аут, HTTP 5xx: временный сбой, предыдущая версия сохраняется."""
    reason = "timeout"
    temporary = True


class SourceEmpty(SourceError):
    """API ответил, но объекта в ответе нет. Удаление этим не подтверждается."""
    reason = "empty"


class SourceDenied(SourceError):
    """Отказ в доступе к одному объекту (закрытый документ, чужая запись)."""
    reason = "denied"


class SourceAuth(SourceDenied):
    """Отказ авторизации оператора: токен отозван, просрочен или не сохранён.

    Касается всего прохода, а не одного материала: проход останавливается,
    состояние доступа — «нужен вход».
    """
    reason = "auth"


class PaidFeedDenied(SourceDenied):
    """Платная лента закрыта доступу оператора: закончилась подписка или права в сообществе."""


class PaidFeedLost(SourceError):
    """Платная лента пуста или резко сократилась. Проход останавливается, каталог не меняется:
    иначе платные публикации были бы сочтены пропавшими или открытыми."""
    reason = "paid_feed"

    def __init__(self, count: int | None, previous: int | None):
        shown = "не читается" if count is None else f"записей {count}"
        super().__init__(f"Платная лента {shown}" + (f", в прошлом успешном проходе {previous}" if previous else ""))
        self.count, self.previous = count, previous


class SourceRateLimit(SourceError):
    """Источник ограничил частоту обращений; паузы не помогли. Проход приостанавливается."""
    reason = "rate_limit"


class SourceParse(SourceError):
    """Ответ получен, но не разобран."""
    reason = "parse"


class SourceDeleted(SourceError):
    """Подтверждённое удаление: источник явно сообщает, что объект удалён."""
    reason = "deleted"


def with_retry(operation, *, attempts: int = MAX_ATTEMPTS, delay: float = BASE_DELAY, sleep=time.sleep):
    """Выполнить operation(attempt). Временную ошибку повторить с растущей задержкой.

    operation получает номер попытки (с 1) и при повторе сама запрашивает свежий
    адрес дорожки. Возвращает (результат, число попыток).
    """
    for attempt in range(1, attempts + 1):
        try:
            return operation(attempt), attempt
        except SourceError as exc:
            if not exc.temporary or attempt == attempts:
                exc.attempts = attempt
                raise
            sleep(delay * attempt)
