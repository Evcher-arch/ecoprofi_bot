"""Одноразовый проход индексатора: python -m ecoprofi.indexer --control-posts 41120 30861

Токен оператора запрашивается скрытым вводом, в аргументы, окружение и журнал не попадает.
"""
import argparse
import sys
import time

from ecoprofi.ai import AIError, YandexAI
from ecoprofi.db import connect
from ecoprofi.indexer.publish import index_video
from ecoprofi.indexer.retry import SourceDeleted, SourceError, with_retry
from ecoprofi.indexer.vk_sources import CONTROL_SCHEDULE, VkSources, read_token_hidden


def run_posts(conn, ai, vk, post_ids, schedule_map, *, sleep=time.sleep) -> list[dict]:
    """Обработать посты. Сбой одного поста не останавливает остальные."""
    results = []
    for post_id in post_ids:
        schedule = schedule_map.get(post_id)
        if schedule is None:
            results.append({"ref": f"post {post_id}", "outcome": "failed:schedule",
                            "error": "Пост не связан со строкой расписания."})
            continue
        try:
            videos, _ = with_retry(lambda attempt: vk.post_videos(post_id), sleep=sleep)
        except SourceDeleted:
            # Подтверждённое удаление поста исключает его материалы из поиска.
            with conn.transaction():
                conn.execute("UPDATE material SET state = 'deleted' WHERE owner_id = %s AND post_id = %s",
                             (vk.owner_id, post_id))
            results.append({"ref": f"post {post_id}", "outcome": "deleted"})
            continue
        except SourceError as exc:
            state = "retry" if exc.temporary else "failed"
            results.append({"ref": f"post {post_id}", "outcome": f"{state}:{exc.reason}", "error": str(exc)})
            continue
        for source in videos:
            results.append(index_video(conn, ai, vk, source, schedule, sleep=sleep))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ecoprofi.indexer")
    parser.add_argument("--control-posts", nargs="+", type=int, required=True, metavar="POST_ID",
                        help="номера контрольных постов сообщества")
    args = parser.parse_args(argv)
    try:
        ai = YandexAI.from_env()
        if not ai.doc_model:
            raise AIError("Не задана YANDEX_DOC_EMBEDDING_MODEL.")
        vk = VkSources(read_token_hidden())
    except (AIError, SourceError) as exc:
        print(f"Проход не начат: {exc}", file=sys.stderr)
        return 2
    with connect() as conn:
        results = run_posts(conn, ai, vk, args.control_posts, CONTROL_SCHEDULE)
    ok = True
    for r in results:
        line = f"{r['ref']}: {r['outcome']}"
        if "version_id" in r:
            line += f" | версия {r['version_id']} | эпизодов {r['episodes']} | дорожка {'есть' if r['track'] else 'нет'}"
        if "error" in r:
            line += f" | {r['error']}"
        if "operation_id" in r:
            line += f" | операция {r['operation_id']}"
        print(line)
        ok = ok and r["outcome"] in ("published", "unchanged", "deleted")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
