"""FastAPI: POST /api/search — внутренний контракт адаптера (contracts/local-dialog.md)."""
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from ecoprofi import telemetry
from ecoprofi.ai import AIError, YandexAI
from ecoprofi.bot import cards as cards_module
from ecoprofi.bot import events
from ecoprofi.bot.access import Forbidden, Unauthorized, check_service_key, resolve_access, visible_versions
from ecoprofi.bot.outcomes import decide
from ecoprofi.bot.search import find_candidates
from ecoprofi.db import connect

MAX_RECHECKS = 3


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


def answer(conn, ai, request: SearchRequest) -> dict:
    """Обработать событие: права, поиск, сборка карточек, перепроверка перед возвратом."""
    operation_id = telemetry.new_operation_id()
    started = time.monotonic()
    resolve_access(conn, request.group_id, request.peer_id, request.user_id)
    state = events.register(conn, request.group_id, request.event_id, request.peer_id,
                            request.user_id, request.text)

    outcome, cards, tokens = "empty", [], 0
    search_ms = generation_ms = 0
    versions: dict[int, int] = {}
    for _ in range(MAX_RECHECKS):
        # Права читаются заново при каждом проходе, включая уточнения и повторы.
        access = resolve_access(conn, request.group_id, request.peer_id, request.user_id)
        mark = time.monotonic()
        decision = decide(find_candidates(conn, ai, state.effective_query, access.audiences))
        search_ms = round((time.monotonic() - mark) * 1000)

        mark = time.monotonic()
        explained, tokens = cards_module.explanations(ai, state.effective_query, decision.cards)
        generation_ms = round((time.monotonic() - mark) * 1000)

        # Перед возвратом: права и опубликованные версии не изменились за время обработки.
        current = resolve_access(conn, request.group_id, request.peer_id, request.user_id)
        visible = visible_versions(conn, current.audiences)
        versions = {c.material_id: c.version_id for c in decision.cards}
        if current.audiences == access.audiences and all(visible.get(m) == v for m, v in versions.items()):
            outcome, cards = decision.outcome, cards_module.build_cards(decision.cards, explained)
            break
    else:
        # Данные менялись при каждой попытке: безопасный отказ без карточек.
        outcome, cards, versions = "empty", [], {}

    if state.first:
        events.update_context(conn, request.group_id, request.peer_id, request.user_id,
                              outcome, state.effective_query)
    with conn.transaction():
        telemetry.record(
            conn, operation_id, "bot", outcome, event_id=request.event_id, repeat=not state.first,
            materials=sorted(versions), versions=sorted(set(versions.values())),
            search_ms=search_ms, generation_ms=generation_ms, tokens=tokens,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return {
        "event_id": request.event_id, "peer_id": request.peer_id, "outcome": outcome,
        "text": cards_module.message_text(outcome, cards), "cards": cards,
        "random_id": state.random_id, "operation_id": operation_id,
    }


def create_app(*, connect_fn=None, ai=None, service_key: str | None = None) -> FastAPI:
    app = FastAPI(title="Экопрофи: поиск эпизода лекции", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.connect = connect_fn or connect
    app.state.ai = ai

    def get_ai():
        if app.state.ai is None:
            app.state.ai = YandexAI.from_env()
        return app.state.ai

    def service_key_checked(x_service_key: str | None = Header(default=None)):
        try:
            check_service_key(x_service_key, service_key)
        except Unauthorized:
            raise HTTPException(status_code=401, detail="Неверный служебный ключ.") from None

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/search", dependencies=[Depends(service_key_checked)])
    def search(request: SearchRequest):
        try:
            with app.state.connect() as conn:
                # Права проверяются до обращения к модели и до любого поиска.
                resolve_access(conn, request.group_id, request.peer_id, request.user_id)
                return answer(conn, get_ai(), request)
        except Forbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        except AIError as exc:
            # Временная недоступность: адаптер повторит тот же event_id.
            raise HTTPException(status_code=503, detail=str(exc)) from None

    @app.post("/api/delivered", dependencies=[Depends(service_key_checked)])
    def delivered(request: DeliveredRequest):
        with app.state.connect() as conn:
            events.mark_delivered(conn, request.group_id, request.event_id)
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = create_app()
