"""Очередь распознавания: задания, аренда, переходы состояний, запуски.

Переходы — диаграмма data-model.md. Каждая операция — одна транзакция;
задание блокируется SELECT … FOR UPDATE. Результат принимается только по
действующей аренде (FR-08). Одновременно идёт не больше одного запуска и у
распознавателя не больше одного задания.
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from ecoprofi.asr import (
    ATTEMPTS_PER_RUN, AUDIO_RECHECK, DEADLINE_EXTRA, DEADLINE_FACTOR, LEASE_TTL, PERMANENT_AFTER_RUNS, RETRY_DELAY,
    window,
)

FINAL_INITIAL = ("published", "audio_unavailable", "no_speech", "permanent", "cancelled")
ACTIVE = ("preparing", "leased")

ALLOWED = {
    "queued": {"preparing", "cancelled", "queued"},
    "preparing": {"leased", "audio_unavailable", "retry", "queued", "cancelled"},
    "leased": {"transcribed", "retry", "queued", "cancelled", "permanent"},
    "transcribed": {"published", "no_speech", "queued", "cancelled", "transcribed"},
    "retry": {"queued", "preparing", "permanent", "cancelled"},
    "published": {"queued", "cancelled"},
    "no_speech": {"queued", "cancelled", "preparing"},
    "audio_unavailable": {"preparing", "queued", "cancelled"},
    "permanent": {"queued", "cancelled", "preparing"},
    "cancelled": {"queued", "cancelled"},
}

# Исход FR-14 для сводки: каждое состояние — ровно один исход.
OUTCOME = {
    "published": "recognized", "queued": "waiting", "preparing": "waiting", "leased": "waiting",
    "transcribed": "waiting", "retry": "temporary", "audio_unavailable": "audio_unavailable",
    "no_speech": "no_speech", "permanent": "permanent",
}


class TransitionError(RuntimeError):
    """Недопустимый переход состояния задания."""


class LeaseInvalid(RuntimeError):
    """Аренда истекла, отменена или передана другому исполнителю."""


def ref(owner_id: int, item_id: int) -> str:
    return f"{owner_id}_{item_id}"


def parse_ref(text: str) -> tuple[int, int]:
    owner, _, item = str(text).strip().removeprefix("video").rpartition("_")
    try:
        return int(owner), int(item)
    except ValueError:
        raise ValueError("Запись задаётся как <owner>_<item>, например -95098598_456239017.") from None


def source_version(owner_id: int, item_id: int, duration, date) -> str | None:
    """Версия источника из полей video.get. Без длительности и даты — не определена."""
    if duration is None and date is None:
        return None
    payload = json.dumps([owner_id, item_id, duration, date])
    return hashlib.sha256(payload.encode()).hexdigest()


def outcome_of(state: str, reason: str | None) -> str:
    if state == "cancelled":
        return "replaced_by_subtitles" if reason == "subtitles" else "removed_deleted"
    return OUTCOME[state]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_job(conn, owner_id: int, item_id: int) -> dict | None:
    return conn.execute("SELECT * FROM asr_job WHERE owner_id = %s AND item_id = %s FOR UPDATE",
                        (owner_id, item_id)).fetchone()


def _update(conn, job: dict, to_state: str, **fields) -> None:
    if to_state not in ALLOWED[job["state"]]:
        raise TransitionError(f"{ref(job['owner_id'], job['item_id'])}: {job['state']} → {to_state}")
    fields["state"] = to_state
    sets = ", ".join(f"{name} = %({name})s" for name in fields)
    conn.execute(f"UPDATE asr_job SET {sets}, updated_at = now() WHERE owner_id = %(o)s AND item_id = %(i)s",
                 {**fields, "o": job["owner_id"], "i": job["item_id"]})
    job.update(fields)


NO_LEASE = {"lease_id": None, "lease_worker_id": None, "lease_expires_at": None, "deadline_at": None}


def transition(conn, owner_id: int, item_id: int, to_state: str, *, expected_lease=None, **fields) -> dict:
    """Перевести задание с проверкой перехода и аренды. Возвращает обновлённую строку."""
    with conn.transaction():
        job = _lock_job(conn, owner_id, item_id)
        if job is None:
            raise TransitionError(f"Задание {ref(owner_id, item_id)} не найдено.")
        if expected_lease is not None and str(job["lease_id"]) != str(expected_lease):
            raise LeaseInvalid()
        _update(conn, job, to_state, **fields)
        if to_state in FINAL_INITIAL:
            check_initial_done(conn)
    return job


# --- связь с индексатором (R7) ---

def enqueue(conn, material_id: int, owner_id: int, item_id: int, version: str | None) -> None:
    """Запись без пригодной дорожки: создать задание или обновить версию источника.

    Вызывается индексатором в транзакции публикации. NULL у задания из миграции
    заполняется без сброса; смена непустой версии возвращает задание в очередь.
    """
    job = _lock_job(conn, owner_id, item_id)
    if job is None:
        conn.execute("INSERT INTO asr_job (owner_id, item_id, material_id, source_version) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT DO NOTHING", (owner_id, item_id, material_id, version))
        return
    if version is None or job["source_version"] == version:
        return
    if job["source_version"] is None:
        conn.execute("UPDATE asr_job SET source_version = %s, material_id = %s WHERE owner_id = %s AND item_id = %s",
                     (version, material_id, owner_id, item_id))
        return
    if job["state"] == "cancelled" and job["reason"] == "subtitles":
        conn.execute("UPDATE asr_job SET source_version = %s WHERE owner_id = %s AND item_id = %s",
                     (version, owner_id, item_id))
        return
    _update(conn, job, "queued", source_version=version, material_id=material_id, attempts=0, failed_runs=0,
            reason=None, retry_at=None, audio_path=None, **NO_LEASE)


def cancel(conn, owner_id: int, item_id: int, reason: str) -> bool:
    """Субтитры появились или удаление подтверждено: задание снимается, аренда аннулируется."""
    job = _lock_job(conn, owner_id, item_id)
    if job is None or job["state"] == "cancelled" and job["reason"] == reason:
        return False
    _update(conn, job, "cancelled", reason=reason, retry_at=None, audio_path=None, **NO_LEASE)
    if job["initial"]:
        check_initial_done(conn)
    return True


# --- график и запуски ---

def schedule(conn, *, lock: bool = False) -> dict:
    return conn.execute("SELECT * FROM asr_schedule" + (" FOR UPDATE" if lock else "")).fetchone()


def open_run(conn) -> dict | None:
    return conn.execute("SELECT * FROM asr_run WHERE finished_at IS NULL").fetchone()


def _last_scheduled_outcome(conn) -> str | None:
    row = conn.execute("SELECT outcome FROM asr_run WHERE kind IN ('scheduled', 'catch_up') "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return row["outcome"] if row else None


def open_or_get_run(conn, worker_id: int, now: datetime | None = None) -> dict | None:
    """Вернуть открытый запуск или открыть новый, если окно открыто. Иначе None."""
    now = now or _now()
    with conn.transaction():
        sched = schedule(conn, lock=True)
        run = open_run(conn)
        if run is not None:
            if not run_active(run, sched, now):
                return None
            return run
        win = window.is_open(sched, now, last_outcome=_last_scheduled_outcome(conn))
        if win is None:
            return None
        run = conn.execute(
            "INSERT INTO asr_run (worker_id, kind, only_refs, started_at, until) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING *", (worker_id, win.kind, list(win.only) if win.only else None, now, win.until)).fetchone()
        updates = {}
        if win.kind == "manual":
            updates.update(manual_requested_at=None, manual_only=None)
        if win.due is not None:
            updates["last_due_at"] = win.due
        if win.kind == "initial" and sched["initial_pass"] == "pending":
            updates.update(initial_pass="running", initial_started_at=now)
        if updates:
            sets = ", ".join(f"{k} = %({k})s" for k in updates)
            conn.execute(f"UPDATE asr_schedule SET {sets}", updates)
    return run


def run_active(run: dict, sched: dict, now: datetime) -> bool:
    """Запуск ещё идёт: окно не закрылось и нет паузы (запуск по списку паузу не учитывает)."""
    if run is None or run["finished_at"] is not None:
        return False
    if run["until"] is not None and now >= run["until"]:
        return False
    if sched["paused"] and not run["only_refs"]:
        return False
    return True


def finish_run(conn, run_id: int, outcome: str, now: datetime | None = None) -> bool:
    with conn.transaction():
        row = conn.execute("UPDATE asr_run SET finished_at = %s, outcome = %s WHERE id = %s AND finished_at IS NULL "
                           "RETURNING id", (now or _now(), outcome, run_id)).fetchone()
    return row is not None


def close_inactive_runs(conn, now: datetime | None = None) -> list[tuple[int, str]]:
    """Закрыть запуски: потеря связи с ПК, конец окна или пауза без активных аренд."""
    now = now or _now()
    closed = []
    with conn.transaction():
        run = conn.execute("SELECT r.*, w.last_seen_at FROM asr_run r LEFT JOIN asr_worker w ON w.id = r.worker_id "
                           "WHERE r.finished_at IS NULL FOR UPDATE OF r").fetchone()
        if run is None:
            return closed
        sched = schedule(conn)
        active = conn.execute("SELECT count(*) AS n FROM asr_job WHERE run_id = %s AND state = ANY(%s) "
                              "AND lease_expires_at > %s", (run["id"], list(ACTIVE), now)).fetchone()["n"]
        outcome = None
        if run["last_seen_at"] is None or run["last_seen_at"] < now - LEASE_TTL:
            outcome = "worker_lost"
        elif not run_active(run, sched, now) and active == 0:
            outcome = "paused" if sched["paused"] and not run["only_refs"] else "window_closed"
        if outcome:
            conn.execute("UPDATE asr_run SET finished_at = %s, outcome = %s WHERE id = %s", (now, outcome, run["id"]))
            closed.append((run["id"], outcome))
    return closed


# --- аренда ---

def _deadline(duration_s, start: datetime) -> datetime | None:
    if not duration_s:
        return None
    return start + timedelta(seconds=float(duration_s) * DEADLINE_FACTOR) + DEADLINE_EXTRA


def lease_next(conn, run: dict, worker_id: int, now: datetime | None = None) -> dict | None:
    """Выдать одно задание. Действующая аренда распознавателя возвращается повторно."""
    now = now or _now()
    with conn.transaction():
        current = conn.execute(
            "SELECT * FROM asr_job WHERE lease_worker_id = %s AND state = ANY(%s) AND lease_expires_at > %s "
            "FOR UPDATE", (worker_id, list(ACTIVE), now)).fetchone()
        if current is not None:
            return current
        if run["only_refs"]:
            job = conn.execute(
                "SELECT * FROM asr_job WHERE (owner_id::text || '_' || item_id::text) = ANY(%s) "
                "AND state IN ('queued', 'retry', 'audio_unavailable', 'no_speech', 'permanent') "
                "ORDER BY updated_at LIMIT 1 FOR UPDATE SKIP LOCKED", (run["only_refs"],)).fetchone()
        else:
            job = conn.execute(
                "SELECT * FROM asr_job WHERE state = 'queued' "
                "OR (state = 'retry' AND (retry_at IS NULL OR retry_at <= %(now)s)) "
                "OR (state = 'audio_unavailable' AND updated_at <= %(recheck)s) "
                "ORDER BY initial DESC, (state = 'queued') DESC, updated_at "
                "LIMIT 1 FOR UPDATE SKIP LOCKED", {"now": now, "recheck": now - AUDIO_RECHECK}).fetchone()
        if job is None:
            return None
        attempts = job["attempts"] if job["run_id"] == run["id"] else 0
        _update(conn, job, "preparing", lease_id=uuid.uuid4(), lease_worker_id=worker_id,
                lease_expires_at=now + LEASE_TTL, deadline_at=None, run_id=run["id"], attempts=attempts,
                retry_at=None, audio_path=None, audio_sha256=None)
        conn.execute("UPDATE asr_run SET taken = taken + 1 WHERE id = %s", (run["id"],))
    return job


def by_lease(conn, lease_id, *, lock: bool = False) -> dict | None:
    try:
        value = uuid.UUID(str(lease_id))
    except ValueError:
        return None
    return conn.execute("SELECT * FROM asr_job WHERE lease_id = %s" + (" FOR UPDATE" if lock else ""),
                        (value,)).fetchone()


def lease_valid(job: dict | None, now: datetime, states=("leased",)) -> bool:
    return bool(job and job["state"] in states and job["lease_expires_at"] and job["lease_expires_at"] > now)


def prepared(conn, job: dict, *, path: str, size: int, sha256: str, duration_s: int | None,
             version: str | None, now: datetime | None = None) -> None:
    """Звук готов: задание переходит в leased, считается предел на запись."""
    now = now or _now()
    with conn.transaction():
        current = _lock_job(conn, job["owner_id"], job["item_id"])
        if current is None or current["lease_id"] != job["lease_id"] or current["state"] != "preparing":
            raise LeaseInvalid()
        if version and current["source_version"] and current["source_version"] != version:
            # Запись изменилась у владельца: распознаётся новая версия, со сбросом счётчиков.
            _update(conn, current, "queued", source_version=version, attempts=0, failed_runs=0, reason=None,
                    audio_path=None, **NO_LEASE)
        else:
            _update(conn, current, "leased", audio_path=path, audio_bytes=size, audio_sha256=sha256,
                    duration_s=duration_s, source_version=current["source_version"] or version,
                    deadline_at=_deadline(duration_s, now), lease_expires_at=now + LEASE_TTL)
            return
    raise LeaseInvalid()


def audio_unavailable(conn, job: dict, reason: str) -> None:
    with conn.transaction():
        current = _lock_job(conn, job["owner_id"], job["item_id"])
        if current is None or current["lease_id"] != job["lease_id"]:
            raise LeaseInvalid()
        _update(conn, current, "audio_unavailable", reason=reason, audio_path=None,
                lease_expires_at=None, deadline_at=None)
        if current["run_id"]:
            conn.execute("UPDATE asr_run SET failed = failed + 1 WHERE id = %s", (current["run_id"],))
        if current["initial"]:
            check_initial_done(conn)


def heartbeat(conn, lease_id, now: datetime | None = None) -> dict:
    """Продлить аренду. Возвращает задание; LeaseInvalid — аренда не действует."""
    now = now or _now()
    with conn.transaction():
        job = by_lease(conn, lease_id, lock=True)
        if not lease_valid(job, now, ACTIVE):
            raise LeaseInvalid()
        conn.execute("UPDATE asr_job SET lease_expires_at = %s WHERE owner_id = %s AND item_id = %s",
                     (now + LEASE_TTL, job["owner_id"], job["item_id"]))
        job["lease_expires_at"] = now + LEASE_TTL
    return job


def release(conn, lease_id, now: datetime | None = None) -> dict | None:
    """Вернуть задание без результата и без расхода попыток."""
    now = now or _now()
    with conn.transaction():
        job = by_lease(conn, lease_id, lock=True)
        if not lease_valid(job, now, ACTIVE):
            raise LeaseInvalid()
        _update(conn, job, "queued", audio_path=None, **NO_LEASE)
    return job


def _defer_or_retry(conn, job: dict, reason: str, now: datetime) -> str:
    """Временный сбой: повтор в запуске, после лимита — до следующего запуска, затем постоянный сбой."""
    attempts = job["attempts"] + 1
    run = conn.execute("SELECT * FROM asr_run WHERE id = %s", (job["run_id"],)).fetchone() if job["run_id"] else None
    if run:
        conn.execute("UPDATE asr_run SET failed = failed + 1 WHERE id = %s", (run["id"],))
    if attempts < ATTEMPTS_PER_RUN:
        _update(conn, job, "retry", reason=reason, attempts=attempts, retry_at=now + RETRY_DELAY * attempts,
                audio_path=None, lease_expires_at=None, deadline_at=None)
        return "retry"
    failed_runs = job["failed_runs"] + 1
    if failed_runs >= PERMANENT_AFTER_RUNS:
        _update(conn, job, "permanent", reason=reason, attempts=attempts, failed_runs=failed_runs, retry_at=None,
                audio_path=None, lease_expires_at=None, deadline_at=None)
        from ecoprofi.indexer import alerts
        alerts.open_alert(conn, f"asr_permanent:{ref(job['owner_id'], job['item_id'])}",
                          alerts.text_asr_permanent(ref(job["owner_id"], job["item_id"]), reason))
        if job["initial"]:
            check_initial_done(conn)
        return "permanent"
    until = run["until"] if run and run["until"] else now + timedelta(hours=6)
    _update(conn, job, "retry", reason=reason, attempts=attempts, failed_runs=failed_runs,
            retry_at=max(until, now + RETRY_DELAY), audio_path=None, lease_expires_at=None, deadline_at=None)
    return "retry"


def fail(conn, lease_id, kind: str, now: datetime | None = None) -> str:
    """Неудача на ПК (contracts/asr-api.md, failure)."""
    now = now or _now()
    with conn.transaction():
        job = by_lease(conn, lease_id, lock=True)
        if not lease_valid(job, now, ACTIVE):
            raise LeaseInvalid()
        if kind == "gpu_unavailable":
            _update(conn, job, "queued", audio_path=None, **NO_LEASE)
            if job["run_id"]:
                conn.execute("UPDATE asr_run SET finished_at = %s, outcome = 'resource_unavailable' "
                             "WHERE id = %s AND finished_at IS NULL", (now, job["run_id"]))
            return "queued"
        reason = {"decode": "decode", "timeout": "timeout"}.get(kind, "server")
        return _defer_or_retry(conn, job, reason, now)


def save_result(conn, lease_id, body: dict, now: datetime | None = None) -> tuple[str, bool]:
    """Сохранить расшифровку. (состояние, новая ли) — повтор того же результата дублей не создаёт."""
    now = now or _now()
    from psycopg.types.json import Jsonb
    with conn.transaction():
        job = by_lease(conn, lease_id, lock=True)
        if job and job["state"] in ("transcribed", "published", "no_speech"):
            return job["state"], False
        if not lease_valid(job, now):
            raise LeaseInvalid()
        conn.execute(
            "INSERT INTO asr_transcript (owner_id, item_id, source_version, processor_version, language, segments, "
            "audio_seconds, processing_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (owner_id, item_id, source_version, processor_version) DO UPDATE SET "
            "segments = excluded.segments, language = excluded.language, "
            "audio_seconds = excluded.audio_seconds, processing_seconds = excluded.processing_seconds, "
            "version_id = NULL, created_at = now()",
            (job["owner_id"], job["item_id"], job["source_version"], body["processor_version"], body["language"],
             Jsonb(body["segments"]), body["audio_seconds"], body["processing_seconds"]))
        _update(conn, job, "transcribed", audio_path=None, retry_at=None, lease_expires_at=None, deadline_at=None)
        if job["run_id"]:
            conn.execute("UPDATE asr_run SET audio_seconds = audio_seconds + %s, "
                         "processing_seconds = processing_seconds + %s WHERE id = %s",
                         (body["audio_seconds"], body["processing_seconds"], job["run_id"]))
    return "transcribed", True


def reject_result(conn, lease_id, now: datetime | None = None) -> None:
    """Результат не прошёл проверку: временный сбой разбора."""
    now = now or _now()
    with conn.transaction():
        job = by_lease(conn, lease_id, lock=True)
        if lease_valid(job, now):
            _defer_or_retry(conn, job, "decode", now)


def expire_leases(conn, now: datetime | None = None) -> list[str]:
    """Истёкшие аренды возвращаются в очередь без расхода попыток; превышение предела — тайм-аут."""
    now = now or _now()
    expired = []
    with conn.transaction():
        for job in conn.execute("SELECT * FROM asr_job WHERE state = ANY(%s) AND (lease_expires_at <= %s "
                                "OR deadline_at <= %s) FOR UPDATE SKIP LOCKED", (list(ACTIVE), now, now)).fetchall():
            if job["deadline_at"] and job["deadline_at"] <= now:
                _defer_or_retry(conn, job, "timeout", now)
            else:
                _update(conn, job, "queued", audio_path=None, **NO_LEASE)
            expired.append(ref(job["owner_id"], job["item_id"]))
    return expired


def check_initial_done(conn, now: datetime | None = None) -> bool:
    """Первичный проход завершён, когда у каждого задания initial есть итоговый исход (FR-19)."""
    left = conn.execute("SELECT count(*) AS n FROM asr_job WHERE initial AND NOT (state = ANY(%s))",
                        (list(FINAL_INITIAL),)).fetchone()["n"]
    if left:
        return False
    row = conn.execute("UPDATE asr_schedule SET initial_pass = 'done', initial_done_at = %s "
                       "WHERE initial_pass <> 'done' RETURNING id", (now or _now(),)).fetchone()
    if row:
        conn.execute("UPDATE asr_run SET finished_at = %s, outcome = 'queue_empty' "
                     "WHERE finished_at IS NULL AND kind = 'initial'", (now or _now(),))
    return True


# --- команды оператора ---

def requeue(conn, owner_id: int, item_id: int) -> bool:
    with conn.transaction():
        job = _lock_job(conn, owner_id, item_id)
        if job is None or job["state"] in ACTIVE:
            return False
        _update(conn, job, "queued", attempts=0, failed_runs=0, reason=None, retry_at=None, audio_path=None,
                **NO_LEASE)
        from ecoprofi.indexer import alerts
        alerts.close_alert(conn, f"asr_permanent:{ref(owner_id, item_id)}")
    return True


def reprocess(conn, processor_version: str) -> int:
    """Поставить в очередь записи, распознанные другой версией распознавателя (FR-18)."""
    with conn.transaction():
        rows = conn.execute(
            "SELECT j.owner_id, j.item_id FROM asr_job j WHERE j.state IN ('published', 'no_speech') "
            "AND NOT EXISTS (SELECT 1 FROM asr_transcript t WHERE t.owner_id = j.owner_id AND t.item_id = j.item_id "
            "AND t.source_version = j.source_version AND t.processor_version = %s) FOR UPDATE", (processor_version,)
        ).fetchall()
        for row in rows:
            job = _lock_job(conn, row["owner_id"], row["item_id"])
            _update(conn, job, "queued", attempts=0, failed_runs=0, reason=None, retry_at=None, **NO_LEASE)
    return len(rows)
