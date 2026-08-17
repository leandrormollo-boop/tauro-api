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


_READINESS_CONTABLE_SQL = """
WITH columnas_dinero AS (
    SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = CURRENT_SCHEMA()
      AND (table_name, column_name) IN (
          ('pagos', 'monto_ars'),
          ('envios', 'monto_ars')
      )
), fk_pagos AS (
    SELECT c.*
    FROM pg_constraint c
    WHERE c.contype = 'f'
      AND c.conrelid = 'pagos'::regclass
      AND c.confrelid = 'clientes'::regclass
      AND c.conkey = ARRAY[(
          SELECT a.attnum FROM pg_attribute a
          WHERE a.attrelid = 'pagos'::regclass
            AND a.attname = 'cliente_id' AND NOT a.attisdropped
      )]::SMALLINT[]
      AND c.confkey = ARRAY[(
          SELECT a.attnum FROM pg_attribute a
          WHERE a.attrelid = 'clientes'::regclass
            AND a.attname = 'cliente_id' AND NOT a.attisdropped
      )]::SMALLINT[]
), fk_envios AS (
    SELECT c.*
    FROM pg_constraint c
    WHERE c.contype = 'f'
      AND c.conrelid = 'envios'::regclass
      AND c.confrelid = 'clientes'::regclass
      AND c.conkey = ARRAY[(
          SELECT a.attnum FROM pg_attribute a
          WHERE a.attrelid = 'envios'::regclass
            AND a.attname = 'cliente_id' AND NOT a.attisdropped
      )]::SMALLINT[]
      AND c.confkey = ARRAY[(
          SELECT a.attnum FROM pg_attribute a
          WHERE a.attrelid = 'clientes'::regclass
            AND a.attname = 'cliente_id' AND NOT a.attisdropped
      )]::SMALLINT[]
)
SELECT
    EXISTS (
        SELECT 1 FROM columnas_dinero
        WHERE table_name = 'pagos' AND column_name = 'monto_ars'
          AND data_type = 'numeric'
          AND numeric_precision = 14 AND numeric_scale = 2
    ) AS pagos_monto_numeric_14_2,
    EXISTS (
        SELECT 1 FROM columnas_dinero
        WHERE table_name = 'envios' AND column_name = 'monto_ars'
          AND data_type = 'numeric'
          AND numeric_precision = 14 AND numeric_scale = 2
    ) AS envios_monto_numeric_14_2,
    EXISTS (
        SELECT 1 FROM pg_class c
        WHERE c.oid = TO_REGCLASS('pagos_aplicaciones')
          AND c.relkind IN ('r', 'p')
    ) AS pagos_aplicaciones_existe,
    EXISTS (
        SELECT 1 FROM pg_trigger t
        WHERE t.tgrelid = TO_REGCLASS('pagos_aplicaciones')
          AND t.tgname = 'trg_validar_pago_aplicacion'
          AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A')
    ) AS trigger_pago_aplicacion_habilitado,
    EXISTS (
        SELECT 1 FROM pg_trigger t
        WHERE t.tgrelid = TO_REGCLASS('pagos')
          AND t.tgname = 'trg_validar_pago_con_aplicaciones'
          AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A')
    ) AS trigger_pago_padre_habilitado,
    EXISTS (
        SELECT 1
        FROM pg_class indice
        JOIN pg_index i ON i.indexrelid = indice.oid
        WHERE indice.oid = TO_REGCLASS('uq_envios_fc_normalizada')
          AND i.indrelid = TO_REGCLASS('envios')
          AND i.indisunique AND i.indisvalid AND i.indisready
          AND i.indnkeyatts = 1
          AND i.indexprs IS NOT NULL AND i.indpred IS NOT NULL
          AND LOWER(PG_GET_EXPR(i.indexprs, i.indrelid)) LIKE '%regexp_replace%'
          AND LOWER(PG_GET_EXPR(i.indexprs, i.indrelid)) LIKE '%upper%'
          AND LOWER(PG_GET_EXPR(i.indexprs, i.indrelid)) LIKE '%btrim%'
          AND LOWER(PG_GET_EXPR(i.indexprs, i.indrelid)) LIKE '%nro_fc%'
          AND LOWER(PG_GET_EXPR(i.indexprs, i.indrelid)) NOT LIKE '%cliente_id%'
          AND PG_GET_EXPR(i.indexprs, i.indrelid) LIKE '%[^A-Z0-9]%'
          AND LOWER(PG_GET_EXPR(i.indpred, i.indrelid)) LIKE '%estado%'
          AND LOWER(PG_GET_EXPR(i.indpred, i.indrelid)) LIKE '%nc%'
          AND LOWER(PG_GET_EXPR(i.indpred, i.indrelid)) LIKE '%nro_fc%'
          AND LOWER(PG_GET_EXPR(i.indpred, i.indrelid)) LIKE '%regexp_replace%'
          AND PG_GET_EXPR(i.indpred, i.indrelid) LIKE '%<>%'
    ) AS indice_fc_global_correcto,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('envios')
          AND c.contype = 'c'
          AND c.conname = 'ck_envios_nro_fc_valida'
          AND c.convalidated
          AND LOWER(PG_GET_CONSTRAINTDEF(c.oid)) LIKE '%nro_fc%'
          AND LOWER(PG_GET_CONSTRAINTDEF(c.oid)) LIKE '%regexp_replace%'
    ) AS check_fc_valida,
    (
        SELECT COUNT(*) = 1 AND COALESCE(BOOL_AND(
            conname = 'pagos_cliente_id_fkey'
            AND confdeltype = 'r' AND convalidated
        ), FALSE)
        FROM fk_pagos
    ) AS fk_pagos_cliente_restrict,
    (
        SELECT COUNT(*) = 1 AND COALESCE(BOOL_AND(
            conname = 'envios_cliente_id_fkey'
            AND confdeltype = 'r' AND convalidated
        ), FALSE)
        FROM fk_envios
    ) AS fk_envios_cliente_restrict
"""

_READINESS_CONTABLE_CAMPOS = (
    "pagos_monto_numeric_14_2",
    "envios_monto_numeric_14_2",
    "pagos_aplicaciones_existe",
    "trigger_pago_aplicacion_habilitado",
    "trigger_pago_padre_habilitado",
    "indice_fc_global_correcto",
    "check_fc_valida",
    "fk_pagos_cliente_restrict",
    "fk_envios_cliente_restrict",
)


def _verificar_readiness_contable(cur) -> dict[str, bool]:
    """Audita el schema financiero con una consulta; nunca migra ni repara."""
    cur.execute(_READINESS_CONTABLE_SQL)
    fila = cur.fetchone()
    if not fila:
        raise RuntimeError("Readiness contable sin resultado.")
    resultado = dict(fila)
    fallas = [
        campo for campo in _READINESS_CONTABLE_CAMPOS
        if not bool(resultado.get(campo, False))
    ]
    if fallas:
        raise RuntimeError(
            "Schema contable no listo: " + ", ".join(fallas)
        )
    return {campo: True for campo in _READINESS_CONTABLE_CAMPOS}


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
        if os.getenv("DATABASE_URL"):
            raise RuntimeError("sql/schema.sql no existe; init_db abortado.")
        print("[db] ADVERTENCIA: sql/schema.sql no encontrado, saltando init_db.")
        return
    sql = schema_path.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            _verificar_readiness_contable(cur)
    print("[db] Schema inicializado OK.")
