"""Перенос данных 001 миграцией 002 (T007): записи и эпизоды остаются на месте."""
import shutil

from ecoprofi.bot.migrate import MIGRATIONS_DIR, migrate

SCHEMA = "upgrade_check"


def test_upgrade_keeps_materials_versions_and_episodes(owner_conn, tmp_path):
    only_001 = tmp_path / "migrations"
    only_001.mkdir()
    shutil.copy(MIGRATIONS_DIR / "001_pilot.sql", only_001)
    conn = owner_conn
    conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.execute(f"SET search_path TO {SCHEMA}, public")
    try:
        assert migrate(conn, only_001) == ["001_pilot.sql"]
        lecture = conn.execute(
            "INSERT INTO lecture (schedule_row, title) VALUES ('pilot-control', 'Контрольная строка') RETURNING id"
        ).fetchone()["id"]
        material = conn.execute(
            "INSERT INTO material (lecture_id, post_id, owner_id, video_id, link_type, title, url) "
            "VALUES (%s, 30861, -1, 391, 'attachment', 'Новый перечень веществ 2909-р. Лекция от 05.04.2024 г', "
            "'https://vkvideo.ru/video-1_391') RETURNING id", (lecture,)).fetchone()["id"]
        conn.execute("INSERT INTO material_policy (owner_id, video_id, audience) VALUES (-1, 391, 'club')")
        version = conn.execute(
            "INSERT INTO material_version (material_id, content_hash, processor_version, embedding_model, "
            "card_state, track_state) VALUES (%s, 'h', 'subtitles-2', 'm', 'ready', 'ready') RETURNING id",
            (material,)).fetchone()["id"]
        conn.execute("UPDATE material SET published_version_id = %s WHERE id = %s", (version, material))
        for start in (0, 1_563_860):
            conn.execute(
                "INSERT INTO episode (version_id, start_ms, end_ms, text, embedding) "
                "VALUES (%s, %s, %s, 'взвешенные вещества', array_fill(0.1::real, ARRAY[256])::vector)",
                (version, start, start + 26_060))
        conn.execute("INSERT INTO index_job (owner_id, video_id, post_id, state) VALUES (-1, 391, 30861, 'done')")

        # Миграция 003 (функция 003) применяется следом и каталог не меняет.
        assert migrate(conn, MIGRATIONS_DIR) == ["002_documents.sql", "003_autonomy.sql", "004_miniapp.sql", "005_asr.sql"]

        row = conn.execute(
            "SELECT m.item_type, m.item_id, m.kind, m.doc_numbers, m.published_version_id, v.text_state "
            "FROM material m JOIN material_version v ON v.id = m.published_version_id").fetchone()
        assert (row["item_type"], row["item_id"], row["kind"], row["text_state"]) == ("video", 391, "video", "ready")
        assert row["doc_numbers"] == ["2909-р"] and row["published_version_id"] == version
        fragments = conn.execute("SELECT kind, start_ms, end_ms, page_no FROM fragment ORDER BY start_ms").fetchall()
        assert [(f["kind"], f["start_ms"], f["end_ms"], f["page_no"]) for f in fragments] == [
            ("episode", 0, 26_060, None), ("episode", 1_563_860, 1_589_920, None)]
        link = conn.execute("SELECT lecture_id, material_id, post_id, link_type, paid_feed FROM material_link").fetchone()
        assert (link["lecture_id"], link["material_id"], link["post_id"], link["link_type"], link["paid_feed"]) == (
            lecture, material, 30861, "attachment", None)   # лента ещё не проверена
        policy = conn.execute("SELECT item_type, item_id, audience, seen_signal FROM material_policy").fetchone()
        assert (policy["item_type"], policy["item_id"], policy["audience"], policy["seen_signal"]) == (
            "video", 391, "club", None)
        job = conn.execute("SELECT item_type, item_id, state FROM index_job").fetchone()
        assert (job["item_type"], job["item_id"], job["state"]) == ("video", 391, "done")

        assert migrate(conn, MIGRATIONS_DIR) == []          # повторный запуск ничего не меняет
        assert conn.execute("SELECT count(*) AS n FROM fragment").fetchone()["n"] == 2
    finally:
        conn.execute("SET search_path TO public")
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_fragment_place_is_checked_by_kind(loaded):
    import psycopg
    import pytest

    version = loaded.owner.execute("SELECT id FROM material_version LIMIT 1").fetchone()["id"]
    vector = "array_fill(0.1::real, ARRAY[256])::vector"
    bad = [
        f"INSERT INTO fragment (version_id, kind, page_no, part_no, text, embedding) VALUES (%s, 'page', 0, 1, 'x', {vector})",
        f"INSERT INTO fragment (version_id, kind, page_no, part_no, start_ms, end_ms, text, embedding) "
        f"VALUES (%s, 'page', 1, 1, 0, 10, 'x', {vector})",
        f"INSERT INTO fragment (version_id, kind, start_ms, end_ms, page_no, text, embedding) "
        f"VALUES (%s, 'episode', 0, 10, 1, 'x', {vector})",
        f"INSERT INTO fragment (version_id, kind, text, embedding) VALUES (%s, 'letter_part', 'x', {vector})",
    ]
    for statement in bad:
        with pytest.raises(psycopg.errors.CheckViolation):
            loaded.owner.execute(statement, (version,))
