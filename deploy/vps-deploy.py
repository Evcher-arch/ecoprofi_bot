#!/usr/bin/env python3
"""
Единый скрипт деплоя бота Экопрофи на VPS.
Работает с любого ПК при наличии Python 3.10+ и Docker.

Требования:
  pip install paramiko

Использование:
  python deploy/vps-deploy.py \\
    --host 79.174.78.44 \\
    --user root \\
    --password <VPS_PASSWORD> \\
    [--project-dir /path/to/repo] \\
    [--domain miniapp.bot-alimony.ru] \\
    [--email admin@bot-alimony.ru]

  Пароль в репозиторий не записывается. Предпочтительно передавать его через
  переменную окружения, а не через --password: аргумент остаётся в истории
  командной оболочки.

  Или через переменные окружения:
    export VPS_HOST=79.174.78.44 VPS_USER=root VPS_PASSWORD=...
    python deploy/vps-deploy.py
"""
import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import io
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("paramiko не найден. Установите: pip install paramiko", file=sys.stderr)
    sys.exit(1)


# ---------- Аргументы ----------

def parse_args():
    p = argparse.ArgumentParser(description="Деплой бота Экопрофи на VPS")
    p.add_argument("--host", default=os.environ.get("VPS_HOST", "79.174.78.44"))
    p.add_argument("--user", default=os.environ.get("VPS_USER", "root"))
    p.add_argument("--password", default=os.environ.get("VPS_PASSWORD", ""))
    p.add_argument("--project-dir", default=os.environ.get("PROJECT_DIR", None))
    p.add_argument("--domain", default=os.environ.get("VPS_DOMAIN", "miniapp.bot-alimony.ru"))
    p.add_argument("--email", default=os.environ.get("VPS_EMAIL", "admin@bot-alimony.ru"))
    # 006: после первой миграции 005_asr.sql сразу поставить распознавание на паузу,
    # чтобы первичный проход не начался до пробы (quickstart §2).
    p.add_argument("--asr-pause", action="store_true", default=os.environ.get("ASR_PAUSE") == "1",
                   help="поставить распознавание на паузу после миграций")
    return p.parse_args()


# ---------- Docker ----------

def build_image(target, tag, project_dir):
    """Собрать Docker-образ по target из Dockerfile."""
    print(f"\n=== Build {target} ===", file=sys.stderr)
    r = subprocess.run(
        ["docker", "build", "--target", target, "-t", tag, "."],
        cwd=str(project_dir),
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  STDERR: {r.stderr[-500:]}", file=sys.stderr)
        sys.exit(1)
    print(f"  OK: {tag}", file=sys.stderr)


def save_images(tags, tar_path):
    """Сохранить образы в tar-архив (без сжатия — gzip отсутствует на Windows)."""
    print(f"\n=== Save images to {tar_path} ===", file=sys.stderr)
    r = subprocess.run(
        ["docker", "save", "-o", tar_path] + tags,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  ERROR: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    size_mb = os.path.getsize(tar_path) / 1024 / 1024
    print(f"  Saved: {size_mb:.1f} MB", file=sys.stderr)


# ---------- SSH helpers ----------

def make_ssh(args):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(args.host, username=args.user, password=args.password, timeout=15)
    return c


def run_ssh(c, cmd, timeout=60, stdin_data=None):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    if stdin_data is not None:
        stdin.write(stdin_data)
        stdin.close()
    out = stdout.read().decode()
    err = stderr.read().decode()
    return out, err


def upload_dir(sftp, c, local, remote, project_root):
    """Загрузить директорию на VPS через tar.gz (быстро)."""
    local_path = Path(local)
    remote_tar = f"{remote}/{local_path.name}.tar.gz"
    
    # Создаём tar.gz в памяти
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in local_path.rglob("*"):
            if item.is_dir():
                continue
            rel = str(item.relative_to(project_root))
            # Пропускаем ненужное
            parts = Path(rel).parts
            if any(p in (".git", "__pycache__", ".gigacode", ".idea", ".claude", ".specify") for p in parts):
                continue
            if item.suffix == ".pyc":
                continue
            tar.add(item, arcname=rel)
    
    buf.seek(0)
    size_mb = len(buf.getvalue()) / 1024 / 1024
    
    # Загружаем tar на VPS
    sftp.putfo(buf, remote_tar)
    print(f"    {local_path.name}: {size_mb:.1f} MB", file=sys.stderr)
    
    # Распаковываем на VPS
    run_ssh(c, f"mkdir -p {remote}/{local_path.name} && tar xzf {remote_tar} -C {remote}/{local_path.name} && rm -f {remote_tar}")


# ---------- Деплой ----------

def deploy(args):
    project_dir = Path(args.project_dir) if args.project_dir else Path.cwd().resolve()
    if not (project_dir / "Dockerfile").exists():
        print(f"Ошибка: Dockerfile не найден в {project_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Project: {project_dir}", file=sys.stderr)
    print(f"VPS: {args.user}@{args.host}", file=sys.stderr)

    # 1. Собираем образы
    tags = {
        "app": "ecoprofi-app:local",
        "indexer": "ecoprofi-indexer:local",
        "miniapp-gateway": "ecoprofi-miniapp-gateway:local",
    }
    for target, tag in tags.items():
        build_image(target, tag, project_dir)

    # 2. Сохраняем в tar
    with tempfile.TemporaryDirectory(prefix="ecoprofi-deploy-") as tmpdir:
        tar_path = os.path.join(tmpdir, "images.tar")
        save_images(list(tags.values()), tar_path)

        # 3. SSH + загрузка файлов
        print("\n=== Upload to VPS ===", file=sys.stderr)
        client = make_ssh(args)
        sftp = client.open_sftp()

        remote_root = "/opt/ecoprofi"

        # Загружаем проект (кроме deploy/, .env, __pycache__, images.*)
        print("  Uploading project files...", file=sys.stderr)
        for item in project_dir.iterdir():
            if item.is_dir():
                if item.name in ("deploy", "__pycache__", ".git", ".gigacode", ".idea", ".claude", ".specify",
                                 "secrets"):
                    continue
                upload_dir(sftp, client, item, remote_root, project_dir)
            elif item.name in (".env", "images.tar", "images.tar.gz", ".env.remote"):
                continue  # пропускаем
            else:
                sftp.put(str(item), remote_root + "/" + item.name)

        # Файлы шлюза из deploy/, которые монтирует compose.vps.yaml (каталог deploy/ целиком не загружается).
        for rel in ("deploy/vps/miniapp-nginx.conf", "deploy/asr-location.inc"):
            run_ssh(client, f"mkdir -p {remote_root}/{Path(rel).parent.as_posix()}")
            sftp.put(str(project_dir / rel), f"{remote_root}/{rel}")
            print(f"    {rel}", file=sys.stderr)

        # Загружаем images.tar
        print(f"  Uploading images.tar ({os.path.getsize(tar_path) / 1024 / 1024:.1f} MB)...", file=sys.stderr)
        sftp.put(tar_path, remote_root + "/images.tar")
        sftp.close()

        # 4. docker load
        print("\n=== docker load ===", file=sys.stderr)
        out, err = run_ssh(client, f"docker load < {remote_root}/images.tar 2>&1", timeout=300)
        for line in (out + err).split("\n")[-10:]:
            if line.strip():
                print(f"  {line[:120]}", file=sys.stderr)

        # Проверка
        out, _ = run_ssh(client, "docker images ecoprofi-*:local --format '{{.Repository}}:{{.Tag}}'")
        for line in out.strip().split("\n"):
            if line.strip():
                print(f"  {line.strip()}", file=sys.stderr)

        # Очистка
        run_ssh(client, f"rm -f {remote_root}/images.tar")

        # 5. Останавливаем системный nginx (порты 80/443 нужны Docker)
        print("\n=== Stop system nginx ===", file=sys.stderr)
        run_ssh(client, "systemctl stop nginx && systemctl disable nginx")

        # 6. docker compose up
        print("\n=== docker compose up -d ===", file=sys.stderr)
        out, err = run_ssh(
            client,
            f"cd {remote_root} && docker compose -f compose.yaml -f compose.vps.yaml "
            f"--profile miniapp --profile daemon up -d 2>&1",
            timeout=120,
        )
        for line in (out + err).split("\n")[-20:]:
            if line.strip():
                print(f"  {line[:120]}", file=sys.stderr)

        # 7. Ждём БД
        print("\n=== Waiting for DB ===", file=sys.stderr)
        for i in range(30):
            out, _ = run_ssh(
                client,
                f"cd {remote_root} && docker inspect --format='{{{{.State.Health.Status}}}}' ecoprofi-db-1 2>/dev/null"
            )
            status = out.strip()
            if status == "healthy":
                print(f"  DB: healthy ({i+1} attempts)", file=sys.stderr)
                break
            print(f"  DB: {status} ({i+1})", file=sys.stderr)
            time.sleep(5)
        else:
            print("  WARNING: DB timeout!", file=sys.stderr)

        # 8. Создаём роли БД (pgvector не запускает .sh из initdb.d)
        print("\n=== Create DB roles ===", file=sys.stderr)
        sftp = client.open_sftp()
        sftp.get(f"{remote_root}/.env", os.path.join(tmpdir, ".env"))
        sftp.close()

        env_path = os.path.join(tmpdir, ".env")
        passwords = {}
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    passwords[k] = v

        bot_pw = passwords.get("BOT_DB_PASSWORD", "")
        indexer_pw = passwords.get("INDEXER_DB_PASSWORD", "")

        if not bot_pw or not indexer_pw:
            print("  WARNING: BOT_DB_PASSWORD или INDEXER_DB_PASSWORD не найдены в .env", file=sys.stderr)
        else:
            sql = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_bot') THEN
        CREATE ROLE ecoprofi_bot LOGIN PASSWORD '{bot_pw}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecoprofi_indexer') THEN
        CREATE ROLE ecoprofi_indexer LOGIN PASSWORD '{indexer_pw}';
    END IF;
END
$$;
CREATE EXTENSION IF NOT EXISTS vector;
"""
            run_ssh(
                client,
                f"cd {remote_root} && docker exec -i ecoprofi-db-1 psql -U ecoprofi_owner -d ecoprofi",
                timeout=30,
                stdin_data=sql,
            )
            print("  Roles created", file=sys.stderr)

        # 9. Миграции
        print("\n=== Run migrations ===", file=sys.stderr)
        out, err = run_ssh(
            client,
            f"cd {remote_root} && docker compose -f compose.yaml -f compose.vps.yaml run --rm bot python -m ecoprofi.bot.migrate 2>&1",
            timeout=120,
        )
        for line in (out + err).split("\n")[-10:]:
            if line.strip():
                print(f"  {line[:120]}", file=sys.stderr)

        # 9a. Распознавание (006): пауза до пробы, если запрошена.
        if args.asr_pause:
            out, err = run_ssh(
                client,
                f"cd {remote_root} && docker compose -f compose.yaml -f compose.vps.yaml --profile indexer "
                f"run --rm indexer python -m ecoprofi.asr pause 2>&1",
                timeout=120,
            )
            print(f"  ASR: {(out + err).strip()[-200:]}", file=sys.stderr)

        # 10. Перезапускаем все сервисы
        print("\n=== Restart services ===", file=sys.stderr)
        run_ssh(
            client,
            f"cd {remote_root} && docker compose -f compose.yaml -f compose.vps.yaml --profile miniapp --profile daemon up -d --force-recreate -t 10",
            timeout=60,
        )
        time.sleep(8)

        # 10a. Проверка конфигурации шлюза (asr-location.inc подключён в server 443).
        out, err = run_ssh(client, f"cd {remote_root} && docker compose -f compose.yaml -f compose.vps.yaml "
                                   f"exec -T miniapp-gateway nginx -t 2>&1")
        print(f"  nginx -t: {(out + err).strip()[-300:]}", file=sys.stderr)
        if "successful" not in (out + err):
            print("  ОШИБКА: конфигурация шлюза не прошла nginx -t; проверьте deploy/asr-location.inc", file=sys.stderr)

        # 11. Certbot
        print(f"\n=== SSL: {args.domain} ===", file=sys.stderr)
        out, err = run_ssh(
            client,
            f"certbot --nginx -d {args.domain} --non-interactive --agree-tos -m {args.email} 2>&1",
            timeout=120,
        )
        for line in (out + err).split("\n")[-15:]:
            if line.strip():
                print(f"  {line[:120]}", file=sys.stderr)

        # 12. Финальная проверка
        print("\n=== docker compose ps ===", file=sys.stderr)
        out, _ = run_ssh(client, f"cd {remote_root} && docker compose ps")
        print(out, file=sys.stderr)

        print(f"\n=== HTTPS: {args.domain} ===", file=sys.stderr)
        out, _ = run_ssh(
            client,
            f"curl -sk https://{args.domain} -o /dev/null -w 'HTTP: %{{http_code}}'"
        )
        print(f"  {out.strip()}", file=sys.stderr)

        # Логи сервисов
        for svc in ["bot", "indexer-daemon", "vk-adapter", "asr-gateway"]:
            print(f"\n=== {svc} logs (last 5) ===", file=sys.stderr)
            out, _ = run_ssh(client, f"cd {remote_root} && docker logs --tail 5 {svc} 2>&1")
            for line in out.strip().split("\n")[-5:]:
                if line.strip():
                    print(f"  {line[:120]}", file=sys.stderr)

        client.close()
        print("\n=== DEPLOY COMPLETE ===", file=sys.stderr)


if __name__ == "__main__":
    args = parse_args()
    if not args.password:
        print("Ошибка: укажите --password или переменную VPS_PASSWORD", file=sys.stderr)
        sys.exit(1)
    deploy(args)
