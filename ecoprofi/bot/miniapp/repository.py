"""Транзакции сессий, диалогов, обращений и безопасной истории Mini App."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

from ecoprofi import telemetry
from ecoprofi.bot.miniapp import config
from ecoprofi.bot.miniapp.config import (ATTEMPT_LIMIT, BUDGET_SECONDS, DIALOG_LIMIT, QUESTION_LIMIT,
                                         REQUEST_LIMIT, SESSION_SECONDS)
from ecoprofi.bot.miniapp.auth import launch_time

ACTIVE = ("queued", "running")


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable=False):
        super().__init__(code)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


class MiniAppRepository:
    def __init__(self, connect_fn, *, club=None, alert=None):
        self.connect = connect_fn
        self.club = club
        self.alert = alert or (lambda opened: None)

    def access(self, user_id: int):
        from ecoprofi.bot.access import resolve_member_access
        return resolve_member_access(user_id, self.club)

    def session(self, conn, token: str, *, lock=False, check_expiry=True):
        row = conn.execute(
            "SELECT s.*, d.state AS dialog_state FROM miniapp_session s "
            "LEFT JOIN miniapp_dialog d ON d.session_id = s.id AND d.id = s.current_dialog_id "
            "WHERE s.token_hash = %s" + (" FOR UPDATE OF s" if lock else ""),
            (hashlib.sha256(token.encode()).digest(),)).fetchone()
        if not row or row["revoked_at"] is not None:
            raise ApiProblem(401, "invalid_session", "Войдите в приложение заново.")
        if check_expiry and row["expires_at"] <= config.utcnow():
            self._expire(conn, row["id"])
            raise ApiProblem(401, "session_expired", "Сессия истекла. Откройте приложение заново.")
        return row

    def create_session(self, launch):
        token = secrets.token_urlsafe(32)
        now = config.utcnow()
        with self.connect() as conn:
            with conn.transaction():
                session_id, dialog_id = uuid.uuid4(), uuid.uuid4()
                conn.execute(
                    "INSERT INTO miniapp_session (id, token_hash, app_id, user_id, launch_digest, launch_ts, "
                    "created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (session_id, hashlib.sha256(token.encode()).digest(), launch.app_id, launch.user_id,
                     launch.digest, launch_time(launch.timestamp), now, now + timedelta(seconds=SESSION_SECONDS)))
                conn.execute("INSERT INTO miniapp_dialog (id, session_id, created_at) VALUES (%s, %s, %s)",
                             (dialog_id, session_id, now))
                conn.execute("UPDATE miniapp_session SET current_dialog_id = %s WHERE id = %s", (dialog_id, session_id))
        return {"token": token, "expires_at": now + timedelta(seconds=SESSION_SECONDS),
                "dialog_id": dialog_id, "user_id": launch.user_id, "limits": {"question_chars": QUESTION_LIMIT}}

    def resume(self, token, launch):
        with self.connect() as conn:
            with conn.transaction():
                session = self.session(conn, token)
                if session["user_id"] != launch.user_id or session["app_id"] != launch.app_id or session["launch_digest"] != launch.digest:
                    raise ApiProblem(401, "resume_mismatch", "Откройте приложение заново из VK.")
                self.access(session["user_id"])
                return {"expires_at": session["expires_at"], "dialog_id": session["current_dialog_id"],
                        "user_id": session["user_id"], "limits": {"question_chars": QUESTION_LIMIT}}

    def delete_session(self, token):
        with self.connect() as conn:
            with conn.transaction():
                session = self.session(conn, token, lock=True)
                self._close_session(conn, session["id"])

    def _close_session(self, conn, session_id):
        now = config.utcnow()
        conn.execute("UPDATE miniapp_session SET revoked_at = %s WHERE id = %s", (now, session_id))
        conn.execute("UPDATE miniapp_request SET state = 'cancelled', attempt_id = gen_random_uuid(), "
                     "finished_at = %s, result = NULL, provenance = '[]'::jsonb WHERE dialog_id IN "
                     "(SELECT id FROM miniapp_dialog WHERE session_id = %s) AND state IN ('queued', 'running')",
                     (now, session_id))
        conn.execute("UPDATE miniapp_dialog SET state = 'closed', closed_at = %s, pending_query = NULL, "
                     "pending_request_id = NULL WHERE session_id = %s", (now, session_id))

    def _expire(self, conn, session_id):
        conn.execute("UPDATE miniapp_request SET state = 'cancelled', attempt_id = gen_random_uuid(), "
                     "result = NULL, provenance = '[]'::jsonb WHERE dialog_id IN "
                     "(SELECT id FROM miniapp_dialog WHERE session_id = %s) AND state IN ('queued', 'running')", (session_id,))
        conn.execute("UPDATE miniapp_dialog SET state = 'closed', closed_at = coalesce(closed_at, %s), "
                     "pending_query = NULL, pending_request_id = NULL WHERE session_id = %s", (config.utcnow(), session_id))

    def cleanup(self):
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM miniapp_session WHERE expires_at <= %s OR revoked_at IS NOT NULL", (config.utcnow(),))

    def dialog(self, conn, session, dialog_id=None, *, lock=False, allow_closed=False):
        dialog_id = dialog_id or session["current_dialog_id"]
        row = conn.execute(
            "SELECT d.* FROM miniapp_dialog d JOIN miniapp_session s ON s.id = d.session_id "
            "WHERE d.id = %s AND d.session_id = %s" + (" FOR UPDATE OF d" if lock else ""),
            (dialog_id, session["id"])).fetchone()
        if not row:
            raise ApiProblem(404, "not_found", "Диалог не найден.")
        if row["state"] != "active" and not allow_closed:
            raise ApiProblem(409, "dialog_closed", "Диалог завершён.")
        return row

    def new_dialog(self, token, dialog_id):
        with self.connect() as conn:
            with conn.transaction():
                session = self.session(conn, token, lock=True)
                self.access(session["user_id"])
                if session["current_dialog_id"] == dialog_id:
                    return {"dialog_id": dialog_id, "state": "active"}, False
                previous = conn.execute("SELECT state FROM miniapp_dialog WHERE id = %s AND session_id = %s",
                                        (dialog_id, session["id"])).fetchone()
                if previous:
                    raise ApiProblem(409, "dialog_closed", "Этот диалог уже завершён.")
                count = conn.execute("SELECT count(*) AS n FROM miniapp_dialog WHERE session_id = %s",
                                     (session["id"],)).fetchone()["n"]
                if count >= DIALOG_LIMIT:
                    raise ApiProblem(429, "session_limit", "Создайте новую сессию, открыв приложение заново.")
                now = config.utcnow()
                if session["current_dialog_id"]:
                    self.dialog(conn, session, lock=True, allow_closed=True)
                    conn.execute("UPDATE miniapp_request SET state = 'cancelled', attempt_id = gen_random_uuid(), "
                                 "result = NULL, provenance = '[]'::jsonb, finished_at = %s "
                                 "WHERE dialog_id = %s AND state IN ('queued', 'running')", (now, session["current_dialog_id"]))
                    conn.execute("UPDATE miniapp_dialog SET state = 'closed', closed_at = %s, pending_query = NULL, "
                                 "pending_request_id = NULL WHERE id = %s", (now, session["current_dialog_id"]))
                conn.execute("INSERT INTO miniapp_dialog (id, session_id, created_at) VALUES (%s, %s, %s)",
                             (dialog_id, session["id"], now))
                conn.execute("UPDATE miniapp_session SET current_dialog_id = %s WHERE id = %s", (dialog_id, session["id"]))
                return {"dialog_id": dialog_id, "state": "active"}, True

    def submit(self, token, dialog_id, request_id, text):
        if len(text) > QUESTION_LIMIT or not text.strip():
            raise ApiProblem(422, "invalid_input", "Введите от 1 до 2000 символов.")
        with self.connect() as conn:
            with conn.transaction():
                self._expire_overdue(conn)
                session = self.session(conn, token)
                self.access(session["user_id"])
                dialog = self.dialog(conn, session, dialog_id, lock=True)
                row = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                                   (dialog_id, request_id)).fetchone()
                if row:
                    if row["original_text"] != text or row["text_digest"] != hashlib.sha256(text.encode()).digest():
                        raise ApiProblem(409, "request_conflict", "Измените идентификатор для нового вопроса.")
                    return self._retry_or_serialize(conn, session, dialog, row)
                busy = conn.execute("SELECT 1 FROM miniapp_request WHERE dialog_id = %s AND state = ANY(%s) LIMIT 1",
                                    (dialog_id, list(ACTIVE))).fetchone()
                if busy:
                    raise ApiProblem(409, "dialog_busy", "Дождитесь ответа на текущий вопрос.")
                count = conn.execute("SELECT count(*) AS n FROM miniapp_request r JOIN miniapp_dialog d ON d.id = r.dialog_id "
                                     "WHERE d.session_id = %s", (session["id"],)).fetchone()["n"]
                if count >= REQUEST_LIMIT:
                    raise ApiProblem(429, "session_limit", "Создайте новую сессию, открыв приложение заново.")
                conn.execute("SELECT pg_advisory_xact_lock(827451)")
                queue = conn.execute("SELECT count(*) AS n FROM miniapp_request WHERE state = ANY(%s)",
                                     (list(ACTIVE),)).fetchone()["n"]
                if queue >= 32:
                    raise ApiProblem(429, "queue_full", "Очередь заполнена. Повторите позже.", retryable=True)
                now = config.utcnow()
                conn.execute(
                    "INSERT INTO miniapp_request (dialog_id, request_id, sequence, original_text, text_digest, "
                    "context_request_id, state, operation_id, queued_at, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s)",
                    (dialog_id, request_id, dialog["next_sequence"], text, hashlib.sha256(text.encode()).digest(),
                     dialog["pending_request_id"], telemetry.new_operation_id(), now, now))
                conn.execute("UPDATE miniapp_dialog SET next_sequence = next_sequence + 1 WHERE id = %s", (dialog_id,))
                row = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                                   (dialog_id, request_id)).fetchone()
                return self._serialize(conn, session, row), True

    def _retry_or_serialize(self, conn, session, dialog, row):
        if row["state"] in ("retryable_failed", "denied"):
            if conn.execute("SELECT 1 FROM miniapp_request WHERE dialog_id = %s AND sequence > %s LIMIT 1",
                            (row["dialog_id"], row["sequence"])).fetchone():
                raise ApiProblem(409, "retry_superseded", "Создайте новый вопрос.")
            if row["attempt_no"] >= ATTEMPT_LIMIT:
                return self._serialize(conn, session, row), False
            if conn.execute("SELECT 1 FROM miniapp_request WHERE dialog_id = %s AND state = ANY(%s)",
                            (row["dialog_id"], list(ACTIVE))).fetchone():
                raise ApiProblem(409, "dialog_busy", "Дождитесь ответа на текущий вопрос.")
            if row["context_request_id"] and not self._dependency_valid(conn, session, row["dialog_id"], row["context_request_id"]):
                self._invalidate(conn, row["dialog_id"], row["request_id"])
                return self._serialize(conn, session, row), False
            conn.execute("SELECT pg_advisory_xact_lock(827451)")
            queue = conn.execute("SELECT count(*) AS n FROM miniapp_request WHERE state = ANY(%s)", (list(ACTIVE),)).fetchone()["n"]
            if queue >= 32:
                raise ApiProblem(429, "queue_full", "Очередь заполнена. Повторите позже.", retryable=True)
            conn.execute("UPDATE miniapp_request SET state = 'queued', queued_at = %s, error_code = NULL, "
                         "finished_at = NULL WHERE dialog_id = %s AND request_id = %s",
                         (config.utcnow(), row["dialog_id"], row["request_id"]))
            row = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                               (row["dialog_id"], row["request_id"])).fetchone()
        return self._serialize(conn, session, row), row["state"] in ("queued", "running")

    def get_request(self, token, dialog_id, request_id):
        with self.connect() as conn:
            with conn.transaction():
                self._expire_overdue(conn)
                session = self.session(conn, token)
                self.access(session["user_id"])
                self.dialog(conn, session, dialog_id, lock=True)
                row = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                                   (dialog_id, request_id)).fetchone()
                if not row:
                    raise ApiProblem(404, "not_found", "Обращение не найдено.")
                data = self._serialize(conn, session, row)
                if data["state"] not in ("queued", "running"):
                    # Чтение итогового состояния по стабильному operation_id обращения (без текста).
                    from ecoprofi.bot.miniapp import telemetry as mini_telemetry
                    mini_telemetry.record(conn, row["operation_id"], data["state"], event="read",
                                          attempt_no=row["attempt_no"])
                return data

    def history(self, token, dialog_id, after=0, limit=50):
        with self.connect() as conn:
            with conn.transaction():
                self._expire_overdue(conn)
                session = self.session(conn, token)
                self.access(session["user_id"])
                self.dialog(conn, session, dialog_id, lock=True)
                rows = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND sequence > %s "
                                    "ORDER BY sequence LIMIT %s", (dialog_id, after, limit + 1)).fetchall()
                items = [self._serialize(conn, session, row) for row in rows[:limit]]
                return {"dialog_id": str(dialog_id), "items": items,
                        "next_after_sequence": items[-1]["sequence"] if items else after,
                        "has_more": len(rows) > limit}

    def _serialize(self, conn, session, row):
        if row["state"] == "completed" and not self._provenance_valid(conn, session, row["provenance"]):
            self._invalidate(conn, row["dialog_id"], row["request_id"])
            row = conn.execute("SELECT * FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                               (row["dialog_id"], row["request_id"])).fetchone()
        data = {"dialog_id": str(row["dialog_id"]), "request_id": str(row["request_id"]),
                "sequence": row["sequence"], "state": row["state"], "operation_id": row["operation_id"],
                "question": row["original_text"],
                "retry_allowed": row["state"] in ("retryable_failed", "denied") and row["attempt_no"] < ATTEMPT_LIMIT}
        if row["state"] == "completed":
            data["answer"] = row["result"]
        elif row["state"] == "invalidated":
            data["message"] = "Материалы ответа изменились или недоступны. Выполните новый поиск."
        elif row["state"] == "cancelled":
            data["message"] = "Диалог завершён."
        elif row["state"] == "retryable_failed":
            data["error_code"] = row["error_code"] or "temporary_error"
            data["message"] = "Не удалось завершить поиск. Повторите попытку."
        return data

    def _provenance_valid(self, conn, session, provenance):
        from ecoprofi.bot.access import visible_versions
        visible = visible_versions(conn, self.access(session["user_id"]).audiences)
        return all(isinstance(item, dict) and visible.get(item.get("material_id")) == item.get("version_id")
                   for item in provenance)

    def _dependency_valid(self, conn, session, dialog_id, request_id):
        dependency = conn.execute("SELECT state, outcome, provenance FROM miniapp_request WHERE dialog_id = %s AND request_id = %s",
                                  (dialog_id, request_id)).fetchone()
        return bool(dependency and dependency["state"] == "completed" and dependency["outcome"] == "clarify"
                    and self._provenance_valid(conn, session, dependency["provenance"]))

    def _invalidate(self, conn, dialog_id, request_id):
        conn.execute("UPDATE miniapp_request SET state = 'invalidated', outcome = NULL, result = NULL, "
                     "provenance = '[]'::jsonb, attempt_id = gen_random_uuid(), finished_at = %s, "
                     "error_code = 'source_invalidated' WHERE dialog_id = %s AND request_id = %s",
                     (config.utcnow(), dialog_id, request_id))
        conn.execute("UPDATE miniapp_dialog SET pending_query = NULL, pending_request_id = NULL "
                     "WHERE id = %s AND pending_request_id = %s", (dialog_id, request_id))

    def _expire_overdue(self, conn):
        now = config.utcnow()
        conn.execute("UPDATE miniapp_request SET state = 'retryable_failed', error_code = 'queue_timeout', "
                     "attempt_id = gen_random_uuid(), finished_at = %s WHERE state = 'queued' AND queued_at < %s",
                     (now, now - timedelta(seconds=BUDGET_SECONDS)))
        conn.execute("UPDATE miniapp_request SET state = 'retryable_failed', error_code = 'attempt_timeout', "
                     "attempt_id = gen_random_uuid(), finished_at = %s WHERE state = 'running' AND started_at < %s",
                     (now, now - timedelta(seconds=BUDGET_SECONDS)))
