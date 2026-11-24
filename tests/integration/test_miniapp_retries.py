"""T043: повторы, конфликт текста, busy, ограничения и terminal-состояния."""
import uuid

from ecoprofi.bot.miniapp.worker import MiniAppWorker


def test_completed_repeat_does_not_call_ai(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    request_id = uuid.uuid4()
    body = {"request_id": str(request_id), "text": "а что и как"}
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", body, token=token).status_code == 202
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).run_once()
    before = len(miniapp.stand.ai.explain_calls)
    repeat = miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", body, token=token)
    assert repeat.status_code == 200 and repeat.json()["state"] == "completed"
    assert len(miniapp.stand.ai.explain_calls) == before


def test_failed_explicit_retry(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    request_id = uuid.uuid4()
    miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(request_id), "text": "а что и как"}, token=token)
    miniapp.advance(181)
    status = miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/requests/{request_id}", token)
    assert status.json()["state"] == "retryable_failed" and status.json()["retry_allowed"]
    retry = miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(request_id), "text": "а что и как"}, token=token)
    assert retry.status_code == 202
    row = miniapp.owner.execute("SELECT effective_query, context_request_id FROM miniapp_request WHERE request_id = %s", (request_id,)).fetchone()
    assert row is not None


def test_superseded_retry(miniapp):
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    first, second = uuid.uuid4(), uuid.uuid4()
    miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(first), "text": "первый"}, token=token)
    miniapp.advance(181)
    assert miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/requests/{first}", token).json()["state"] == "retryable_failed"
    miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(second), "text": "второй"}, token=token)
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(first), "text": "первый"}, token=token).status_code == 409


def test_terminal_never_requeued(miniapp):
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    request_id = uuid.uuid4()
    miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(request_id), "text": "вопрос"}, token=token)
    miniapp.post("/api/miniapp/v1/dialogs", {"dialog_id": str(uuid.uuid4())}, token=token)
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {"request_id": str(request_id), "text": "вопрос"}, token=token).status_code == 409
