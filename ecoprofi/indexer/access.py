"""Доступ оператора к источникам VK: вход, хранение, продление, отказ.

Значения (рабочий токен, токен обновления, device_id, номер приложения) лежат
одним файлом на томе индексатора и заменяются атомарно. В БД — только состояние
без значений. VK ID при каждом продлении выдаёт новый токен обновления взамен
прежнего, поэтому новое значение записывается на диск до первого использования,
а продлевает доступ один процесс в каждый момент (блокировка в БД).

Ни одно значение не попадает в журнал, исключения, отчёты и оповещения.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ecoprofi.indexer import alerts
from ecoprofi.indexer.retry import SourceAuth, SourceTimeout

AUTH_URL = "https://id.vk.ru/authorize"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"
REDIRECT = "https://localhost/callback"
REFRESH_MARGIN = 600            # продлевать, когда рабочему токену осталось меньше 10 минут
DEFAULT_LIFETIME = 3600
DEFAULT_PATH = "/secrets/access.json"
_LOCK = "ecoprofi:operator-access"
_FIELDS = ("access_token", "refresh_token", "device_id", "app", "expires_at")


class LoginError(ValueError):
    """Вход не состоялся. Текст не содержит кода и токенов."""


class IdTransport:
    """Обмен с VK ID. Отказ с телом JSON возвращается словарём, сбой сети — временная ошибка."""

    def post(self, url: str, fields: dict) -> dict:
        request = Request(url, data=urlencode(fields).encode())
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                if exc.code >= 500 or exc.code == 429:
                    raise SourceTimeout(f"HTTP {exc.code} (VK ID)") from None
                try:
                    body = json.load(exc)
                except ValueError:
                    body = None
                return body if isinstance(body, dict) and body else {"error": f"http_{exc.code}"}
            finally:
                exc.close()
        except (URLError, OSError, TimeoutError) as exc:
            raise SourceTimeout(f"{type(exc).__name__} (VK ID)") from None
        except ValueError:
            raise SourceTimeout("VK ID вернул не JSON.") from None


class AccessStore:
    """Файл значений доступа: чтение, атомарная замена, удаление."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("ACCESS_FILE", DEFAULT_PATH))

    def load(self) -> dict | None:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if not isinstance(values, dict) or not all(values.get(k) for k in _FIELDS):
            return None
        return values

    def save(self, values: dict) -> None:
        """Записать новый файл рядом и заменить прежний одной операцией.

        Прежний файл цел, пока замена не состоялась: сбой записи не теряет доступ.
        """
        data = json.dumps({k: values[k] for k in _FIELDS}).encode()
        temp = self.path.with_name(self.path.name + ".new")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, self.path)
        try:
            directory = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return                       # каталог не открывается на чтение (Windows): замена уже состоялась
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)

    def forget(self) -> None:
        for path in (self.path, self.path.with_name(self.path.name + ".new")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class LoginRequest:
    """Начатый вход: адрес для браузера и то, с чем сверяется возврат."""
    url: str
    app: str
    state: str
    verifier: str


def login_request(app: str) -> LoginRequest:
    if not str(app).isdigit():
        raise LoginError("Не задан номер приложения VK ID (VK_ID_APP).")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(32)
    query = dict(client_id=app, redirect_uri=REDIRECT, state=state, response_type="code",
                 code_challenge=challenge, code_challenge_method="S256")
    return LoginRequest(AUTH_URL + "?" + urlencode(query), str(app), state, verifier)


def callback_fields(address: str, state: str) -> dict:
    """Код и device_id из адреса возврата. Чужой адрес и чужой state отклоняются."""
    parsed = urlsplit(address.strip())
    if (parsed.scheme, parsed.netloc, parsed.path) != ("https", "localhost", "/callback"):
        raise LoginError("Ожидается адрес возврата https://localhost/callback.")
    fields = parse_qs(parsed.query)
    if fields.get("state") != [state]:
        raise LoginError("Параметр state не совпал: начните вход заново.")
    if not all(len(fields.get(k, [])) == 1 for k in ("code", "device_id")):
        raise LoginError("В адресе возврата нет кода и device_id.")
    return {k: fields[k][0] for k in ("code", "device_id")}


def describe(response: dict) -> dict:
    """Состав ответа VK ID без значений: что пришло и на какой срок."""
    shown = ("expires_in", "refresh_token_expires_in", "token_type", "scope")
    return {key: (response[key] if key in shown else "есть") for key in sorted(response)}


class Access:
    """Доступ оператора. connect — функция, открывающая отдельное соединение с БД
    для состояния и блокировки (не зависит от транзакции прохода); None — без БД."""

    def __init__(self, store: AccessStore, connect=None, *, transport=None, clock=time.time):
        self.store = store
        self._connect = connect
        self._transport = transport or IdTransport()
        self._clock = clock

    # --- состояние в БД, без значений ---
    def _state(self, sql: str, params: tuple = (), *, alert: str | None = None) -> None:
        if self._connect is None:
            return
        with self._connect() as conn:
            conn.execute(sql, params)
            if alert == "open":
                alerts.open_alert(conn, "access", alerts.text_access())
            elif alert == "close":
                alerts.close_alert(conn, "access")

    def _when(self, seconds: float) -> datetime:
        return datetime.fromtimestamp(seconds, timezone.utc)

    def _mark_login(self, values: dict, refresh_expires) -> None:
        self._state(
            "UPDATE operator_access SET relogin_verified_at = CASE WHEN state = 'login_required' "
            "THEN now() ELSE relogin_verified_at END, state = 'active', logged_in_at = now(), "
            "refreshed_at = NULL, access_expires_at = %s, refresh_expires_at = %s, last_denial = NULL",
            (self._when(values["expires_at"]), refresh_expires), alert="close")

    def _mark_refresh(self, values: dict, refresh_expires) -> None:
        self._state(
            "UPDATE operator_access SET state = 'active', refreshed_at = now(), access_expires_at = %s, "
            "refresh_expires_at = coalesce(%s, refresh_expires_at), "
            "refresh_verified_at = coalesce(refresh_verified_at, now())",
            (self._when(values["expires_at"]), refresh_expires))

    def _mark_login_required(self, reason: str) -> None:
        self._state("UPDATE operator_access SET state = 'login_required', last_denial = %s",
                    (reason[:200],), alert="open")

    def _refresh_expiry(self, response: dict):
        seconds = response.get("refresh_token_expires_in")
        if isinstance(seconds, (int, float)) and seconds > 0:
            return self._when(self._clock() + seconds)
        return None

    def _values(self, response: dict, *, app: str, device_id: str, refresh_token: str | None = None) -> dict:
        lifetime = response.get("expires_in")
        lifetime = lifetime if isinstance(lifetime, (int, float)) and lifetime > 0 else DEFAULT_LIFETIME
        return {"access_token": response["access_token"],
                "refresh_token": response.get("refresh_token") or refresh_token,
                "device_id": device_id, "app": app, "expires_at": self._clock() + lifetime}

    # --- вход ---
    def complete_login(self, request: LoginRequest, address: str) -> dict:
        """Обменять код из адреса возврата и сохранить доступ. Возвращает состав ответа без значений."""
        fields = callback_fields(address, request.state)
        response = self._transport.post(TOKEN_URL, dict(
            grant_type="authorization_code", code=fields["code"], device_id=fields["device_id"],
            code_verifier=request.verifier, client_id=request.app, redirect_uri=REDIRECT, state=request.state))
        if not isinstance(response, dict) or not response.get("access_token"):
            error = response.get("error") if isinstance(response, dict) else None
            raise LoginError(f"VK ID отклонил вход ({error or 'нет рабочего токена'}).")
        if not response.get("refresh_token"):
            raise LoginError("VK ID не выдал токен обновления: продление без входа невозможно.")
        values = self._values(response, app=request.app, device_id=fields["device_id"])
        self.store.save(values)
        self._mark_login(values, self._refresh_expiry(response))
        return describe(response)

    def forget(self) -> None:
        self.store.forget()
        self._state("UPDATE operator_access SET state = 'absent', access_expires_at = NULL")

    # --- токен для вызова ---
    def token(self) -> str:
        """Рабочий токен; при близком сроке сначала продлевается."""
        values = self.store.load()
        if values is None:
            # Файл пропал при действующем состоянии: это потеря доступа, а не «ещё не входили».
            self._state("UPDATE operator_access SET state = 'login_required', "
                        "last_denial = 'значения доступа не найдены' WHERE state = 'active'")
            raise SourceAuth("Доступ оператора не сохранён: нужен вход (access login).")
        if values["expires_at"] - self._clock() > REFRESH_MARGIN:
            return values["access_token"]
        return self._refresh(lambda current: current["expires_at"] - self._clock() > REFRESH_MARGIN)

    def force_refresh(self, failed_token: str) -> str:
        """Продлить после отказа авторизации, если это ещё не сделал другой процесс."""
        return self._refresh(lambda current: current["access_token"] != failed_token)

    def _refresh(self, fresh_enough) -> str:
        if self._connect is None:
            return self._refresh_locked(fresh_enough)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (_LOCK,))
            try:
                return self._refresh_locked(fresh_enough)
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (_LOCK,))

    def _refresh_locked(self, fresh_enough) -> str:
        # После захвата блокировки файл перечитывается: доступ мог продлить другой процесс.
        values = self.store.load()
        if values is None:
            raise SourceAuth("Доступ оператора не сохранён: нужен вход (access login).")
        if fresh_enough(values):
            return values["access_token"]
        state = secrets.token_urlsafe(16)
        response = self._transport.post(TOKEN_URL, dict(
            grant_type="refresh_token", refresh_token=values["refresh_token"],
            client_id=values["app"], device_id=values["device_id"], state=state))
        if not isinstance(response, dict) or not response.get("access_token"):
            error = response.get("error") if isinstance(response, dict) else None
            reason = f"VK ID отклонил продление ({error or 'нет рабочего токена'})"
            self.store.forget()
            self._mark_login_required(reason)
            raise SourceAuth(reason + ": нужен вход (access login).")
        renewed = self._values(response, app=values["app"], device_id=values["device_id"],
                               refresh_token=values["refresh_token"])
        # Прежний токен обновления уже недействителен: новое значение — на диск до любого использования.
        self.store.save(renewed)
        self._mark_refresh(renewed, self._refresh_expiry(response))
        return renewed["access_token"]

    def remaining(self) -> timedelta | None:
        values = self.store.load()
        if values is None:
            return None
        return timedelta(seconds=values["expires_at"] - self._clock())


def state(conn) -> dict:
    """Строка состояния доступа."""
    return conn.execute("SELECT * FROM operator_access").fetchone()
