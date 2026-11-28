"""Подготовка звука на сервере (research R1, вариант A).

Свежий адрес медиа берётся из video.get доступом оператора и существует только
в памяти процесса: он передаётся ffmpeg аргументом и не пишется в журналы,
БД и ответы API. ffmpeg извлекает звук в Opus 16 кГц моно с низким
приоритетом. Файл лежит на томе asr-tmp до результата, отказа или истечения
аренды.
"""
import hashlib
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ecoprofi.asr import TMP_LIMIT_BYTES, jobs, short_lease
from ecoprofi.indexer.retry import SourceDeleted, SourceDenied, SourceEmpty, SourceError, SourceTimeout

log = logging.getLogger("ecoprofi.asr.audio")

TMP_DIR = Path(os.environ.get("ASR_TMP", "/tmp/asr"))
FFMPEG_TIMEOUT = int(os.environ.get("ASR_FFMPEG_TIMEOUT", "1800"))
FFMPEG_ARGS = ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k"]
# Прогрессивные файлы от меньшего качества к большему, затем потоки.
MP4_ORDER = ("mp4_144", "mp4_240", "mp4_360", "mp4_480", "mp4_720", "mp4_1080", "mp4_1440", "mp4_2160")
STREAM_ORDER = ("dash_sep", "hls", "dash_uni", "dash_webm", "hls_ondemand", "dash_ondemand")
_SECRET = re.compile(r"https?://\S+")


class AudioUnavailable(Exception):
    """Сервер не может получить звук. reason — denied | no_file | deleted."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


class AudioTemporary(Exception):
    """Временный сбой (сеть, тайм-аут, нет места на томе)."""


@dataclass(frozen=True)
class MediaStreams:
    """Адреса медиа одной записи. Значения не сохраняются и не выводятся."""
    files: dict
    duration: int | None
    date: int | None

    def fields(self) -> list[str]:
        """Имена доступных полей — без адресов (для пробы)."""
        return sorted(self.files)

    def separate_audio(self) -> bool:
        return "dash_sep" in self.files

    def choose(self) -> tuple[str, str] | None:
        for key in MP4_ORDER + STREAM_ORDER:
            url = self.files.get(key)
            if isinstance(url, str) and url.startswith("https://"):
                return key, url
        return None


def scrub(text: str) -> str:
    """Убрать адреса из текста ошибки перед записью в журнал."""
    return _SECRET.sub("<адрес>", text or "")


def ffmpeg_command(url: str, out: Path) -> list[str]:
    return ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", url, *FFMPEG_ARGS, str(out)]


def tmp_usage(directory: Path = TMP_DIR) -> int:
    if not directory.exists():
        return 0
    return sum(p.stat().st_size for p in directory.iterdir() if p.is_file())


def extract(url: str, out: Path, *, runner=subprocess.run, timeout: int = FFMPEG_TIMEOUT) -> None:
    """Извлечь звук. Ошибка ffmpeg — временный сбой; адрес в журнал не попадает."""
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = runner(ffmpeg_command(url, out), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise AudioTemporary("ffmpeg: тайм-аут") from None
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        detail = scrub((result.stderr or "").strip().splitlines()[-1] if result.stderr else "")
        if re.search(r"\b(403|401) Forbidden|\b403\b", result.stderr or ""):
            raise AudioUnavailable("denied", "ffmpeg: доступ запрещён")
        if re.search(r"\b404\b|Not Found|\b410\b", result.stderr or ""):
            raise AudioTemporary("ffmpeg: адрес истёк")
        raise AudioTemporary(f"ffmpeg: код {result.returncode} {detail}".strip())


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_streams(vk, owner_id: int, item_id: int) -> MediaStreams:
    try:
        raw = vk.media_streams(owner_id, item_id)
        streams = MediaStreams(raw["files"], raw.get("duration"), raw.get("date"))
    except SourceDeleted:
        raise AudioUnavailable("deleted", "video.get: запись удалена") from None
    except SourceDenied:
        raise AudioUnavailable("denied", "video.get: доступ запрещён") from None
    except SourceEmpty:
        raise AudioUnavailable("deleted", "video.get: запись не возвращена") from None
    except SourceTimeout as exc:
        raise AudioTemporary(scrub(str(exc))) from None
    except SourceError as exc:
        raise AudioTemporary(scrub(str(exc))) from None
    if streams.choose() is None:
        raise AudioUnavailable("no_file", "video.get: нет медиафайла")
    return streams


def produce(vk, owner_id: int, item_id: int, out: Path, *, runner=subprocess.run) -> tuple[MediaStreams, float]:
    """Получить свежий адрес и извлечь звук. При истёкшем адресе — один повтор со свежим."""
    started = time.monotonic()
    for attempt in (1, 2):
        streams = fetch_streams(vk, owner_id, item_id)
        _, url = streams.choose()
        try:
            extract(url, out, runner=runner)
            return streams, time.monotonic() - started
        except AudioTemporary:
            if attempt == 2:
                raise
        finally:
            url = None    # адрес не переживает попытку
    raise AudioTemporary("ffmpeg: не удалось")


def prepare(conn, vk, job: dict, *, runner=subprocess.run, directory: Path = TMP_DIR) -> str:
    """Подготовить звук по аренде. Возвращает итог: leased | audio_unavailable | retry | deferred."""
    if tmp_usage(directory) >= TMP_LIMIT_BYTES:
        return "deferred"         # задание остаётся в preparing до освобождения места
    out = directory / f"{short_lease(job['lease_id'])}_{abs(job['owner_id'])}_{job['item_id']}.ogg"
    key = jobs.ref(job["owner_id"], job["item_id"])
    try:
        streams, seconds = produce(vk, job["owner_id"], job["item_id"], out, runner=runner)
    except AudioUnavailable as exc:
        log.info("звук недоступен: %s (%s)", key, exc.reason)
        jobs.audio_unavailable(conn, job, exc.reason)
        return "audio_unavailable"
    except AudioTemporary as exc:
        log.warning("временный сбой подготовки звука: %s: %s", key, scrub(str(exc)))
        with conn.transaction():
            current = conn.execute("SELECT * FROM asr_job WHERE owner_id = %s AND item_id = %s FOR UPDATE",
                                   (job["owner_id"], job["item_id"])).fetchone()
            if current and current["lease_id"] == job["lease_id"] and current["state"] == "preparing":
                jobs._defer_or_retry(conn, current, "server", jobs._now())
        return "retry"
    version = jobs.source_version(job["owner_id"], job["item_id"], streams.duration, streams.date)
    try:
        jobs.prepared(conn, job, path=str(out), size=out.stat().st_size, sha256=sha256_of(out),
                      duration_s=streams.duration, version=version)
    except jobs.LeaseInvalid:
        out.unlink(missing_ok=True)
        return "retry"
    log.info("звук готов: %s, аренда %s, %.0f с подготовки", key, short_lease(job["lease_id"]), seconds)
    return "leased"


def cleanup(conn, directory: Path = TMP_DIR, *, everything: bool = False) -> int:
    """Удалить файлы без действующей аренды (FR-15, принцип IV). everything — удалить всё.

    Файл принадлежит аренде, если его имя начинается с короткого вида lease_id
    задания в preparing (идёт извлечение) или leased (ждёт скачивания).
    """
    if not directory.exists():
        return 0
    keep = set()
    if not everything:
        keep = {short_lease(row["lease_id"]) for row in conn.execute(
            "SELECT lease_id FROM asr_job WHERE state IN ('preparing', 'leased') AND lease_id IS NOT NULL").fetchall()}
        conn.commit()
    removed = 0
    for path in directory.iterdir():
        if path.is_file() and path.name.split("_", 1)[0] not in keep:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def probe(vk, owner_id: int, item_id: int, *, runner=subprocess.run, directory: Path = TMP_DIR) -> dict:
    """Проба истории 1: поля медиа, размер и время подготовки. Файл удаляется сразу."""
    out = directory / f"probe_{abs(owner_id)}_{item_id}.ogg"
    try:
        streams, seconds = produce(vk, owner_id, item_id, out, runner=runner)
        size = out.stat().st_size
    except AudioUnavailable as exc:
        return {"ref": jobs.ref(owner_id, item_id), "outcome": "audio_unavailable", "reason": exc.reason}
    except AudioTemporary as exc:
        return {"ref": jobs.ref(owner_id, item_id), "outcome": "retry", "error": scrub(str(exc))}
    finally:
        out.unlink(missing_ok=True)
    chosen, _ = streams.choose()
    return {"ref": jobs.ref(owner_id, item_id), "outcome": "ok", "fields": streams.fields(), "chosen": chosen,
            "separate_audio": streams.separate_audio(), "duration_s": streams.duration, "audio_bytes": size,
            "prepare_seconds": round(seconds, 1)}
