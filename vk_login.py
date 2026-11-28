"""VK operator login — complete flow, saves access.json locally then uploads to VPS."""
import os, sys, time, secrets, base64, hashlib, json
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

TOKEN_URL = "https://id.vk.ru/oauth2/auth"
AUTH_URL = "https://id.vk.ru/authorize"
REDIRECT = "https://localhost/callback"

app = os.environ.get("VK_ID_APP", "54913053")
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
state = secrets.token_urlsafe(32)

auth_url = AUTH_URL + "?" + urlencode(dict(
    client_id=app, redirect_uri=REDIRECT, state=state,
    response_type="code", code_challenge=challenge,
    code_challenge_method="S256"
))
print("Откройте в браузере:")
print(auth_url)
print("\nВставьте адрес возврата (https://localhost/callback?...):")
callback = input().strip()

parsed = urlsplit(callback.strip())
if (parsed.scheme, parsed.netloc, parsed.path) != ("https", "localhost", "/callback"):
    print("ERROR: неверный адрес возврата", file=sys.stderr)
    sys.exit(1)

fields = parse_qs(parsed.query)
if fields.get("state") != [state]:
    print("ERROR: state не совпал", file=sys.stderr)
    sys.exit(1)
if not all(len(fields.get(k, [])) == 1 for k in ("code", "device_id")):
    print("ERROR: нет code/device_id", file=sys.stderr)
    sys.exit(1)

code = fields["code"][0]
device_id = fields["device_id"][0]

print("\nОбмен кода на токен...")
req = Request(TOKEN_URL, data=urlencode(dict(
    grant_type="authorization_code", code=code, device_id=device_id,
    code_verifier=verifier, client_id=app, redirect_uri=REDIRECT, state=state
)).encode())

try:
    with urlopen(req, timeout=35) as resp:
        response = json.load(resp)
except HTTPError as e:
    try:
        body = json.load(e)
    except ValueError:
        body = None
    print(f"ERROR: HTTP {e.code}, body={body}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

if not response.get("access_token"):
    print(f"ERROR: нет access_token. Ответ: {response}", file=sys.stderr)
    sys.exit(1)
if not response.get("refresh_token"):
    print("ERROR: нет refresh_token", file=sys.stderr)
    sys.exit(1)

# Save locally first
local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.json")
values = {
    "access_token": response["access_token"],
    "refresh_token": response["refresh_token"],
    "device_id": device_id,
    "app": app,
    "expires_at": int(time.time() + response.get("expires_in", 3600))
}
with open(local_path, "w") as f:
    json.dump(values, f)
print(f"Сохранено локально: {local_path}")
print(f"expires_in: {response.get('expires_in')}s")
print("\nЗагрузка на VPS...")

# Upload to VPS
import paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("79.174.78.44", username="root", password="2wPChBsRddjJguuP", timeout=15)
sftp = c.open_sftp()
sftp.put(local_path, "/opt/ecoprofi/secrets/access.json")
sftp.close()
c.close()
print("Загружено на VPS: /opt/ecoprofi/secrets/access.json")

# Update DB state on VPS
_, s, _ = c.exec_command(
    'cd /opt/ecoprofi && docker exec ecoprofi-db-1 psql -U ecoprofi_owner -d ecoprofi -c '
    + "'" + "UPDATE operator_access SET state = 'active', relogin_verified_at = now(), "
    "logged_in_at = now(), access_expires_at = now() + interval '1 hour', "
    "refresh_expires_at = now() + interval '24 hours', last_denial = NULL" + "'"
)
print("DB state:", s.read().decode().strip())

print("\nГотово! Перезапускаю indexer-daemon...")
_, s, _ = c.exec_command("cd /opt/ecoprofi && docker compose restart indexer-daemon")
print(s.read().decode().strip())
