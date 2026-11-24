"""Поиск по заголовкам доступного каталога без переиндексации фрагментов."""
from ecoprofi.bot.search import find_candidates
from tests.fixtures import docs_corpus as corpus


def test_lecture_title_contributes_a_candidate_with_russian_morphology(docs):
    docs.owner.execute(
        "UPDATE lecture SET title = %s WHERE title = %s",
        ("Лекция 4. Виды негативного воздействия на окружающую среду",
         "Лекция 1. Плата за негативное воздействие"),
    )
    answer = docs.ask("club", "виды воздействия")
    assert corpus.SCAN in docs.doc_ids(answer)


def test_attachment_title_rewards_proximity_and_requires_two_words(docs):
    for item_id, title in [(corpus.TEXT, "Виды воздействия"),
                           (corpus.SCAN, "Виды прямого и косвенного воздействия")]:
        docs.owner.execute("UPDATE material SET title = %s WHERE item_type = 'doc' AND item_id = %s",
                           (title, item_id))

    def scores(query):
        hits = find_candidates(docs.owner, docs.ai, query, ("public", "club")).catalog
        return {candidate.title: candidate.score for candidate in hits}

    matched = scores("вид воздействия")
    assert matched["Виды воздействия"] > matched["Виды прямого и косвенного воздействия"]
    assert "Виды воздействия" not in scores("виды отходов")
