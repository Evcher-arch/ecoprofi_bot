"""Очередь оповещений оператору (T023): одна причина — одно сообщение, напоминание, восстановление."""
import re
from datetime import datetime, timedelta

import pytest

from ecoprofi.db import connect
from ecoprofi.duetime import MOSCOW
from ecoprofi.indexer import alerts
from tests.integration.conftest import SERVICE_KEY

HEADERS = {"X-Service-Key": SERVICE_KEY}


def pending(stand) -> list:
    response = stand.client.get("/api/alerts/pending", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["alerts"]


def sent(stand, alert_id, send_no) -> bool:
    response = stand.client.post(f"/api/alerts/{alert_id}/sent", json={"send_no": send_no}, headers=HEADERS)
    assert response.status_code == 200
    return response.json()["changed"]


def indexer(stand):
    return connect(stand.indexer_dsn, vector=False)


def test_queue_requires_service_key(stand):
    assert stand.client.get("/api/alerts/pending").status_code == 401
    assert stand.client.post("/api/alerts/1/sent", json={"send_no": 1}).status_code == 401


def test_one_record_per_cause_reminder_and_recovery(stand):
    with indexer(stand) as conn:
        alerts.open_alert(conn, "access", "нужен вход 1")
        alerts.open_alert(conn, "access", "нужен вход 2")
    assert stand.owner.execute("SELECT count(*) AS n FROM alert").fetchone()["n"] == 1
    [message] = pending(stand)
    assert (message["send_no"], message["text"]) == (1, "нужен вход 2")
    assert sent(stand, message["id"], 1) and not sent(stand, message["id"], 1)
    assert pending(stand) == []

    stand.owner.execute("UPDATE alert SET last_sent_at = now() - interval '25 hours'")
    [reminder] = pending(stand)
    assert reminder["send_no"] == 2 and reminder["text"].startswith("Напоминание")
    assert sent(stand, reminder["id"], 2) and pending(stand) == []

    with indexer(stand) as conn:
        alerts.close_alert(conn, "access")
    [recovery] = pending(stand)
    assert recovery["send_no"] == 3 and recovery["text"].startswith("Восстановлено")
    assert sent(stand, recovery["id"], 3) and pending(stand) == []


def test_cause_closed_before_sending_needs_no_recovery(stand):
    with indexer(stand) as conn:
        alerts.open_alert(conn, "editor", "ждут решения")
        alerts.close_alert(conn, "editor")
    assert pending(stand) == []


def test_summary_is_sent_once(stand):
    with indexer(stand) as conn:
        alerts.info(conn, "run:7", "итог прохода")
    [message] = pending(stand)
    assert sent(stand, message["id"], 1)
    assert pending(stand) == [] and stand.alerts("closed")["run:7"]["recovery_sent"]


def test_stale_index_is_reported_by_bot(stand):
    stand.owner.execute("INSERT INTO index_run (kind, state, started_at, finished_at) "
                        "VALUES ('scheduled', 'done', now() - interval '51 hours', now() - interval '50 hours')")
    [message] = pending(stand)
    assert "Индекс не обновляется" in message["text"] and "50 ч" in message["text"]
    sent(stand, message["id"], 1)
    stand.owner.execute("INSERT INTO index_run (kind, state, finished_at) VALUES ('scheduled', 'done', now())")
    [recovery] = pending(stand)
    assert recovery["text"].startswith("Восстановлено")


def test_missed_slot_is_reported_by_bot(stand):
    now = datetime.now(MOSCOW)
    if now.hour == 0 and now.minute < 30:
        pytest.skip("срок «20 минут назад» попадает на прошлые сутки")
    slot = (now - timedelta(minutes=20)).time().replace(second=0, microsecond=0)
    stand.owner.execute("UPDATE run_schedule SET enabled = true, morning = %s, evening = NULL, last_due_at = NULL",
                        (slot,))
    [message] = pending(stand)
    assert "не началось" in message["text"]
    stand.owner.execute("UPDATE run_schedule SET last_due_at = now()")
    assert pending(stand) == [] and "stale" not in stand.alerts()


def test_alert_texts_name_action_without_addresses_or_secrets():
    texts = [alerts.text_access(), alerts.text_run_failed(1, "лента не прочитана (timeout)"),
             alerts.text_schedule_failed(1), alerts.text_permanent("doc1_2", 3, "timeout"),
             alerts.text_post_missing(3, 2), alerts.text_editor(4), alerts.text_ai_limit(1),
             alerts.text_stale(50), alerts.text_stale(None), alerts.text_paid_feed(0, 1039),
             alerts.text_paid_feed(None, None), alerts.text_club(), alerts.text_run_summary(1, {"published": 3})]
    for text in texts:
        assert not re.search(r"https?://|token|hash=|[A-Za-z0-9_-]{32,}", text), text
    for text in texts[:-1]:
        assert "Действие:" in text


def test_failed_subscription_check_opens_club_cause(stand):
    from ecoprofi.bot.club import ClubUnavailable

    stand.import_policy()                                    # сообщество подключено к поиску
    stand.club.failure = ClubUnavailable("VK недоступен")
    assert stand.post(stand.users["club"], "перечень").status_code == 503
    assert stand.post(stand.users["club"], "перечень").status_code == 503
    assert list(stand.alerts()) == ["club"]
    [message] = pending(stand)
    assert "CLUB_GROUP_TOKEN" in message["text"] and "Действие:" in message["text"]
    sent(stand, message["id"], 1)

    stand.club.failure = None
    assert stand.post(stand.outsider, "перечень").status_code == 200     # отказ неподписчику — проверка прошла
    assert "club" not in stand.alerts()
    [recovery] = pending(stand)
    assert recovery["text"].startswith("Восстановлено")


def test_refusal_to_non_subscriber_opens_nothing(stand):
    stand.import_policy()
    assert stand.post(stand.outsider, "перечень").status_code == 200
    assert stand.alerts() == {}


def test_exempt_owner_searches_while_check_fails_and_cause_stays_open(stand):
    from ecoprofi.bot.club import ClubUnavailable

    stand.import_policy()
    owner = stand.users["unknown"]                 # без подписки
    stand.club.exempt.add(owner)
    stand.club.failure = ClubUnavailable("VK недоступен")
    assert stand.post(stand.users["club"], "перечень").status_code == 503
    answer = stand.post(owner, "загрязняющих веществ")
    assert answer.status_code == 200 and answer.json()["outcome"] != "not_subscribed"
    assert "club" in stand.alerts()                # исключение не закрывает причину: VK не проверялся
