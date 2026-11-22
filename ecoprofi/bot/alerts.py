"""Очередь оповещений оператору для vk-adapter (contracts/alerts.md).

bot не отправляет сообщения сам: ключ сообщений есть только у vk-adapter.
Перед выдачей очереди bot проверяет, не устарел ли индекс: если сервис
запусков остановлен, сообщить о собственном сбое он не может.
"""
import os
from datetime import datetime, timedelta, timezone

from ecoprofi.duetime import last_due
from ecoprofi.indexer import alerts as causes

REMIND_AFTER = timedelta(hours=causes.REMIND_AFTER_HOURS)
DUE_GRACE = timedelta(minutes=15)


def stale_hours() -> int:
    return max(1, int(os.environ.get("ALERT_STALE_HOURS", "48")))


def check_stale(conn, now: datetime) -> None:
    """Открыть или закрыть причину «индекс не обновляется»."""
    done = conn.execute("SELECT max(finished_at) AS at FROM index_run WHERE state = 'done'").fetchone()["at"]
    schedule = conn.execute("SELECT * FROM run_schedule").fetchone()
    hours = None
    if done is not None and now - done > timedelta(hours=stale_hours()):
        hours = int((now - done).total_seconds() // 3600)
    missed = False
    if schedule and schedule["enabled"]:
        due = last_due(now, schedule["morning"], schedule["evening"])
        missed = (due is not None and now - due > DUE_GRACE
                  and (schedule["last_due_at"] is None or schedule["last_due_at"] < due))
    if hours is not None or missed:
        causes.open_alert(conn, "stale", causes.text_stale(hours))
    else:
        causes.close_alert(conn, "stale")


def pending(conn, now: datetime | None = None) -> list[dict]:
    """Сообщения к отправке: первое, напоминание раз в сутки, восстановление."""
    now = now or datetime.now(timezone.utc)
    with conn.transaction():
        check_stale(conn, now)
    rows = conn.execute(
        "SELECT * FROM alert WHERE (state = 'open' AND (sent_count = 0 OR (kind = 'action' AND last_sent_at < %s))) "
        "OR (state = 'closed' AND NOT recovery_sent) ORDER BY id", (now - REMIND_AFTER,)).fetchall()
    conn.commit()
    messages = []
    for row in rows:
        first_line = row["body"].split("\n", 1)[0]
        if row["state"] == "closed":
            text = f"Восстановлено: {first_line}\nПричина устранена, действий не требуется."
        elif row["sent_count"]:
            text = "Напоминание. " + row["body"]
        else:
            text = row["body"]
        messages.append({"id": row["id"], "send_no": row["sent_count"] + 1, "text": text})
    return messages


def mark_sent(conn, alert_id: int, send_no: int, now: datetime | None = None) -> bool:
    """Отметить отправку. Повтор с тем же номером отправки ничего не меняет."""
    now = now or datetime.now(timezone.utc)
    with conn.transaction():
        row = conn.execute(
            "UPDATE alert SET sent_count = %(n)s, last_sent_at = %(now)s, "
            "recovery_sent = recovery_sent OR state = 'closed' OR kind = 'info', "
            "state = CASE WHEN kind = 'info' THEN 'closed' ELSE state END, "
            "closed_at = CASE WHEN kind = 'info' THEN %(now)s ELSE closed_at END "
            "WHERE id = %(id)s AND sent_count = %(n)s - 1 RETURNING 1",
            {"id": alert_id, "n": send_no, "now": now}).fetchone()
    return row is not None
