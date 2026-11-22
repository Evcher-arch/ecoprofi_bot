"""Сроки автоматических запусков (T029): не чаще двух раз в сутки, утром и вечером."""
from datetime import datetime, time, timedelta

import pytest

from ecoprofi.duetime import MOSCOW, ScheduleError, is_due, next_due, parse_slots

MORNING, EVENING = time(7, 0), time(19, 0)


def msk(day, hour, minute=0):
    return datetime(2026, 12, day, hour, minute, tzinfo=MOSCOW)


def test_slot_is_due_once():
    assert is_due(msk(1, 6, 59), MORNING, EVENING, None) is None
    due = is_due(msk(1, 7, 0), MORNING, EVENING, None)
    assert due == msk(1, 7)
    assert is_due(msk(1, 7, 1), MORNING, EVENING, due) is None
    assert is_due(msk(1, 19, 0), MORNING, EVENING, due) == msk(1, 19)


def test_missed_days_give_one_catch_up_and_next_slot_is_morning():
    last = msk(1, 19)
    # Стенд включён через двое суток в 21:00: догоняется только сегодняшний вечерний срок.
    due = is_due(msk(3, 21), MORNING, EVENING, last)
    assert due == msk(3, 19)
    assert is_due(msk(3, 23), MORNING, EVENING, due) is None
    assert next_due(msk(3, 21), MORNING, EVENING) == msk(4, 7)


def test_no_more_than_two_runs_per_day_including_catch_up():
    last, runs = None, []
    moment = msk(5, 0)
    while moment < msk(6, 0):
        due = is_due(moment, MORNING, EVENING, last)
        if due:
            runs.append(moment)
            last = due
        moment += timedelta(minutes=13)
    assert len(runs) == 2


def test_single_slot_per_day():
    assert is_due(msk(1, 20), MORNING, None, msk(1, 7)) is None
    assert next_due(msk(1, 8), MORNING, None) == msk(2, 7)


def test_slot_validation():
    assert parse_slots(["19:00", "07:00"]) == (MORNING, EVENING)
    assert parse_slots(["08:30"]) == (time(8, 30), None)
    with pytest.raises(ScheduleError, match="одно или два"):
        parse_slots(["07:00", "12:00", "19:00"])
    with pytest.raises(ScheduleError, match="шести часов"):
        parse_slots(["07:00", "12:00"])
    with pytest.raises(ScheduleError, match="ЧЧ:ММ"):
        parse_slots(["7 утра"])
