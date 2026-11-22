"""Перечень лекций для редактора: python -m ecoprofi.indexer --report /out/lectures.md

В отчёте нет текста материалов, ключей доступа и адресов файлов — только
названия, идентификаторы VK, состояния и аудитории.
"""
from ecoprofi.bot.access import MATERIAL_AUDIENCE
from ecoprofi.textkeys import material_key

_KIND = {"video": "запись", "presentation": "презентация", "letter": "письмо", "document": "документ"}
_AUDIENCE = {None: "не определена", "public": "для всех", "club": "клуб", "unknown": "закрыт до решения"}
_STATE = {"planned": "в разработке", "has_materials": "с материалами", "orphaned": "нет в расписании"}

_MATERIALS = f"""
    SELECT a.id, a.item_type, a.owner_id, a.item_id, a.kind, a.title, a.state, a.decision, a.seen_signal,
           a.signal, a.audience, a.req_state, a.req_date, a.req_number,
           v.text_state, v.pages_total, v.pages_unreadable,
           (SELECT count(*) FROM fragment f WHERE f.version_id = v.id) AS fragments
    FROM ({MATERIAL_AUDIENCE}) a
    LEFT JOIN material_version v ON v.id = a.published_version_id
"""


def _cell(value) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ")


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines + [""]


def build_report(conn) -> str:
    lectures = conn.execute(
        "SELECT l.id, l.schedule_row, l.title, l.lecture_date, l.section, l.state, "
        "(pl.schedule_row IS NOT NULL) AS in_pilot "
        "FROM lecture l LEFT JOIN pilot_lecture pl ON pl.schedule_row = l.schedule_row "
        "ORDER BY l.lecture_date NULLS FIRST, l.id").fetchall()
    posts = conn.execute(
        "SELECT lecture_id, post_id, row_role, title, schedule_club FROM lecture_post ORDER BY lecture_id, "
        "(row_role = 'lecture') DESC, post_id").fetchall()
    materials = {m["id"]: m for m in conn.execute(_MATERIALS).fetchall()}
    links = conn.execute(
        "SELECT lecture_id, material_id, post_id, link_type, paid_feed FROM material_link "
        "ORDER BY lecture_id, post_id, material_id").fetchall()
    jobs = conn.execute(
        "SELECT item_type, owner_id, item_id, post_id, state, attempt, failure_reason FROM index_job "
        "WHERE state IN ('retry', 'failed') OR failure_reason = 'unsupported' "
        "ORDER BY item_type, owner_id, item_id").fetchall()

    def key(m) -> str:
        return material_key(m["item_type"], m["owner_id"], m["item_id"])

    counts = {state: sum(l["state"] == state for l in lectures) for state in _STATE}
    out = ["# Перечень лекций и материалов", "",
           f"Строк расписания: {len(lectures)} — с материалами {counts['has_materials']}, "
           f"в разработке {counts['planned']}, нет в расписании {counts['orphaned']}. "
           f"В выборке пилота: {sum(l['in_pilot'] for l in lectures)}. Материалов в каталоге: {len(materials)}.", ""]

    out += ["## Выборка пилота", ""]
    pilot = [l for l in lectures if l["in_pilot"]]
    out += _table(["Ключ строки", "Дата", "Лекция"],
                  [[l["schedule_row"], l["lecture_date"], l["title"]] for l in pilot]) if pilot else ["Не задана.", ""]

    out += ["## Материалы по строкам расписания", ""]
    rows = []
    for link in links:
        m = materials.get(link["material_id"])
        lecture = next((l for l in lectures if l["id"] == link["lecture_id"]), None)
        if m is None or lecture is None:
            continue
        pages = ""
        if m["pages_total"] is not None:
            pages = f"{m['pages_total']}" + (f", нечитаемых {m['pages_unreadable']}" if m["pages_unreadable"] else "")
        rows.append([lecture["lecture_date"], lecture["title"], _KIND.get(m["kind"], m["kind"]), m["title"],
                     key(m), "вложение" if link["link_type"] == "attachment" else "упоминание",
                     link["post_id"], {True: "да", False: "нет"}.get(link["paid_feed"], "не проверена"),
                     pages, m["fragments"],
                     _AUDIENCE.get(m["signal"], m["signal"]), _AUDIENCE.get(m["decision"], m["decision"]),
                     _AUDIENCE.get(m["audience"], m["audience"]) if m["state"] == "active" else m["state"]])
    out += _table(["Дата", "Лекция", "Вид", "Материал", "Ключ материала", "Связь", "Публикация", "Платная лента",
                   "Страниц", "Фрагментов", "По расписанию", "Решение редактора", "Действует"], rows) \
        if rows else ["Материалов нет.", ""]

    out += ["## Расхождения аудитории", "",
            "Решение редактора не совпадает с расписанием и принято до изменения расписания. "
            "До нового решения действует более строгая аудитория.", ""]
    conflicts = [m for m in materials.values()
                 if m["decision"] and m["signal"] and m["decision"] != m["signal"] and m["seen_signal"] != m["signal"]]
    out += _table(["Материал", "Ключ материала", "По расписанию", "Решение редактора", "Действует"],
                  [[m["title"], key(m), _AUDIENCE[m["signal"]], _AUDIENCE[m["decision"]],
                    _AUDIENCE.get(m["audience"])] for m in conflicts]) if conflicts else ["Нет.", ""]

    out += ["## Реквизиты не подтверждены", ""]
    unverified = [m for m in materials.values() if m["req_state"] == "unverified"]
    out += _table(["Материал", "Ключ материала", "Распознано"],
                  [[m["title"], key(m), f"{m['req_date'] or ''} № {m['req_number'] or ''}"] for m in unverified]) \
        if unverified else ["Нет.", ""]

    out += ["## Нечитаемые страницы", ""]
    unreadable = [m for m in materials.values() if m["pages_unreadable"]]
    out += _table(["Материал", "Ключ материала", "Страниц", "Нечитаемых"],
                  [[m["title"], key(m), m["pages_total"], m["pages_unreadable"]] for m in unreadable]) \
        if unreadable else ["Нет.", ""]

    out += ["## Сбои чтения и неподдерживаемые форматы", ""]
    out += _table(["Ключ материала", "Публикация", "Состояние", "Причина", "Попыток"],
                  [[material_key(j["item_type"], j["owner_id"], j["item_id"]), j["post_id"], j["state"],
                    j["failure_reason"], j["attempt"]] for j in jobs]) if jobs else ["Нет.", ""]

    out += ["## Строки, которых нет в расписании", ""]
    orphaned = [l for l in lectures if l["state"] == "orphaned"]
    out += _table(["Ключ строки", "Лекция"], [[l["schedule_row"], l["title"]] for l in orphaned]) \
        if orphaned else ["Нет.", ""]

    out += ["## Все строки расписания", ""]
    by_lecture: dict[int, list] = {}
    for post in posts:
        mark = " (Клуб)" if post["schedule_club"] else ""
        role = "лекция" if post["row_role"] == "lecture" else "доп."
        by_lecture.setdefault(post["lecture_id"], []).append(f"{post['post_id']} {role}{mark}")
    out += _table(["Дата", "Раздел", "Лекция", "Состояние", "Публикации", "Ключ строки"],
                  [[l["lecture_date"], l["section"], l["title"], _STATE.get(l["state"], l["state"]),
                    "; ".join(by_lecture.get(l["id"], [])), l["schedule_row"]] for l in lectures])
    return "\n".join(out)
