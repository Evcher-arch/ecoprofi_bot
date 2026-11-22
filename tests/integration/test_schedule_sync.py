"""Перечень лекций из расписания (T012, T016): повтор без дублей, осиротевшие строки,
перепривязка материалов 001, выборка пилота."""
import pytest

from ecoprofi.bot.import_policy import PolicyError, apply_policy, parse_policy
from ecoprofi.db import connect
from ecoprofi.indexer.schedule import parse_schedule, sync_schedule
from tests.fixtures import docs_corpus as corpus
from tests.integration.conftest import OWNER_ID


def lecture(stand, key):
    return stand.owner.execute("SELECT * FROM lecture WHERE schedule_row = %s", (key,)).fetchone()


def totals(stand):
    return stand.owner.execute(
        "SELECT (SELECT count(*) FROM lecture) AS lectures, (SELECT count(*) FROM lecture_post) AS posts, "
        "(SELECT count(*) FROM material_link) AS links").fetchone()


def test_first_sync_creates_rows_and_repeat_changes_nothing(stand):
    first = stand.sync()
    assert (first["total"], first["with_materials"], first["planned"], first["new"]) == (6, 4, 2, 6)
    before = totals(stand)
    second = stand.sync()
    assert (second["new"], second["new_links"], second["orphaned"]) == (0, 0, 0)
    assert totals(stand) == before


def test_row_stores_date_section_state_and_club_mark(stand):
    stand.sync()
    row = lecture(stand, corpus.ROW_FEES)
    assert (str(row["lecture_date"]), row["section"], row["state"]) == ("2025-03-05", "Раздел 1. Основы", "has_materials")
    posts = {p["post_id"]: p for p in stand.owner.execute(
        "SELECT post_id, row_role, schedule_club FROM lecture_post WHERE lecture_id = %s", (row["id"],)).fetchall()}
    assert (posts[9101]["row_role"], posts[9101]["schedule_club"]) == ("lecture", False)
    assert (posts[9102]["row_role"], posts[9102]["schedule_club"]) == ("review", True)


def test_planned_row_is_kept_without_publications(stand):
    stand.sync()
    row = lecture(stand, corpus.ROW_PLANNED)
    assert row["state"] == "planned"
    assert stand.owner.execute("SELECT count(*) AS n FROM lecture_post WHERE lecture_id = %s",
                               (row["id"],)).fetchone()["n"] == 0


def test_row_missing_twice_becomes_orphaned_and_returns(stand):
    stand.sync()
    gone = {"without": ("Лекция 2. Нормативы выбросов",)}
    assert stand.sync(**gone)["orphaned"] == 0                      # один пропуск ничего не значит
    assert lecture(stand, corpus.ROW_LIMITS)["state"] == "has_materials"
    assert stand.sync(**gone)["orphaned"] == 1
    assert lecture(stand, corpus.ROW_LIMITS)["state"] == "orphaned"
    stand.sync()
    row = lecture(stand, corpus.ROW_LIMITS)
    assert (row["state"], row["missed"]) == ("has_materials", 0)


def test_link_removed_from_row_is_removed_from_catalog(stand):
    stand.sync()
    html = corpus.schedule_html().replace(f"wall{OWNER_ID}_9102", "wall-1_1")   # ссылка стала чужой
    with connect(stand.indexer_dsn) as conn:
        sync_schedule(conn, parse_schedule(html, community_id=OWNER_ID))
    row = lecture(stand, corpus.ROW_FEES)
    posts = [p["post_id"] for p in stand.owner.execute(
        "SELECT post_id FROM lecture_post WHERE lecture_id = %s", (row["id"],)).fetchall()]
    assert posts == [9101]


def test_materials_of_001_are_moved_to_the_real_row(loaded):
    # Запись razbor_club опубликована в 001 по условной строке; расписание называет её публикацию.
    html = ("<table>" + corpus._row("05.04.2024", f"<p>{corpus._wall(9001, 'Разбор 2909-р')}</p>") + "</table>")
    with connect(loaded.indexer_dsn) as conn:
        counts = sync_schedule(conn, parse_schedule(html, community_id=OWNER_ID))
    assert counts["relinked"] == 1
    row = loaded.owner.execute(
        "SELECT l.schedule_row, l.lecture_date FROM material_link ml JOIN lecture l ON l.id = ml.lecture_id "
        "WHERE ml.material_id = %s", (loaded.material_id("razbor_club"),)).fetchall()
    assert [(r["schedule_row"], str(r["lecture_date"])) for r in row] == [("2024-04-05 разбор 2909-р", "2024-04-05")]
    # Решение редактора «клуб» принято до расписания и строже него: аудитория не меняется молча.
    assert "razbor_club" in loaded.keys(loaded.ask("club", "взвешенные вещества"))
    assert "razbor_club" not in loaded.keys(loaded.ask("public", "взвешенные вещества"))
    card = loaded.ask("club", "взвешенные вещества")["cards"][0]
    assert card["lecture"] == {"title": "Разбор 2909-р", "date": "2024-04-05"}


def test_pilot_sample_is_imported_and_unknown_key_changes_nothing(stand):
    stand.sync()
    policy = dict(stand.policy(materials={}), version=2, lectures=[corpus.ROW_FEES, corpus.ROW_LIMITS])
    counts = apply_policy(stand.owner, parse_policy(policy))
    assert counts["lectures"] == 2
    rows = stand.owner.execute("SELECT schedule_row FROM pilot_lecture ORDER BY 1").fetchall()
    assert [r["schedule_row"] for r in rows] == [corpus.ROW_FEES, corpus.ROW_LIMITS]

    bad = dict(policy, lectures=["2030-01-01 нет такой строки"], testers={})
    with pytest.raises(PolicyError):
        apply_policy(stand.owner, parse_policy(bad))
    assert stand.owner.execute("SELECT count(*) AS n FROM pilot_lecture").fetchone()["n"] == 2
    assert stand.owner.execute("SELECT count(*) AS n FROM participant").fetchone()["n"] == 3   # откат целиком


def test_policy_without_lectures_section_keeps_the_sample(stand):
    stand.sync()
    apply_policy(stand.owner, parse_policy(dict(stand.policy(materials={}), version=2, lectures=[corpus.ROW_FEES])))
    apply_policy(stand.owner, parse_policy(stand.policy(materials={})))           # файл версии 1
    assert stand.owner.execute("SELECT count(*) AS n FROM pilot_lecture").fetchone()["n"] == 1


def test_policy_keys_of_both_versions_are_accepted_and_duplicates_rejected():
    policy = parse_policy({"group_id": 1, "version": 2, "materials": {
        "-95098598_456240391": "club", "video-95098598_456240761": "club", "doc177068782_686984961": "public"}})
    assert policy["materials"] == {("video", -95098598, 456240391): "club", ("video", -95098598, 456240761): "club",
                                   ("doc", 177068782, 686984961): "public"}
    with pytest.raises(PolicyError):      # один материал двумя написаниями ключа
        parse_policy({"group_id": 1, "materials": {"-1_2": "club", "video-1_2": "public"}})
    with pytest.raises(PolicyError):
        parse_policy({"group_id": 1, "materials": {"post-1_2": "club"}})
    with pytest.raises(PolicyError):
        parse_policy({"group_id": 1, "version": 3})
