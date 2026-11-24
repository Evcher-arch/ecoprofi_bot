"""Общий поиск без событий, сообщений и транспортного контекста."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ecoprofi.bot import cards as cards_module
from ecoprofi.bot.access import visible_versions
from ecoprofi.bot.outcomes import decide
from ecoprofi.bot.search import find_candidates

MAX_RECHECKS = 3


@dataclass(frozen=True)
class SearchOutcome:
    outcome: str
    text: str
    cards: list[dict]
    provenance: dict[int, int]
    metrics: dict[str, int] = field(default_factory=dict)


def search_answer(conn, ai, question: str, check_access: Callable, *, deadline=None) -> SearchOutcome:
    """Проверять допуск до поиска, перед генерацией и перед выдачей.

    check_access() возвращает Access. Он может использовать общий ClubMembers,
    но не транспортный контекст. Версии проверяются до внешней передачи и выдачи.
    """
    started = time.monotonic()
    search_ms = generation_ms = tokens = 0
    for _ in range(MAX_RECHECKS):
        if deadline is not None:
            deadline.check()
        access = check_access()
        mark = time.monotonic()
        decision = decide(find_candidates(conn, ai, question, access.audiences))
        search_ms = round((time.monotonic() - mark) * 1000)
        if deadline is not None:
            deadline.check()

        # Не передавать модели выдержки, чья опубликованная версия уже недоступна.
        # Только кандидаты с подтверждённой выдержкой требуют перепроверки версии
        # перед генерацией. Navigation/material — карточки каталога без выдержки.
        versions = {c.material_id: c.version_id for c in decision.cards if c.kind in {"episode", "page", "letter", "document"}}
        versions.update(decision.provenance)
        current = check_access()
        visible = visible_versions(conn, current.audiences)
        if current.audiences != access.audiences or any(visible.get(m) != v for m, v in versions.items()):
            continue
        mark = time.monotonic()
        explained, tokens = cards_module.explanations(ai, question, decision.cards)
        generation_ms = round((time.monotonic() - mark) * 1000)
        if deadline is not None:
            deadline.check()

        current = check_access()
        visible = visible_versions(conn, current.audiences)
        versions = {c.material_id: c.version_id for c in decision.cards}
        versions.update(decision.provenance)
        if current.audiences == access.audiences and all(visible.get(m) == v for m, v in versions.items()):
            cards = cards_module.build_cards(decision.cards, explained)[:3]
            return SearchOutcome(
                decision.outcome,
                cards_module.message_text(decision.outcome, cards, decision.options),
                cards,
                versions,
                {"search_ms": search_ms, "generation_ms": generation_ms, "tokens": tokens,
                 "duration_ms": round((time.monotonic() - started) * 1000)},
            )
    # Данные менялись при каждой попытке: безопасный отказ без материалов.
    return SearchOutcome("empty", cards_module.EMPTY_TEXT, [], {},
                         {"search_ms": search_ms, "generation_ms": generation_ms, "tokens": tokens,
                          "duration_ms": round((time.monotonic() - started) * 1000)})
