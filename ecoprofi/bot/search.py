"""Гибридный поиск по опубликованным фрагментам: эпизодам записей, страницам
презентаций и частям писем.

Оба списка кандидатов — полнотекстовый и векторный — строятся только по
материалам, доступным участнику. Ранги объединяются, совпадения группируются
по материалам, для каждого материала остаётся лучший фрагмент. Отдельно
подбираются материалы, чей номер документа назван в вопросе.
"""
from dataclasses import dataclass
from datetime import date

from ecoprofi.bot.access import VISIBLE_MATERIALS
from ecoprofi.textkeys import doc_numbers

CANDIDATES_PER_CHANNEL = 50
RRF_K = 60
# Вид карточки по виду материала, когда совпал фрагмент текста.
_FRAGMENT_CARD = {"video": "episode", "presentation": "page", "letter": "letter", "document": "document"}


@dataclass(frozen=True)
class Candidate:
    material_id: int
    version_id: int
    lecture_id: int | None
    title: str
    url: str
    kind: str                 # episode / page / letter / document / material / navigation
    score: float              # объединённый ранг
    coverage: float           # доля слов вопроса, найденных в тексте
    similarity: float         # косинусная близость к вопросу (для фрагмента)
    episode_id: int | None = None      # идентификатор фрагмента
    start_ms: int | None = None
    end_ms: int | None = None
    text: str | None = None            # выдержка из каталога
    page_no: int | None = None
    pages_total: int | None = None
    recognized: bool = False
    lecture_title: str | None = None
    lecture_date: date | None = None
    material_kind: str = "video"
    req_state: str = "absent"
    req_date: date | None = None
    req_number: str | None = None
    req_sender: str | None = None
    by_number: bool = False            # номер документа из вопроса есть в подписи материала

    @property
    def group(self) -> tuple:
        """Ключ группировки по лекциям; материал без лекции — сам себе группа."""
        return ("l", self.lecture_id) if self.lecture_id is not None else ("m", self.material_id)


@dataclass(frozen=True)
class SearchResult:
    lexemes: tuple[str, ...]
    episodes: tuple[Candidate, ...]     # все кандидаты-фрагменты, по убыванию ранга
    cards: tuple[Candidate, ...]        # записи без дорожки, совпавшие по описанию
    numbered: tuple[Candidate, ...] = ()   # материалы, найденные по номеру документа
    numbers: tuple[str, ...] = ()


def query_lexemes(conn, text: str) -> tuple[str, ...]:
    """Нормализованные слова вопроса (русская морфология PostgreSQL, без стоп-слов)."""
    row = conn.execute("SELECT tsvector_to_array(to_tsvector('russian', %s)) AS lexemes", (text,)).fetchone()
    return tuple(row["lexemes"] or ())


def _or_query(lexemes) -> str:
    return " | ".join("'" + lex.replace("\\", "\\\\").replace("'", "''") + "'" for lex in lexemes)


_VIS_COLUMNS = """vis.material_id, vis.version_id, vis.lecture_id, vis.lecture_title, vis.lecture_date,
       vis.title, vis.url, vis.material_kind, vis.pages_total,
       vis.req_state, vis.req_date, vis.req_number, vis.req_sender, vis.doc_numbers"""

_FRAGMENTS = f"""
WITH vis AS ({VISIBLE_MATERIALS}),
q AS (SELECT to_tsquery('simple', %(tsquery)s) AS query),
fts AS (
    SELECT f.id, row_number() OVER (ORDER BY ts_rank_cd(f.tsv, q.query) DESC, f.id) AS rnk
    FROM fragment f
    JOIN vis ON vis.version_id = f.version_id
    CROSS JOIN q
    WHERE %(has_lexemes)s AND f.tsv @@ q.query
    ORDER BY rnk LIMIT %(limit)s
),
vec AS (
    SELECT f.id, row_number() OVER (ORDER BY f.embedding <=> %(vector)s::vector, f.id) AS rnk
    FROM fragment f
    JOIN vis ON vis.version_id = f.version_id
    ORDER BY rnk LIMIT %(limit)s
)
SELECT f.id AS fragment_id, f.kind AS fragment_kind, f.start_ms, f.end_ms, f.page_no, f.recognized,
       -- Эпизод показывается целиком; из страницы и письма берётся дословный отрывок.
       CASE WHEN f.kind = 'episode' OR NOT %(has_lexemes)s THEN f.text
            ELSE ts_headline('russian', f.text, q.query,
                             'MaxWords=45, MinWords=20, MaxFragments=0, StartSel="", StopSel=""') END AS text,
       {_VIS_COLUMNS},
       coalesce(1.0 / (%(k)s + fts.rnk), 0) + coalesce(1.0 / (%(k)s + vec.rnk), 0) AS score,
       1 - (f.embedding <=> %(vector)s::vector) AS similarity,
       (SELECT count(*) FROM unnest(%(lexemes)s::text[]) AS l(lex)
         WHERE l.lex = ANY(tsvector_to_array(f.tsv))) AS matched
FROM fragment f
JOIN vis ON vis.version_id = f.version_id
CROSS JOIN q
LEFT JOIN fts ON fts.id = f.id
LEFT JOIN vec ON vec.id = f.id
WHERE fts.id IS NOT NULL OR vec.id IS NOT NULL
ORDER BY score DESC, f.id
"""

_CARDS = f"""
WITH vis AS ({VISIBLE_MATERIALS})
SELECT {_VIS_COLUMNS},
       (SELECT count(*) FROM unnest(%(lexemes)s::text[]) AS l(lex)
         WHERE l.lex = ANY(tsvector_to_array(to_tsvector('russian', vis.title || ' ' || vis.description)))) AS matched
FROM vis
WHERE vis.text_state = 'absent' AND vis.material_kind = 'video'
ORDER BY matched DESC, vis.material_id
"""

_NUMBERED = f"""
WITH vis AS ({VISIBLE_MATERIALS})
SELECT {_VIS_COLUMNS}
FROM vis
WHERE vis.doc_numbers && %(numbers)s::text[]
ORDER BY vis.lecture_date NULLS LAST, vis.material_id
"""


def _material_fields(row, numbers) -> dict:
    return dict(
        material_id=row["material_id"], version_id=row["version_id"], lecture_id=row["lecture_id"],
        title=row["title"], url=row["url"], lecture_title=row["lecture_title"],
        lecture_date=row["lecture_date"], material_kind=row["material_kind"],
        pages_total=row["pages_total"], req_state=row["req_state"], req_date=row["req_date"],
        req_number=row["req_number"], req_sender=row["req_sender"],
        by_number=bool(set(numbers) & set(row["doc_numbers"] or ())),
    )


def find_candidates(conn, ai, question: str, audiences: tuple[str, ...]) -> SearchResult:
    """Найти кандидатов в части корпуса, доступной аудиториям audiences."""
    lexemes = query_lexemes(conn, question)
    numbers = tuple(doc_numbers(question))
    total = max(len(lexemes), 1)
    params = {
        "audiences": list(audiences), "lexemes": list(lexemes), "has_lexemes": bool(lexemes),
        "tsquery": _or_query(lexemes) if lexemes else "'x'",
        "vector": str(list(ai.embed_query(question))), "limit": CANDIDATES_PER_CHANNEL, "k": RRF_K,
    }
    fragments = tuple(
        Candidate(
            kind=_FRAGMENT_CARD[r["material_kind"]], score=float(r["score"]),
            coverage=r["matched"] / total if lexemes else 0.0, similarity=float(r["similarity"]),
            episode_id=r["fragment_id"], start_ms=r["start_ms"], end_ms=r["end_ms"], text=r["text"],
            page_no=r["page_no"], recognized=r["recognized"], **_material_fields(r, numbers),
        )
        for r in conn.execute(_FRAGMENTS, params).fetchall()
    )
    cards = tuple(
        Candidate(kind="navigation", score=0.0, coverage=r["matched"] / total, similarity=0.0,
                  **_material_fields(r, numbers))
        for r in conn.execute(_CARDS, {"audiences": list(audiences), "lexemes": list(lexemes)}).fetchall()
        if lexemes and r["matched"]
    )
    numbered = ()
    if numbers:
        numbered = tuple(
            Candidate(kind="material", score=0.0, coverage=1.0, similarity=0.0, **_material_fields(r, numbers))
            for r in conn.execute(_NUMBERED, {"audiences": list(audiences), "numbers": list(numbers)}).fetchall()
        )
    return SearchResult(lexemes, fragments, cards, numbered, numbers)


def best_per_material(candidates, limit: int | None = None) -> list[Candidate]:
    """Лучший кандидат каждого материала — до ограничения выдачи. Порядок входа сохраняется."""
    seen, result = set(), []
    for candidate in candidates:
        if candidate.material_id in seen:
            continue
        seen.add(candidate.material_id)
        result.append(candidate)
        if limit is not None and len(result) == limit:
            break
    return result
