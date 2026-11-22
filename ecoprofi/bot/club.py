"""Проверка подписки участника на клуб Экопрофи (VK Donut сообщества «Экопрофи»).

Список донов читается методом groups.getMembers(filter=donut) с ключом
сообщества Экопрофи (CLUB_GROUP_TOKEN) и держится в памяти CLUB_CACHE_SECONDS
секунд: оформленная или отменённая подписка учитывается не позже, чем через
этот срок. Ключ сообщества в журнал и исключения не попадает.
"""
import logging
import os
import threading
import time

import httpx

API = "https://api.vk.com/method/groups.getMembers"
API_VERSION = "5.199"
CLUB_GROUP_ID = 95098598          # сообщество «Экопрофи»
PAGE = 1000                       # наибольшая страница groups.getMembers
DEFAULT_TTL = 60
log = logging.getLogger("ecoprofi.bot.club")


class ClubUnavailable(Exception):
    """Подписку проверить не удалось (нет ключа, отказ или сбой VK): доступ не выдаётся."""


class ClubMembers:
    """Подписчики клуба: is_member(user_id) -> bool."""

    def __init__(self, token: str, group_id: int = CLUB_GROUP_ID, *, ttl: float = DEFAULT_TTL,
                 transport: httpx.BaseTransport | None = None, clock=time.monotonic):
        self._token = token
        self.group_id = group_id
        self._ttl = ttl
        self._clock = clock
        self._http = httpx.Client(transport=transport, timeout=20.0)
        self._lock = threading.Lock()
        self._members: frozenset[int] | None = None
        self._loaded_at = 0.0

    @classmethod
    def from_env(cls) -> "ClubMembers":
        group = os.environ.get("CLUB_GROUP_ID", "")
        ttl = os.environ.get("CLUB_CACHE_SECONDS", "")
        return cls(os.environ.get("CLUB_GROUP_TOKEN", ""),
                   int(group) if group.isdigit() else CLUB_GROUP_ID,
                   ttl=float(ttl) if ttl.replace(".", "", 1).isdigit() else DEFAULT_TTL)

    def __call__(self, user_id: int) -> bool:
        return self.is_member(user_id)

    def is_member(self, user_id: int) -> bool:
        with self._lock:
            if self._members is None or self._clock() - self._loaded_at >= self._ttl:
                self._members = self._load()
                self._loaded_at = self._clock()
            return user_id in self._members

    def _load(self) -> frozenset[int]:
        if not self._token:
            raise ClubUnavailable("Не задан ключ сообщества Экопрофи (CLUB_GROUP_TOKEN).")
        members: set[int] = set()
        offset = 0
        while True:
            page = self._call(offset)
            items = page.get("items") or []
            members.update(int(x) for x in items)
            offset += len(items)
            if not items or offset >= int(page.get("count", 0)):
                return frozenset(members)

    def _call(self, offset: int) -> dict:
        try:
            response = self._http.post(API, data=dict(
                group_id=self.group_id, filter="donut", count=PAGE, offset=offset,
                access_token=self._token, v=API_VERSION))
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Текст исключения httpx может содержать параметры запроса: в журнал — только тип.
            log.warning("подписка на клуб: сбой сети (%s)", type(exc).__name__)
            raise ClubUnavailable("VK недоступен: подписку на клуб проверить не удалось.") from None
        if "error" in body or not isinstance(body.get("response"), dict):
            code = (body.get("error") or {}).get("error_code")
            log.warning("подписка на клуб: VK отклонил запрос (код %s)", code)
            raise ClubUnavailable(f"VK отклонил проверку подписки на клуб (код {code}).")
        return body["response"]
