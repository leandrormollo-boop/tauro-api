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

from __future__ import annotations

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
        # No colgarse minutos si la DB no responde al conectar
        connect_timeout=10,
        # TCP keepalive: detecta conexiones muertas (reinicio de Postgres,
        # corte de red) en segundos en vez de esperar al próximo error
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )


def get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = _init_pool()
    return _pool


class _ConnContext:
    """Context manager que toma/devuelve una conexión del pool y hace
    commit/rollback.

    AUTO-REPARACIÓN: si Postgres se reinició, las conexiones del pool quedan
    muertas. Antes esto dejaba la app rota hasta un restart manual. Ahora cada
    checkout hace un ping (SELECT 1); las conexiones muertas se descartan y se
    abre una fresca. Y si commit/rollback fallan, la conexión rota se saca del
    pool en vez de filtrarse (10 filtradas = pool agotado = app muerta)."""

    def __init__(self):
        self._conn = None

    def __enter__(self):
        p = get_pool()
        ultimo_error = None
        for _ in range(3):
            conn = p.getconn()
            try:
                if not conn.closed:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                    conn.rollback()  # limpiar la transacción del ping
                    self._conn = conn
                    return conn
            except psycopg2.Error as e:
                ultimo_error = e
            # Conexión muerta: descartarla del pool y probar con otra
            try:
                p.putconn(conn, close=True)
            except Exception:
                pass
        raise RuntimeError(f"Sin conexión viva a la base de datos: {ultimo_error}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        except psycopg2.Error:
            # La conexión murió a mitad de camino: descartarla, no filtrarla.
            try:
                get_pool().putconn(self._conn, close=True)
            except Exception:
                pass
            self._conn = None
            if exc_type is None:
                raise  # el commit falló: el caller TIENE que enterarse
            return False  # ya había una excepción original; que propague esa
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
