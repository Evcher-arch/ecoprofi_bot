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


# --- функция 002: вложения и упоминания за один проход по лентам ---

class FeedTransport:
    """Подмена wall.get: ленты по фильтру, страницы по два поста."""

    def __init__(self, feeds):
        self.feeds, self.calls = feeds, []

    def post(self, url, fields):
        feed, offset = fields["filter"], int(fields["offset"])
        self.calls.append((feed, offset))
        items = self.feeds.get(feed, [])
        return {"response": {"count": len(items), "items": items[offset:offset + 2]}}


def _post(post_id, attachments=(), text="", **extra):
    return {"id": post_id, "owner_id": OWNER, "text": text, "attachments": list(attachments), **extra}


def _doc(doc_id, title="Файл.pdf", ext="pdf"):
    return {"type": "doc", "doc": {"owner_id": 77, "id": doc_id, "title": title, "ext": ext,
                                   "url": f"https://files.invalid/{doc_id}?hash=abc"}}


def _read(feeds, wanted):
    transport = FeedTransport(feeds)
    vk = VkSources("token", transport=transport, owner_id=OWNER)
    return vk.posts_materials(wanted, sleep=lambda s: None), transport


def test_posts_materials_reads_documents_videos_and_paid_flag():
    video = {"type": "video", "video": {"owner_id": OWNER, "id": 456, "title": "Лекция"}}
    found, _ = _read({"donut": [_post(2, [_doc(20)])], "owner": [_post(1, [_doc(10, "Слайды.PDF", "PDF"), video])]},
                     [1, 2])
    assert (found[1].found, found[1].paid_feed, found[2].paid_feed) == (True, False, True)
    doc = found[1].docs[0]
    assert (doc.owner_id, doc.doc_id, doc.title, doc.ext, doc.link_type, doc.paid_feed) == (
        77, 10, "Слайды.PDF", "pdf", "attachment", False)
    assert doc.file_url.endswith("hash=abc")
    assert [(v.video_id, v.paid_feed) for v in found[1].videos] == [(456, False)]
    assert found[2].docs[0].paid_feed is True


def test_mentions_in_text_and_links_are_not_attachments():
    link = {"type": "link", "link": {"url": "https://vk.com/doc77_30?dl=1"}}
    text = f"См. video{OWNER}_900 и doc77_10 в комментариях"
    found, _ = _read({"owner": [_post(1, [_doc(10), link], text)]}, [1])
    assert [d.doc_id for d in found[1].docs] == [10]
    assert found[1].mentions == [("video", OWNER, 900), ("doc", 77, 30)]     # приложенный doc77_10 не упоминание


def test_missing_post_is_not_reported_as_deleted():
    found, _ = _read({"owner": [_post(1)]}, [1, 5])
    assert (found[5].found, found[5].deleted) == (False, False)


def test_deleted_flag_is_passed_through():
    found, _ = _read({"owner": [_post(1, [_doc(10)], deleted=True)]}, [1])
    assert found[1].deleted and found[1].docs == []


def test_scan_stops_when_all_posts_are_found():
    feeds = {"donut": [_post(9), _post(1)], "owner": [_post(n) for n in range(100, 110)]}
    found, transport = _read(feeds, [1])
    assert found[1].paid_feed
    assert transport.calls == [("donut", 0)]             # открытая лента не читалась


def test_foreign_owner_post_with_same_number_is_ignored():
    foreign = dict(_post(1, [_doc(10)]), owner_id=-5)
    found, _ = _read({"owner": [foreign]}, [1])
    assert not found[1].found
