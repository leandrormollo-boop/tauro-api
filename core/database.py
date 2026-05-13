# ============================================================
# core/database.py — Pool de conexiones PostgreSQL
# ============================================================
# Usa psycopg2 (sync). Un pool de hasta 10 conexiones.
# DATABASE_URL se lee del env (Railway lo inyecta automáticamente).
#
# Uso:
#   from core.database import get_conn
#   with get_conn() as conn:
#       with conn.cursor() as cur:
#           cur.execute("SELECT ...")
#           rows = cur.fetchall()
# ============================================================

import os
import psycopg2
import psycopg2.extras
from psycopg2 import pool

_pool: pool.ThreadedConnectionPool | None = None


def _init_pool() -> pool.ThreadedConnectionPool:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL no está configurada en el entorno.")
    # Railway usa postgres:// pero psycopg2 necesita postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=10,
        dsn=url,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = _init_pool()
    return _pool


class _ConnContext:
    """Context manager que toma/devuelve una conexión del pool y hace commit/rollback."""

    def __init__(self):
        self._conn = None

    def __enter__(self):
        self._conn = get_pool().getconn()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        get_pool().putconn(self._conn)
        return False  # no suprimir la excepción


def get_conn() -> _ConnContext:
    """Devuelve un context manager con una conexión del pool."""
    return _ConnContext()


def init_db():
    """
    Crea las tablas si no existen. Llamar al startup de la app.
    Lee el schema desde sql/schema.sql relativo a este archivo.
    """
    import pathlib
    schema_path = pathlib.Path(__file__).parent.parent / "sql" / "schema.sql"
    if not schema_path.exists():
        print("[db] ADVERTENCIA: sql/schema.sql no encontrado, saltando init_db.")
        return
    sql = schema_path.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print("[db] Schema inicializado OK.")
