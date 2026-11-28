"""Команды оператора распознавания (contracts/operator-commands.md).

    python -m ecoprofi.asr worker add <имя> | revoke <имя> | list
    python -m ecoprofi.asr schedule show | set --days 7 --at 01:00 --window 8h [--catch-up on|off]
    python -m ecoprofi.asr pause | resume | run-now [--only <owner>_<item>[,…]]
    python -m ecoprofi.asr status | jobs [--state S]
    python -m ecoprofi.asr requeue <owner>_<item> | reprocess --processor-version V
    python -m ecoprofi.asr probe <owner>_<item>

Флаг --json перед командой печатает JSON. Ключ распознавателя печатается
только один раз командой worker add; временные адреса не печатаются никогда.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

from ecoprofi.asr import PROCESSOR_VERSION, jobs, window, workers
from ecoprofi.duetime import MOSCOW

OK, PARTIAL, USAGE = 0, 1, 2
LABELS = {"recognized": "распознано", "waiting": "ждёт", "audio_unavailable": "звук недоступен",
          "no_speech": "речь не найдена", "temporary": "сбой", "permanent": "постоянный сбой",
          "replaced_by_subtitles": "заменено субтитрами", "removed_deleted": "снято после удаления"}
WEEKDAYS = {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс"}


def _connect():
    from ecoprofi.db import connect
    conn = connect(vector=False)
    conn.autocommit = True
    return conn


def _local(value) -> str:
    return value.astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M") if value else "—"


def _hours(seconds) -> float:
    return round(float(seconds or 0) / 3600, 1)


def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _out(args, data: dict, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False, default=_default, indent=1) if args.json else text)


def schedule_line(sched: dict) -> str:
    days = ",".join(WEEKDAYS[d] for d in sched["weekdays"])
    hours = sched["window_length"].total_seconds() / 3600
    return (f"{days} {sched['start_at']:%H:%M} МСК, окно {hours:g} ч, догон {'вкл.' if sched['catch_up'] else 'выкл.'}"
            + (" · ПАУЗА" if sched["paused"] else ""))


def indexer_line(conn) -> str:
    row = conn.execute("SELECT morning, evening, enabled FROM run_schedule").fetchone()
    if row is None:
        return "индексатор: расписание не задано"
    times = f"{row['morning']:%H:%M}" + (f" и {row['evening']:%H:%M}" if row["evening"] else "")
    return f"индексатор: {times} МСК" + ("" if row["enabled"] else " (выключен)")


def build_status(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sched = jobs.schedule(conn)
    counts = {key: 0 for key in LABELS}
    for row in conn.execute("SELECT state, reason, count(*) AS n FROM asr_job GROUP BY state, reason").fetchall():
        counts[jobs.outcome_of(row["state"], row["reason"])] += row["n"]
    initial = conn.execute(
        "SELECT count(*) AS total, count(*) FILTER (WHERE state = ANY(%s)) AS done FROM asr_job WHERE initial",
        (list(jobs.FINAL_INITIAL),)).fetchone()
    last_run = conn.execute("SELECT * FROM asr_run ORDER BY id DESC LIMIT 1").fetchone()
    totals = conn.execute("SELECT coalesce(sum(audio_seconds), 0) AS audio, "
                          "coalesce(sum(processing_seconds), 0) AS processing FROM asr_run").fetchone()
    worker = conn.execute("SELECT name, last_seen_at, device FROM asr_worker WHERE revoked_at IS NULL "
                          "ORDER BY last_seen_at DESC NULLS LAST LIMIT 1").fetchone()
    speed = (float(totals["audio"]) / float(totals["processing"])) if totals["processing"] else None
    return {"initial_pass": sched["initial_pass"], "initial_started_at": sched["initial_started_at"],
            "initial_done_at": sched["initial_done_at"], "initial_done": initial["done"],
            "initial_total": initial["total"], "outcomes": counts, "last_run": last_run,
            "worker": worker, "audio_hours": _hours(totals["audio"]),
            "processing_hours": _hours(totals["processing"]), "speed": round(speed, 1) if speed else None,
            "schedule": schedule_line(sched), "indexer": indexer_line(conn),
            "next_due_at": window.next_due_at(sched, now), "now": now}


def status_text(s: dict) -> str:
    phase = {"pending": "не начат", "running": "идёт", "done": "завершён"}[s["initial_pass"]]
    lines = [f"Первичный проход: {phase}, {s['initial_done']} из {s['initial_total']}"
             + (f" (начат {_local(s['initial_started_at'])})" if s["initial_started_at"] else "")]
    lines.append("Исходы: " + " · ".join(f"{LABELS[k]} {v}" for k, v in s["outcomes"].items()))
    run = s["last_run"]
    if run:
        lines.append(f"Последний запуск #{run['id']} ({run['kind']}): {_local(run['started_at'])}–"
                     f"{_local(run['finished_at']) if run['finished_at'] else '…'}"
                     f" · исход {run['outcome'] or 'идёт'} · взято {run['taken']} · опубликовано {run['published']}"
                     f" · сбоев {run['failed']}")
    else:
        lines.append("Запусков ещё не было.")
    worker = s["worker"]
    if worker:
        seen = worker["last_seen_at"]
        ago = f"{int((s['now'] - seen).total_seconds() // 60)} мин назад" if seen else "ещё не выходил на связь"
        gpu = (worker["device"] or {}).get("gpu")
        vram = (worker["device"] or {}).get("vram_total_mb")
        lines.append(f"ПК: {worker['name']}, на связи {ago}" + (f", {gpu}" if gpu else "")
                     + (f" {round(vram / 1024)} ГБ" if vram else ""))
    else:
        lines.append("ПК: распознаватель не зарегистрирован (worker add)")
    lines.append(f"Звук: {s['audio_hours']} ч · обработка: {s['processing_hours']} ч"
                 + (f" · скорость {s['speed']} ч/ч" if s["speed"] else ""))
    lines.append(f"График: {s['schedule']} · {s['indexer']}")
    lines.append(f"Следующее плановое окно: {_local(s['next_due_at'])} МСК")
    return "\n".join(lines)


def _worker(args) -> int:
    with _connect() as conn:
        if args.action == "add":
            try:
                key = workers.issue_key(conn, args.name)
            except workers.WorkerExists as exc:
                print(str(exc), file=sys.stderr)
                return USAGE
            print(f"Распознаватель {args.name} зарегистрирован. Ключ (показывается один раз):\n{key}\n"
                  "Сохраните его на ПК в secrets/asr_worker_token.")
            return OK
        if args.action == "revoke":
            if not workers.revoke(conn, args.name):
                print("Распознаватель не найден или уже отозван.", file=sys.stderr)
                return USAGE
            print(f"Ключ {args.name} отозван; действующие аренды возвращены в очередь.")
            return OK
        rows = workers.list_workers(conn)
        text = "\n".join(
            f"{r['name']}: выпущен {_local(r['created_at'])}"
            + (f", отозван {_local(r['revoked_at'])}" if r["revoked_at"] else "")
            + f", связь {_local(r['last_seen_at'])}"
            + (f", {(r['device'] or {}).get('gpu')}" if (r["device"] or {}).get("gpu") else "")
            for r in rows) or "Распознаватели не зарегистрированы."
        _out(args, {"workers": rows}, text)
    return OK


def _schedule(args) -> int:
    with _connect() as conn:
        if args.action == "set":
            changes = {}
            try:
                if args.days:
                    changes["weekdays"] = window.parse_days(args.days)
                if args.at:
                    changes["start_at"] = window.parse_at(args.at)
                if args.window:
                    changes["window_length"] = window.parse_window(args.window)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return USAGE
            if args.catch_up:
                changes["catch_up"] = args.catch_up == "on"
            if not changes:
                print("Укажите --days, --at, --window или --catch-up.", file=sys.stderr)
                return USAGE
            sets = ", ".join(f"{k} = %({k})s" for k in changes)
            conn.execute(f"UPDATE asr_schedule SET {sets}", changes)
        sched = jobs.schedule(conn)
        phase = {"pending": "не начат", "running": "идёт", "done": "завершён"}[sched["initial_pass"]]
        now = datetime.now(timezone.utc)
        data = {"asr": sched, "indexer": indexer_line(conn), "next_due_at": window.next_due_at(sched, now)}
        _out(args, data, f"Распознавание: {schedule_line(sched)}\nПервичный проход: {phase}\n"
                         f"Следующее плановое окно: {_local(data['next_due_at'])} МСК\n"
                         f"{indexer_line(conn).capitalize()} — графики независимы.")
    return OK


def _pause(args) -> int:
    with _connect() as conn:
        conn.execute("UPDATE asr_schedule SET paused = %s", (args.command == "pause",))
    print("Автоматические запуски распознавания приостановлены, включая первичный проход."
          if args.command == "pause" else "Автоматические запуски распознавания возобновлены.")
    return OK


def _run_now(args) -> int:
    only = None
    if args.only:
        try:
            only = [jobs.ref(*jobs.parse_ref(part)) for part in args.only.split(",") if part.strip()]
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return USAGE
    with _connect() as conn, conn.transaction():
        if jobs.open_run(conn):
            print("Запуск распознавания уже идёт; внеочередной запуск не нужен.", file=sys.stderr)
            return PARTIAL
        if only:
            found = {r["ref"] for r in conn.execute(
                "SELECT owner_id::text || '_' || item_id::text AS ref FROM asr_job "
                "WHERE (owner_id::text || '_' || item_id::text) = ANY(%s)", (only,)).fetchall()}
            missing = [r for r in only if r not in found]
            if missing:
                print("Нет заданий распознавания для: " + ", ".join(missing), file=sys.stderr)
                return USAGE
        conn.execute("UPDATE asr_schedule SET manual_requested_at = now(), manual_only = %s", (only,))
    print("Внеочередной запуск начнётся при следующем выходе ПК на связь (до 5 минут)"
          + (f"; только записи: {', '.join(only)} (пауза не мешает)." if only else "."))
    return OK


def _status(args) -> int:
    with _connect() as conn:
        data = build_status(conn)
        _out(args, data, status_text(data))
    return OK


def _jobs(args) -> int:
    with _connect() as conn:
        sql = ("SELECT owner_id, item_id, state, reason, initial, attempts, failed_runs, retry_at, duration_s, "
               "updated_at FROM asr_job")
        rows = conn.execute(sql + (" WHERE state = %s" if args.state else "") + " ORDER BY updated_at",
                            (args.state,) if args.state else ()).fetchall()
        text = "\n".join(
            f"{jobs.ref(r['owner_id'], r['item_id'])}: {r['state']}"
            + (f" ({r['reason']})" if r["reason"] else "") + (" · начальная" if r["initial"] else "")
            + (f" · попыток {r['attempts']}, запусков со сбоем {r['failed_runs']}" if r["attempts"] or r["failed_runs"]
               else "") + (f" · {r['duration_s'] // 60} мин" if r["duration_s"] else "")
            for r in rows) or "Заданий нет."
        _out(args, {"jobs": rows}, text)
    return OK


def _requeue(args) -> int:
    try:
        owner, item = jobs.parse_ref(args.ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    with _connect() as conn:
        if not jobs.requeue(conn, owner, item):
            print("Задание не найдено или сейчас выполняется.", file=sys.stderr)
            return USAGE
    print(f"{jobs.ref(owner, item)} возвращена в очередь распознавания.")
    return OK


def _reprocess(args) -> int:
    with _connect() as conn:
        count = jobs.reprocess(conn, args.processor_version)
    print(f"Поставлено на переобработку: {count}. Прежние версии остаются в поиске до новой публикации.")
    return OK


def _probe(args) -> int:
    from ecoprofi.asr import audio
    from ecoprofi.indexer.context import make_vk
    try:
        owner, item = jobs.parse_ref(args.ref)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    result = audio.probe(make_vk(), owner, item)
    if result["outcome"] == "ok":
        text = (f"{result['ref']}: поля медиа {', '.join(result['fields'])}; выбрано {result['chosen']}; "
                f"отдельная звуковая дорожка: {'да' if result['separate_audio'] else 'нет'}; "
                f"длительность {result['duration_s']} с; звук {result['audio_bytes'] / 1e6:.1f} МБ; "
                f"подготовка {result['prepare_seconds']} с. Файл удалён.")
    else:
        text = f"{result['ref']}: {result['outcome']} ({result.get('reason') or result.get('error')})"
    _out(args, result, text)
    return OK if result["outcome"] == "ok" else PARTIAL


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ecoprofi.asr")
    parser.add_argument("--json", action="store_true", help="вывод в JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    worker = commands.add_parser("worker", help="ключи распознавателя")
    worker_actions = worker.add_subparsers(dest="action", required=True)
    worker_actions.add_parser("add").add_argument("name")
    worker_actions.add_parser("revoke").add_argument("name")
    worker_actions.add_parser("list")

    schedule = commands.add_parser("schedule", help="график распознавания")
    schedule_actions = schedule.add_subparsers(dest="action", required=True)
    schedule_actions.add_parser("show")
    set_cmd = schedule_actions.add_parser("set")
    set_cmd.add_argument("--days", help="ISO-дни через запятую, 7 — воскресенье")
    set_cmd.add_argument("--at", help="ЧЧ:ММ по Москве")
    set_cmd.add_argument("--window", help="8h, 90m, 1h30m")
    set_cmd.add_argument("--catch-up", choices=("on", "off"))

    commands.add_parser("pause")
    commands.add_parser("resume")
    run_now = commands.add_parser("run-now", help="внеочередной запуск")
    run_now.add_argument("--only", help="только эти записи: <owner>_<item>[,…]; работает и при паузе")
    commands.add_parser("status")
    jobs_cmd = commands.add_parser("jobs")
    jobs_cmd.add_argument("--state", choices=sorted(jobs.ALLOWED))
    commands.add_parser("requeue").add_argument("ref")
    reprocess = commands.add_parser("reprocess")
    reprocess.add_argument("--processor-version", default=PROCESSOR_VERSION)
    commands.add_parser("probe").add_argument("ref")

    args = parser.parse_args(argv)
    handler = {"worker": _worker, "schedule": _schedule, "pause": _pause, "resume": _pause, "run-now": _run_now,
               "status": _status, "jobs": _jobs, "requeue": _requeue, "reprocess": _reprocess,
               "probe": _probe}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
