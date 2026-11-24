"""T056: секреты и входные данные не отражаются в ответах, telemetry и логах; служебные API отделены."""
import logging
import time
import uuid

from ecoprofi.bot.miniapp.worker import MiniAppWorker
from tests.fixtures.miniapp import SECRET


def test_validation_error_does_not_echo_input(miniapp):
    query = miniapp.query()
    response = miniapp.post("/api/miniapp/v1/session", {"launch_query": query, "unexpected": "МАРКЕР-ТЕКСТА"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_input"
    for value in (query, "МАРКЕР-ТЕКСТА", "sign=", SECRET):
        assert value not in response.text
    long_text = "😀" * 2001
    session = miniapp.login()
    response = miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                            {"request_id": str(uuid.uuid4()), "text": long_text}, token=session["token"])
    assert response.status_code == 422 and long_text not in response.text


def test_invalid_launch_and_tokens_not_logged(miniapp, caplog):
    caplog.set_level(logging.DEBUG)
    bad = miniapp.query()[:-3] + "AAA"
    assert miniapp.post("/api/miniapp/v1/session", {"launch_query": bad}).status_code == 401
    session = miniapp.login()
    miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"])
    for value in (bad, session["token"], SECRET):
        assert value not in caplog.text


def test_telemetry_has_no_question_text(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    text = "загрязняющих веществ уникальный-маркер"
    miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                 {"request_id": str(uuid.uuid4()), "text": text}, token=session["token"])
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).run_once()
    rows = miniapp.owner.execute("SELECT details::text AS d FROM operation_log WHERE details->>'channel' = 'miniapp'").fetchall()
    assert rows and all("уникальный-маркер" not in r["d"] and session["token"] not in r["d"] for r in rows)


def test_operation_id_traces_attempts_reads_and_errors(miniapp):
    """T052: по operation_id видны попытка (исход, длительность, версии) и чтение; ошибка до регистрации — свой ID."""
    miniapp.stand.load()
    session = miniapp.login()
    request_id = uuid.uuid4()
    created = miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                           {"request_id": str(request_id), "text": "загрязняющих веществ"}, token=session["token"]).json()
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).run_once()
    miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests/{request_id}", session["token"])
    rows = miniapp.owner.execute("SELECT component, outcome, details FROM operation_log WHERE operation_id = %s ORDER BY created_at",
                                 (created["operation_id"],)).fetchall()
    events = [(r["details"]["event"], r["outcome"]) for r in rows]
    assert ("attempt", "found") in events and ("read", "completed") in events
    attempt = next(r for r in rows if r["details"]["event"] == "attempt")
    assert attempt["component"] == "bot" and attempt["details"]["channel"] == "miniapp"
    assert attempt["details"]["attempt_no"] == 1 and attempt["details"]["versions"] and "duration_ms" in attempt["details"]
    bad = miniapp.post("/api/miniapp/v1/session", {"launch_query": "vk_app_id=1"})
    error = miniapp.owner.execute("SELECT details FROM operation_log WHERE operation_id = %s",
                                  (bad.json()["operation_id"],)).fetchone()
    assert error["details"] == {"channel": "miniapp", "event": "error", "error_code": "401:invalid_launch"}


def test_service_routes_still_need_service_key(miniapp):
    session = miniapp.login()
    bearer = {"Authorization": f"Bearer {session['token']}"}
    assert miniapp.client.get("/api/alerts/pending", headers=bearer).status_code == 401
    assert miniapp.client.post("/api/search", json={}, headers=bearer).status_code in (401, 422)
    assert miniapp.client.get("/openapi.json").status_code == 404
    assert miniapp.client.get("/docs").status_code == 404


def test_disabled_without_full_settings(stand, monkeypatch):
    from fastapi.testclient import TestClient
    from ecoprofi.bot.api import create_app
    from ecoprofi.db import connect
    monkeypatch.setenv("MINIAPP_ENABLED", "true")
    monkeypatch.delenv("MINIAPP_APP_SECRET", raising=False)
    app = create_app(connect_fn=lambda: connect(stand.bot_dsn), ai=stand.ai, club=stand.club, miniapp=True)
    with TestClient(app) as client:
        assert client.post("/api/miniapp/v1/session", json={"launch_query": "x"}).status_code == 404


def test_lifecycle_worker_processes_request(stand, monkeypatch):
    """T040: при полной настройке worker стартует вместе с приложением и останавливается с ним."""
    from fastapi.testclient import TestClient
    from ecoprofi.bot.api import create_app
    from ecoprofi.db import connect
    from tests.fixtures.miniapp import APP_ID, ORIGIN, launch_query
    stand.load()
    for key, value in {"MINIAPP_ENABLED": "true", "MINIAPP_APP_ID": str(APP_ID),
                       "MINIAPP_APP_SECRET": SECRET, "MINIAPP_PUBLIC_ORIGIN": ORIGIN}.items():
        monkeypatch.setenv(key, value)
    stand.club.members.add(777)
    app = create_app(connect_fn=lambda: connect(stand.bot_dsn), ai=stand.ai, club=stand.club, miniapp=True)
    with TestClient(app) as client:
        login = client.post("/api/miniapp/v1/session", json={"launch_query": launch_query(user_id=777, ts=int(time.time()))},
                            headers={"Origin": ORIGIN})
        assert login.status_code == 201, login.text
        token, dialog = login.json()["token"], login.json()["dialog_id"]
        request_id = str(uuid.uuid4())
        headers = {"Origin": ORIGIN, "Authorization": f"Bearer {token}"}
        assert client.post(f"/api/miniapp/v1/dialogs/{dialog}/requests",
                           json={"request_id": request_id, "text": "загрязняющих веществ"}, headers=headers).status_code == 202
        state = None
        for _ in range(50):
            state = client.get(f"/api/miniapp/v1/dialogs/{dialog}/requests/{request_id}", headers=headers).json()["state"]
            if state not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert state == "completed"
    assert all(not getattr(s, "_thread", None) for s in app.state.miniapp_services)
