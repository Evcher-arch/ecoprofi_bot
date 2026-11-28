"""Распознавание речи: Whisper large-v3 через faster-whisper (CUDA 12, cuDNN 9).

faster-whisper импортируется лениво: модуль загружается в тестах без GPU.
"""
import os
import subprocess
import time


class GpuUnavailable(RuntimeError):
    """Видеокарта занята, недоступна или не хватает видеопамяти."""


class DecodeError(RuntimeError):
    """Звук не читается."""


class Stopped(RuntimeError):
    """Распознавание прервано: окно закрылось, аренда отменена или сервис останавливают."""


def vram() -> dict | None:
    """Модель видеокарты и видеопамять из nvidia-smi; None — видеокарта не видна."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    name, total, free = [part.strip() for part in out.stdout.strip().splitlines()[0].split(",")]
    return {"gpu": name, "vram_total_mb": int(float(total)), "vram_free_mb": int(float(free))}


class Transcriber:
    def __init__(self, model: str | None = None, compute_type: str | None = None, models_dir: str | None = None,
                 language: str = "ru"):
        self.model_name = model or os.environ.get("ASR_MODEL", "large-v3")
        self.compute_type = compute_type or os.environ.get("ASR_COMPUTE_TYPE", "float16")
        self.models_dir = models_dir or os.environ.get("ASR_MODELS", "/models")
        self.language = language
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self.model_name, device="cuda", compute_type=self.compute_type,
                                           download_root=self.models_dir)
            except (RuntimeError, ValueError) as exc:
                raise GpuUnavailable(type(exc).__name__) from None
        return self._model

    def transcribe(self, path: str, *, progress=None, should_stop=None) -> dict:
        """Распознать файл. progress(processed_s) вызывается по мере готовности отрезков."""
        model = self._load()
        started = time.monotonic()
        try:
            segments, info = model.transcribe(path, language=self.language, vad_filter=True, beam_size=5,
                                              condition_on_previous_text=False)
        except (RuntimeError, MemoryError) as exc:
            if "cuda" in str(exc).lower() or "memory" in str(exc).lower() or isinstance(exc, MemoryError):
                raise GpuUnavailable(type(exc).__name__) from None
            raise DecodeError(type(exc).__name__) from None
        except Exception as exc:  # PyAV и прочие ошибки чтения звука
            raise DecodeError(type(exc).__name__) from None
        result = []
        try:
            for seg in segments:
                if should_stop and should_stop():
                    raise Stopped()
                result.append({"start_ms": int(round(seg.start * 1000)), "end_ms": int(round(seg.end * 1000)),
                               "text": seg.text.strip(), "avg_logprob": round(seg.avg_logprob, 4),
                               "no_speech_prob": round(seg.no_speech_prob, 4),
                               "compression_ratio": round(seg.compression_ratio, 4)})
                if progress:
                    progress(seg.end)
        except RuntimeError as exc:
            if isinstance(exc, Stopped):
                raise
            raise GpuUnavailable(type(exc).__name__) from None
        # Нулевые и перевёрнутые интервалы сервер отклоняет.
        result = [s for s in result if s["text"] and 0 <= s["start_ms"] < s["end_ms"]]
        return {"language": info.language or self.language, "audio_seconds": round(float(info.duration), 1),
                "processing_seconds": round(time.monotonic() - started, 1), "segments": result}
