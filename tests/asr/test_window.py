"""Окно распознавания (data-model «Окно открыто, если»): без БД."""
from datetime import datetime, time, timedelta, timezone

import pytest

from ecoprofi.asr import window
from ecoprofi.duetime import MOSCOW


def sched(**changes):
    base = {"weekdays": [7], "start_at": time(1, 0), "window_length": timedelta(hours=8), "catch_up": False,
            "paused": False, "initial_pass": "done", "manual_requested_at": None, "manual_only": None,
            "last_due_at": None}
    base.update(changes)
    return base


def msk(*args):
    return datetime(*args, tzinfo=MOSCOW)


SUNDAY = msk(2026, 11, 29, 3, 0)
MONDAY = msk(2026, 11, 30, 3, 0)


def test_initial_pass_opens_without_window():
    win = window.is_open(sched(initial_pass="pending"), MONDAY)
    assert win.kind == "initial" and win.until is None
    assert window.is_open(sched(initial_pass="running"), MONDAY).kind == "initial"


def test_scheduled_window_sunday_only():
    win = window.is_open(sched(), SUNDAY)
    assert win.kind == "scheduled"
    assert win.due == msk(2026, 11, 29, 1, 0) and win.until == msk(2026, 11, 29, 9, 0)
    assert window.is_open(sched(), MONDAY) is None
    assert window.is_open(sched(), msk(2026, 11, 29, 9, 0)) is None      # окно 01:00–09:00


def test_pause_closes_everything_including_initial_pass():
    assert window.is_open(sched(paused=True), SUNDAY) is None
    assert window.is_open(sched(paused=True, initial_pass="pending"), MONDAY) is None
    assert window.is_open(sched(paused=True, manual_requested_at=MONDAY), MONDAY) is None


def test_run_now_by_list_ignores_pause_and_initial_pass():
    win = window.is_open(sched(paused=True, initial_pass="pending", manual_only=["-1_2"]), MONDAY)
    assert win.kind == "manual" and win.only == ("-1_2",) and win.until == MONDAY + timedelta(hours=8)


def test_manual_run():
    win = window.is_open(sched(manual_requested_at=MONDAY), MONDAY)
    assert win.kind == "manual" and win.only is None


def test_last_due_prevents_second_run_in_same_window():
    due = msk(2026, 11, 29, 1, 0)
    assert window.is_open(sched(last_due_at=due), SUNDAY) is None
    # ПК перезагрузили посреди окна: запуск закрыт worker_lost — окно открывается снова.
    assert window.is_open(sched(last_due_at=due), SUNDAY, last_outcome="worker_lost").kind == "scheduled"
    assert window.is_open(sched(last_due_at=due), SUNDAY, last_outcome="queue_empty") is None


def test_catch_up_only_when_enabled():
    after = msk(2026, 11, 29, 12, 0)     # окно закончилось в 09:00
    assert window.is_open(sched(), after) is None
    win = window.is_open(sched(catch_up=True), after)
    assert win.kind == "catch_up" and win.due == msk(2026, 11, 29, 1, 0)
    assert window.is_open(sched(catch_up=True, last_due_at=msk(2026, 11, 29, 1, 0)), after) is None
    assert window.is_open(sched(catch_up=True), msk(2026, 11, 30, 10, 0)) is None   # больше суток


def test_next_due_at_from_thursday():
    now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
    assert window.next_due_at(sched(), now) == msk(2026, 11, 29, 1, 0)
    assert window.next_due_at(sched(), now).isoformat() == "2026-11-29T01:00:00+03:00"


def test_parsers():
    assert window.parse_window("8h") == timedelta(hours=8)
    assert window.parse_window("1h30m") == timedelta(minutes=90)
    assert window.parse_days("6,7") == [6, 7]
    assert window.parse_at("01:00") == time(1, 0)
    for bad in ("10m", "25h", "x"):
        with pytest.raises(ValueError):
            window.parse_window(bad)
    for bad in ("", "0", "8", "a"):
        with pytest.raises(ValueError):
            window.parse_days(bad)
    with pytest.raises(ValueError):
        window.parse_at("25:00")
