"""Пользовательские DTO и маршруты Mini App v1."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from ecoprofi import telemetry
from ecoprofi.bot.access import Forbidden, NotSubscribed
from ecoprofi.bot.club import ClubUnavailable
from ecoprofi.bot.miniapp.auth import LaunchError, verify_launch
from ecoprofi.bot.miniapp.config import BODY_LIMIT, MiniAppConfig, utcnow
from ecoprofi.bot.miniapp.repository import ApiProblem, MiniAppRepository
from ecoprofi.bot.miniapp import telemetry as mini_telemetry


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    launch_query: str = Field(min_length=1, max_length=BODY_LIMIT)


class DialogBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dialog_id: uuid.UUID


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    text: str = Field(min_length=1, max_length=2000)


_RECORDER = {"connect": None}


def _record_error(operation_id: str, code: str, status: int) -> None:
    """Ошибка до регистрации получает собственный operation_id; пишутся только код и HTTP-статус."""
    connect = _RECORDER["connect"]
    if connect is None:
        return
    try:
        with connect() as conn:
            with conn.transaction():
                mini_telemetry.record(conn, operation_id, "error", event="error", error_code=f"{status}:{code}")
    except Exception:
        pass        # диагностика вспомогательна и не меняет ответ


def error_response(status, code, message, *, retryable=False, club_url=""):
    from fastapi.responses import JSONResponse
    operation_id = telemetry.new_operation_id()
    _record_error(operation_id, code, status)
    content = {"error": {"code": code, "message": message, "retryable": retryable},
               "operation_id": operation_id}
    if club_url and code == "not_subscribed":
        content["club_url"] = club_url
    return JSONResponse(content, status_code=status, headers={"Cache-Control": "no-store"})


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer ") or not value[7:].strip():
        raise ApiProblem(401, "invalid_session", "Войдите в приложение заново.")
    return value[7:].strip()


def build_router(config: MiniAppConfig, repository: MiniAppRepository) -> APIRouter:
    router = APIRouter(prefix="/api/miniapp/v1")

    @router.post("/session", status_code=201)
    def login(body: LaunchBody):
        try:
            launch = verify_launch(body.launch_query, secret=config.secret, expected_app_id=config.app_id)
        except LaunchError as error:
            raise ApiProblem(401, error.code, "Не удалось подтвердить запуск. Откройте приложение заново.") from None
        repository.access(launch.user_id)
        if launch.user_id not in getattr(repository.club, "exempt", ()):
            repository.alert(False)      # успешная проверка VK закрывает причину club
        return repository.create_session(launch)

    @router.post("/session/resume")
    def resume(body: LaunchBody, authorization: str | None = Header(default=None)):
        token = _bearer(authorization)
        try:
            launch = verify_launch(body.launch_query, secret=config.secret, expected_app_id=config.app_id, check_age=False)
        except LaunchError:
            raise ApiProblem(401, "resume_mismatch", "Откройте приложение заново из VK.") from None
        return repository.resume(token, launch)

    @router.delete("/session", status_code=204)
    def logout(authorization: str | None = Header(default=None)):
        repository.delete_session(_bearer(authorization))
        return None

    @router.post("/dialogs")
    def new_dialog(body: DialogBody, authorization: str | None = Header(default=None)):
        from fastapi.responses import JSONResponse
        result, created = repository.new_dialog(_bearer(authorization), body.dialog_id)
        result["dialog_id"] = str(result["dialog_id"])
        return JSONResponse(result, status_code=201 if created else 200)

    @router.post("/dialogs/{dialog_id}/requests")
    def submit(dialog_id: uuid.UUID, body: RequestBody, authorization: str | None = Header(default=None)):
        from fastapi.responses import JSONResponse
        result, accepted = repository.submit(_bearer(authorization), dialog_id, body.request_id, body.text)
        return JSONResponse(result, status_code=202 if accepted else 200)

    @router.get("/dialogs/{dialog_id}/requests/{request_id}")
    def status(dialog_id: uuid.UUID, request_id: uuid.UUID, authorization: str | None = Header(default=None)):
        return repository.get_request(_bearer(authorization), dialog_id, request_id)

    @router.get("/dialogs/{dialog_id}/history")
    def history(dialog_id: uuid.UUID, after_sequence: int = 0, limit: int = 50,
                authorization: str | None = Header(default=None)):
        if after_sequence < 0 or not 1 <= limit <= 50:
            raise ApiProblem(422, "invalid_input", "Некорректные параметры истории.")
        return repository.history(_bearer(authorization), dialog_id, after_sequence, limit)

    return router


PREFIX = "/api/miniapp/v1"


def _miniapp(request) -> bool:
    return request.url.path.startswith(PREFIX)


def register_exception_handlers(app, config: MiniAppConfig, repository: MiniAppRepository):
    """Обработчики действуют только для маршрутов Mini App; старый /api/search не меняется."""
    _RECORDER["connect"] = repository.connect
    from fastapi.exceptions import RequestValidationError
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.responses import JSONResponse

    def other(request, exc):
        if isinstance(exc, RequestValidationError):
            return request_validation_exception_handler(request, exc)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    def problem(request, exc):
        return error_response(exc.status, exc.code, exc.message, retryable=exc.retryable, club_url=config.club_url)

    def subscribed(request, exc):
        if not _miniapp(request):
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return error_response(403, "not_subscribed", "Поиск доступен подписчикам клуба Экопрофи.",
                              club_url=config.club_url)

    def forbidden(request, exc):
        if not _miniapp(request):
            return JSONResponse({"detail": str(exc)}, status_code=403)
        return error_response(403, "forbidden", "Доступ запрещён.")

    def unavailable(request, exc):
        if not _miniapp(request):
            return JSONResponse({"detail": str(exc)}, status_code=503)
        repository.alert(True)
        return error_response(503, "membership_unavailable", "Проверка подписки временно недоступна.", retryable=True)

    async def invalid(request, exc):
        # Без поля input: тело запроса (launch_query, текст) не отражается в ответе.
        if not _miniapp(request):
            return await other(request, exc)
        return error_response(422, "invalid_input", "Некорректный запрос.")

    def unexpected(request, exc):
        if not _miniapp(request):
            return other(request, exc)
        return error_response(503, "service_unavailable", "Сервис временно недоступен.", retryable=True)

    app.exception_handler(ApiProblem)(problem)
    app.exception_handler(NotSubscribed)(subscribed)
    app.exception_handler(Forbidden)(forbidden)
    app.exception_handler(ClubUnavailable)(unavailable)
    app.exception_handler(RequestValidationError)(invalid)
    app.exception_handler(Exception)(unexpected)


def miniapp_middleware(config: MiniAppConfig):
    async def protect(request: Request, call_next):
        if not _miniapp(request):
            return await call_next(request)
        origin = request.headers.get("origin")
        if request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            if origin and origin != config.origin:
                return error_response(403, "invalid_origin", "Недопустимый источник запроса.")
            if origin is None and request.headers.get("sec-fetch-site") == "cross-site":
                return error_response(403, "invalid_origin", "Недопустимый источник запроса.")
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > BODY_LIMIT:
                return error_response(413, "payload_too_large", "Слишком большой запрос.")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    return protect
