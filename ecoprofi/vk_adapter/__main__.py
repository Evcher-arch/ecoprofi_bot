"""Адаптер тестового диалога: python -m ecoprofi.vk_adapter

Принимает личные message_new тестовой группы через Bots Long Poll, вызывает
POST /api/search и отправляет ответ в исходный peer_id. Между циклами Long Poll
забирает у bot очередь оповещений и отправляет их оператору (ALERT_VK_USER_ID).
Ключ сообщений и параметры Long Poll в журнал не выводятся.
"""
import hashlib
import logging
import os
import sys
import time

import httpx

from ecoprofi.vk_adapter.delivery import BotUnavailable, SendRejected, SendUncertain, deliver

API = "https://api.vk.com/method/"
API_VERSION = "5.199"
log = logging.getLogger("ecoprofi.vk_adapter")


class VkError(Exception):
    def __init__(self, code):
        super().__init__(f"VK {code}")
        self.code = code


class VkGroup:
    """Вызовы VK от имени сообщества."""

    def __init__(self, token: str, group_id: int, transport: httpx.BaseTransport | None = None):
        self._token = token
        self.group_id = group_id
        self._http = httpx.Client(transport=transport, timeout=40.0)

    def call(self, method: str, **params):
        response = self._http.post(API + method, data=dict(params, access_token=self._token, v=API_VERSION))
        body = response.json()
        if "error" in body:
            raise VkError(body["error"].get("error_code"))
        return body["response"]

    def long_poll_server(self) -> dict:
        return self.call("groups.getLongPollServer", group_id=self.group_id)

    def poll(self, server: dict, wait: int = 25) -> dict:
        response = self._http.get(server["server"], params={
            "act": "a_check", "key": server["key"], "ts": server["ts"], "wait": wait})
        return response.json()

    def send(self, peer_id: int, random_id: int, text: str) -> None:
        try:
            self.call("messages.send", peer_id=peer_id, random_id=random_id, message=text,
                      dont_parse_links=0)
        except VkError as exc:
            if exc.code in (1, 6, 9, 10):
                raise SendUncertain() from None
            raise SendRejected() from None
        except (httpx.HTTPError, ValueError):
            raise SendUncertain() from None


class BotClient:
    def __init__(self, url: str, service_key: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=url, transport=transport, timeout=60.0,
                                  headers={"X-Service-Key": service_key})

    def search(self, event: dict) -> dict | None:
        try:
            response = self._http.post("/api/search", json=event)
        except httpx.HTTPError:
            raise BotUnavailable() from None
        if response.status_code == 200:
            return response.json()
        if response.status_code in (401, 403, 422):
            log.info("событие отклонено: HTTP %s", response.status_code)
            return None
        raise BotUnavailable()

    def delivered(self, event: dict) -> None:
        try:
            self._http.post("/api/delivered", json={"event_id": event["event_id"], "group_id": event["group_id"]})
        except httpx.HTTPError:
            pass  # отметка доставки вспомогательная

    def alerts_pending(self) -> list[dict]:
        try:
            response = self._http.get("/api/alerts/pending")
        except httpx.HTTPError:
            raise BotUnavailable() from None
        if response.status_code != 200:
            raise BotUnavailable()
        return response.json().get("alerts", [])

    def alert_sent(self, alert_id: int, send_no: int) -> None:
        try:
            self._http.post(f"/api/alerts/{alert_id}/sent", json={"send_no": send_no})
        except httpx.HTTPError:
            raise BotUnavailable() from None


def alert_random_id(alert_id: int, send_no: int) -> int:
    """Один random_id на отправку: повтор после неясного исхода не даёт второго сообщения."""
    digest = hashlib.sha256(f"alert:{alert_id}:{send_no}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2**31 - 1) + 1


def flush_alerts(vk, bot, user_id: int) -> int:
    """Отправить оповещения из очереди. Сбой не мешает обработке входящих сообщений."""
    try:
        messages = bot.alerts_pending()
    except BotUnavailable:
        log.warning("оповещения: bot недоступен")
        return 0
    sent = 0
    for message in messages:
        try:
            vk.send(user_id, alert_random_id(message["id"], message["send_no"]), message["text"][:4000])
        except SendUncertain:
            log.warning("оповещение %s: исход отправки неизвестен, повтор в следующий раз", message["id"])
            continue
        except SendRejected:
            log.warning("оповещение %s: VK отклонил отправку", message["id"])
            continue
        try:
            bot.alert_sent(message["id"], message["send_no"])
        except BotUnavailable:
            log.warning("оповещение %s: отметка отправки не сохранена", message["id"])
            continue
        sent += 1
    return sent


def to_event(update: dict, group_id: int) -> dict | None:
    """Преобразовать обновление Long Poll в событие контракта. None — не наш случай."""
    if update.get("type") != "message_new" or update.get("group_id") != group_id:
        return None
    message = (update.get("object") or {}).get("message") or {}
    from_id, peer_id = message.get("from_id"), message.get("peer_id")
    text = " ".join(str(message.get("text") or "").split())
    if not isinstance(from_id, int) or from_id <= 0 or peer_id != from_id or not text:
        return None  # беседа, сообщество-отправитель или пустой текст
    event_id = update.get("event_id") or f"msg-{peer_id}-{message.get('id')}"
    return {"event_id": str(event_id), "group_id": group_id, "peer_id": peer_id,
            "user_id": from_id, "text": text[:2000]}


ALERT_INTERVAL = 60


def run(vk, bot, *, sleep=time.sleep, max_polls: int | None = None, alert_user: int | None = None,
        clock=time.monotonic) -> None:
    """Цикл Long Poll. После потери параметров они запрашиваются заново для той же группы."""
    server = None
    polls = 0
    alerts_at = None
    while max_polls is None or polls < max_polls:
        polls += 1
        if alert_user and (alerts_at is None or clock() - alerts_at >= ALERT_INTERVAL):
            alerts_at = clock()
            flush_alerts(vk, bot, alert_user)
        try:
            if server is None:
                server = vk.long_poll_server()
                log.info("Long Poll подключён")
            reply = vk.poll(server)
        except (VkError, httpx.HTTPError, ValueError, KeyError) as exc:
            # Код VK безопасен для журнала; текст остальных ошибок может содержать ключ Long Poll.
            log.warning("Long Poll: сбой (%s), переподключение",
                        exc if isinstance(exc, VkError) else type(exc).__name__)
            server = None
            sleep(3)
            continue
        failed = reply.get("failed")
        if failed == 1 and "ts" in reply:
            server["ts"] = reply["ts"]       # история устарела: продолжить с нового ts
            continue
        if failed:
            server = None                    # ключ истёк или параметры потеряны
            continue
        server["ts"] = reply.get("ts", server["ts"])
        for update in reply.get("updates", []):
            event = to_event(update, vk.group_id)
            if event is None:
                continue
            result = deliver(vk, bot, event, sleep=sleep)
            log.info("событие %s: %s", event["event_id"], result)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # адреса запросов содержат ключ Long Poll
    token, group = os.environ.get("VK_GROUP_TOKEN", ""), os.environ.get("VK_GROUP_ID", "")
    service_key = os.environ.get("SERVICE_KEY", "")
    if not token or not group.isdigit() or not service_key:
        print("Адаптер не запущен: задайте VK_GROUP_ID, VK_GROUP_TOKEN и SERVICE_KEY в .env "
              "(задача T017).", file=sys.stderr)
        # Без настроек сервис ждёт, не перезапускаясь в цикле.
        while True:
            time.sleep(3600)
    alert_user = os.environ.get("ALERT_VK_USER_ID", "")
    run(VkGroup(token, int(group)), BotClient(os.environ.get("BOT_URL", "http://bot:8000"), service_key),
        alert_user=int(alert_user) if alert_user.isdigit() else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
