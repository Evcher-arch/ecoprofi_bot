"""Синтетические launch-подписи, часы и управляемые барьеры Mini App."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi.testclient import TestClient

from ecoprofi.bot.api import create_app
from ecoprofi.db import connect

SECRET = "miniapp-test-secret"
APP_ID = 54916430
NOW = 1_700_000_000
ORIGIN = "https://miniapp.test"


def launch_query(*, app_id=APP_ID, user_id=12345, ts=NOW, extra=()):
    params = [("vk_app_id", str(app_id)), ("vk_user_id", str(user_id)), ("vk_ts", str(ts)),
              ("vk_language", "ru"), ("vk_access_token_settings", ""), *extra]
    canonical = urlencode(sorted(params))
    sign = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return urlencode([*params, ("sign", sign)])


class MiniAppStand:
    def __init__(self, stand):
        self.stand = stand
        self.owner = stand.owner
        self.club = stand.club
        self.user_id = 12345
        self.club.members.add(self.user_id)
        self.now = NOW
        env = {
            "MINIAPP_ENABLED": "true", "MINIAPP_APP_ID": str(APP_ID),
            "MINIAPP_APP_SECRET": SECRET, "MINIAPP_PUBLIC_ORIGIN": ORIGIN,
            "CLUB_URL": "https://vk.ru/ecoprofi",
        }
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.clock = patch("ecoprofi.bot.miniapp.config.utcnow", side_effect=self.utcnow)
        self.clock.start()
        self.app = create_app(connect_fn=lambda: connect(stand.bot_dsn), ai=stand.ai,
                              service_key="test-service-key", club=self.club, miniapp=True,
                              miniapp_worker=False)   # тесты управляют worker вручную
        self.client = TestClient(self.app)
        self.client.__enter__()

    def utcnow(self):
        return datetime.fromtimestamp(self.now, timezone.utc)

    def advance(self, seconds):
        self.now += seconds

    def query(self, *, user_id=None, ts=None, extra=()):
        return launch_query(user_id=user_id or self.user_id, ts=self.now if ts is None else ts, extra=extra)

    def post(self, path, body, *, token=None, origin=ORIGIN):
        headers = {"Origin": origin} if origin else {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(path, json=body, headers=headers)

    def get(self, path, token):
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def login(self, *, user_id=None, extra=()):
        if user_id:
            self.club.members.add(user_id)
        response = self.post("/api/miniapp/v1/session", {"launch_query": self.query(user_id=user_id, extra=extra)})
        assert response.status_code == 201, response.text
        return response.json()

    def close(self):
        self.client.__exit__(None, None, None)
        self.clock.stop()
        self.env.stop()
