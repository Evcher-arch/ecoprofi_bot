"""Подключение к PostgreSQL с pgvector."""
import os

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

EMBEDDING_DIM = 256


def connect(dsn: str | None = None, *, env: str = "DATABASE_URL", vector: bool = True) -> psycopg.Connection:
    """Открыть соединение. Строки возвращаются словарями."""
    dsn = dsn or os.environ.get(env)
    if not dsn:
        raise RuntimeError(f"Не задана переменная окружения {env}")
    conn = psycopg.connect(dsn, row_factory=dict_row)
    if vector:
        register_vector(conn)
    return conn
