"""One-time VK ID user authorization and lecture attachment check."""
import base64
import getpass
import hashlib
import html
import math
from collections import Counter
import json
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

REDIRECT = "https://localhost/callback"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"
WALL_URL = "https://api.vk.com/method/wall.getById"


def check_network():
    ok = True
    for url in (TOKEN_URL, WALL_URL):
        host = urlsplit(url).hostname
        try:
            with urlopen(Request(url, method="HEAD"), timeout=15) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
        except (OSError, TimeoutError) as exc:
            print(f"{host}: connection failed | {exc}")
            ok = False
            continue
        print(f"{host}: HTTPS OK | HTTP {status}")
    return ok


def post(url, fields):
    print(f"POST {urlsplit(url).hostname}{urlsplit(url).path}", flush=True)
    request = Request(url, data=urlencode(fields).encode())
    try:
        with urlopen(request, timeout=35) as response:
            return json.load(response)
    except HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} ({urlsplit(url).hostname})") from None


def callback_fields(address, state):
    parsed = urlsplit(address)
    if (parsed.scheme, parsed.netloc, parsed.path) != ("https", "localhost", "/callback"):
        raise ValueError("Expected https://localhost/callback")
    fields = parse_qs(parsed.query)
    if fields.get("state") != [state]:
        raise ValueError("Callback state mismatch. Restart the test.")
    if not all(len(fields.get(k, [])) == 1 for k in ("code", "device_id")):
        raise ValueError("Authorization did not return code and device_id.")
    return {k: fields[k][0] for k in ("code", "device_id")}


def probe_document(url):
    if not url or urlsplit(url).scheme != "https":
        return "no HTTPS file URL"
    try:
        with urlopen(Request(url, headers={"Range": "bytes=0-31"}), timeout=20) as reply:
            head = reply.read(32)
            signature = "other"
            if head.startswith(b"%PDF-"):
                signature = "PDF"
            elif head.startswith(b"{\\rtf"):
                signature = "RTF"
            return f"HTTP={reply.status} | signature={signature}"
    except HTTPError as exc:
        return f"HTTP={exc.code}"
    except (OSError, ValueError) as exc:
        return f"read failed ({type(exc).__name__})"


def show_materials(target, item, inspect_files=False):
    ids = set()
    for index, source in enumerate([item, *item.get("copy_history", [])]):
        origin = "post" if index == 0 else "repost"
        texts = [source.get("text", "")]
        for attachment in source.get("attachments", []):
            kind = attachment.get("type")
            obj = attachment.get(kind, {})
            if kind in ("doc", "video") and "owner_id" in obj and "id" in obj:
                ref = f"{kind}{obj['owner_id']}_{obj['id']}"
                ids.add(ref)
                if inspect_files:
                    detail = probe_document(obj.get("url")) if kind == "doc" else f"seconds={obj.get('duration')}"
                    print(f"Material {target} | {origin} attachment | {ref} | {obj.get('title', '')} | {detail}")
            if kind == "link":
                texts.append(obj.get("url", ""))
        for position, value in enumerate(texts):
            refs = set(re.findall(r"(?:doc|video)-?\d+_\d+", unquote(value)))
            ids.update(refs)
            if inspect_files:
                location = "text" if position == 0 else "link"
                for ref in sorted(refs):
                    print(f"Material {target} | {origin} {location} | {ref}")
    print(f"Post {target}: found | IDs: {', '.join(sorted(ids)) or 'none'}")


def inspect_subtitle(ref, track, collect=None):
    prefix = f"Subtitle {ref}"
    if not isinstance(track, dict):
        print(f"{prefix} | entry type={type(track).__name__}")
        return
    keys = ", ".join(sorted(track))
    url = track.get("url") or track.get("src")
    if not isinstance(url, str) or not url.startswith("https://"):
        print(f"{prefix} | no HTTPS track URL | fields={keys}")
        return
    prefix += f" | host={urlsplit(url).hostname}"
    if collect is not None or "--inspect-videos-ipv4" in sys.argv[1:]:
        if any(ord(c) < 32 or c in '"\\' for c in url):
            print(f"{prefix} | invalid URL")
            return
        timings = ("IP=%{remote_ip} | DNS=%{time_namelookup} | TCP=%{time_connect}"
                   " | TLS=%{time_appconnect} | first=%{time_starttransfer}"
                   " | redirects=%{num_redirects} | total=%{time_total}")
        for label, address in (("track", url), ("root", f"https://{urlsplit(url).netloc}/")):
            try:
                result = subprocess.run(
                    ["curl.exe", "-q", "-4", "--silent", "--location", "--globoff",
                 "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10",
                 "--max-time", "35", "--max-filesize", "2000000",
                     "--write-out", "\n%{http_code}\t%{content_type}\t" + timings, "--config", "-"],
                    input=f'url = "{address}"\n'.encode(), capture_output=True, timeout=40)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"{prefix} | {label} | {type(exc).__name__}")
                continue
            raw, _, trailer = result.stdout.rpartition(b"\n")
            status, _, rest = trailer.decode("ascii", "replace").partition("\t")
            mime, _, stats = rest.partition("\t")
            print(f"{prefix} | {label} | exit={result.returncode} | HTTP={status} | {stats}", flush=True)
            if label == "track" and result.returncode == 0 and status in ("200", "206"):
                if collect is not None:
                    if status == "200" and raw.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
                        collect(raw)
                    return
                report_subtitle(prefix, raw, status, mime, keys)
                return
        return
    for attempt in (1, 2):
        stage = "open"
        try:
            with urlopen(url, timeout=25) as response:
                stage = "read"
                raw = response.read(2_000_001)
                status = response.status
                mime = response.headers.get_content_type()
            break
        except HTTPError as exc:
            print(f"{prefix} | HTTP={exc.code}")
            exc.close()
            return
        except (OSError, ValueError) as exc:
            cause = exc.reason if isinstance(exc, URLError) else exc
            category = type(cause).__name__
            if isinstance(cause, ssl.SSLCertVerificationError):
                category = f"certificate verify_code={cause.verify_code}"
            elif isinstance(cause, ssl.SSLError):
                category = f"TLS {cause.reason}"
            elif isinstance(cause, socket.gaierror):
                category = "DNS"
            elif isinstance(cause, TimeoutError):
                category = "TLS handshake timeout" if "handshake" in str(cause).lower() else "timeout"
            elif "tunnel connection failed" in str(cause).lower():
                category = "proxy tunnel failed"
            code = getattr(cause, "winerror", None) or getattr(cause, "errno", None)
            print(f"{prefix} | attempt={attempt} | stage={stage} | reason={category} | code={code}", flush=True)
            if attempt == 2 or not isinstance(cause, (TimeoutError, ConnectionResetError, socket.gaierror)):
                return
            time.sleep(1)
    report_subtitle(prefix, raw, status, mime, keys)


def report_subtitle(prefix, raw, status, mime, keys):
    body = raw[:2_000_000].decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    stamp = r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}"
    cue = re.compile(rf"^({stamp})[ \t]+-->[ \t]+({stamp})[^\n]*\n(.+)", re.S)
    count = 0
    first = ""
    for block in re.split(r"\n[ \t]*\n", body):
        match = re.search(cue.pattern, block, re.M | re.S)
        if match:
            caption = " ".join(re.sub(r"<[^>]*>", "", match[3]).split())
            if caption:
                count += 1
                if not first:
                    first = f"{match[1]} --> {match[2]} | {caption[:140]}"
    print(f"{prefix} | HTTP={status} | type={mime} | cues={count} | capped={len(raw) > 2_000_000}")
    if first:
        print(f"{prefix} | first={first}")
    else:
        print(f"{prefix} | no VTT/SRT cues recognized | fields={keys}")


def inspect_video(video, token, corpus=None):
    ref = f"{video['owner_id']}_{video['id']}"
    visited = set()

    def probe_files(record, origin):
        raw_files = record.get("files")
        files = raw_files if isinstance(raw_files, dict) else {}
        file_keys = ", ".join(sorted(files)) if files else (
            "absent" if "files" not in record else f"empty or {type(raw_files).__name__}")
        hints = sorted(k for k in record if re.search(r"subtitle|caption|transcript", k, re.I))
        print(f"Video {ref} | {origin} | player={bool(record.get('player'))} | files={file_keys}")
        for key in hints:
            value = record[key]
            detail = str(value) if isinstance(value, (bool, int)) else type(value).__name__
            if isinstance(value, (list, dict)):
                detail += f" ({len(value)})"
            print(f"Video {ref} | {origin} | {key}={detail}")
        if not hints:
            print(f"Video {ref} | {origin} | subtitle fields absent")
        tracks = record.get("subtitles")
        if isinstance(tracks, list):
            for track in tracks:
                if corpus is None:
                    inspect_subtitle(ref, track)
                elif ref not in corpus:
                    def collect(raw):
                        passages = subtitle_passages(raw)
                        if passages:
                            corpus[ref] = (record.get("title") or video.get("title") or ref, passages)
                    inspect_subtitle(ref, track, collect)
        if corpus is not None:
            return ref in corpus
        for name, url in files.items():
            if not (re.fullmatch(r"mp4_\d+|(?:hls|dash)(?:_[a-z0-9_]+)?", name)
                    and isinstance(url, str) and url.startswith("https://") and url not in visited):
                continue
            visited.add(url)
            try:
                req = Request(url, headers={"Range": "bytes=0-8191"})
                with urlopen(req, timeout=20) as reply:
                    prefix = reply.read(8192)
                    status = reply.status
            except HTTPError as exc:
                print(f"Video {ref} | {name} | HTTP={exc.code}")
                exc.close()
                continue
            except (OSError, ValueError) as exc:
                print(f"Video {ref} | {name} | {type(exc).__name__}")
                continue
            signature = "unknown"
            if prefix[4:8] == b"ftyp":
                signature = "MP4"
            elif prefix.lstrip().startswith(b"#EXTM3U"):
                signature = "HLS"
            elif b"<MPD" in prefix:
                signature = "DASH"
            print(f"Video {ref} | {name} | HTTP={status} | signature={signature}")
            if signature != "unknown":
                return True
        return False

    if probe_files(video, "attachment"):
        return
    lookup = ref + ("_" + video["access_key"] if video.get("access_key") else "")
    try:
        result = post("https://api.vk.com/method/video.get", dict(
            videos=lookup, access_token=token, v="5.199"))
    except (OSError, ValueError) as exc:
        print(f"Video {ref} | video.get | request failed ({type(exc).__name__})")
        return
    if "error" in result:
        print(f"Video {ref} | video.get | VK {result['error'].get('error_code')}")
        return
    for record in result.get("response", {}).get("items", []):
        if (record.get("owner_id"), record.get("id")) == (video["owner_id"], video["id"]):
            if not probe_files(record, "video.get") and corpus is None:
                print(f"Video {ref} | no verified media source")
            return
    print(f"Video {ref} | video.get | not returned")


def scan_donut(access_token, inspect_files=False, inspect_videos=False, corpus=None):
    missing = {41120, 30861}
    offset = 0
    total = None
    seen = set()
    while missing and (total is None or offset < total):
        fields = dict(owner_id=-95098598, count=100, offset=offset, filter="donut",
                      access_token=access_token, v="5.199")
        for attempt in range(3):
            result = post("https://api.vk.com/method/wall.get", fields)
            error = result.get("error", {})
            if not error:
                break
            if error.get("error_code") != 6 or attempt == 2:
                raise ValueError(f"Donut offset {offset}: VK {error.get('error_code')} | {error.get('error_msg')}")
            time.sleep(1 + attempt)
        page = result["response"]
        if total is None:
            total = page["count"]
        items = page["items"]
        if not items:
            break
        for item in items:
            key = (item.get("owner_id"), item.get("id"))
            seen.add(key)
            if key[0] == -95098598 and key[1] in missing:
                show_materials(key[1], item, inspect_files)
                if inspect_videos:
                    for attachment in item.get("attachments", []):
                        if attachment.get("type") == "video":
                            inspect_video(attachment["video"], access_token, corpus)
                missing.remove(key[1])
        offset += len(items)
        if missing and offset < total:
            time.sleep(0.5)
    print(f"Donut: read={offset} | unique={len(seen)} | initial total={total}")
    for target in sorted(missing):
        print(f"Post {target}: not returned in scanned pages")


def subtitle_passages(raw):
    """Build short passages within a continuous run of captions."""
    body = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    stamp = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
    timing = re.compile(rf"^({stamp})[ \t]+-->[ \t]+({stamp}).*$", re.M)

    def seconds(value):
        result = 0.0
        for part in value.split(":"):
            result = result * 60 + float(part)
        return result

    passages = []
    lines = []
    start = end = 0
    for block in re.split(r"\n[ \t]*\n", body):
        if block.startswith(("NOTE", "STYLE", "REGION")):
            continue
        match = timing.search(block)
        if not match:
            continue
        begin, finish = map(seconds, match.groups())
        caption = " ".join(html.unescape(re.sub(r"<[^>]+>", "", block[match.end():])).split())
        if not caption or finish <= begin:
            continue
        if lines and (begin - end > 5 or begin - start >= 24):
            passages.append((start, end, " ".join(lines)))
            lines = []
        if not lines:
            start, end = begin, finish
        end = max(end, finish)
        if not lines or caption != lines[-1]:
            lines.append(caption)
    if lines:
        passages.append((start, end, " ".join(lines)))
    return passages


def terms(text):
    words = re.findall(r"[а-яa-z0-9]+", text.casefold().replace("ё", "е"))
    stop = {"и", "в", "на", "с", "о", "по", "как", "что", "об", "для", "где", "из", "это", "р"}
    return [w for w in words if w not in stop]


def search_loop(corpus):
    rows = []
    frequency = Counter()
    for ref, (title, passages) in corpus.items():
        for start, end, text in passages:
            words = set(terms(text))
            frequency.update(words)
            rows.append((ref, title, start, end, text, words))
    print(f"Записей загружено: {len(corpus)} из 2. Фрагментов: {len(rows)}.")
    if not rows:
        print("Доступных субтитров нет. Проверьте сообщения загрузки выше.")
        return
    while True:
        query = input("Поиск по субтитрам (Enter — выход): ").strip()
        if not query:
            return
        wanted = set(terms(query))
        matches = []
        for ref, title, start, end, text, words in rows:
            common = wanted & words
            if common:
                weight = sum(math.log1p(len(rows) / frequency[w]) for w in common)
                matches.append((len(common), weight, ref, title, start, end, text))
        if not matches:
            print("Совпадений нет. Попробуйте другое слово или номер документа.")
        for count, _, ref, title, start, end, excerpt in sorted(matches, key=lambda m: (-m[0], -m[1], m[2], m[4]))[:3]:
            t = int(start)
            last = math.ceil(end)
            print(f"\n{title} | {t//60}:{t%60:02d}–{last//60}:{last%60:02d} | совпало слов: {count}/{len(wanted)}")
            print(excerpt)
            print(f"https://vkvideo.ru/video{ref}?t={t}s")


_SHOWN = ("expires_in", "token_type", "scope", "refresh_token_expires_in", "error", "error_description")
_SECRET = ("access_token", "refresh_token", "id_token")


def describe_token(label, token):
    """Показать состав ответа VK ID. Значения токенов и личные данные на экран не выводятся."""
    print(f"\n{label}:")
    for key in sorted(token):
        value = token[key]
        if key in _SECRET:
            print(f"  {key}: есть, длина {len(str(value))}" if value else f"  {key}: пусто")
        elif key in _SHOWN:
            print(f"  {key}: {value}")
        else:
            print(f"  {key}: есть")
    for key in _SECRET[:2]:
        if key not in token:
            print(f"  {key}: НЕТ В ОТВЕТЕ")


def wall_access(access_token):
    """Работает ли токен для чтения платной ленты сообщества."""
    result = post("https://api.vk.com/method/wall.get", dict(
        owner_id=-95098598, count=1, filter="donut", access_token=access_token, v="5.199"))
    if "error" in result:
        err = result["error"]
        return f"VK {err.get('error_code')} | {err.get('error_msg')}"
    return f"работает (постов в платной ленте: {result['response']['count']})"


def probe_refresh(app, device_id, token):
    """Проба OPS-01: выдаёт ли VK ID токен обновления и можно ли продлить доступ без входа.

    Токены держатся только в памяти процесса и на экран не выводятся. Обновление
    заменяет токен обновления новым; после пробы полученные токены нигде не остаются.
    """
    describe_token("Ответ на вход (обмен кода)", token)
    print(f"  чтение платной ленты: {wall_access(token['access_token'])}")
    if not token.get("refresh_token"):
        print("\nИТОГ: токен обновления не выдан. Продление без входа оператора невозможно.")
        return
    current, access = token["refresh_token"], token["access_token"]
    for step in (1, 2):
        state = secrets.token_urlsafe(32)
        try:
            renewed = post(TOKEN_URL, dict(grant_type="refresh_token", refresh_token=current,
                                           client_id=app, device_id=device_id, state=state))
        except ValueError as exc:
            print(f"\nОбновление {step}: отказ | {exc}")
            print("ИТОГ: токен обновления выдан, но обновление не удалось.")
            return
        describe_token(f"Обновление {step}", renewed)
        if not renewed.get("access_token"):
            print("ИТОГ: токен обновления выдан, но обновление не вернуло рабочий токен.")
            return
        if "state" in renewed:
            print(f"  state совпал: {'да' if renewed['state'] == state else 'НЕТ'}")
        print(f"  рабочий токен новый: {'да' if renewed['access_token'] != access else 'нет, тот же'}")
        rotated = renewed.get("refresh_token")
        print("  токен обновления: " + ("заменён новым" if rotated and rotated != current
                                         else "тот же" if rotated else "в ответе нет, действует прежний"))
        print(f"  чтение платной ленты новым токеном: {wall_access(renewed['access_token'])}")
        if step == 1:
            print(f"  чтение платной ленты прежним токеном: {wall_access(access)}")
        access, current = renewed["access_token"], rotated or current
    print("\nИТОГ: токен обновления выдан, два обновления подряд прошли без входа оператора.")
    print("Срок жизни самого токена обновления проба не измеряет: его покажет только время.")


def main():
    app = input("Numeric VK ID application ID: ").strip()
    if not re.fullmatch(r"[0-9]+", app):
        raise ValueError("Expected a numeric application ID.")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    common = dict(client_id=app, redirect_uri=REDIRECT, state=secrets.token_urlsafe(32))
    auth = dict(common, response_type="code", code_challenge=challenge, code_challenge_method="S256")
    if not webbrowser.open("https://id.vk.ru/authorize?" + urlencode(auth)):
        raise ValueError("Could not open the default browser.")
    address = getpass.getpass("Paste the final localhost callback URL here (hidden): ").strip()
    fields = callback_fields(address, common["state"])
    token = post(TOKEN_URL, dict(common, **fields, grant_type="authorization_code", code_verifier=verifier))
    if not token.get("access_token"):
        raise ValueError("VK ID token exchange failed: " + str(token.get("error", "missing access token")))
    print("VK ID: OK")
    if sys.argv[1:] == ["--probe-refresh"]:
        probe_refresh(app, fields["device_id"], token)
        return
    if sys.argv[1:] == ["--copy-token"]:
        # Диагностический режим. Индексатор с функции 003 его не использует: вход — «access login».
        # Передача токена: в буфер обмена, без вывода на экран и в журнал.
        subprocess.run(["clip.exe"], input=token["access_token"].encode("ascii"), check=True)
        print("Токен скопирован в буфер обмена. Вставьте его в скрытое приглашение индексатора.")
        input("После вставки нажмите Enter — буфер обмена будет очищен: ")
        subprocess.run(["clip.exe"], input=b"", check=True)
        print("Буфер обмена очищен.")
        return
    if sys.argv[1:] == ["--search-content"]:
        corpus = {}
        scan_donut(token["access_token"], inspect_videos=True, corpus=corpus)
        search_loop(corpus)
        return
    if sys.argv[1:] in (["--inspect-videos"], ["--inspect-videos-ipv4"]):
        scan_donut(token["access_token"], inspect_videos=True)
        return
    if sys.argv[1:] in (["--scan-donut"], ["--inspect-materials"]):
        scan_donut(token["access_token"], sys.argv[1] == "--inspect-materials")
        return
    if sys.argv[1:] == ["--check-wall"]:
        for wall_filter in ("all", "donut"):
            result = post("https://api.vk.com/method/wall.get", dict(
                owner_id=-95098598, count=1, filter=wall_filter,
                access_token=token["access_token"], v="5.199"))
            if "error" in result:
                err = result["error"]
                print(f"Wall {wall_filter}: VK {err.get('error_code')} | {err.get('error_msg')}")
            else:
                page = result["response"]
                print(f"Wall {wall_filter}: total={page['count']} | returned={len(page['items'])}")
        return
    for target in (41120, 30861):
        result = post(WALL_URL, dict(
            posts=f"-95098598_{target}", access_token=token["access_token"], v="5.199"))
        if "error" in result:
            err = result["error"]
            print(f"Post {target}: VK {err.get('error_code')} | {err.get('error_msg')}")
            continue
        items = result.get("response", [])
        if isinstance(items, dict):
            items = items.get("items", [])
        item = next((p for p in items if p.get("id") == target and p.get("owner_id") == -95098598), None)
        if item is None:
            print(f"Post {target}: not returned")
            continue
        show_materials(target, item)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-network"]:
            raise SystemExit(0 if check_network() else 1)
        main()
    except (ValueError, URLError, TimeoutError) as exc:
        raise SystemExit(str(exc))
