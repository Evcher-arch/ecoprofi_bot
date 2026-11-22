"""Список работ планового обновления: только новое, изменившееся и отложенное.

Расписание лекций перечитывается, затем одним проходом по лентам вложения всех
публикаций расписания сравниваются с каталогом. Файлы при сравнении не
скачиваются: номер документа VK неизменен, замена файла в публикации даёт новый
номер, поэтому сравнения набора вложений достаточно.
"""
import time
from datetime import datetime, timezone

from ecoprofi.indexer import runs
from ecoprofi.indexer.publish import DOC_PROCESSOR_VERSION, PROCESSOR_VERSION
from ecoprofi.indexer.retry import with_retry
from ecoprofi.indexer.schedule import ScheduleError, parse_schedule, sync_schedule
from ecoprofi.textkeys import material_key


def refresh_schedule(conn, fetch, community_id: int) -> str:
    """Перечитать расписание лекций. При сбое перечень не меняется."""
    try:
        rows = parse_schedule(fetch(), community_id=community_id)
        sync_schedule(conn, rows)
        conn.commit()
        return "ok"
    except (ScheduleError, OSError, UnicodeDecodeError):
        conn.rollback()
        return "failed"


def _drop_attachment(conn, post_id: int, kind: str, owner_id: int, item_id: int) -> None:
    """Вложение убрано из публикации: связь снимается, материал без связей исключается из поиска."""
    ids = [row["material_id"] for row in conn.execute(
        "DELETE FROM material_link ml USING material m WHERE ml.material_id = m.id AND ml.post_id = %s "
        "AND ml.link_type = 'attachment' AND m.item_type = %s AND m.owner_id = %s AND m.item_id = %s "
        "RETURNING ml.material_id", (post_id, kind, owner_id, item_id)).fetchall()]
    if ids:
        conn.execute("UPDATE material m SET state = 'deleted' WHERE m.id = ANY(%s) "
                     "AND NOT EXISTS (SELECT 1 FROM material_link ml WHERE ml.material_id = m.id)", (ids,))


def update_worklist(conn, vk, ai, *, fetch, sleep=time.sleep, now=None) -> tuple[list, str]:
    """Лекции, в которых есть что обновить: [(лекция, ключи материалов)] и итог чтения расписания."""
    now = now or datetime.now(timezone.utc)
    schedule_outcome = refresh_schedule(conn, fetch, vk.owner_id)
    owners = runs.post_owners(conn)
    targets: dict[int, set] = {}

    def add(lecture_id: int, key: str) -> None:
        targets.setdefault(lecture_id, set()).add(key)

    jobs = {(j["item_type"], j["owner_id"], j["item_id"]): j for j in conn.execute(
        "SELECT item_type, owner_id, item_id, post_id, state, retry_at, waiting_source, failure_reason "
        "FROM index_job").fetchall()}
    known: dict[int, set] = {}
    for row in conn.execute(
            "SELECT ml.post_id, m.item_type, m.owner_id, m.item_id FROM material_link ml "
            "JOIN material m ON m.id = ml.material_id WHERE ml.link_type = 'attachment'").fetchall():
        known.setdefault(row["post_id"], set()).add((row["item_type"], row["owner_id"], row["item_id"]))
    conn.commit()

    def due(job) -> bool:
        if job["state"] == "permanent" or job["failure_reason"] in ("deleted", "unsupported"):
            return False
        if job["state"] == "retry":
            return job["retry_at"] is None or job["retry_at"] <= now
        return job["state"] in ("failed", "queued", "running") or job["waiting_source"]

    if owners:
        found, _ = with_retry(lambda attempt: vk.posts_materials(owners, sleep=sleep), sleep=sleep)
        runs.check_paid_feed(conn, vk)       # до снятия вложений: пустая лента не должна ничего менять
        for post_id, lecture_id in owners.items():
            post = found[post_id]
            if not post.found or post.deleted:
                # Пропуск и удаление учитывает сама обработка лекции.
                add(lecture_id, f"post:{post_id}")
                continue
            attached = {("video", s.owner_id, s.video_id) for s in post.videos} | \
                       {("doc", s.owner_id, s.doc_id) for s in post.docs}
            for item in attached - known.get(post_id, set()):
                job = jobs.get(item)
                if job is None or due(job):
                    add(lecture_id, material_key(*item))        # новое вложение либо ещё не опубликованное
            for item in known.get(post_id, set()) - attached:
                _drop_attachment(conn, post_id, *item)
        conn.commit()

    # Отложенные с наступившим сроком и записи, ждущие источника.
    for item, job in jobs.items():
        if due(job) and job["post_id"] in owners:
            add(owners[job["post_id"]], material_key(*item))

    # Материалы, опубликованные прежней версией обработчика или другой моделью.
    for row in conn.execute(
            "SELECT m.item_type, m.owner_id, m.item_id, ml.post_id FROM material m "
            "JOIN material_version v ON v.id = m.published_version_id "
            "JOIN material_link ml ON ml.material_id = m.id AND ml.link_type = 'attachment' "
            "WHERE m.state <> 'deleted' AND (v.embedding_model <> %s OR v.processor_version <> "
            "CASE WHEN m.item_type = 'video' THEN %s ELSE %s END)",
            (ai.doc_model, PROCESSOR_VERSION, DOC_PROCESSOR_VERSION)).fetchall():
        job = jobs.get((row["item_type"], row["owner_id"], row["item_id"]))
        if row["post_id"] in owners and not (job and job["state"] == "permanent"):
            add(owners[row["post_id"]], material_key(row["item_type"], row["owner_id"], row["item_id"]))

    order = {row["id"]: n for n, row in enumerate(conn.execute(
        "SELECT id FROM lecture ORDER BY lecture_date NULLS LAST, id").fetchall())}
    conn.commit()
    items = [(lecture_id, keys) for lecture_id, keys in sorted(targets.items(), key=lambda t: order.get(t[0], 0))]
    return items, schedule_outcome
