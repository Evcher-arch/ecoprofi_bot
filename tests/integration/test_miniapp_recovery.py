"""T044: падение после claim, второй worker, поздний commit, 180 с, задержки, истечение сессии и очистка."""
import uuid
from unittest.mock import patch

from ecoprofi.bot.miniapp import worker as worker_module
from ecoprofi.bot.miniapp.worker import LOCK_ID, Maintenance, MiniAppWorker


def _ask(miniapp, session, text="загрязняющих веществ"):
    request_id = uuid.uuid4()
    miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                 {"request_id": str(request_id), "text": text}, token=session["token"])
    return request_id


def _row(miniapp, request_id):
    return miniapp.owner.execute("SELECT * FROM miniapp_request WHERE request_id = %s", (request_id,)).fetchone()


def _claim(worker):
    """Claim без обработки: имитация падения процесса сразу после перевода в running."""
    captured = {}
    with patch.object(MiniAppWorker, "_process", lambda self, row, attempt: captured.update(row=row, attempt=attempt)):
        assert worker.run_once()
    return captured["row"], captured["attempt"]


def test_crash_after_claim_recovery_blocks_late_commit(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    first = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    row, old_attempt = _claim(first)
    assert _row(miniapp, request_id)["state"] == "running"
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).recover()     # новый владелец lock
    recovered = _row(miniapp, request_id)
    assert recovered["state"] == "retryable_failed" and recovered["error_code"] == "interrupted"
    assert recovered["attempt_id"] != old_attempt
    first._process(row, old_attempt)            # старая попытка всё-таки досчитала
    late = _row(miniapp, request_id)
    assert late["state"] == "retryable_failed" and late["result"] is None


def test_retry_after_interrupt_completes_once(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    _claim(worker)
    worker.recover()
    retry = miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                         {"request_id": str(request_id), "text": "загрязняющих веществ"}, token=session["token"])
    assert retry.status_code == 202
    worker.run_once()
    row = _row(miniapp, request_id)
    assert row["state"] == "completed" and row["attempt_no"] == 2
    count = miniapp.owner.execute("SELECT count(*) AS n FROM miniapp_request WHERE dialog_id = %s",
                                  (session["dialog_id"],)).fetchone()["n"]
    assert count == 1


def test_second_worker_does_not_take_lock(miniapp):
    holder = miniapp.app.state.miniapp.connect()
    holder.autocommit = True
    holder.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
    try:
        second = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
        with patch.object(MiniAppWorker, "recover", side_effect=AssertionError("второй worker не восстанавливает")):
            second._serve()       # lock занят: сразу выходит, ничего не обрабатывая
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        holder.close()


def test_running_over_budget_becomes_retryable_on_get(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    row, attempt = _claim(worker)
    miniapp.advance(181)
    status = miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests/{request_id}", session["token"]).json()
    assert status["state"] == "retryable_failed" and status["error_code"] == "attempt_timeout"
    worker._process(row, attempt)               # поздний результат после срока не сохраняется
    assert _row(miniapp, request_id)["result"] is None


def test_slow_stage_beyond_deadline_is_not_saved(miniapp):
    """Задержка ClubMembers/AI дольше бюджета: попытка завершается attempt_timeout, без empty/резервного ответа."""
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    original = miniapp.stand.ai.embed_query

    def slow(text):
        miniapp.advance(200)
        return original(text)
    miniapp.stand.ai.embed_query = slow
    try:
        worker.run_once()
    finally:
        miniapp.stand.ai.embed_query = original
    row = _row(miniapp, request_id)
    assert (row["state"], row["error_code"], row["outcome"]) == ("retryable_failed", "attempt_timeout", None)


def test_stage_timeout_bounded_by_remaining_budget(miniapp):
    started = worker_module.config.utcnow()
    deadline = worker_module.AttemptDeadline(started)
    assert deadline.stage() == 30.0
    miniapp.advance(170)
    assert 0 < deadline.stage() <= 10.0


def test_session_expiry_during_attempt_discards_result(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    row, attempt = _claim(worker)
    miniapp.owner.execute("UPDATE miniapp_session SET revoked_at = now()")
    worker._process(row, attempt)
    assert _row(miniapp, request_id)["result"] is None


def test_cleanup_deletes_running_attempt_and_late_commit_is_noop(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id = _ask(miniapp, session)
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    row, attempt = _claim(worker)
    miniapp.advance(8 * 3600 + 1)
    miniapp.app.state.miniapp.cleanup()
    worker._process(row, attempt)
    assert _row(miniapp, request_id) is None
    assert miniapp.owner.execute("SELECT count(*) AS n FROM miniapp_session").fetchone()["n"] == 0


def test_expired_session_closed_before_cleanup(miniapp):
    session = miniapp.login()
    miniapp.advance(8 * 3600 + 1)
    response = miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"])
    assert response.status_code == 401 and response.json()["error"]["code"] == "session_expired"


def test_maintenance_runs_independently_of_search_thread(miniapp):
    miniapp.login()
    miniapp.advance(8 * 3600 + 1)
    maintenance = Maintenance(miniapp.app.state.miniapp, interval=3600)
    maintenance.start()          # очистка при старте, в отдельном потоке
    maintenance.stop()
    assert miniapp.owner.execute("SELECT count(*) AS n FROM miniapp_session").fetchone()["n"] == 0
