"""Команды индексатора (contracts/operator-commands.md):

    python -m ecoprofi.indexer --schedule-url <адрес> | --schedule-file <путь>
    python -m ecoprofi.indexer --materials [--only <ключ материала>]
    python -m ecoprofi.indexer --report <путь.md>
    python -m ecoprofi.indexer --control-posts 41120 30861

Токен оператора запрашивается скрытым вводом, в аргументы, окружение и журнал не попадает.
Расписанию и отчёту токен не нужен.
"""
import argparse
import sys
import time
from pathlib import Path

from ecoprofi.ai import AIError, YandexAI
from ecoprofi.db import connect
from ecoprofi.indexer.publish import index_document, index_video, unpublish_post
from ecoprofi.indexer.report import build_report
from ecoprofi.indexer.retry import SourceDeleted, SourceError, with_retry
from ecoprofi.indexer.schedule import ScheduleError, fetch_schedule, parse_schedule, sync_schedule
from ecoprofi.indexer.vk_sources import CONTROL_SCHEDULE, VkSources, read_token_hidden
from ecoprofi.textkeys import material_key


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
    """Обработать документы и записи лекций из выборки. Сбой одного материала не останавливает остальные."""
    posts = pilot_posts(conn)
    if not posts:
        return [{"ref": "выборка", "outcome": "failed:schedule",
                 "error": "Выборка пилота пуста: обновите расписание и импортируйте раздел lectures."}]
    try:
        found, _ = with_retry(lambda attempt: vk.posts_materials(posts, sleep=sleep), sleep=sleep)
    except SourceError as exc:
        state = "retry" if exc.temporary else "failed"
        return [{"ref": "лента", "outcome": f"{state}:{exc.reason}", "error": str(exc)}]

    results = []
    for post_id, lecture_id in posts.items():
        post = found[post_id]
        if only is not None and (not post.found or post.deleted):
            continue    # повтор одного материала не трогает остальные публикации
        if not post.found:
            # Пропуск в ленте требует проверки и удаление не подтверждает.
            results.append({"ref": f"post {post_id}", "outcome": "failed:empty",
                            "error": "Публикация не возвращена лентой."})
            continue
        if post.deleted:
            unpublish_post(conn, post_id)
            results.append({"ref": f"post {post_id}", "outcome": "deleted"})
            continue
        if not post.videos and not post.docs and only is None:
            results.append({"ref": f"post {post_id}", "outcome": "unchanged", "note": "вложений нет"})
        for source in post.videos:
            if only in (None, material_key("video", source.owner_id, source.video_id)):
                results.append(dict(index_video(conn, ai, vk, source, lecture_id, sleep=sleep), post=post_id))
        for source in post.docs:
            if only in (None, material_key("doc", source.owner_id, source.doc_id)):
                results.append(dict(index_document(conn, ai, vk, source, lecture_id, sleep=sleep,
                                                   recognize=recognize), post=post_id))
        for kind, owner_id, item_id in post.mentions:
            if only is not None:
                continue
            # Упоминание не выдаётся за вложение: связь появляется только у материала из каталога.
            linked = conn.execute(
                "INSERT INTO material_link (lecture_id, material_id, post_id, link_type, paid_feed) "
                "SELECT %s, m.id, %s, 'mention', %s FROM material m "
                "WHERE m.item_type = %s AND m.owner_id = %s AND m.item_id = %s "
                "ON CONFLICT (lecture_id, material_id, post_id) DO NOTHING RETURNING 1",
                (lecture_id, post_id, post.paid_feed, kind, owner_id, item_id)).fetchone()
            results.append({"ref": material_key(kind, owner_id, item_id), "post": post_id,
                            "outcome": "mention", "note": "связь сохранена" if linked else "материала нет в каталоге"})
        conn.commit()
    if only is not None and not any(r.get("ref") == only for r in results):
        results.append({"ref": only, "outcome": "failed:empty", "error": "Материал не найден в публикациях выборки."})
    return results


_OK = ("published", "unchanged", "deleted", "mention")


def _print_results(results: list[dict]) -> bool:
    ok = True
    for r in results:
        line = f"{r['ref']}: {r['outcome']}"
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
        print(line)
        ok = ok and r["outcome"] in _OK
    return ok


def _schedule(args) -> int:
    try:
        if args.schedule_file:
            html = Path(args.schedule_file).read_text(encoding="utf-8")
        else:
            html = fetch_schedule(args.schedule_url)
        rows = parse_schedule(html)
    except (OSError, UnicodeDecodeError, ScheduleError) as exc:
        print(f"Расписание не разобрано, перечень не изменён: {exc}", file=sys.stderr)
        return 2
    with connect(vector=False) as conn:
        counts = sync_schedule(conn, rows)
    print(f"Строк расписания: {counts['total']} — с материалами {counts['with_materials']}, "
          f"в разработке {counts['planned']}. Новых строк: {counts['new']}, новых связей с публикациями: "
          f"{counts['new_links']}, осиротевших: {counts['orphaned']}, перепривязано материалов: {counts['relinked']}.")
    return 0


def _report(args) -> int:
    with connect(vector=False) as conn:
        text = build_report(conn)
    Path(args.report).write_text(text, encoding="utf-8")
    print(f"Отчёт записан: {args.report}")
    return 0


def main(argv=None) -> int:
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

    try:
        ai = YandexAI.from_env()
        if not ai.doc_model:
            raise AIError("Не задана YANDEX_DOC_EMBEDDING_MODEL.")
        vk = VkSources(read_token_hidden())
    except (AIError, SourceError) as exc:
        print(f"Проход не начат: {exc}", file=sys.stderr)
        return 2
    with connect() as conn:
        if args.materials:
            results = run_materials(conn, ai, vk, only=args.only)
        else:
            results = run_posts(conn, ai, vk, args.control_posts, CONTROL_SCHEDULE)
    return 0 if _print_results(results) else 1


if __name__ == "__main__":
    sys.exit(main())
