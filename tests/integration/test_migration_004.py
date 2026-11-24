"""T011: добавочная схема Mini App, каскады, ограничения и права ролей."""
import uuid

import psycopg
import pytest

from ecoprofi.bot.migrate import migrate
from ecoprofi.db import connect

SESSION = uuid.uuid4()
DIALOG = uuid.uuid4()


def _session(owner, *, session=SESSION, dialog=DIALOG):
    owner.execute(
        "INSERT INTO miniapp_session (id, token_hash, app_id, user_id, launch_digest, launch_ts, created_at, expires_at) "
        "VALUES (%s, %s, 54916430, 12345, %s, now(), now(), now() + interval '8 hours')",
        (session, b"token", b"launch"))
    owner.execute(
        "INSERT INTO miniapp_dialog (id, session_id, created_at) VALUES (%s, %s, now())", (dialog, session))
    owner.execute("UPDATE miniapp_session SET current_dialog_id = %s WHERE id = %s", (dialog, session))


def test_migration_004_idempotent_catalog_constraints_and_roles(stand):
    owner = stand.owner
    before = stand.catalog()
    assert migrate(owner) == []
    assert stand.catalog() == before
    _session(owner)
    assert owner.execute("SELECT current_dialog_id FROM miniapp_session WHERE id = %s", (SESSION,)).fetchone()
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute("INSERT INTO miniapp_dialog (id, session_id, created_at) VALUES (%s, %s, now())", (uuid.uuid4(), SESSION))
    request_id = uuid.uuid4()
    owner.execute(
        "INSERT INTO miniapp_request (dialog_id, request_id, sequence, original_text, text_digest, state, operation_id, created_at) "
        "VALUES (%s, %s, 1, 'вопрос', %s, 'queued', 'op', now())", (DIALOG, request_id, b"digest"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        owner.execute(
            "INSERT INTO miniapp_request (dialog_id, request_id, sequence, original_text, text_digest, state, operation_id, created_at) "
            "VALUES (%s, %s, 2, 'другой', %s, 'running', 'op2', now())", (DIALOG, uuid.uuid4(), b"other"))
    for text in ('', '   ', 'x' * 2001):
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "INSERT INTO miniapp_request (dialog_id, request_id, sequence, original_text, text_digest, state, operation_id, created_at) "
                "VALUES (%s, %s, 2, %s, %s, 'queued', 'bad', now())", (DIALOG, uuid.uuid4(), text, b"bad"))
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute("UPDATE miniapp_request SET outcome = 'found' WHERE dialog_id = %s", (DIALOG,))
    owner.execute("DELETE FROM miniapp_session WHERE id = %s", (SESSION,))
    assert owner.execute("SELECT count(*) AS n FROM miniapp_dialog").fetchone()["n"] == 0
    assert owner.execute("SELECT count(*) AS n FROM miniapp_request").fetchone()["n"] == 0


def test_migration_004_role_isolation(stand):
    with connect(stand.bot_dsn, vector=False) as bot:
        bot.execute("SELECT count(*) FROM miniapp_session").fetchone()
    with connect(stand.indexer_dsn, vector=False) as indexer:
        for operation in ("SELECT count(*) FROM miniapp_session", "DELETE FROM miniapp_dialog", "SELECT count(*) FROM miniapp_request"):
            with indexer.transaction():
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    indexer.execute(operation)
