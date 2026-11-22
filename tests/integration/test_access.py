"""История 2: ответ в пределах текущих прав. Проверяется и ответ, и то, что получила модель."""
import json

import pytest

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


@pytest.mark.parametrize("role", ["public", "unknown"])
def test_limited_roles_get_only_public_materials(loaded, role):
    answer = loaded.ask(role, CONTROL)
    assert answer["outcome"] == "found"
    assert set(loaded.keys(answer)) == {"lecture_public", "same_title_public"}
    assert_nothing_closed(loaded, answer)


@pytest.mark.parametrize("role", ["public", "unknown"])
def test_query_matching_only_closed_episode_reveals_nothing(loaded, role):
    answer = loaded.ask(role, CLOSED_ONLY)
    assert answer["outcome"] in ("clarify", "empty")
    assert answer["cards"] == []
    assert_nothing_closed(loaded, answer)


def test_revoked_club_rights_apply_to_repeated_query_without_restart(loaded):
    assert "razbor_club" in loaded.keys(loaded.ask("club", CONTROL))
    loaded.set_role("club", "public")
    loaded.ai.explain_calls.clear()
    assert_nothing_closed(loaded, loaded.ask("club", CONTROL))


def test_repeated_event_is_recomputed_with_current_rights(loaded):
    first = loaded.ask("club", CONTROL, event_id="same")
    assert "razbor_club" in loaded.keys(first)
    loaded.set_role("club", "unknown")
    loaded.ai.explain_calls.clear()
    repeat = loaded.ask("club", CONTROL, event_id="same")
    assert repeat["random_id"] == first["random_id"]
    assert_nothing_closed(loaded, repeat)


def test_removed_tester_is_forbidden(loaded):
    loaded.set_role("club", None)
    assert loaded.post(loaded.users["club"], CONTROL).status_code == 403


def test_rights_changed_during_processing_do_not_leak(loaded):
    """Права отозваны, пока модель готовит пояснение: ответ пересчитывается."""
    fired = []

    def revoke():
        if not fired:
            fired.append(True)
            loaded.set_role("club", "public")

    loaded.ai.before_explain = revoke
    answer = loaded.ask("club", CONTROL)
    assert fired
    assert "razbor_club" not in loaded.keys(answer)
    dump = json.dumps(answer, ensure_ascii=False)
    assert all(marker not in dump for marker in CLOSED_MARKERS)


def test_material_with_unknown_or_missing_audience_is_hidden_from_everyone(loaded):
    materials = loaded.policy()["materials"]
    materials["-100001_111"] = "unknown"
    materials.pop("-100001_333")
    loaded.import_policy(materials=materials)
    assert loaded.keys(loaded.ask("club", CONTROL)) == ["lecture_public"]


def test_material_reclassified_to_club_disappears_for_public(loaded):
    materials = loaded.policy()["materials"]
    materials["-100001_222"] = "club"
    loaded.import_policy(materials=materials)
    assert loaded.keys(loaded.ask("public", CONTROL)) == ["same_title_public"]


def test_operator_rights_do_not_grant_participant_access(loaded):
    """Индексатор прочитал закрытую запись, но участник без роли club её не получает."""
    assert loaded.owner.execute("SELECT count(*) AS n FROM index_job WHERE state = 'done'").fetchone()["n"] == 4
    assert_nothing_closed(loaded, loaded.ask("public", CONTROL))
