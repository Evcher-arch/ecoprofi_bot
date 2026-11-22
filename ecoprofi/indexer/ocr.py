"""Локальное распознавание страниц и порог читаемости (research.md, R4 и R10).

Распознавание выполняет Tesseract с русской моделью внутри контейнера
индексатора: текст материалов не покидает стенд.
"""
import re

LANGUAGE = "rus"
RENDER_DPI = 300
MIN_LETTERS = 40          # меньше букв — страница пуста или декоративна
MIN_WORDS = 5             # столько русских слов должно быть на странице
MIN_WORD_SHARE = 0.6      # доля русских слов среди слов от трёх букв

_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_CYRILLIC_WORD = re.compile(r"^[а-яё]{3,}$")


def _prepare(image):
    """Оттенки серого и растянутый контраст: цветной фон слайда мешает распознаванию."""
    from PIL import ImageOps

    return ImageOps.autocontrast(image.convert("L"))


def invert(image):
    """Обратить цвета: светлый текст на тёмном фоне."""
    from PIL import ImageOps

    return ImageOps.invert(image.convert("L"))


def recognize(image) -> str:
    """Распознать изображение страницы. Возвращает текст без завершающих пробелов."""
    import pytesseract   # импорт по месту: в образе bot распознавания нет

    return pytesseract.image_to_string(_prepare(image), lang=LANGUAGE).strip()


def is_readable(text: str) -> bool:
    """Пригодна ли страница для поиска: достаточно букв и осмысленных слов.

    Числа, номера актов, сокращения и предлоги («ФЗ», «№ 7», «п. 2») в долю не
    входят: в правовых текстах их много, и страница с перечнем актов читаема.
    """
    if sum(ch.isalpha() for ch in text) < MIN_LETTERS:
        return False
    # Слова от трёх букв, без чисел: только они показывают, текст это или шум.
    words = [t.lower() for t in _TOKEN.findall(text) if len(t) >= 3 and t.isalpha()]
    good = sum(bool(_CYRILLIC_WORD.match(w)) for w in words)
    return good >= MIN_WORDS and good / len(words) >= MIN_WORD_SHARE
