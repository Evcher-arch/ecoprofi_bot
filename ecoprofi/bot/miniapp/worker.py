"""Один обработчик постоянной очереди Mini App с fencing attempt_id."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

from ecoprofi.bot.miniapp import telemetry as mini_telemetry
from ecoprofi.ai import AIError
from ecoprofi.bot import cards as cards_module
from ecoprofi.bot.access import NotSubscribed
from ecoprofi.bot.club import ClubUnavailable
from ecoprofi.bot.miniapp import config
from ecoprofi.bot.miniapp.config import ATTEMPT_LIMIT, BUDGET_SECONDS
from ecoprofi.bot.miniapp.repository import MiniAppRepository
from ecoprofi.bot.service import MAX_QUERY, search_in_context

LOCK_ID = 827450
log = logging.getLogger("ecoprofi.bot.miniapp.worker")


STAGE_TIMEOUT = 30.0


class AttemptDeadline:
    def __init__(self, started, seconds=BUDGET_SECONDS):
        self.end = started + timedelta(seconds=seconds)

    def check(self):
        if config.utcnow() >= self.end:
            raise TimeoutError("attempt budget exceeded")

    def remaining(self) -> float:
        return max(0.0, (self.end - config.utcnow()).total_seconds())

    def stage(self) -> float:
        """Тайм-аут сетевой стадии: не более 30 с и не более остатка бюджета."""
        self.check()
        return min(STAGE_TIMEOUT, self.remaining())


class BudgetAI:
    """Каждый вызов AI получает тайм-аут min(30 с, остаток) и проверку срока до и после."""

    def __init__(self, ai, deadline: AttemptDeadline):
        self._ai, self._deadline = ai, deadline

    def _client(self):
        timeout = self._deadline.stage()
        return self._ai.with_timeout(timeout) if hasattr(self._ai, "with_timeout") else self._ai

    def embed_query(self, text):
        result = self._client().embed_query(text)
        self._deadline.check()
        return result

    def explain(self, question, candidates):
        result = self._client().explain(question, candidates)
        self._deadline.check()
        return result

    def __getattr__(self, name):
        return getattr(self._ai, name)


class MiniAppWorker:
    def __init__(self, repository: MiniAppRepository, ai, *, wake_interval=1.0):
        self.repository = repository
        self.ai = ai
        self.wake_interval = wake_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miniapp-worker", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)
            self._thread = None

    def wake(self):
        self._wake.set()

    def _run(self):
        # Внешний цикл: после сбоя соединения worker не умирает, а заново берёт singleton lock.
        while not self._stop.is_set():
            try:
                self._serve()
            except Exception as exc:
                # Не записывать SQL/DSN/тексты внешних вызовов в лог — только тип.
                log.warning("Mini App worker: перезапуск после сбоя (%s)", type(exc).__name__)
            self._stop.wait(5)

    def _serve(self):
        connection = self.repository.connect()
        connection.autocommit = True
        try:
            if not connection.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK_ID,)).fetchone()["locked"]:
                return          # lock у другого процесса: этот worker очередь не обслуживает
            self.recover()
            while not self._stop.is_set():
                # Утрата соединения с lock прекращает новые claims: исключение выводит из цикла.
                connection.execute("SELECT 1")
                if not self.run_once():
                    self._wake.wait(self.wake_interval)
                    self._wake.clear()
        finally:
            try:
                connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            except Exception:
                pass
            connection.close()

    def recover(self):
        with self.repository.connect() as conn:
            with conn.transaction():
                conn.execute("UPDATE miniapp_request SET state = 'retryable_failed', error_code = 'interrupted', "
                             "attempt_id = gen_random_uuid(), finished_at = %s WHERE state = 'running'", (config.utcnow(),))

    def run_once(self) -> bool:
        with self.repository.connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT r.*, s.id AS session_id, s.user_id, s.expires_at, s.revoked_at, "
                    "d.state AS dialog_state, s.current_dialog_id FROM miniapp_request r "
                    "JOIN miniapp_dialog d ON d.id = r.dialog_id JOIN miniapp_session s ON s.id = d.session_id "
                    "WHERE r.state = 'queued' AND r.attempt_no < %s ORDER BY r.queued_at, r.sequence LIMIT 1 FOR UPDATE OF r SKIP LOCKED",
                    (ATTEMPT_LIMIT,)).fetchone()
                if not row:
                    return False
                now = config.utcnow()
                if row["revoked_at"] or row["expires_at"] <= now or row["dialog_state"] != "active" or row["current_dialog_id"] != row["dialog_id"]:
                    conn.execute("UPDATE miniapp_request SET state = 'cancelled', attempt_id = gen_random_uuid(), "
                                 "result = NULL, provenance = '[]'::jsonb, finished_at = %s "
                                 "WHERE dialog_id = %s AND request_id = %s", (now, row["dialog_id"], row["request_id"]))
                    return True
                if row["queued_at"] < now - timedelta(seconds=BUDGET_SECONDS):
                    conn.execute("UPDATE miniapp_request SET state = 'retryable_failed', error_code = 'queue_timeout', "
                                 "attempt_id = gen_random_uuid(), finished_at = %s WHERE dialog_id = %s AND request_id = %s",
                                 (now, row["dialog_id"], row["request_id"]))
                    return True
                attempt_id = uuid.uuid4()
                conn.execute("UPDATE miniapp_request SET state = 'running', attempt_id = %s, attempt_no = attempt_no + 1, "
                             "started_at = %s, error_code = NULL WHERE dialog_id = %s AND request_id = %s",
                             (attempt_id, now, row["dialog_id"], row["request_id"]))
        self._process(row, attempt_id)
        return True

    def _process(self, row, attempt_id):
        import psycopg
        started = config.utcnow()
        deadline = AttemptDeadline(started)
        try:
            deadline.check()
            self.repository.access(row["user_id"])
            with self.repository.connect() as conn:
                with conn.transaction():
                    prepared = self._question(conn, row, attempt_id)
            if prepared is None:
                self._record(row, "invalidated", started, error_code="source_invalidated")
                return
            text, context = prepared
            with self.repository.connect() as conn:
                # SQL statement timeout ограничен остатком бюджета; сеть AI — BudgetAI.
                conn.execute("SELECT set_config('statement_timeout', %s, false)",
                             (f"{max(100, int(deadline.remaining() * 1000))}ms",))
                found, question = search_in_context(conn, BudgetAI(self.ai, deadline), text, context,
                                                    lambda: self.repository.access(row["user_id"]), deadline=deadline)
            self._complete(row, attempt_id, found, deadline, question, started)
        except NotSubscribed:
            self._fail(row, attempt_id, "denied", "not_subscribed", started)
        except (TimeoutError, psycopg.errors.QueryCanceled):
            # Исчерпание бюджета — временная ошибка, а не empty и не резервный успешный ответ.
            self._fail(row, attempt_id, "retryable_failed", "attempt_timeout", started)
        except (ClubUnavailable, AIError):
            self._fail(row, attempt_id, "retryable_failed", "service_unavailable", started)
        except Exception as exc:
            # Только тип: DETAIL ошибок БД может содержать текст вопроса.
            log.warning("Mini App: сбой попытки (%s)", type(exc).__name__)
            self._fail(row, attempt_id, "retryable_failed", "temporary_error", started)

    def _record(self, row, outcome, started, **details):
        try:
            with self.repository.connect() as conn:
                with conn.transaction():
                    mini_telemetry.record(conn, row["operation_id"], outcome, event="attempt",
                                          attempt_no=row["attempt_no"] + 1,
                                          duration_ms=round((config.utcnow() - started).total_seconds() * 1000),
                                          **details)
        except Exception as exc:
            log.warning("Mini App: telemetry не записана (%s)", type(exc).__name__)

    def _question(self, conn, row, attempt_id) -> str | None:
        """Под блокировкой диалога (порядок: диалог → обращения) проверить зависимость уточнения."""
        conn.execute("SELECT 1 FROM miniapp_dialog WHERE id = %s FOR UPDATE", (row["dialog_id"],))
        current = conn.execute("SELECT state, attempt_id FROM miniapp_request WHERE dialog_id = %s AND request_id = %s "
                               "FOR UPDATE", (row["dialog_id"], row["request_id"])).fetchone()
        if not current or current["state"] != "running" or current["attempt_id"] != attempt_id:
            return None
        if row["context_request_id"] and not self.repository._dependency_valid(
                conn, row, row["dialog_id"], row["context_request_id"]):
            # Недействительное уточнение: invalidated без поиска и передачи текста модели.
            self.repository._invalidate(conn, row["dialog_id"], row["request_id"])
            return None
        if row["effective_query"]:
            return row["effective_query"], None
        question = " ".join(row["original_text"].split())[-MAX_QUERY:]
        context = None
        if row["context_request_id"]:
            dependency = conn.execute("SELECT effective_query FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                                      (row["dialog_id"], row["context_request_id"])).fetchone()
            context = dependency["effective_query"] if dependency else None
        # Итоговый запрос (с контекстом или без) записывается вместе с ответом в _complete.
        return question, context

    def _complete(self, row, attempt_id, found, deadline, question, started):
        with self.repository.connect() as conn:
            with conn.transaction():
                deadline.check()
                dialog = conn.execute("SELECT d.state, s.expires_at, s.revoked_at, s.current_dialog_id "
                                      "FROM miniapp_dialog d JOIN miniapp_session s ON s.id = d.session_id "
                                      "WHERE d.id = %s FOR UPDATE OF d", (row["dialog_id"],)).fetchone()
                current = conn.execute("SELECT state, attempt_id FROM miniapp_request WHERE dialog_id = %s "
                                       "AND request_id = %s FOR UPDATE", (row["dialog_id"], row["request_id"])).fetchone()
                if (not dialog or not current or current["state"] != "running" or current["attempt_id"] != attempt_id
                        or dialog["revoked_at"] or dialog["expires_at"] <= config.utcnow()
                        or dialog["state"] != "active" or dialog["current_dialog_id"] != row["dialog_id"]):
                    return      # поздний результат прежней попытки/сессии/диалога отбрасывается
                if row["context_request_id"] and not self.repository._dependency_valid(
                        conn, row, row["dialog_id"], row["context_request_id"]):
                    self.repository._invalidate(conn, row["dialog_id"], row["request_id"])
                    return
                self.repository.access(row["user_id"])
                if not self.repository._provenance_valid(conn, row, list(found.provenance and [
                        {"material_id": m, "version_id": v} for m, v in found.provenance.items()] or [])):
                    self.repository._invalidate(conn, row["dialog_id"], row["request_id"])
                    return
                result = {"outcome": found.outcome, "cards": found.cards,
                          "text": cards_module.lead_text(found.outcome, found.cards, found.text)}
                conn.execute("UPDATE miniapp_request SET state = 'completed', outcome = %s, result = %s, "
                             "provenance = %s, finished_at = %s, error_code = NULL, effective_query = %s "
                             "WHERE dialog_id = %s AND request_id = %s AND state = 'running' AND attempt_id = %s",
                             (found.outcome, Jsonb(result),
                              Jsonb([{"material_id": m, "version_id": v} for m, v in found.provenance.items()]),
                              config.utcnow(), question, row["dialog_id"], row["request_id"], attempt_id))
                if found.outcome == "clarify":
                    conn.execute("UPDATE miniapp_dialog SET pending_query = %s, pending_request_id = %s WHERE id = %s",
                                 (question, row["request_id"], row["dialog_id"]))
                else:
                    conn.execute("UPDATE miniapp_dialog SET pending_query = NULL, pending_request_id = NULL WHERE id = %s",
                                 (row["dialog_id"],))
                mini_telemetry.record(conn, row["operation_id"], found.outcome, event="attempt",
                                      attempt_no=row["attempt_no"] + 1,
                                      duration_ms=round((config.utcnow() - started).total_seconds() * 1000),
                                      materials=sorted(found.provenance),
                                      versions=sorted(set(found.provenance.values())))

    def _fail(self, row, attempt_id, state, code, started=None):
        with self.repository.connect() as conn:
            with conn.transaction():
                changed = conn.execute(
                    "UPDATE miniapp_request SET state = %s, error_code = %s, finished_at = %s, "
                    "result = NULL, provenance = '[]'::jsonb WHERE dialog_id = %s AND request_id = %s "
                    "AND state = 'running' AND attempt_id = %s",
                    (state, code, config.utcnow(), row["dialog_id"], row["request_id"], attempt_id)).rowcount
        if changed and started is not None:
            self._record(row, state, started, error_code=code)


class _Invalidated(Exception):
    pass


class Maintenance:
    """Очистка истёкших сессий при старте и каждые 15 минут, вне потока поиска."""

    def __init__(self, repository: MiniAppRepository, *, interval=15 * 60):
        self.repository = repository
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miniapp-maintenance", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def _run(self):
        while not self._stop.is_set():
            try:
                self.repository.cleanup()
            except Exception as exc:
                import logging
                logging.getLogger("ecoprofi.bot.miniapp.worker").warning(
                    "Mini App: очистка не выполнена (%s)", type(exc).__name__)
            self._stop.wait(self.interval)
