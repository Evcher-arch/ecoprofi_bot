"""Сводка состояния (T035)."""
from ecoprofi.db import connect
from ecoprofi.indexer.status import build_status


def status(stand) -> str:
    with connect(stand.indexer_dsn, vector=False) as conn:
        return build_status(conn)


def test_status_during_and_after_run(stand):
    stand.use_run_corpus(12)
    during = []
    stand.run(report=lambda portion, *rest: during.append(status(stand)) if portion == 1 else None)
    assert "идёт" in during[0] and "лекций пройдено 5 из 12, осталось 7, порция 2" in during[0]
    text = status(stand)
    assert "завершён" in text and "возраст индекса" in text
    assert "Материалы (24): опубликован — 24" in text
    assert "не доставлено" in text                     # итог полного прохода ещё не отправлен
    assert "Доступ: не сохранён" in text and "Расписание запусков: 07:00 и 19:00, выключено" in text
    for secret in ("fixture.invalid", "hash=", "fixture-token"):
        assert secret not in text


def test_status_lists_permanent_failures_and_missing_posts(stand):
    stand.use_run_corpus(2)
    stand.run()
    stand.owner.execute("UPDATE index_job SET state = 'permanent', failure_reason = 'timeout' "
                        "WHERE item_id = (SELECT min(item_id) FROM index_job)")
    stand.owner.execute("UPDATE lecture_post SET missed = 2 WHERE post_id = 9502")
    text = status(stand)
    assert "Постоянный сбой: doc" in text and "9502 (пропусков 2)" in text
    assert "Платная лента: записей 40 при последней проверке, 40 в последнем успешном проходе" in text
