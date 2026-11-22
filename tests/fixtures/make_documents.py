"""Сборка синтетических документов для проверок: PDF со сканами, PDF с текстовым
слоем и письмо RTF. Файлы строятся в памяти при запуске тестов и в репозиторий
не попадают. Все тексты, реквизиты и номера вымышлены.
"""
import io
from functools import lru_cache

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
PAGE = (1654, 1170)      # альбомная страница, 150 точек на дюйм

SLIDE_FEES = [
    "Плата за негативное воздействие",
    "Плата взимается за выбросы загрязняющих веществ",
    "в атмосферный воздух стационарными источниками,",
    "за сбросы в водные объекты",
    "и за размещение отходов производства.",
]
SLIDE_LIMITS = [
    "Нормативы допустимых выбросов",
    "Нормативы допустимых выбросов устанавливаются",
    "для стационарных источников объектов",
    "первой и второй категории.",
]
SLIDE_TABLE = [
    "Категория объекта и вид разрешения",
    "Первая категория: комплексное экологическое разрешение",
    "Вторая категория: декларация о воздействии",
    "Третья категория: отчётность о выбросах",
]
LETTER_DATE = "01.02.2024"
LETTER_NUMBER = "АБ-01-02-03/1234"
LETTER_LINES = [
    "МИНИСТЕРСТВО ПРИРОДНЫХ РЕСУРСОВ УЧЕБНОГО ПРИМЕРА",
    f"{LETTER_DATE} № {LETTER_NUMBER}",
    "О применении перечня загрязняющих веществ",
    "Сообщаем, что взвешенные вещества нормируются по перечню, утверждённому распоряжением 9999-р.",
    "Плата за сбросы взвешенных веществ рассчитывается по установленным ставкам.",
]
LETTER_TABLE = [("Показатель", "Ставка"), ("Взвешенные вещества", "девятьсот рублей"), ("Нефтепродукты", "тысяча рублей")]


def page_image(lines: list[str]):
    """Белая страница с чёрным текстом. Пустой список — пустая страница."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(image)
    y = 120
    for index, line in enumerate(lines):
        font = ImageFont.truetype(FONT, 56 if index == 0 else 44)
        draw.text((110, y), line, fill="black", font=font)
        y += 110 if index == 0 else 84
    return image


@lru_cache(maxsize=None)
def scan_pdf(*pages: tuple[str, ...]) -> bytes:
    """PDF из изображений страниц, без текстового слоя."""
    images = [page_image(list(lines)) for lines in pages]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", resolution=150.0, save_all=True, append_images=images[1:])
    return buffer.getvalue()


@lru_cache(maxsize=None)
def text_pdf(*pages: tuple[str, ...]) -> bytes:
    """PDF с текстовым слоем: страницы собираются распознавателем в «PDF с поиском»."""
    import pypdfium2 as pdfium
    import pytesseract

    result = pdfium.PdfDocument.new()
    for lines in pages:
        single = pytesseract.image_to_pdf_or_hocr(page_image(list(lines)), extension="pdf", lang="rus")
        source = pdfium.PdfDocument(single)
        result.import_pages(source)
        source.close()
    buffer = io.BytesIO()
    result.save(buffer)
    result.close()
    return buffer.getvalue()


def _rtf_escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("\\'%02x" % ch.encode("cp1251")[0])
    return "".join(out)


@lru_cache(maxsize=None)
def rtf_letter(lines: tuple[str, ...] = tuple(LETTER_LINES), table: tuple = tuple(LETTER_TABLE)) -> bytes:
    """Письмо RTF в кодовой странице 1251 с таблицей."""
    body = ["{\\rtf1\\ansi\\ansicpg1251\\deff0{\\fonttbl{\\f0 Times New Roman;}}"]
    for line in lines:
        body.append("\\pard " + _rtf_escape(line) + "\\par")
    for row in table:
        cells = "".join("\\intbl " + _rtf_escape(cell) + "\\cell " for cell in row)
        body.append("\\trowd\\cellx4000\\cellx8000 " + cells + "\\row")
    body.append("\\pard\\par}")
    return "\n".join(body).encode("ascii")


def slides_scan() -> bytes:
    """Презентация-скан: читаемая страница и пустая страница."""
    return scan_pdf(tuple(SLIDE_FEES), ())


def slides_text() -> bytes:
    """Презентация с текстовым слоем: вторая страница — таблица."""
    return text_pdf(tuple(SLIDE_LIMITS), tuple(SLIDE_TABLE))


def letter_scan() -> bytes:
    """То же письмо одной страницей-изображением: реквизиты только распознаются."""
    return scan_pdf(tuple(LETTER_LINES))
