"""Автоматические запуски обновления: python -m ecoprofi.indexer.daemon

Не чаще двух раз в сутки — утром и вечером по московскому времени. Сервис раз
в минуту сверяет расписание запусков в БД со временем и запускает обновление.
Срок, пропущенный из-за выключенного стенда, выполняется один раз и только
если он относится к текущим суткам.
"""
import logging
import sys
import time
from datetime import datetime, timezone

from ecoprofi.db import connect
from ecoprofi.duetime import ScheduleError, is_due, parse_slots
from ecoprofi.indexer import alerts, runs

TICK = 60
log = logging.getLogger("ecoprofi.daemon")


def schedule_set(conn, values: list[str]) -> tuple:
    """Назначить утреннее и вечернее время. Больше двух значений и близкие сроки отклоняются."""
    morning, evening = parse_slots(values)
    conn.execute("UPDATE run_schedule SET morning = %s, evening = %s", (morning, evening))
    conn.commit()
    return morning, evening


def schedule_on(conn, now: datetime | None = None) -> None:
    """Включить автоматические запуски — только после наблюдавшихся продления и повторного входа."""
    access = conn.execute("SELECT refresh_verified_at, relogin_verified_at FROM operator_access").fetchone()
    missing = [name for name, at in (
        ("успешное продление доступа без входа", access["refresh_verified_at"]),
        ("вход после состояния «нужен вход»", access["relogin_verified_at"])) if at is None]
    if missing:
        conn.rollback()
        raise ScheduleError("Автоматические запуски не включены: не наблюдалось " + " и ".join(missing) + ".")
    # Отсчёт сроков — с момента включения: прошедший сегодня срок не догоняется.
    conn.execute("UPDATE run_schedule SET enabled = true, last_due_at = %s", (now or datetime.now(timezone.utc),))
    conn.commit()


def schedule_off(conn) -> None:
    conn.execute("UPDATE run_schedule SET enabled = false")
    conn.commit()


def step(conn, start, now: datetime | None = None) -> str | None:
    """Один такт: наступил ли срок и что с ним сделано. start(conn) запускает обновление."""
    now = now or datetime.now(timezone.utc)
    runs.reconcile(conn)
    schedule = conn.execute("SELECT * FROM run_schedule").fetchone()
    conn.commit()
    if not schedule["enabled"]:
        return None
    due = is_due(now, schedule["morning"], schedule["evening"], schedule["last_due_at"])
    if due is None:
        return None
    # Срок учитывается до запуска: сбой прохода не приводит к повторному опросу в тот же срок.
    conn.execute("UPDATE run_schedule SET last_due_at = %s", (due,))
    conn.commit()
    if conn.execute("SELECT state FROM operator_access").fetchone()["state"] != "active":
        alerts.open_alert(conn, "access", alerts.text_access())
        runs.record_skipped(conn, "scheduled", "auth")
        return "skipped:auth"
    result = start(conn)
    if result["state"] == "busy":
        runs.record_skipped(conn, "scheduled", "busy")
        return "skipped:busy"
    return result["state"]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from ecoprofi.indexer import context      # тяжёлые зависимости — только в процессе сервиса

    log.info("сервис запусков работает; расписание читается из БД")
    while True:
        try:
            with connect() as conn:
                outcome = step(conn, lambda c: context.update_run(c, context.make_ai(), context.make_vk(), "scheduled"))
            if outcome:
                log.info("плановый срок: %s", outcome)
        except Exception as exc:    # сервис не останавливается; текст ошибки может содержать адреса
            log.warning("такт не выполнен (%s)", type(exc).__name__)
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
