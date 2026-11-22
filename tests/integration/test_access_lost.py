"""Доступ оператора во время прохода (T013): продление, отзыв, параллельное продление, перезапуск."""
import threading

from ecoprofi.db import connect
from ecoprofi.indexer.access import Access, AccessStore


def access_row(stand) -> dict:
    return stand.owner.execute("SELECT * FROM operator_access").fetchone()


def test_long_run_refreshes_access_without_operator(stand, tmp_path):
    stand.use_run_corpus(12)
    access = stand.login(tmp_path)
    stand.transport.on_call = lambda method: stand.clock.sleep(900)   # проход дольше часа
    result = stand.run(access=access)
    assert result["state"] == "done"
    assert stand.vkid.refreshes >= 1 and stand.vkid.grants.count("authorization_code") == 1
    row = access_row(stand)
    assert row["state"] == "active" and row["refreshed_at"] and row["refresh_verified_at"]


def test_revoked_access_stops_run_and_relogin_resumes(stand, tmp_path):
    stand.use_run_corpus(12)
    access = stand.login(tmp_path)

    def revoke(portion, *rest):
        if portion == 1:
            stand.vkid.revoke()

    result = stand.run(access=access, report=revoke)
    assert (result["state"], result["stop_reason"]) == ("stopped", "auth")
    assert stand.catalog()["versions"] == 10
    row = access_row(stand)
    assert row["state"] == "login_required" and "продление" in row["last_denial"]
    assert access.store.load() is None and "access" in stand.alerts()
    assert row["relogin_verified_at"] is None

    access = stand.login(tmp_path)
    row = access_row(stand)
    assert row["state"] == "active" and row["relogin_verified_at"] is not None
    assert "access" not in stand.alerts()
    resumed = stand.run(access=access)
    assert (resumed["id"], resumed["state"]) == (result["id"], "done") and stand.catalog()["versions"] == 24


def test_concurrent_processes_refresh_once(stand, tmp_path):
    first = stand.login(tmp_path)
    second = Access(AccessStore(tmp_path / "access.json"), lambda: connect(stand.indexer_dsn, vector=False),
                    transport=stand.vkid, clock=stand.clock)
    stand.clock.t += 3600
    tokens, barrier = [], threading.Barrier(2)

    def take(access):
        barrier.wait()
        tokens.append(access.token())

    threads = [threading.Thread(target=take, args=(a,)) for a in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert stand.vkid.refreshes == 1 and len(set(tokens)) == 1


def test_saved_access_survives_restart(stand, tmp_path):
    access = stand.login(tmp_path)
    stand.clock.t += 3600
    token = access.token()
    restarted = AccessStore(tmp_path / "access.json")
    assert restarted.load()["access_token"] == token


def test_forgotten_access_stops_reading(stand, tmp_path):
    stand.use_run_corpus(2)
    access = stand.login(tmp_path)
    access.forget()
    assert access_row(stand)["state"] == "absent"
    result = stand.run(access=access)
    assert (result["state"], result["stop_reason"]) == ("stopped", "auth")
    assert stand.transport.calls == []
