"""История 2: повтор событий, изоляция диалогов и доставка через адаптер."""
import httpx

from ecoprofi.vk_adapter.__main__ import BotClient, to_event
from ecoprofi.vk_adapter.delivery import SendUncertain, deliver
from tests.integration.conftest import SERVICE_KEY

CONTROL = "загрязняющих веществ"


class RecordingVk:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.sent = []

    def send(self, peer_id, random_id, text):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((peer_id, random_id, text))


def bot_client(stand) -> BotClient:
    """Клиент адаптера, подключённый к приложению bot в памяти."""
    client = BotClient("http://bot", SERVICE_KEY)
    client._http = stand.client
    client._http.headers["X-Service-Key"] = SERVICE_KEY
    return client


def update(stand, role, text, event_id):
    user = stand.users[role]
    return to_event({"type": "message_new", "group_id": stand.group_id, "event_id": event_id,
                     "object": {"message": {"id": 1, "from_id": user, "peer_id": user, "text": text}}},
                    stand.group_id)


def test_duplicate_event_is_registered_once(loaded):
    first = loaded.ask("club", CONTROL, event_id="dup")
    second = loaded.ask("club", CONTROL, event_id="dup")
    assert second["random_id"] == first["random_id"]
    assert loaded.owner.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 1


def test_event_id_of_another_dialog_is_rejected(loaded):
    loaded.ask("club", CONTROL, event_id="shared")
    assert loaded.post(loaded.users["public"], CONTROL, event_id="shared").status_code == 403


def test_two_dialogs_get_only_their_own_answers(loaded):
    vk, bot = RecordingVk(), bot_client(loaded)
    assert deliver(vk, bot, update(loaded, "club", CONTROL, "a1"), sleep=lambda s: None) == "sent"
    assert deliver(vk, bot, update(loaded, "public", CONTROL, "b1"), sleep=lambda s: None) == "sent"
    by_peer = {peer: text for peer, _, text in vk.sent}
    assert set(by_peer) == {loaded.users["club"], loaded.users["public"]}
    assert "video-100001_111" in by_peer[loaded.users["club"]]
    assert "video-100001_111" not in by_peer[loaded.users["public"]]
    assert "взвешенные вещества" not in by_peer[loaded.users["public"]]
    states = loaded.owner.execute("SELECT state FROM event ORDER BY event_id").fetchall()
    assert [s["state"] for s in states] == ["delivered", "delivered"]


def test_uncertain_send_resends_current_answer_with_same_random_id(loaded):
    """Между неудачной отправкой и повтором клубные права отозваны."""
    class RevokingVk(RecordingVk):
        def send(self, peer_id, random_id, text):
            if self.errors:
                self.first_attempt = (random_id, text)
                loaded.set_role("club", "public")
            super().send(peer_id, random_id, text)

    vk = RevokingVk(errors=[SendUncertain()])
    assert deliver(vk, bot_client(loaded), update(loaded, "club", CONTROL, "u1"), sleep=lambda s: None) == "sent"
    assert "video-100001_111" in vk.first_attempt[1]
    (peer, random_id, text), = vk.sent
    assert peer == loaded.users["club"]
    assert random_id == vk.first_attempt[0]
    assert "video-100001_111" not in text and "взвешенные вещества" not in text
    assert loaded.owner.execute("SELECT count(*) AS n FROM event").fetchone()["n"] == 1


def test_not_admitted_sender_gets_no_message(loaded):
    vk = RecordingVk()
    event = {"event_id": "x1", "group_id": loaded.group_id, "peer_id": loaded.outsider,
             "user_id": loaded.outsider, "text": CONTROL}
    assert deliver(vk, bot_client(loaded), event, sleep=lambda s: None) == "skipped"
    assert vk.sent == []


def test_bot_client_treats_server_error_as_temporary(loaded):
    import pytest
    from ecoprofi.vk_adapter.delivery import BotUnavailable
    client = BotClient("http://bot", SERVICE_KEY, transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(BotUnavailable):
        client.search({"event_id": "e"})
