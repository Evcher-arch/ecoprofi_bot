"""Распознавание записей без субтитров (specs/006-video-asr-transcriber).

Серверная часть (gateway, jobs, window, audio, publish, workers, команды
оператора) работает в образе indexer на VPS. Распознаватель на ПК — подпакет
ecoprofi.asr.worker; он не импортирует серверные модули и этот файл
использует только ради констант протокола.
"""
from datetime import timedelta

PROCESSOR_VERSION = "asr-1/large-v3"

LEASE_TTL = timedelta(minutes=20)          # срок аренды без продления
HEARTBEAT_EVERY = timedelta(minutes=2)     # продление аренды распознавателем
SESSION_EVERY = timedelta(minutes=5)       # «я на связи» в простое
DEADLINE_FACTOR = 1.5                      # предел на запись: 1,5 × длительность звука
DEADLINE_EXTRA = timedelta(minutes=15)     # … плюс 15 минут
ATTEMPTS_PER_RUN = 3                       # попыток одной записи в одном запуске
PERMANENT_AFTER_RUNS = 3                   # запусков подряд со сбоем до постоянного сбоя
MIN_SPEECH_CHARS = 200                     # меньше — «речь не найдена»
AUDIO_RECHECK = timedelta(days=7)          # перепроверка «звук недоступен»
RETRY_DELAY = timedelta(minutes=5)         # пауза перед повтором в том же запуске (× номер попытки)
PUBLISH_RETRY = timedelta(minutes=15)      # пауза перед повтором публикации
CLIENT_BACKOFF_MIN = 5.0                   # секунды, повторы клиента при сбое сети
CLIENT_BACKOFF_MAX = 300.0

TMP_LIMIT_BYTES = 2 * 1024 ** 3            # временные файлы звука на VPS
VRAM_REQUIRED_MB = {"float16": 4500, "int8_float16": 3000, "int8": 2500}


def short_lease(lease_id) -> str:
    """Короткий вид аренды для журналов: полное значение не печатается."""
    return str(lease_id)[:8] if lease_id else "-"
