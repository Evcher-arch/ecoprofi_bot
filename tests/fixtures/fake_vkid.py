"""Подмена VK ID и часов для проверок доступа оператора без сети.

Каждое продление выдаёт новую пару токенов и делает прежний токен обновления
недействительным — как наблюдалось в пробе 2026-11-22. Все значения вымышлены.
"""
import itertools

from ecoprofi.indexer.retry import SourceTimeout

CODE = "fake-code-7f3a"
DEVICE = "fake-device-91c2"


class FakeClock:
    """Подставные часы: time() и sleep(), сдвигающий время."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeVkId:
    """Обмен кода и продление. mode: None, 'invalid_grant', 'network', 'no_refresh'."""

    def __init__(self, lifetime: int = 3600):
        self.lifetime = lifetime
        self.mode = None
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        self.issued: list[str] = []            # все выданные значения — для поиска утечек
        self.grants: list[str] = []
        self.refresh_expires_in = None
        self._n = itertools.count(1)

    @property
    def refreshes(self) -> int:
        return self.grants.count("refresh_token")

    def _issue(self) -> dict:
        n = next(self._n)
        access, refresh = f"fake-access-{n}-c0ffee{n:04d}", f"fake-refresh-{n}-beef{n:04d}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        self.issued += [access, refresh]
        response = {"access_token": access, "refresh_token": refresh, "expires_in": self.lifetime,
                    "token_type": "Bearer", "scope": "vkid.personal_info", "user_id": 12345, "id_token": "fake-id"}
        if self.refresh_expires_in:
            response["refresh_token_expires_in"] = self.refresh_expires_in
        return response

    def post(self, url: str, fields: dict) -> dict:
        grant = fields["grant_type"]
        self.grants.append(grant)
        if self.mode == "network":
            raise SourceTimeout("fake network")
        if grant == "authorization_code":
            if fields.get("code") != CODE or fields.get("device_id") != DEVICE or not fields.get("code_verifier"):
                return {"error": "invalid_request"}
            return self._issue()
        if self.mode == "invalid_grant" or fields.get("refresh_token") not in self.valid_refresh:
            return {"error": "invalid_grant", "error_description": "refresh token is invalid"}
        if fields.get("device_id") != DEVICE:
            return {"error": "invalid_request"}
        self.valid_refresh.discard(fields["refresh_token"])   # прежний токен обновления перестаёт действовать
        response = self._issue()
        if self.mode == "no_refresh":
            self.valid_refresh.discard(response.pop("refresh_token"))
            self.valid_refresh.add(fields["refresh_token"])
        return response

    def revoke(self) -> None:
        """Оператор отозвал доступ приложения."""
        self.valid_access.clear()
        self.valid_refresh.clear()


def callback(state: str, *, code: str = CODE, device: str = DEVICE) -> str:
    return f"https://localhost/callback?code={code}&device_id={device}&state={state}&type=code_v2"
