"""Разбор WebVTT и сборка эпизодов.

Координаты хранятся в миллисекундах. Реплики, перекрывающиеся или стыкующиеся по
времени, часто повторяют хвост предыдущей строки («бегущие» автоматические
субтитры VK показывают каждую строку в двух-трёх репликах подряд) — такой повтор
устраняется. Реплика, начавшаяся после паузы, сохраняется целиком, даже если
текст совпадает: это действительный повтор речи.
"""
import html
import re
from dataclasses import dataclass

GAP_MS = 5_000        # пауза, начинающая новый эпизод
EPISODE_MS = 24_000   # реплика, начавшаяся позже этого срока от начала эпизода, открывает новый
TOUCH_MS = 100        # реплика, начавшаяся не позже этого срока после предыдущей, считается её продолжением
TAIL_WORDS = 60       # сколько последних слов сравнивается с началом следующей реплики

_STAMP = r"(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})"
_TIMING = re.compile(rf"^{_STAMP}[ \t]+-->[ \t]+{_STAMP}.*$", re.M)


class SubtitleParseError(ValueError):
    """Дорожка не является пригодным WebVTT."""


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Episode:
    start_ms: int
    end_ms: int
    text: str


def _ms(hours, minutes, seconds, millis) -> int:
    return ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def parse_vtt(raw: bytes) -> list[Cue]:
    """Разобрать WebVTT. Разметка и служебные блоки отбрасываются."""
    body = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if not body.lstrip().startswith("WEBVTT"):
        raise SubtitleParseError("Нет заголовка WEBVTT.")
    cues = []
    for block in re.split(r"\n[ \t]*\n", body):
        if block.lstrip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        match = _TIMING.search(block)
        if not match:
            continue
        groups = match.groups()
        start, end = _ms(*groups[:4]), _ms(*groups[4:])
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", block[match.end():])).split())
        if text and start < end:
            cues.append(Cue(start, end, text))
    cues.sort(key=lambda c: (c.start_ms, c.end_ms))
    return cues


def _norm(word: str) -> str:
    return re.sub(r"[^\w]+", "", word.casefold().replace("ё", "е"))


def _without_overlap(previous: list[str], current: list[str]) -> list[str]:
    """Убрать из начала current слова, повторяющие конец previous."""
    prev = [_norm(w) for w in previous]
    cur = [_norm(w) for w in current]
    for size in range(min(len(prev), len(cur)), 0, -1):
        if prev[-size:] == cur[:size]:
            return current[size:]
    return current


def build_episodes(cues: list[Cue]) -> list[Episode]:
    """Собрать реплики в эпизоды, устранив повторы от перекрытия строк."""
    episodes: list[Episode] = []
    words: list[str] = []
    start = end = 0
    # Хвост непрерывной речи. Он переживает границу эпизода: в «бегущих» дорожках
    # реплика повторяет предыдущую строку и тогда, когда с неё начинается новый эпизод.
    tail: list[str] = []
    tail_end = 0
    for cue in cues:
        cue_words = cue.text.split()
        if tail and cue.start_ms <= tail_end + TOUCH_MS:
            # Перекрытие или стык по времени — искусственный повтор.
            cue_words = _without_overlap(tail, cue_words)
        else:
            tail = []
        tail_end = max(tail_end, cue.end_ms)
        if not cue_words:
            if words:
                end = max(end, cue.end_ms)
            continue
        tail = (tail + cue_words)[-TAIL_WORDS:]
        if words and (cue.start_ms - end > GAP_MS or cue.start_ms - start >= EPISODE_MS):
            episodes.append(Episode(start, end, " ".join(words)))
            words = []
        if not words:
            start, end = cue.start_ms, cue.end_ms
        words.extend(cue_words)
        end = max(end, cue.end_ms)
    if words:
        episodes.append(Episode(start, end, " ".join(words)))
    return episodes


def episodes_from_vtt(raw: bytes) -> list[Episode]:
    return build_episodes(parse_vtt(raw))
