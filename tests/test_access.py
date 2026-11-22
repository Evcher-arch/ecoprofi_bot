"""Хранилище и продление доступа оператора (T008) без БД и сети."""
import logging
import os
import stat

import pytest

from ecoprofi.indexer.access import REFRESH_MARGIN, Access, AccessStore, LoginError, login_request
from ecoprofi.indexer.retry import SourceAuth, SourceTimeout
from tests.fixtures.fake_vkid import CODE, DEVICE, FakeClock, FakeVkId, callback


@pytest.fixture
def vkid():
    return FakeVkId()


@pytest.fixture
def clock():
    return FakeClock()


def logged_in(tmp_path, vkid, clock):
    access = Access(AccessStore(tmp_path / "access.json"), transport=vkid, clock=clock)
    request = login_request("123456")
    shown = access.complete_login(request, callback(request.state))
    return access, shown


def test_login_saves_file_readable_only_by_owner(tmp_path, vkid, clock):
    access, shown = logged_in(tmp_path, vkid, clock)
    mode = stat.S_IMODE(os.stat(tmp_path / "access.json").st_mode)
    assert mode == 0o600
    values = access.store.load()
    assert values["device_id"] == DEVICE and values["app"] == "123456"
    assert values["access_token"] in vkid.valid_access and values["refresh_token"] in vkid.valid_refresh
    # Состав ответа показывается без значений.
    assert shown["access_token"] == "есть" and shown["refresh_token"] == "есть" and shown["expires_in"] == 3600
    assert not any(v in str(shown) for v in vkid.issued)


def test_login_rejects_foreign_address_and_state(tmp_path, vkid, clock):
    access = Access(AccessStore(tmp_path / "access.json"), transport=vkid, clock=clock)
    request = login_request("123456")
    with pytest.raises(LoginError, match="state"):
        access.complete_login(request, callback("другой"))
    with pytest.raises(LoginError, match="localhost"):
        access.complete_login(request, callback(request.state).replace("localhost", "evil.invalid"))
    with pytest.raises(LoginError):
        login_request("")
    assert access.store.load() is None and vkid.grants == []


def test_login_without_refresh_token_is_rejected(tmp_path, clock):
    class NoRefresh(FakeVkId):
        def _issue(self):
            response = super()._issue()
            response.pop("refresh_token")
            return response

    access = Access(AccessStore(tmp_path / "access.json"), transport=NoRefresh(), clock=clock)
    request = login_request("1")
    with pytest.raises(LoginError, match="обновления"):
        access.complete_login(request, callback(request.state))
    assert access.store.load() is None


def test_token_is_refreshed_only_near_expiry(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    first = access.token()
    clock.t += 3600 - REFRESH_MARGIN - 60          # осталось 11 минут
    assert access.token() == first and vkid.refreshes == 0
    clock.t += 120                                  # осталось 9 минут
    second = access.token()
    assert second != first and vkid.refreshes == 1
    stored = access.store.load()
    # Новая пара записана до возврата токена; прежнего токена обновления в файле нет.
    assert stored["access_token"] == second and stored["refresh_token"] in vkid.valid_refresh
    assert stored["refresh_token"] != "fake-refresh-1-beef0001"
    assert access.token() == second and vkid.refreshes == 1


def test_failed_write_keeps_previous_file(tmp_path, vkid, clock, monkeypatch):
    access, _ = logged_in(tmp_path, vkid, clock)
    before = (tmp_path / "access.json").read_bytes()

    def broken(*args):
        raise OSError("диск заполнен")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        access.store.save(dict(access.store.load(), access_token="другое"))
    assert (tmp_path / "access.json").read_bytes() == before


def test_rejected_refresh_forgets_access(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    vkid.revoke()
    clock.t += 3600
    with pytest.raises(SourceAuth, match="нужен вход"):
        access.token()
    assert access.store.load() is None and not (tmp_path / "access.json").exists()


def test_network_failure_during_refresh_keeps_access(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    before = access.store.load()
    vkid.mode = "network"
    clock.t += 3600
    with pytest.raises(SourceTimeout):
        access.token()
    assert access.store.load() == before


def test_refresh_without_new_refresh_token_keeps_previous(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    previous = access.store.load()["refresh_token"]
    vkid.mode = "no_refresh"
    clock.t += 3600
    access.token()
    assert access.store.load()["refresh_token"] == previous


def test_force_refresh_skips_when_token_already_replaced(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    old = access.token()
    renewed = access.force_refresh(old)
    assert renewed != old and vkid.refreshes == 1
    assert access.force_refresh(old) == renewed and vkid.refreshes == 1   # уже продлено другим вызовом


def test_forget_removes_file(tmp_path, vkid, clock):
    access, _ = logged_in(tmp_path, vkid, clock)
    access.forget()
    assert access.store.load() is None
    with pytest.raises(SourceAuth):
        access.token()


def test_no_secret_reaches_the_log(tmp_path, vkid, clock, caplog):
    caplog.set_level(logging.DEBUG)
    access, _ = logged_in(tmp_path, vkid, clock)
    clock.t += 3600
    access.token()
    vkid.revoke()
    clock.t += 3600
    with pytest.raises(SourceAuth) as info:
        access.token()
    text = caplog.text + str(info.value)
    for secret in vkid.issued + [CODE, DEVICE]:
        assert secret not in text
