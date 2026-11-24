"""Общие зависимости bot: один ClubMembers и одна причина оповещения club."""
from __future__ import annotations

import logging
from collections.abc import Callable

from ecoprofi.bot.club import ClubMembers
from ecoprofi.indexer import alerts as alert_causes

log = logging.getLogger("ecoprofi.bot.runtime")


class BotRuntime:
    def __init__(self, *, connect_fn: Callable, club=None):
        self.connect = connect_fn
        self.club = club
        self._club_alert: bool | None = None
        self.miniapp = None

    def get_club(self) -> ClubMembers:
        if self.club is None:
            self.club = ClubMembers.from_env()
        return self.club

    def club_alert(self, opened: bool) -> None:
        """Сохранить общий переход причины; ответ участника не зависит от оповещения."""
        if self._club_alert is opened:
            return
        try:
            with self.connect() as conn:
                if opened:
                    alert_causes.open_alert(conn, "club", alert_causes.text_club())
                else:
                    alert_causes.close_alert(conn, "club")
            self._club_alert = opened
        except Exception as exc:
            log.warning("оповещение club не записано (%s)", type(exc).__name__)
