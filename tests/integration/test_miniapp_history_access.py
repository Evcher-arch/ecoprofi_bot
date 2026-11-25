"""T045: отзыв подписки/источника закрывает историю и повторы; гонка уточнения A и вопроса B."""
import uuid

from psycopg.types.json import Jsonb

from ecoprofi.bot.club import ClubUnavailable
from ecoprofi.bot.miniapp.worker import MiniAppWorker


def _ask(miniapp, session, text, request_id=None):
    request_id = request_id or uuid.uuid4()
    response = miniapp.post(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests",
                            {"request_id": str(request_id), "text": text}, token=session["token"])
    return request_id, response


def _get(miniapp, session, request_id):
    return miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/requests/{request_id}", session["token"])


def _row(miniapp, request_id):
    return miniapp.owner.execute("SELECT * FROM miniapp_request WHERE request_id = %s", (request_id,)).fetchone()


def _clarify_with_source(miniapp, session, worker):
    """Уточнение A, чьи варианты опираются на опубликованный материал (provenance не пуст)."""
    a, _ = _ask(miniapp, session, "а что и как")
    worker.run_once()
    material = miniapp.owner.execute("SELECT id, published_version_id FROM material WHERE published_version_id IS NOT NULL "
                                     "ORDER BY id LIMIT 1").fetchone()
    miniapp.owner.execute("UPDATE miniapp_request SET provenance = %s WHERE request_id = %s",
                          (Jsonb([{"material_id": material["id"], "version_id": material["published_version_id"]}]), a))
    assert _row(miniapp, a)["outcome"] == "clarify"
    return a, material["id"]


def test_history_closes_after_membership_loss(miniapp):
    session = miniapp.login()
    _ask(miniapp, session, "секретный вопрос")
    miniapp.club.members.clear()
    response = miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"])
    assert response.status_code == 403
    assert "секретный" not in response.text
    assert _ask(miniapp, session, "секретный вопрос")[1].status_code == 403


def test_exempt_keeps_history_during_vk_outage(miniapp):
    session = miniapp.login()
    miniapp.club.exempt.add(miniapp.user_id)
    miniapp.club.failure = ClubUnavailable("test outage")
    assert miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"]).status_code == 200


def test_membership_error_hides_history(miniapp):
    session = miniapp.login()
    miniapp.club.failure = ClubUnavailable("test outage")
    response = miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"])
    assert response.status_code == 503 and response.json()["error"]["code"] == "membership_unavailable"


def test_source_removal_invalidates_full_answer(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id, _ = _ask(miniapp, session, "загрязняющих веществ")
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).run_once()
    assert _get(miniapp, session, request_id).json()["state"] == "completed"
    miniapp.owner.execute("UPDATE material SET state = 'deleted'")
    after = _get(miniapp, session, request_id).json()
    assert after["state"] == "invalidated" and "answer" not in after
    assert _row(miniapp, request_id)["result"] is None and _row(miniapp, request_id)["provenance"] == []


def test_new_version_invalidates_saved_answer(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    request_id, _ = _ask(miniapp, session, "загрязняющих веществ")
    MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai).run_once()
    used = _row(miniapp, request_id)["provenance"][0]
    columns = [r["column_name"] for r in miniapp.owner.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'material_version' "
        "AND column_name NOT IN ('id', 'content_hash', 'created_at') ORDER BY ordinal_position").fetchall()]
    names = ", ".join(columns)
    miniapp.owner.execute(f"INSERT INTO material_version (content_hash, {names}) "
                          f"SELECT 'new-hash', {names} FROM material_version WHERE id = %s", (used["version_id"],))
    miniapp.owner.execute("UPDATE material SET published_version_id = (SELECT max(id) FROM material_version) WHERE id = %s",
                          (used["material_id"],))
    assert _get(miniapp, session, request_id).json()["state"] == "invalidated"


def test_race_clarification_source_removed_before_worker(miniapp):
    """A уточнение → B queued (context_request_id=A) → снятие источника A → GET history
    инвалидирует A и очищает pending → worker: B invalidated без поиска/AI; повтор B ничего не запускает."""
    miniapp.stand.load()
    session = miniapp.login()
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    a, material_id = _clarify_with_source(miniapp, session, worker)
    b, response = _ask(miniapp, session, "загрязняющих веществ")
    assert response.status_code == 202 and _row(miniapp, b)["context_request_id"] == a
    miniapp.owner.execute("UPDATE material SET state = 'deleted' WHERE id = %s", (material_id,))
    history = miniapp.get(f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"]).json()
    assert [i["state"] for i in history["items"]][0] == "invalidated"
    dialog = miniapp.owner.execute("SELECT pending_request_id FROM miniapp_dialog WHERE id = %s",
                                   (session["dialog_id"],)).fetchone()
    assert dialog["pending_request_id"] is None
    calls = len(miniapp.stand.ai.explain_calls)
    embed = miniapp.stand.ai.embed_query
    miniapp.stand.ai.embed_query = lambda text: (_ for _ in ()).throw(AssertionError("поиск не должен вызываться"))
    try:
        assert worker.run_once()
    finally:
        miniapp.stand.ai.embed_query = embed
    row = _row(miniapp, b)
    assert row["state"] == "invalidated" and row["context_request_id"] == a and row["effective_query"] is None
    assert len(miniapp.stand.ai.explain_calls) == calls
    attempts = row["attempt_no"]
    repeat = _ask(miniapp, session, "загрязняющих веществ", request_id=b)[1]
    assert repeat.status_code == 200 and repeat.json()["state"] == "invalidated"
    assert _row(miniapp, b)["attempt_no"] == attempts and not worker.run_once()


def test_race_control_valid_clarification_used_once(miniapp):
    miniapp.stand.load()
    session = miniapp.login()
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    a, _ = _clarify_with_source(miniapp, session, worker)
    b, _ = _ask(miniapp, session, "загрязняющих веществ")
    worker.run_once()
    row = _row(miniapp, b)
    # Новое сообщение нашлось само по себе: контекст уточнения к нему не приклеивается.
    assert row["state"] == "completed" and row["effective_query"] == "загрязняющих веществ"
    # Повтор готового ответа не продвигает контекст второй раз.
    _ask(miniapp, session, "загрязняющих веществ", request_id=b)
    assert _row(miniapp, b)["effective_query"] == "загрязняющих веществ"


def test_concurrent_registration_keeps_dependency(miniapp):
    """Регистрация B и инвалидация A сериализуются блокировкой диалога: зависимость B не теряется."""
    from concurrent.futures import ThreadPoolExecutor
    miniapp.stand.load()
    session = miniapp.login()
    worker = MiniAppWorker(miniapp.app.state.miniapp, miniapp.stand.ai)
    a, material_id = _clarify_with_source(miniapp, session, worker)
    miniapp.owner.execute("UPDATE material SET state = 'deleted' WHERE id = %s", (material_id,))
    b = uuid.uuid4()
    with ThreadPoolExecutor(max_workers=2) as pool:
        submit = pool.submit(_ask, miniapp, session, "второй вопрос", b)
        read = pool.submit(miniapp.get, f"/api/miniapp/v1/dialogs/{session['dialog_id']}/history", session["token"])
        submit.result(), read.result()
    row = _row(miniapp, b)
    assert row is not None
    if row["context_request_id"] == a:
        worker.run_once()
        assert _row(miniapp, b)["state"] == "invalidated"
    else:
        assert row["context_request_id"] is None     # B зарегистрирован уже после очистки pending
