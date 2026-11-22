"""Страницы презентаций и распознавание (T018)."""
import pytest

from ecoprofi.indexer import documents, ocr
from ecoprofi.indexer.retry import SourceParse
from tests.fixtures import make_documents as make


def test_format_is_detected_by_signature_not_extension():
    assert documents.detect_format(make.slides_scan()) == "pdf"
    assert documents.detect_format(make.rtf_letter()) == "rtf"
    assert documents.detect_format(b"PK\x03\x04 office file") == "unsupported"


def test_unsupported_format_gives_document_without_fragments():
    parsed = documents.parse_document(b"PK\x03\x04 office file")
    assert (parsed.kind, parsed.source_format, parsed.fragments, parsed.text_state) == (
        "document", "unsupported", [], "absent")


def test_broken_pdf_is_a_parse_error():
    with pytest.raises(SourceParse):
        documents.parse_document(b"%PDF-1.4 broken")


def test_text_layer_pages_are_not_recognised_and_keep_numbers():
    calls = []
    parsed = documents.parse_document(make.slides_text(), recognize=lambda image: calls.append(1) or "")
    assert calls == []                                   # текстовый слой есть: распознавание не нужно
    assert parsed.kind == "presentation" and parsed.pages_total == 2 and parsed.pages_unreadable == 0
    assert [(f.kind, f.page_no, f.part_no, f.recognized) for f in parsed.fragments] == [
        ("page", 1, 1, False), ("page", 2, 1, False)]
    assert "допустимых выбросов" in parsed.fragments[0].text
    # Таблица страницы не разрезается: заголовок и значения в одном фрагменте.
    table = parsed.fragments[1].text
    assert "Категория объекта" in table and "комплексное экологическое разрешение" in table


def test_scanned_page_is_recognised_and_blank_page_is_left_out():
    parsed = documents.parse_document(make.slides_scan())
    assert parsed.pages_total == 2 and parsed.pages_unreadable == 1 and parsed.text_state == "partial"
    assert parsed.recognized_pages == 2
    assert [(f.page_no, f.recognized) for f in parsed.fragments] == [(1, True)]
    text = parsed.fragments[0].text.lower()
    assert "негативное воздействие" in text and "размещение отходов" in text


def test_long_page_is_split_into_parts_with_the_same_page_number():
    sentence = "Плата за выбросы рассчитывается по ставкам и коэффициентам. "
    pages = [documents.Page(7, sentence * 60, False, True)]
    fragments = documents.page_fragments(pages)
    assert len(fragments) > 1
    assert {f.page_no for f in fragments} == {7}
    assert [f.part_no for f in fragments] == list(range(1, len(fragments) + 1))
    assert all(len(f.text) <= documents.FRAGMENT_CHARS for f in fragments)


def test_oversized_file_is_rejected(monkeypatch):
    monkeypatch.setattr(documents, "MAX_BYTES", 10)
    with pytest.raises(SourceParse):
        documents.parse_document(make.slides_scan())


def test_readability_threshold():
    assert ocr.is_readable("Плата взимается за выбросы загрязняющих веществ в атмосферный воздух источниками")
    assert not ocr.is_readable("")
    assert not ocr.is_readable("Стр. 2")                                        # слишком мало букв
    assert not ocr.is_readable("|| ~~ 4f 7x q1 zz 0o lI ;; ## xx 9k w2 e3 r4 t5 y6 u7 i8 o9 p0 a1 s2 d3 f4 g5 h6")
    # Шум распознавания логотипов и рамок: букв много, слов нет.
    assert not ocr.is_readable("экю ПРОФИ ЕНЕДЖМЕ ОО Нев ЦСВ xqz wrt plk mnb vcx zzz qqq www rrr ttt yyy ppp sss ddd")


def test_page_listing_legal_acts_is_readable():
    # Номера, даты, сокращения и предлоги не должны делать страницу «нечитаемой».
    page = ("ФЗ от 10.01.2002 № 7-ФЗ «Об охране окружающей среды». ФЗ от 04.05.1999 № 96-ФЗ «Об охране "
            "атмосферного воздуха». ФЗ от 24.06.1998 № 89-ФЗ «Об отходах производства и потребления». "
            "ПП РФ от 31.12.2020 № 2398, п. 2, ст. 16.1, ст. 4.1, КоАП РФ ст. 8.5, НК РФ, ВК РФ, ЗК РФ")
    assert ocr.is_readable(page)
