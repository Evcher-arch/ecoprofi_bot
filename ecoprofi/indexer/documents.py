"""Разбор документов: страницы презентаций (PDF) и письма (RTF).

Файл обрабатывается в памяти и на диск не записывается. Страница без текстового
слоя распознаётся локально. Реквизиты письма считаются подтверждёнными только
когда взяты из текстового слоя оригинала (research.md, R6).
"""
import re
from dataclasses import dataclass, field
from datetime import date

from ecoprofi.indexer import ocr
from ecoprofi.indexer.retry import SourceParse

MAX_BYTES = 50 * 1024 * 1024
FRAGMENT_CHARS = 1500        # предел длины фрагмента (research.md, R9)
MIN_TEXT_LAYER = 20          # меньше знаков в текстовом слое — страница распознаётся
REQUISITES_WINDOW = 1500     # реквизиты ищутся в начале письма
LETTER_PDF_PAGES = 4         # PDF длиннее считается презентацией, даже если в нём есть дата и номер
LETTER_PDF_WINDOW = 600

_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
           "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_DATE = (r"(?:(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})"
         r"|(?P<d2>\d{1,2})\s+(?P<month>" + "|".join(_MONTHS) + r")\s+(?P<y2>\d{4}))")
_REQ = _DATE + r"\s*(?:г\.?|года)?\s*(?:№\s*|N\s+)(?P<number>[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё/.\-]*)"
# Реквизиты признаются только в двух положениях: в заголовке «ПИСЬМО от … № …» либо отдельной
# строкой бланка. Дата и номер акта, упомянутого в тексте, реквизитами письма не считаются.
_REQ_HEADING = re.compile(r"ПИСЬМО\s+(?:от\s+)?" + _REQ, re.I)
_REQ_LINE = re.compile(r"^[ 	]*(?:от\s+)?" + _REQ + r"[ 	]*$", re.I | re.M)
_SENDER = re.compile(r"МИНИСТЕРСТВО|ФЕДЕРАЛЬН|СЛУЖБА|АГЕНТСТВО|ДЕПАРТАМЕНТ|УПРАВЛЕНИЕ|РОСПРИРОДНАДЗОР|МИНПРИРОДЫ")
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")


@dataclass(frozen=True)
class Fragment:
    kind: str                 # page / letter_part
    text: str
    page_no: int | None = None
    part_no: int = 1
    recognized: bool = False


@dataclass(frozen=True)
class Requisites:
    state: str = "absent"     # confirmed / unverified / absent
    when: date | None = None
    number: str | None = None
    sender: str | None = None


@dataclass
class Parsed:
    """Результат разбора одного файла."""
    kind: str                           # presentation / letter / document
    source_format: str                  # pdf / rtf / unsupported
    fragments: list[Fragment] = field(default_factory=list)
    requisites: Requisites = field(default_factory=Requisites)
    pages_total: int | None = None
    pages_unreadable: int | None = None
    recognized_pages: int = 0

    @property
    def text_state(self) -> str:
        if not self.fragments:
            return "absent"
        return "partial" if self.pages_unreadable else "ready"


def detect_format(raw: bytes) -> str:
    """Формат по сигнатуре файла, а не по расширению."""
    head = raw.lstrip(b"\xef\xbb\xbf \t\r\n")[:8]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    return "unsupported"


def clean(text: str) -> str:
    return " ".join((text or "").replace("\x00", " ").split())


def split_text(text: str, limit: int = FRAGMENT_CHARS) -> list[str]:
    """Разделить текст на части не длиннее limit: по предложениям, затем по словам."""
    text = clean(text)
    if len(text) <= limit:
        return [text] if text else []
    parts, current = [], ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                parts.append(current)
                current = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


# --- PDF ---

@dataclass(frozen=True)
class Page:
    number: int
    text: str
    recognized: bool
    readable: bool
    lines: str = ""           # текст с переводами строк — для поиска реквизитов


def pdf_pages(raw: bytes, *, recognize=None) -> list[Page]:
    """Страницы PDF: текстовый слой, при его отсутствии — распознавание изображения."""
    import pypdfium2 as pdfium   # импорт по месту: в образе bot чтения PDF нет

    recognize = recognize or ocr.recognize
    try:
        pdf = pdfium.PdfDocument(raw)
    except Exception as exc:   # pdfium сообщает о повреждённом файле собственными исключениями
        raise SourceParse(f"PDF не открыт ({type(exc).__name__}).") from None
    pages = []
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                try:
                    lines = textpage.get_text_range()
                finally:
                    textpage.close()
                recognized = False
                if len(clean(lines)) < MIN_TEXT_LAYER:
                    image = page.render(scale=ocr.RENDER_DPI / 72).to_pil()
                    lines, recognized = recognize(image), True
                    if not ocr.is_readable(lines):
                        # Светлый текст на тёмном фоне распознаётся после обращения цветов.
                        inverted = recognize(ocr.invert(image))
                        if ocr.is_readable(inverted):
                            lines = inverted
            finally:
                page.close()
            text = clean(lines)
            pages.append(Page(index + 1, text, recognized, ocr.is_readable(text), lines))
    finally:
        pdf.close()
    if not pages:
        raise SourceParse("В PDF нет страниц.")
    return pages


def page_fragments(pages: list[Page]) -> list[Fragment]:
    """Один фрагмент на страницу; длинная страница делится на части с тем же номером."""
    fragments = []
    for page in pages:
        if not page.readable:
            continue
        for part, text in enumerate(split_text(page.text), 1):
            fragments.append(Fragment("page", text, page_no=page.number, part_no=part,
                                      recognized=page.recognized))
    return fragments


# --- RTF ---

def rtf_text(raw: bytes) -> str:
    """Текст RTF с учётом кодовой страницы документа."""
    from striprtf.striprtf import rtf_to_text

    body = raw.decode("latin-1")
    codepage = re.search(r"\\ansicpg(\d+)", body)
    encoding = f"cp{codepage.group(1)}" if codepage else "cp1251"
    try:
        return rtf_to_text(body, encoding=encoding, errors="ignore")
    except LookupError:
        return rtf_to_text(body, encoding="cp1251", errors="ignore")
    except Exception as exc:
        raise SourceParse(f"RTF не разобран ({type(exc).__name__}).") from None


def table_rows(text: str) -> str:
    """Строки таблицы записать как «заголовок графы: значение».

    Разборщик RTF отделяет ячейки знаком «|». Первая строка подряд идущих
    табличных строк — заголовки граф.
    """
    result, header = [], None
    for line in text.splitlines():
        if "|" not in line:
            header = None
            result.append(line)
            continue
        cells = [clean(cell) for cell in line.split("|")]
        if cells and not cells[-1]:
            cells.pop()
        if header is None:
            header = cells
            continue
        pairs = [f"{head}: {value}" if head else value
                 for head, value in zip(header, cells) if value]
        pairs += [value for value in cells[len(header):] if value]
        if pairs:
            result.append("; ".join(pairs) + ".")
    return "\n".join(result)


def find_requisites(text: str, *, confirmed: bool, window: int = REQUISITES_WINDOW) -> Requisites:
    """Дата и номер письма в начале текста: заголовок «ПИСЬМО от … № …» или отдельная строка бланка."""
    head = text[:window]
    match = _REQ_HEADING.search(head) or _REQ_LINE.search(head)
    if not match:
        return Requisites()
    group = match.groupdict()
    try:
        if group["month"]:
            when = date(int(group["y2"]), _MONTHS[group["month"].lower()], int(group["d2"]))
        else:
            when = date(int(group["y"]), int(group["m"]), int(group["d"]))
    except ValueError:
        return Requisites()
    number = group["number"].rstrip(".-/")
    if not any(ch.isdigit() for ch in number):
        return Requisites()
    # Отправитель — ближайшая к реквизитам строка бланка с названием органа.
    before = [clean(line) for line in head[:match.start()].splitlines()]
    sender = next((line for line in reversed(before) if line and _SENDER.search(line.upper()) and len(line) <= 200),
                  None)
    return Requisites("confirmed" if confirmed else "unverified", when, number, sender)


def letter_fragments(text: str) -> list[Fragment]:
    """Части письма: абзацы объединяются до предела длины."""
    parts, current = [], ""
    for paragraph in (clean(p) for p in text.splitlines()):
        if not paragraph:
            continue
        if current and len(current) + 1 + len(paragraph) > FRAGMENT_CHARS:
            parts.extend(split_text(current))
            current = paragraph
        else:
            current = f"{current} {paragraph}".strip()
    parts.extend(split_text(current))
    return [Fragment("letter_part", part, part_no=number) for number, part in enumerate(parts, 1)]


# --- общий вход ---

def parse_document(raw: bytes, *, recognize=None) -> Parsed:
    """Определить формат и вид материала, извлечь фрагменты и реквизиты."""
    if len(raw) > MAX_BYTES:
        raise SourceParse("Документ превышает допустимый размер.")
    fmt = detect_format(raw)
    if fmt == "pdf":
        pages = pdf_pages(raw, recognize=recognize)
        parsed = Parsed(
            kind="presentation", source_format="pdf", fragments=page_fragments(pages),
            pages_total=len(pages), pages_unreadable=sum(not p.readable for p in pages),
            recognized_pages=sum(p.recognized for p in pages),
        )
        first = pages[0]
        if len(pages) <= LETTER_PDF_PAGES and first.readable:
            # Короткий PDF с датой и номером в начале — письмо. Реквизиты из
            # распознанного изображения остаются неподтверждёнными.
            requisites = find_requisites(first.lines or first.text, confirmed=not first.recognized,
                                         window=LETTER_PDF_WINDOW)
            if requisites.state != "absent":
                parsed.kind, parsed.requisites = "letter", requisites
        return parsed
    if fmt == "rtf":
        text = table_rows(rtf_text(raw))
        requisites = find_requisites(text, confirmed=True)
        return Parsed(kind="letter" if requisites.state == "confirmed" else "document",
                      source_format="rtf", fragments=letter_fragments(text), requisites=requisites)
    return Parsed(kind="document", source_format="unsupported")
