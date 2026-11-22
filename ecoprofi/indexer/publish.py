"""Обработка одного материала и атомарная публикация версии.

Новая версия готовится целиком и одной транзакцией становится опубликованной;
в той же транзакции завершается задание. Обновления одного материала
сериализуются блокировкой. Временный сбой оставляет прежнюю версию доступной.
"""
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

from ecoprofi import telemetry
from ecoprofi.indexer.retry import (
    SourceDeleted, SourceEmpty, SourceError, SourceParse, with_retry,
)
from ecoprofi.indexer.subtitles import SubtitleParseError, episodes_from_vtt
from ecoprofi.indexer.vk_sources import VideoSource

PROCESSOR_VERSION = "subtitles-2"   # 2: повторы «бегущих» реплик, стыкующихся по времени
RETRY_AFTER = timedelta(minutes=15)


def content_hash(source: VideoSource, track: bytes | None) -> str:
    track_hash = hashlib.sha256(track).hexdigest() if track is not None else None
    payload = json.dumps([source.title, source.description, track_hash], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _set_job(conn, source: VideoSource, state: str, *, reason: str | None = None,
             attempt: int | None = None, retry_at=None) -> None:
    conn.execute(
        """
        INSERT INTO index_job (owner_id, video_id, post_id, state, attempt, failure_reason, retry_at)
        VALUES (%(o)s, %(v)s, %(p)s, %(s)s, coalesce(%(a)s, 0), %(r)s, %(t)s)
        ON CONFLICT (owner_id, video_id) DO UPDATE SET
            post_id = excluded.post_id, state = excluded.state,
            attempt = coalesce(%(a)s, index_job.attempt),
            failure_reason = excluded.failure_reason, retry_at = excluded.retry_at, updated_at = now()
        """,
        {"o": source.owner_id, "v": source.video_id, "p": source.post_id,
         "s": state, "a": attempt, "r": reason, "t": retry_at},
    )


def _published(conn, source: VideoSource) -> dict | None:
    return conn.execute(
        """
        SELECT m.id AS material_id, m.state, v.id AS version_id, v.content_hash, v.processor_version,
               v.embedding_model, v.track_state
        FROM material m JOIN material_version v ON v.id = m.published_version_id
        WHERE m.owner_id = %s AND m.video_id = %s
        """,
        (source.owner_id, source.video_id),
    ).fetchone()


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


def publish_version(conn, ai, source: VideoSource, schedule: tuple[str, str], track: bytes | None) -> dict:
    """Подготовить и опубликовать версию. Повтор с теми же ключами дублей не создаёт."""
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
    if current and current["track_state"] == "ready" and track is None:
        # Исчезнувшая дорожка не заменяет проверенные эпизоды карточкой.
        raise SourceEmpty("Дорожка не возвращена; прежняя версия сохранена.")

    # Векторы готовятся до транзакции: внешний вызов не держит блокировку.
    vectors = [] if unchanged else ai.embed_documents([e.text for e in episodes])
    if len(vectors) != (0 if unchanged else len(episodes)):
        raise SourceParse("Число векторов не совпало с числом эпизодов.")

    with conn.transaction():
        # Сериализация обновлений одного материала.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"material:{source.owner_id}_{source.video_id}",))
        lecture_id = conn.execute(
            "INSERT INTO lecture (schedule_row, title) VALUES (%s, %s) "
            "ON CONFLICT (schedule_row) DO UPDATE SET title = excluded.title RETURNING id",
            schedule,
        ).fetchone()["id"]
        material = conn.execute(
            """
            INSERT INTO material (lecture_id, post_id, owner_id, video_id, link_type, title, description, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (owner_id, video_id) DO UPDATE SET
                lecture_id = excluded.lecture_id, post_id = excluded.post_id, link_type = excluded.link_type,
                title = excluded.title, description = excluded.description, url = excluded.url,
                state = 'active'
            RETURNING id, published_version_id
            """,
            (lecture_id, source.post_id, source.owner_id, source.video_id, source.link_type,
             source.title, source.description, source.url),
        ).fetchone()
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
                "embedding_model, card_state, track_state) VALUES (%s, %s, %s, %s, 'ready', %s) RETURNING id",
                (material["id"], digest, PROCESSOR_VERSION, ai.doc_model,
                 "ready" if track is not None else "absent"),
            ).fetchone()["id"]
            for episode, vector in zip(episodes, vectors):
                conn.execute(
                    "INSERT INTO episode (version_id, start_ms, end_ms, text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s::vector)",
                    (version_id, episode.start_ms, episode.end_ms, episode.text, str(list(vector))),
                )
            created = True
        # Переключение указателя, снятие прежних версий и завершение задания — одна транзакция.
        conn.execute("UPDATE material SET published_version_id = %s WHERE id = %s", (version_id, material["id"]))
        conn.execute("DELETE FROM material_version WHERE material_id = %s AND id <> %s",
                     (material["id"], version_id))
        _set_job(conn, source, "done")
        count = conn.execute("SELECT count(*) AS n FROM episode WHERE version_id = %s", (version_id,)).fetchone()["n"]
    return {"material_id": material["id"], "version_id": version_id, "created": created,
            "episodes": count, "track": track is not None}


def unpublish_deleted(conn, source: VideoSource) -> None:
    """Подтверждённое удаление исключает материал из поиска."""
    with conn.transaction():
        conn.execute("UPDATE material SET state = 'deleted' WHERE owner_id = %s AND video_id = %s",
                     (source.owner_id, source.video_id))
        _set_job(conn, source, "done", reason="deleted")


def index_video(conn, ai, vk, source: VideoSource, schedule: tuple[str, str], *, sleep=time.sleep) -> dict:
    """Полный цикл одного материала. Возвращает итог без секретов и временных адресов."""
    operation_id = telemetry.new_operation_id()
    started = time.monotonic()
    ref = f"{source.owner_id}_{source.video_id}"
    with conn.transaction():
        _set_job(conn, source, "running")
    attempts = 1
    try:
        track, attempts = read_track(vk, source, sleep=sleep)
        result = publish_version(conn, ai, source, schedule, track)
        outcome = "published" if result["created"] else "unchanged"
        summary = {"ref": ref, "outcome": outcome, **result}
    except SourceDeleted:
        unpublish_deleted(conn, source)
        outcome, summary = "deleted", {"ref": ref, "outcome": "deleted"}
    except SourceError as exc:
        attempts = getattr(exc, "attempts", attempts)
        state = "retry" if exc.temporary else "failed"
        retry_at = datetime.now(timezone.utc) + RETRY_AFTER if exc.temporary else None
        with conn.transaction():
            _set_job(conn, source, state, reason=exc.reason, attempt=attempts, retry_at=retry_at)
        outcome = f"{state}:{exc.reason}"
        summary = {"ref": ref, "outcome": outcome, "error": str(exc)}
    except Exception as exc:  # сбой векторизации и прочее: прежняя версия сохраняется
        with conn.transaction():
            _set_job(conn, source, "retry", reason="timeout", attempt=attempts,
                     retry_at=datetime.now(timezone.utc) + RETRY_AFTER)
        outcome = "retry:timeout"
        summary = {"ref": ref, "outcome": outcome, "error": f"{type(exc).__name__}: {exc}"}
    with conn.transaction():
        telemetry.record(
            conn, operation_id, "indexer", outcome, material=ref, attempts=attempts,
            version_id=summary.get("version_id"), episodes=summary.get("episodes"),
            embedding_model=ai.doc_model, duration_ms=round((time.monotonic() - started) * 1000),
        )
    summary["operation_id"] = operation_id
    return summary
