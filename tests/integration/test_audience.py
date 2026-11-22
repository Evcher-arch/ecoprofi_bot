"""Действующая аудитория материала (T029): расписание, платная лента, решение редактора."""
from ecoprofi.bot.access import MATERIAL_AUDIENCE
from ecoprofi.bot.import_policy import apply_policy, parse_policy
from ecoprofi.db import connect
from ecoprofi.indexer.report import build_report
from tests.fixtures import docs_corpus as corpus
from tests.fixtures import make_documents as make

LETTER_QUESTION = "взвешенные вещества нормируются по перечню"
KEY = "doc{}_{}".format(corpus.DOC_OWNER, "{}").format


def audience(stand, doc_id) -> dict:
    return stand.owner.execute(
        f"SELECT a.signal, a.decision, a.seen_signal, a.audience FROM ({MATERIAL_AUDIENCE}) a "
        "WHERE a.item_type = 'doc' AND a.item_id = %s", (doc_id,)).fetchone()


def decide(stand, materials: dict, **extra) -> None:
    apply_policy(stand.owner, parse_policy(dict(stand.policy(materials=materials), version=2, **extra)))


def report(stand) -> str:
    with connect(stand.indexer_dsn) as conn:
        return build_report(conn)


def test_club_mark_and_paid_feed_close_the_material(docs):
    row = audience(docs, corpus.LETTER)
    assert (row["signal"], row["decision"], row["audience"]) == ("club", None, "club")
    assert audience(docs, corpus.SCAN)["audience"] == "public"


def test_users_without_subscription_get_nothing_closed(docs):
    assert corpus.LETTER in docs.doc_ids(docs.ask("club", LETTER_QUESTION))
    for role in ("public", "unknown"):
        docs.ai.explain_calls.clear()
        for question in (LETTER_QUESTION, make.LETTER_NUMBER):
            response = docs.post(docs.users[role], question)
            docs.refused(response)
            assert make.LETTER_NUMBER not in response.text and "нормируются" not in response.text, role
        assert docs.ai.explain_calls == [], role                              # модель ничего не получила


def test_cancelled_subscription_closes_documents_without_restart(docs):
    assert corpus.LETTER in docs.doc_ids(docs.ask("club", LETTER_QUESTION))
    docs.subscribe("club", False)
    response = docs.post(docs.users["club"], LETTER_QUESTION)
    docs.refused(response)
    assert "нормируются" not in response.text


def test_paid_post_without_mark_is_club_even_for_shared_document(docs):
    feeds = docs.transport.feeds
    post = next(p for p in feeds["owner"] if p["id"] == 9103)
    feeds["owner"].remove(post)
    feeds["donut"].append(post)
    docs.materials_run()
    assert audience(docs, corpus.TEXT)["audience"] == "club"
    # Документ приложен и к открытой публикации 9101, и к платной 9103 — действует более строгая.
    assert audience(docs, corpus.SCAN)["audience"] == "club"


def test_editor_decision_made_with_current_signal_is_in_force(docs):
    decide(docs, {KEY(corpus.LETTER): "public"})
    row = audience(docs, corpus.LETTER)
    assert (row["signal"], row["decision"], row["seen_signal"], row["audience"]) == ("club", "public", "club", "public")
    assert corpus.LETTER in docs.doc_ids(docs.ask("club", LETTER_QUESTION))
    assert "Расхождения аудитории\n\n" in report(docs) and KEY(corpus.LETTER) not in report(docs).split(
        "## Расхождения аудитории")[1].split("##")[0]


def test_decision_made_before_schedule_gives_the_stricter_audience(docs):
    # Так выглядят решения функции 001: сигнал расписания на момент решения не записан.
    docs.owner.execute(
        "INSERT INTO material_policy (item_type, owner_id, item_id, audience) VALUES ('doc', %s, %s, 'public')",
        (corpus.DOC_OWNER, corpus.LETTER))
    row = audience(docs, corpus.LETTER)
    assert (row["seen_signal"], row["audience"]) == (None, "club")
    conflicts = report(docs).split("## Расхождения аудитории")[1].split("##")[0]
    assert KEY(corpus.LETTER) in conflicts and "клуб" in conflicts and "для всех" in conflicts


def test_changed_schedule_reopens_the_conflict_until_confirmed(docs):
    decide(docs, {KEY(corpus.SCAN): "public"})
    assert audience(docs, corpus.SCAN)["audience"] == "public"
    docs.sync(club_on_lecture_1=True)                         # в расписании появилась пометка «Клуб»
    row = audience(docs, corpus.SCAN)
    assert (row["signal"], row["seen_signal"], row["audience"]) == ("club", "public", "club")
    decide(docs, {KEY(corpus.SCAN): "public"})                # прежнее решение без изменений
    assert audience(docs, corpus.SCAN)["audience"] == "club"  # расхождение молча не закрывается
    decide(docs, {KEY(corpus.SCAN): "public"}, confirmed=[KEY(corpus.SCAN)])
    row = audience(docs, corpus.SCAN)
    assert (row["seen_signal"], row["audience"]) == ("club", "public")


def test_unknown_decision_and_review_state_close_material_for_everyone(docs):
    decide(docs, {KEY(corpus.LETTER): "unknown"})
    assert audience(docs, corpus.LETTER)["audience"] == "unknown"
    assert corpus.LETTER not in docs.doc_ids(docs.ask("club", LETTER_QUESTION))

    docs.owner.execute("UPDATE material SET state = 'review' WHERE item_type = 'doc' AND item_id = %s",
                       (corpus.LETTER_SCAN,))
    assert corpus.LETTER_SCAN not in docs.doc_ids(docs.ask("club", LETTER_QUESTION))
    docs.materials_run()                                      # повторная обработка не снимает проверку
    assert docs.doc(corpus.LETTER_SCAN)["state"] == "review"


def test_material_without_schedule_and_decision_stays_closed(loaded):
    # Правило функции 001 сохранено: запись вне расписания без решения редактора не выдаётся.
    loaded.import_policy(materials={})
    answer = loaded.ask("club", "загрязняющих веществ")
    assert answer["cards"] == []


def test_report_lists_materials_without_their_text(docs):
    text = report(docs)
    assert corpus.ROW_FEES in text and KEY(corpus.SCAN) in text and "презентация" in text and "письмо" in text
    assert "нечитаемых 1" in text                                         # пустая страница презентации
    assert KEY(corpus.BROKEN) in text.split("## Сбои чтения")[1]          # сбой чтения виден редактору
    assert KEY(corpus.LETTER_SCAN) in text.split("## Реквизиты не подтверждены")[1].split("##")[0]
    assert "в разработке" in text
    for secret in ("нормируются", "взимается", "secret-", "fixture.invalid", "hash="):
        assert secret not in text, secret


def test_link_with_unchecked_feed_does_not_open_the_material(docs):
    # Связи, перенесённые из 001, не знают, платная ли лента публикации: без решения
    # редактора такой материал закрыт, даже если в расписании нет пометки «Клуб».
    docs.owner.execute(
        "UPDATE material_link SET paid_feed = NULL WHERE material_id = %s", (docs.doc(corpus.TEXT)["id"],))
    row = audience(docs, corpus.TEXT)
    assert (row["signal"], row["audience"]) == (None, None)
    assert corpus.TEXT not in docs.doc_ids(docs.ask("club", "нормативы допустимых выбросов стационарных источников"))
    docs.materials_run()                                   # проход по ленте проверяет её и открывает материал
    assert audience(docs, corpus.TEXT)["audience"] == "public"
