"""Imputación por documento y arrastre sobre PostgreSQL aislado."""

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
from servicios import facturacion_clientes


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def cuenta_db(monkeypatch):
    schema = f"test_pagos_documentales_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
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

        monkeypatch.setattr(cuenta_corriente, "get_conn", conexion)
        monkeypatch.setattr(facturacion_clientes, "get_conn", conexion)
        yield conexion
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def test_pago_sobre_diez_envios_arrastra_a_factura_y_deja_credito(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clientes (cliente_id,email) "
                "VALUES ('WAIMAO','qa-waimao@example.invalid')"
            )
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id,fecha,monto_ars,estado,descripcion,tracking,ambito
                )
                SELECT 'WAIMAO', DATE '2026-09-01' + (n-1), 299000,
                       'ACTIVO', 'Carga ' || n, 'QA-' || n, 'INTERNACIONAL'
                  FROM GENERATE_SERIES(1,10) n
                RETURNING id
                """
            )
            envios = [int(fila["id"]) for fila in cur.fetchall()]

    pago_id = cuenta_corriente.registrar_pago(
        cliente_id="WAIMAO",
        fecha="2026-09-03",
        monto_ars="3000000",
        metodo="transferencia",
        estado="PENDIENTE",
        comprobante=b"%PDF-1.4\n%%EOF\n",
        destinos=[f"E:{envio_id}" for envio_id in envios],
        idempotency_key="aceptacion_pago_documental_1234567890",
    )
    assert cuenta_corriente.resolver_pago(
        pago_id, aprobar=True, aplicaciones=None,
    )
    resumen = cuenta_corriente.resumen_cuenta_por_ambito("WAIMAO")
    assert resumen["credito_sin_imputar_ars"] == Decimal("10000.00")

    factura = facturacion_clientes.crear_factura_cliente(
        cliente_id="WAIMAO",
        tipo="FC",
        punto_venta=4,
        numero=600,
        cae="",
        fecha_emision="2026-09-03",
        periodo_desde="2026-09-01",
        periodo_hasta="2026-09-10",
        subtotal="2990000",
        iva="0",
        pdf=b"%PDF-1.4\n%%EOF\n",
        pdf_nombre="waimao.pdf",
        seleccion=[f"E:{envio_id}" for envio_id in envios],
        created_by="test",
    )
    [documento] = facturacion_clientes.listar_facturas_cliente("WAIMAO")
    assert documento["id"] == factura["id"]
    assert documento["pagado"] == Decimal("2990000.00")
    assert documento["saldo"] == Decimal("0.00")


def test_trigger_deriva_ambito_y_bloquea_sobrepago(cuenta_db):
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clientes (cliente_id,email) "
                "VALUES ('CLIENTE','qa-cliente@example.invalid')"
            )
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id,fecha,monto_ars,estado,tracking,ambito
                ) VALUES ('CLIENTE',CURRENT_DATE,100,'ACTIVO','QA','NACIONAL')
                RETURNING id
                """
            )
            envio_id = int(cur.fetchone()["id"])

    cuenta_corriente.registrar_pago(
        cliente_id="CLIENTE", fecha="2026-09-03", monto_ars="80",
        metodo="transferencia", destinos=[f"E:{envio_id}"],
    )
    segundo = cuenta_corriente.registrar_pago(
        cliente_id="CLIENTE", fecha="2026-09-03", monto_ars="30",
        metodo="transferencia", destinos=[f"E:{envio_id}"],
    )
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT monto_ars,ambito FROM pagos_aplicaciones "
                "WHERE pago_id=%s", (segundo,),
            )
            aplicacion = cur.fetchone()
            assert aplicacion["monto_ars"] == Decimal("20.00")
            assert aplicacion["ambito"] == "NACIONAL"

    with pytest.raises(psycopg2.Error, match="saldo del documento"):
        with cuenta_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pagos (cliente_id,fecha,monto_ars,metodo,estado) "
                    "VALUES ('CLIENTE',CURRENT_DATE,5,'test','APROBADO') "
                    "RETURNING id"
                )
                pago_id = int(cur.fetchone()["id"])
                cur.execute(
                    "INSERT INTO pagos_aplicaciones "
                    "(pago_id,ambito,monto_ars,estado,envio_id) "
                    "VALUES (%s,'INTERNACIONAL',5,'APLICADA',%s)",
                    (pago_id, envio_id),
                )
