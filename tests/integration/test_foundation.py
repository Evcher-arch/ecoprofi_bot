"""Фаза 2: миграции, импорт политики, раздельные права ролей БД."""
import psycopg
import pytest

from ecoprofi.bot.access import Forbidden, resolve_access
from ecoprofi.bot.import_policy import PolicyError, apply_policy, load_policy, parse_policy
from ecoprofi.bot.migrate import migrate
from ecoprofi.db import connect


def test_repeated_migration_keeps_data(loaded):
    before = loaded.counts()
    assert migrate(loaded.owner) == []
    assert loaded.counts() == before


def test_schema_rejects_invalid_coordinates_and_roles(loaded):
    version = loaded.owner.execute("SELECT id FROM material_version LIMIT 1").fetchone()["id"]
    vector = "[" + ",".join(["0.1"] * 256) + "]"
    for start, end in ((-1, 5), (5, 5), (9, 3)):
        with pytest.raises(psycopg.errors.CheckViolation):
            loaded.owner.execute("INSERT INTO fragment (version_id, start_ms, end_ms, text, embedding) "
                                 "VALUES (%s, %s, %s, 'x', %s::vector)", (version, start, end, vector))
    with pytest.raises(psycopg.errors.CheckViolation):
        loaded.owner.execute("INSERT INTO participant (group_id, vk_id, tester, role) VALUES (%s, 1, true, 'admin')",
                             (loaded.group_id,))


def test_component_roles_are_separated(loaded):
    with connect(loaded.bot_dsn) as bot:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            bot.execute("UPDATE material SET state = 'deleted'")
    with connect(loaded.bot_dsn) as bot:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            bot.execute("UPDATE participant SET role = 'club'")
    with connect(loaded.indexer_dsn) as indexer:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            indexer.execute("SELECT * FROM participant")
    with connect(loaded.indexer_dsn) as indexer:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            indexer.execute("UPDATE material_policy SET audience = 'public'")


def test_policy_import_is_atomic_and_replaces_previous(loaded):
    club = loaded.users["club"]
    loaded.import_policy(materials={})
    assert loaded.owner.execute("SELECT count(*) AS n FROM material_policy").fetchone()["n"] == 0
    assert resolve_access(loaded.owner, loaded.group_id, club, club, loaded.club).role == "club"
    apply_policy(loaded.owner, parse_policy(dict(loaded.policy(), group_id=loaded.group_id + 1)))
    with pytest.raises(Forbidden):                     # прежнее сообщество отключено
        resolve_access(loaded.owner, loaded.group_id, club, club, loaded.club)


def test_old_policy_with_testers_section_is_imported_without_them(loaded):
    policy = parse_policy(dict(loaded.policy(), testers={"abc": "admin"}))
    assert policy["ignored_testers"] and "testers" not in policy
    apply_policy(loaded.owner, policy)
    assert loaded.owner.execute("SELECT count(*) AS n FROM participant").fetchone()["n"] == 0


@pytest.mark.parametrize("broken", [
    {"group_id": 0, "materials": {}},
    {"group_id": 1, "materials": {"111": "club"}},
    {"group_id": 1, "materials": {"-1_2": "secret"}},
])
def test_broken_policy_is_rejected_before_any_change(broken):
    with pytest.raises(PolicyError):
        parse_policy(broken)


@pytest.mark.parametrize("text", [
    '{"group_id": 1, "group_id": 2, "materials": {}}',
    '{"group_id": 1, "materials": {"-1_2": "club", "-1_2": "public"}}',
])
def test_duplicate_policy_key_is_rejected(text):
    with pytest.raises(PolicyError):
        load_policy(text)


def test_access_rules(loaded):
    club = loaded.users["club"]
    assert resolve_access(loaded.owner, loaded.group_id, club, club, loaded.club).audiences == ("public", "club")
    for group, peer, user in (
        (loaded.group_id + 1, club, club),          # чужая группа
        (loaded.group_id, 2000000001, club),        # беседа
        (loaded.group_id, loaded.outsider, loaded.outsider),               # без подписки
        (loaded.group_id, loaded.users["public"], loaded.users["public"]),  # без подписки
        (loaded.group_id, -5, -5),                  # не пользователь
    ):
        with pytest.raises(Forbidden):
            resolve_access(loaded.owner, group, peer, user, loaded.club)
