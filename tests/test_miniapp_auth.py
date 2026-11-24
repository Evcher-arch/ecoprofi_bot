"""T017: строгий запуск, дубли, кодировка, app/user/ts и границы."""
from datetime import datetime, timezone

import pytest

from ecoprofi.bot.miniapp.auth import LaunchError, verify_launch
from tests.fixtures.miniapp import launch_query

NOW = datetime.fromtimestamp(1_700_000_000, timezone.utc)


def test_signed_launch_and_tampering():
    query = launch_query()
    launch = verify_launch(query, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)
    assert (launch.app_id, launch.user_id) == (54916430, 12345)
    for changed in (query.replace("12345", "99999"), query.replace("54916430", "42"),
                    query.replace("1700000000", "1700000001"), query[:-1] + "A"):
        with pytest.raises(LaunchError):
            verify_launch(changed, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)


@pytest.mark.parametrize("bad", ["", "?vk_app_id=1", "https://vk.ru/app1", "vk_app_id=1",
                                 "vk_app_id=1&vk_app_id=2", "vk_app_id=%FF", "vk_app_id=%",
                                 "vk_app_id=1&&vk_user_id=2", "vk_app_id=1&vk_user_id=-1&vk_ts=1&sign=bad"])
def test_reject_bad_query(bad):
    with pytest.raises(LaunchError):
        verify_launch(bad, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)


def test_unknown_vk_and_encoding_are_signed():
    query = launch_query(extra=(("vk_custom", "a b"), ("vk_empty", ""), ("vk_plus", "a+b")))
    launch = verify_launch(query, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)
    assert launch.digest


def test_time_boundaries_and_query_size():
    for offset in (-300, 60):
        query = launch_query(ts=1_700_000_000 + offset)
        assert verify_launch(query, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)
    for offset in (-301, 61):
        query = launch_query(ts=1_700_000_000 + offset)
        with pytest.raises(LaunchError) as expired:
            verify_launch(query, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)
        assert expired.value.code == "launch_expired"
    with pytest.raises(LaunchError):
        verify_launch("x" * 16385, secret="miniapp-test-secret", expected_app_id=54916430, now=NOW)


def test_client_cannot_choose_app_or_role():
    query = launch_query(extra=(("vk_viewer_group_role", "admin"),))
    with pytest.raises(LaunchError):
        verify_launch(query, secret="miniapp-test-secret", expected_app_id=42, now=NOW)
