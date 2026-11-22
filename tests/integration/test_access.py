"""История 2: ответ в пределах текущих прав. Проверяется и ответ, и то, что получила модель.

Допуск — подписка на клуб Экопрофи: подписчик видит открытые и клубные материалы,
пользователь без подписки получает ответ про клуб — до поиска и обращения к модели.
"""
import json

import pytest

from ecoprofi.bot.club import ClubUnavailable
from ecoprofi.bot.import_policy import apply_policy, parse_policy

CONTROL = "загрязняющих веществ"
CLOSED_ONLY = "взвешенные вещества"
CLOSED_MARKERS = ("взвешенные вещества", "расчёта платы", "video-100001_111")


def assert_nothing_closed(stand, answer):
    assert "razbor_club" not in stand.keys(answer)
    dump = json.dumps(answer, ensure_ascii=False)
    for marker in CLOSED_MARKERS:
        assert marker not in dump
    for call in stand.ai.explain_calls:
        sent = json.dumps(call["candidates"], ensure_ascii=False)
        for marker in CLOSED_MARKERS:
            assert marker not in sent, "закрытая выдержка передана модели"


def test_club_gets_closed_episode(loaded):
    assert "razbor_club" in loaded.keys(loaded.ask("club", CONTROL))


def test_subscriber_gets_public_and_club_materials(loaded):
    assert {"razbor_club", "lecture_public"} <= set(loaded.keys(loaded.ask("club", CONTROL)))


@pytest.mark.parametrize("user", ["public", "unknown"])
def test_user_without_subscription_is_refused_before_search(loaded, user):
    answer = loaded.refused(loaded.post(loaded.users[user], CONTROL))
    assert answer["peer_id"] == loaded.users[user] and answer["random_id"] > 0
    assert loaded.ai.explain_calls == []
    assert all(marker not in answer["text"] for marker in CLOSED_MARKERS)
    assert loaded.owner.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 0   # вопрос не сохранён


def test_refusal_repeated_for_same_event_keeps_random_id(loaded):
    first = loaded.refused(loaded.post(loaded.users["public"], CONTROL, event_id="r1"))
    again = loaded.refused(loaded.post(loaded.users["public"], CONTROL, event_id="r1"))
    other = loaded.refused(loaded.post(loaded.users["public"], CONTROL, event_id="r2"))
    assert first["random_id"] == again["random_id"] != other["random_id"]


def test_refusal_carries_club_link_when_configured(loaded, monkeypatch):
    monkeypatch.setenv("CLUB_URL", "https://vk.com/club1")
    assert loaded.refused(loaded.post(loaded.users["public"], CONTROL))["text"].endswith("\nhttps://vk.com/club1")


def test_cancelled_subscription_applies_to_repeated_query_without_restart(loaded):
    assert "razbor_club" in loaded.keys(loaded.ask("club", CONTROL))
    loaded.subscribe("club", False)
    loaded.ai.explain_calls.clear()
    loaded.refused(loaded.post(loaded.users["club"], CONTROL))
    assert loaded.ai.explain_calls == []


def test_new_subscriber_is_admitted_without_restart(loaded):
    loaded.refused(loaded.post(loaded.users["public"], CONTROL))
    loaded.subscribe("public")
    assert "razbor_club" in loaded.keys(loaded.ask("public", CONTROL))


def test_repeated_event_is_recomputed_with_current_rights(loaded):
    first = loaded.ask("club", CONTROL, event_id="same")
    assert "razbor_club" in loaded.keys(first)
    loaded.subscribe("club", False)
    repeat = loaded.refused(loaded.post(loaded.users["club"], CONTROL, event_id="same"))
    assert repeat["random_id"] == first["random_id"]                 # второго сообщения VK не доставит


def test_tester_list_in_policy_grants_nothing(loaded):
    """Прежний раздел testers пропускается: допуск даёт только подписка."""
    policy = dict(loaded.policy(), testers={str(loaded.users["public"]): "club"})
    apply_policy(loaded.owner, parse_policy(policy))
    loaded.refused(loaded.post(loaded.users["public"], CONTROL))


def test_failed_subscription_check_gives_no_answer(loaded):
    """VK не ответил: подписка не подтверждена — 503, адаптер повторит событие."""
    loaded.club.failure = ClubUnavailable("VK недоступен")
    response = loaded.post(loaded.users["club"], CONTROL)
    assert response.status_code == 503
    assert "razbor_club" not in response.text and loaded.ai.explain_calls == []


def test_subscription_cancelled_during_processing_does_not_leak(loaded):
    """Подписка отменена, пока модель готовит пояснение: закрытое не возвращается."""
    fired = []

    def revoke():
        if not fired:
            fired.append(True)
            loaded.subscribe("club", False)

    loaded.ai.before_explain = revoke
    response = loaded.post(loaded.users["club"], CONTROL)
    assert fired
    loaded.refused(response)
    assert all(marker not in response.text for marker in CLOSED_MARKERS)


def test_material_with_unknown_or_missing_audience_is_hidden_from_everyone(loaded):
    materials = loaded.policy()["materials"]
    materials["-100001_111"] = "unknown"
    materials.pop("-100001_333")
    loaded.import_policy(materials=materials)
    assert loaded.keys(loaded.ask("club", CONTROL)) == ["lecture_public"]


def test_operator_rights_do_not_grant_participant_access(loaded):
    """Индексатор прочитал закрытую запись, но пользователь без подписки её не получает."""
    assert loaded.owner.execute("SELECT count(*) AS n FROM index_job WHERE state = 'done'").fetchone()["n"] == 4
    response = loaded.post(loaded.users["public"], CONTROL)
    loaded.refused(response)
    assert all(marker not in response.text for marker in CLOSED_MARKERS)
