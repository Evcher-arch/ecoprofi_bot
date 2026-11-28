"""Распознаватель на ПК без GPU: цикл задания, очистка, повторы, ключ вне журнала (FR-15)."""
import gzip
import hashlib
import json
import logging

import httpx
import pytest

from ecoprofi.asr.worker.__main__ import Worker
from ecoprofi.asr.worker.client import AsrClient
from ecoprofi.asr.worker.transcribe import GpuUnavailable

KEY = "worker-key-SECRET"
LEASE = "8d3c1111-2222-3333-4444-555555555555"
AUDIO = b"OggS" + b"\1" * 4096


class Server:
    """Подмена /asr/v1/ поверх httpx.MockTransport."""

    def __init__(self):
        self.calls = []
        self.prepared_after = 1          # сколько раз ответить 202 на audio
        self.leases = [LEASE]
        self.heartbeat = {"continue": True}
        self.result_status = 202
        self.audio_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/asr/v1", "")
        assert request.headers["authorization"] == f"Bearer {KEY}"
        self.calls.append((request.method, path))
        if path == "/session":
            return httpx.Response(200, json={"run": {"id": 41, "kind": "initial", "until": None}, "paused": False,
                                             "next_due_at": None})
        if path == "/jobs/lease":
            if not self.leases:
                return httpx.Response(204)
            lease = self.leases.pop(0)
            return httpx.Response(200, json={"lease_id": lease, "job": {"ref": "-1_2", "duration_s": 10},
                                             "audio": {"href": f"/asr/v1/jobs/{lease}/audio"}})
        if path.endswith("/audio"):
            if self.prepared_after:
                self.prepared_after -= 1
                return httpx.Response(202, headers={"Retry-After": "0"})
            if self.audio_status != 200:
                return httpx.Response(self.audio_status, json={"error": {"code": "audio_unavailable"}})
            return httpx.Response(200, content=AUDIO, headers={"X-Audio-Sha256": hashlib.sha256(AUDIO).hexdigest()})
        if path.endswith("/heartbeat"):
            return httpx.Response(200, json=self.heartbeat)
        if path.endswith("/result"):
            body = json.loads(gzip.decompress(request.content))
            self.result = body
            return httpx.Response(self.result_status, json={"state": "transcribed"})
        if path.endswith(("/failure", "/release")) or "/runs/" in path:
            self.last_body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={})
        return httpx.Response(404)


class FakeTranscriber:
    model_name, compute_type = "large-v3", "float16"

    def __init__(self, error=None, stop_after=None):
        self.error, self.stop_after, self.paths = error, stop_after, []

    def transcribe(self, path, *, progress=None, should_stop=None):
        self.paths.append(path)
        with open(path, "rb") as handle:
            assert handle.read() == AUDIO
        if self.error:
            raise self.error
        progress(5.0)
        return {"language": "ru", "audio_seconds": 10.0, "processing_seconds": 1.0,
                "segments": [{"start_ms": 0, "end_ms": 4000, "text": "речь", "avg_logprob": -0.1,
                              "no_speech_prob": 0.01, "compression_ratio": 1.2}]}


def make(server, tmp_path, transcriber=None):
    client = AsrClient("https://example.invalid/asr/v1/", KEY,
                       http=httpx.Client(transport=httpx.MockTransport(server.handler)), sleep=lambda s: None)
    return Worker(client, transcriber or FakeTranscriber(), tmp_path, sleep=lambda s: None,
                  device=lambda: {"gpu": "Test", "vram_total_mb": 10240, "vram_free_mb": 9000},
                  heartbeat_every=0.01)


def test_full_cycle_and_cleanup(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    (tmp_path / "left-over.ogg").write_bytes(b"x")       # после аварийной остановки
    server = Server()
    worker = make(server, tmp_path)
    assert worker.clean_tmp() == 1
    state = worker.session()
    assert worker.run_window(state["run"]) == "queue_empty"
    assert server.result["processor_version"] == "asr-1/large-v3"
    assert server.result["audio_sha256"] == hashlib.sha256(AUDIO).hexdigest()
    assert ("POST", "/runs/41/finish") in server.calls
    assert not list(tmp_path.iterdir())                  # звук удалён (FR-15)
    assert KEY not in caplog.text and LEASE not in caplog.text


def test_window_closed_on_heartbeat_releases(tmp_path):
    import time as _time
    server = Server()
    server.heartbeat = {"continue": False}

    class Slow(FakeTranscriber):
        def transcribe(self, path, *, progress=None, should_stop=None):
            for _ in range(200):
                if should_stop():
                    from ecoprofi.asr.worker.transcribe import Stopped
                    raise Stopped()
                _time.sleep(0.01)
            raise AssertionError("не остановлено")

    worker = make(server, tmp_path, Slow())
    assert worker.run_window({"id": 41}) == "window_closed"
    assert any(path.endswith("/release") for _, path in server.calls)
    assert not list(tmp_path.iterdir())


def test_gpu_unavailable_reports_failure(tmp_path):
    server = Server()
    worker = make(server, tmp_path, FakeTranscriber(error=GpuUnavailable("cuda")))
    assert worker.run_window({"id": 41}) == "resource_unavailable"
    assert server.last_body["kind"] == "gpu_unavailable"


def test_audio_unavailable_takes_next(tmp_path):
    server = Server()
    server.audio_status = 410
    worker = make(server, tmp_path)
    assert worker.run_window({"id": 41}) == "queue_empty"
    assert not any(path.endswith("/result") for _, path in server.calls)


def test_network_errors_back_off_exponentially():
    delays = []
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] <= 8:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"run": None})

    client = AsrClient("https://example.invalid/asr/v1/", KEY,
                       http=httpx.Client(transport=httpx.MockTransport(handler)), sleep=delays.append)
    assert client.call("POST", "session", {}).status == 200
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]


def test_unauthorized_is_not_retried(tmp_path):
    client = AsrClient("https://example.invalid/asr/v1/", "bad",
                       http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
                       sleep=lambda s: pytest.fail("401 не повторяется"))
    worker = Worker(client, FakeTranscriber(), tmp_path, device=lambda: None)
    assert worker.session() is None
