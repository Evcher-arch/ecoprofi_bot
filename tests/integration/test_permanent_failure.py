"""Постоянный сбой, пропуск публикации, ожидание источника, итоговые состояния (T016, T021)."""
from ecoprofi.db import connect
from ecoprofi.indexer import runs
from ecoprofi.indexer.outcomes import material_outcomes
from ecoprofi.indexer.report import build_report
from tests.fixtures import docs_corpus as corpus
from tests.fixtures import run_corpus

BROKEN = f"doc{corpus.DOC_OWNER}_{corpus.BROKEN}"


def prepare(stand):
    stand.sync()
    stand.use_docs()


def test_material_failing_three_runs_becomes_permanent(stand):
    prepare(stand)
    for expected in (1, 2):
        stand.run()
        job = stand.job(BROKEN)
        assert (job["state"], job["failed_runs"]) == ("retry", expected)
    stand.run()
    assert stand.job(BROKEN)["state"] == "permanent"
    assert f"material:{BROKEN}" in stand.alerts()

    stand.transport.calls.clear()
    stand.run()
    assert corpus.BROKEN not in stand.transport.file_reads()
    assert stand.owner.execute("SELECT count(*) AS n FROM alert WHERE cause_key = %s",
                               (f"material:{BROKEN}",)).fetchone()["n"] == 1
    assert BROKEN in build_report(stand.owner).split("## Постоянный сбой")[1].split("##")[0]

    with connect(stand.indexer_dsn) as conn:
        assert runs.requeue(conn, ("doc", corpus.DOC_OWNER, corpus.BROKEN))
        assert not runs.requeue(conn, ("doc", corpus.DOC_OWNER, corpus.BROKEN))
    job = stand.job(BROKEN)
    assert (job["state"], job["failed_runs"]) == ("queued", 0)
    assert f"material:{BROKEN}" not in stand.alerts()


def test_success_between_failures_resets_the_counter(stand):
    prepare(stand)
    stand.run()
    stand.transport.files[corpus.BROKEN] = corpus.files()[corpus.TEXT]
    stand.run()
    assert (stand.job(BROKEN)["state"], stand.job(BROKEN)["failed_runs"]) == ("done", 0)
    del stand.transport.files[corpus.BROKEN]
    stand.run()
    assert stand.job(BROKEN)["failed_runs"] == 1


def test_unsupported_format_is_final_without_counting(stand):
    prepare(stand)
    stand.run()
    stand.run()
    job = stand.job(f"doc{corpus.DOC_OWNER}_{corpus.DOCX}")
    assert (job["failure_reason"], job["failed_runs"]) == ("unsupported", 0)
    keys = [i["key"] for i in material_outcomes(stand.owner)["items"]["unsupported"]]
    assert f"doc{corpus.DOC_OWNER}_{corpus.DOCX}" in keys


def test_post_missing_from_feed_is_counted_and_kept_in_search(stand):
    prepare(stand)
    stand.run()
    feeds = corpus.feeds()
    feeds["owner"] = [p for p in feeds["owner"] if p["id"] != 9103]
    stand.transport.feeds = feeds
    stand.run()
    assert stand.owner.execute("SELECT missed FROM lecture_post WHERE post_id = 9103").fetchone()["missed"] == 1
    assert stand.doc(corpus.TEXT)["state"] == "active"
    assert "post:9103" in stand.alerts()
    stand.transport.feeds = corpus.feeds()
    stand.run()
    assert stand.owner.execute("SELECT missed FROM lecture_post WHERE post_id = 9103").fetchone()["missed"] == 0
    assert "post:9103" not in stand.alerts()


def test_record_without_track_waits_for_source(stand):
    prepare(stand)
    feeds = corpus.feeds()
    next(p for p in feeds["owner"] if p["id"] == 9106)["attachments"].append(run_corpus.video_attachment())
    stand.transport.feeds = feeds
    stand.run()
    owner, video = run_corpus.VIDEO_WITHOUT_TRACK
    key = f"video{owner}_{video}"
    assert stand.job(key)["waiting_source"] is True
    assert key in [i["key"] for i in material_outcomes(stand.owner)["items"]["waiting_source"]]


def test_finished_run_leaves_no_material_without_outcome(stand):
    prepare(stand)
    stand.run()
    rows = stand.owner.execute(
        "SELECT m.item_type, m.owner_id, m.item_id, j.state AS job_state FROM material m "
        "LEFT JOIN index_job j ON (j.item_type, j.owner_id, j.item_id) = (m.item_type, m.owner_id, m.item_id) "
        "WHERE m.state <> 'deleted'").fetchall()
    assert rows and all(r["job_state"] for r in rows)
    # Итог есть у каждого задания, включая сбойный документ, у которого ещё нет строки материала.
    jobs = stand.owner.execute("SELECT * FROM index_job WHERE coalesce(failure_reason, '') <> 'deleted'").fetchall()
    outcomes = material_outcomes(stand.owner)
    assert outcomes["total"] == len(jobs) > len(rows)
    assert f"doc{corpus.DOC_OWNER}_{corpus.BROKEN}" in [i["key"] for i in outcomes["items"]["deferred"]]
