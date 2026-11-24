from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import create_app
from test_probe import signed


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {
            "MINIAPP_APP_ID": "54916430",
            "MINIAPP_APP_SECRET": "test-only",
            "MINIAPP_PUBLIC_ORIGIN": "https://probe.example",
        })
        self.env.start()
        self.client = TestClient(create_app(), raise_server_exceptions=False)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.env.stop()

    def test_health_and_static(self) -> None:
        self.assertEqual(self.client.get("/api/probe/health").status_code, 200)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors", response.headers["content-security-policy"])
        self.assertNotIn("x-frame-options", response.headers)

    def test_launch_and_tamper(self) -> None:
        query = signed("test-only", 1_000_000_000)
        with patch("launch_params.time.time", return_value=1_000_000_000):
            response = self.client.post(
                "/api/probe/session",
                json={"launch_query": query},
                headers={"Origin": "https://probe.example"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("token", response.text)
            response = self.client.post(
                "/api/probe/session",
                json={"launch_query": query.replace("12345", "99999")},
                headers={"Origin": "https://probe.example"},
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {"error": {"code": "invalid_launch"}})

    def test_origin_body_and_method(self) -> None:
        self.assertEqual(self.client.post(
            "/api/probe/session", json={}, headers={"Origin": "https://evil.example"}
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/probe/session", content=b"x" * (32 * 1024 + 1),
            headers={"Origin": "https://probe.example", "Content-Type": "application/json"},
        ).status_code, 413)
        self.assertEqual(self.client.delete(
            "/api/probe/session", headers={"Origin": "https://probe.example"}
        ).status_code, 405)
        self.assertEqual(self.client.post(
            "/api/probe/session", json={}
        ).status_code, 422)
        self.assertEqual(self.client.post(
            "/api/probe/session", json={}, headers={"Origin": "https://vk.ru"}
        ).status_code, 422)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)


if __name__ == "__main__":
    unittest.main()
