"""Redaccion Shopify sobre PostgreSQL real y con aislamiento por tienda.

Se ejecuta en release con ``TAURO_TEST_DATABASE_URL``. El schema aleatorio y
los datos sinteticos evitan tocar datos locales o productivos.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from servicios import integraciones_tienda


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)

DOMINIO_A = "redact-a.myshopify.com"
DOMINIO_B = "redact-b.myshopify.com"
PEDIDO_COMPARTIDO = "1001"


@pytest.fixture
def gdpr_db(monkeypatch):
    schema = f"test_gdpr_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")

    admin = psycopg2.connect(DATABASE_URL)
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

        monkeypatch.setattr(integraciones_tienda, "get_conn", get_conn_aislada)
        monkeypatch.setattr(integraciones_tienda, "_tablas_listas", False)
        integraciones_tienda._ensure_tablas()
        yield get_conn_aislada
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _sembrar_tenants(get_conn_aislada) -> dict[str, int]:
    solicitudes: dict[str, int] = {}
    with get_conn_aislada() as conn:
        with conn.cursor() as cur:
            for cliente, dominio in (("TENANT_A", DOMINIO_A), ("TENANT_B", DOMINIO_B)):
                cur.execute(
                    "INSERT INTO clientes (cliente_id, email, nombre) VALUES (%s, %s, %s)",
                    (cliente, f"{cliente.lower()}@example.invalid", cliente),
                )
                cur.execute(
                    """
                    INSERT INTO tiendas_conectadas
                        (cliente_id, plataforma, dominio, secreto, activa)
                    VALUES (%s, 'shopify', %s, 'oauth:shopify-app', TRUE)
                    RETURNING id
                    """,
                    (cliente, dominio),
                )
                tienda_id = int(cur.fetchone()["id"])
                sufijo = cliente[-1]
                cur.execute(
                    """
                    INSERT INTO solicitudes_guia (
                        cliente_id, producto_alias, destino_pais,
                        remitente_nombre, remitente_email, remitente_direccion,
                        remitente_ciudad, remitente_zip,
                        dest_nombre, dest_email, dest_telefono, dest_direccion,
                        dest_ciudad, dest_zip, observaciones, tracking, label_pdf,
                        origen_plataforma, origen_dominio,
                        origen_pedido_externo_id
                    ) VALUES (
                        %s, %s, 'US', %s, %s, %s, 'Buenos Aires', '1000',
                        %s, %s, %s, %s, 'Miami', '33101', %s, %s, %s,
                        'shopify', %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        cliente,
                        f"producto-{sufijo}",
                        f"Remitente {sufijo}",
                        f"remitente-{sufijo.lower()}@example.invalid",
                        f"Origen privado {sufijo}",
                        f"Comprador {sufijo}",
                        f"comprador-{sufijo.lower()}@example.invalid",
                        f"+1-555-{sufijo}",
                        f"Destino privado {sufijo}",
                        f"nota privada {sufijo}",
                        f"TRACK-SOL-{sufijo}",
                        psycopg2.Binary(f"label-{sufijo}".encode()),
                        dominio,
                        PEDIDO_COMPARTIDO,
                    ),
                )
                solicitud_id = int(cur.fetchone()["id"])
                solicitudes[cliente] = solicitud_id
                cur.execute(
                    """
                    INSERT INTO pedidos_tienda (
                        cliente_id, tienda_id, plataforma, pedido_externo_id,
                        destinatario, items, solicitud_id
                    ) VALUES (%s, %s, 'shopify', %s, %s, '[]'::jsonb, %s)
                    """,
                    (
                        cliente,
                        tienda_id,
                        PEDIDO_COMPARTIDO,
                        psycopg2.extras.Json({
                            "nombre": f"Comprador {sufijo}",
                            "email": f"comprador-{sufijo.lower()}@example.invalid",
                        }),
                        solicitud_id,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO direcciones (
                        cliente_id, nombre, email, direccion, ciudad, cp, pais,
                        origen_plataforma, origen_dominio,
                        origen_pedido_externo_id
                    ) VALUES (%s, %s, %s, %s, 'Miami', '33101', 'US',
                              'shopify', %s, %s)
                    """,
                    (
                        cliente,
                        f"Comprador {sufijo}",
                        f"comprador-{sufijo.lower()}@example.invalid",
                        f"Destino privado {sufijo}",
                        dominio,
                        PEDIDO_COMPARTIDO,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO envios (
                        cliente_id, fecha, monto_ars, descripcion, tracking,
                        solicitud_id, factura_pdf
                    ) VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente,
                        Decimal("1234.56"),
                        f"descripcion privada {sufijo}",
                        f"TRACK-ENV-{sufijo}",
                        solicitud_id,
                        psycopg2.Binary(f"factura-{sufijo}".encode()),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO recolecciones (
                        cliente_id, fecha, direccion, instrucciones,
                        confirmation_code, ubicacion, error_operativo,
                        solicitud_id
                    ) VALUES (%s, CURRENT_DATE, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cliente,
                        f"Origen privado {sufijo}",
                        f"llamar a contacto {sufijo}",
                        f"PICK-{sufijo}",
                        f"ubicacion privada {sufijo}",
                        f"error privado {sufijo}",
                        solicitud_id,
                    ),
                )
            cur.execute(
                """
                INSERT INTO shopify_gdpr_solicitudes
                    (request_id, dominio, shop_id, orders_requested, estado)
                VALUES ('req-pendiente-a', %s, 'shop-a', '["1001"]'::jsonb,
                        'PENDIENTE')
                """,
                (DOMINIO_A,),
            )
    return solicitudes


def _operacion(get_conn_aislada, solicitud_id: int) -> dict:
    with get_conn_aislada() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.tracking AS solicitud_tracking, s.dest_email,
                       s.dest_direccion, s.remitente_email,
                       s.remitente_direccion, s.label_pdf,
                       e.id AS envio_id, e.monto_ars, e.tracking AS envio_tracking,
                       e.descripcion, e.factura_pdf,
                       r.id AS recoleccion_id, r.direccion AS retiro_direccion,
                       r.instrucciones, r.ubicacion, r.error_operativo,
                       r.confirmation_code
                  FROM solicitudes_guia s
                  JOIN envios e ON e.solicitud_id = s.id
                  JOIN recolecciones r ON r.solicitud_id = s.id
                 WHERE s.id = %s
                """,
                (solicitud_id,),
            )
            return dict(cur.fetchone())


def test_customers_redact_limpia_derivados_sin_tocar_otro_tenant(gdpr_db):
    get_conn_aislada = gdpr_db
    solicitudes = _sembrar_tenants(get_conn_aislada)

    integraciones_tienda.anonimizar_pedidos(
        DOMINIO_A,
        [PEDIDO_COMPARTIDO],
    )

    tenant_a = _operacion(get_conn_aislada, solicitudes["TENANT_A"])
    tenant_b = _operacion(get_conn_aislada, solicitudes["TENANT_B"])

    assert tenant_a["solicitud_tracking"] is None
    assert tenant_a["dest_email"] is None
    assert tenant_a["label_pdf"] is None
    assert tenant_a["envio_tracking"] is None
    assert tenant_a["descripcion"] is None
    assert tenant_a["retiro_direccion"] is None
    assert tenant_a["instrucciones"] is None
    assert tenant_a["ubicacion"] is None
    assert tenant_a["error_operativo"] is None
    assert tenant_a["envio_id"] and tenant_a["recoleccion_id"]
    assert tenant_a["monto_ars"] == Decimal("1234.56")
    assert bytes(tenant_a["factura_pdf"]) == b"factura-A"
    assert tenant_a["confirmation_code"] == "PICK-A"

    assert tenant_b["solicitud_tracking"] == "TRACK-SOL-B"
    assert tenant_b["dest_email"] == "comprador-b@example.invalid"
    assert tenant_b["envio_tracking"] == "TRACK-ENV-B"
    assert tenant_b["descripcion"] == "descripcion privada B"
    assert tenant_b["retiro_direccion"] == "Origen privado B"
    assert tenant_b["instrucciones"] == "llamar a contacto B"
    assert tenant_b["ubicacion"] == "ubicacion privada B"
    assert tenant_b["error_operativo"] == "error privado B"

    with get_conn_aislada() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cliente_id FROM direcciones ORDER BY cliente_id",
            )
            assert [fila["cliente_id"] for fila in cur.fetchall()] == ["TENANT_B"]


def test_shop_redact_limpia_derivados_y_preserva_evidencia_financiera(gdpr_db):
    get_conn_aislada = gdpr_db
    solicitudes = _sembrar_tenants(get_conn_aislada)

    integraciones_tienda.borrar_datos_tienda(DOMINIO_A)

    tenant_a = _operacion(get_conn_aislada, solicitudes["TENANT_A"])
    tenant_b = _operacion(get_conn_aislada, solicitudes["TENANT_B"])

    assert tenant_a["solicitud_tracking"] is None
    assert tenant_a["dest_email"] is None
    assert tenant_a["remitente_email"] is None
    assert "dato eliminado" in tenant_a["remitente_direccion"]
    assert tenant_a["envio_tracking"] is None
    assert tenant_a["descripcion"] is None
    assert tenant_a["retiro_direccion"] is None
    assert tenant_a["instrucciones"] is None
    assert tenant_a["ubicacion"] is None
    assert tenant_a["error_operativo"] is None
    assert tenant_a["envio_id"] and tenant_a["recoleccion_id"]
    assert tenant_a["monto_ars"] == Decimal("1234.56")
    assert bytes(tenant_a["factura_pdf"]) == b"factura-A"
    assert tenant_a["confirmation_code"] == "PICK-A"

    assert tenant_b["solicitud_tracking"] == "TRACK-SOL-B"
    assert tenant_b["dest_email"] == "comprador-b@example.invalid"
    assert tenant_b["remitente_email"] == "remitente-b@example.invalid"
    assert tenant_b["envio_tracking"] == "TRACK-ENV-B"
    assert tenant_b["retiro_direccion"] == "Origen privado B"

    with get_conn_aislada() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT dominio FROM tiendas_conectadas ORDER BY dominio")
            assert [fila["dominio"] for fila in cur.fetchall()] == [DOMINIO_B]
            cur.execute("SELECT cliente_id FROM pedidos_tienda ORDER BY cliente_id")
            assert [fila["cliente_id"] for fila in cur.fetchall()] == ["TENANT_B"]
            cur.execute("SELECT cliente_id FROM direcciones ORDER BY cliente_id")
            assert [fila["cliente_id"] for fila in cur.fetchall()] == ["TENANT_B"]
            cur.execute(
                "SELECT estado FROM shopify_gdpr_solicitudes WHERE request_id=%s",
                ("req-pendiente-a",),
            )
            assert cur.fetchone()["estado"] == "PENDIENTE"
