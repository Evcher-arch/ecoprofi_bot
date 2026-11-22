"""Синтетическое расписание и ленты публикаций с документами.

Идентификаторы вымышлены. Адреса файлов условные: фиктивный транспорт отдаёт по
ним документы из make_documents.py.
"""
from tests.fixtures import make_documents as docs

COMMUNITY = -100001
DOC_OWNER = 700001
FILES = "https://fixture.invalid/files/"

SCAN, LETTER, LETTER_SCAN, TEXT, DOCX, BROKEN = 501, 502, 503, 504, 506, 507

ROW_FEES = "2025-03-05 лекция 1. плата за негативное воздействие"
ROW_LIMITS = "2025-03-12 лекция 2. нормативы выбросов"
ROW_PLANNED = "2025-03-19 лекция 3. отходы"
ROW_DELETED = "2025-03-26 лекция 5. удалённая публикация"
ROW_MIXED = "2025-04-02 лекция 6. форматы"


def _cell(html: str) -> str:
    return f'<td data-table-cell class="ArticleTableCell"><div class="ArticleTableCell__content">{html}</div></td>'


def _row(date: str, topic: str, extra: str = "<p></p>") -> str:
    return f'<tr data-table-row class="ArticleTableRow">{_cell(f"<p><strong>{date}</strong></p>")}{_cell(topic)}{_cell(extra)}</tr>'


def _wall(post_id: int, text: str) -> str:
    return f'<a href="https://vk.ru/wall{COMMUNITY}_{post_id}">{text}</a>'


def schedule_html(*, without: tuple[str, ...] = (), club_on_lecture_1: bool = False) -> str:
    """Страница расписания. without — названия строк, которые убрать из таблицы."""
    rows = [
        _row("Дата", "<p>Тема (вписать экспертов)</p>", "<p>Доп.материалы</p>"),
        _row("", "<p>Раздел 1. Основы</p>"),
        _row("05.03.2025", f"<p><strong>{_wall(9101, 'Лекция 1. Плата за негативное воздействие')}</strong>"
                           f"{' (Клуб)' if club_on_lecture_1 else ''}</p>",
             f"<p>{_wall(9102, 'Разбор письма')} (Клуб)</p>"
             '<p><a href="https://vk.ru/@fixture-article">Статья</a> (для всех)</p>'),
        _row("12.03.2025", f"<p><strong>{_wall(9103, 'Лекция 2. Нормативы выбросов')}</strong></p>"),
        _row("19.03.2025", "<p>Лекция 3. Отходы</p>"),
        _row("19.03.2025", "<p>Лекция 4. «Паспорта» отходов</p>"),
        _row("", "<p>Раздел 2. Практика</p>"),
        _row("26.03.2025", f"<p><strong>{_wall(9105, 'Лекция 5. Удалённая публикация')}</strong></p>"),
        _row("02.04.2025", f"<p><strong>{_wall(9106, 'Лекция 6. Форматы')}</strong></p>",
             f'<p><a href="https://vk.ru/away.php?to=https://vk.com/wall{COMMUNITY}_9103&utf=1">Ещё раз лекция 2</a></p>'),
    ]
    rows = [r for r in rows if not any(name in r for name in without)]
    return f"<html><body><h1>Расписание</h1><table>{''.join(rows)}</table></body></html>"


def _doc(doc_id: int, title: str, ext: str) -> dict:
    return {"type": "doc", "doc": {"owner_id": DOC_OWNER, "id": doc_id, "title": title, "ext": ext,
                                   "url": f"{FILES}{doc_id}?hash=secret-{doc_id}&dl=1"}}


def feeds() -> dict:
    """Ленты wall.get: платная и открытая."""
    return {
        "donut": [
            {"id": 9102, "owner_id": COMMUNITY, "text": "Разбор письма для клуба",
             "attachments": [_doc(LETTER, "Письмо о перечне.rtf", "rtf"),
                             _doc(LETTER_SCAN, "Письмо скан.pdf", "pdf")]},
        ],
        "owner": [
            {"id": 9106, "owner_id": COMMUNITY, "text": "Форматы",
             "attachments": [_doc(DOCX, "Таблица ставок 7777-р.docx", "docx"),
                             _doc(BROKEN, "Недоступный файл.pdf", "pdf")]},
            {"id": 9105, "owner_id": COMMUNITY, "text": "Удалено", "deleted": True, "attachments": []},
            {"id": 9103, "owner_id": COMMUNITY,
             "text": f"Лекция 2. См. также video{COMMUNITY}_111 и doc{DOC_OWNER}_{SCAN}",
             "attachments": [_doc(TEXT, "Нормативы выбросов.pdf", "pdf"),
                             _doc(SCAN, "Плата за НВОС.pdf", "pdf")]},
            {"id": 9101, "owner_id": COMMUNITY, "text": "Лекция 1",
             "attachments": [_doc(SCAN, "Плата за НВОС.pdf", "pdf")]},
        ],
    }


def files() -> dict:
    """Содержимое файлов по номеру документа."""
    return {
        SCAN: docs.slides_scan(), LETTER: docs.rtf_letter(), LETTER_SCAN: docs.letter_scan(),
        TEXT: docs.slides_text(), DOCX: b"PK\x03\x04 not a supported document",
    }
