"""T018: подтверждённые сессии, resume, отказ и завершение."""
from datetime import datetime, timezone

from ecoprofi.bot.club import ClubUnavailable


def test_login_resume_logout(miniapp):
    first = miniapp.login()
    assert first["token"] and first["dialog_id"] and first["user_id"] == 12345
    assert first["expires_at"] == datetime.fromtimestamp(miniapp.now + 8 * 3600, timezone.utc).isoformat()
    resumed = miniapp.post("/api/miniapp/v1/session/resume", {"launch_query": miniapp.query()}, token=first["token"])
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["dialog_id"] == first["dialog_id"]
    assert "token" not in resumed.json()
    deleted = miniapp.client.delete("/api/miniapp/v1/session", headers={
        "Authorization": f"Bearer {first['token']}", "Origin": "https://miniapp.test"})
    assert deleted.status_code == 204
    assert miniapp.get(f"/api/miniapp/v1/dialogs/{first['dialog_id']}/history", first["token"]).status_code == 401


def test_login_denied_without_ai(miniapp):
    miniapp.club.members.clear()
    response = miniapp.post("/api/miniapp/v1/session", {"launch_query": miniapp.query()})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_subscribed"
    assert miniapp.stand.ai.explain_calls == []
    miniapp.club.exempt.add(12345)
    assert miniapp.login()["user_id"] == 12345


def test_membership_failure(miniapp):
    miniapp.club.failure = ClubUnavailable("test outage")
    response = miniapp.post("/api/miniapp/v1/session", {"launch_query": miniapp.query()})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "membership_unavailable"
    assert "outage" not in response.text


def test_expiry_and_resume_mismatch(miniapp):
    first = miniapp.login()
    mismatch = miniapp.post("/api/miniapp/v1/session/resume", {"launch_query": miniapp.query(extra=(("vk_custom", "different"),))}, token=first["token"])
    assert mismatch.status_code == 401 and mismatch.json()["error"]["code"] == "resume_mismatch"
    miniapp.advance(8 * 3600 + 1)
    response = miniapp.get(f"/api/miniapp/v1/dialogs/{first['dialog_id']}/history", first["token"])
    assert response.status_code == 401 and response.json()["error"]["code"] == "session_expired"


def test_old_launch_cannot_create_new_session(miniapp):
    first = miniapp.login()
    miniapp.advance(3600)
    assert miniapp.post("/api/miniapp/v1/session", {"launch_query": miniapp.query(ts=miniapp.now - 3600)}).status_code == 401
    assert miniapp.post("/api/miniapp/v1/session/resume", {"launch_query": miniapp.query(ts=miniapp.now - 3600)}, token=first["token"]).status_code == 200


def test_origin_and_unknown_fields(miniapp):
    assert miniapp.post("/api/miniapp/v1/session", {"launch_query": miniapp.query()}, origin="https://evil.test").status_code == 403
    assert miniapp.post("/api/miniapp/v1/session", {"launch_query": miniapp.query(), "user_id": 999}).status_code == 422
