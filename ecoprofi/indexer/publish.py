"""Обработка одного материала и атомарная публикация версии.

Новая версия готовится целиком и одной транзакцией становится опубликованной;
в той же транзакции завершается задание. Обновления одного материала
сериализуются блокировкой. Временный сбой оставляет прежнюю версию доступной.
Файл документа читается в память и после обработки не сохраняется.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from ecoprofi import telemetry
from ecoprofi.ai import AILimit
from ecoprofi.asr import jobs as asr_jobs
from ecoprofi.indexer import documents
from ecoprofi.indexer.retry import (
    SourceAuth, SourceDeleted, SourceEmpty, SourceError, SourceParse, SourceRateLimit, with_retry,
)
from ecoprofi.indexer.subtitles import SubtitleParseError, episodes_from_vtt
from ecoprofi.indexer.vk_sources import DocSource, VideoSource, post_url
from ecoprofi.textkeys import doc_numbers, material_key

PROCESSOR_VERSION = "subtitles-2"   # 2: повторы «бегущих» реплик, стыкующихся по времени
DOC_PROCESSOR_VERSION = "documents-2"   # 2: реквизиты в заголовке письма, порог читаемости страниц
RETRY_AFTER = timedelta(minutes=15)

# Причины, которые касаются всего прохода, а не одного материала: проход останавливается,
# материал возвращается в очередь без расхода счётчика постоянного сбоя.
RUN_STOPPERS = (SourceAuth, SourceRateLimit, AILimit)


def permanent_after() -> int:
    """Число проходов подряд без успеха, после которого материал считается постоянным сбоем."""
    return max(1, int(os.environ.get("PERMANENT_AFTER_RUNS", "3")))


def content_hash(source: VideoSource, track: bytes | None) -> str:
    track_hash = hashlib.sha256(track).hexdigest() if track is not None else None
    payload = json.dumps([source.title, source.description, track_hash], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def document_hash(source: DocSource, raw: bytes) -> str:
    payload = json.dumps([source.title, hashlib.sha256(raw).hexdigest()], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _item(source) -> tuple[str, int, int]:
    if isinstance(source, DocSource):
        return "doc", source.owner_id, source.doc_id
    return "video", source.owner_id, source.video_id


def _set_job(conn, source, state: str, *, reason: str | None = None,
             attempt: int | None = None, retry_at=None, run_id: int | None = None) -> None:
    item_type, owner_id, item_id = _item(source)
    conn.execute(
        """
        INSERT INTO index_job (item_type, owner_id, item_id, post_id, state, attempt, failure_reason, retry_at,
                               last_run_id)
        VALUES (%(k)s, %(o)s, %(v)s, %(p)s, %(s)s, coalesce(%(a)s, 0), %(r)s, %(t)s, %(run)s)
        ON CONFLICT (item_type, owner_id, item_id) DO UPDATE SET
            post_id = excluded.post_id, state = excluded.state,
            attempt = coalesce(%(a)s, index_job.attempt),
            failure_reason = excluded.failure_reason, retry_at = excluded.retry_at, updated_at = now(),
            last_run_id = coalesce(%(run)s, index_job.last_run_id),
            -- Успех обнуляет счётчик проходов без успеха.
            failed_runs = CASE WHEN excluded.state = 'done' THEN 0 ELSE index_job.failed_runs END
        """,
        {"k": item_type, "o": owner_id, "v": item_id, "p": source.post_id,
         "s": state, "a": attempt, "r": reason, "t": retry_at, "run": run_id},
    )


def _count_failed_run(conn, source) -> bool:
    """Учесть проход без успеха. True — материал переведён в постоянный сбой."""
    key = _item(source)
    failed = conn.execute(
        "UPDATE index_job SET failed_runs = failed_runs + 1 "
        "WHERE item_type = %s AND owner_id = %s AND item_id = %s RETURNING failed_runs", key).fetchone()
    if failed is None or failed["failed_runs"] < permanent_after():
        return False
    conn.execute("UPDATE index_job SET state = 'permanent', retry_at = NULL "
                 "WHERE item_type = %s AND owner_id = %s AND item_id = %s", key)
    return True


def _published(conn, source) -> dict | None:
    return conn.execute(
        """
        SELECT m.id AS material_id, m.state, v.id AS version_id, v.content_hash, v.processor_version,
               v.embedding_model, v.text_state, v.speech_source
        FROM material m JOIN material_version v ON v.id = m.published_version_id
        WHERE m.item_type = %s AND m.owner_id = %s AND m.item_id = %s
        """,
        _item(source),
    ).fetchone()


def _lecture_id(conn, schedule) -> int:
    """schedule — идентификатор лекции либо пара (ключ строки, название) режима контрольных постов."""
    if isinstance(schedule, int):
        return schedule
    return conn.execute(
        "INSERT INTO lecture (schedule_row, title) VALUES (%s, %s) "
        "ON CONFLICT (schedule_row) DO UPDATE SET title = excluded.title RETURNING id",
        schedule,
    ).fetchone()["id"]


def _link(conn, lecture_id: int, material_id: int, source) -> None:
    conn.execute(
        """
        INSERT INTO material_link (lecture_id, material_id, post_id, link_type, paid_feed)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (lecture_id, material_id, post_id) DO UPDATE SET
            link_type = excluded.link_type,
            paid_feed = coalesce(excluded.paid_feed, material_link.paid_feed)
        """,
        (lecture_id, material_id, source.post_id, source.link_type, source.paid_feed),
    )
    conn.execute("UPDATE lecture SET state = 'has_materials' WHERE id = %s AND state = 'planned'", (lecture_id,))


def _lock(conn, source) -> None:
    # Сериализация обновлений одного материала.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 ("material:" + material_key(*_item(source)),))


def _switch(conn, material_id: int, version_id: int) -> None:
    # Переключение указателя и снятие прежних версий.
    conn.execute("UPDATE material SET published_version_id = %s WHERE id = %s", (version_id, material_id))
    conn.execute("DELETE FROM material_version WHERE material_id = %s AND id <> %s", (material_id, version_id))


def read_track(vk, source: VideoSource, *, sleep=time.sleep) -> tuple[bytes | None, int]:
    """Прочитать дорожку. При повторе запрашивается свежий адрес. (None, n) — дорожки нет."""
    url = source.track_url or vk.fresh_track_url(source)
    if url is None:
        return None, 1

    def operation(attempt: int) -> bytes:
        address = url if attempt == 1 else vk.fresh_track_url(source)
        if address is None:
            raise SourceEmpty("При повторе адрес дорожки не получен.")
        return vk.read_track(address)

    return with_retry(operation, sleep=sleep)


def publish_version(conn, ai, source: VideoSource, schedule, track: bytes | None) -> dict:
    """Подготовить и опубликовать версию записи. Повтор с теми же ключами дублей не создаёт."""
    digest = content_hash(source, track)
    episodes = []
    if track is not None:
        try:
            episodes = episodes_from_vtt(track)
        except SubtitleParseError as exc:
            raise SourceParse(str(exc)) from None
        if not episodes:
            raise SourceParse("В дорожке нет пригодных реплик.")

    current = _published(conn, source)
    unchanged = bool(current and current["state"] == "active" and current["content_hash"] == digest
                     and current["processor_version"] == PROCESSOR_VERSION
                     and current["embedding_model"] == ai.doc_model)
    if current and current["speech_source"] == "asr" and track is None:
        # Распознанная версия не заменяется карточкой (006, R7): запись по-прежнему ждёт
        # субтитров, а задание распознавания получает актуальную версию источника.
        return _keep_recognized(conn, source, schedule, current)
    if current and current["text_state"] == "ready" and track is None:
        # Исчезнувшая дорожка не заменяет проверенные эпизоды карточкой.
        raise SourceEmpty("Дорожка не возвращена; прежняя версия сохранена.")

    # Векторы готовятся до транзакции: внешний вызов не держит блокировку.
    vectors = [] if unchanged else ai.embed_documents([e.text for e in episodes])
    if len(vectors) != (0 if unchanged else len(episodes)):
        raise SourceParse("Число векторов не совпало с числом эпизодов.")

    with conn.transaction():
        _lock(conn, source)
        lecture_id = _lecture_id(conn, schedule)
        material = conn.execute(
            """
            INSERT INTO material (item_type, owner_id, item_id, kind, source_format, title, description, url,
                                  doc_numbers)
            VALUES ('video', %s, %s, 'video', 'video', %s, %s, %s, %s)
            ON CONFLICT (item_type, owner_id, item_id) DO UPDATE SET
                title = excluded.title, description = excluded.description, url = excluded.url,
                doc_numbers = excluded.doc_numbers,
                state = CASE WHEN material.state = 'review' THEN 'review' ELSE 'active' END
            RETURNING id, published_version_id
            """,
            (source.owner_id, source.video_id, source.title, source.description, source.url,
             doc_numbers(source.title)),
        ).fetchone()
        _link(conn, lecture_id, material["id"], source)
        existing = conn.execute(
            "SELECT id FROM material_version WHERE material_id = %s AND content_hash = %s "
            "AND processor_version = %s AND embedding_model = %s",
            (material["id"], digest, PROCESSOR_VERSION, ai.doc_model),
        ).fetchone()
        if existing:
            version_id, created = existing["id"], False
        else:
            if unchanged:
                raise SourceParse("Версия изменилась во время обработки; повторите проход.")
            version_id = conn.execute(
                "INSERT INTO material_version (material_id, content_hash, processor_version, "
                "embedding_model, card_state, text_state, speech_source) "
                "VALUES (%s, %s, %s, %s, 'ready', %s, %s) RETURNING id",
                (material["id"], digest, PROCESSOR_VERSION, ai.doc_model,
                 "ready" if track is not None else "absent", "subtitles" if track is not None else None),
            ).fetchone()["id"]
            for episode, vector in zip(episodes, vectors):
                conn.execute(
                    "INSERT INTO fragment (version_id, kind, start_ms, end_ms, text, embedding) "
                    "VALUES (%s, 'episode', %s, %s, %s, %s::vector)",
                    (version_id, episode.start_ms, episode.end_ms, episode.text, str(list(vector))),
                )
            created = True
        # Переключение, снятие прежних версий и завершение задания — одна транзакция.
        _switch(conn, material["id"], version_id)
        _set_job(conn, source, "done")
        # Распознавание (006): без дорожки запись встаёт в очередь, субтитры снимают задание.
        if track is None:
            asr_jobs.enqueue(conn, material["id"], source.owner_id, source.video_id,
                             asr_jobs.source_version(source.owner_id, source.video_id, source.duration, source.date))
        else:
            asr_jobs.cancel(conn, source.owner_id, source.video_id, "subtitles")
        count = conn.execute("SELECT count(*) AS n FROM fragment WHERE version_id = %s", (version_id,)).fetchone()["n"]
    return {"material_id": material["id"], "version_id": version_id, "created": created,
            "episodes": count, "track": track is not None}


def _keep_recognized(conn, source: VideoSource, schedule, current: dict) -> dict:
    with conn.transaction():
        _lock(conn, source)
        _link(conn, _lecture_id(conn, schedule), current["material_id"], source)
        asr_jobs.enqueue(conn, current["material_id"], source.owner_id, source.video_id,
                         asr_jobs.source_version(source.owner_id, source.video_id, source.duration, source.date))
        _set_job(conn, source, "done")
        count = conn.execute("SELECT count(*) AS n FROM fragment WHERE version_id = %s",
                             (current["version_id"],)).fetchone()["n"]
    return {"material_id": current["material_id"], "version_id": current["version_id"], "created": False,
            "episodes": count, "track": False}


def publish_document(conn, ai, community_id: int, source: DocSource, lecture_id: int, raw: bytes,
                     *, recognize=None) -> dict:
    """Разобрать документ и опубликовать версию: страницы презентации или части письма."""
    digest = document_hash(source, raw)
    current = _published(conn, source)
    unchanged = bool(current and current["state"] == "active" and current["content_hash"] == digest
                     and current["processor_version"] == DOC_PROCESSOR_VERSION
                     and current["embedding_model"] == ai.doc_model)
    if unchanged:
        # Тот же файл, обработчик и модель: распознавание и векторизация не повторяются.
        with conn.transaction():
            _lock(conn, source)
            _link(conn, lecture_id, current["material_id"], source)
            fmt = conn.execute("SELECT source_format FROM material WHERE id = %s",
                               (current["material_id"],)).fetchone()["source_format"]
            # Неподдерживаемый формат остаётся итогом и при повторе без изменений.
            _set_job(conn, source, "done", reason="unsupported" if fmt == "unsupported" else None)
            row = conn.execute(
                "SELECT m.kind, v.pages_total, v.pages_unreadable, v.text_state, "
                "(SELECT count(*) FROM fragment f WHERE f.version_id = v.id) AS n "
                "FROM material m JOIN material_version v ON v.id = m.published_version_id WHERE m.id = %s",
                (current["material_id"],)).fetchone()
        return {"material_id": current["material_id"], "version_id": current["version_id"], "created": False,
                "kind": row["kind"], "pages": row["pages_total"], "unreadable": row["pages_unreadable"],
                "fragments": row["n"], "text_state": row["text_state"], "recognized_pages": 0, "parse_ms": 0}

    started = time.monotonic()
    parsed = documents.parse_document(raw, recognize=recognize)
    parse_ms = round((time.monotonic() - started) * 1000)
    # Векторы готовятся до транзакции: внешний вызов не держит блокировку.
    vectors = ai.embed_documents([f.text for f in parsed.fragments]) if parsed.fragments else []
    if len(vectors) != len(parsed.fragments):
        raise SourceParse("Число векторов не совпало с числом фрагментов.")

    title = source.title.rsplit(".", 1)[0] if "." in source.title else source.title
    title = title.strip().lstrip("+").strip() or source.title
    with conn.transaction():
        _lock(conn, source)
        req = parsed.requisites
        decision = conn.execute(
            "SELECT req_date, req_number, req_sender FROM requisite_decision "
            "WHERE item_type = 'doc' AND owner_id = %s AND item_id = %s",
            (source.owner_id, source.doc_id)).fetchone()
        if decision:
            # Реквизиты, подтверждённые редактором по оригиналу, важнее извлечённых.
            req = documents.Requisites("confirmed", decision["req_date"], decision["req_number"],
                                       decision["req_sender"] or req.sender)
        kind = "letter" if decision and parsed.kind == "document" else parsed.kind
        numbers = doc_numbers(title, req.number if req.state == "confirmed" else None)
        material = conn.execute(
            """
            INSERT INTO material (item_type, owner_id, item_id, kind, source_format, title, description, url,
                                  req_date, req_number, req_sender, req_state, doc_numbers)
            VALUES ('doc', %s, %s, %s, %s, %s, '', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (item_type, owner_id, item_id) DO UPDATE SET
                kind = excluded.kind, source_format = excluded.source_format, title = excluded.title,
                url = CASE WHEN material.state = 'deleted' THEN excluded.url ELSE material.url END,
                req_date = excluded.req_date, req_number = excluded.req_number,
                req_sender = excluded.req_sender, req_state = excluded.req_state,
                doc_numbers = excluded.doc_numbers,
                state = CASE WHEN material.state = 'review' THEN 'review' ELSE 'active' END
            RETURNING id
            """,
            (source.owner_id, source.doc_id, kind, parsed.source_format, title,
             post_url(community_id, source.post_id), req.when, req.number, req.sender, req.state, numbers),
        ).fetchone()
        _link(conn, lecture_id, material["id"], source)
        existing = conn.execute(
            "SELECT id FROM material_version WHERE material_id = %s AND content_hash = %s "
            "AND processor_version = %s AND embedding_model = %s",
            (material["id"], digest, DOC_PROCESSOR_VERSION, ai.doc_model),
        ).fetchone()
        if existing:
            version_id, created = existing["id"], False
        else:
            version_id = conn.execute(
                "INSERT INTO material_version (material_id, content_hash, processor_version, embedding_model, "
                "card_state, text_state, pages_total, pages_unreadable) "
                "VALUES (%s, %s, %s, %s, 'ready', %s, %s, %s) RETURNING id",
                (material["id"], digest, DOC_PROCESSOR_VERSION, ai.doc_model, parsed.text_state,
                 parsed.pages_total, parsed.pages_unreadable),
            ).fetchone()["id"]
            for fragment, vector in zip(parsed.fragments, vectors):
                conn.execute(
                    "INSERT INTO fragment (version_id, kind, page_no, part_no, recognized, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
                    (version_id, fragment.kind, fragment.page_no, fragment.part_no, fragment.recognized,
                     fragment.text, str(list(vector))),
                )
            created = True
        _switch(conn, material["id"], version_id)
        _set_job(conn, source, "done", reason="unsupported" if parsed.source_format == "unsupported" else None)
        count = conn.execute("SELECT count(*) AS n FROM fragment WHERE version_id = %s", (version_id,)).fetchone()["n"]
    return {"material_id": material["id"], "version_id": version_id, "created": created, "kind": kind,
            "pages": parsed.pages_total, "unreadable": parsed.pages_unreadable, "fragments": count,
            "text_state": parsed.text_state, "recognized_pages": parsed.recognized_pages, "parse_ms": parse_ms,
            "req_state": req.state}


def unpublish_deleted(conn, source) -> None:
    """Подтверждённое удаление исключает материал из поиска."""
    with conn.transaction():
        conn.execute("UPDATE material SET state = 'deleted' WHERE item_type = %s AND owner_id = %s AND item_id = %s",
                     _item(source))
        _set_job(conn, source, "done", reason="deleted")
        if isinstance(source, VideoSource):
            asr_jobs.cancel(conn, source.owner_id, source.video_id, "deleted")


def unpublish_post(conn, post_id: int) -> int:
    """Подтверждённое удаление публикации: материал снимается, если у него не осталось других связей."""
    with conn.transaction():
        ids = [r["material_id"] for r in conn.execute(
            "DELETE FROM material_link WHERE post_id = %s RETURNING material_id", (post_id,)).fetchall()]
        if not ids:
            return 0
        return conn.execute(
            "UPDATE material m SET state = 'deleted' WHERE m.id = ANY(%s) "
            "AND NOT EXISTS (SELECT 1 FROM material_link ml WHERE ml.material_id = m.id)",
            (ids,)).rowcount


def _run(conn, ai, source, work, ref: str, details, run_id: int | None = None) -> dict:
    """Общий цикл одного материала: задание, обработка, статус сбоя, измерение.

    run_id — проход, в рамках которого идёт обработка: по нему продолжение пропускает
    уже обработанные материалы и считаются проходы без успеха.
    """
    operation_id = telemetry.new_operation_id()
    started = time.monotonic()
    with conn.transaction():
        _set_job(conn, source, "running", run_id=run_id)
    attempts = [1]

    def failed(state: str, reason: str, tries: int, retry_at) -> str:
        with conn.transaction():
            _set_job(conn, source, state, reason=reason, attempt=tries, retry_at=retry_at)
            if run_id is not None and _count_failed_run(conn, source):
                state = "permanent"
        return f"{state}:{reason}"

    try:
        result = work(attempts)
        outcome = "published" if result["created"] else "unchanged"
        summary = {"ref": ref, "outcome": outcome, **result}
        with conn.transaction():
            # Запись без дорожки опубликована карточкой и ждёт появления источника.
            conn.execute("UPDATE index_job SET waiting_source = %s "
                         "WHERE item_type = %s AND owner_id = %s AND item_id = %s",
                         (result.get("track") is False, *_item(source)))
    except RUN_STOPPERS as exc:
        if run_id is not None:
            # Проход останавливается целиком; материал возвращается в очередь без расхода счётчика.
            # Незафиксированная часть — только чтение каталога: вызывающий фиксирует до обработки.
            conn.rollback()
            with conn.transaction():
                _set_job(conn, source, "queued", run_id=run_id)
            raise
        # Разовые режимы 001–002: сбой одного материала, как прежде.
        if isinstance(exc, SourceError):
            outcome = failed("failed", exc.reason, attempts[0], None)
        else:
            outcome = failed("retry", "timeout", attempts[0], datetime.now(timezone.utc) + RETRY_AFTER)
        summary = {"ref": ref, "outcome": outcome, "error": str(exc)}
    except SourceDeleted:
        unpublish_deleted(conn, source)
        outcome, summary = "deleted", {"ref": ref, "outcome": "deleted"}
    except SourceError as exc:
        tries = getattr(exc, "attempts", attempts[0])
        retry_at = datetime.now(timezone.utc) + RETRY_AFTER if exc.temporary else None
        outcome = failed("retry" if exc.temporary else "failed", exc.reason, tries, retry_at)
        attempts[0] = tries
        summary = {"ref": ref, "outcome": outcome, "error": str(exc)}
    except Exception as exc:  # сбой векторизации и прочее: прежняя версия сохраняется
        outcome = failed("retry", "timeout", attempts[0], datetime.now(timezone.utc) + RETRY_AFTER)
        summary = {"ref": ref, "outcome": outcome, "error": f"{type(exc).__name__}: {exc}"}
    with conn.transaction():
        telemetry.record(
            conn, operation_id, "indexer", outcome, material=ref, attempts=attempts[0],
            version_id=summary.get("version_id"), embedding_model=ai.doc_model,
            duration_ms=round((time.monotonic() - started) * 1000), **details(summary),
        )
    summary["operation_id"] = operation_id
    return summary


def index_video(conn, ai, vk, source: VideoSource, schedule, *, sleep=time.sleep,
                run_id: int | None = None) -> dict:
    """Полный цикл одной записи. Возвращает итог без секретов и временных адресов."""
    def work(attempts):
        track, attempts[0] = read_track(vk, source, sleep=sleep)
        return publish_version(conn, ai, source, schedule, track)

    return _run(conn, ai, source, work, f"{source.owner_id}_{source.video_id}",
                lambda s: {"episodes": s.get("episodes")}, run_id)


def index_document(conn, ai, vk, source: DocSource, lecture_id: int, *, sleep=time.sleep, recognize=None,
                   run_id: int | None = None) -> dict:
    """Полный цикл одного документа. Файл читается в память; при сбое повторяется только он."""
    def work(attempts):
        raw, attempts[0] = with_retry(lambda attempt: vk.read_document(source, documents.MAX_BYTES), sleep=sleep)
        return publish_document(conn, ai, vk.owner_id, source, lecture_id, raw, recognize=recognize)

    return _run(conn, ai, source, work, material_key("doc", source.owner_id, source.doc_id),
                lambda s: {"pages": s.get("pages"), "recognized_pages": s.get("recognized_pages"),
                           "fragments": s.get("fragments"), "embeddings": s.get("fragments") if s.get("created") else 0,
                           "parse_ms": s.get("parse_ms")}, run_id)
