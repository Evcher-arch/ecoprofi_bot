"""История 3: уточнение, обоснованный отказ, карточка без дорожки."""
import pytest

from ecoprofi.ai import AIError


def pending(stand, role):
    row = stand.owner.execute("SELECT pending_query FROM dialog_context WHERE user_id = %s",
                              (stand.users[role],)).fetchone()
    return row and row["pending_query"]


@pytest.fixture
def subscribers(loaded):
    """Все три тестовые учётные записи подписаны на клуб: отдельные диалоги для проверки изоляции."""
    for role in ("public", "unknown"):
        loaded.subscribe(role)
    return loaded


def test_expected_outcomes_from_fixture_table(loaded):
    """Сильный, неоднозначный, посторонний запросы и запись без дорожки — по expectations.json."""
    for query in loaded.expectations["queries"]:
        for role, expected in query["by_role"].items():
            label = f"{query['text']} / {role}"
            answer = loaded.ask(role, query["text"])
            if "outcome" in expected:
                assert answer["outcome"] == expected["outcome"], label
            if "outcome_in" in expected:
                assert answer["outcome"] in expected["outcome_in"], label
            keys = loaded.keys(answer)
            assert set(expected.get("materials_include", [])) <= set(keys), label
            assert not set(expected.get("materials_exclude", [])) & set(keys), label
            if "cards" in expected:
                assert len(answer["cards"]) == expected["cards"], label
            if "card_types" in expected:
                assert [c["type"] for c in answer["cards"]] == expected["card_types"], label
            assert len(answer["cards"]) <= 3, label
            # следующий запрос не должен наследовать контекст уточнения
            loaded.owner.execute("DELETE FROM dialog_context")


def test_off_corpus_question_returns_no_quotes_or_timecodes(loaded):
    answer = loaded.ask("club", "рецепт борща со сметаной")
    assert answer["outcome"] == "empty" and answer["cards"] == []
    assert "vkvideo" not in answer["text"] and "«" not in answer["text"]
    assert loaded.ai.explain_calls == []


def test_card_without_track_is_navigation_only(loaded):
    answer = loaded.ask("club", "паспортизация отходов")
    (card,) = answer["cards"]
    assert set(card) == {"type", "material_id", "title", "lecture", "explanation", "url"}
    assert card["type"] == "navigation"
    assert card["url"] == "https://vkvideo.ru/video-100001_444"
    assert "недоступен" in card["explanation"]
    assert "Эпизод" not in answer["text"] and "?t=" not in answer["text"]


def test_navigation_card_counts_towards_limit_of_three(loaded):
    loaded.owner.execute(
        "UPDATE material SET description = description || ' Перечень загрязняющих веществ.' WHERE item_id = 444")
    answer = loaded.ask("club", "загрязняющих веществ")
    assert len(answer["cards"]) == 3


def test_clarification_continues_own_dialog_and_finds_episode(subscribers):
    loaded = subscribers
    first = loaded.ask("public", "а что и как")
    assert first["outcome"] == "clarify" and first["cards"] == []
    assert pending(loaded, "public") == "а что и как"
    second = loaded.ask("public", "для атмосферного воздуха")
    assert second["outcome"] == "found"
    assert "same_title_public" in loaded.keys(second)
    assert pending(loaded, "public") is None


def test_new_question_after_clarification_is_searched_on_its_own(subscribers):
    """Самостоятельный новый вопрос после уточнения не склеивается с прежним."""
    loaded = subscribers
    loaded.ask("public", "а что и как", event_id="n1")
    loaded.ask("public", "атмосферного воздуха", event_id="n2")
    effective = loaded.owner.execute("SELECT effective_query FROM event WHERE event_id = 'n2'").fetchone()
    assert effective["effective_query"] == "атмосферного воздуха"


def test_failed_clarifications_do_not_accumulate(subscribers):
    """Два неудачных уточнения подряд: контекстом остаётся только последнее сообщение."""
    loaded = subscribers
    loaded.ask("public", "а что и как")
    assert loaded.ask("public", "и как же так")["outcome"] == "clarify"
    assert pending(loaded, "public") == "и как же так"


def test_two_simultaneous_clarifications_stay_isolated(subscribers):
    loaded = subscribers
    loaded.ask("public", "а что и как")
    loaded.ask("unknown", "и как же так")
    assert pending(loaded, "public") == "а что и как"
    assert pending(loaded, "unknown") == "и как же так"
    answer = loaded.ask("unknown", "допустимую концентрацию")
    assert loaded.keys(answer) == ["lecture_public"]
    assert pending(loaded, "public") == "а что и как"       # чужой контекст не тронут
    answer = loaded.ask("public", "атмосферного воздуха")
    assert "same_title_public" in loaded.keys(answer)


def test_repeated_event_does_not_advance_clarification_twice(subscribers):
    loaded = subscribers
    loaded.ask("public", "а что и как", event_id="c1")
    loaded.ask("public", "а что и как", event_id="c1")
    assert pending(loaded, "public") == "а что и как"
    loaded.ask("public", "атмосферного воздуха", event_id="c2")
    repeat = loaded.ask("public", "атмосферного воздуха", event_id="c2")
    assert repeat["outcome"] == "found"
    effective = loaded.owner.execute("SELECT effective_query FROM event WHERE event_id = 'c2'").fetchone()
    assert effective["effective_query"] == "атмосферного воздуха"


def test_role_change_between_clarification_turns(loaded):
    """Подписчик начал уточнение, отменил подписку и не получает закрытый эпизод."""
    first = loaded.ask("club", "нормативы")
    assert first["outcome"] in ("found", "clarify")
    loaded.owner.execute("DELETE FROM dialog_context")
    loaded.owner.execute(
        "INSERT INTO dialog_context (group_id, peer_id, user_id, pending_query) VALUES (%s, %s, %s, 'взвешенные')",
        (loaded.group_id, loaded.users["club"], loaded.users["club"]))
    loaded.subscribe("club", False)
    response = loaded.post(loaded.users["club"], "вещества")
    loaded.refused(response)
    assert "взвешенные вещества" not in response.text


def test_question_of_stop_words_asks_to_clarify(loaded):
    assert loaded.ask("club", "а что и как")["outcome"] == "clarify"


def test_model_outage_keeps_answer_with_default_explanation(loaded):
    def broken(question, candidates):
        raise AIError("outage")

    loaded.ai.explain = broken
    answer = loaded.ask("club", "загрязняющих веществ")
    assert answer["outcome"] == "found"
    assert all(c["explanation"] for c in answer["cards"])


def test_embedding_outage_is_temporary_error(loaded):
    def broken(text):
        raise AIError("outage")

    loaded.ai.embed_query = broken
    assert loaded.post(loaded.users["club"], "загрязняющих веществ").status_code == 503
