"""Обмен адаптера с фиктивными транспортами VK и bot."""
from ecoprofi.vk_adapter.__main__ import VkError, run, to_event
from ecoprofi.vk_adapter.delivery import BotUnavailable, SendUncertain, deliver

GROUP = 900001


def update(peer=5001, sender=5001, text="загрязняющих веществ", group=GROUP, event_id="e1", kind="message_new"):
    return {"type": kind, "group_id": group, "event_id": event_id,
            "object": {"message": {"id": 10, "from_id": sender, "peer_id": peer, "text": text}}}


class FakeVk:
    group_id = GROUP

    def __init__(self, replies, send_errors=()):
        self.replies = list(replies)
        self.send_errors = list(send_errors)
        self.sent = []
        self.server_requests = 0

    def long_poll_server(self):
        self.server_requests += 1
        return {"server": "https://lp.invalid/x", "key": "k", "ts": "1"}

    def poll(self, server):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def send(self, peer_id, random_id, text):
        if self.send_errors:
            raise self.send_errors.pop(0)
        self.sent.append((peer_id, random_id, text))


class FakeBot:
    def __init__(self, answers=None, errors=()):
        self.answers = answers
        self.errors = list(errors)
        self.requests = []
        self.delivered_events = []

    def search(self, event):
        self.requests.append(event)
        if self.errors:
            raise self.errors.pop(0)
        if self.answers is not None:
            answer = self.answers.pop(0)
            return answer and dict(answer)
        return {"event_id": event["event_id"], "peer_id": event["peer_id"], "outcome": "found",
                "text": f"ответ на «{event['text']}»", "cards": [], "random_id": 777}

    def delivered(self, event):
        self.delivered_events.append(event["event_id"])


def no_sleep(seconds):
    pass


def test_to_event_accepts_only_personal_text_messages_of_own_group():
    assert to_event(update(), GROUP) == {"event_id": "e1", "group_id": GROUP, "peer_id": 5001,
                                        "user_id": 5001, "text": "загрязняющих веществ"}
    assert to_event(update(peer=2000000001), GROUP) is None      # беседа
    assert to_event(update(sender=-5, peer=-5), GROUP) is None   # сообщество
    assert to_event(update(text="   "), GROUP) is None           # пустой текст
    assert to_event(update(group=1), GROUP) is None              # чужая группа
    assert to_event(update(kind="message_reply"), GROUP) is None


def test_exchange_answers_in_source_dialog():
    vk = FakeVk([{"ts": "2", "updates": [update(), update(peer=5002, sender=5002, event_id="e2", text="перечень")]}])
    bot = FakeBot()
    run(vk, bot, sleep=no_sleep, max_polls=1)
    assert [(peer, rid) for peer, rid, _ in vk.sent] == [(5001, 777), (5002, 777)]
    assert "загрязняющих веществ" in vk.sent[0][2] and "перечень" in vk.sent[1][2]
    assert bot.delivered_events == ["e1", "e2"]


def test_lost_long_poll_parameters_are_requested_again_for_same_group():
    vk = FakeVk([{"failed": 2}, {"failed": 1, "ts": "9"}, VkError(10), {"ts": "10", "updates": [update()]}])
    run(vk, FakeBot(), sleep=no_sleep, max_polls=4)
    assert vk.server_requests == 3      # начало, после failed=2 и после сбоя
    assert len(vk.sent) == 1


def test_rejected_event_sends_nothing():
    vk = FakeVk([])
    assert deliver(vk, FakeBot(answers=[None]), to_event(update(), GROUP), sleep=no_sleep) == "skipped"
    assert vk.sent == []


def test_answer_for_another_peer_is_dropped():
    vk = FakeVk([])
    foreign = {"event_id": "e1", "peer_id": 5002, "text": "чужое", "random_id": 1}
    assert deliver(vk, FakeBot(answers=[foreign]), to_event(update(), GROUP), sleep=no_sleep) == "mismatch"
    assert vk.sent == []


def test_bot_outage_repeats_same_event_id():
    vk, bot = FakeVk([]), FakeBot(errors=[BotUnavailable(), BotUnavailable()])
    assert deliver(vk, bot, to_event(update(), GROUP), sleep=no_sleep) == "sent"
    assert [r["event_id"] for r in bot.requests] == ["e1", "e1", "e1"]
    assert len(vk.sent) == 1


def test_uncertain_send_keeps_random_id_and_asks_for_current_answer():
    vk = FakeVk([], send_errors=[SendUncertain()])
    event = to_event(update(), GROUP)
    first = {"event_id": "e1", "peer_id": 5001, "text": "закрытая выдержка", "random_id": 42}
    second = {"event_id": "e1", "peer_id": 5001, "text": "только публичное", "random_id": 42}
    bot = FakeBot(answers=[first, second])
    assert deliver(vk, bot, event, sleep=no_sleep) == "sent"
    assert vk.sent == [(5001, 42, "только публичное")]
    assert len(bot.requests) == 2
