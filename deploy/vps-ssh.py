#!/usr/bin/env python3
"""
Доступ к VPS без интерактивного ввода пароля (specs/005-vps-migration, T028).

Пароль берётся только из переменной окружения VPS_PASSWORD и в репозиторий
не записывается. Если пароль не задан, используется SSH-ключ из ~/.ssh.

Требования: pip install paramiko

Переменные окружения: VPS_HOST (по умолчанию 79.174.78.44), VPS_USER (root),
VPS_PASSWORD (необязательно при входе по ключу).

Команды:
  python deploy/vps-ssh.py install-key [~/.ssh/id_ed25519.pub]
      добавить открытый ключ в ~/.ssh/authorized_keys на VPS (повторно не дублирует);
      после этого работают обычные ssh/scp без пароля
  python deploy/vps-ssh.py put <локальный файл> <путь на VPS>
  python deploy/vps-ssh.py run "<команда>"
      код возврата команды на VPS возвращается как код возврата скрипта
"""
import os
import sys
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Ошибка: установите paramiko (pip install paramiko)", file=sys.stderr)
    sys.exit(1)


def connect():
    host = os.environ.get("VPS_HOST", "79.174.78.44")
    user = os.environ.get("VPS_USER", "root")
    password = os.environ.get("VPS_PASSWORD") or None
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if password:
        # Парольный вход: подключаемся через Transport напрямую,
        # чтобы paramiko не пытался читать ~/.ssh/* (могут быть зашифрованы).
        transport = paramiko.Transport((host, 22))
        transport.start_client()
        transport.auth_password(user, password)
        client._transport = transport
    else:
        client.connect(host, username=user, password=password, timeout=15,
                       look_for_keys=True, allow_agent=True)
    return client


def run(client, cmd):
    _, stdout, stderr = client.exec_command(cmd)
    for line in stdout:
        print(line, end="")
    err = stderr.read().decode()
    if err:
        print(err, end="", file=sys.stderr)
    return stdout.channel.recv_exit_status()


def install_key(client, pub_path):
    key = Path(pub_path).expanduser().read_text(encoding="utf-8").strip()
    if not key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-")):
        print(f"Ошибка: {pub_path} не похож на открытый ключ", file=sys.stderr)
        return 2
    # Ключ передаётся через stdin, а не в тексте команды.
    cmd = ('umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; read -r k; '
           'grep -qxF "$k" ~/.ssh/authorized_keys || echo "$k" >> ~/.ssh/authorized_keys; '
           'echo "ключ установлен"')
    stdin, stdout, stderr = client.exec_command(cmd)
    stdin.write(key + "\n")
    stdin.channel.shutdown_write()
    print(stdout.read().decode(), end="")
    print(stderr.read().decode(), end="", file=sys.stderr)
    return stdout.channel.recv_exit_status()


def main(argv):
    if not argv or argv[0] not in ("install-key", "put", "run"):
        print(__doc__, file=sys.stderr)
        return 2
    client = connect()
    try:
        if argv[0] == "install-key":
            return install_key(client, argv[1] if len(argv) > 1 else "~/.ssh/id_ed25519.pub")
        if argv[0] == "put":
            if len(argv) != 3:
                print("Использование: put <локальный файл> <путь на VPS>", file=sys.stderr)
                return 2
            sftp = client.open_sftp()
            sftp.put(argv[1], argv[2])
            sftp.close()
            print(f"передано: {argv[1]} -> {argv[2]}")
            return 0
        if len(argv) != 2:
            print('Использование: run "<команда>"', file=sys.stderr)
            return 2
        return run(client, argv[1])
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
