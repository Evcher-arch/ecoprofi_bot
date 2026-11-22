"""Автоматические запуски (T029, T032): включение после проверки доступа, один запуск на срок."""
from datetime import datetime

import pytest

from ecoprofi.db import connect
from ecoprofi.duetime import MOSCOW, ScheduleError
from ecoprofi.indexer import daemon


def conn_of(stand):
    return connect(stand.indexer_dsn, vector=False)


def enable(stand, *, access="active"):
    stand.owner.execute("UPDATE run_schedule SET enabled = true, last_due_at = %s",
                        (datetime(2026, 12, 1, 6, 0, tzinfo=MOSCOW),))
    stand.owner.execute("UPDATE operator_access SET state = %s", (access,))


def test_schedule_on_requires_observed_refresh_and_relogin(stand):
    with conn_of(stand) as conn:
        with pytest.raises(ScheduleError, match="продление.*и вход"):
            daemon.schedule_on(conn)
        stand.owner.execute("UPDATE operator_access SET refresh_verified_at = now()")
        with pytest.raises(ScheduleError, match="вход после"):
            daemon.schedule_on(conn)
        stand.owner.execute("UPDATE operator_access SET relogin_verified_at = now()")
        daemon.schedule_on(conn)
    assert stand.owner.execute("SELECT enabled FROM run_schedule").fetchone()["enabled"] is True


def test_schedule_set_allows_at_most_two_distant_slots(stand):
    with conn_of(stand) as conn:
        for bad in (["07:00", "12:00", "19:00"], ["07:00", "12:00"]):
            with pytest.raises(ScheduleError):
                daemon.schedule_set(conn, bad)
        daemon.schedule_set(conn, ["08:00", "20:30"])
    row = stand.owner.execute("SELECT morning, evening FROM run_schedule").fetchone()
    assert (str(row["morning"]), str(row["evening"])) == ("08:00:00", "20:30:00")


def test_slot_starts_one_update(stand):
    enable(stand)
    started = []
    start = lambda conn: started.append(1) or {"state": "done"}  # noqa: E731
    now = datetime(2026, 12, 1, 7, 0, 30, tzinfo=MOSCOW)
    with conn_of(stand) as conn:
        assert daemon.step(conn, start, now) == "done"
        assert daemon.step(conn, start, now.replace(minute=5)) is None
        assert daemon.step(conn, start, now.replace(hour=19, minute=1)) == "done"
        assert daemon.step(conn, start, now.replace(hour=23)) is None
    assert len(started) == 2


def test_busy_slot_is_recorded_as_skipped(stand):
    enable(stand)
    with conn_of(stand) as conn:
        assert daemon.step(conn, lambda c: {"state": "busy"}, datetime(2026, 12, 1, 7, 1, tzinfo=MOSCOW)) == "skipped:busy"
    row = stand.owner.execute("SELECT kind, state, stop_reason FROM index_run").fetchone()
    assert (row["kind"], row["state"], row["stop_reason"]) == ("scheduled", "skipped", "busy")


def test_slot_without_access_opens_alert(stand):
    enable(stand, access="login_required")
    with conn_of(stand) as conn:
        outcome = daemon.step(conn, lambda c: pytest.fail("проход без доступа"),
                              datetime(2026, 12, 1, 7, 1, tzinfo=MOSCOW))
    assert outcome == "skipped:auth" and "access" in stand.alerts()


def test_disabled_schedule_does_nothing(stand):
    enable(stand)
    daemon.schedule_off(stand.owner)
    with conn_of(stand) as conn:
        assert daemon.step(conn, lambda c: pytest.fail("запуск"), datetime(2026, 12, 1, 7, 1, tzinfo=MOSCOW)) is None
