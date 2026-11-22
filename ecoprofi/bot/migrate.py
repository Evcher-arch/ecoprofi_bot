"""Применение миграций: python -m ecoprofi.bot.migrate

Каждый файл migrations/*.sql выполняется один раз в собственной транзакции.
Повторный запуск ничего не меняет и сохраняет данные.
"""
import os
import sys
from pathlib import Path

from ecoprofi.db import connect

MIGRATIONS_DIR = Path(os.environ.get("MIGRATIONS_DIR", Path(__file__).resolve().parents[2] / "migrations"))


def migrate(conn, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Применить недостающие миграции, вернуть их имена."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    applied = []
    for path in sorted(directory.glob("*.sql")):
        with conn.transaction():
            # Блокировка сериализует одновременные запуски.
            conn.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
            done = conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,)).fetchone()
            if done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def main() -> int:
    with connect(env="MIGRATION_DATABASE_URL", vector=False) as conn:
        applied = migrate(conn)
    print("Применены миграции: " + ", ".join(applied) if applied else "Схема актуальна, изменений нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
