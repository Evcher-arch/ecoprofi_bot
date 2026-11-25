"""Выбор исхода (T022, T027, T032): группировка по лекциям, номер документа, уточнение."""
from ecoprofi.bot import cards
from ecoprofi.bot.outcomes import decide
from ecoprofi.bot.search import Candidate, SearchResult


def fragment(material_id, lecture_id, score, *, coverage=1.0, kind="page", by_number=False, title=None):
    return Candidate(material_id=material_id, version_id=material_id, lecture_id=lecture_id,
                     title=title or f"Материал {material_id}", url="https://vk.com/wall-1_1", kind=kind,
                     score=score, coverage=coverage, similarity=0.5, text="выдержка",
                     lecture_title=f"Лекция {lecture_id}", by_number=by_number)


def result(fragments, *, lexemes=("плат", "выброс"), numbered=(), cards_=()):
    return SearchResult(tuple(lexemes), tuple(fragments), tuple(cards_), tuple(numbered),
                        ("2909-р",) if numbered else ())


def test_one_lecture_does_not_take_all_places_when_others_match():
    decision = decide(result([fragment(1, 10, 0.9), fragment(2, 10, 0.8), fragment(3, 10, 0.7), fragment(4, 20, 0.2)]))
    assert decision.outcome == "found"
    assert [c.material_id for c in decision.cards] == [1, 2, 4]


def test_documents_do_not_push_out_a_confirmed_recording():
    ranked = [fragment(1, 10, 0.9), fragment(2, 10, 0.8), fragment(3, 20, 0.7),
              fragment(4, 20, 0.6, kind="episode"), fragment(5, 30, 0.5, kind="episode")]
    decision = decide(result(ranked))
    assert [(c.material_id, c.kind) for c in decision.cards] == [(1, "page"), (2, "page"), (4, "episode")]


def test_recordings_do_not_push_out_a_confirmed_document():
    ranked = [fragment(1, 10, 0.9, kind="episode"), fragment(2, 20, 0.8, kind="episode"),
              fragment(3, 30, 0.7, kind="episode"), fragment(4, 30, 0.2, kind="letter")]
    decision = decide(result(ranked))
    assert [c.kind for c in decision.cards] == ["episode", "episode", "letter"]


def test_single_lecture_may_fill_all_places():
    decision = decide(result([fragment(1, 10, 0.9), fragment(2, 10, 0.8), fragment(3, 10, 0.7)]))
    assert [c.material_id for c in decision.cards] == [1, 2, 3]


def test_letter_and_recording_of_one_lecture_are_separate_cards():
    decision = decide(result([fragment(1, 10, 0.9, kind="letter"), fragment(2, 10, 0.8, kind="episode")]))
    assert [c.kind for c in decision.cards] == ["letter", "episode"]


def test_more_close_lectures_than_cards_lead_to_clarification():
    close = [fragment(n, n * 10, 0.50 - n * 0.001) for n in range(1, 6)]
    decision = decide(result(close))
    assert decision.outcome == "clarify" and decision.cards == ()
    assert decision.options == ("Лекция 10", "Лекция 20", "Лекция 30", "Лекция 40", "Лекция 50")
    text = cards.message_text("clarify", [], decision.options)
    assert "• Лекция 10" in text and "презентация, письмо, запись" in text


def test_clear_leader_among_many_lectures_is_answered():
    spread = [fragment(1, 10, 0.9), fragment(2, 20, 0.5), fragment(3, 30, 0.4), fragment(4, 40, 0.1)]
    decision = decide(result(spread))
    assert decision.outcome == "found" and [c.material_id for c in decision.cards] == [1, 2, 3]


def test_three_lectures_fit_into_the_answer_without_clarification():
    decision = decide(result([fragment(n, n * 10, 0.5) for n in range(1, 4)]))
    assert decision.outcome == "found" and len(decision.cards) == 3


def test_material_named_by_number_goes_first():
    numbered = fragment(7, 70, 0.0, kind="material", by_number=True)
    decision = decide(result([fragment(1, 10, 0.9), fragment(2, 20, 0.8)], numbered=[numbered]))
    assert [c.material_id for c in decision.cards] == [7, 1, 2]
    assert decision.cards[0].kind == "material"


def test_fragment_of_numbered_material_replaces_the_whole_material_card():
    matched = fragment(7, 70, 0.3, by_number=True)
    numbered = fragment(7, 70, 0.0, kind="material", by_number=True)
    decision = decide(result([fragment(1, 10, 0.9), matched], numbered=[numbered]))
    assert [(c.material_id, c.kind) for c in decision.cards] == [(7, "page"), (1, "page")]


def test_number_alone_is_enough_without_other_words():
    numbered = fragment(7, 70, 0.0, kind="material", by_number=True)
    decision = decide(result([], lexemes=(), numbered=[numbered]))
    assert decision.outcome == "found" and [c.material_id for c in decision.cards] == [7]


def test_question_without_words_and_numbers_asks_to_clarify():
    assert decide(result([], lexemes=())).outcome == "clarify"


def test_weak_match_asks_and_no_match_refuses():
    assert decide(result([fragment(1, 10, 0.4, coverage=0.3)])).outcome == "clarify"
    assert decide(result([fragment(1, 10, 0.1, coverage=0.0)])).outcome == "empty"


def test_card_text_for_each_kind_of_place():
    page = {"type": "page", "page": 2, "pages_total": 8, "recognized": False}
    assert cards._place(page) == "Презентация, стр. 2 из 8"
    letter = {"type": "letter", "requisites": {"date": "2024-02-08", "number": "ВЧ-09-03-34/3616", "sender": None}}
    assert cards._place(letter) == "Письмо от 08.02.2024 № ВЧ-09-03-34/3616"
    assert cards._place({"type": "letter", "requisites": None}) == "Письмо, реквизиты не подтверждены"
    assert cards._place({"type": "material", "material_kind": "video"}) == "Запись целиком"


def test_lead_text_omits_listing_when_cards_shown():
    # Mini App рисует карточки сам: в тексте остаётся только вступление (без дубля перечня).
    card = {"title": "Лекция 67", "explanation": "Пояснение", "url": "https://vk.com/wall-1_2"}
    full = cards.message_text("found", [card])
    assert "Лекция 67" in full
    assert cards.lead_text("found", [card], full) == cards.FOUND_LEAD
    assert cards.lead_text("empty", [], cards.EMPTY_TEXT) == cards.EMPTY_TEXT
    assert cards.lead_text("clarify", [], cards.CLARIFY_TEXT) == cards.CLARIFY_TEXT
