"""Чтение постов VK: обход закрытого wall.getById и различение отказов."""
import pytest

from ecoprofi.indexer.retry import SourceDenied
from ecoprofi.indexer.vk_sources import API, VkSources

OWNER = -100001
POST = {"id": 7, "owner_id": OWNER, "attachments": [
    {"type": "video", "video": {"owner_id": OWNER, "id": 456, "title": "Лекция"}}]}


class Transport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def post(self, url, fields):
        method = url.removeprefix(API)
        self.calls.append(method)
        return self.replies[method]


def test_post_is_read_from_feed_when_get_by_id_is_closed_for_token_type():
    transport = Transport({
        "wall.getById": {"error": {"error_code": 1051}},
        "wall.get": {"response": {"count": 1, "items": [POST]}},
    })
    videos = VkSources("token", transport, owner_id=OWNER).post_videos(7)
    assert [(v.owner_id, v.video_id, v.title) for v in videos] == [(OWNER, 456, "Лекция")]
    assert transport.calls == ["wall.getById", "wall.get"]


def test_invalid_token_is_reported_as_denied_without_feed_scan():
    transport = Transport({"wall.getById": {"error": {"error_code": 5}}})
    with pytest.raises(SourceDenied):
        VkSources("token", transport, owner_id=OWNER).post_videos(7)
    assert transport.calls == ["wall.getById"]
