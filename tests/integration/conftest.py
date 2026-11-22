"""Изолированная PostgreSQL с pgvector, миграции и загрузка синтетического корпуса.

Запуск: docker compose --profile test run --rm tests
Без переменных TEST_*_DATABASE_URL интеграционные проверки пропускаются.
"""
import itertools
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ecoprofi.bot.api import create_app
from ecoprofi.bot.import_policy import apply_policy, parse_policy
from ecoprofi.bot.migrate import migrate
from ecoprofi.db import connect
from ecoprofi.indexer.__main__ import run_posts
from ecoprofi.indexer.retry import SourceDeleted, SourceTimeout
from ecoprofi.indexer.vk_sources import VkSources
from tests.fixtures.fake_ai import FakeAI

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SERVICE_KEY = "test-service-key"
OWNER_ID = -100001
TABLES = ("operation_log, index_job, event, dialog_context, participant, test_group, "
          "episode, material_version, material, material_policy, lecture")


def _dsn(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} не задана: нужен стенд docker compose --profile test")
    return value


class FakeVkTransport:
    """Подмена сети VK: отвечает данными vk_wall.json и файлами .vtt."""

    def __init__(self):
        self.items = json.loads((FIXTURES / "vk_wall.json").read_text(encoding="utf-8"))["response"]["items"]
        self.tracks: dict[str, bytes] = {}      # подмена содержимого дорожки по имени файла
        self.get_failures: list[Exception] = [] # очередь сбоев чтения дорожки
        self.missing_posts: set[int] = set()    # пост не возвращён (пропуск в ленте)
        self.deleted_posts: set[int] = set()    # подтверждённое удаление
        self.calls: list[str] = []
        self.fresh = itertools.count(1)

    def _video(self, owner_id, video_id):
        for item in self.items:
            for attachment in item["attachments"]:
                video = attachment["video"]
                if (video["owner_id"], video["id"]) == (owner_id, video_id):
                    return video
        return None

    def post(self, url, fields):
        method = url.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method == "wall.getById":
            post_id = int(fields["posts"].split("_")[1])
            if post_id in self.missing_posts:
                return {"response": {"items": []}}
            item = next((i for i in self.items if i["id"] == post_id), None)
            if item and post_id in self.deleted_posts:
                item = dict(item, deleted=True)
            return {"response": {"items": [item] if item else []}}
        if method == "wall.get":
            return {"response": {"count": 0, "items": []}}
        if method == "video.get":
            owner, video = (int(x) for x in fields["videos"].split("_")[:2])
            record = self._video(owner, video)
            if record is None:
                return {"response": {"items": []}}
            record = json.loads(json.dumps(record))
            for track in record.get("subtitles", []):
                track["url"] += f"?fresh={next(self.fresh)}"   # каждый раз новый временный адрес
            return {"response": {"items": [record]}}
        raise AssertionError(f"неожиданный метод {method}")

    def get(self, url):
        self.calls.append("track:" + url)
        if self.get_failures:
            raise self.get_failures.pop(0)
        name = url.split("?")[0].rsplit("/", 1)[-1]
        return self.tracks.get(name) or (FIXTURES / name).read_bytes()


class Stand:
    """Всё, что нужно сценарию: соединения, подмены, загрузка и запросы."""

    def __init__(self, owner, indexer_dsn, bot_dsn):
        self.owner = owner
        self.indexer_dsn = indexer_dsn
        self.bot_dsn = bot_dsn
        self.ai = FakeAI()
        self.transport = FakeVkTransport()
        self.corpus = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
        self.expectations = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))
        self.group_id = self.corpus["group_id"]
        self.materials = {m["key"]: m for l in self.corpus["lectures"] for m in l["materials"]}
        self.schedule = {m["post_id"]: (l["schedule_row"], l["title"])
                         for l in self.corpus["lectures"] for m in l["materials"]}
        self.users = {p["role"]: p["vk_id"] for p in self.corpus["participants"] if p["tester"]}
        self.outsider = next(p["vk_id"] for p in self.corpus["participants"] if not p["tester"])
        self.client = TestClient(create_app(
            connect_fn=lambda: connect(bot_dsn), ai=self.ai, service_key=SERVICE_KEY))
        self._events = itertools.count(1)

    # --- политика ---
    def policy(self, *, testers=None, materials=None) -> dict:
        testers = testers if testers is not None else {
            str(p["vk_id"]): p["role"] for p in self.corpus["participants"] if p["tester"]}
        materials = materials if materials is not None else {
            f"{m['owner_id']}_{m['video_id']}": m["audience"] for m in self.materials.values()}
        return {"group_id": self.group_id, "testers": testers, "materials": materials}

    def import_policy(self, **changes) -> None:
        apply_policy(self.owner, parse_policy(self.policy(**changes)))

    def set_role(self, role_key: str, new_role: str | None) -> None:
        """Сменить роль участника повторным импортом; None — убрать из политики."""
        testers = {str(p["vk_id"]): p["role"] for p in self.corpus["participants"] if p["tester"]}
        vk_id = str(self.users[role_key])
        if new_role is None:
            testers.pop(vk_id)
        else:
            testers[vk_id] = new_role
        self.import_policy(testers=testers)

    # --- индексатор ---
    def index(self, post_ids=None, *, sleep=lambda s: None) -> list[dict]:
        post_ids = post_ids or sorted(self.schedule)
        with connect(self.indexer_dsn) as conn:
            vk = VkSources("fixture-token", transport=self.transport, owner_id=OWNER_ID)
            return run_posts(conn, self.ai, vk, post_ids, self.schedule, sleep=sleep)

    def load(self) -> None:
        self.import_policy()
        results = self.index()
        assert [r["outcome"] for r in results] == ["published"] * 4, results

    # --- запросы ---
    def material_id(self, key: str) -> int:
        m = self.materials[key]
        return self.owner.execute("SELECT id FROM material WHERE owner_id = %s AND video_id = %s",
                                  (m["owner_id"], m["video_id"])).fetchone()["id"]

    def key_of(self, material_id: int) -> str:
        row = self.owner.execute("SELECT video_id FROM material WHERE id = %s", (material_id,)).fetchone()
        return next(k for k, m in self.materials.items() if m["video_id"] == row["video_id"])

    def post(self, user_id: int, text: str, *, event_id=None, peer_id=None, group_id=None, key=SERVICE_KEY):
        body = {"event_id": event_id or f"ev-{next(self._events)}", "group_id": group_id or self.group_id,
                "peer_id": user_id if peer_id is None else peer_id, "user_id": user_id, "text": text}
        headers = {"X-Service-Key": key} if key else {}
        return self.client.post("/api/search", json=body, headers=headers)

    def ask(self, role_key: str, text: str, **kwargs) -> dict:
        response = self.post(self.users[role_key], text, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def keys(self, answer: dict) -> list[str]:
        return [self.key_of(c["material_id"]) for c in answer["cards"]]

    def counts(self) -> dict:
        return self.owner.execute(
            "SELECT (SELECT count(*) FROM material_version) AS versions, "
            "(SELECT count(*) FROM episode) AS episodes, "
            "(SELECT count(*) FROM material WHERE published_version_id IS NOT NULL) AS published"
        ).fetchone()


@pytest.fixture(scope="session")
def owner_conn():
    conn = connect(_dsn("TEST_OWNER_DATABASE_URL"), vector=False)
    conn.autocommit = True
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def stand(owner_conn):
    owner_conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    return Stand(owner_conn, _dsn("TEST_INDEXER_DATABASE_URL"), _dsn("TEST_BOT_DATABASE_URL"))


@pytest.fixture
def loaded(stand):
    stand.load()
    return stand


__all__ = ["SourceDeleted", "SourceTimeout"]
