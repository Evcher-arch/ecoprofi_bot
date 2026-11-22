"""Сводка состояния индексатора: python -m ecoprofi.indexer status

Значений доступа, адресов и текста материалов в сводке нет.
"""
from datetime import datetime, timezone

from ecoprofi.duetime import MOSCOW, next_due
from ecoprofi.indexer import runs
from ecoprofi.indexer.outcomes import STATES, TITLES, material_outcomes

_ACCESS = {"absent": "не сохранён", "active": "действует", "login_required": "нужен вход"}
_KIND = {"full": "полный проход", "scheduled": "плановое обновление", "manual": "внеочередное обновление"}
_STATE = {
    "running": "идёт", "interrupted": "прерван, продолжится следующим запуском", "done": "завершён",
    "paused": "приостановлен", "stopped": "остановлен", "failed": "сбой", "skipped": "пропущен",
    "cancelled": "отменён",
}
_REASON = {"auth": "нужен вход", "rate_limit": "ограничение частоты VK", "ai_limit": "лимит внешней обработки",
           "paid_feed": "платная лента не читается доступом оператора",
           "schedule": "расписание не прочитано", "busy": "шёл другой проход"}


def _at(value) -> str:
    return value.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M") if value else "—"


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def index_age(conn, now: datetime):
    """Время с последнего успешно завершённого прохода."""
    row = conn.execute("SELECT max(finished_at) AS at FROM index_run WHERE state = 'done'").fetchone()
    return (now - row["at"]) if row and row["at"] else None


def build_status(conn, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    runs.reconcile(conn)
    out = ["Сводка индексатора на " + _at(now) + " (время московское)", ""]

    access = conn.execute("SELECT * FROM operator_access").fetchone()
    line = f"Доступ: {_ACCESS[access['state']]}"
    if access["state"] == "active":
        left = (access["access_expires_at"] - now).total_seconds() if access["access_expires_at"] else None
        line += f"; вход {_at(access['logged_in_at'])}; последнее продление {_at(access['refreshed_at'])}"
        if left is not None:
            line += f"; рабочему токену осталось {_span(left)}" if left > 0 else "; рабочий токен истёк, будет продлён"
        if access["refresh_expires_at"]:
            line += f"; токен обновления до {_at(access['refresh_expires_at'])}"
    elif access["last_denial"]:
        line += f" ({access['last_denial']})"
    out.append(line)
    out.append("Проверено для автоматических запусков: продление — "
               f"{'да' if access['refresh_verified_at'] else 'нет'}, повторный вход — "
               f"{'да' if access['relogin_verified_at'] else 'нет'}")

    last = conn.execute("SELECT * FROM index_run WHERE state <> 'skipped' ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        line = f"Последний проход: № {last['id']}, {_KIND[last['kind']]}, {_STATE[last['state']]}"
        if last["stop_reason"]:
            line += f" ({_REASON.get(last['stop_reason'], last['stop_reason'])})"
        if last["error"]:
            line += f" ({last['error']})"
        line += f"; начат {_at(last['started_at'])}"
        progress = conn.execute(
            "SELECT count(*) FILTER (WHERE state = 'done') AS done, count(*) AS total, "
            "min(portion_no) FILTER (WHERE state = 'pending') AS portion FROM run_lecture WHERE run_id = %s",
            (last["id"],)).fetchone()
        line += f"; лекций пройдено {progress['done']} из {progress['total']}"
        if progress["portion"]:
            line += f", осталось {progress['total'] - progress['done']}, порция {progress['portion']}"
        out.append(line)
        out.append(f"  материалы прохода: новых {last['n_new']}, изменённых {last['n_changed']}, "
                   f"без изменений {last['n_unchanged']}, отложенных {last['n_deferred']}, сбойных {last['n_failed']}")
    else:
        out.append("Последний проход: проходов не было")
    skipped = conn.execute(
        "SELECT started_at, stop_reason FROM index_run WHERE state = 'skipped' ORDER BY id DESC LIMIT 1").fetchone()
    if skipped:
        out.append(f"Последний пропущенный срок: {_at(skipped['started_at'])} "
                   f"({_REASON.get(skipped['stop_reason'], skipped['stop_reason'])})")

    done = conn.execute("SELECT id, finished_at FROM index_run WHERE state = 'done' ORDER BY finished_at DESC "
                        "LIMIT 1").fetchone()
    age = index_age(conn, now)
    out.append(f"Последний успешный проход: № {done['id']}, {_at(done['finished_at'])}; "
               f"возраст индекса {_span(age.total_seconds())}" if done else "Последний успешный проход: не было")
    feed = conn.execute("SELECT paid_feed_count FROM index_run WHERE paid_feed_count IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if feed:
        baseline = runs.last_paid_feed(conn)
        out.append(f"Платная лента: записей {feed['paid_feed_count']} при последней проверке"
                   + (f", {baseline} в последнем успешном проходе" if baseline is not None else ""))

    outcomes = material_outcomes(conn)
    out += ["", f"Материалы ({outcomes['total']}): " +
            "; ".join(f"{TITLES[s]} — {outcomes['counts'][s]}" for s in STATES)]
    deferred = [i for i in outcomes["items"]["deferred"] if i["retry_at"]]
    if deferred:
        out.append(f"Отложенные: {len(outcomes['items']['deferred'])}, ближайший повтор "
                   f"{_at(min(i['retry_at'] for i in deferred))}")
    if outcomes["items"]["permanent"]:
        out.append("Постоянный сбой: " + ", ".join(
            f"{i['key']} (публикация {i['post_id']}, {i['reason'] or 'причина не записана'})"
            for i in outcomes["items"]["permanent"]))
    missing = conn.execute("SELECT DISTINCT post_id, missed FROM lecture_post WHERE missed > 0 ORDER BY post_id").fetchall()
    if missing:
        out.append("Публикации, пропавшие из ленты: " +
                   ", ".join(f"{m['post_id']} (пропусков {m['missed']})" for m in missing))

    opened = conn.execute(
        "SELECT cause_key, kind, state, opened_at, sent_count, recovery_sent FROM alert "
        "WHERE state = 'open' OR NOT recovery_sent ORDER BY opened_at").fetchall()
    out.append("")
    if opened:
        out.append("Оповещения:")
        for alert in opened:
            if alert["state"] == "open":
                mark = "открыто" + (", не доставлено" if alert["sent_count"] == 0 else f", отправлено {alert['sent_count']}")
            else:
                mark = "закрыто, сообщение о восстановлении не доставлено"
            out.append(f"  {alert['cause_key']}: {mark}; с {_at(alert['opened_at'])}")
    else:
        out.append("Оповещения: открытых нет")

    schedule = conn.execute("SELECT * FROM run_schedule").fetchone()
    slots = schedule["morning"].strftime("%H:%M") + (f" и {schedule['evening'].strftime('%H:%M')}"
                                                     if schedule["evening"] else "")
    line = f"Расписание запусков: {slots}, {'включено' if schedule['enabled'] else 'выключено'}"
    if schedule["enabled"]:
        line += f"; следующий срок {_at(next_due(now, schedule['morning'], schedule['evening']))}"
    if schedule["last_due_at"]:
        line += f"; последний учтённый срок {_at(schedule['last_due_at'])}"
    out += ["", line]
    conn.commit()
    return "\n".join(out)
