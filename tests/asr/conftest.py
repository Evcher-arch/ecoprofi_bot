"""Фикстуры распознавания (006).

Проверки без БД (окно, фильтр отрезков, подготовка звука, клиент ПК) идут
везде. Проверки с БД требуют стенда: docker compose --profile test run --rm tests pytest tests/asr
Без переменных TEST_*_DATABASE_URL они пропускаются.
"""
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

OWNER_ID = -100001
FAKE_MEDIA = "https://vkvd123.okcdn.ru/?expires=1&sig=SECRETSIG&srcIp=1.2.3.4"
TABLES = ("asr_transcript, asr_job, asr_run, asr_worker, fragment, material_link, material_version, material, "
          "index_job, alert, lecture")


def _dsn(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} не задана: нужен стенд docker compose --profile test")
    return value


class Clock:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeVk:
    """Подмена VkSources.media_streams: адреса медиа не настоящие и проверяются на утечку."""

    def __init__(self):
        self.records = {}       # (owner, item) -> dict | Exception
        self.calls = 0

    def media_streams(self, owner_id, video_id, access_key=None):
        self.calls += 1
        value = self.records.get((owner_id, video_id))
        if isinstance(value, Exception):
            raise value
        if value is None:
            return {"files": {"mp4_144": FAKE_MEDIA}, "duration": 600, "date": 1700000000}
        return value


class FakeRunner:
    """Подмена subprocess.run для ffmpeg: пишет файл звука или возвращает ошибку."""

    def __init__(self, returncode: int = 0, stderr: str = "", payload: bytes = b"OggS" + b"\0" * 2048):
        self.returncode, self.stderr, self.payload = returncode, stderr, payload
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        if self.returncode == 0:
            with open(cmd[-1], "wb") as handle:
                handle.write(self.payload)

        class Result:
            pass
        result = Result()
        result.returncode, result.stderr, result.stdout = self.returncode, self.stderr, ""
        return result


def gz(body: dict) -> bytes:
    return gzip.compress(json.dumps(body).encode())


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(scope="session")
def asr_owner():
    from ecoprofi.bot.migrate import migrate
    from ecoprofi.db import connect
    conn = connect(_dsn("TEST_OWNER_DATABASE_URL"), vector=False)
    conn.autocommit = True
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def db(asr_owner):
    """Соединение под ролью индексатора на чистых таблицах."""
    from ecoprofi.db import connect
    asr_owner.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    asr_owner.execute("UPDATE asr_schedule SET weekdays = '{7}', start_at = '01:00', window_length = '8 hours', "
                      "catch_up = false, paused = false, initial_pass = 'done', initial_started_at = NULL, "
                      "initial_done_at = NULL, manual_requested_at = NULL, manual_only = NULL, last_due_at = NULL, "
                      "silence_alert_days = 14")
    conn = connect(_dsn("TEST_INDEXER_DATABASE_URL"), vector=False)
    conn.autocommit = True
    yield conn
    conn.close()


def add_video(conn, item_id: int, *, initial: bool = False, state: str = "queued", version: str | None = "v1",
              owner_id: int = OWNER_ID) -> int:
    """Запись каталога с карточкой и заданием распознавания."""
    lecture = conn.execute("INSERT INTO lecture (schedule_row, title) VALUES (%s, %s) "
                           "ON CONFLICT (schedule_row) DO UPDATE SET title = excluded.title RETURNING id",
                           ("asr-row", "Лекция")).fetchone()["id"]
    material = conn.execute(
        "INSERT INTO material (item_type, owner_id, item_id, kind, source_format, title, description, url) "
        "VALUES ('video', %s, %s, 'video', 'video', %s, '', %s) RETURNING id",
        (owner_id, item_id, f"Запись {item_id}", f"https://vkvideo.ru/video{owner_id}_{item_id}")).fetchone()["id"]
    conn.execute("INSERT INTO material_link (lecture_id, material_id, post_id, link_type) VALUES (%s, %s, 1, "
                 "'attachment')", (lecture, material))
    version_id = conn.execute(
        "INSERT INTO material_version (material_id, content_hash, processor_version, embedding_model, card_state, "
        "text_state) VALUES (%s, 'card', 'subtitles-2', 'fake://doc/1', 'ready', 'absent') RETURNING id",
        (material,)).fetchone()["id"]
    conn.execute("UPDATE material SET published_version_id = %s WHERE id = %s", (version_id, material))
    conn.execute("INSERT INTO index_job (item_type, owner_id, item_id, post_id, state, waiting_source) "
                 "VALUES ('video', %s, %s, 1, 'done', true)", (owner_id, item_id))
    conn.execute("INSERT INTO asr_job (owner_id, item_id, material_id, source_version, state, initial) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (owner_id, item_id, material, version, state, initial))
    return material


@pytest.fixture
def worker_key(db):
    from ecoprofi.asr import workers
    return workers.issue_key(db, "owner-pc")


@pytest.fixture
def gateway(db, clock, tmp_path):
    """Шлюз с подменой VK и AI; фоновые потоки не запускаются, шаги вызываются явно."""
    from fastapi.testclient import TestClient

    from ecoprofi.asr.gateway import create_app
    from ecoprofi.db import connect
    from tests.fixtures.fake_ai import FakeAI

    vk = FakeVk()
    runner = FakeRunner()
    dsn = _dsn("TEST_INDEXER_DATABASE_URL")

    def connect_fn():
        conn = connect(dsn, vector=False)
        conn.autocommit = True
        return conn

    import ecoprofi.asr.audio as audio_module
    original = audio_module.extract

    def fake_extract(url, out, *, runner_=None, timeout=None, **kw):
        return original(url, out, runner=runner)

    audio_module.extract = fake_extract
    app = create_app(connect_fn=connect_fn, vk_factory=lambda: vk, ai_factory=FakeAI, tmp_dir=tmp_path,
                     background=False, clock=clock)
    client = TestClient(app)
    client.vk, client.runner, client.tmp, client.app_ref = vk, runner, tmp_path, app
    yield client
    audio_module.extract = original
