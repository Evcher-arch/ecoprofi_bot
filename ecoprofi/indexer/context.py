"""Сборка рабочего окружения индексатора: доступ, клиент VK, AI Studio, обновление."""
import os

from ecoprofi.ai import AIError, YandexAI
from ecoprofi.db import connect
from ecoprofi.indexer import runs
from ecoprofi.indexer.access import Access, AccessStore
from ecoprofi.indexer.schedule import fetch_schedule
from ecoprofi.indexer.update import update_worklist
from ecoprofi.indexer.vk_sources import VkSources

DEFAULT_SCHEDULE_URL = "https://vk.ru/@eco_manager_iso-raspisanie-lekcii-ekobezbez-granic"


def make_access() -> Access:
    # Состояние и блокировка продления — на отдельном соединении, вне транзакции прохода.
    return Access(AccessStore(), lambda: connect(vector=False))


def make_vk(access: Access | None = None) -> VkSources:
    return VkSources(access or make_access())


def make_ai() -> YandexAI:
    ai = YandexAI.from_env()
    if not ai.doc_model:
        raise AIError("Не задана YANDEX_DOC_EMBEDDING_MODEL.")
    return ai


def schedule_url() -> str:
    return os.environ.get("SCHEDULE_URL") or DEFAULT_SCHEDULE_URL


def update_run(conn, ai, vk, kind: str, *, report=None) -> dict:
    """Обновление: расписание лекций, разница вложений, отложенные материалы."""
    url = schedule_url()
    return runs.run(conn, ai, vk, kind, report=report,
                    worklist=lambda c: update_worklist(c, vk, ai, fetch=lambda: fetch_schedule(url)))
