"""Миграция 003 (T006): значения по умолчанию, ограничения, права, повторный запуск."""
import psycopg
import pytest

from ecoprofi.bot.migrate import migrate
from ecoprofi.db import connect


def test_migration_003_defaults_and_constraints(stand):
    owner = stand.owner
    assert migrate(owner) == []
    access = owner.execute("SELECT * FROM operator_access").fetchall()
    assert len(access) == 1 and access[0]["state"] == "absent"
    schedule = owner.execute("SELECT * FROM run_schedule").fetchone()
    assert (str(schedule["morning"]), str(schedule["evening"]), schedule["enabled"]) == ("07:00:00", "19:00:00", False)
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("UPDATE run_schedule SET evening = '09:00'")
    owner.execute("INSERT INTO alert (cause_key, body) VALUES ('access', 'a')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute("INSERT INTO alert (cause_key, body) VALUES ('access', 'b')")
    owner.execute("INSERT INTO index_run (kind, state) VALUES ('full', 'running')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute("INSERT INTO index_run (kind, state) VALUES ('manual', 'stopped')")
    owner.execute("UPDATE index_run SET state = 'done'")
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("INSERT INTO index_run (kind, state, stop_reason) VALUES ('full', 'stopped', 'other')")
    owner.execute("INSERT INTO index_run (kind, state, stop_reason, paid_feed_count) "
                  "VALUES ('full', 'stopped', 'paid_feed', 0)")
    assert owner.execute("SELECT failed_runs, waiting_source FROM index_job LIMIT 1").fetchone() is None
    with connect(stand.bot_dsn, vector=False) as bot:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            bot.execute("UPDATE operator_access SET state = 'active'")
