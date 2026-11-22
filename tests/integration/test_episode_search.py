"""История 1: от загрузки фикстур до POST /api/search."""
CONTROL = "загрязняющих веществ"


def test_control_query_returns_club_review_with_exact_episode(loaded):
    answer = loaded.ask("club", CONTROL)
    assert answer["outcome"] == "found"
    assert 1 <= len(answer["cards"]) <= 3
    keys = loaded.keys(answer)
    assert "razbor_club" in keys
    card = answer["cards"][keys.index("razbor_club")]
    control = loaded.expectations["subtitles"]["razbor_club.vtt"]["control_episode"]
    assert set(card) == {"type", "material_id", "title", "lecture", "explanation", "excerpt", "start_ms", "end_ms", "url"}
    assert card["type"] == "episode"
    assert (card["start_ms"], card["end_ms"]) == (control["start_ms"], control["end_ms"])
    assert card["excerpt"] == control["text"]
    assert card["url"] == "https://vkvideo.ru/video-100001_111?t=1563s"
    assert card["explanation"]


def test_cards_are_distinct_materials_even_with_same_title(loaded):
    answer = loaded.ask("club", CONTROL)
    keys = loaded.keys(answer)
    assert len(keys) == len(set(keys)) == 3
    assert {"razbor_club", "same_title_public"} <= set(keys)
    titles = [c["title"] for c in answer["cards"]]
    assert titles.count("Разбор перечня загрязняющих веществ 2909-р") == 2


def test_quote_interval_and_link_belong_to_one_version(loaded):
    answer = loaded.ask("club", CONTROL)
    for card in answer["cards"]:
        row = loaded.owner.execute(
            "SELECT e.text, m.url, m.title FROM fragment e "
            "JOIN material m ON m.published_version_id = e.version_id "
            "WHERE m.id = %s AND e.start_ms = %s AND e.end_ms = %s",
            (card["material_id"], card["start_ms"], card["end_ms"]),
        ).fetchone()
        assert row is not None, "интервал не принадлежит опубликованной версии этой записи"
        assert card["excerpt"] == row["text"] and card["title"] == row["title"]
        assert card["url"] == f"{row['url']}?t={card['start_ms'] // 1000}s"


def test_link_start_rounds_down_and_displayed_end_rounds_up(loaded):
    answer = loaded.ask("club", "допустимую концентрацию")
    card = answer["cards"][loaded.keys(answer).index("lecture_public")]
    assert (card["start_ms"], card["end_ms"]) == (720_000, 739_000)
    loaded.owner.execute("UPDATE fragment SET start_ms = 720900, end_ms = 738100 WHERE start_ms = 720000")
    answer = loaded.ask("club", "допустимую концентрацию")
    card = answer["cards"][loaded.keys(answer).index("lecture_public")]
    assert card["url"].endswith("?t=720s")
    assert "Эпизод 12:00–12:19" in answer["text"]


def test_message_text_carries_title_excerpt_and_link(loaded):
    answer = loaded.ask("club", CONTROL)
    assert "Эпизод 26:03–26:30" in answer["text"]
    assert "взвешенные вещества" in answer["text"]
    assert "https://vkvideo.ru/video-100001_111?t=1563s" in answer["text"]
    assert "fixture.invalid" not in answer["text"]      # временный адрес дорожки наружу не выдаётся


def test_response_echoes_event_and_peer(loaded):
    answer = loaded.ask("club", CONTROL, event_id="evt-42")
    assert (answer["event_id"], answer["peer_id"]) == ("evt-42", loaded.users["club"])
    assert isinstance(answer["random_id"], int) and answer["random_id"] > 0


def test_model_explanation_for_foreign_candidate_is_ignored(loaded):
    loaded.ai.foreign_id = 999_999
    answer = loaded.ask("club", CONTROL)
    assert all(c["material_id"] != 999_999 for c in answer["cards"])
    assert all(c["explanation"].startswith("Пояснение к") for c in answer["cards"])


def test_access_is_checked_before_model_is_needed(loaded, monkeypatch):
    """Без настроек AI Studio пользователь без подписки получает ответ про клуб, подписчик — временную ошибку 503."""
    from fastapi.testclient import TestClient

    from ecoprofi.bot.api import create_app
    from ecoprofi.db import connect
    for name in ("YANDEX_FOLDER_ID", "YANDEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(create_app(connect_fn=lambda: connect(loaded.bot_dsn), service_key="k", club=loaded.club))

    def post(user):
        return client.post("/api/search", headers={"X-Service-Key": "k"}, json={
            "event_id": "p", "group_id": loaded.group_id, "peer_id": user, "user_id": user, "text": CONTROL})

    loaded.refused(post(loaded.outsider))
    assert post(loaded.users["club"]).status_code == 503


def test_status_codes(loaded):
    club = loaded.users["club"]
    assert loaded.post(club, CONTROL, key="wrong").status_code == 401
    assert loaded.post(club, CONTROL, key=None).status_code == 401
    assert loaded.post(club, "   ", key="wrong").status_code == 401     # ключ проверяется раньше тела
    loaded.refused(loaded.post(loaded.outsider, CONTROL))
    assert loaded.post(club, CONTROL, group_id=loaded.group_id + 1).status_code == 403
    assert loaded.post(club, CONTROL, peer_id=2000000001).status_code == 403
    assert loaded.post(club, "   ").status_code == 422
    assert loaded.client.post("/api/search", json={"text": "x"},
                              headers={"X-Service-Key": "test-service-key"}).status_code == 422
