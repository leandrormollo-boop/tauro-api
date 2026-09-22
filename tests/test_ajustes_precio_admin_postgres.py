"""Ajustes comerciales sobre PostgreSQL real y un schema descartable."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from servicios import ajustes_precio_admin, cuenta_corriente, solicitudes_guia


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def db(monkeypatch):
    schema = f"test_precio_admin_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(schema_sql)

        @contextmanager
        def get_conn():
            conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
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

        monkeypatch.setattr(ajustes_precio_admin, "get_conn", get_conn)
        monkeypatch.setattr(cuenta_corriente, "get_conn", get_conn)
        monkeypatch.setattr(solicitudes_guia, "get_conn", get_conn)
        yield get_conn
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _crear_envio(db) -> tuple[int, int]:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES ('WAIMAO', 'waimao@example.invalid', 'WAIMAO')
            """
        )
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, destino_pais, dest_nombre,
                dest_direccion, dest_ciudad, dest_zip, courier, tracking,
                coti_id, precio_tauro_ars, estado, test, visible_cliente
            ) VALUES (
                'WAIMAO', 'Producto', 'US', 'Destino', 'Calle 1',
                'Miami', '33101', 'DHL', 'TRACK-PRECIO', 'COTI-PRECIO',
                10000, 'GUIA_LISTA', FALSE, TRUE
            ) RETURNING id
            """
        )
        solicitud_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO envios (
                cliente_id, fecha, monto_ars, estado, descripcion,
                tracking, solicitud_id, ambito
            ) VALUES (
                'WAIMAO', CURRENT_DATE, 10000, 'ACTIVO', 'Flete',
                'TRACK-PRECIO', %s, 'INTERNACIONAL'
            ) RETURNING id
            """,
            (solicitud_id,),
        )
        return solicitud_id, int(cur.fetchone()["id"])


def test_descuento_y_nuevo_precio_preservan_original_y_son_idempotentes(db):
    solicitud_id, envio_id = _crear_envio(db)
    clave_1 = "descuento_admin_000000000000000001"
    primero = ajustes_precio_admin.aplicar_nuevo_precio(
        cliente_id="WAIMAO",
        envio_id=envio_id,
        nuevo_precio_ars="8000",
        motivo="Descuento comercial acordado",
        actor="test-admin",
        idempotency_key=clave_1,
    )
    assert primero["tipo"] == "CREDITO"
    assert primero["monto_ajuste_ars"] == Decimal("2000.00")

    repetido = ajustes_precio_admin.aplicar_nuevo_precio(
        cliente_id="WAIMAO",
        envio_id=envio_id,
        nuevo_precio_ars="8000",
        motivo="Descuento comercial acordado",
        actor="test-admin",
        idempotency_key=clave_1,
    )
    assert repetido["duplicado"] is True

    segundo = ajustes_precio_admin.aplicar_nuevo_precio(
        cliente_id="WAIMAO",
        envio_id=envio_id,
        nuevo_precio_ars="8500",
        motivo="Corrección final aprobada",
        actor="test-admin",
        idempotency_key="precio_admin_00000000000000000002",
    )
    assert segundo["tipo"] == "DEBITO"
    assert segundo["precio_anterior_ars"] == Decimal("8000.00")
    assert segundo["monto_ajuste_ars"] == Decimal("500.00")

    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT monto_ars FROM envios WHERE id=%s", (envio_id,))
        assert cur.fetchone()["monto_ars"] == Decimal("10000.00")
        cur.execute(
            """
            SELECT COUNT(*) AS cantidad, SUM(monto_ars) AS ajuste,
                   BOOL_AND(origen='AJUSTE_COMERCIAL_ADMIN') AS origen_ok
            FROM ajustes_cliente WHERE solicitud_id=%s
            """,
            (solicitud_id,),
        )
        fila = cur.fetchone()
        assert fila["cantidad"] == 2
        assert fila["ajuste"] == Decimal("-1500.0000")
        assert fila["origen_ok"] is True

    assert cuenta_corriente.get_facturado_real("WAIMAO") == 8500.0
    movimientos = cuenta_corriente.movimientos_cuenta_paginados(
        "WAIMAO", "consolidado", "todos", 1, 25
    )["items"]
    comerciales = [m for m in movimientos if m["tipo"] == "AJUSTE_PRECIO"]
    assert len(comerciales) == 2
    assert any(m["haber_ars"] == Decimal("2000.00") for m in comerciales)
    assert any(m["debe_ars"] == Decimal("500.00") for m in comerciales)
    envio = solicitudes_guia.obtener_solicitud_de_cliente(solicitud_id, "WAIMAO")
    assert envio["precio_inicial_cliente_ars"] == Decimal("10000.00")
    assert envio["ajuste_comercial_ars"] == Decimal("-1500.0000")
    assert envio["precio_final_cliente_ars"] == Decimal("8500.0000")


def test_ajuste_rechaza_otro_cliente_y_motivo_sin_evidencia(db):
    _solicitud_id, envio_id = _crear_envio(db)
    with pytest.raises(
        ajustes_precio_admin.AjustePrecioAdminError,
        match="pertenece a otro cliente",
    ):
        ajustes_precio_admin.aplicar_nuevo_precio(
            cliente_id="MELCIOR",
            envio_id=envio_id,
            nuevo_precio_ars="9000",
            motivo="Descuento comercial acordado",
            actor="test-admin",
            idempotency_key="precio_admin_00000000000000000003",
        )
    with pytest.raises(
        ajustes_precio_admin.AjustePrecioAdminError,
        match="al menos 8 caracteres",
    ):
        ajustes_precio_admin.aplicar_nuevo_precio(
            cliente_id="WAIMAO",
            envio_id=envio_id,
            nuevo_precio_ars="9000",
            motivo="promo",
            actor="test-admin",
            idempotency_key="precio_admin_00000000000000000004",
        )
