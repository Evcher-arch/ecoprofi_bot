from ecoprofi.telemetry import sanitize


def test_secrets_and_temporary_urls_are_removed():
    clean = sanitize({
        "access_token": "vk1.a.secret", "api_key": "k", "SERVICE_KEY": "s", "redirect_uri": "https://localhost/callback",
        "track_url": "https://vkvd.example/sub.vtt?sign=abc",
        "note": "дорожка https://vkvd.example/sub.vtt?sign=abc прочитана",
        "url": "https://vkvideo.ru/video-95098598_456240391?t=1563s",
        "nested": [{"password": "p", "episodes": 3}],
        "materials": [1, 2],
    })
    assert clean == {
        "note": "дорожка [url] прочитана",
        "url": "https://vkvideo.ru/video-95098598_456240391?t=1563s",
        "nested": [{"episodes": 3}],
        "materials": [1, 2],
    }
