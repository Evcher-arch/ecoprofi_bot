"""T007: общий поиск не создаёт events и проверяет допуск/версии до выдачи."""
import pytest

from ecoprofi.ai import AIError
from ecoprofi.bot.access import resolve_member_access
from ecoprofi.bot.service import search_answer


def test_service_preserves_found_and_no_transport_events(loaded):
    user = loaded.users["club"]
    with loaded.client.app.state.connect() as conn:
        result = search_answer(conn, loaded.ai, "загрязняющих веществ",
                               lambda: resolve_member_access(user, loaded.club))
        assert result.outcome == "found"
        assert 1 <= len(result.cards) <= 3
        assert result.provenance
        assert result.text and result.metrics["duration_ms"] >= 0
        assert conn.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM dialog_context").fetchone()["n"] == 0


def test_service_preserves_clarify_and_empty(loaded):
    user = loaded.users["club"]
    with loaded.client.app.state.connect() as conn:
        clarify = search_answer(conn, loaded.ai, "а что и как",
                                lambda: resolve_member_access(user, loaded.club))
        assert clarify.outcome == "clarify" and clarify.cards == []
        empty = search_answer(conn, loaded.ai, "рецепт борща со сметаной",
                              lambda: resolve_member_access(user, loaded.club))
        assert empty.outcome == "empty" and empty.cards == []
        assert loaded.ai.explain_calls == []


def test_service_denied_before_ai(loaded):
    user = loaded.outsider
    with loaded.client.app.state.connect() as conn:
        with pytest.raises(Exception) as refused:
            search_answer(conn, loaded.ai, "загрязняющих веществ",
                          lambda: resolve_member_access(user, loaded.club))
        assert type(refused.value).__name__ == "NotSubscribed"
        assert loaded.ai.explain_calls == []


def test_service_keeps_fallback_explanation(loaded):
    loaded.ai.explain = lambda *_: (_ for _ in ()).throw(AIError("test outage"))
    user = loaded.users["club"]
    with loaded.client.app.state.connect() as conn:
        result = search_answer(conn, loaded.ai, "загрязняющих веществ",
                               lambda: resolve_member_access(user, loaded.club))
        assert result.outcome == "found"
        assert all(c["explanation"] for c in result.cards)
