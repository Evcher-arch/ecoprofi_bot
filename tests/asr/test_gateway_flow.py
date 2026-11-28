"""API /asr/v1/ на стенде БД (contracts/asr-api.md; FR-03, FR-08, FR-14). Нужен docker compose --profile test."""
import hashlib
from datetime import timedelta

from tests.asr.conftest import FAKE_MEDIA, OWNER_ID, add_video, gz

LONG = "Обращение с отходами первого класса опасности требует лицензии и паспорта отходов. "


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def segments(n=12):
    return [{"start_ms": i * 5000, "end_ms": i * 5000 + 4800, "text": f"{LONG}Пункт {i}.", "avg_logprob": -0.2,
             "no_speech_prob": 0.02, "compression_ratio": 1.4} for i in range(n)]


def open_manual(db):
    db.execute("UPDATE asr_schedule SET manual_requested_at = now()")


def lease_one(gateway, db, key):
    run = gateway.post("/asr/v1/session", json={"version": "t", "compute_type": "float16"}, headers=auth(key))
    assert run.status_code == 200 and run.json()["run"]
    leased = gateway.post("/asr/v1/jobs/lease", json={"run_id": run.json()["run"]["id"]}, headers=auth(key))
    assert leased.status_code == 200
    return run.json()["run"], leased.json()


def prepared(gateway, db, key):
    run, body = lease_one(gateway, db, key)
    lease = body["lease_id"]
    assert gateway.get(f"/asr/v1/jobs/{lease}/audio", headers=auth(key)).status_code == 202
    gateway.app_ref.state.prepare_step()
    audio = gateway.get(f"/asr/v1/jobs/{lease}/audio", headers=auth(key))
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/ogg"
    assert audio.headers["x-audio-sha256"] == hashlib.sha256(audio.content).hexdigest()
    return run, lease, audio


def result_body(sha, segs=None):
    return {"processor_version": "asr-1/large-v3", "language": "ru", "audio_sha256": sha, "audio_seconds": 60.0,
            "processing_seconds": 6.0, "segments": segs if segs is not None else segments()}


def test_unauthorized_is_uniform(gateway, worker_key, db):
    from ecoprofi.asr import workers
    bodies = []
    for headers in ({}, auth("wrong"), {"Authorization": "Basic x"}):
        response = gateway.post("/asr/v1/session", json={}, headers=headers)
        assert response.status_code == 401
        bodies.append(response.json())
    workers.revoke(db, "owner-pc")
    revoked = gateway.post("/asr/v1/session", json={}, headers=auth(worker_key))
    assert revoked.status_code == 401 and revoked.json() == bodies[0] == {"error": {
        "code": "unauthorized", "message": "unauthorized", "retryable": False}}
    assert db.execute("SELECT count(*) AS n FROM asr_worker WHERE token_hash = %s",
                      (worker_key,)).fetchone()["n"] == 0          # хранится только хеш


def test_closed_window_returns_no_run(gateway, worker_key, db):
    add_video(db, 1)
    body = gateway.post("/asr/v1/session", json={}, headers=auth(worker_key)).json()
    assert body["run"] is None and body["next_due_at"] == "2026-11-29T01:00:00+03:00"


def test_full_cycle_publishes_recognized_version(gateway, worker_key, db):
    material = add_video(db, 1)
    open_manual(db)
    run, lease, audio = prepared(gateway, db, worker_key)
    beat = gateway.post(f"/asr/v1/jobs/{lease}/heartbeat", json={"processed_s": 10}, headers=auth(worker_key))
    assert beat.status_code == 200 and beat.json()["continue"] is True
    sha = audio.headers["x-audio-sha256"]
    sent = gateway.post(f"/asr/v1/jobs/{lease}/result", content=gz(result_body(sha)),
                        headers={**auth(worker_key), "Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert sent.status_code == 202 and sent.json() == {"state": "transcribed"}
    assert not list(gateway.tmp.iterdir())                                       # звук удалён
    again = gateway.post(f"/asr/v1/jobs/{lease}/result", content=gz(result_body(sha)),
                         headers={**auth(worker_key), "Content-Encoding": "gzip"})
    assert again.status_code == 200
    gateway.app_ref.state.maintenance_step()                                     # фоновый публикатор
    job = db.execute("SELECT * FROM asr_job WHERE item_id = 1").fetchone()
    assert job["state"] == "published"
    version = db.execute("SELECT v.* FROM material m JOIN material_version v ON v.id = m.published_version_id "
                         "WHERE m.id = %s", (material,)).fetchone()
    assert version["speech_source"] == "asr" and version["processor_version"] == "asr-1/large-v3"
    frags = db.execute("SELECT * FROM fragment WHERE version_id = %s", (version["id"],)).fetchall()
    assert frags and all(f["recognized"] and f["kind"] == "episode" and f["start_ms"] < f["end_ms"] for f in frags)
    assert db.execute("SELECT count(*) AS n FROM asr_transcript").fetchone()["n"] == 1
    stats = db.execute("SELECT * FROM asr_run WHERE id = %s", (run["id"],)).fetchone()
    assert stats["taken"] == 1 and stats["published"] == 1 and float(stats["audio_seconds"]) == 60.0
    # Второй запуск не берёт распознанную запись (SC-06).
    assert gateway.post("/asr/v1/jobs/lease", json={"run_id": run["id"]}, headers=auth(worker_key)).status_code == 204


def test_range_resume(gateway, worker_key, db):
    add_video(db, 1)
    open_manual(db)
    _, lease, audio = prepared(gateway, db, worker_key)
    part = gateway.get(f"/asr/v1/jobs/{lease}/audio", headers={**auth(worker_key), "Range": "bytes=100-"})
    assert part.status_code == 206 and part.content == audio.content[100:]


def test_invalid_result_goes_to_retry(gateway, worker_key, db):
    add_video(db, 1)
    open_manual(db)
    _, lease, audio = prepared(gateway, db, worker_key)
    bad = gateway.post(f"/asr/v1/jobs/{lease}/result", content=gz(result_body("other-sha")),
                       headers={**auth(worker_key), "Content-Encoding": "gzip"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_result"
    job = db.execute("SELECT state, reason, attempts FROM asr_job WHERE item_id = 1").fetchone()
    assert (job["state"], job["reason"], job["attempts"]) == ("retry", "decode", 1)


def test_result_after_release_is_rejected(gateway, worker_key, db):
    add_video(db, 1)
    open_manual(db)
    _, lease, audio = prepared(gateway, db, worker_key)
    assert gateway.post(f"/asr/v1/jobs/{lease}/release", headers=auth(worker_key)).status_code == 200
    job = db.execute("SELECT state, attempts, lease_id FROM asr_job WHERE item_id = 1").fetchone()
    assert (job["state"], job["attempts"], job["lease_id"]) == ("queued", 0, None)
    late = gateway.post(f"/asr/v1/jobs/{lease}/result", content=gz(result_body(audio.headers["x-audio-sha256"])),
                        headers={**auth(worker_key), "Content-Encoding": "gzip"})
    assert late.status_code == 409 and late.json()["error"]["code"] == "lease_invalid"
    assert db.execute("SELECT count(*) AS n FROM asr_transcript").fetchone()["n"] == 0


def test_audio_unavailable_reason(gateway, worker_key, db):
    from ecoprofi.indexer.retry import SourceDenied
    add_video(db, 1)
    gateway.vk.records[(OWNER_ID, 1)] = SourceDenied("VK 15")
    open_manual(db)
    _, body = lease_one(gateway, db, worker_key)
    gateway.app_ref.state.prepare_step()
    response = gateway.get(f"/asr/v1/jobs/{body['lease_id']}/audio", headers=auth(worker_key))
    assert response.status_code == 410
    job = db.execute("SELECT state, reason FROM asr_job WHERE item_id = 1").fetchone()
    assert (job["state"], job["reason"]) == ("audio_unavailable", "denied")


def test_gpu_unavailable_closes_run_without_attempts(gateway, worker_key, db):
    add_video(db, 1)
    open_manual(db)
    run, lease, _ = prepared(gateway, db, worker_key)
    assert gateway.post(f"/asr/v1/jobs/{lease}/failure", json={"kind": "gpu_unavailable", "detail": "x"},
                        headers=auth(worker_key)).status_code == 200
    job = db.execute("SELECT state, attempts FROM asr_job WHERE item_id = 1").fetchone()
    assert (job["state"], job["attempts"]) == ("queued", 0)
    assert db.execute("SELECT outcome FROM asr_run WHERE id = %s", (run["id"],)).fetchone()["outcome"] == \
        "resource_unavailable"


def test_low_vram_does_not_open_run(gateway, worker_key, db):
    add_video(db, 1)
    open_manual(db)
    body = gateway.post("/asr/v1/session", json={"compute_type": "float16", "device": {"vram_free_mb": 1000}},
                        headers=auth(worker_key)).json()
    assert body["run"] is None


def test_expired_lease_returns_to_queue_and_deadline_is_timeout(gateway, worker_key, db, clock):
    from ecoprofi.asr import jobs
    add_video(db, 1)
    add_video(db, 2)
    open_manual(db)
    run, lease, _ = prepared(gateway, db, worker_key)
    clock.advance(minutes=21)
    jobs.expire_leases(db, clock())
    job = db.execute("SELECT * FROM asr_job WHERE lease_id IS NULL AND item_id IN (1, 2) AND state = 'queued' "
                     "ORDER BY item_id").fetchall()
    assert job and job[0]["attempts"] == 0
    # Предел 1,5 × длительность + 15 мин: задание с длительностью 600 с — не дольше 30 мин.
    db.execute("UPDATE asr_job SET state = 'leased', lease_id = gen_random_uuid(), lease_worker_id = 1, "
               "lease_expires_at = %s, deadline_at = %s, run_id = %s WHERE item_id = 1",
               (clock() + timedelta(minutes=10), clock() - timedelta(seconds=1), run["id"]))
    jobs.expire_leases(db, clock())
    timed = db.execute("SELECT state, reason, attempts FROM asr_job WHERE item_id = 1").fetchone()
    assert (timed["state"], timed["reason"], timed["attempts"]) == ("retry", "timeout", 1)


def test_one_job_per_worker(gateway, worker_key, db):
    add_video(db, 1)
    add_video(db, 2)
    open_manual(db)
    run, first = lease_one(gateway, db, worker_key)
    second = gateway.post("/asr/v1/jobs/lease", json={"run_id": run["id"]}, headers=auth(worker_key)).json()
    assert second["lease_id"] == first["lease_id"]


def test_no_secret_in_api_or_logs(gateway, worker_key, db, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    add_video(db, 1)
    open_manual(db)
    _, lease, audio = prepared(gateway, db, worker_key)
    gateway.post(f"/asr/v1/jobs/{lease}/result", content=gz(result_body(audio.headers["x-audio-sha256"])),
                 headers={**auth(worker_key), "Content-Encoding": "gzip"})
    dump = str(db.execute("SELECT * FROM asr_job").fetchall()) + caplog.text
    for secret in ("SECRETSIG", FAKE_MEDIA, worker_key):
        assert secret not in dump
    assert lease not in caplog.text            # в журнале только первые 8 символов аренды
