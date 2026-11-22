"""Поиск по страницам и письмам (T022, T027): место в источнике из каталога,
подтверждённые реквизиты, поиск по номеру документа."""
from tests.fixtures import docs_corpus as corpus
from tests.fixtures import make_documents as make

WALL = f"https://vk.com/wall{corpus.COMMUNITY}_"


def card_of(stand, answer, doc_id):
    for card in answer["cards"]:
        row = stand.owner.execute("SELECT item_type, item_id FROM material WHERE id = %s",
                                  (card["material_id"],)).fetchone()
        if (row["item_type"], row["item_id"]) == ("doc", doc_id):
            return card
    raise AssertionError(f"документа {doc_id} нет в карточках: {answer['cards']}")


def test_scanned_page_is_found_with_page_number_and_lecture(docs):
    answer = docs.ask("club", "плата за размещение отходов")
    assert answer["outcome"] == "found"
    card = card_of(docs, answer, corpus.SCAN)
    assert set(card) == {"type", "material_id", "title", "lecture", "explanation", "url",
                         "excerpt", "page", "pages_total", "recognized"}
    assert (card["type"], card["page"], card["pages_total"], card["recognized"]) == ("page", 1, 2, True)
    assert card["title"] == "Плата за НВОС"
    assert card["lecture"] == {"title": "Лекция 1. Плата за негативное воздействие", "date": "2025-03-05"}
    assert card["url"] == WALL + "9101"
    assert "отход" in card["excerpt"].lower()
    assert "Презентация, стр. 1 из 2 (текст распознан)" in answer["text"]
    assert "Лекция: Лекция 1. Плата за негативное воздействие · 05.03.2025" in answer["text"]
    assert "fixture.invalid" not in answer["text"] and "secret-" not in answer["text"]


def test_excerpt_is_verbatim_text_of_that_page(docs):
    card = card_of(docs, docs.ask("club", "плата за размещение отходов"), corpus.SCAN)
    page = docs.owner.execute(
        "SELECT f.text FROM fragment f JOIN material m ON m.published_version_id = f.version_id "
        "WHERE m.id = %s AND f.page_no = %s", (card["material_id"], card["page"])).fetchone()["text"]
    assert card["excerpt"] in page


def test_text_layer_page_is_found_without_recognition_mark(docs):
    answer = docs.ask("club", "нормативы допустимых выбросов стационарных источников")
    card = card_of(docs, answer, corpus.TEXT)
    assert (card["type"], card["page"], card["recognized"]) == ("page", 1, False)
    assert card["url"] == WALL + "9103"
    assert "(текст распознан)" not in answer["text"].split(card["title"])[1].split("\n\n")[0]


def test_table_value_comes_with_its_heading(docs):
    card = card_of(docs, docs.ask("club", "комплексное экологическое разрешение первая категория"), corpus.TEXT)
    assert card["page"] == 2
    assert "категория" in card["excerpt"].lower()


def test_letter_is_returned_with_confirmed_requisites(docs):
    answer = docs.ask("club", "взвешенные вещества нормируются по перечню")
    card = card_of(docs, answer, corpus.LETTER)
    assert card["type"] == "letter"
    assert card["requisites"] == {"date": "2024-02-01", "number": make.LETTER_NUMBER,
                                  "sender": "МИНИСТЕРСТВО ПРИРОДНЫХ РЕСУРСОВ УЧЕБНОГО ПРИМЕРА"}
    assert card["requisites_note"] is None
    assert card["url"] == WALL + "9102"
    assert f"Письмо от 01.02.2024 № {make.LETTER_NUMBER}" in answer["text"]


def test_recognised_requisites_are_never_shown(docs):
    answer = docs.ask("club", "взвешенные вещества нормируются по перечню")
    card = card_of(docs, answer, corpus.LETTER_SCAN)
    assert card["type"] == "letter" and card["requisites"] is None
    assert card["requisites_note"] == "реквизиты не подтверждены"
    assert (card["page"], card["recognized"]) == (1, True)
    block = next(part for part in answer["text"].split("\n\n") if "Письмо скан" in part)
    assert "Письмо, реквизиты не подтверждены" in block
    # Дата и номер могут стоять в самой выдержке письма, но не в строке реквизитов.
    assert f"Письмо от 01.02.2024 № {make.LETTER_NUMBER}" not in block


def test_question_with_document_number_only_finds_the_letter(docs):
    answer = docs.ask("club", make.LETTER_NUMBER)
    assert answer["outcome"] == "found"
    assert docs.doc_ids(answer)[0] == corpus.LETTER
    assert corpus.LETTER_SCAN not in docs.doc_ids(answer) or \
        card_of(docs, answer, corpus.LETTER_SCAN)["requisites"] is None


def test_number_in_title_gives_whole_material_card_without_place(docs):
    answer = docs.ask("club", "таблица 7777-р")
    assert answer["outcome"] == "found"
    card = card_of(docs, answer, corpus.DOCX)
    assert set(card) == {"type", "material_id", "title", "lecture", "explanation", "url", "material_kind"}
    assert (card["type"], card["material_kind"]) == ("material", "document")
    assert "Документ целиком" in answer["text"] and "стр." not in answer["text"]


def test_similar_number_is_a_different_document(docs):
    answer = docs.ask("club", "7777")
    assert corpus.DOCX not in docs.doc_ids(answer)


def test_model_gets_only_excerpts_and_cannot_add_candidates(docs):
    docs.ai.foreign_id = 999_999
    answer = docs.ask("club", "плата за размещение отходов")
    sent = docs.ai.explain_calls[-1]["candidates"]
    assert {c["id"] for c in sent} == {c["material_id"] for c in answer["cards"] if "excerpt" in c}
    assert all(c["material_id"] != 999_999 for c in answer["cards"])


def test_question_outside_corpus_has_no_pages_or_requisites(docs):
    answer = docs.ask("club", "рецепт борща со сметаной")
    assert (answer["outcome"], answer["cards"]) == ("empty", [])
    assert "стр." not in answer["text"] and "№" not in answer["text"]


def test_document_survives_reconnect_without_reindexing(docs):
    before = docs.counts()
    answer = docs.ask("club", "плата за размещение отходов")
    assert docs.doc_ids(answer)[0] == corpus.SCAN
    assert docs.counts() == before
