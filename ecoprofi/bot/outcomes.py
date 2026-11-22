"""Выбор исхода поиска: found / clarify / empty.

Эпизод считается подтверждённым, когда его текст содержит достаточную долю слов
вопроса. Векторная близость участвует в ранжировании; учитывать её как
самостоятельное подтверждение можно только после калибровки на реальной модели
(переменные SEARCH_SIM_STRONG и SEARCH_SIM_WEAK, по умолчанию выключены).
"""
import os
from dataclasses import dataclass

from ecoprofi.bot.search import Candidate, SearchResult, best_per_material

MAX_CARDS = 3
MIN_COVERAGE = 0.6      # доля слов вопроса в тексте для уверенного совпадения


@dataclass(frozen=True)
class Decision:
    outcome: str                      # found / clarify / empty
    cards: tuple[Candidate, ...] = ()


def _threshold(name: str) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return 2.0   # недостижимое значение: порог выключен


def decide(result: SearchResult) -> Decision:
    sim_strong, sim_weak = _threshold("SEARCH_SIM_STRONG"), _threshold("SEARCH_SIM_WEAK")
    if not result.lexemes:
        # Вопрос без содержательных слов: искать не по чему.
        return Decision("clarify")

    strong = [c for c in result.episodes if c.coverage >= MIN_COVERAGE or c.similarity >= sim_strong]
    episodes = best_per_material(strong)
    taken = {c.material_id for c in episodes}
    # Запись без дорожки предлагается только для навигации и входит в общий лимит.
    navigation = [c for c in result.cards if c.coverage >= MIN_COVERAGE and c.material_id not in taken]
    cards = (episodes + navigation)[:MAX_CARDS]

    if cards:
        # Одно слово, подходящее сразу нескольким записям, не определяет тему.
        if len(result.lexemes) == 1 and len(episodes + navigation) > 1:
            return Decision("clarify")
        return Decision("found", tuple(cards))

    weak = any(c.coverage > 0 or c.similarity >= sim_weak for c in result.episodes) or bool(result.cards)
    return Decision("clarify" if weak else "empty")
