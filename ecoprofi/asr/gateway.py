"""API распознавателя /asr/v1/ (contracts/asr-api.md).

Внутренний сервис asr-gateway на VPS: шлюз nginx проксирует к нему префикс
/asr/v1/. Распознаватель на ПК предъявляет ключ (Bearer); сервер ведёт очередь,
сам готовит звук доступом оператора и публикует расшифровку. Временный адрес
VK клиенту не передаётся ни в каком виде (R1). В журнал не пишутся ключ,
полный lease_id и адреса.

Запуск: uvicorn ecoprofi.asr.gateway:app --host 0.0.0.0 --port 8100 --no-access-log
"""
import gzip
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ecoprofi.asr import VRAM_REQUIRED_MB, audio, jobs, publish, short_lease, window, workers
from ecoprofi.asr.jobs import LeaseInvalid

log = logging.getLogger("ecoprofi.asr.gateway")

MAX_RESULT_BYTES = 64 * 1024 * 1024     # после распаковки gzip
MAINTENANCE_EVERY = 60
PREPARE_EVERY = 5
SILENCE_CHECK_EVERY = 3600
CHUNK = 1 << 16


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", retryable: bool = False, headers=None):
        super().__init__(code)
        self.status, self.code, self.message, self.retryable, self.headers = status, code, message, retryable, headers


def _error(status: int, code: str, message: str = "", retryable: bool = False, headers=None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message or code, "retryable": retryable}},
                        status_code=status, headers=headers)


def _iso(value):
    return value.isoformat() if value else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _run_body(run: dict | None):
    return None if run is None else {"id": run["id"], "kind": run["kind"], "until": _iso(run["until"])}


def _job_body(job: dict) -> dict:
    lease = str(job["lease_id"])
    return {"lease_id": lease, "lease_expires_at": _iso(job["lease_expires_at"]),
            "deadline_at": _iso(job["deadline_at"]),
            "job": {"ref": jobs.ref(job["owner_id"], job["item_id"]), "duration_s": job["duration_s"],
                    "source_version": job["source_version"]},
            "audio": {"href": f"/asr/v1/jobs/{lease}/audio", "format": "ogg/opus", "sample_rate": 16000,
                      "channels": 1}}


def validate_result(body: dict, job: dict) -> str | None:
    """Причина отклонения результата или None (contracts: 422 invalid_result)."""
    if not isinstance(body, dict):
        return "тело не объект"
    for name in ("processor_version", "language"):
        if not isinstance(body.get(name), str) or not body[name].strip():
            return f"нет поля {name}"
    for name in ("audio_seconds", "processing_seconds"):
        if not isinstance(body.get(name), (int, float)) or body[name] < 0:
            return f"нет поля {name}"
    if job.get("audio_sha256") and body.get("audio_sha256") != job["audio_sha256"]:
        return "audio_sha256 не совпадает"
    segments = body.get("segments")
    if not isinstance(segments, list) or not segments:
        return "пустой массив segments"
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("text"), str):
            return "отрезок без текста"
        start, end = seg.get("start_ms"), seg.get("end_ms")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            return "нарушен интервал отрезка"
    return None


def create_app(*, connect_fn=None, vk_factory=None, ai_factory=None, tmp_dir: Path | None = None,
               background: bool = True, clock=_now) -> FastAPI:
    if connect_fn is None:
        from ecoprofi.db import connect

        def connect_fn():
            conn = connect(vector=False)
            conn.autocommit = True
            return conn
    if vk_factory is None:
        def vk_factory():
            from ecoprofi.indexer.context import make_vk
            return make_vk()
    if ai_factory is None:
        def ai_factory():
            from ecoprofi.indexer.context import make_ai
            return make_ai()
    tmp = tmp_dir or audio.TMP_DIR
    stop = threading.Event()

    def background_loop(step, every: float, name: str):
        def loop():
            while not stop.is_set():
                try:
                    step()
                except Exception as exc:   # сбой одного шага не останавливает сервис
                    log.warning("%s: %s", name, audio.scrub(f"{type(exc).__name__}: {exc}"))
                stop.wait(every)
        thread = threading.Thread(target=loop, name=name, daemon=True)
        thread.start()
        return thread

    state = {"vk": None, "ai": None, "silence_checked": 0.0}

    def prepare_step():
        """Подготовка звука: одно задание за раз."""
        with connect_fn() as conn:
            job = conn.execute("SELECT * FROM asr_job WHERE state = 'preparing' AND lease_expires_at > %s "
                               "ORDER BY updated_at LIMIT 1", (clock(),)).fetchone()
            if job is None:
                return
            if state["vk"] is None:
                state["vk"] = vk_factory()
            audio.prepare(conn, state["vk"], job, directory=tmp)

    def maintenance_step():
        with connect_fn() as conn:
            now = clock()
            for key in jobs.expire_leases(conn, now):
                log.info("аренда истекла: %s", key)
            for run_id, outcome in jobs.close_inactive_runs(conn, now):
                log.info("запуск %s закрыт: %s", run_id, outcome)
            audio.cleanup(conn, tmp)
            if state["ai"] is None:
                state["ai"] = ai_factory()
            publish.publish_pending(conn, state["ai"], now)
            if time.monotonic() - state["silence_checked"] >= SILENCE_CHECK_EVERY:
                check_silence(conn, now)
                state["silence_checked"] = time.monotonic()

    @asynccontextmanager
    async def lifespan(app):
        threads = []
        if background:
            with connect_fn() as conn:
                # Извлечение звука прервано остановкой: задание вернётся в очередь без расхода попыток.
                conn.execute("UPDATE asr_job SET audio_path = NULL, state = 'queued', lease_id = NULL, "
                             "lease_worker_id = NULL, lease_expires_at = NULL, deadline_at = NULL "
                             "WHERE state = 'preparing'")
                removed = audio.cleanup(conn, tmp)
                if removed:
                    log.info("удалены временные файлы после остановки: %d", removed)
            threads = [background_loop(prepare_step, PREPARE_EVERY, "asr-prepare"),
                       background_loop(maintenance_step, MAINTENANCE_EVERY, "asr-maintenance")]
        yield
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    app = FastAPI(title="ecoprofi asr-gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.prepare_step = prepare_step
    app.state.maintenance_step = maintenance_step

    @app.exception_handler(ApiError)
    async def api_error(request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message, exc.retryable, exc.headers)

    @app.exception_handler(Exception)
    async def internal_error(request, exc: Exception):
        log.error("внутренняя ошибка: %s", audio.scrub(f"{type(exc).__name__}: {exc}"))
        return _error(500, "internal", "Внутренняя ошибка.", True)

    def db():
        conn = connect_fn()
        try:
            yield conn
        finally:
            conn.close()

    def worker(request: Request, conn=Depends(db)) -> dict:
        found = workers.authenticate(conn, request.headers.get("authorization"))
        if found is None:
            raise ApiError(401, "unauthorized")
        workers.touch(conn, found["id"])
        return found

    async def body_json(request: Request, limit: int = 1024 * 1024) -> dict:
        raw = await request.body()
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.GzipFile(fileobj=__import__("io").BytesIO(raw)).read(MAX_RESULT_BYTES + 1)
            except OSError:
                raise ApiError(400, "bad_request", "Тело не является gzip.") from None
            limit = MAX_RESULT_BYTES
        if len(raw) > limit:
            raise ApiError(413, "payload_too_large")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(400, "bad_request", "Тело не является JSON.") from None

    def own_job(conn, lease_id: str, me: dict, *, lock=False) -> dict:
        job = jobs.by_lease(conn, lease_id, lock=lock)
        if job is None or job["lease_worker_id"] != me["id"]:
            raise ApiError(409, "lease_invalid", "Аренда не действует.")
        return job

    @app.post("/asr/v1/session")
    async def session(request: Request, me=Depends(worker), conn=Depends(db)):
        body = await body_json(request)
        device = body.get("device") if isinstance(body.get("device"), dict) else None
        workers.touch(conn, me["id"], str(body.get("version") or "")[:100] or None, device)
        from ecoprofi.indexer import alerts
        alerts.close_alert(conn, "asr_silent")
        now = clock()
        sched = jobs.schedule(conn)
        compute = str(body.get("compute_type") or "float16")
        free = (device or {}).get("vram_free_mb")
        if isinstance(free, (int, float)) and free < VRAM_REQUIRED_MB.get(compute, VRAM_REQUIRED_MB["float16"]):
            current = jobs.open_run(conn)
            if current:
                jobs.finish_run(conn, current["id"], "resource_unavailable", now)
            log.info("видеопамяти недостаточно: %s МБ свободно", free)
            run = None
        else:
            run = jobs.open_or_get_run(conn, me["id"], now)
        return {"run": _run_body(run), "paused": sched["paused"], "next_due_at": _iso(window.next_due_at(sched, now))}

    @app.post("/asr/v1/jobs/lease")
    async def lease(request: Request, me=Depends(worker), conn=Depends(db)):
        body = await body_json(request)
        now = clock()
        run = jobs.open_run(conn)
        if run is None or run["id"] != body.get("run_id") or not jobs.run_active(run, jobs.schedule(conn), now):
            raise ApiError(409, "window_closed", "Окно распознавания закрыто.")
        job = jobs.lease_next(conn, run, me["id"], now)
        if job is None:
            return Response(status_code=204)
        log.info("задание %s выдано, аренда %s", jobs.ref(job["owner_id"], job["item_id"]),
                 short_lease(job["lease_id"]))
        return _job_body(job)

    @app.get("/asr/v1/jobs/{lease_id}/audio")
    def get_audio(lease_id: str, request: Request, me=Depends(worker), conn=Depends(db)):
        job = own_job(conn, lease_id, me)
        now = clock()
        if job["state"] == "preparing" and jobs.lease_valid(job, now, ("preparing",)):
            return Response(status_code=202, headers={"Retry-After": "15"})
        if job["state"] == "audio_unavailable":
            raise ApiError(410, "audio_unavailable", f"Звук недоступен: {job['reason']}.")
        if not jobs.lease_valid(job, now) or not job["audio_path"] or not Path(job["audio_path"]).exists():
            raise ApiError(409, "lease_invalid", "Аренда не действует.")
        path = Path(job["audio_path"])
        size = path.stat().st_size
        start, status = 0, 200
        header = request.headers.get("range", "")
        if header.startswith("bytes="):
            try:
                start = int(header[6:].split("-", 1)[0] or 0)
            except ValueError:
                start = 0
            if start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206 if start else 200

        def stream():
            with path.open("rb") as handle:
                handle.seek(start)
                while chunk := handle.read(CHUNK):
                    yield chunk

        headers = {"Content-Length": str(size - start), "Accept-Ranges": "bytes",
                   "X-Audio-Sha256": job["audio_sha256"] or ""}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
        return StreamingResponse(stream(), status_code=status, media_type="audio/ogg", headers=headers)

    @app.post("/asr/v1/jobs/{lease_id}/heartbeat")
    async def heartbeat(lease_id: str, request: Request, me=Depends(worker), conn=Depends(db)):
        await body_json(request)
        own_job(conn, lease_id, me)
        now = clock()
        try:
            job = jobs.heartbeat(conn, lease_id, now)
        except LeaseInvalid:
            raise ApiError(409, "lease_invalid", "Аренда не действует.") from None
        run = conn.execute("SELECT * FROM asr_run WHERE id = %s", (job["run_id"],)).fetchone()
        return {"lease_expires_at": _iso(job["lease_expires_at"]),
                "continue": jobs.run_active(run, jobs.schedule(conn), now)}

    @app.post("/asr/v1/jobs/{lease_id}/result")
    async def result(lease_id: str, request: Request, me=Depends(worker), conn=Depends(db)):
        body = await body_json(request)
        job = own_job(conn, lease_id, me)
        now = clock()
        if job["state"] in ("transcribed", "published", "no_speech"):
            return JSONResponse({"state": job["state"]}, status_code=200)
        if not jobs.lease_valid(job, now):
            raise ApiError(409, "lease_invalid", "Аренда не действует.")
        problem = validate_result(body, job)
        if problem:
            jobs.reject_result(conn, lease_id, now)
            raise ApiError(422, "invalid_result", problem)
        try:
            saved, created = jobs.save_result(conn, lease_id, body, now)
        except LeaseInvalid:
            raise ApiError(409, "lease_invalid", "Аренда не действует.") from None
        if job["audio_path"]:
            Path(job["audio_path"]).unlink(missing_ok=True)
        log.info("расшифровка принята: %s, %d отрезков", jobs.ref(job["owner_id"], job["item_id"]),
                 len(body["segments"]))
        return JSONResponse({"state": saved}, status_code=202 if created else 200)

    @app.post("/asr/v1/jobs/{lease_id}/failure")
    async def failure(lease_id: str, request: Request, me=Depends(worker), conn=Depends(db)):
        body = await body_json(request)
        kind = body.get("kind")
        if kind not in ("decode", "gpu_unavailable", "timeout", "internal"):
            raise ApiError(400, "bad_request", "kind: decode | gpu_unavailable | timeout | internal")
        job = own_job(conn, lease_id, me)
        try:
            state_after = jobs.fail(conn, lease_id, kind, clock())
        except LeaseInvalid:
            raise ApiError(409, "lease_invalid", "Аренда не действует.") from None
        if job["audio_path"]:
            Path(job["audio_path"]).unlink(missing_ok=True)
        log.info("неудача на ПК: %s, %s → %s", jobs.ref(job["owner_id"], job["item_id"]), kind, state_after)
        return {"state": state_after}

    @app.post("/asr/v1/jobs/{lease_id}/release")
    async def release(lease_id: str, me=Depends(worker), conn=Depends(db)):
        job = own_job(conn, lease_id, me)
        try:
            jobs.release(conn, lease_id, clock())
        except LeaseInvalid:
            raise ApiError(409, "lease_invalid", "Аренда не действует.") from None
        if job["audio_path"]:
            Path(job["audio_path"]).unlink(missing_ok=True)
        return {"state": "queued"}

    @app.post("/asr/v1/runs/{run_id}/finish")
    async def finish(run_id: int, request: Request, me=Depends(worker), conn=Depends(db)):
        body = await body_json(request)
        outcome = body.get("outcome")
        if outcome not in ("queue_empty", "window_closed", "paused", "error"):
            raise ApiError(400, "bad_request", "outcome: queue_empty | window_closed | paused | error")
        jobs.finish_run(conn, run_id, outcome, clock())
        return {"run_id": run_id, "outcome": outcome}

    return app


def check_silence(conn, now: datetime) -> bool:
    """Оповещение asr_silent: ПК не выходил на связь дольше заданного срока (FR-17)."""
    from ecoprofi.indexer import alerts
    sched = jobs.schedule(conn)
    row = conn.execute("SELECT max(last_seen_at) AS seen FROM asr_worker WHERE revoked_at IS NULL").fetchone()
    if row["seen"] is None:
        return False
    days = sched["silence_alert_days"]
    if (now - row["seen"]).total_seconds() < days * 86400:
        return False
    alerts.open_alert(conn, "asr_silent", alerts.text_asr_silent(days))
    return True


def _configure_logging():
    logging.basicConfig(level=os.environ.get("ASR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _lazy_app():
    _configure_logging()
    return create_app()


class _App:
    """Приложение создаётся при первом обращении uvicorn: импорт модуля в тестах не подключается к БД."""
    _app = None

    async def __call__(self, scope, receive, send):
        if _App._app is None:
            _App._app = _lazy_app()
        await _App._app(scope, receive, send)


app = _App()
