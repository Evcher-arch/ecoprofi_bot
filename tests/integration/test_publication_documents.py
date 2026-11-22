"""Публикация документов (T021, T026, T035): страницы и письма, повтор без дублей,
сбой одного вложения, удаление, один документ в двух публикациях."""
import os
import tempfile

from ecoprofi.bot.import_policy import apply_policy, parse_policy
from ecoprofi.indexer.retry import SourceTimeout
from tests.fixtures import docs_corpus as corpus
from tests.fixtures import make_documents as make

KEY = "doc{}_{}".format(corpus.DOC_OWNER, "{}").format


def job(stand, doc_id):
    return stand.owner.execute(
        "SELECT state, failure_reason, attempt FROM index_job WHERE item_type = 'doc' AND item_id = %s",
        (doc_id,)).fetchone()


def links(stand, doc_id):
    return stand.owner.execute(
        "SELECT ml.post_id, ml.link_type, ml.paid_feed FROM material_link ml JOIN material m ON m.id = ml.material_id "
        "WHERE m.item_type = 'doc' AND m.item_id = %s ORDER BY ml.post_id", (doc_id,)).fetchall()


def test_scanned_presentation_is_published_by_pages(docs):
    result = docs.docs_results[KEY(corpus.SCAN)]
    assert (result["outcome"], result["kind"], result["pages"], result["unreadable"], result["fragments"]) == (
        "published", "presentation", 2, 1, 1)
    row = docs.doc(corpus.SCAN)
    assert (row["kind"], row["source_format"], row["text_state"], row["title"]) == (
        "presentation", "pdf", "partial", "Плата за НВОС")
    fragment = docs.owner.execute("SELECT kind, page_no, part_no, recognized FROM fragment WHERE version_id = %s",
                                  (row["published_version_id"],)).fetchone()
    assert (fragment["kind"], fragment["page_no"], fragment["part_no"], fragment["recognized"]) == ("page", 1, 1, True)


def test_text_presentation_is_published_without_recognition(docs):
    row = docs.doc(corpus.TEXT)
    assert (row["kind"], row["text_state"], row["pages_total"], row["pages_unreadable"], row["fragments"]) == (
        "presentation", "ready", 2, 0, 2)


def test_letter_is_published_with_confirmed_requisites(docs):
    row = docs.doc(corpus.LETTER)
    assert (row["kind"], row["req_state"], str(row["req_date"]), row["req_number"]) == (
        "letter", "confirmed", "2024-02-01", make.LETTER_NUMBER)
    assert make.LETTER_NUMBER.lower() in row["doc_numbers"]
    assert row["fragments"] >= 1


def test_recognised_letter_keeps_requisites_out_of_signature(docs):
    row = docs.doc(corpus.LETTER_SCAN)
    assert (row["kind"], row["req_state"]) == ("letter", "unverified")
    assert make.LETTER_NUMBER.lower() not in row["doc_numbers"]


def test_editor_can_confirm_requisites(docs):
    key = KEY(corpus.LETTER_SCAN)
    policy = dict(docs.policy(materials={}), version=2,
                  requisites={key: {"date": "2024-02-01", "number": make.LETTER_NUMBER}})
    apply_policy(docs.owner, parse_policy(policy))
    row = docs.doc(corpus.LETTER_SCAN)
    assert (row["req_state"], row["req_number"]) == ("confirmed", make.LETTER_NUMBER)
    assert make.LETTER_NUMBER.lower() in row["doc_numbers"]
    # Подтверждение редактора переживает повторную обработку изменившегося файла.
    docs.transport.files[corpus.LETTER_SCAN] = make.scan_pdf(tuple(make.LETTER_LINES + ["Дополнение."]))
    assert {r["ref"]: r for r in docs.materials_run()}[key]["outcome"] == "published"
    assert docs.doc(corpus.LETTER_SCAN)["req_state"] == "confirmed"


def test_unsupported_format_stays_a_material_without_text(docs):
    row = docs.doc(corpus.DOCX)
    assert (row["kind"], row["source_format"], row["text_state"], row["fragments"]) == (
        "document", "unsupported", "absent", 0)
    assert row["doc_numbers"] == ["7777-р"]
    assert job(docs, corpus.DOCX)["failure_reason"] == "unsupported"


def test_failed_attachment_does_not_block_the_rest_and_is_retried_alone(docs):
    failed = docs.docs_results[KEY(corpus.BROKEN)]
    assert failed["outcome"] == "retry:timeout"
    state = job(docs, corpus.BROKEN)
    assert (state["state"], state["failure_reason"], state["attempt"]) == ("retry", "timeout", 3)
    assert docs.doc(corpus.BROKEN) is None
    assert docs.doc(corpus.DOCX)["published_version_id"] is not None      # сосед по публикации опубликован

    docs.transport.files[corpus.BROKEN] = make.scan_pdf(tuple(make.SLIDE_TABLE))
    docs.transport.calls.clear()
    results = docs.materials_run(only=KEY(corpus.BROKEN))
    assert [(r["ref"], r["outcome"]) for r in results] == [(KEY(corpus.BROKEN), "published")]
    assert [c for c in docs.transport.calls if c.startswith("file:")] == [f"file:{corpus.BROKEN}"]
    assert job(docs, corpus.BROKEN)["state"] == "done"


def test_repeat_run_creates_no_versions(docs):
    before = docs.counts()
    again = {r["ref"]: r["outcome"] for r in docs.materials_run()}
    assert again[KEY(corpus.SCAN)] == again[KEY(corpus.LETTER)] == again[KEY(corpus.TEXT)] == "unchanged"
    assert docs.counts() == before


def test_one_document_in_two_publications_is_one_material(docs):
    assert docs.owner.execute("SELECT count(*) AS n FROM material WHERE item_type = 'doc' AND item_id = %s",
                              (corpus.SCAN,)).fetchone()["n"] == 1
    assert [(l["post_id"], l["link_type"]) for l in links(docs, corpus.SCAN)] == [
        (9101, "attachment"), (9103, "attachment")]


def test_temporary_failure_keeps_the_published_version(docs):
    version = docs.doc(corpus.TEXT)["published_version_id"]
    docs.transport.files[corpus.TEXT] = make.scan_pdf(tuple(make.SLIDE_TABLE))     # файл изменился…
    docs.transport.file_failures[corpus.TEXT] = [SourceTimeout("HTTP 504")] * 3    # …но не читается
    result = {r["ref"]: r for r in docs.materials_run()}[KEY(corpus.TEXT)]
    assert result["outcome"] == "retry:timeout"
    row = docs.doc(corpus.TEXT)
    assert (row["published_version_id"], row["state"]) == (version, "active")


def test_changed_file_replaces_the_version_entirely(docs):
    old = docs.doc(corpus.TEXT)["published_version_id"]
    docs.transport.files[corpus.TEXT] = make.scan_pdf(tuple(make.SLIDE_TABLE))
    assert {r["ref"]: r for r in docs.materials_run()}[KEY(corpus.TEXT)]["outcome"] == "published"
    row = docs.doc(corpus.TEXT)
    assert row["published_version_id"] != old and (row["pages_total"], row["fragments"]) == (1, 1)
    assert docs.owner.execute("SELECT count(*) AS n FROM material_version WHERE material_id = %s",
                              (row["id"],)).fetchone()["n"] == 1
    assert docs.owner.execute(
        "SELECT count(*) AS n FROM fragment f WHERE NOT EXISTS "
        "(SELECT 1 FROM material m WHERE m.published_version_id = f.version_id)").fetchone()["n"] == 0


def test_deleted_publication_removes_material_only_without_other_links(docs):
    assert docs.docs_results["post 9105"]["outcome"] == "deleted"
    feed = docs.transport.feeds["owner"]
    next(p for p in feed if p["id"] == 9101)["deleted"] = True
    docs.materials_run()
    assert docs.doc(corpus.SCAN)["state"] == "active"                  # осталась связь с публикацией 9103
    assert [l["post_id"] for l in links(docs, corpus.SCAN)] == [9103]
    next(p for p in feed if p["id"] == 9103)["deleted"] = True
    docs.materials_run()
    assert docs.doc(corpus.SCAN)["state"] == "deleted"
    assert docs.doc(corpus.TEXT)["state"] == "deleted"


def test_publication_missing_from_feed_does_not_confirm_deletion(docs):
    docs.transport.feeds["owner"] = [p for p in docs.transport.feeds["owner"] if p["id"] != 9103]
    results = {r["ref"]: r for r in docs.materials_run()}
    assert results["post 9103"]["outcome"] == "failed:empty"
    assert docs.doc(corpus.TEXT)["state"] == "active"


def test_paid_feed_is_recorded_on_the_link(docs):
    assert [l["paid_feed"] for l in links(docs, corpus.LETTER)] == [True]
    assert [l["paid_feed"] for l in links(docs, corpus.SCAN)] == [False, False]


def test_mention_is_not_stored_as_attachment(docs):
    mention = docs.docs_results[f"video{corpus.COMMUNITY}_111"]
    assert (mention["outcome"], mention["note"]) == ("mention", "материала нет в каталоге")
    assert docs.owner.execute("SELECT count(*) AS n FROM material WHERE item_type = 'video'").fetchone()["n"] == 0


def test_links_are_stable_and_file_addresses_are_not_stored(docs):
    urls = [r["url"] for r in docs.owner.execute("SELECT url FROM material").fetchall()]
    assert urls and all(u.startswith(f"https://vk.com/wall{corpus.COMMUNITY}_") for u in urls)
    logged = docs.owner.execute("SELECT string_agg(details::text, ' ') AS t FROM operation_log").fetchone()["t"]
    assert "secret-" not in logged and "fixture.invalid" not in logged


def test_no_document_files_are_left_on_disk(stand):
    def listing():
        found = set()
        for folder in (tempfile.gettempdir(), os.getcwd()):
            for name in os.listdir(folder):
                if name.lower().endswith((".pdf", ".rtf", ".png", ".tif", ".tiff", ".ppm")):
                    found.add(os.path.join(folder, name))
        return found

    before = listing()
    stand.load_docs()
    assert listing() == before
