"""Разбор расписания (T010): дата, раздел, ссылки, пометка «Клуб», ключ строки."""
from datetime import date

import pytest

from ecoprofi.indexer.schedule import ScheduleError, parse_schedule, row_key
from tests.fixtures import docs_corpus as corpus

ROWS = parse_schedule(corpus.schedule_html(), community_id=corpus.COMMUNITY)
BY_KEY = {row.key: row for row in ROWS}


def test_rows_have_date_section_and_title():
    assert len(ROWS) == 6
    row = BY_KEY[corpus.ROW_FEES]
    assert (row.when, row.section, row.title) == (date(2025, 3, 5), "Раздел 1. Основы",
                                                  "Лекция 1. Плата за негативное воздействие")
    assert BY_KEY[corpus.ROW_MIXED].section == "Раздел 2. Практика"


def test_header_and_section_rows_are_not_lectures():
    assert not any("Тема" in row.title or row.title.startswith("Раздел") for row in ROWS)


def test_club_mark_applies_to_the_marked_material_only():
    links = {link.post_id: link for link in BY_KEY[corpus.ROW_FEES].links}
    assert (links[9101].role, links[9101].club) == ("lecture", False)
    assert (links[9102].role, links[9102].club) == ("review", True)


def test_club_mark_next_to_lecture_link_marks_the_lecture():
    rows = parse_schedule(corpus.schedule_html(club_on_lecture_1=True), community_id=corpus.COMMUNITY)
    links = {link.post_id: link for link in rows[0].links}
    assert links[9101].club and links[9102].club
    assert rows[0].key == corpus.ROW_FEES             # пометка не меняет ключ строки


def test_row_without_link_is_kept_as_planned():
    row = BY_KEY[corpus.ROW_PLANNED]
    assert row.links == [] and row.state == "planned"
    assert BY_KEY[corpus.ROW_FEES].state == "has_materials"


def test_links_outside_community_wall_are_skipped_and_counted():
    row = BY_KEY[corpus.ROW_FEES]
    assert [link.post_id for link in row.links] == [9101, 9102]
    assert row.other_links == 1


def test_redirect_link_to_wall_post_is_recognised():
    assert [link.post_id for link in BY_KEY[corpus.ROW_MIXED].links] == [9106, 9103]


def test_two_rows_of_one_date_get_different_keys():
    same_day = [row.key for row in ROWS if row.when == date(2025, 3, 19)]
    assert same_day == [corpus.ROW_PLANNED, "2025-03-19 лекция 4. паспорта отходов"]


def test_key_ignores_case_spaces_quotes_and_trailing_dot():
    assert row_key(date(2025, 3, 19), "  Лекция 4.  «Паспорта»   отходов. ") == "2025-03-19 лекция 4. паспорта отходов"


def test_page_without_dated_rows_is_an_error_not_an_empty_list():
    with pytest.raises(ScheduleError):
        parse_schedule("<html><body><p>Страница недоступна</p></body></html>")
