"""Сборка карточек и текста ответа (contracts/search-cards.md).

Цитата, название, лекция, место в источнике и ссылка берутся сервером из
каталога — из одной версии материала. Модель даёт только пояснение и только для
кандидатов текущего поиска; посторонние идентификаторы отбрасываются.
"""
from ecoprofi.ai import AIError

DEFAULT_EXPLANATION = "В выдержке есть слова из вашего вопроса."
NAVIGATION_EXPLANATION = "Совпадение по описанию записи. Поиск по речи для неё пока недоступен."
MATERIAL_EXPLANATION = "Номер документа из вопроса есть в названии или реквизитах материала."
REQUISITES_NOTE = "реквизиты не подтверждены"
CLARIFY_TEXT = ("Точного совпадения не нашлось. Уточните, пожалуйста: какой документ, "
                "вид воздействия или тема вас интересует?")
CLARIFY_LECTURES = ("Подходящие материалы есть в нескольких лекциях:\n{lectures}\n"
                    "Уточните, пожалуйста, лекцию или вид материала: презентация, письмо, запись.")
EMPTY_TEXT = "В доступных вам материалах подходящий материал не найден."
MAX_MESSAGE = 4000
_WITH_EXCERPT = ("episode", "page", "letter", "document")
_KIND_NAME = {"presentation": "Презентация", "letter": "Письмо", "document": "Документ", "video": "Запись"}


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
    quoted = [c for c in candidates if c.kind in _WITH_EXCERPT and c.text]
    if not quoted:
        return {}, 0
    try:
        result = ai.explain(question, [
            {"id": c.material_id, "title": c.title, "excerpt": c.text} for c in quoted
        ])
    except AIError:
        return {}, 0
    allowed = {c.material_id for c in quoted}
    return {k: v for k, v in result.by_id.items() if k in allowed}, result.tokens


def _lecture(c) -> dict | None:
    if c.lecture_id is None:
        return None
    return {"title": c.lecture_title, "date": c.lecture_date.isoformat() if c.lecture_date else None}


def _requisites(c) -> dict | None:
    """Реквизиты показываются только подтверждённые."""
    if c.req_state != "confirmed" or not c.req_number:
        return None
    return {"date": c.req_date.isoformat() if c.req_date else None, "number": c.req_number,
            "sender": c.req_sender}


def build_cards(candidates, explained: dict[int, str]) -> list[dict]:
    cards = []
    for c in candidates:
        card = {"type": c.kind, "material_id": c.material_id, "title": c.title, "lecture": _lecture(c),
                "explanation": explained.get(c.material_id, DEFAULT_EXPLANATION), "url": c.url}
        if c.kind == "episode":
            card.update(excerpt=c.text, start_ms=c.start_ms, end_ms=c.end_ms,
                        url=episode_url(c.url, c.start_ms))
        elif c.kind == "page":
            card.update(excerpt=c.text, page=c.page_no, pages_total=c.pages_total, recognized=c.recognized)
        elif c.kind == "letter":
            requisites = _requisites(c)
            card.update(excerpt=c.text, requisites=requisites,
                        requisites_note=None if requisites else REQUISITES_NOTE)
            if c.page_no is not None:
                card.update(page=c.page_no, pages_total=c.pages_total, recognized=c.recognized)
        elif c.kind == "document":
            card.update(excerpt=c.text)
        elif c.kind == "material":
            reason = MATERIAL_EXPLANATION if c.by_number else "Совпадение по названию лекции или вложения."
            card.update(explanation=reason, material_kind=c.material_kind)
        else:
            card.update(explanation=NAVIGATION_EXPLANATION)
        cards.append(card)
    return cards


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    year, month, day = iso.split("-")
    return f"{day}.{month}.{year}"


def _place(card: dict) -> str | None:
    """Строка о месте в источнике."""
    kind = card["type"]
    if kind == "episode":
        return f"Эпизод {interval(card['start_ms'], card['end_ms'])}"
    page = None
    if card.get("page"):
        page = f"стр. {card['page']}" + (f" из {card['pages_total']}" if card.get("pages_total") else "")
        if card.get("recognized"):
            page += " (текст распознан)"
    if kind == "page":
        return f"Презентация, {page}"
    if kind == "letter":
        req = card.get("requisites")
        head = (f"Письмо от {_date(req['date'])} № {req['number']}" if req and req.get("date")
                else f"Письмо № {req['number']}" if req else f"Письмо, {REQUISITES_NOTE}")
        return f"{head}, {page}" if page else head
    if kind == "document":
        return "Документ"
    if kind == "material":
        return f"{_KIND_NAME.get(card.get('material_kind'), 'Материал')} целиком"
    return None


FOUND_LEAD = "Вот что нашлось в доступных вам материалах:"


def lead_text(outcome: str, cards: list[dict], text: str) -> str:
    """Текст ответа для Mini App: карточки рисуются отдельно, перечень в тексте их дублировал бы."""
    if outcome == "found" and cards:
        return FOUND_LEAD
    return text


def message_text(outcome: str, cards: list[dict], options=()) -> str:
    if outcome == "clarify":
        if options:
            return CLARIFY_LECTURES.format(lectures="\n".join(f"• {name}" for name in options))
        return CLARIFY_TEXT
    if outcome == "empty" or not cards:
        return EMPTY_TEXT
    parts = [FOUND_LEAD]
    for number, card in enumerate(cards, 1):
        lines = [f"{number}. {card['title']}"]
        lecture = card.get("lecture")
        if lecture and lecture.get("title") and lecture["title"] != card["title"]:
            when = f" · {_date(lecture['date'])}" if lecture.get("date") else ""
            lines.append(f"Лекция: {lecture['title']}{when}")
        place = _place(card)
        if place:
            lines.append(place)
        if card.get("excerpt"):
            lines.append(f"«{card['excerpt']}»")
        lines.append(card["explanation"])
        lines.append(card["url"])
        parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"
