"""Ответ на уточнение: новое сообщение ищется само, контекст — запасной вариант без накопления."""
import pytest

from ecoprofi.bot import service
from ecoprofi.bot.service import SearchOutcome, search_in_context


@pytest.fixture
def fake(monkeypatch):
    """search_answer по таблице «запрос → исход»; записывает порядок вызовов."""
    table, calls = {}, []

    def search_answer(conn, ai, question, check_access, *, deadline=None):
        calls.append(question)
        return SearchOutcome(table.get(question, "clarify"), question, [], {})

    monkeypatch.setattr(service, "search_answer", search_answer)
    return table, calls


def test_without_context_searches_text_once(fake):
    table, calls = fake
    found, query = search_in_context(None, None, "выбросы", None, lambda: None)
    assert query == "выбросы" and calls == ["выбросы"]


def test_standalone_hit_ignores_context(fake):
    table, calls = fake
    table["инвентаризация выбросов"] = "found"
    found, query = search_in_context(None, None, "инвентаризация выбросов", "водные биоресурсы", lambda: None)
    assert found.outcome == "found" and query == "инвентаризация выбросов"
    assert calls == ["инвентаризация выбросов"]


def test_context_used_when_text_alone_fails(fake):
    table, calls = fake
    table["перечень для атмосферного воздуха"] = "found"
    found, query = search_in_context(None, None, "для атмосферного воздуха", "перечень", lambda: None)
    assert found.outcome == "found" and query == "перечень для атмосферного воздуха"
    assert calls == ["для атмосферного воздуха", "перечень для атмосферного воздуха"]


def test_no_hit_keeps_only_new_text_as_next_context(fake):
    table, calls = fake
    found, query = search_in_context(None, None, "рыба", "водные биоресурсы", lambda: None)
    assert found.outcome == "clarify" and query == "рыба"
