"""Общие ключи: номера документов и ключи материалов.

Номер документа — слово с цифрой и дефисом или косой чертой («2909-р»,
«ВЧ-09-03-34/3616», «2-ТП»). Номера сравниваются точно: «2909-р» и «2909» —
разные документы. То же правило применяет миграция 002 к названиям записей.
"""
import re

_NUMBER = re.compile(r"[0-9a-zа-яё]+(?:[-/][0-9a-zа-яё]+)+")
_KEY = re.compile(r"^(video|doc)?(-?\d+)_(\d+)$")


def doc_numbers(*texts: str | None) -> list[str]:
    """Номера документов из текстов, в нижнем регистре, без повторов."""
    found = set()
    for text in texts:
        for match in _NUMBER.findall((text or "").lower()):
            if any(ch.isdigit() for ch in match):
                found.add(match)
    return sorted(found)


def material_key(item_type: str, owner_id: int, item_id: int) -> str:
    """Ключ материала для отчётов и файла политики: video-95098598_456 или doc177_686."""
    return f"{item_type}{owner_id}_{item_id}"


def parse_material_key(key: str) -> tuple[str, int, int] | None:
    """Разобрать ключ. Ключ без префикса (файл политики версии 1) считается записью."""
    match = _KEY.match(str(key).strip())
    if not match:
        return None
    return match.group(1) or "video", int(match.group(2)), int(match.group(3))
