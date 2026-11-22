"""Разбор расписания лекций и ведение перечня лекций.

Расписание — статья VK с таблицей из трёх граф: дата, тема лекции, дополнительные
материалы. Строка раздела не имеет даты. Лекция без ссылки ещё в разработке.
Пометка «(Клуб)» после ссылки относится к этой ссылке, а не ко всей строке.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser

import httpx

COMMUNITY_ID = -95098598
MAX_MISSED = 2                       # строка, не найденная столько обновлений подряд, осиротела
_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_MARK = re.compile(r"\((?:клуб|для всех)\)", re.I)
_QUOTES = str.maketrans("", "", "«»\"“”„'")


class ScheduleError(ValueError):
    """Страница не разобрана: перечень лекций не меняется."""


@dataclass(frozen=True)
class RowLink:
    post_id: int
    role: str          # lecture — тема лекции; review — дополнительный материал
    title: str
    club: bool


@dataclass
class ScheduleRow:
    key: str
    when: date
    section: str
    title: str
    links: list[RowLink] = field(default_factory=list)
    other_links: int = 0             # ссылки не на публикации сообщества

    @property
    def state(self) -> str:
        return "has_materials" if self.links else "planned"


def row_key(when: date, title: str) -> str:
    """Ключ строки: дата и нормализованное название. Ссылка в ключ не входит —
    она появляется позже самой строки."""
    name = " ".join(title.translate(_QUOTES).lower().split()).rstrip(" .")
    return f"{when.isoformat()} {name}"[:200]


class _Table(HTMLParser):
    """Собирает строки таблицы: в каждой ячейке — абзацы из кусков текста и ссылок."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._paragraph, self._href = [], None, None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "p" and self._cell is not None:
            self._paragraph = []
        elif tag == "a" and self._paragraph is not None:
            self._href = dict(attrs).get("href") or ""
            self._paragraph.append(["link", self._href, ""])

    def handle_endtag(self, tag):
        if tag == "a":
            self._href = None
        elif tag == "p" and self._paragraph is not None and self._cell is not None:
            self._cell.append(self._paragraph)
            self._paragraph = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._paragraph is None:
            return
        if self._href is not None:
            self._paragraph[-1][2] += data
        else:
            self._paragraph.append(["text", data])


def _cell_text(cell) -> str:
    return " ".join(" ".join(piece[-1] for paragraph in cell for piece in paragraph).split())


def _post_id(href: str, community_id: int) -> int | None:
    match = re.search(rf"wall{community_id}_(\d+)", href.replace("%2D", "-"))
    return int(match.group(1)) if match else None


def _links(cell, role: str, community_id: int) -> tuple[list[RowLink], int]:
    links, other = [], 0
    for paragraph in cell:
        for index, piece in enumerate(paragraph):
            if piece[0] != "link":
                continue
            post_id = _post_id(piece[1], community_id)
            if post_id is None:
                other += 1
                continue
            # Пометка относится к ссылке, за которой стоит, — до следующей ссылки.
            tail = ""
            for following in paragraph[index + 1:]:
                if following[0] == "link":
                    break
                tail += following[-1]
            links.append(RowLink(post_id, role, " ".join(piece[2].split()), "(клуб)" in tail.lower()))
    return links, other


def parse_schedule(html: str, *, community_id: int = COMMUNITY_ID) -> list[ScheduleRow]:
    """Разобрать страницу расписания в строки лекций."""
    table = _Table()
    table.feed(html)
    rows, section, seen = [], "", {}
    for cells in table.rows:
        if len(cells) < 2:
            continue
        match = _DATE.match(_cell_text(cells[0]))
        # Пометка аудитории в ячейке темы относится к ссылке и в название строки не входит.
        title = " ".join(_MARK.sub(" ", _cell_text(cells[1])).split())
        if not match:
            # Строка без даты — заголовок раздела (или шапка таблицы).
            if title and not _cell_text(cells[0]):
                section = title
            continue
        if not title:
            continue
        day, month, year = (int(x) for x in match.groups())
        try:
            when = date(year, month, day)
        except ValueError:
            continue
        key = row_key(when, title)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key} #{seen[key]}"
        row = ScheduleRow(key, when, section, title)
        main, other_main = _links(cells[1], "lecture", community_id)
        extra, other_extra = _links(cells[2], "review", community_id) if len(cells) > 2 else ([], 0)
        taken = set()
        for link in main + extra:
            if link.post_id not in taken:      # одна публикация в строке учитывается один раз
                taken.add(link.post_id)
                row.links.append(link)
        row.other_links = other_main + other_extra
        rows.append(row)
    if not rows:
        raise ScheduleError("В странице не найдено ни одной строки с датой лекции.")
    return rows


def fetch_schedule(url: str, *, transport: httpx.BaseTransport | None = None) -> str:
    """Загрузить страницу расписания. Она открыта без входа; токен не нужен."""
    try:
        with httpx.Client(transport=transport, timeout=40, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise ScheduleError(f"Расписание не загружено ({type(exc).__name__}).") from None
    if response.status_code != 200:
        raise ScheduleError(f"Расписание не загружено: HTTP {response.status_code}.")
    return response.text


def sync_schedule(conn, rows: list[ScheduleRow]) -> dict:
    """Обновить перечень лекций и их публикации одной транзакцией. Повтор не создаёт дублей."""
    counts = {"total": len(rows), "with_materials": 0, "planned": 0, "new": 0, "new_links": 0,
              "orphaned": 0, "relinked": 0}
    with conn.transaction():
        conn.execute("LOCK TABLE lecture IN SHARE ROW EXCLUSIVE MODE")
        keys = []
        for row in rows:
            keys.append(row.key)
            counts["with_materials" if row.links else "planned"] += 1
            lecture = conn.execute(
                """
                INSERT INTO lecture (schedule_row, title, lecture_date, section, state, missed, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, 0, now())
                ON CONFLICT (schedule_row) DO UPDATE SET
                    title = excluded.title, lecture_date = excluded.lecture_date, section = excluded.section,
                    state = excluded.state, missed = 0, last_seen_at = now()
                RETURNING id, (xmax = 0) AS inserted
                """,
                (row.key, row.title, row.when, row.section, row.state),
            ).fetchone()
            counts["new"] += bool(lecture["inserted"])
            posts = [link.post_id for link in row.links]
            conn.execute("DELETE FROM lecture_post WHERE lecture_id = %s AND NOT (post_id = ANY(%s))",
                         (lecture["id"], posts))
            for link in row.links:
                inserted = conn.execute(
                    """
                    INSERT INTO lecture_post (lecture_id, post_id, row_role, title, schedule_club)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (lecture_id, post_id) DO UPDATE SET
                        row_role = excluded.row_role, title = excluded.title, schedule_club = excluded.schedule_club
                    RETURNING (xmax = 0) AS inserted
                    """,
                    (lecture["id"], link.post_id, link.role, link.title, link.club),
                ).fetchone()["inserted"]
                counts["new_links"] += bool(inserted)

        # Строка расписания, не найденная два обновления подряд, считается осиротевшей.
        conn.execute("UPDATE lecture SET missed = missed + 1 "
                     "WHERE lecture_date IS NOT NULL AND NOT (schedule_row = ANY(%s))", (keys,))
        counts["orphaned"] = conn.execute(
            "UPDATE lecture SET state = 'orphaned' WHERE lecture_date IS NOT NULL AND missed >= %s "
            "AND NOT (schedule_row = ANY(%s))", (MAX_MISSED, keys)).rowcount

        # Материалы, опубликованные до появления расписания (условная строка функции 001),
        # привязываются к настоящей строке по номеру публикации.
        placeholders = conn.execute(
            "SELECT ml.id, ml.material_id, ml.post_id FROM material_link ml "
            "JOIN lecture old ON old.id = ml.lecture_id WHERE old.lecture_date IS NULL").fetchall()
        for link in placeholders:
            target = conn.execute(
                "SELECT lp.lecture_id FROM lecture_post lp JOIN lecture l ON l.id = lp.lecture_id "
                "WHERE lp.post_id = %s AND l.state <> 'orphaned' "
                "ORDER BY (lp.row_role = 'lecture') DESC, l.lecture_date, l.id LIMIT 1",
                (link["post_id"],)).fetchone()
            if target is None:
                continue
            duplicate = conn.execute(
                "SELECT 1 FROM material_link WHERE lecture_id = %s AND material_id = %s AND post_id = %s",
                (target["lecture_id"], link["material_id"], link["post_id"])).fetchone()
            if duplicate:
                conn.execute("DELETE FROM material_link WHERE id = %s", (link["id"],))
            else:
                conn.execute("UPDATE material_link SET lecture_id = %s WHERE id = %s",
                             (target["lecture_id"], link["id"]))
                counts["relinked"] += 1
        conn.execute("DELETE FROM lecture l WHERE l.lecture_date IS NULL "
                     "AND NOT EXISTS (SELECT 1 FROM material_link ml WHERE ml.lecture_id = l.id) "
                     "AND l.schedule_row = 'pilot-control'")
    return counts
