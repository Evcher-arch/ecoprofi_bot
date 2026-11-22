"""Сборка карточек и текста ответа.

Цитата, название, координаты и ссылка берутся сервером из каталога — из одной
версии материала. Модель даёт только пояснение и только для кандидатов текущего
поиска; посторонние идентификаторы отбрасываются.
"""
from ecoprofi.ai import AIError

DEFAULT_EXPLANATION = "В выдержке есть слова из вашего вопроса."
NAVIGATION_EXPLANATION = "Совпадение по описанию записи. Поиск по речи для неё пока недоступен."
CLARIFY_TEXT = ("Точного совпадения не нашлось. Уточните, пожалуйста: какой документ, "
                "вид воздействия или тема вас интересует?")
EMPTY_TEXT = "В доступных вам материалах подходящий эпизод не найден."
MAX_MESSAGE = 4000


def episode_url(url: str, start_ms: int) -> str:
    """Ссылка с позицией начала; начало округляется вниз до целой секунды."""
    return f"{url}?t={start_ms // 1000}s"


def clock(ms: int, *, up: bool = False) -> str:
    seconds = -(-ms // 1000) if up else ms // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def interval(start_ms: int, end_ms: int) -> str:
    """Интервал для показа: начало вниз, конец вверх до целой секунды."""
    return f"{clock(start_ms)}–{clock(end_ms, up=True)}"


def explanations(ai, question: str, candidates) -> tuple[dict[int, str], int]:
    """Пояснения модели по material_id. Сбой модели не срывает ответ."""
    episodes = [c for c in candidates if c.kind == "episode"]
    if not episodes:
        return {}, 0
    try:
        result = ai.explain(question, [
            {"id": c.material_id, "title": c.title, "excerpt": c.text} for c in episodes
        ])
    except AIError:
        return {}, 0
    allowed = {c.material_id for c in episodes}
    return {k: v for k, v in result.by_id.items() if k in allowed}, result.tokens


def build_cards(candidates, explained: dict[int, str]) -> list[dict]:
    cards = []
    for c in candidates:
        if c.kind == "episode":
            cards.append({
                "type": "episode", "material_id": c.material_id, "title": c.title,
                "explanation": explained.get(c.material_id, DEFAULT_EXPLANATION),
                "excerpt": c.text, "start_ms": c.start_ms, "end_ms": c.end_ms,
                "url": episode_url(c.url, c.start_ms),
            })
        else:
            cards.append({
                "type": "navigation", "material_id": c.material_id, "title": c.title,
                "explanation": NAVIGATION_EXPLANATION, "url": c.url,
            })
    return cards


def message_text(outcome: str, cards: list[dict]) -> str:
    if outcome == "clarify":
        return CLARIFY_TEXT
    if outcome == "empty" or not cards:
        return EMPTY_TEXT
    parts = ["Вот что нашлось в доступных вам материалах:"]
    for number, card in enumerate(cards, 1):
        lines = [f"{number}. {card['title']}"]
        if card["type"] == "episode":
            lines.append(f"Эпизод {interval(card['start_ms'], card['end_ms'])}")
            lines.append(f"«{card['excerpt']}»")
        lines.append(card["explanation"])
        lines.append(card["url"])
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"
