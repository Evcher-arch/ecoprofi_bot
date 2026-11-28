"""Цикл распознавателя: session → lease → audio → transcribe → result.

    python -m ecoprofi.asr.worker

Окружение: ASR_SERVER (https://…/asr/v1/), ASR_TOKEN_FILE, ASR_TMP, ASR_MODEL,
ASR_COMPUTE_TYPE, ASR_MODELS. Временные файлы удаляются после каждого
задания и при старте после аварийной остановки (FR-15).
"""
import logging
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from ecoprofi.asr import HEARTBEAT_EVERY, PROCESSOR_VERSION, SESSION_EVERY, short_lease
from ecoprofi.asr.worker import __version__
from ecoprofi.asr.worker.client import AsrClient, read_token, sha256_of
from ecoprofi.asr.worker.transcribe import DecodeError, GpuUnavailable, Stopped, Transcriber, vram

log = logging.getLogger("asr-worker")


class LeaseLost(Exception):
    """Аренда больше не действует (409 lease_invalid)."""


class WindowClosed(Exception):
    """Окно закрылось или стоит пауза (heartbeat continue: false, 409 window_closed)."""


class Worker:
    def __init__(self, client: AsrClient, transcriber, tmp_dir: Path, *, sleep=time.sleep, device=vram,
                 heartbeat_every: float = HEARTBEAT_EVERY.total_seconds(),
                 session_every: float = SESSION_EVERY.total_seconds()):
        self.client, self.transcriber, self.tmp = client, transcriber, Path(tmp_dir)
        self.sleep, self.device = sleep, device
        self.heartbeat_every, self.session_every = heartbeat_every, session_every
        self.stopping = threading.Event()
        self.current_lease: str | None = None

    # --- служебное ---

    def clean_tmp(self) -> int:
        """Удалить всё на томе временных файлов."""
        self.tmp.mkdir(parents=True, exist_ok=True)
        removed = 0
        for path in self.tmp.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        return removed

    def session(self) -> dict | None:
        body = {"version": f"asr-worker {__version__}", "model": getattr(self.transcriber, "model_name", "large-v3"),
                "compute_type": getattr(self.transcriber, "compute_type", "float16"), "device": self.device()}
        response = self.client.call("POST", "session", body)
        if response.status == 401:
            log.error("сервер отклонил ключ распознавателя (401): проверьте secrets/asr_worker_token")
            return None
        if response.status != 200:
            log.warning("session: ответ %s", response.status)
            return None
        return response.body

    # --- одно задание ---

    def _heartbeat(self, lease: str, flags: dict, processed: list) -> None:
        while not flags["done"].wait(self.heartbeat_every):
            response = self.client.call("POST", f"jobs/{lease}/heartbeat", {"processed_s": int(processed[0])})
            if response.status == 409:
                flags["lost"] = True
                return
            if response.status == 200 and response.body and response.body.get("continue") is False:
                flags["closed"] = True
                return

    def _fetch_audio(self, lease: str, href: str, target: Path) -> str | None:
        """Скачать звук. Возвращает sha256 или None, если звук недоступен (410)."""
        while True:
            if self.stopping.is_set():
                raise Stopped()
            response = self.client.download(href, target)
            if response.status == 202:
                self.sleep(float(response.headers.get("Retry-After", 15)))
                continue
            if response.status == 599:
                continue           # докачка по Range
            if response.status == 410:
                return None
            if response.status == 409:
                raise LeaseLost()
            if response.status not in (200, 206):
                raise LeaseLost()
            expected = response.headers.get("X-Audio-Sha256")
            actual = sha256_of(target)
            if expected and actual != expected:
                target.unlink(missing_ok=True)
                continue           # повреждённый файл скачивается заново
            return actual

    def process(self, job: dict) -> str:
        """Обработать одно выданное задание. Итог: result | audio_unavailable | lost | closed | gpu | decode."""
        lease = job["lease_id"]
        self.current_lease = lease
        target = self.tmp / f"{short_lease(lease)}.ogg"
        flags = {"done": threading.Event(), "lost": False, "closed": False}
        processed = [0.0]
        try:
            log.info("задание %s взято, аренда %s", job["job"]["ref"], short_lease(lease))
            beat = threading.Thread(target=self._heartbeat, args=(lease, flags, processed), daemon=True)
            beat.start()
            try:
                sha = self._fetch_audio(lease, job["audio"]["href"], target)
                if sha is None:
                    log.info("звук недоступен на сервере: %s", job["job"]["ref"])
                    return "audio_unavailable"

                def progress(seconds):
                    processed[0] = seconds

                def should_stop():
                    return flags["lost"] or flags["closed"] or self.stopping.is_set()

                try:
                    result = self.transcriber.transcribe(str(target), progress=progress, should_stop=should_stop)
                except GpuUnavailable:
                    self.client.call("POST", f"jobs/{lease}/failure",
                                     {"kind": "gpu_unavailable", "detail": "видеокарта недоступна"})
                    return "gpu"
                except DecodeError as exc:
                    self.client.call("POST", f"jobs/{lease}/failure", {"kind": "decode", "detail": str(exc)[:200]})
                    return "decode"
            finally:
                flags["done"].set()
                beat.join(timeout=5)
            if flags["lost"]:
                return "lost"
            body = {"processor_version": PROCESSOR_VERSION, "audio_sha256": sha, **result}
            response = self.client.call("POST", f"jobs/{lease}/result", body, compress=True)
            if response.status in (200, 202):
                log.info("результат отправлен: %s, %d отрезков, %.0f с звука за %.0f с",
                         job["job"]["ref"], len(result["segments"]), result["audio_seconds"],
                         result["processing_seconds"])
                return "result"
            if response.status == 409:
                return "lost"
            log.warning("результат отклонён: %s %s", response.status, response.code)
            return "decode"
        except Stopped:
            if flags["lost"]:
                return "lost"
            self.client.call("POST", f"jobs/{lease}/release", {})
            return "closed"
        except LeaseLost:
            return "lost"
        finally:
            self.current_lease = None
            target.unlink(missing_ok=True)
            self.clean_tmp()

    # --- запуск ---

    def run_window(self, run: dict) -> str:
        """Брать задания, пока очередь не опустеет или не закроется окно."""
        while not self.stopping.is_set():
            response = self.client.call("POST", "jobs/lease", {"run_id": run["id"]})
            if response.status == 204:
                self.client.call("POST", f"runs/{run['id']}/finish", {"outcome": "queue_empty"})
                log.info("очередь пуста, запуск %s завершён", run["id"])
                return "queue_empty"
            if response.status == 409:
                return "window_closed"
            if response.status != 200:
                log.warning("lease: ответ %s", response.status)
                return "error"
            outcome = self.process(response.body)
            if outcome == "closed":
                if not self.stopping.is_set():
                    self.client.call("POST", f"runs/{run['id']}/finish", {"outcome": "window_closed"})
                return "window_closed"
            if outcome == "gpu":
                return "resource_unavailable"
        return "stopped"

    def run_forever(self) -> None:
        removed = self.clean_tmp()
        if removed:
            log.info("удалены временные файлы после остановки: %d", removed)
        while not self.stopping.is_set():
            state = self.session()
            if state and state.get("run"):
                log.info("на связи, окно открыто: запуск %s (%s)", state["run"]["id"], state["run"]["kind"])
                self.run_window(state["run"])
                continue
            if state is not None:
                log.info("на связи, окно закрыто; следующее плановое окно %s", state.get("next_due_at"))
            self.stopping.wait(self.session_every)

    def stop(self, *_):
        self.stopping.set()


def main() -> int:
    logging.basicConfig(level=os.environ.get("ASR_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = os.environ.get("ASR_SERVER", "https://miniapp.bot-alimony.ru/asr/v1/")
    worker = Worker(AsrClient(server, read_token()), Transcriber(), Path(os.environ.get("ASR_TMP", "/tmp/asr")))
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
