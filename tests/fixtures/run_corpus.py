"""Корпус для проверок прохода порциями: N лекций по две публикации-письма.

Документы — письма RTF: они разбираются без распознавания, поэтому корпус из
десятков материалов собирается и обрабатывается быстро. Идентификаторы вымышлены.
"""
from tests.fixtures import docs_corpus
from tests.fixtures import make_documents as docs

COMMUNITY = docs_corpus.COMMUNITY
DOC_OWNER = docs_corpus.DOC_OWNER
FILES = docs_corpus.FILES
VIDEO_WITHOUT_TRACK = (-100001, 444)      # запись из vk_wall.json без субтитров
# Платная лента корпуса пуста по содержимому, но VK сообщает её объём: проверка FR-23
# сравнивает именно это число.
PAID_FEED_COUNT = 40


def post_id(i: int) -> int:
    return 9500 + i


def doc_ids(i: int) -> tuple[int, int]:
    return 810000 + i * 10 + 1, 810000 + i * 10 + 2


def doc_key(doc_id: int) -> str:
    return f"doc{DOC_OWNER}_{doc_id}"


def _date(i: int) -> str:
    return f"{i:02d}.01.2026"


def schedule_html(n: int = 12, *, extra_planned: bool = True) -> str:
    rows = [docs_corpus._row("Дата", "<p>Тема</p>", "<p>Доп.материалы</p>"),
            docs_corpus._row("", "<p>Раздел 1. Полный обход</p>")]
    for i in range(1, n + 1):
        rows.append(docs_corpus._row(
            _date(i), f"<p><strong>{docs_corpus._wall(post_id(i), f'Лекция {i}. Тема номер {i}')}</strong></p>"))
    if extra_planned:
        rows.append(docs_corpus._row("28.02.2026", "<p>Лекция в разработке</p>"))
    return f"<html><body><table>{''.join(rows)}</table></body></html>"


def _doc(doc_id: int, title: str) -> dict:
    return {"type": "doc", "doc": {"owner_id": DOC_OWNER, "id": doc_id, "title": title, "ext": "rtf",
                                   "url": f"{FILES}{doc_id}?hash=secret-{doc_id}&dl=1"}}


def post(i: int, *, extra=()) -> dict:
    first, second = doc_ids(i)
    return {"id": post_id(i), "owner_id": COMMUNITY, "text": f"Лекция {i}",
            "attachments": [_doc(first, f"Письмо {i}-1.rtf"), _doc(second, f"Письмо {i}-2.rtf"), *extra]}


def video_attachment() -> dict:
    owner, video = VIDEO_WITHOUT_TRACK
    return {"type": "video", "video": {"owner_id": owner, "id": video, "title": "Запись без субтитров"}}


def feeds(n: int = 12) -> dict:
    return {"donut": [], "owner": [post(i) for i in range(n, 0, -1)]}


def letter(i: int, part: int) -> bytes:
    lines = ("УЧЕБНОЕ ВЕДОМСТВО ПРИМЕРА", f"Письмо по теме номер {i}, часть {part}",
             f"Разъяснение порядка расчёта платы за выбросы загрязняющих веществ для лекции номер {i}.",
             "Нормативы допустимых выбросов устанавливаются для стационарных источников.")
    return docs.rtf_letter(lines, ())


def files(n: int = 12) -> dict:
    result = {}
    for i in range(1, n + 1):
        first, second = doc_ids(i)
        result[first], result[second] = letter(i, 1), letter(i, 2)
    return result
