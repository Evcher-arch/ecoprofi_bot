"""Подготовка звука на сервере (R1): выбор потока, ffmpeg, статусы, адреса вне журналов. Без БД."""
import logging

import pytest

from ecoprofi.asr import audio
from ecoprofi.indexer.retry import SourceDenied, SourceEmpty, SourceTimeout
from tests.asr.conftest import FAKE_MEDIA, FakeRunner, FakeVk


def test_lowest_quality_progressive_file_is_chosen():
    streams = audio.MediaStreams({"mp4_720": "https://a/720", "mp4_240": "https://a/240", "hls": "https://a/h"},
                                 600, 1)
    assert streams.choose() == ("mp4_240", "https://a/240")
    assert audio.MediaStreams({"hls": "https://a/h", "dash_sep": "https://a/d"}, 1, 1).choose()[0] == "dash_sep"
    assert audio.MediaStreams({}, 1, 1).choose() is None
    assert streams.fields() == ["hls", "mp4_240", "mp4_720"]


def test_ffmpeg_command_is_niced_opus_mono_16k(tmp_path):
    cmd = audio.ffmpeg_command("https://x", tmp_path / "a.ogg")
    assert cmd[:3] == ["nice", "-n", "10"]
    joined = " ".join(cmd)
    assert "-vn -ac 1 -ar 16000 -c:a libopus -b:a 24k" in joined


def test_probe_reports_fields_and_removes_file(tmp_path):
    vk, runner = FakeVk(), FakeRunner()
    result = audio.probe(vk, -1, 2, runner=runner, directory=tmp_path)
    assert result["outcome"] == "ok" and result["fields"] == ["mp4_144"] and result["audio_bytes"] > 0
    assert not list(tmp_path.iterdir())
    assert FAKE_MEDIA not in repr(result)


@pytest.mark.parametrize("error,reason", [(SourceDenied("x"), "denied"), (SourceEmpty("x"), "deleted")])
def test_unavailable_reasons(tmp_path, error, reason):
    vk = FakeVk()
    vk.records[(-1, 2)] = error
    result = audio.probe(vk, -1, 2, runner=FakeRunner(), directory=tmp_path)
    assert result == {"ref": "-1_2", "outcome": "audio_unavailable", "reason": reason}


def test_no_media_file(tmp_path):
    vk = FakeVk()
    vk.records[(-1, 2)] = {"files": {}, "duration": 10, "date": 1}
    assert audio.probe(vk, -1, 2, runner=FakeRunner(), directory=tmp_path)["reason"] == "no_file"


def test_source_timeout_is_temporary(tmp_path):
    vk = FakeVk()
    vk.records[(-1, 2)] = SourceTimeout("HTTP 504")
    assert audio.probe(vk, -1, 2, runner=FakeRunner(), directory=tmp_path)["outcome"] == "retry"


def test_expired_address_is_refetched_once(tmp_path):
    vk = FakeVk()
    runner = FakeRunner(returncode=1, stderr=f"{FAKE_MEDIA}: Server returned 404 Not Found")
    result = audio.probe(vk, -1, 2, runner=runner, directory=tmp_path)
    assert result["outcome"] == "retry" and vk.calls == 2 and len(runner.commands) == 2


def test_forbidden_stream_is_denied(tmp_path):
    runner = FakeRunner(returncode=1, stderr=f"{FAKE_MEDIA}: Server returned 403 Forbidden")
    assert audio.probe(FakeVk(), -1, 2, runner=runner, directory=tmp_path)["reason"] == "denied"


def test_address_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    runner = FakeRunner(returncode=1, stderr=f"Error opening input {FAKE_MEDIA}")
    result = audio.probe(FakeVk(), -1, 2, runner=runner, directory=tmp_path)
    assert "SECRETSIG" not in caplog.text and "SECRETSIG" not in repr(result)
    assert audio.scrub(f"x {FAKE_MEDIA} y") == "x <адрес> y"


class FakeConn:
    def __init__(self, leases):
        self.leases = leases

    def execute(self, *args):
        rows = [{"lease_id": lease} for lease in self.leases]

        class Result:
            def fetchall(self_inner):
                return rows
        return Result()

    def commit(self):
        pass


def test_cleanup_keeps_only_active_leases(tmp_path):
    (tmp_path / "aaaaaaaa_1_2.ogg").write_bytes(b"1")
    (tmp_path / "bbbbbbbb_1_3.ogg").write_bytes(b"1")
    removed = audio.cleanup(FakeConn(["aaaaaaaa-1111-2222-3333-444444444444"]), tmp_path)
    assert removed == 1 and [p.name for p in tmp_path.iterdir()] == ["aaaaaaaa_1_2.ogg"]
    assert audio.cleanup(FakeConn([]), tmp_path, everything=True) == 1
