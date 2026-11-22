import json

import httpx
import pytest

from ecoprofi.ai import AIError, YandexAI, parse_explanations


def client(handler, **kwargs):
    return YandexAI(folder_id="folder", api_key="secret-key", doc_model="text-search-doc/latest",
                    query_model="text-search-query/latest", completion_model="yandexgpt-5.1",
                    transport=httpx.MockTransport(handler), **kwargs)


def test_embeddings_use_separate_models_and_api_key():
    seen = []

    def handler(request):
        seen.append((request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"embedding": [0.5] * 256, "numTokens": "3"})

    ai = client(handler)
    assert len(ai.embed_documents(["текст"])[0]) == 256
    assert len(ai.embed_query("вопрос")) == 256
    assert seen[0] == ("Api-Key secret-key", {"modelUri": "emb://folder/text-search-doc/latest", "text": "текст"})
    assert seen[1][1]["modelUri"] == "emb://folder/text-search-query/latest"
    assert ai.doc_model == "emb://folder/text-search-doc/latest"


def test_wrong_dimension_is_rejected():
    ai = client(lambda request: httpx.Response(200, json={"embedding": [0.1] * 128}))
    with pytest.raises(AIError, match="128"):
        ai.embed_query("вопрос")


def test_http_error_does_not_leak_key():
    ai = client(lambda request: httpx.Response(401, json={"message": "bad key secret-key"}))
    with pytest.raises(AIError) as info:
        ai.embed_query("вопрос")
    assert "secret-key" not in str(info.value)


def test_explain_passes_only_given_candidates_and_parses_json():
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        content = 'Ответ: {"7": "Про взвешенные вещества.", "x": "мусор"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"total_tokens": 42}})

    result = client(handler).explain("вопрос", [{"id": 7, "title": "Разбор", "excerpt": "цитата"}])
    assert result.by_id == {7: "Про взвешенные вещества."}
    assert result.tokens == 42
    assert sent["model"] == "gpt://folder/yandexgpt-5.1"
    assert "цитата" in sent["messages"][1]["content"]


@pytest.mark.parametrize("content", ["", "нет json", "{не json}", "[1, 2]", '{"1": 5}'])
def test_garbage_explanations_are_ignored(content):
    assert parse_explanations(content) == {}
