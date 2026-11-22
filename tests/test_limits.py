"""Различение причин сбоев (T006–T007): авторизация, ограничение частоты, лимит AI Studio."""
import json

import httpx
import pytest

from ecoprofi.ai import AILimit, YandexAI
from ecoprofi.indexer.retry import SourceAuth, SourceRateLimit, SourceTimeout, with_retry
from ecoprofi.indexer.vk_sources import API, VkSources

OWNER = -100001


class Transport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def post(self, url, fields):
        self.calls.append((url.removeprefix(API), fields["access_token"]))
        return self.replies.pop(0)


class Provider:
    def __init__(self):
        self.current, self.forced = "t1", []

    def token(self):
        return self.current

    def force_refresh(self, failed):
        self.forced.append(failed)
        self.current = "t2"
        return self.current


OK = {"response": {"count": 3, "items": []}}
AUTH = {"error": {"error_code": 5}}


def test_auth_failure_forces_one_refresh_then_retries():
    provider, transport = Provider(), Transport(AUTH, OK)
    assert VkSources(provider, transport, owner_id=OWNER).paid_feed_count() == 3
    assert provider.forced == ["t1"] and [t for _, t in transport.calls] == ["t1", "t2"]


def test_second_auth_failure_stops_with_auth_reason():
    provider = Provider()
    with pytest.raises(SourceAuth) as info:
        VkSources(provider, Transport(AUTH, AUTH), owner_id=OWNER).paid_feed_count()
    assert info.value.reason == "auth" and provider.forced == ["t1"]


def test_too_fast_is_waited_without_spending_attempts():
    pauses = []
    transport = Transport({"error": {"error_code": 6}}, {"error": {"error_code": 6}}, OK)
    vk = VkSources("t", transport, owner_id=OWNER, sleep=pauses.append)
    assert vk.paid_feed_count() == 3 and pauses == [1.0, 1.0]


def test_daily_limit_pauses_the_run():
    with pytest.raises(SourceRateLimit):
        VkSources("t", Transport({"error": {"error_code": 29}}), owner_id=OWNER).paid_feed_count()


def test_run_stoppers_are_not_retried():
    calls = []

    def operation(attempt):
        calls.append(attempt)
        raise SourceAuth("x")

    with pytest.raises(SourceAuth):
        with_retry(operation, sleep=lambda s: None)
    assert calls == [1]
    with pytest.raises(SourceTimeout):
        with_retry(lambda a: (_ for _ in ()).throw(SourceTimeout("x")), sleep=lambda s: None)


def test_calls_are_throttled_to_three_per_second():
    now, pauses = [0.0], []

    def sleep(seconds):
        pauses.append(round(seconds, 2))
        now[0] += seconds

    vk = VkSources("t", Transport(OK, OK, OK), owner_id=OWNER, sleep=sleep, clock=lambda: now[0],
                   min_interval=0.34)
    for _ in range(3):
        vk.paid_feed_count()
    assert pauses == [0.34, 0.34]


def ai(handler):
    return YandexAI(folder_id="f", api_key="secret-key", doc_model="m", transport=httpx.MockTransport(handler))


def test_rejected_key_is_an_ai_limit():
    with pytest.raises(AILimit) as info:
        ai(lambda request: httpx.Response(403)).embed_documents(["текст"])
    assert info.value.status == 403 and "secret-key" not in str(info.value)


def test_short_rate_limit_is_waited():
    replies = [httpx.Response(429), httpx.Response(200, json={"embedding": [0.1] * 256})]
    pauses = []
    vectors = ai(lambda request: replies.pop(0)).embed_documents(["текст"], sleep=pauses.append)
    assert len(vectors) == 1 and pauses == [2.0]


def test_persistent_rate_limit_stops():
    with pytest.raises(AILimit):
        ai(lambda request: httpx.Response(429)).embed_documents(["текст"], sleep=lambda s: None)


def test_probe_by_id_reports_feed_fields():
    post = {"id": 7, "owner_id": OWNER, "attachments": [{}], "donut": {"is_donut": True}, "text": "x"}
    found = VkSources("t", Transport({"response": {"items": [post]}}), owner_id=OWNER).probe_by_id([7, 8])
    assert found == {7: {"attachments": 1, "fields": ["donut"]}, 8: None}
    assert json.dumps(found)
