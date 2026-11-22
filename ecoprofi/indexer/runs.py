"""Проход индексатора порциями с продолжением после остановки.

Любой проход — список лекций в порядке расписания, который обрабатывается
порциями. После каждой порции её материалы опубликованы, а место сохранено:
прерванный проход продолжается с первой незавершённой лекции и не трогает
материалы, уже обработанные в этом проходе. Одновременно идёт один проход.

Вложения ищутся заново для каждой порции: адреса файлов подписаны, живут
недолго и не сохраняются.
"""
import os
import time

from ecoprofi.ai import AILimit
from ecoprofi.indexer import alerts
from ecoprofi.indexer.outcomes import material_outcomes
from ecoprofi.indexer.publish import index_document, index_video, unpublish_post
from ecoprofi.indexer.retry import (
    PaidFeedDenied, PaidFeedLost, SourceAuth, SourceError, SourceRateLimit, with_retry,
)
from ecoprofi.textkeys import material_key

_LOCK = "ecoprofi:index-run"
OPEN = ("running", "interrupted", "paused", "stopped", "failed")
_HANDLED = ("done", "retry", "failed", "permanent")

# Публикация относится к одной лекции: тема лекции важнее дополнительного материала.
_POST_OWNER = """
    SELECT DISTINCT ON (lp.post_id) lp.post_id, lp.lecture_id, l.lecture_date
    FROM lecture_post lp JOIN lecture l ON l.id = lp.lecture_id
    WHERE l.state = 'has_materials'
    ORDER BY lp.post_id, (lp.row_role = 'lecture') DESC, l.lecture_date, l.id
"""


def portion_size() -> int:
    return max(1, int(os.environ.get("RUN_PORTION", "5")))


def paid_feed_ratio() -> float:
    return float(os.environ.get("PAID_FEED_MIN_RATIO", "0.5"))


def last_paid_feed(conn) -> int | None:
    row = conn.execute("SELECT paid_feed_count FROM index_run WHERE state = 'done' AND paid_feed_count IS NOT NULL "
                       "ORDER BY finished_at DESC LIMIT 1").fetchone()
    return row["paid_feed_count"] if row else None


def check_paid_feed(conn, vk) -> None:
    """Платная лента читается доступом оператора в прежнем объёме (FR-23).

    Ноль записей или меньше доли прежнего числа — остановка до любых изменений каталога.
    Проверка выполняется только внутри прохода; число записывается в идущий проход.
    """
    count = vk.paid_feed_seen
    if count is None:
        return                       # лента в этом обходе не запрашивалась
    previous = last_paid_feed(conn)
    if count == 0 or (previous and count < previous * paid_feed_ratio()):
        raise PaidFeedLost(count, previous)
    conn.execute("UPDATE index_run SET paid_feed_count = %s WHERE state = 'running'", (count,))
    alerts.close_alert(conn, "paid_feed")


# --- блокировка одного прохода ---

def acquire(conn) -> bool:
    got = conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", (_LOCK,)).fetchone()["ok"]
    conn.commit()
    return got


def release(conn) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_LOCK,))
    conn.commit()


def reconcile(conn) -> None:
    """Проход числится идущим, а блокировка свободна: процесс был остановлен."""
    if not acquire(conn):
        return
    try:
        conn.execute("UPDATE index_run SET state = 'interrupted' WHERE state = 'running'")
        conn.commit()
    finally:
        release(conn)


# --- учёт ---

def open_run(conn) -> dict | None:
    return conn.execute("SELECT * FROM index_run WHERE state = ANY(%s)", (list(OPEN),)).fetchone()


def post_owners(conn, lecture_ids=None) -> dict[int, int]:
    """Публикации лекций с материалами: номер публикации → лекция."""
    if lecture_ids is None:
        rows = conn.execute(f"SELECT post_id, lecture_id FROM ({_POST_OWNER}) o "
                            "ORDER BY lecture_date NULLS LAST, lecture_id, post_id").fetchall()
    else:
        rows = conn.execute(f"SELECT post_id, lecture_id FROM ({_POST_OWNER}) o WHERE lecture_id = ANY(%s) "
                            "ORDER BY lecture_date NULLS LAST, lecture_id, post_id", (list(lecture_ids),)).fetchall()
    return {row["post_id"]: row["lecture_id"] for row in rows}


def full_worklist(conn) -> tuple[list, None]:
    """Все лекции расписания с материалами в порядке расписания; выборка пилота не учитывается."""
    rows = conn.execute(
        "SELECT l.id FROM lecture l WHERE l.state = 'has_materials' "
        "AND EXISTS (SELECT 1 FROM lecture_post lp WHERE lp.lecture_id = l.id) "
        "ORDER BY l.lecture_date NULLS LAST, l.id").fetchall()
    return [(row["id"], None) for row in rows], None


def cancel(conn) -> int | None:
    """Отменить незавершённый проход: следующий запуск начнёт новый список."""
    row = conn.execute("UPDATE index_run SET state = 'cancelled', finished_at = now() "
                       "WHERE state = ANY(%s) RETURNING id", (list(OPEN),)).fetchone()
    conn.commit()
    return row["id"] if row else None


def requeue(conn, item: tuple[str, int, int]) -> bool:
    """Вернуть материал из постоянного сбоя в очередь."""
    row = conn.execute("UPDATE index_job SET state = 'queued', failed_runs = 0, retry_at = NULL, "
                       "failure_reason = NULL WHERE item_type = %s AND owner_id = %s AND item_id = %s "
                       "AND state = 'permanent' RETURNING 1", item).fetchone()
    if row:
        alerts.close_alert(conn, f"material:{material_key(*item)}")
    conn.commit()
    return row is not None


def record_skipped(conn, kind: str, reason: str) -> None:
    """Срок наступил, но проход не начат: идёт другой проход или нет доступа."""
    conn.execute("INSERT INTO index_run (kind, state, stop_reason, finished_at) VALUES (%s, 'skipped', %s, now())",
                 (kind, reason))
    conn.commit()


def finish(conn, run_id: int, state: str, reason: str | None = None, error: str | None = None) -> dict:
    run = conn.execute(
        "UPDATE index_run SET state = %s, stop_reason = %s, error = %s, finished_at = now() "
        "WHERE id = %s RETURNING *", (state, reason, error, run_id)).fetchone()
    if state == "done":
        alerts.close_alert(conn, "ai_limit")
        if run["schedule_outcome"] == "failed":
            alerts.open_alert(conn, "run", alerts.text_schedule_failed(run_id))
        else:
            alerts.close_alert(conn, "run")
        counts = material_outcomes(conn)["counts"]
        if counts["editor"]:
            alerts.open_alert(conn, "editor", alerts.text_editor(counts["editor"]))
        else:
            alerts.close_alert(conn, "editor")
        if run["kind"] == "full":
            alerts.info(conn, f"run:{run_id}", alerts.text_run_summary(run_id, counts))
    elif state == "failed":
        alerts.open_alert(conn, "run", alerts.text_run_failed(run_id, error or "непредвиденная ошибка"))
    elif reason == "ai_limit":
        alerts.open_alert(conn, "ai_limit", alerts.text_ai_limit(run_id))
    elif reason == "auth":
        alerts.open_alert(conn, "access", alerts.text_access())
    conn.commit()
    return dict(run)


# --- обработка публикаций ---

def _skip(conn, run_id: int | None, kind: str, owner_id: int, item_id: int) -> str | None:
    """Постоянный сбой автоматически не повторяется; обработанное в этом проходе не трогается."""
    if run_id is None:
        return None
    job = conn.execute("SELECT state, last_run_id FROM index_job "
                       "WHERE item_type = %s AND owner_id = %s AND item_id = %s", (kind, owner_id, item_id)).fetchone()
    if job is None:
        return None
    if job["state"] == "permanent":
        return "permanent"
    if job["last_run_id"] == run_id and job["state"] in _HANDLED:
        return "handled"
    return None


def _had_version(conn, kind: str, owner_id: int, item_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM material WHERE item_type = %s AND owner_id = %s AND item_id = %s "
        "AND published_version_id IS NOT NULL", (kind, owner_id, item_id)).fetchone() is not None


def process_posts(conn, ai, vk, posts: dict[int, int], *, only: str | None = None, run_id: int | None = None,
                  targets: dict | None = None, sleep=time.sleep, recognize=None) -> list[dict]:
    """Найти вложения публикаций и обработать материалы. Сбой одного материала не останавливает остальные.

    posts — номер публикации → лекция. targets — лекция → множество ключей материалов,
    которыми ограничена обработка (None — все). Ошибка чтения ленты пробрасывается.
    """
    found, _ = with_retry(lambda attempt: vk.posts_materials(posts, sleep=sleep), sleep=sleep)
    if run_id is not None:
        check_paid_feed(conn, vk)    # до любых изменений: пропуски, связи, признак платной ленты
    results = []
    for post_id, lecture_id in posts.items():
        post = found[post_id]
        wanted = (targets or {}).get(lecture_id)
        if only is not None and (not post.found or post.deleted):
            continue    # повтор одного материала не трогает остальные публикации
        if not post.found:
            # Пропуск в ленте требует проверки и удаление не подтверждает.
            missed = conn.execute("UPDATE lecture_post SET missed = missed + 1 WHERE post_id = %s RETURNING missed",
                                  (post_id,)).fetchone()
            alerts.open_alert(conn, f"post:{post_id}", alerts.text_post_missing(post_id, missed["missed"] if missed else 1))
            conn.commit()
            results.append({"ref": f"post {post_id}", "outcome": "failed:empty",
                            "error": "Публикация не возвращена лентой."})
            continue
        conn.execute("UPDATE lecture_post SET missed = 0 WHERE post_id = %s AND missed > 0", (post_id,))
        alerts.close_alert(conn, f"post:{post_id}")
        if post.deleted:
            unpublish_post(conn, post_id)
            conn.commit()
            results.append({"ref": f"post {post_id}", "outcome": "deleted"})
            continue
        if not post.videos and not post.docs and only is None and wanted is None:
            results.append({"ref": f"post {post_id}", "outcome": "unchanged", "note": "вложений нет"})
        sources = [("video", s.owner_id, s.video_id, s) for s in post.videos] + \
                  [("doc", s.owner_id, s.doc_id, s) for s in post.docs]
        for kind, owner_id, item_id, source in sources:
            key = material_key(kind, owner_id, item_id)
            if only not in (None, key) or (wanted is not None and key not in wanted):
                continue
            skipped = _skip(conn, run_id, kind, owner_id, item_id)
            if skipped:
                results.append({"ref": key, "post": post_id, "outcome": f"skipped:{skipped}"})
                continue
            had = _had_version(conn, kind, owner_id, item_id)
            conn.commit()
            if kind == "video":
                result = index_video(conn, ai, vk, source, lecture_id, sleep=sleep, run_id=run_id)
            else:
                result = index_document(conn, ai, vk, source, lecture_id, sleep=sleep, recognize=recognize,
                                        run_id=run_id)
            result = dict(result, post=post_id, key=key, had_version=had)
            if result["outcome"].startswith("permanent"):
                alerts.open_alert(conn, f"material:{key}",
                                  alerts.text_permanent(key, post_id, result["outcome"].partition(":")[2]))
            elif result["outcome"] in ("published", "unchanged"):
                alerts.close_alert(conn, f"material:{key}")
            conn.commit()       # материал обработан: место сохранено
            results.append(result)
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
    if only is not None and not any(r.get("ref") == only or r.get("key") == only for r in results):
        results.append({"ref": only, "outcome": "failed:empty", "error": "Материал не найден в публикациях выборки."})
    return results


def tally(results: list[dict]) -> dict:
    """Счётчики прохода по итогам материалов."""
    counts = {"n_new": 0, "n_changed": 0, "n_unchanged": 0, "n_deferred": 0, "n_failed": 0}
    for result in results:
        outcome = result["outcome"]
        if outcome == "published":
            counts["n_changed" if result.get("had_version") else "n_new"] += 1
        elif outcome == "unchanged" and "key" in result:
            counts["n_unchanged"] += 1
        elif outcome.startswith("retry"):
            counts["n_deferred"] += 1
        elif outcome.startswith(("failed", "permanent")):
            counts["n_failed"] += 1
    return counts


# --- проход ---

def _next_portion(conn, run_id: int) -> list[dict]:
    return conn.execute(
        "SELECT lecture_id, targets, portion_no FROM run_lecture WHERE run_id = %(r)s AND state = 'pending' "
        "AND portion_no = (SELECT min(portion_no) FROM run_lecture WHERE run_id = %(r)s AND state = 'pending') "
        "ORDER BY position", {"r": run_id}).fetchall()


def _progress(conn, run_id: int) -> dict:
    return conn.execute(
        "SELECT count(*) FILTER (WHERE state = 'done') AS done, count(*) AS total FROM run_lecture "
        "WHERE run_id = %s", (run_id,)).fetchone()


def run(conn, ai, vk, kind: str, *, worklist=full_worklist, portion: int | None = None,
        sleep=time.sleep, recognize=None, report=None) -> dict:
    """Начать проход либо продолжить незавершённый.

    worklist(conn) возвращает ([(лекция, ключи материалов | None)], итог чтения расписания).
    report(порция, итоги, пройдено, всего) вызывается после каждой порции.
    Возвращает строку прохода; {"state": "busy"} — идёт другой проход.
    """
    if not acquire(conn):
        return {"state": "busy", "stop_reason": "busy", "id": None}
    try:
        return _run_locked(conn, ai, vk, kind, worklist, portion or portion_size(), sleep, recognize, report)
    finally:
        try:
            conn.rollback()
        finally:
            release(conn)


def _run_locked(conn, ai, vk, kind, worklist, portion, sleep, recognize, report) -> dict:
    current = open_run(conn)
    if current and kind == "full" and current["kind"] != "full":
        # Полный проход покрывает незавершённое обновление.
        conn.execute("UPDATE index_run SET state = 'cancelled', finished_at = now() WHERE id = %s", (current["id"],))
        current = None
    if current:
        run_id = current["id"]
        conn.execute("UPDATE index_run SET state = 'running', stop_reason = NULL, error = NULL, "
                     "finished_at = NULL WHERE id = %s", (run_id,))
    else:
        run_id = conn.execute("INSERT INTO index_run (kind, state, portion_size) VALUES (%s, 'running', %s) "
                              "RETURNING id", (kind, portion)).fetchone()["id"]
    conn.commit()
    try:
        if not conn.execute("SELECT 1 FROM run_lecture WHERE run_id = %s LIMIT 1", (run_id,)).fetchone():
            conn.commit()
            items, schedule_outcome = worklist(conn)
            size = conn.execute("SELECT portion_size FROM index_run WHERE id = %s", (run_id,)).fetchone()["portion_size"]
            for position, (lecture_id, keys) in enumerate(items):
                conn.execute(
                    "INSERT INTO run_lecture (run_id, lecture_id, position, portion_no, targets) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (run_id, lecture_id, position, position // size + 1, sorted(keys) if keys is not None else None))
            conn.execute("UPDATE index_run SET schedule_outcome = %s WHERE id = %s", (schedule_outcome, run_id))
        conn.commit()
        while True:
            rows = _next_portion(conn, run_id)
            if not rows:
                break
            targets = {row["lecture_id"]: (set(row["targets"]) if row["targets"] is not None else None)
                       for row in rows}
            posts = post_owners(conn, list(targets))
            conn.commit()
            results = process_posts(conn, ai, vk, posts, run_id=run_id, targets=targets, sleep=sleep,
                                    recognize=recognize) if posts else []
            counts = tally(results)
            conn.execute("UPDATE run_lecture SET state = 'done', done_at = now() "
                         "WHERE run_id = %s AND lecture_id = ANY(%s)", (run_id, list(targets)))
            conn.execute(
                "UPDATE index_run SET n_new = n_new + %(n_new)s, n_changed = n_changed + %(n_changed)s, "
                "n_unchanged = n_unchanged + %(n_unchanged)s, n_deferred = n_deferred + %(n_deferred)s, "
                "n_failed = n_failed + %(n_failed)s WHERE id = %(id)s", dict(counts, id=run_id))
            conn.commit()       # порция завершена: её материалы в поиске, место сохранено
            if report:
                progress = _progress(conn, run_id)
                report(rows[0]["portion_no"], results, progress["done"], progress["total"])
        return finish(conn, run_id, "done")
    except SourceAuth:
        conn.rollback()
        return finish(conn, run_id, "stopped", "auth")
    except SourceRateLimit:
        conn.rollback()
        return finish(conn, run_id, "paused", "rate_limit")
    except (PaidFeedLost, PaidFeedDenied) as exc:
        conn.rollback()
        lost = exc if isinstance(exc, PaidFeedLost) else PaidFeedLost(None, last_paid_feed(conn))
        alerts.open_alert(conn, "paid_feed", alerts.text_paid_feed(lost.count, lost.previous))
        return finish(conn, run_id, "stopped", "paid_feed", error=str(lost))
    except AILimit:
        conn.rollback()
        return finish(conn, run_id, "stopped", "ai_limit")
    except SourceError as exc:
        # Лента не прочитана: порция не обработана, проход продолжится следующим запуском.
        conn.rollback()
        return finish(conn, run_id, "failed", error=f"лента не прочитана ({exc.reason})")
    except Exception as exc:
        conn.rollback()
        return finish(conn, run_id, "failed", error=f"непредвиденная ошибка ({type(exc).__name__})")
