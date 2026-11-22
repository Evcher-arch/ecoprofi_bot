"""Проверка подписки на клуб Экопрофи: groups.getMembers(filter=donut) с фиктивным транспортом VK."""
from urllib.parse import parse_qs

import httpx
import pytest

from ecoprofi.bot.club import CLUB_GROUP_ID, ClubMembers, ClubUnavailable

TOKEN = "secret-club-token"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def vk(dons, *, error=None, calls=None, page=None):
    def handler(request: httpx.Request) -> httpx.Response:
        fields = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if calls is not None:
            calls.append(fields)
        if error is not None:
            return httpx.Response(200, json={"error": {"error_code": error, "error_msg": "x"}})
        offset, count = int(fields["offset"]), min(int(fields["count"]), page or 1000)
        return httpx.Response(200, json={"response": {"count": len(dons), "items": dons[offset:offset + count]}})
    return httpx.MockTransport(handler)


def test_subscriber_is_member_and_others_are_not():
    calls = []
    club = ClubMembers(TOKEN, transport=vk([11, 12], calls=calls))
    assert club(11) and club.is_member(12)
    assert not club(13)
    (fields,) = calls                                    # список прочитан один раз
    assert fields["group_id"] == str(CLUB_GROUP_ID) and fields["filter"] == "donut"


def test_all_pages_are_read(monkeypatch):
    monkeypatch.setattr("ecoprofi.bot.club.PAGE", 2)
    calls = []
    club = ClubMembers(TOKEN, transport=vk([1, 2, 3, 4, 5], calls=calls, page=2))
    assert club(5)
    assert [c["offset"] for c in calls] == ["0", "2", "4"]


def test_list_is_reread_after_ttl():
    dons, clock, calls = [11], Clock(), []
    club = ClubMembers(TOKEN, ttl=60, clock=clock, transport=vk(dons, calls=calls))
    assert club(11)
    dons.clear()                                         # подписка отменена
    clock.now = 59
    assert club(11) and len(calls) == 1                  # в пределах срока — прежний список
    clock.now = 60
    assert not club(11) and len(calls) == 2


@pytest.mark.parametrize("transport", [
    vk([], error=15),                                    # нет прав на список донов
    httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("timeout"))),
    httpx.MockTransport(lambda r: httpx.Response(502, text="Bad gateway")),
])
def test_failed_check_denies_access_without_leaking_token(transport, caplog):
    club = ClubMembers(TOKEN, transport=transport)
    with pytest.raises(ClubUnavailable) as raised:
        club(11)
    assert TOKEN not in str(raised.value) and TOKEN not in caplog.text


def test_failure_is_not_cached_as_empty_list():
    state = {"error": 10}

    def handler(request):
        if state["error"]:
            return httpx.Response(200, json={"error": {"error_code": state.pop("error")}})
        return httpx.Response(200, json={"response": {"count": 1, "items": [11]}})

    state["error"] = 10
    club = ClubMembers(TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(ClubUnavailable):
        club(11)
    state["error"] = None
    assert club(11)


def test_missing_token_denies_access():
    with pytest.raises(ClubUnavailable, match="CLUB_GROUP_TOKEN"):
        ClubMembers("", transport=vk([11]))(11)


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CLUB_GROUP_TOKEN", TOKEN)
    monkeypatch.setenv("CLUB_GROUP_ID", "123")
    monkeypatch.setenv("CLUB_CACHE_SECONDS", "15")
    club = ClubMembers.from_env()
    assert (club.group_id, club._ttl) == (123, 15.0)
    monkeypatch.delenv("CLUB_GROUP_ID")
    assert ClubMembers.from_env().group_id == CLUB_GROUP_ID


# --- исключения: владелец и администраторы сообщества без подписки ---

def test_exempt_user_is_admitted_without_asking_vk():
    calls = []
    club = ClubMembers(TOKEN, transport=vk([11], calls=calls), exempt={177068782})
    assert club(177068782) and calls == []
    assert club(11) and not club(12)


def test_exempt_user_is_admitted_when_check_fails():
    club = ClubMembers("", transport=vk([], error=15), exempt={177068782})
    assert club(177068782)
    with pytest.raises(ClubUnavailable):
        club(11)


def test_exempt_ids_from_environment(monkeypatch):
    monkeypatch.setenv("CLUB_GROUP_TOKEN", TOKEN)
    monkeypatch.setenv("CLUB_EXEMPT_IDS", "177068782, 5")
    assert ClubMembers.from_env().exempt == {177068782, 5}
    monkeypatch.setenv("CLUB_EXEMPT_IDS", "")
    assert ClubMembers.from_env().exempt == frozenset()
    monkeypatch.setenv("CLUB_EXEMPT_IDS", "177068782,admin")
    with pytest.raises(ValueError, match="admin"):
        ClubMembers.from_env()
