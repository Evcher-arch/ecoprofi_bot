"""Миграция, первичный проход, запуски, постоянный сбой, сводка и оповещения (FR-10…FR-19).
Нужен стенд docker compose --profile test."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from ecoprofi.asr import jobs, workers
from ecoprofi.asr.__main__ import build_status, status_text
from ecoprofi.asr.gateway import check_silence
from tests.asr.conftest import OWNER_ID, _dsn, add_video

THURSDAY = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def worker_id(db):
    workers.issue_key(db, "owner-pc")
    db.execute("UPDATE asr_worker SET last_seen_at = %s", (THURSDAY,))
    return db.execute("SELECT id FROM asr_worker").fetchone()["id"]


# --- миграция 005 ---

def test_migration_defaults_and_constraints(asr_owner, db):
    row = asr_owner.execute("SELECT column_default FROM information_schema.columns "
                            "WHERE table_name = 'asr_schedule' AND column_name = 'initial_pass'").fetchone()
    assert "pending" in row["column_default"]
    assert asr_owner.execute("SELECT count(*) AS n FROM asr_schedule").fetchone()["n"] == 1
    material = add_video(db, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE material_version SET speech_source = 'ocr' WHERE material_id = %s", (material,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE asr_schedule SET window_length = '10 minutes'")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE asr_schedule SET weekdays = '{8}'")
    db.execute("INSERT INTO asr_run (kind) VALUES ('manual')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO asr_run (kind) VALUES ('manual')")       # одновременно один запуск


def test_bot_role_cannot_read_worker_keys(db):
    from ecoprofi.db import connect
    with connect(_dsn("TEST_BOT_DATABASE_URL"), vector=False) as bot:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            bot.execute("SELECT token_hash FROM asr_worker")


# --- первичный проход и запуски ---

def test_initial_pass_runs_until_every_initial_job_has_outcome(db):
    wid = worker_id(db)
    add_video(db, 1, initial=True)
    add_video(db, 2, initial=True)
    db.execute("UPDATE asr_schedule SET initial_pass = 'pending'")
    run = jobs.open_or_get_run(db, wid, THURSDAY)
    assert run["kind"] == "initial" and run["until"] is None
    assert jobs.schedule(db)["initial_pass"] == "running"
    jobs.transition(db, OWNER_ID, 1, "cancelled", reason="subtitles")
    assert jobs.schedule(db)["initial_pass"] == "running"
    job = jobs.lease_next(db, run, wid, THURSDAY)
    jobs.audio_unavailable(db, job, "no_file")
    sched = jobs.schedule(db)
    assert sched["initial_pass"] == "done" and sched["initial_done_at"]
    assert jobs.open_run(db) is None                       # запуск initial закрыт
    assert jobs.open_or_get_run(db, wid, THURSDAY) is None  # четверг: недельное окно закрыто


def test_worker_lost_closes_run_and_job_returns(db):
    wid = worker_id(db)
    add_video(db, 1, initial=True)
    db.execute("UPDATE asr_schedule SET initial_pass = 'running'")
    run = jobs.open_or_get_run(db, wid, THURSDAY)
    jobs.lease_next(db, run, wid, THURSDAY)
    later = THURSDAY + timedelta(minutes=30)
    jobs.expire_leases(db, later)
    assert jobs.close_inactive_runs(db, later) == [(run["id"], "worker_lost")]
    assert db.execute("SELECT state, attempts FROM asr_job").fetchone() == {"state": "queued", "attempts": 0}
    db.execute("UPDATE asr_worker SET last_seen_at = %s", (later,))
    assert jobs.open_or_get_run(db, wid, later)["kind"] == "initial"   # проход продолжается


def test_run_now_by_list_works_during_pause(db):
    wid = worker_id(db)
    add_video(db, 1, initial=True)
    add_video(db, 2, initial=True)
    db.execute("UPDATE asr_schedule SET initial_pass = 'pending', paused = true, manual_only = %s",
               ([f"{OWNER_ID}_2"],))
    run = jobs.open_or_get_run(db, wid, THURSDAY)
    assert run["kind"] == "manual" and run["only_refs"] == [f"{OWNER_ID}_2"]
    assert jobs.schedule(db)["initial_pass"] == "pending"            # первичный проход не начат
    job = jobs.lease_next(db, run, wid, THURSDAY)
    assert job["item_id"] == 2
    jobs.release(db, job["lease_id"], THURSDAY)
    db.execute("UPDATE asr_job SET state = 'published' WHERE item_id = 2")
    assert jobs.lease_next(db, run, wid, THURSDAY) is None


def test_window_close_mid_job_returns_without_partial(db):
    wid = worker_id(db)
    add_video(db, 1)
    db.execute("UPDATE asr_schedule SET manual_requested_at = now()")
    run = jobs.open_or_get_run(db, wid, THURSDAY)
    jobs.lease_next(db, run, wid, THURSDAY)
    after = run["until"] + timedelta(minutes=1)
    assert not jobs.run_active(run, jobs.schedule(db), after)
    db.execute("UPDATE asr_job SET state = 'leased', lease_expires_at = %s", (after + timedelta(minutes=5),))
    lease = db.execute("SELECT lease_id FROM asr_job").fetchone()["lease_id"]
    jobs.release(db, lease, after)
    assert db.execute("SELECT state FROM asr_job").fetchone()["state"] == "queued"
    assert db.execute("SELECT count(*) AS n FROM fragment WHERE recognized").fetchone()["n"] == 0


def test_second_scheduled_run_in_same_window_is_not_opened(db):
    wid = worker_id(db)
    sunday = datetime(2026, 11, 29, 1, 5, tzinfo=timezone(timedelta(hours=3)))
    db.execute("UPDATE asr_worker SET last_seen_at = %s", (sunday,))
    run = jobs.open_or_get_run(db, wid, sunday)
    assert run["kind"] == "scheduled"
    jobs.finish_run(db, run["id"], "queue_empty", sunday)
    assert jobs.open_or_get_run(db, wid, sunday + timedelta(hours=1)) is None


def test_schedule_set_does_not_touch_indexer_schedule(db, monkeypatch):
    from ecoprofi.asr.__main__ import main
    monkeypatch.setenv("DATABASE_URL", _dsn("TEST_INDEXER_DATABASE_URL"))
    before = db.execute("SELECT * FROM run_schedule").fetchone()
    assert main(["schedule", "set", "--days", "6,7", "--at", "02:30", "--window", "4h"]) == 0
    assert db.execute("SELECT * FROM run_schedule").fetchone() == before
    sched = jobs.schedule(db)
    assert sched["weekdays"] == [6, 7] and sched["window_length"] == timedelta(hours=4)


# --- сбои, сводка, оповещения ---

def test_three_failed_runs_make_permanent_with_one_alert(db):
    wid = worker_id(db)
    add_video(db, 1)
    for n in range(3):
        db.execute("UPDATE asr_job SET state = 'queued', retry_at = NULL")
        db.execute("UPDATE asr_schedule SET manual_requested_at = now(), last_due_at = NULL")
        run = jobs.open_or_get_run(db, wid, THURSDAY)
        for _ in range(3):
            db.execute("UPDATE asr_job SET state = CASE WHEN state = 'retry' THEN 'queued' ELSE state END, "
                       "retry_at = NULL")
            job = jobs.lease_next(db, run, wid, THURSDAY)
            if job is None:
                break
            db.execute("UPDATE asr_job SET state = 'leased'")
            jobs.fail(db, job["lease_id"], "decode", THURSDAY)
        jobs.finish_run(db, run["id"], "queue_empty", THURSDAY)
    final = db.execute("SELECT state, failed_runs FROM asr_job").fetchone()
    assert final == {"state": "permanent", "failed_runs": 3}
    alerts = db.execute("SELECT cause_key FROM alert WHERE state = 'open'").fetchall()
    assert [a["cause_key"] for a in alerts] == [f"asr_permanent:{OWNER_ID}_1"]
    assert jobs.requeue(db, OWNER_ID, 1)
    assert db.execute("SELECT state, failed_runs FROM asr_job").fetchone() == {"state": "queued", "failed_runs": 0}
    assert db.execute("SELECT count(*) AS n FROM alert WHERE state = 'open'").fetchone()["n"] == 0


def test_silence_alert_once_after_threshold(db):
    worker_id(db)
    assert not check_silence(db, THURSDAY + timedelta(days=13))
    assert check_silence(db, THURSDAY + timedelta(days=15))
    assert check_silence(db, THURSDAY + timedelta(days=16))      # повторное событие обновляет ту же причину
    assert db.execute("SELECT count(*) AS n FROM alert WHERE cause_key = 'asr_silent'").fetchone()["n"] == 1


def test_status_counts_every_state_once(db):
    add_video(db, 1, state="published")
    add_video(db, 2, state="retry")
    add_video(db, 3, state="cancelled")
    db.execute("UPDATE asr_job SET reason = 'subtitles' WHERE item_id = 3")
    add_video(db, 4, state="audio_unavailable")
    status = build_status(db, THURSDAY)
    out = status["outcomes"]
    assert (out["recognized"], out["temporary"], out["replaced_by_subtitles"], out["audio_unavailable"],
            out["waiting"]) == (1, 1, 1, 1, 0)
    assert sum(out.values()) == 4
    text = status_text(status)
    assert "заменено субтитрами 1" in text and "индексатор:" in text
