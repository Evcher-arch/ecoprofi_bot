"""Полный проход порциями (T015): публикация по порциям, продолжение, блокировка, остановки."""
import pytest

from ecoprofi.bot.access import visible_versions
from ecoprofi.db import connect
from ecoprofi.indexer import runs
from tests.fixtures import run_corpus


def published(stand) -> int:
    return stand.owner.execute(
        "SELECT count(*) AS n FROM material WHERE published_version_id IS NOT NULL").fetchone()["n"]


def test_portions_are_published_one_after_another(stand):
    stand.use_run_corpus(12)
    seen = []

    def report(portion, results, done, total):
        # Материалы завершённой порции уже доступны поиску, не дожидаясь конца прохода.
        seen.append((portion, done, total, published(stand), len(visible_versions(stand.owner, ("public",)))))

    result = stand.run(portion=5, report=report)
    assert (result["state"], result["kind"], result["n_new"]) == ("done", "full", 24)
    assert seen == [(1, 5, 12, 10, 10), (2, 10, 12, 20, 20), (3, 12, 12, 24, 24)]
    portions = stand.owner.execute(
        "SELECT portion_no, count(*) AS n FROM run_lecture WHERE run_id = %s GROUP BY 1 ORDER BY 1",
        (result["id"],)).fetchall()
    assert [(p["portion_no"], p["n"]) for p in portions] == [(1, 5), (2, 5), (3, 2)]
    # Строка «в разработке» в список не входит.
    assert stand.catalog()["lectures"] == 13


def test_interrupted_run_resumes_without_rereading(stand):
    stand.use_run_corpus(12)
    stand.transport.crash_after_files = 13          # остановка посреди второй порции
    with pytest.raises(KeyboardInterrupt):
        stand.run(portion=5)
    first = stand.owner.execute("SELECT * FROM index_run").fetchone()
    assert first["state"] == "running"
    done_before = {r["item_id"] for r in stand.owner.execute(
        "SELECT item_id FROM index_job WHERE state = 'done' AND last_run_id = %s", (first["id"],)).fetchall()}
    assert len(done_before) == 13
    crashed = stand.owner.execute("SELECT item_id FROM index_job WHERE state = 'running'").fetchone()["item_id"]
    # У материала, на котором случилась остановка, нет версии и фрагментов (строки материала ещё нет).
    row = stand.doc(crashed)
    assert row is None or row["published_version_id"] is None

    with connect(stand.indexer_dsn) as conn:
        runs.reconcile(conn)
    assert stand.owner.execute("SELECT state FROM index_run").fetchone()["state"] == "interrupted"

    stand.transport.calls.clear()
    result = stand.run(portion=5)
    assert (result["id"], result["state"]) == (first["id"], "done")
    assert not set(stand.transport.file_reads()) & done_before
    assert crashed in stand.transport.file_reads()
    assert published(stand) == 24


def test_repeated_full_run_creates_nothing(stand):
    stand.use_run_corpus(12)
    stand.run()
    before = stand.catalog()
    result = stand.run()
    assert result["state"] == "done" and result["n_new"] == result["n_changed"] == 0
    assert result["n_unchanged"] == 24 and stand.catalog() == before


def test_second_run_is_refused_while_one_is_going(stand):
    stand.use_run_corpus(2)
    with connect(stand.indexer_dsn) as other:
        assert runs.acquire(other)
        try:
            assert stand.run()["state"] == "busy"
        finally:
            runs.release(other)
    assert stand.owner.execute("SELECT count(*) AS n FROM index_run").fetchone()["n"] == 0


def test_lost_authorization_stops_and_resumes_from_same_place(stand):
    stand.use_run_corpus(12)
    stand.transport.valid_tokens = {"fixture-token"}

    def revoke(portion, *rest):
        if portion == 1:
            stand.transport.valid_tokens = set()

    result = stand.run(report=revoke)
    assert (result["state"], result["stop_reason"]) == ("stopped", "auth")
    assert published(stand) == 10 and "access" in stand.alerts()
    stand.transport.valid_tokens = {"fixture-token"}
    resumed = stand.run()
    assert (resumed["id"], resumed["state"]) == (result["id"], "done") and published(stand) == 24


def test_ai_limit_stops_without_counting_a_failure(stand):
    stand.use_run_corpus(12)

    def limit(portion, *rest):
        if portion == 1:
            stand.ai.limit = 403

    result = stand.run(report=limit)
    assert (result["state"], result["stop_reason"]) == ("stopped", "ai_limit")
    queued = stand.owner.execute("SELECT state, failed_runs FROM index_job WHERE state <> 'done'").fetchall()
    assert [(q["state"], q["failed_runs"]) for q in queued] == [("queued", 0)]
    assert "ai_limit" in stand.alerts()
    stand.ai.limit = None
    assert stand.run()["state"] == "done"
    assert "ai_limit" not in stand.alerts() and published(stand) == 24


def test_daily_rate_limit_pauses_without_alert(stand):
    stand.use_run_corpus(3)
    stand.transport.errors["wall.get"] = [29]
    result = stand.run()
    assert (result["state"], result["stop_reason"]) == ("paused", "rate_limit")
    assert stand.alerts() == {}
    assert stand.run()["state"] == "done"


def test_pilot_sample_does_not_limit_the_full_run(stand):
    stand.use_run_corpus(4)
    stand.owner.execute("INSERT INTO pilot_lecture SELECT schedule_row FROM lecture ORDER BY lecture_date LIMIT 1")
    stand.run()
    assert published(stand) == 8


def test_full_run_summary_is_sent_once(stand):
    stand.use_run_corpus(2)
    result = stand.run()
    summary = stand.alerts()[f"run:{result['id']}"]
    assert summary["kind"] == "info" and "Опубликовано: 4" in summary["body"]
    assert run_corpus.FILES not in summary["body"]


# --- FR-23: платная лента читается доступом оператора в прежнем объёме ---

def _paid_flags(stand):
    return stand.owner.execute("SELECT post_id, paid_feed FROM material_link ORDER BY id").fetchall()


@pytest.mark.parametrize("lost", ["empty", "shrunk", "denied"])
def test_lost_paid_feed_stops_without_touching_catalog(stand, lost):
    stand.use_run_corpus(12)
    assert stand.run()["paid_feed_count"] == run_corpus.PAID_FEED_COUNT
    stand.transport.feeds = run_corpus.feeds(12)
    before, flags = stand.catalog(), _paid_flags(stand)
    if lost == "empty":
        stand.transport.donut_count = 0
    elif lost == "shrunk":
        stand.transport.donut_count = run_corpus.PAID_FEED_COUNT // 2 - 1
    else:
        stand.transport.errors["wall.get"] = [15]
    result = stand.run()
    assert (result["state"], result["stop_reason"]) == ("stopped", "paid_feed")
    assert stand.catalog() == before and _paid_flags(stand) == flags
    assert stand.owner.execute("SELECT max(missed) AS m FROM lecture_post").fetchone()["m"] == 0
    assert "paid_feed" in stand.alerts()
    assert "Действие:" in stand.alerts()["paid_feed"]["body"]

    stand.transport.donut_count = run_corpus.PAID_FEED_COUNT
    resumed = stand.run()
    assert (resumed["id"], resumed["state"]) == (result["id"], "done")
    assert "paid_feed" not in stand.alerts()


def test_paid_feed_within_ratio_continues(stand):
    stand.use_run_corpus(4)
    stand.run()
    stand.transport.donut_count = run_corpus.PAID_FEED_COUNT // 2 + 1
    assert stand.run()["state"] == "done"
