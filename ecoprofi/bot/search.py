"""Гибридный поиск по опубликованным эпизодам.

Оба списка кандидатов — полнотекстовый и векторный — строятся только по
материалам, доступным участнику. Ранги объединяются, совпадения группируются
по материалам, для каждой записи остаётся лучший эпизод.
"""
from dataclasses import dataclass

from ecoprofi.bot.access import VISIBLE_MATERIALS

CANDIDATES_PER_CHANNEL = 50
RRF_K = 60


@dataclass(frozen=True)
class Candidate:
    material_id: int
    version_id: int
    lecture_id: int
    title: str
    url: str
    kind: str                 # episode / navigation
    score: float              # объединённый ранг
    coverage: float           # доля слов вопроса, найденных в тексте
    similarity: float         # косинусная близость к вопросу (для эпизода)
    episode_id: int | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    text: str | None = None


@dataclass(frozen=True)
class SearchResult:
    lexemes: tuple[str, ...]
    episodes: tuple[Candidate, ...]     # все кандидаты-эпизоды, по убыванию ранга
    cards: tuple[Candidate, ...]        # записи без дорожки, совпавшие по описанию


def query_lexemes(conn, text: str) -> tuple[str, ...]:
    """Нормализованные слова вопроса (русская морфология PostgreSQL, без стоп-слов)."""
    row = conn.execute("SELECT tsvector_to_array(to_tsvector('russian', %s)) AS lexemes", (text,)).fetchone()
    return tuple(row["lexemes"] or ())


def _or_query(lexemes) -> str:
    return " | ".join("'" + lex.replace("\\", "\\\\").replace("'", "''") + "'" for lex in lexemes)


_EPISODES = f"""
WITH vis AS ({VISIBLE_MATERIALS}),
fts AS (
    SELECT e.id, row_number() OVER (ORDER BY ts_rank_cd(e.tsv, q.query) DESC, e.id) AS rnk
    FROM episode e
    JOIN vis ON vis.version_id = e.version_id
    CROSS JOIN (SELECT to_tsquery('simple', %(tsquery)s) AS query) q
    WHERE %(has_lexemes)s AND e.tsv @@ q.query
    ORDER BY rnk LIMIT %(limit)s
),
vec AS (
    SELECT e.id, row_number() OVER (ORDER BY e.embedding <=> %(vector)s::vector, e.id) AS rnk
    FROM episode e
    JOIN vis ON vis.version_id = e.version_id
    ORDER BY rnk LIMIT %(limit)s
)
SELECT e.id AS episode_id, e.start_ms, e.end_ms, e.text,
       vis.material_id, vis.version_id, vis.lecture_id, vis.title, vis.url,
       coalesce(1.0 / (%(k)s + fts.rnk), 0) + coalesce(1.0 / (%(k)s + vec.rnk), 0) AS score,
       1 - (e.embedding <=> %(vector)s::vector) AS similarity,
       (SELECT count(*) FROM unnest(%(lexemes)s::text[]) AS l(lex)
         WHERE l.lex = ANY(tsvector_to_array(e.tsv))) AS matched
FROM episode e
JOIN vis ON vis.version_id = e.version_id
LEFT JOIN fts ON fts.id = e.id
LEFT JOIN vec ON vec.id = e.id
WHERE fts.id IS NOT NULL OR vec.id IS NOT NULL
ORDER BY score DESC, e.id
"""

_CARDS = f"""
WITH vis AS ({VISIBLE_MATERIALS})
SELECT vis.material_id, vis.version_id, vis.lecture_id, vis.title, vis.url,
       (SELECT count(*) FROM unnest(%(lexemes)s::text[]) AS l(lex)
         WHERE l.lex = ANY(tsvector_to_array(to_tsvector('russian', vis.title || ' ' || vis.description)))) AS matched
FROM vis
WHERE vis.track_state = 'absent'
ORDER BY matched DESC, vis.material_id
"""


def find_candidates(conn, ai, question: str, audiences: tuple[str, ...]) -> SearchResult:
    """Найти кандидатов в части корпуса, доступной аудиториям audiences."""
    lexemes = query_lexemes(conn, question)
    total = max(len(lexemes), 1)
    params = {
        "audiences": list(audiences), "lexemes": list(lexemes), "has_lexemes": bool(lexemes),
        "tsquery": _or_query(lexemes) if lexemes else "'x'",
        "vector": str(list(ai.embed_query(question))), "limit": CANDIDATES_PER_CHANNEL, "k": RRF_K,
    }
    episodes = tuple(
        Candidate(
            material_id=r["material_id"], version_id=r["version_id"], lecture_id=r["lecture_id"],
            title=r["title"], url=r["url"], kind="episode", score=float(r["score"]),
            coverage=r["matched"] / total if lexemes else 0.0, similarity=float(r["similarity"]),
            episode_id=r["episode_id"], start_ms=r["start_ms"], end_ms=r["end_ms"], text=r["text"],
        )
        for r in conn.execute(_EPISODES, params).fetchall()
    )
    cards = tuple(
        Candidate(
            material_id=r["material_id"], version_id=r["version_id"], lecture_id=r["lecture_id"],
            title=r["title"], url=r["url"], kind="navigation", score=0.0,
            coverage=r["matched"] / total, similarity=0.0,
        )
        for r in conn.execute(_CARDS, {"audiences": list(audiences), "lexemes": list(lexemes)}).fetchall()
        if lexemes and r["matched"]
    )
    return SearchResult(lexemes, episodes, cards)


def best_per_material(candidates, limit: int | None = None) -> list[Candidate]:
    """Лучший кандидат каждой записи — до ограничения выдачи. Порядок входа сохраняется."""
    seen, result = set(), []
    for candidate in candidates:
        if candidate.material_id in seen:
            continue
        seen.add(candidate.material_id)
        result.append(candidate)
        if limit is not None and len(result) == limit:
            break
    return result
