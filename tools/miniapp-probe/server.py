"""Минимальный сервер этапа 0: только статика пробы и проверка подписи."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from launch_params import LaunchError, verify_launch

ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 32 * 1024


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    launch_query: str = Field(min_length=1, max_length=MAX_BODY_BYTES)


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def safe_response(request: Request, call_next):
        content_length = request.headers.get("content-length", "0")
        if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            return _error(413, "payload_too_large")
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return _error(413, "payload_too_large")
        origin = os.environ.get("MINIAPP_PUBLIC_ORIGIN", "")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            presented = request.headers.get("origin")
            # WebView VK может не передать Origin либо передать origin контейнера.
            # Он не является удостоверением личности: обязательная HMAC-подпись ниже.
            allowed = {origin, "https://vk.com", "https://vk.ru", "null", ""}
            if presented is not None and presented not in allowed:
                return _error(403, "invalid_origin")
            length = request.headers.get("content-length")
            if request.method == "POST" and (length is None or not length.isdigit() or int(length) > MAX_BODY_BYTES):
                return _error(413, "payload_too_large")
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "POST, GET"
            response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors https://vk.com https://*.vk.com "
            "https://vk.ru https://*.vk.ru"
        )
        return response

    @app.post("/api/probe/session")
    async def session(request: Request):
        try:
            body = LaunchBody.model_validate(await request.json())
            expected = int(os.environ["MINIAPP_APP_ID"])
            launch = verify_launch(
                body.launch_query,
                secret=os.environ["MINIAPP_APP_SECRET"],
                expected_app_id=expected,
            )
        except LaunchError as error:
            return _error(401 if error.code == "launch_expired" else 422, error.code)
        except (ValueError, KeyError, TypeError):
            return _error(422, "invalid_input")
        return {"verified": True, "app_id": launch.app_id, "user_id": launch.user_id}

    @app.post("/api/probe/verify")
    async def verify_again(request: Request):
        # Только контрольная повторная проверка владельцем; сессий не создаёт.
        return await session(request)

    @app.get("/api/probe/health")
    async def health():
        return {"ready": True}

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return _error(404, "not_found")
        return _error(404, "not_found")

    @app.exception_handler(405)
    async def method_not_allowed(request: Request, exc):
        return _error(405, "method_not_allowed")

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc):
        # Не отражать исключение, тело, query или headers в ответ/журнал.
        return _error(500, "service_unavailable")

    app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="probe")
    return app


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code}})


app = create_app()
