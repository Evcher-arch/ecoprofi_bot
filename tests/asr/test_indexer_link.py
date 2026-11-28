"""Связь с индексатором (R7; FR-01, FR-09, SC-05): постановка, защита от карточки, субтитры, удаление.
Нужен стенд docker compose --profile test."""
from pathlib import Path

from ecoprofi.asr import jobs
from ecoprofi.indexer.publish import publish_version, unpublish_deleted
from ecoprofi.indexer.vk_sources import VideoSource
from tests.asr.conftest import OWNER_ID
from tests.fixtures.fake_ai import FakeAI

VTT = (Path(__file__).resolve().parents[1] / "fixtures" / "lecture_public.vtt").read_bytes()
SCHEDULE = ("asr-row", "Лекция")


def source(item_id=7, duration=600, date=1700000000):
    return VideoSource(post_id=1, owner_id=OWNER_ID, video_id=item_id, title=f"Запись {item_id}", description="",
                       link_type="attachment", access_key=None, track_url=None, duration=duration, date=date)


def job(db, item_id=7):
    return db.execute("SELECT * FROM asr_job WHERE item_id = %s", (item_id,)).fetchone()


def publish_recognized(db, item_id=7):
    """Опубликовать распознанную версию напрямую, как это делает asr-gateway."""
    from ecoprofi.asr.publish import publish_transcript
    from psycopg.types.json import Jsonb
    text = "Обращение с отходами первого класса опасности требует лицензии и паспорта отходов. "
    segs = [{"start_ms": i * 5000, "end_ms": i * 5000 + 4800, "text": text + str(i), "avg_logprob": -0.2,
             "no_speech_prob": 0.01, "compression_ratio": 1.3} for i in range(10)]
    current = job(db, item_id)
    db.execute("INSERT INTO asr_transcript (owner_id, item_id, source_version, processor_version, language, segments, "
               "audio_seconds, processing_seconds) VALUES (%s, %s, %s, 'asr-1/large-v3', 'ru', %s, 60, 6)",
               (OWNER_ID, item_id, current["source_version"], Jsonb(segs)))
    db.execute("UPDATE asr_job SET state = 'transcribed' WHERE item_id = %s", (item_id,))
    assert publish_transcript(db, FakeAI(), OWNER_ID, item_id) == "published"


def published(db, item_id=7):
    return db.execute("SELECT v.*, (SELECT count(*) FROM fragment f WHERE f.version_id = v.id) AS n "
                      "FROM material m JOIN material_version v ON v.id = m.published_version_id "
                      "WHERE m.item_type = 'video' AND m.item_id = %s", (item_id,)).fetchone()


def test_card_enqueues_job_with_source_version(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    created = job(db)
    assert created["state"] == "queued" and not created["initial"]
    assert created["source_version"] == jobs.source_version(OWNER_ID, 7, 600, 1700000000)


def test_null_version_from_migration_is_filled_without_reset(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    db.execute("UPDATE asr_job SET source_version = NULL, state = 'retry', attempts = 2, initial = true")
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    filled = job(db)
    assert filled["source_version"] and (filled["state"], filled["attempts"]) == ("retry", 2)


def test_changed_source_version_requeues(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    db.execute("UPDATE asr_job SET state = 'no_speech', reason = 'no_speech', failed_runs = 2")
    publish_version(db, FakeAI(), source(duration=601), SCHEDULE, None)
    changed = job(db)
    assert (changed["state"], changed["failed_runs"]) == ("queued", 0)


def test_card_does_not_replace_recognized_version(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    publish_recognized(db)
    before = published(db)
    result = publish_version(db, FakeAI(), source(), SCHEDULE, None)
    after = published(db)
    assert result["created"] is False and result["track"] is False
    assert after["id"] == before["id"] and after["speech_source"] == "asr" and after["n"] == before["n"] > 0
    # Изменилась версия источника: распознанная версия остаётся в поиске до новой публикации.
    publish_version(db, FakeAI(), source(duration=900), SCHEDULE, None)
    assert published(db)["id"] == before["id"] and job(db)["state"] == "queued"


def test_subtitles_replace_recognized_and_cancel_job(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    publish_recognized(db)
    db.execute("UPDATE asr_job SET lease_id = gen_random_uuid()")
    publish_version(db, FakeAI(), source(), SCHEDULE, VTT)
    version = published(db)
    assert version["speech_source"] == "subtitles"
    assert db.execute("SELECT count(*) AS n FROM material_version v JOIN material m ON m.id = v.material_id "
                      "WHERE m.item_id = 7").fetchone()["n"] == 1                       # один комплект эпизодов
    cancelled = job(db)
    assert (cancelled["state"], cancelled["reason"], cancelled["lease_id"]) == ("cancelled", "subtitles", None)
    assert db.execute("SELECT count(*) AS n FROM fragment f JOIN material_version v ON v.id = f.version_id "
                      "JOIN material m ON m.id = v.material_id WHERE m.item_id = 7 AND f.recognized").fetchone()["n"] == 0


def test_confirmed_deletion_cancels_job(db):
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    unpublish_deleted(db, source())
    assert (job(db)["state"], job(db)["reason"]) == ("cancelled", "deleted")


def test_audio_unavailable_deleted_does_not_unpublish(db):
    """«Звук недоступен: удалена» от asr-gateway удаление не подтверждает (U3)."""
    publish_version(db, FakeAI(), source(), SCHEDULE, None)
    db.execute("UPDATE asr_job SET state = 'audio_unavailable', reason = 'deleted'")
    state = db.execute("SELECT state FROM material WHERE item_type = 'video' AND item_id = 7").fetchone()["state"]
    assert state == "active"
