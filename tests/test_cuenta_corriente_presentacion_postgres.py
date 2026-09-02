"""Contrato de presentación de movimientos sobre PostgreSQL real y aislado."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from servicios import cuenta_corriente


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def cuenta_db(monkeypatch):
    schema = f"test_cuenta_presentacion_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def get_conn_aislada():
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        monkeypatch.setattr(cuenta_corriente, "get_conn", get_conn_aislada)
        yield get_conn_aislada
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def test_envio_se_presenta_ordenado_sin_cambiar_el_movimiento(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clientes (cliente_id, email, nombre)
                VALUES ('WAIMAO', 'qa-waimao@example.invalid', 'WAIMAO')
                """
            )
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, remitente_nombre,
                    destino_pais, dest_nombre, dest_direccion, dest_ciudad,
                    dest_zip, tracking, ambito
                ) VALUES (
                    'WAIMAO', 'Ropa', 'SLINGER', 'UY', 'MARSANTEX',
                    'Calle QA 123', 'Montevideo', '11000',
                    '6781215324', 'INTERNACIONAL'
                )
                RETURNING id
                """
            )
            solicitud_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, descripcion,
                    tracking, solicitud_id, ambito
                ) VALUES (
                    'WAIMAO', DATE '2026-09-02', 1714134, 'ACTIVO',
                    'Flete internacional', '6781215324', %s, 'INTERNACIONAL'
                )
                """,
                (solicitud_id,),
            )

    movimientos = cuenta_corriente.movimientos_cuenta_paginados(
        "WAIMAO", "internacional", "cargos", 1, 10,
    )

    assert movimientos["total_resultados"] == 1
    assert len(movimientos["items"]) == 1
    movimiento = movimientos["items"][0]
    assert movimiento["concepto"] == "Flete"
    assert movimiento["numero_guia"] == "6781215324"
    assert movimiento["destinatario"] == "MARSANTEX"
    assert movimiento["fecha"] == "02/09/2026"
    assert movimiento["remitente"] == "SLINGER"
    assert movimiento["valor_envio_ars"] == Decimal("1714134.00")
    assert movimiento["debe_ars"] == Decimal("1714134.00")
    assert movimiento["haber_ars"] == Decimal("0.00")
