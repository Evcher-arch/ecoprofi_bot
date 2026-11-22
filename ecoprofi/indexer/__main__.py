"""Команды индексатора (specs/003-autonomous-indexer/contracts/operator-commands.md):

    python -m ecoprofi.indexer access login | check | forget | probe POST_ID...
    python -m ecoprofi.indexer run full [--portion N] | run update | run cancel
    python -m ecoprofi.indexer requeue <ключ материала>
    python -m ecoprofi.indexer schedule show | set ЧЧ:ММ [ЧЧ:ММ] | on | off
    python -m ecoprofi.indexer status

Команды функций 001–002 сохраняются:

    python -m ecoprofi.indexer --schedule-url <адрес> | --schedule-file <путь>
    python -m ecoprofi.indexer --materials [--only <ключ материала>]
    python -m ecoprofi.indexer --report <путь.md>
    python -m ecoprofi.indexer --control-posts 41120 30861

Доступ оператора берётся из хранилища на томе индексатора; вход выполняется
один раз командой access login. Значения доступа, код входа и адреса файлов
не печатаются.
"""
import argparse
import getpass
import os
import sys
import time
from pathlib import Path

from ecoprofi.ai import AIError
from ecoprofi.db import connect
from ecoprofi.duetime import ScheduleError
from ecoprofi.indexer import runs
from ecoprofi.indexer.publish import index_video, unpublish_post
from ecoprofi.indexer.report import build_report
from ecoprofi.indexer.retry import SourceAuth, SourceDeleted, SourceError, with_retry
from ecoprofi.indexer.schedule import ScheduleError as ScheduleParseError
from ecoprofi.indexer.schedule import fetch_schedule, parse_schedule, sync_schedule
from ecoprofi.indexer.vk_sources import CONTROL_SCHEDULE
from ecoprofi.textkeys import material_key, parse_material_key

OK, PARTIAL, USAGE, LOGIN_REQUIRED, LIMIT, BUSY = 0, 1, 2, 3, 4, 5
NEED_LOGIN = "Нужен вход оператора: python -m ecoprofi.indexer access login"


def _schedule_lecture(conn, post_id: int):
    """Строка расписания, к которой относится публикация (тема лекции важнее доп. материала)."""
    row = conn.execute(
        "SELECT lp.lecture_id FROM lecture_post lp JOIN lecture l ON l.id = lp.lecture_id "
        "WHERE lp.post_id = %s AND l.state <> 'orphaned' "
        "ORDER BY (lp.row_role = 'lecture') DESC, l.lecture_date, l.id LIMIT 1", (post_id,)).fetchone()
    return row["lecture_id"] if row else None


def run_posts(conn, ai, vk, post_ids, schedule_map, *, sleep=time.sleep) -> list[dict]:
    """Обработать записи контрольных постов. Сбой одного поста не останавливает остальные."""
    results = []
    for post_id in post_ids:
        # После разбора расписания пост относится к настоящей строке, а не к условной.
        schedule = _schedule_lecture(conn, post_id) or schedule_map.get(post_id)
        if schedule is None:
            results.append({"ref": f"post {post_id}", "outcome": "failed:schedule",
                            "error": "Пост не связан со строкой расписания."})
            continue
        try:
            videos, _ = with_retry(lambda attempt: vk.post_videos(post_id), sleep=sleep)
        except SourceDeleted:
            # Подтверждённое удаление поста исключает его материалы из поиска.
            unpublish_post(conn, post_id)
            results.append({"ref": f"post {post_id}", "outcome": "deleted"})
            continue
        except SourceError as exc:
            state = "retry" if exc.temporary else "failed"
            results.append({"ref": f"post {post_id}", "outcome": f"{state}:{exc.reason}", "error": str(exc)})
            continue
        for source in videos:
            results.append(index_video(conn, ai, vk, source, schedule, sleep=sleep))
    return results


def pilot_posts(conn) -> dict[int, int]:
    """Публикации лекций из выборки пилота: номер публикации → лекция."""
    rows = conn.execute(
        "SELECT lp.post_id, l.id AS lecture_id FROM pilot_lecture pl "
        "JOIN lecture l ON l.schedule_row = pl.schedule_row "
        "JOIN lecture_post lp ON lp.lecture_id = l.id "
        "ORDER BY l.lecture_date, l.id, (lp.row_role = 'lecture') DESC, lp.post_id").fetchall()
    posts: dict[int, int] = {}
    for row in rows:
        posts.setdefault(row["post_id"], row["lecture_id"])
    return posts


def run_materials(conn, ai, vk, *, only: str | None = None, sleep=time.sleep, recognize=None) -> list[dict]:
    """Обработать документы и записи лекций из выборки пилота (режим 002)."""
    posts = pilot_posts(conn)
    if not posts:
        return [{"ref": "выборка", "outcome": "failed:schedule",
                 "error": "Выборка пилота пуста: обновите расписание и импортируйте раздел lectures."}]
    conn.commit()
    try:
        return runs.process_posts(conn, ai, vk, posts, only=only, sleep=sleep, recognize=recognize)
    except SourceError as exc:
        conn.rollback()
        state = "retry" if exc.temporary else "failed"
        return [{"ref": "лента", "outcome": f"{state}:{exc.reason}", "error": str(exc)}]


_OK = ("published", "unchanged", "deleted", "mention")


def _line(r: dict) -> str:
    line = f"{r.get('key') or r['ref']}: {r['outcome']}"
    if "post" in r:
        line += f" | публикация {r['post']}"
    if "kind" in r:
        line += (f" | {r['kind']} | текст {r['text_state']} | страниц {r['pages'] if r['pages'] is not None else '—'}"
                 f" | нечитаемых {r['unreadable'] or 0} | фрагментов {r['fragments']}")
        if r.get("req_state") not in (None, "absent"):
            line += f" | реквизиты {r['req_state']}"
    elif "version_id" in r:
        line += f" | версия {r['version_id']} | эпизодов {r['episodes']} | дорожка {'есть' if r['track'] else 'нет'}"
    if "note" in r:
        line += f" | {r['note']}"
    if "error" in r:
        line += f" | {r['error']}"
    if "attempts" in r:
        line += f" | попыток {r['attempts']}"
    if "operation_id" in r:
        line += f" | операция {r['operation_id']}"
    return line


def _print_results(results: list[dict]) -> bool:
    ok = True
    for r in results:
        print(_line(r))
        ok = ok and r["outcome"] in _OK
    return ok


# --- режимы 001–002 ---

def _schedule(args) -> int:
    try:
        if args.schedule_file:
            html = Path(args.schedule_file).read_text(encoding="utf-8")
        else:
            html = fetch_schedule(args.schedule_url)
        rows = parse_schedule(html)
    except (OSError, UnicodeDecodeError, ScheduleParseError) as exc:
        print(f"Расписание не разобрано, перечень не изменён: {exc}", file=sys.stderr)
        return USAGE
    with connect(vector=False) as conn:
        counts = sync_schedule(conn, rows)
    print(f"Строк расписания: {counts['total']} — с материалами {counts['with_materials']}, "
          f"в разработке {counts['planned']}. Новых строк: {counts['new']}, новых связей с публикациями: "
          f"{counts['new_links']}, осиротевших: {counts['orphaned']}, перепривязано материалов: {counts['relinked']}.")
    return OK


def _report(args) -> int:
    with connect(vector=False) as conn:
        text = build_report(conn)
    Path(args.report).write_text(text, encoding="utf-8")
    print(f"Отчёт записан: {args.report}")
    return OK


def _legacy(argv) -> int:
    parser = argparse.ArgumentParser(prog="python -m ecoprofi.indexer")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--schedule-file", metavar="ПУТЬ", help="сохранённая страница расписания")
    mode.add_argument("--schedule-url", metavar="АДРЕС", help="адрес страницы расписания")
    mode.add_argument("--materials", action="store_true", help="обработать материалы лекций из выборки пилота")
    mode.add_argument("--report", metavar="ПУТЬ", help="записать перечень лекций для редактора (Markdown)")
    mode.add_argument("--control-posts", nargs="+", type=int, metavar="POST_ID",
                      help="записи контрольных постов (режим функции 001)")
    parser.add_argument("--only", metavar="КЛЮЧ", help="с --materials: только один материал, например doc1_2")
    args = parser.parse_args(argv)
    if args.only and not args.materials:
        parser.error("--only применяется вместе с --materials")

    if args.schedule_file or args.schedule_url:
        return _schedule(args)
    if args.report:
        return _report(args)

    from ecoprofi.indexer import context
    try:
        ai = context.make_ai()
    except AIError as exc:
        print(f"Проход не начат: {exc}", file=sys.stderr)
        return USAGE
    access = context.make_access()
    if access.store.load() is None:
        print(NEED_LOGIN, file=sys.stderr)
        return LOGIN_REQUIRED
    vk = context.make_vk(access)
    with connect() as conn:
        if not runs.acquire(conn):
            print("Идёт другой проход индексатора; повторите позже.", file=sys.stderr)
            return BUSY
        try:
            if args.materials:
                results = run_materials(conn, ai, vk, only=args.only)
            else:
                results = run_posts(conn, ai, vk, args.control_posts, CONTROL_SCHEDULE)
            conn.commit()
        finally:
            runs.release(conn)
    return OK if _print_results(results) else PARTIAL


# --- доступ ---

def _access(args) -> int:
    from ecoprofi.indexer import context
    from ecoprofi.indexer.access import LoginError, describe, login_request
    access = context.make_access()
    if args.action == "forget":
        access.forget()
        print("Сохранённый доступ удалён. Источники не читаются до нового входа (access login).")
        return OK
    if args.action == "login":
        try:
            request = login_request(os.environ.get("VK_ID_APP", ""))
            print("Откройте адрес в браузере и войдите учётной записью оператора:\n")
            print(request.url + "\n")
            print("После входа браузер откроет https://localhost/callback?... — страница не загрузится, это нормально.")
            address = getpass.getpass("Вставьте адрес из строки браузера (ввод скрыт): ")
            shown = access.complete_login(request, address)
        except (LoginError, SourceError) as exc:
            print(f"Вход не состоялся: {exc}", file=sys.stderr)
            return USAGE
        print("Состав ответа VK ID (без значений): " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    vk = context.make_vk(access)
    try:
        if args.action == "probe":
            for post_id, item in vk.probe_by_id(args.posts).items():
                print(f"публикация {post_id}: " + ("не возвращена" if item is None else
                      f"возвращена, вложений {item['attachments']}, поля ленты {item['fields'] or 'нет'}"))
            return OK
        count = vk.paid_feed_count()
    except SourceAuth as exc:
        print(f"{exc}\n{NEED_LOGIN}", file=sys.stderr)
        return LOGIN_REQUIRED
    except SourceError as exc:
        print(f"Проверка не выполнена: {exc}", file=sys.stderr)
        return USAGE
    left = access.remaining()
    print(("Доступ сохранён. " if args.action == "login" else "Доступ действует. ") +
          f"Платная лента читается: постов {count}." +
          (f" Рабочему токену осталось {int(left.total_seconds() // 60)} мин." if left else ""))
    return OK


# --- проходы ---

def _portion_report(portion: int, results: list[dict], done: int, total: int) -> None:
    counts = runs.tally(results)
    print(f"Порция {portion}: пройдено лекций {done} из {total}, осталось {total - done}. "
          f"Новых {counts['n_new']}, изменённых {counts['n_changed']}, без изменений {counts['n_unchanged']}, "
          f"отложенных {counts['n_deferred']}, сбойных {counts['n_failed']}.")
    for r in results:
        if r["outcome"] not in ("unchanged", "mention", "skipped:handled"):
            print("  " + _line(r))


def _exit_code(result: dict) -> int:
    state, reason = result["state"], result.get("stop_reason")
    if state == "busy":
        print("Идёт другой проход индексатора; повторите позже.", file=sys.stderr)
        return BUSY
    if reason == "auth":
        print(f"Проход {result['id']} остановлен: {NEED_LOGIN}. После входа он продолжится с того же места.",
              file=sys.stderr)
        return LOGIN_REQUIRED
    if reason == "paid_feed":
        print(f"Проход {result['id']} остановлен: {result.get('error')}. Платная лента не читается доступом "
              "оператора — проверьте его подписку на клуб и права в сообществе. Каталог не изменён.",
              file=sys.stderr)
        return LOGIN_REQUIRED
    if state in ("paused", "stopped"):
        print(f"Проход {result['id']} остановлен по лимиту ({reason}); продолжится следующим запуском.",
              file=sys.stderr)
        return LIMIT
    if state == "failed":
        print(f"Проход {result['id']} не завершён: {result.get('error')}. Следующий запуск продолжит его.",
              file=sys.stderr)
        return PARTIAL
    return PARTIAL if result.get("n_deferred") or result.get("n_failed") else OK


def _run(args) -> int:
    from ecoprofi.indexer import context
    from ecoprofi.indexer.outcomes import STATES, TITLES, material_outcomes
    if args.action == "cancel":
        with connect(vector=False) as conn:
            run_id = runs.cancel(conn)
        print(f"Проход {run_id} отменён." if run_id else "Незавершённого прохода нет.")
        return OK
    try:
        ai = context.make_ai()
    except AIError as exc:
        print(f"Проход не начат: {exc}", file=sys.stderr)
        return USAGE
    access = context.make_access()
    if access.store.load() is None:
        print(NEED_LOGIN, file=sys.stderr)
        return LOGIN_REQUIRED
    vk = context.make_vk(access)
    with connect() as conn:
        runs.reconcile(conn)
        if args.action == "full":
            result = runs.run(conn, ai, vk, "full", portion=args.portion, report=_portion_report)
        else:
            result = context.update_run(conn, ai, vk, "manual", report=_portion_report)
        if result["state"] == "done":
            counts = material_outcomes(conn)["counts"]
            print(f"Проход {result['id']} завершён. " + "; ".join(f"{TITLES[s]} — {counts[s]}" for s in STATES))
            if result.get("schedule_outcome") == "failed":
                print("Расписание лекций не прочитано: перечень не изменён.", file=sys.stderr)
    return _exit_code(result)


def _requeue(args) -> int:
    key = parse_material_key(args.key)
    if key is None or not args.key.startswith(("video", "doc")):
        print("Ключ материала задаётся как video<владелец>_<номер> или doc<владелец>_<номер>.", file=sys.stderr)
        return USAGE
    with connect(vector=False) as conn:
        if not runs.requeue(conn, key):
            print("Материал не найден или не в постоянном сбое.", file=sys.stderr)
            return USAGE
    print(f"{material_key(*key)} возвращён в очередь: будет обработан ближайшим проходом.")
    return OK


def _schedule_cmd(args) -> int:
    from ecoprofi.indexer import daemon
    from ecoprofi.indexer.status import build_status
    with connect(vector=False) as conn:
        try:
            if args.action == "set":
                morning, evening = daemon.schedule_set(conn, args.times)
                print(f"Время запусков: {morning:%H:%M}" + (f" и {evening:%H:%M}" if evening else "") +
                      " по Москве.")
            elif args.action == "on":
                daemon.schedule_on(conn)
                print("Автоматические запуски включены. Нужен работающий сервис indexer-daemon.")
            elif args.action == "off":
                daemon.schedule_off(conn)
                print("Автоматические запуски приостановлены.")
        except ScheduleError as exc:
            print(str(exc), file=sys.stderr)
            return USAGE
        print(next(line for line in build_status(conn).splitlines() if line.startswith("Расписание запусков")))
    return OK


def _status(args) -> int:
    from ecoprofi.indexer.status import build_status
    with connect(vector=False) as conn:
        print(build_status(conn))
    return OK


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        return _legacy(argv)
    parser = argparse.ArgumentParser(prog="python -m ecoprofi.indexer")
    commands = parser.add_subparsers(dest="command", required=True)

    access = commands.add_parser("access", help="вход оператора и сохранённый доступ")
    access_actions = access.add_subparsers(dest="action", required=True)
    access_actions.add_parser("login", help="войти через VK ID и сохранить доступ")
    access_actions.add_parser("check", help="продлить при близком сроке и проверить чтение ленты")
    access_actions.add_parser("forget", help="удалить сохранённый доступ")
    probe = access_actions.add_parser("probe", help="проба wall.getById для публикаций")
    probe.add_argument("posts", nargs="+", type=int, metavar="POST_ID")

    run = commands.add_parser("run", help="проходы индексатора")
    run_actions = run.add_subparsers(dest="action", required=True)
    full = run_actions.add_parser("full", help="полный проход всех строк расписания с материалами")
    full.add_argument("--portion", type=int, metavar="N", help="лекций в порции (по умолчанию RUN_PORTION)")
    run_actions.add_parser("update", help="внеочередное обновление")
    run_actions.add_parser("cancel", help="отменить незавершённый проход")

    requeue = commands.add_parser("requeue", help="вернуть материал из постоянного сбоя")
    requeue.add_argument("key", metavar="КЛЮЧ")

    schedule = commands.add_parser("schedule", help="расписание автоматических запусков")
    schedule_actions = schedule.add_subparsers(dest="action", required=True)
    schedule_actions.add_parser("show")
    set_cmd = schedule_actions.add_parser("set", help="утреннее и вечернее время, ЧЧ:ММ [ЧЧ:ММ]")
    set_cmd.add_argument("times", nargs="+", metavar="ЧЧ:ММ")
    schedule_actions.add_parser("on")
    schedule_actions.add_parser("off")

    commands.add_parser("status", help="сводка состояния")
    args = parser.parse_args(argv)
    if args.command == "run" and args.action == "full" and args.portion is not None and args.portion < 1:
        parser.error("--portion должен быть не меньше 1")
    handler = {"access": _access, "run": _run, "requeue": _requeue, "schedule": _schedule_cmd,
               "status": _status}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
