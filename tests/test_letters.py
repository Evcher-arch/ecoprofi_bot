"""Письма и реквизиты (T024)."""
from datetime import date

from ecoprofi.indexer import documents
from tests.fixtures import make_documents as make


def test_rtf_in_cp1251_is_read_without_distortion():
    text = documents.rtf_text(make.rtf_letter())
    assert "МИНИСТЕРСТВО ПРИРОДНЫХ РЕСУРСОВ УЧЕБНОГО ПРИМЕРА" in text
    assert "взвешенные вещества нормируются" in text


def test_requisites_from_text_layer_are_confirmed():
    parsed = documents.parse_document(make.rtf_letter())
    req = parsed.requisites
    assert parsed.kind == "letter" and parsed.source_format == "rtf"
    assert (req.state, req.when, req.number) == ("confirmed", date(2024, 2, 1), make.LETTER_NUMBER)
    assert req.sender == "МИНИСТЕРСТВО ПРИРОДНЫХ РЕСУРСОВ УЧЕБНОГО ПРИМЕРА"


def test_table_row_is_written_with_column_headers():
    parsed = documents.parse_document(make.rtf_letter())
    text = " ".join(f.text for f in parsed.fragments)
    assert "Показатель: Взвешенные вещества; Ставка: девятьсот рублей." in text


def test_letter_is_split_into_parts_within_the_limit():
    lines = tuple(make.LETTER_LINES) + tuple(
        f"Абзац {n}. Плата за сбросы взвешенных веществ рассчитывается по установленным ставкам и коэффициентам."
        for n in range(40))
    parsed = documents.parse_document(make.rtf_letter(lines, ()))
    assert len(parsed.fragments) > 1
    assert [f.part_no for f in parsed.fragments] == list(range(1, len(parsed.fragments) + 1))
    assert all(f.kind == "letter_part" and len(f.text) <= documents.FRAGMENT_CHARS for f in parsed.fragments)


def test_recognised_requisites_stay_unverified():
    parsed = documents.parse_document(make.letter_scan())
    assert parsed.kind == "letter"
    assert parsed.requisites.state == "unverified"


def test_rtf_without_requisites_is_a_plain_document():
    parsed = documents.parse_document(make.rtf_letter(("Сравнительная таблица ставок платы за сбросы.",), ()))
    assert parsed.kind == "document" and parsed.requisites.state == "absent"
    assert parsed.fragments


def test_reference_to_incoming_letter_is_not_taken_as_requisites():
    req = documents.find_requisites("На № 15/77 от 10.01.2024\nО рассмотрении обращения", confirmed=True)
    assert req.state == "absent"


def test_impossible_date_is_not_taken_as_requisites():
    assert documents.find_requisites("45.13.2024 № АБ-1/2", confirmed=True).state == "absent"


HEADING = "\n".join([
    "Министерство природных ресурсов и экологии Российской Федерации",
    "ФЕДЕРАЛЬНАЯ СЛУЖБА ПО НАДЗОРУ В СФЕРЕ ПРИРОДОПОЛЬЗОВАНИЯ",
    "ПИСЬМО",
    "от 8 февраля 2024 года N ВЧ-09-03-34/3616",
    "Об отнесении отдельных видов пыли к группе взвешенных веществ",
])


def test_heading_with_month_in_words_and_latin_number_sign():
    req = documents.find_requisites(HEADING, confirmed=True)
    assert (req.state, req.when, req.number) == ("confirmed", date(2024, 2, 8), "ВЧ-09-03-34/3616")
    assert req.sender == "ФЕДЕРАЛЬНАЯ СЛУЖБА ПО НАДЗОРУ В СФЕРЕ ПРИРОДОПОЛЬЗОВАНИЯ"   # ближайшая к реквизитам строка


def test_act_mentioned_in_text_is_never_taken_for_the_letter_itself():
    # Так выглядело реальное письмо: собственные реквизиты в заголовке, а ниже — распоряжение с датой и номером.
    text = "\n".join([
        "МИНИСТЕРСТВО ПРИРОДНЫХ РЕСУРСОВ И ЭКОЛОГИИ РОССИЙСКОЙ ФЕДЕРАЦИИ",
        "ПИСЬМО",
        "от 29 ноября 2023 года № 12-50/18281-ОГ",
        "О проверке наименования вещества, указанного в Перечне, утвержденном распоряжением "
        "Правительства Российской Федерации от 20.10.2023 № 2909-р",
    ])
    req = documents.find_requisites(text, confirmed=True)
    assert (req.when, req.number) == (date(2023, 11, 29), "12-50/18281-ОГ")

    table = "Сравнительная таблица к распоряжению Правительства РФ от 20.10.2023 № 2909-р и прежнему перечню"
    assert documents.find_requisites(table, confirmed=True).state == "absent"
