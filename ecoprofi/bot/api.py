"""FastAPI: POST /api/search — внутренний контракт адаптера (contracts/local-dialog.md);
GET /api/alerts/pending и POST /api/alerts/{id}/sent — очередь оповещений оператору
(specs/003-autonomous-indexer/contracts/alerts.md)."""
import hashlib
import logging
import os
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from ecoprofi import telemetry
from ecoprofi.ai import AIError, YandexAI
from ecoprofi.bot import alerts as alert_queue
from ecoprofi.indexer import alerts as alert_causes
from ecoprofi.bot import cards as cards_module
from ecoprofi.bot import events
from ecoprofi.bot.access import (Forbidden, NotSubscribed, Unauthorized, check_service_key, resolve_access,
                                 visible_versions)
from ecoprofi.bot.club import ClubUnavailable
from ecoprofi.bot.runtime import BotRuntime
from ecoprofi.bot.service import search_answer, search_in_context
from ecoprofi.db import connect

MAX_RECHECKS = 3

NOT_SUBSCRIBED_TEXT = ("Поиск по материалам лекций доступен подписчикам клуба Экопрофи. "
                       "Оформите подписку на клуб и задайте вопрос снова.")


def not_subscribed_text() -> str:
    """Отказ неподписчику; CLUB_URL — необязательная ссылка на оформление подписки."""
    url = os.environ.get("CLUB_URL", "").strip()
    return f"{NOT_SUBSCRIBED_TEXT}\n{url}" if url else NOT_SUBSCRIBED_TEXT


def refusal_random_id(group_id: int, event_id: str) -> int:
    """Один random_id на событие: повтор после неясного исхода не даёт второго сообщения."""
    digest = hashlib.sha256(f"club:{group_id}:{event_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31 - 1) + 1


def refuse_not_subscribed(conn, request) -> dict:
    """Ответ пользователю без подписки: только текст про клуб, без поиска и обращения к модели."""
    operation_id = telemetry.new_operation_id()
    with conn.transaction():
        # Подписка отменена между повторами события: отказ уходит с прежним random_id,
        # чтобы VK не доставил второе сообщение, если первая отправка всё же прошла.
        row = conn.execute("SELECT random_id FROM event WHERE group_id = %s AND event_id = %s "
                           "AND peer_id = %s AND user_id = %s",
                           (request.group_id, request.event_id, request.peer_id, request.user_id)).fetchone()
        telemetry.record(conn, operation_id, "bot", "not_subscribed", event_id=request.event_id)
    return {
        "event_id": request.event_id, "peer_id": request.peer_id, "outcome": "not_subscribed",
        "text": not_subscribed_text(), "cards": [],
        "random_id": row["random_id"] if row else refusal_random_id(request.group_id, request.event_id),
        "operation_id": operation_id,
    }


class SearchRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    group_id: int = Field(gt=0)
    peer_id: int
    user_id: int
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("event_id", mode="before")
    @classmethod
    def _event_id_text(cls, value):
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("пустой текст")
        return value


class DeliveredRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    group_id: int = Field(gt=0)


class AlertSentRequest(BaseModel):
    send_no: int = Field(ge=1)


def answer(conn, ai, request: SearchRequest, is_member) -> dict:
    """Обработать событие: права, поиск, сборка карточек, перепроверка перед возвратом."""
    operation_id = telemetry.new_operation_id()
    started = time.monotonic()
    resolve_access(conn, request.group_id, request.peer_id, request.user_id, is_member)
    state = events.register(conn, request.group_id, request.event_id, request.peer_id,
                            request.user_id, request.text)

    outcome, cards, tokens, options = "empty", [], 0, ()
    search_ms = generation_ms = 0
    versions: dict[int, int] = {}
    if state.first:
        found, effective_query = search_in_context(
            conn, ai, state.effective_query, state.context,
            lambda: resolve_access(conn, request.group_id, request.peer_id, request.user_id, is_member),
        )
        outcome, cards, versions = found.outcome, found.cards, found.provenance
        tokens = found.metrics["tokens"]
        search_ms = found.metrics["search_ms"]
        generation_ms = found.metrics["generation_ms"]
        events.set_effective_query(conn, request.group_id, request.event_id, effective_query)
        events.update_context(conn, request.group_id, request.peer_id, request.user_id,
                              outcome, effective_query)
    else:
        found = search_answer(
            conn, ai, state.effective_query,
            lambda: resolve_access(conn, request.group_id, request.peer_id, request.user_id, is_member),
        )
        outcome, cards, versions = found.outcome, found.cards, found.provenance
        tokens = found.metrics["tokens"]
        search_ms = found.metrics["search_ms"]
        generation_ms = found.metrics["generation_ms"]

    with conn.transaction():
        telemetry.record(
            conn, operation_id, "bot", outcome, event_id=request.event_id, repeat=not state.first,
            materials=sorted(versions), versions=sorted(set(versions.values())),
            search_ms=search_ms, generation_ms=generation_ms, tokens=tokens,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return {
        "event_id": request.event_id, "peer_id": request.peer_id, "outcome": outcome,
        "text": cards_module.message_text(outcome, cards, options), "cards": cards,
        "random_id": state.random_id, "operation_id": operation_id,
    }


def create_app(*, connect_fn=None, ai=None, service_key: str | None = None, club=None, miniapp: bool = False,
               miniapp_worker: bool = True) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        # Один процесс uvicorn: worker и обслуживание Mini App запускаются только при полной настройке.
        services = getattr(app.state, "miniapp_services", [])
        for service in services:
            service.start()
        try:
            yield
        finally:
            for service in reversed(services):
                service.stop()

    app = FastAPI(title="Экопрофи: поиск материалов лекций", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.miniapp_services = []
    app.state.connect = connect_fn or connect
    app.state.ai = ai
    app.state.runtime = BotRuntime(connect_fn=app.state.connect, club=club)
    app.state.club = club

    def get_ai():
        if app.state.ai is None:
            app.state.ai = YandexAI.from_env()
        return app.state.ai

    def get_club():
        return app.state.runtime.get_club()

    def club_alert(opened: bool) -> None:
        app.state.runtime.club_alert(opened)

    def service_key_checked(x_service_key: str | None = Header(default=None)):
        try:
            check_service_key(x_service_key, service_key)
        except Unauthorized:
            raise HTTPException(status_code=401, detail="Неверный служебный ключ.") from None

    if miniapp:
        from ecoprofi.bot.miniapp.config import MiniAppConfig
        from ecoprofi.bot.miniapp.repository import MiniAppRepository
        from ecoprofi.bot.miniapp.api import build_router, miniapp_middleware, register_exception_handlers
        config = MiniAppConfig.from_env()
        if config.enabled:
            repository = MiniAppRepository(app.state.connect, club=get_club(), alert=club_alert)
            app.state.miniapp = repository
            app.middleware("http")(miniapp_middleware(config))
            register_exception_handlers(app, config, repository)
            app.include_router(build_router(config, repository))
            if miniapp_worker:
                from ecoprofi.bot.miniapp.worker import Maintenance, MiniAppWorker

                class _LazyAI:
                    def __getattr__(self, name):
                        return getattr(get_ai(), name)

                app.state.miniapp_services = [Maintenance(repository), MiniAppWorker(repository, _LazyAI())]

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/search", dependencies=[Depends(service_key_checked)])
    def search(request: SearchRequest):
        try:
            with app.state.connect() as conn:
                # Права проверяются до обращения к модели и до любого поиска.
                club = get_club()
                resolve_access(conn, request.group_id, request.peer_id, request.user_id, club)
                if request.user_id not in getattr(club, "exempt", ()):
                    club_alert(False)        # исключение VK не запрашивает и о проверке не говорит
                return answer(conn, get_ai(), request, club)
        except NotSubscribed:
            # Подписки нет (или она отменена во время обработки): найденное не выдаётся.
            club_alert(False)
            with app.state.connect() as conn:
                return refuse_not_subscribed(conn, request)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except ClubUnavailable as exc:
            # Подписка не подтверждена — ответа нет; адаптер повторит тот же event_id.
            club_alert(True)
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except AIError as exc:
            # Временная недоступность: адаптер повторит тот же event_id.
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.post("/api/delivered", dependencies=[Depends(service_key_checked)])
    def delivered(request: DeliveredRequest):
        with app.state.connect() as conn:
            events.mark_delivered(conn, request.group_id, request.event_id)
        return {"status": "ok"}

    @app.get("/api/alerts/pending", dependencies=[Depends(service_key_checked)])
    def alerts_pending():
        with app.state.connect() as conn:
            return {"alerts": alert_queue.pending(conn)}

    @app.post("/api/alerts/{alert_id}/sent", dependencies=[Depends(service_key_checked)])
    def alert_sent(alert_id: int, request: AlertSentRequest):
        with app.state.connect() as conn:
            changed = alert_queue.mark_sent(conn, alert_id, request.send_no)
        return {"status": "ok", "changed": changed}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = create_app(miniapp=True)   # маршруты Mini App открываются только при MINIAPP_ENABLED и полных настройках
