"""Публикация распознанного текста (research R6).

Отрезки без речи и зацикленный повтор отбрасываются, остальные превращаются в
те же Cue, что и субтитры, и собираются в эпизоды функцией build_episodes
(правила 001). Векторы готовятся до транзакции. Новая версия с
speech_source = 'asr' и фрагментами recognized = true публикуется атомарно;
прежняя версия остаётся в поиске до переключения (FR-07).
"""
import hashlib
import logging
import re
from datetime import datetime, timezone

from ecoprofi.asr import MIN_SPEECH_CHARS, PROCESSOR_VERSION, PUBLISH_RETRY, jobs
from ecoprofi.indexer.subtitles import Cue, build_episodes
from ecoprofi.textkeys import material_key

log = logging.getLogger("ecoprofi.asr.publish")

# Пороги Whisper: отрезок без речи — высокая вероятность тишины при низкой уверенности;
# зацикленный повтор — высокая степень сжатия текста.
NO_SPEECH_PROB = 0.6
LOW_LOGPROB = -1.0
COMPRESSION_RATIO = 2.4


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold().replace("ё", "е")).strip()


def keep_segment(segment: dict) -> bool:
    text = (segment.get("text") or "").strip()
    if not text:
        return False
    if (segment.get("compression_ratio") or 0) > COMPRESSION_RATIO:
        return False
    if (segment.get("no_speech_prob") or 0) > NO_SPEECH_PROB and (segment.get("avg_logprob") or 0) < LOW_LOGPROB:
        return False
    return True


def filter_segments(segments: list[dict]) -> list[Cue]:
    """Отрезки, пригодные для эпизодов. Подряд идущий одинаковый текст — «галлюцинация» на тишине."""
    cues, previous = [], None
    for segment in sorted(segments, key=lambda s: (s["start_ms"], s["end_ms"])):
        if not keep_segment(segment):
            continue
        norm = _norm(segment["text"])
        if norm and norm == previous:
            continue
        previous = norm
        if segment["start_ms"] < segment["end_ms"]:
            cues.append(Cue(int(segment["start_ms"]), int(segment["end_ms"]), " ".join(segment["text"].split())))
    return cues


def episodes_from_segments(segments: list[dict]):
    """(эпизоды, число символов речи после фильтра)."""
    cues = filter_segments(segments)
    return build_episodes(cues), sum(len(c.text) for c in cues)


def content_hash(version: str) -> str:
    return hashlib.sha256(version.encode()).hexdigest()


def publish_transcript(conn, ai, owner_id: int, item_id: int) -> str:
    """Опубликовать расшифровку задания в состоянии transcribed. Возвращает итоговое состояние."""
    job = conn.execute("SELECT * FROM asr_job WHERE owner_id = %s AND item_id = %s", (owner_id, item_id)).fetchone()
    conn.commit()
    if job is None or job["state"] != "transcribed":
        return job["state"] if job else "missing"
    transcript = conn.execute(
        "SELECT * FROM asr_transcript WHERE owner_id = %s AND item_id = %s AND source_version = %s "
        "ORDER BY created_at DESC LIMIT 1", (owner_id, item_id, job["source_version"])).fetchone()
    conn.commit()
    if transcript is None:
        jobs.transition(conn, owner_id, item_id, "queued", reason=None, **jobs.NO_LEASE)
        return "queued"
    episodes, chars = episodes_from_segments(transcript["segments"])
    if chars < MIN_SPEECH_CHARS or not episodes:
        jobs.transition(conn, owner_id, item_id, "no_speech", reason="no_speech", failed_runs=0)
        log.info("речь не найдена: %s (%d символов)", jobs.ref(owner_id, item_id), chars)
        return "no_speech"

    # Векторы готовятся до транзакции: внешний вызов не держит блокировку.
    vectors = ai.embed_documents([e.text for e in episodes])
    if len(vectors) != len(episodes):
        raise RuntimeError("Число векторов не совпало с числом эпизодов.")

    digest = content_hash(job["source_version"])
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("material:" + material_key("video", owner_id, item_id),))
        current = conn.execute("SELECT * FROM asr_job WHERE owner_id = %s AND item_id = %s FOR UPDATE",
                               (owner_id, item_id)).fetchone()
        if current["state"] != "transcribed" or current["source_version"] != job["source_version"]:
            return current["state"]      # отменено субтитрами или изменилась версия
        material = conn.execute("SELECT id, state FROM material WHERE id = %s", (current["material_id"],)).fetchone()
        if material is None or material["state"] == "deleted":
            jobs._update(conn, current, "cancelled", reason="deleted")
            return "cancelled"
        existing = conn.execute(
            "SELECT id FROM material_version WHERE material_id = %s AND content_hash = %s "
            "AND processor_version = %s AND embedding_model = %s",
            (material["id"], digest, transcript["processor_version"], ai.doc_model)).fetchone()
        if existing:
            version_id = existing["id"]
        else:
            version_id = conn.execute(
                "INSERT INTO material_version (material_id, content_hash, processor_version, embedding_model, "
                "card_state, text_state, speech_source) VALUES (%s, %s, %s, %s, 'ready', 'ready', 'asr') "
                "RETURNING id", (material["id"], digest, transcript["processor_version"], ai.doc_model)
            ).fetchone()["id"]
            for episode, vector in zip(episodes, vectors):
                conn.execute(
                    "INSERT INTO fragment (version_id, kind, start_ms, end_ms, recognized, text, embedding) "
                    "VALUES (%s, 'episode', %s, %s, true, %s, %s::vector)",
                    (version_id, episode.start_ms, episode.end_ms, episode.text, str(list(vector))))
        # Переключение и снятие прежних версий — одна транзакция (как в индексаторе).
        conn.execute("UPDATE material SET published_version_id = %s WHERE id = %s", (version_id, material["id"]))
        conn.execute("DELETE FROM material_version WHERE material_id = %s AND id <> %s", (material["id"], version_id))
        conn.execute("UPDATE asr_transcript SET version_id = %s WHERE id = %s", (version_id, transcript["id"]))
        jobs._update(conn, current, "published", reason=None, failed_runs=0, retry_at=None)
        if current["run_id"]:
            conn.execute("UPDATE asr_run SET published = published + 1 WHERE id = %s", (current["run_id"],))
        if current["initial"]:
            jobs.check_initial_done(conn)
    log.info("опубликовано: %s, эпизодов %d", jobs.ref(owner_id, item_id), len(episodes))
    return "published"


def publish_pending(conn, ai, now: datetime | None = None) -> list[tuple[str, str]]:
    """Опубликовать все расшифровки, ждущие публикации. Сбой одной не мешает остальным."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute("SELECT owner_id, item_id FROM asr_job WHERE state = 'transcribed' "
                        "AND (retry_at IS NULL OR retry_at <= %s) ORDER BY updated_at", (now,)).fetchall()
    conn.commit()
    results = []
    for row in rows:
        key = jobs.ref(row["owner_id"], row["item_id"])
        try:
            results.append((key, publish_transcript(conn, ai, row["owner_id"], row["item_id"])))
        except Exception as exc:   # векторизация и прочее: прежняя версия остаётся, повтор позже
            conn.rollback()
            log.warning("публикация отложена: %s: %s", key, type(exc).__name__)
            conn.execute("UPDATE asr_job SET retry_at = %s WHERE owner_id = %s AND item_id = %s "
                         "AND state = 'transcribed'", (now + PUBLISH_RETRY, row["owner_id"], row["item_id"]))
            conn.commit()
            results.append((key, "deferred"))
    return results


__all__ = ["PROCESSOR_VERSION", "publish_transcript", "publish_pending", "episodes_from_segments"]
