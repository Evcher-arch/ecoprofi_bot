"""T027/T056: allowlist шлюза Mini App на запущенном изолированном gateway.

Запуск (без bot и рабочей БД; bot заменён недоступным адресом, поэтому проксируемые
маршруты дают 502, а закрытые — 404/405 от самого шлюза):

    docker network create ecoprofi-gateway-check
    docker run --rm -d --name ecoprofi-gateway-check \
        --network ecoprofi-gateway-check --network-alias bot \
        -p 127.0.0.1:18080:8080 ecoprofi-miniapp-gateway:local
    MINIAPP_GATEWAY_URL=http://127.0.0.1:18080 python -m pytest tests/integration/test_miniapp_gateway.py
    docker stop ecoprofi-gateway-check
    docker network rm ecoprofi-gateway-check

Нужна пользовательская сеть: nginx разрешает bot через DNS Docker 127.0.0.11.
Alias bot ведёт к самому шлюзу, где порт 8000 закрыт; --add-host не заменяет DNS.

Без MINIAPP_GATEWAY_URL проверка пропускается; пропуск не считается прохождением.
"""
import os

import httpx
import pytest

URL = os.environ.get("MINIAPP_GATEWAY_URL", "")
ID = "11111111-1111-1111-1111-111111111111"
pytestmark = pytest.mark.skipif(not URL, reason="нужен запущенный gateway (MINIAPP_GATEWAY_URL)")


def _call(method, path, **kwargs):
    return httpx.request(method, URL + path, timeout=10, **kwargs)


def test_allowed_routes_are_proxied():
    # 502: шлюз пропустил запрос к bot (bot в проверке недоступен).
    for method, path in [("POST", "/api/miniapp/v1/session"), ("DELETE", "/api/miniapp/v1/session"),
                         ("POST", "/api/miniapp/v1/session/resume"), ("POST", "/api/miniapp/v1/dialogs"),
                         ("POST", f"/api/miniapp/v1/dialogs/{ID}/requests"),
                         ("GET", f"/api/miniapp/v1/dialogs/{ID}/requests/{ID}"),
                         ("GET", f"/api/miniapp/v1/dialogs/{ID}/history")]:
        assert _call(method, path).status_code == 502, (method, path)


def test_wrong_methods_rejected():
    for method, path in [("GET", "/api/miniapp/v1/session"), ("DELETE", f"/api/miniapp/v1/dialogs/{ID}/history"),
                         ("PUT", "/api/miniapp/v1/dialogs"), ("POST", f"/api/miniapp/v1/dialogs/{ID}/history")]:
        assert _call(method, path).status_code == 405, (method, path)


def test_service_routes_closed_even_with_service_key():
    headers = {"X-Service-Key": "artificial-service-key"}
    for method, path in [("POST", "/api/search"), ("POST", "/api/delivered"), ("GET", "/api/alerts/pending"),
                         ("POST", "/api/alerts/1/sent"), ("GET", "/docs"), ("GET", "/openapi.json"),
                         ("GET", "/health"), ("GET", "/api/miniapp/v1/unknown"),
                         ("GET", "/api/miniapp/v1/dialogs/not-a-uuid/history")]:
        response = _call(method, path, headers=headers)
        assert response.status_code == 404, (method, path)
        assert "<html" not in response.text.lower() or not path.startswith("/api/"), path


def test_static_headers_allow_vk_iframe():
    response = _call("GET", "/")
    assert response.status_code == 200 and "<div id=\"root\">" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors https://vk.com" in response.headers["content-security-policy"]
    assert "x-frame-options" not in response.headers


def test_body_limit():
    assert _call("POST", "/api/miniapp/v1/session", content=b"x" * 40_000).status_code == 413


def test_rate_limit():
    # 10 запросов/с на IP с burst 20: одновременная серия сверх лимита получает 429 без материалов.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=60) as pool:
        responses = list(pool.map(lambda _: _call("POST", "/api/miniapp/v1/dialogs"), range(60)))
    limited = [r for r in responses if r.status_code == 429]
    assert limited and all(r.json()["error"]["code"] == "rate_limited" for r in limited)
