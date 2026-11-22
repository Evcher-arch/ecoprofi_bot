"""Плановое обновление по разнице вложений (T030)."""
from datetime import datetime, timedelta, timezone

import pytest

from ecoprofi.bot.access import visible_versions
from ecoprofi.indexer.schedule import ScheduleError
from tests.fixtures import run_corpus


@pytest.fixture
def indexed(stand):
    stand.use_run_corpus(12)
    assert stand.run()["state"] == "done"
    stand.transport.calls.clear()
    return stand


def targets(stand, run_id) -> list:
    return [sorted(r["targets"]) for r in stand.owner.execute(
        "SELECT targets FROM run_lecture WHERE run_id = %s ORDER BY position", (run_id,)).fetchall()]


def test_update_without_changes_reads_no_files(indexed):
    result = indexed.update()
    assert (result["state"], result["kind"], result["schedule_outcome"]) == ("done", "manual", "ok")
    assert (result["n_new"], result["n_changed"], result["n_deferred"], result["n_failed"]) == (0, 0, 0, 0)
    assert indexed.transport.file_reads() == [] and targets(indexed, result["id"]) == []


def test_new_schedule_link_processes_only_its_lecture(indexed):
    indexed.transport.feeds = run_corpus.feeds(13)
    indexed.transport.files = run_corpus.files(13)
    result = indexed.update(html=run_corpus.schedule_html(13))
    assert targets(indexed, result["id"]) == [sorted(run_corpus.doc_key(d) for d in run_corpus.doc_ids(13))]
    assert sorted(indexed.transport.file_reads()) == sorted(run_corpus.doc_ids(13))
    new = {indexed.doc(d)["id"] for d in run_corpus.doc_ids(13)}
    assert new <= set(visible_versions(indexed.owner, ("public",)))


def test_new_attachment_in_known_post_is_processed_alone(indexed):
    feeds = run_corpus.feeds(12)
    extra = run_corpus._doc(819999, "Дополнение.rtf")
    next(p for p in feeds["owner"] if p["id"] == run_corpus.post_id(1))["attachments"].append(extra)
    indexed.transport.feeds = feeds
    indexed.transport.files[819999] = run_corpus.letter(1, 3)
    result = indexed.update()
    assert targets(indexed, result["id"]) == [[run_corpus.doc_key(819999)]]
    assert indexed.transport.file_reads() == [819999] and result["n_new"] == 1


def test_removed_attachment_is_withdrawn(indexed):
    feeds = run_corpus.feeds(12)
    post = next(p for p in feeds["owner"] if p["id"] == run_corpus.post_id(2))
    post["attachments"] = post["attachments"][:1]
    indexed.transport.feeds = feeds
    indexed.update()
    gone = run_corpus.doc_ids(2)[1]
    assert indexed.doc(gone)["state"] == "deleted"
    assert indexed.doc(gone)["id"] not in visible_versions(indexed.owner, ("public",))


def test_deferred_material_is_retried_only_when_due(indexed):
    doc = run_corpus.doc_ids(3)[0]
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    indexed.owner.execute("UPDATE index_job SET state = 'retry', retry_at = %s WHERE item_id = %s", (future, doc))
    assert targets(indexed, indexed.update()["id"]) == []
    indexed.owner.execute("UPDATE index_job SET retry_at = now() - interval '1 minute' WHERE item_id = %s", (doc,))
    result = indexed.update()
    assert targets(indexed, result["id"]) == [[run_corpus.doc_key(doc)]]
    assert indexed.job(run_corpus.doc_key(doc))["state"] == "done"


def test_waiting_source_and_old_processor_are_revisited(indexed):
    waiting, old = run_corpus.doc_ids(4)
    indexed.owner.execute("UPDATE index_job SET waiting_source = true WHERE item_id = %s", (waiting,))
    indexed.owner.execute("UPDATE material_version v SET processor_version = 'documents-1' FROM material m "
                          "WHERE m.published_version_id = v.id AND m.item_id = %s", (old,))
    result = indexed.update()
    assert targets(indexed, result["id"]) == [sorted([run_corpus.doc_key(waiting), run_corpus.doc_key(old)])]
    assert result["n_changed"] == 1


def test_unreadable_schedule_keeps_lectures_and_opens_alert(indexed):
    before = indexed.catalog()

    def broken():
        raise ScheduleError("HTTP 502")

    doc = run_corpus.doc_ids(5)[0]
    indexed.owner.execute("UPDATE index_job SET state = 'retry', retry_at = now() WHERE item_id = %s", (doc,))
    result = indexed.update(fetch=broken)
    assert (result["state"], result["schedule_outcome"]) == ("done", "failed")
    assert indexed.catalog()["lectures"] == before["lectures"]
    assert indexed.job(run_corpus.doc_key(doc))["state"] == "done"
    assert "run" in indexed.alerts()
    indexed.update()
    assert "run" not in indexed.alerts()


def test_update_continues_unfinished_full_run(stand):
    stand.use_run_corpus(12)
    stand.transport.crash_after_files = 3
    with pytest.raises(KeyboardInterrupt):
        stand.run()
    result = stand.update()
    assert (result["kind"], result["state"]) == ("full", "done")
    assert stand.owner.execute("SELECT count(*) AS n FROM index_run").fetchone()["n"] == 1


def test_empty_paid_feed_stops_update_before_any_change(indexed):
    feeds = run_corpus.feeds(12)
    post = next(p for p in feeds["owner"] if p["id"] == run_corpus.post_id(2))
    post["attachments"] = post["attachments"][:1]          # снятие вложения не должно случиться
    indexed.transport.feeds = feeds
    indexed.transport.donut_count = 0
    before = indexed.catalog()
    result = indexed.update()
    assert (result["state"], result["stop_reason"]) == ("stopped", "paid_feed")
    assert indexed.catalog() == before
    assert indexed.doc(run_corpus.doc_ids(2)[1])["state"] == "active"
    assert indexed.transport.file_reads() == []
