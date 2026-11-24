"""T030: обработка, один контекст, отмена новым диалогом и отсутствие events."""
import uuid

from ecoprofi.bot.miniapp.worker import MiniAppWorker


def test_worker_found_and_context_once(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    request_id = uuid.uuid4()
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(request_id), "text": "а что и как"}, token=token).status_code == 202
    assert worker.run_once()
    first = miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/requests/{request_id}", token).json()
    assert first["state"] == "completed", first
    assert first["answer"]["outcome"] == "clarify", first
    assert first.get("error_code") != "source_invalidated", first
    assert miniapp.owner.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 0
    second_id = uuid.uuid4()
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(second_id), "text": "загрязняющих веществ"}, token=token).status_code == 202
    saved = miniapp.owner.execute("SELECT context_request_id FROM miniapp_request WHERE request_id = %s",
                                  (second_id,)).fetchone()["context_request_id"]
    assert saved == request_id
    assert worker.run_once()
    second = miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/requests/{second_id}", token).json()
    assert second["state"] == "completed" and second["answer"]["outcome"] in ("found", "empty", "clarify")
    assert miniapp.owner.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 0


def test_new_dialog_discards_late_result(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    request_id = uuid.uuid4()
    miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(request_id), "text": "загрязняющих веществ"}, token=token)
    new_dialog = uuid.uuid4()
    assert miniapp.post("/api/miniapp/v1/dialogs", {"dialog_id": str(new_dialog)}, token=token).status_code == 201
    assert not worker.run_once()
    history = miniapp.get(f"/api/miniapp/v1/dialogs/{new_dialog}/history", token).json()
    assert history["items"] == []
    assert miniapp.stand.ai.explain_calls == []
