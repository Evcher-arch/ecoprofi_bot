"""Детерминированная подмена AI Studio для проверок без сети."""
import hashlib
import math
import re

from ecoprofi.ai import Explanation
from ecoprofi.db import EMBEDDING_DIM

# Грубое усечение окончаний: достаточно, чтобы словоформы фикстур совпадали.
_ENDINGS = ("иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ых", "их", "ые", "ие",
            "ая", "яя", "ое", "ее", "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев",
            "ую", "юю", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь")


def stems(text: str) -> list[str]:
    words = re.findall(r"[а-яa-z0-9]+", text.casefold().replace("ё", "е"))
    result = []
    for word in words:
        if len(word) <= 2:
            continue
        for ending in _ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 4:
                word = word[: -len(ending)]
                break
        result.append(word)
    return result


def vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    values = [0.0] * dim
    for stem in stems(text):
        index = int.from_bytes(hashlib.sha256(stem.encode()).digest()[:4], "big") % dim
        values[index] += 1.0
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        values[0] = 1e-6  # нулевой вектор недопустим для косинусного расстояния
        return values
    return [v / norm for v in values]


class FakeAI:
    doc_model = "fake://doc/1"
    query_model = "fake://query/1"
    completion_model = "fake://gpt/1"
    dim = EMBEDDING_DIM

    def __init__(self):
        self.explain_calls: list[dict] = []   # что именно получила «модель»
        self.foreign_id: int | None = None    # подмешать посторонний идентификатор
        self.fail_embed = False
        self.limit = None                     # код HTTP лимита/отказа ключа (AILimit)
        self.embedded = 0                     # число векторизованных текстов
        self.before_explain = None            # вызывается перед ответом, для проверки гонок

    def embed_documents(self, texts):
        if self.fail_embed:
            from ecoprofi.ai import AIError
            raise AIError("fake outage")
        if self.limit:
            from ecoprofi.ai import AILimit
            raise AILimit(self.limit)
        self.embedded += len(texts)
        return [vector(t) for t in texts]

    def embed_query(self, text):
        return vector(text)

    def explain(self, question, candidates):
        self.explain_calls.append({"question": question, "candidates": [dict(c) for c in candidates]})
        if self.before_explain:
            self.before_explain()
        by_id = {c["id"]: f"Пояснение к «{c['title']}»" for c in candidates}
        if self.foreign_id is not None:
            by_id[self.foreign_id] = "Посторонний кандидат"
        return Explanation(by_id, tokens=7 * len(candidates))
