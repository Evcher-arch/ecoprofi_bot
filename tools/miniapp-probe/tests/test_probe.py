from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from urllib.parse import urlencode

from launch_params import LaunchError, verify_launch

OFFICIAL_SECRET = "wvl68m4dR1UpLrVRli"
OFFICIAL_QUERY = (
    "vk_user_id=494075&vk_app_id=6736218&vk_is_app_user=1&"
    "vk_are_notifications_enabled=1&vk_language=ru&vk_access_token_settings=&"
    "vk_platform=android&vk_ts=1700000000&"
    "sign=GC5oJiHTXXoTRSq9-Dz2hvYcntxE90bfXFzuHx0PFXw"
)


def signed(secret: str, now: int, extra: tuple[str, str] | None = None) -> str:
    params = [
        ("vk_app_id", "54916430"),
        ("vk_user_id", "12345"),
        ("vk_ts", str(now)),
    ]
    if extra:
        params.append(extra)
    canonical = urlencode(sorted(params))
    sign = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return urlencode([*params, ("sign", sign)])


class LaunchParamsTest(unittest.TestCase):
    def test_official_documented_example(self) -> None:
        launch = verify_launch(
            OFFICIAL_QUERY,
            secret=OFFICIAL_SECRET,
            expected_app_id=6736218,
            check_age=False,
        )
        self.assertEqual(launch.user_id, 494075)
        self.assertIn("vk_access_token_settings=", launch.canonical)

    def test_current_official_github_example(self) -> None:
        # Старый официальный URL не содержит vk_ts: он проверяет формулу,
        # но не является допустимым новым запуском по политике этого проекта.
        from urllib.parse import parse_qsl
        from launch_params import _canonical_query
        query = OFFICIAL_QUERY.replace("&vk_ts=1700000000", "").replace(
            "GC5oJiHTXXoTRSq9-Dz2hvYcntxE90bfXFzuHx0PFXw",
            "htQFduJpLxz7ribXRZpDFUH-XEUhC9rBPTJkjUFEkRA",
        )
        pairs = parse_qsl(query, keep_blank_values=True)
        canonical = _canonical_query(pairs, space_as_plus=True)
        actual = base64.urlsafe_b64encode(
            hmac.new(OFFICIAL_SECRET.encode(), canonical.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        self.assertEqual(actual, dict(pairs)["sign"])
        with self.assertRaises(LaunchError):
            verify_launch(
                query, secret=OFFICIAL_SECRET, expected_app_id=6736218, check_age=False
            )

    def test_inconsistent_documented_php_sign_rejected(self) -> None:
        query = OFFICIAL_QUERY.replace(
            "GC5oJiHTXXoTRSq9-Dz2hvYcntxE90bfXFzuHx0PFXw",
            "exTIBPYTrAKDTHLLm2AwJkmcVcvFCzQUNyoa6wAjvW6k",
        )
        with self.assertRaises(LaunchError):
            verify_launch(
                query, secret=OFFICIAL_SECRET, expected_app_id=6736218, check_age=False
            )

    def test_own_signature_and_tamper(self) -> None:
        query = signed("test-only", 1_000)
        self.assertEqual(
            verify_launch(
                query, secret="test-only", expected_app_id=54916430, now=1_000
            ).user_id,
            12345,
        )
        with self.assertRaises(LaunchError):
            verify_launch(
                query.replace("12345", "54321"),
                secret="test-only",
                expected_app_id=54916430,
                now=1_000,
            )

    def test_age_and_app(self) -> None:
        query = signed("test-only", 1_000)
        with self.assertRaises(LaunchError) as expired:
            verify_launch(
                query, secret="test-only", expected_app_id=54916430, now=1_301
            )
        self.assertEqual(expired.exception.code, "launch_expired")
        with self.assertRaises(LaunchError):
            verify_launch(
                query, secret="test-only", expected_app_id=42, now=1_000
            )

    def test_duplicates_and_invalid_encoding(self) -> None:
        query = signed("test-only", 1_000)
        for bad in (query + "&vk_user_id=1", query + "&vk_unknown=%FF", query + "&&x=1"):
            with self.assertRaises(LaunchError):
                verify_launch(
                    bad, secret="test-only", expected_app_id=54916430, now=1_000
                )


if __name__ == "__main__":
    unittest.main()
