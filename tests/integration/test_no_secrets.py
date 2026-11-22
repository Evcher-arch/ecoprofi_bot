"""Значения доступа никуда не попадают (T036, FR-10, SC-04)."""
import logging

from ecoprofi.indexer.report import build_report
from ecoprofi.indexer.status import build_status
from tests.fixtures.fake_vkid import CODE, DEVICE
from tests.integration.conftest import SERVICE_KEY
from tests.integration.test_alerts_api import pending


def test_tokens_codes_and_file_addresses_leave_no_trace(stand, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    stand.use_run_corpus(6)
    access = stand.login(tmp_path)
    stand.transport.on_call = lambda method: stand.clock.sleep(1500)   # продление посреди прохода

    def revoke(portion, *rest):
        if portion == 1:
            stand.vkid.revoke()

    stand.run(access=access, portion=3, report=revoke)            # отказ и остановка
    access = stand.login(tmp_path)                                # повторный вход
    stand.clock.sleep(3600)                                       # рабочий токен истёк: успешное продление
    stand.run(access=access, portion=3)
    pending(stand)                                                # очередь оповещений и проверка устаревания
    from ecoprofi.bot.club import ClubUnavailable
    stand.import_policy()
    stand.club.failure = ClubUnavailable("VK отклонил проверку подписки на клуб (код 5).")
    assert stand.post(stand.users["club"], "перечень").status_code == 503   # сбой проверки: причина club
    stand.club.failure = None
    assert stand.post(stand.users["club"], "перечень").status_code == 200

    secrets = stand.vkid.issued + [CODE, DEVICE, "hash=secret", "fixture.invalid", SERVICE_KEY]
    assert stand.vkid.refreshes >= 2 and len(stand.vkid.issued) >= 6
    tables = [r["tablename"] for r in stand.owner.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()]
    dump = "\n".join(str(stand.owner.execute(f"SELECT * FROM {t}").fetchall()) for t in tables)
    places = {"БД": dump, "журнал": caplog.text, "отчёт": build_report(stand.owner),
              "сводка": build_status(stand.owner)}
    for place, text in places.items():
        for secret in secrets:
            assert secret not in text, f"{place}: найдено значение"
