"""Чтение прямых видеовложений контрольных постов VK.

Функции выделены из диагностического Test-VkUserWall.py (он сохраняет свои режимы).
Пользовательский токен оператора передаётся только параметром вызова, в журнал
и в каталог не попадает. Адрес дорожки временный: используется для чтения и
не сохраняется.
"""
import getpass
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from ecoprofi.indexer.retry import SourceDeleted, SourceDenied, SourceEmpty, SourceParse, SourceTimeout

API = "https://api.vk.com/method/"
API_VERSION = "5.199"
OWNER_ID = -95098598            # сообщество «Экопрофи»
MAX_TRACK_BYTES = 2_000_000

# Связь контрольных публикаций со строкой расписания (research.md: две проверенные связи).
CONTROL_SCHEDULE = {
    41120: ("pilot-control", "Контрольная строка расписания пилота"),
    30861: ("pilot-control", "Контрольная строка расписания пилота"),
    # Пост из pilot-policy.json владельца; связь с расписанием владелец ещё не подтвердил.
    40746: ("pilot-control", "Контрольная строка расписания пилота"),
}

_DENIED = {5, 15, 30, 200, 201, 203, 204, 212, 214}   # авторизация и доступ
_TEMPORARY = {1, 6, 9, 10, 29}                         # сбой сервера и лимиты


@dataclass(frozen=True)
class VideoSource:
    """Карточка видеовложения. track_url — временный адрес, наружу не выдаётся."""
    post_id: int
    owner_id: int
    video_id: int
    title: str
    description: str
    link_type: str
    access_key: str | None
    track_url: str | None

    @property
    def url(self) -> str:
        return video_url(self.owner_id, self.video_id)


def video_url(owner_id: int, video_id: int) -> str:
    """Стабильная ссылка на запись."""
    return f"https://vkvideo.ru/video{owner_id}_{video_id}"


def read_token_hidden() -> str:
    """Запросить токен оператора скрытым вводом."""
    token = getpass.getpass("Пользовательский токен VK (ввод скрыт): ").strip()
    if not token:
        raise SourceDenied("Токен не введён.")
    return token


class HttpTransport:
    """Сетевой транспорт. В тестах заменяется объектом с теми же методами."""

    def post(self, url: str, fields: dict) -> dict:
        request = Request(url, data=urlencode(fields).encode())
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except HTTPError as exc:
            exc.close()
            if exc.code >= 500 or exc.code == 429:
                raise SourceTimeout(f"HTTP {exc.code} ({urlsplit(url).hostname})") from None
            raise SourceDenied(f"HTTP {exc.code} ({urlsplit(url).hostname})") from None
        except (URLError, OSError, TimeoutError) as exc:
            raise SourceTimeout(type(exc).__name__) from None
        except ValueError:
            raise SourceParse("Ответ API не является JSON.") from None

    def get(self, url: str) -> bytes:
        host = urlsplit(url).hostname
        try:
            with urlopen(url, timeout=25) as response:
                return response.read(MAX_TRACK_BYTES + 1)
        except HTTPError as exc:
            exc.close()
            if exc.code >= 500 or exc.code == 429:
                raise SourceTimeout(f"HTTP {exc.code} ({host})") from None
            if exc.code in (401, 403):
                raise SourceDenied(f"HTTP {exc.code} ({host})") from None
            # Истёкший временный адрес: нужен свежий, это временный сбой.
            raise SourceTimeout(f"HTTP {exc.code} ({host})") from None
        except (URLError, OSError, TimeoutError) as exc:
            raise SourceTimeout(f"{type(exc).__name__} ({host})") from None


class VkSources:
    def __init__(self, token: str, transport=None, owner_id: int = OWNER_ID):
        self._token = token
        self._transport = transport or HttpTransport()
        self.owner_id = owner_id

    def _call(self, method: str, **fields) -> dict | list:
        result = self._transport.post(API + method, dict(fields, access_token=self._token, v=API_VERSION))
        if not isinstance(result, dict):
            raise SourceParse(f"{method}: неожиданный ответ.")
        error = result.get("error")
        if error:
            code = error.get("error_code")
            if code in _TEMPORARY:
                raise SourceTimeout(f"{method}: VK {code}")
            if code in _DENIED:
                raise SourceDenied(f"{method}: VK {code}")
            raise SourceParse(f"{method}: VK {code}")
        if "response" not in result:
            raise SourceParse(f"{method}: нет поля response.")
        return result["response"]

    def _pick(self, items, post_id: int) -> dict | None:
        return next((p for p in items or []
                     if p.get("id") == post_id and p.get("owner_id") == self.owner_id), None)

    def _scan_donut(self, post_id: int) -> dict | None:
        """Найти пост в платной ленте: wall.getById такие посты может не вернуть."""
        offset, total = 0, None
        while total is None or offset < total:
            page = self._call("wall.get", owner_id=self.owner_id, count=100, offset=offset, filter="donut")
            if not isinstance(page, dict):
                raise SourceParse("wall.get: неожиданный ответ.")
            items = page.get("items") or []
            total = page.get("count", 0) if total is None else total
            found = self._pick(items, post_id)
            if found or not items:
                return found
            offset += len(items)
        return None

    def post_videos(self, post_id: int) -> list[VideoSource]:
        """Прямые видеовложения поста. Пустой ответ удаление не подтверждает."""
        try:
            response = self._call("wall.getById", posts=f"{self.owner_id}_{post_id}")
            items = response.get("items", []) if isinstance(response, dict) else response
        except SourceParse:
            # Токену VK ID метод wall.getById закрыт (VK 1051); лента wall.get при этом доступна.
            items = []
        item = self._pick(items, post_id) or self._scan_donut(post_id)
        if item is None:
            # Пропуск в ленте требует проверки и удаление не подтверждает.
            raise SourceEmpty(f"Пост {post_id} не возвращён.")
        if item.get("deleted") or item.get("is_deleted"):
            raise SourceDeleted(f"Пост {post_id} удалён.")
        videos = []
        for attachment in item.get("attachments", []):
            if attachment.get("type") != "video":
                continue
            video = attachment.get("video") or {}
            if "owner_id" not in video or "id" not in video:
                continue
            videos.append(VideoSource(
                post_id=post_id, owner_id=video["owner_id"], video_id=video["id"],
                title=(video.get("title") or "").strip() or f"Видео {video['owner_id']}_{video['id']}",
                description=(video.get("description") or "").strip(),
                link_type="attachment", access_key=video.get("access_key"),
                track_url=_track_url(video),
            ))
        if not videos:
            raise SourceEmpty(f"В посте {post_id} нет прямых видеовложений.")
        return videos

    def fresh_track_url(self, source: VideoSource) -> str | None:
        """Запросить карточку заново и вернуть свежий адрес дорожки (или None)."""
        ref = f"{source.owner_id}_{source.video_id}"
        lookup = ref + (f"_{source.access_key}" if source.access_key else "")
        response = self._call("video.get", videos=lookup)
        for record in (response.get("items", []) if isinstance(response, dict) else []):
            if (record.get("owner_id"), record.get("id")) == (source.owner_id, source.video_id):
                return _track_url(record)
        raise SourceEmpty(f"video.get не вернул запись {ref}.")

    def read_track(self, url: str) -> bytes:
        raw = self._transport.get(url)
        if len(raw) > MAX_TRACK_BYTES:
            raise SourceParse("Дорожка превышает допустимый размер.")
        if not raw.lstrip(b"\xef\xbb\xbf").lstrip().startswith(b"WEBVTT"):
            raise SourceParse("Дорожка не является WebVTT.")
        return raw


def _track_url(record: dict) -> str | None:
    """Адрес дорожки субтитров: русская предпочтительнее, иначе первая пригодная."""
    tracks = record.get("subtitles")
    if not isinstance(tracks, list):
        return None
    urls = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        url = track.get("url") or track.get("src")
        if isinstance(url, str) and url.startswith("https://"):
            urls.append((0 if str(track.get("lang", "")).lower().startswith("ru") else 1, url))
    return min(urls)[1] if urls else None
