"""Выбор исхода поиска: found / clarify / empty.

Фрагмент считается подтверждённым, когда его текст содержит достаточную долю слов
вопроса. Материал подтверждается и без фрагмента, если номер документа из вопроса
точно совпал с номером в названии или подтверждённых реквизитах. Векторная
близость участвует в ранжировании; учитывать её как самостоятельное подтверждение
можно только после калибровки на реальной модели (переменные SEARCH_SIM_STRONG и
SEARCH_SIM_WEAK, по умолчанию выключены).
"""
import os
from dataclasses import dataclass, field, replace

from ecoprofi.bot.search import Candidate, SearchResult, best_per_material

MAX_CARDS = 3
MAX_PER_LECTURE = 2     # карточек одной лекции, пока подтверждены материалы других лекций
MIN_COVERAGE = 0.6      # доля слов вопроса в тексте для уверенного совпадения
LECTURE_CLOSE = 0.9     # отношение оценок лекций, при котором они неразличимы
MAX_OPTIONS = 5         # сколько лекций назвать в уточнении


@dataclass(frozen=True)
class Decision:
    outcome: str                      # found / clarify / empty
    cards: tuple[Candidate, ...] = ()
    options: tuple[str, ...] = ()     # названия лекций для уточнения — только доступные участнику
    provenance: dict[int, int] = field(default_factory=dict)  # источники и версии, включая options


def _threshold(name: str, default: float = 2.0) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default   # 2.0 — недостижимое значение: порог выключен


def _cap_per_lecture(ordered: list[Candidate]) -> list[Candidate]:
    """До трёх карточек; одна лекция не занимает все места, если есть другие."""
    taken, per_group, skipped = [], {}, []
    for candidate in ordered:
        if per_group.get(candidate.group, 0) >= MAX_PER_LECTURE:
            skipped.append(candidate)
            continue
        per_group[candidate.group] = per_group.get(candidate.group, 0) + 1
        taken.append(candidate)
        if len(taken) == MAX_CARDS:
            break
    taken = (taken + skipped)[:MAX_CARDS]
    return _mix_kinds(taken, ordered)


def _is_recording(candidate: Candidate) -> bool:
    return candidate.kind == "episode"


def _mix_kinds(taken: list[Candidate], ordered: list[Candidate]) -> list[Candidate]:
    """Не занимать все места одним видом: если выбраны только документы, а есть подтверждённый
    эпизод записи (или наоборот), последнее место отдаётся лучшему кандидату другого вида."""
    with_place = [c for c in taken if c.kind not in ("material", "navigation")]
    if len(taken) < MAX_CARDS or len(with_place) < len(taken) or taken[-1].by_number:
        return taken
    if len({_is_recording(c) for c in taken}) > 1:
        return taken
    chosen = {c.material_id for c in taken}
    other = next((c for c in ordered if c.material_id not in chosen and c.kind not in ("material", "navigation")
                  and _is_recording(c) != _is_recording(taken[0])), None)
    return taken[:-1] + [other] if other else taken


def _too_many_close_lectures(fragments: list[Candidate], close: float) -> tuple[str, ...]:
    """Названия лекций, если подтверждённых лекций больше, чем карточек, и они неразличимы."""
    best: dict[tuple, Candidate] = {}
    for candidate in fragments:
        best.setdefault(candidate.group, candidate)   # вход отсортирован по убыванию ранга
    if len(best) <= MAX_CARDS:
        return ()
    ranked = list(best.values())
    top, edge = ranked[0].score, ranked[MAX_CARDS].score
    if top <= 0 or edge / top < close:
        return ()
    names = []
    for candidate in ranked[:MAX_OPTIONS]:
        name = candidate.lecture_title or candidate.title
        if name not in names:
            names.append(name)
    return tuple(names)


def decide(result: SearchResult) -> Decision:
    sim_strong, sim_weak = _threshold("SEARCH_SIM_STRONG"), _threshold("SEARCH_SIM_WEAK")
    if not result.lexemes and not result.numbered:
        # Вопрос без содержательных слов и без номера документа: искать не по чему.
        return Decision("clarify")

    strong = [c for c in result.episodes if c.coverage >= MIN_COVERAGE or c.similarity >= sim_strong]
    fragments = best_per_material(strong)
    catalog = {c.material_id: c for c in result.catalog}
    fragments = [replace(c, score=c.score + catalog[c.material_id].score)
                 if c.material_id in catalog else c for c in fragments]
    taken = {c.material_id for c in fragments}
    # Материал, найденный по номеру документа, без подтверждённого места в тексте
    # выдаётся целиком: без страницы, реквизитов и выдержки.
    numbered = [c for c in result.numbered if c.material_id not in taken]
    taken |= {c.material_id for c in numbered}
    titles = [c for c in result.catalog if c.coverage >= MIN_COVERAGE and c.material_id not in taken
              and all(c.material_id != navigation.material_id for navigation in result.cards)]
    taken.update(c.material_id for c in titles)
    # Запись без дорожки предлагается только для навигации и входит в общий лимит.
    navigation = [c for c in result.cards if c.coverage >= MIN_COVERAGE and c.material_id not in taken]

    # Сначала материалы, чей номер назван в вопросе, затем остальные подтверждённые.
    ranked = sorted([c for c in fragments if not c.by_number] + titles,
                    key=lambda c: c.score, reverse=True)
    ordered = [c for c in fragments if c.by_number] + numbered + ranked + navigation
    if ordered:
        if not result.numbered:
            # Однословный вопрос, подтверждённый в нескольких материалах, получает лучшие
            # карточки; уточнение — только когда подтверждённые лекции неразличимы по рангу.
            options = _too_many_close_lectures(ranked, _threshold("SEARCH_LECTURE_CLOSE", LECTURE_CLOSE))
            if options:
                provenance = {c.material_id: c.version_id for c in ranked if c.lecture_title or c.title}
                return Decision("clarify", options=options, provenance=provenance)
        return Decision("found", tuple(_cap_per_lecture(ordered)))

    weak = any(c.coverage > 0 or c.similarity >= sim_weak for c in result.episodes) or bool(result.cards)
    return Decision("clarify" if weak or result.catalog else "empty")
