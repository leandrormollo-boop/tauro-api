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


def test_busqueda_literal_periodo_y_export_solo_cuenta_propia(cuenta_db):
    from io import BytesIO
    from openpyxl import load_workbook
    from servicios.export_cuenta import generar_excel_cuenta

    with cuenta_db() as conn:
        with conn.cursor() as cur:
            for cliente in ("DUENO", "AJENO"):
                cur.execute("INSERT INTO clientes (cliente_id, nombre, email) VALUES (%s,%s,%s)", (cliente, cliente, cliente.lower() + "@example.invalid"))
            for cliente, fecha, detalle, monto in (
                ("DUENO", "2026-09-02", "Ropa_% & verano", 125),
                ("DUENO", "2026-09-02", "RopaABC", 250),
                ("DUENO", "2026-08-31", "Ropa_% & verano", 375),
                ("AJENO", "2026-09-02", "Ropa_% & verano", 900),
            ):
                cur.execute("""INSERT INTO envios
                    (cliente_id,fecha,monto_ars,estado,descripcion,ambito)
                    VALUES (%s,%s,%s,'ACTIVO',%s,'INTERNACIONAL')""",
                    (cliente, fecha, monto, detalle))
    filtros = {"q": "Ropa_%", "desde": "2026-09-01", "hasta": "2026-09-02"}
    resultado = cuenta_corriente.movimientos_cuenta_paginados("DUENO", **filtros)
    assert resultado["total_resultados"] == 1
    assert resultado["items"][0]["debe_ars"] == Decimal("125.00")
    assert resultado["items"][0]["fecha_iso"] == "2026-09-02"
    libro = load_workbook(BytesIO(generar_excel_cuenta("DUENO", **filtros)))
    filas = list(libro["Movimientos"].values)
    assert len(filas) == 2
    assert filas[1][7] == 125


def test_pagos_rechazados_y_en_revision_visibles_sin_impacto(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes (cliente_id,nombre,email) VALUES ('PAGOS_QA','Prueba','pagos-qa@example.invalid')")
            for estado in ("PENDIENTE", "RECHAZADO", "APROBADO"):
                cur.execute("""INSERT INTO pagos
                    (cliente_id,fecha,monto_ars,metodo,referencia,estado)
                    VALUES ('PAGOS_QA','2026-09-15',100,'Transferencia',%s,%s)""",
                    (estado, estado))
    resultado = cuenta_corriente.movimientos_cuenta_paginados("PAGOS_QA", tipo="pagos")
    assert {m["tipo"] for m in resultado["items"]} == {"PAGO", "PAGO_PENDIENTE", "PAGO_RECHAZADO"}
    no_acreditados = [m for m in resultado["items"] if m["tipo"] != "PAGO"]
    assert all(m["haber_ars"] == m["debe_ars"] == 0 for m in no_acreditados)
    revision = cuenta_corriente.movimientos_cuenta_paginados("PAGOS_QA", tipo="revision")
    assert len(revision["items"]) == 1
    assert revision["items"][0]["estado"] == "PENDIENTE"
