"""T029: владение, идемпотентность, длины, пагинация и изоляция диалогов."""
import uuid


def test_dialog_idempotency_and_closed(miniapp):
    session = miniapp.login()
    token = session["token"]
    new_id = uuid.uuid4()
    created = miniapp.post("/api/miniapp/v1/dialogs", {"dialog_id": str(new_id)}, token=token)
    assert created.status_code == 201
    assert miniapp.post("/api/miniapp/v1/dialogs", {"dialog_id": str(new_id)}, token=token).status_code == 200
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests", {
        "request_id": str(uuid.uuid4()), "text": "вопрос"}, token=token).status_code == 409
    assert miniapp.post("/api/miniapp/v1/dialogs", {"dialog_id": str(session["dialog_id"])}, token=token).status_code == 409


def test_request_lengths_and_history(miniapp):
    session = miniapp.login()
    token, dialog = session["token"], session["dialog_id"]
    for text in ("", "   ", "😀" * 2001):
        response = miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
            "request_id": str(uuid.uuid4()), "text": text}, token=token)
        assert response.status_code == 422
        assert miniapp.stand.ai.explain_calls == []
    request_id = uuid.uuid4()
    created = miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(request_id), "text": "😀"}, token=token)
    assert created.status_code == 202, created.text
    assert created.json()["state"] == "queued"
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(request_id), "text": "другой"}, token=token).status_code == 409
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{dialog}/requests", {
        "request_id": str(request_id), "text": "😀"}, token=token).status_code == 202
    history = miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/history?limit=1", token)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1 and history.json()["has_more"] is False
    assert miniapp.get(f"/api/miniapp/v1/dialogs/{dialog}/history?limit=51", token).status_code == 422


def test_other_users_cannot_read_or_submit(miniapp):
    first = miniapp.login(user_id=12345)
    second = miniapp.login(user_id=54321)
    response = miniapp.get(f"/api/miniapp/v1/dialogs/{first['dialog_id']}/history", second["token"])
    assert response.status_code == 404
    assert miniapp.post(f"/api/miniapp/v1/dialogs/{first['dialog_id']}/requests", {
        "request_id": str(uuid.uuid4()), "text": "чужой"}, token=second["token"]).status_code == 404
