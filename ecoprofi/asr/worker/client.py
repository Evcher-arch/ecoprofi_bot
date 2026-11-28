"""HTTPS-клиент распознавателя к /asr/v1/ (contracts/asr-api.md).

Связь всегда открывает ПК. Сбой сети, 5xx и 429 повторяются с
экспоненциальной задержкой от 5 секунд до 5 минут; ответы 4xx возвращаются
вызывающему. Ключ передаётся только в заголовке и не пишется в журнал.
"""
import gzip
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

from ecoprofi.asr import CLIENT_BACKOFF_MAX, CLIENT_BACKOFF_MIN

log = logging.getLogger("asr-worker")
# httpx пишет в журнал адрес каждого запроса, а в нём полный lease_id: только предупреждения.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TOKEN_FILE = os.environ.get("ASR_TOKEN_FILE", "/run/secrets/asr_worker_token")


def read_token(path: str = TOKEN_FILE) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"Файл ключа распознавателя пуст: {path}")
    return token


class Response:
    def __init__(self, status: int, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    @property
    def code(self) -> str | None:
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), dict):
            return self.body["error"].get("code")
        return None


class AsrClient:
    def __init__(self, base_url: str, token: str, *, http: httpx.Client | None = None, sleep=time.sleep,
                 backoff_min: float = CLIENT_BACKOFF_MIN, backoff_max: float = CLIENT_BACKOFF_MAX):
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": "asr-worker"}
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False)
        self._sleep = sleep
        self._min, self._max = backoff_min, backoff_max
        self.connection_failures = 0

    def _url(self, path: str) -> str:
        if path.startswith("/asr/v1/"):
            path = path[len("/asr/v1"):]
        return self.base + "/" + path.lstrip("/")

    def _retrying(self, send):
        delay = self._min
        while True:
            try:
                response = send()
            except httpx.HTTPError as exc:
                self.connection_failures += 1
                log.warning("сервер недоступен (%s), повтор через %.0f с", type(exc).__name__, delay)
            else:
                if response.status_code < 500 and response.status_code != 429:
                    return response
                self.connection_failures += 1
                log.warning("сервер ответил %s, повтор через %.0f с", response.status_code, delay)
            self._sleep(delay)
            delay = min(delay * 2, self._max)

    def call(self, method: str, path: str, body: dict | None = None, *, compress: bool = False) -> Response:
        headers = dict(self._headers)
        content = None
        if body is not None:
            content = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
            if compress:
                content = gzip.compress(content)
                headers["Content-Encoding"] = "gzip"
        response = self._retrying(lambda: self._http.request(method, self._url(path), content=content,
                                                             headers=headers))
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = None
        return Response(response.status_code, data, response.headers)

    def download(self, path: str, target: Path, *, should_stop=None) -> Response:
        """Скачать звук с докачкой по Range. 202 — сервер ещё готовит, вызывающий ждёт Retry-After."""
        headers = dict(self._headers)
        have = target.stat().st_size if target.exists() else 0
        if have:
            headers["Range"] = f"bytes={have}-"

        def send():
            request = self._http.build_request("GET", self._url(path), headers=headers)
            return self._http.send(request, stream=True)

        response = self._retrying(send)
        try:
            if response.status_code not in (200, 206):
                response.read()
                try:
                    data = response.json() if response.content else None
                except ValueError:
                    data = None
                return Response(response.status_code, data, response.headers)
            mode = "ab" if response.status_code == 206 else "wb"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open(mode) as handle:
                for chunk in response.iter_bytes(1 << 16):
                    handle.write(chunk)
            return Response(response.status_code, None, response.headers)
        except httpx.HTTPError:
            # Обрыв посреди файла: следующий вызов докачает по Range.
            return Response(599, None, {})
        finally:
            response.close()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
