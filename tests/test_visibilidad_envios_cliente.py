"""Visibilidad reversible de envíos sin alterar la cuenta corriente."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from starlette.requests import Request

from endpoints import admin as admin_endpoint
from servicios import cuenta_corriente, solicitudes_guia


RAIZ = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


def _bloque(fuente: str, inicio: str, fin: str) -> str:
    return fuente.split(inicio, 1)[1].split(fin, 1)[0]


def test_contrato_oculta_sin_cancelar_ni_borrar():
    schema = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")
    servicio = (RAIZ / "servicios/solicitudes_guia.py").read_text(
        encoding="utf-8"
    )
    admin = (RAIZ / "endpoints/admin.py").read_text(encoding="utf-8")
    pedido_admin = (RAIZ / "templates/admin/pedidos.html").read_text(
        encoding="utf-8"
    )
    cliente_admin = (RAIZ / "templates/admin/cliente_detail.html").read_text(
        encoding="utf-8"
    )

    assert "visible_cliente          BOOLEAN NOT NULL DEFAULT TRUE" in schema
    assert (
        "ADD COLUMN IF NOT EXISTS visible_cliente BOOLEAN NOT NULL DEFAULT TRUE"
        in schema
    )
    cambio = _bloque(
        servicio,
        "def cambiar_visibilidad_cliente(",
        "def actualizar_solicitud_guia(",
    )
    assert "UPDATE solicitudes_guia" in cambio
    assert "SET visible_cliente=%s, updated_at=NOW()" in cambio
    assert "UPDATE envios" not in cambio
    assert "DELETE FROM" not in cambio
    assert '@router.post("/pedidos/{solicitud_id}/visibilidad")' in admin
    assert "admin.visibilidad_envio_cliente" in admin
    for plantilla in (pedido_admin, cliente_admin):
        assert "Ocultar del portal" in plantilla
        assert "Mostrar en portal" in plantilla
        assert "No se borrará ni cambiará el saldo" in plantilla


def test_todos_los_accesos_del_cliente_exigen_visibilidad():
    servicio = (RAIZ / "servicios/solicitudes_guia.py").read_text(
        encoding="utf-8"
    )
    panel = (RAIZ / "servicios/panel_cliente.py").read_text(encoding="utf-8")

    for inicio, fin in (
        ("def listar_solicitudes_cliente(", "def periodos_solicitudes_cliente("),
        ("def periodos_solicitudes_cliente(", "def listar_envios_api("),
        ("def listar_envios_api(", "def contar_guias_listas("),
        ("def contar_guias_listas(", "def listar_solicitudes_admin("),
        ("def obtener_solicitud_de_cliente(", "def obtener_label_de_cliente("),
        ("def obtener_label_de_cliente(", "# ── Emisión de guía real"),
        ("def preparar_documentos_envio_portal(", "def cargar_envio_externo("),
    ):
        assert "visible_cliente=TRUE" in _bloque(servicio, inicio, fin)

    assert panel.count("s.visible_cliente=TRUE") >= 2
    assert "AND s.test=FALSE AND s.visible_cliente=TRUE" in _bloque(
        servicio, "def validar_reemision_cliente(", "def _validar_cancelacion_desde_fila("
    )
    assert "AND s.test=FALSE AND s.visible_cliente=TRUE" in _bloque(
        servicio, "def validar_cancelacion_cliente(", "def cancelar_solicitud_cliente("
    )
    assert "AND s.test=FALSE AND s.visible_cliente=TRUE" in _bloque(
        servicio, "def cancelar_solicitud_cliente(", "def crear_solicitud_guia("
    )
    assert "AND s.test=FALSE AND s.visible_cliente=TRUE" in _bloque(
        servicio, "def _reservar_credito_cliente(", "def generar_guia("
    )


def test_endpoint_admin_audita_y_vuelve_al_cliente(monkeypatch):
    monkeypatch.setattr(admin_endpoint, "_is_auth", lambda token: token == "ok")
    cambios = []
    monkeypatch.setattr(
        admin_endpoint,
        "cambiar_visibilidad_cliente",
        lambda solicitud_id, visible: cambios.append((solicitud_id, visible)) or {
            "id": solicitud_id,
            "cliente_id": "WAIMAO",
            "visible_cliente": visible,
            "test": False,
        },
    )
    auditoria = []
    monkeypatch.setattr(
        "servicios.auditoria.registrar_desde_request",
        lambda *_args, **kwargs: auditoria.append(kwargs),
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/admin/pedidos/91/visibilidad",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "scheme": "http",
    })

    respuesta = admin_endpoint.admin_pedido_visibilidad(
        request,
        91,
        visible_cliente="0",
        volver_cliente="waimao",
        admin_token="ok",
    )

    assert cambios == [(91, False)]
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/admin/clientes/WAIMAO?ok=envio_oculto"
    assert auditoria[0]["event"] == "admin.visibilidad_envio_cliente"
    assert auditoria[0]["metadata"]["visible_cliente"] is False


@pytest.fixture
def visibilidad_db(monkeypatch):
    if not DATABASE_URL:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL aislada")

    schema = f"test_visibilidad_envios_{uuid.uuid4().hex}"
    schema_sql = (RAIZ / "sql/schema.sql").read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            # La migración debe seguir siendo idempotente en instalaciones ya creadas.
            cur.execute(schema_sql)
            cur.execute(schema_sql)

        @contextmanager
        def conexion():
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

        monkeypatch.setattr(solicitudes_guia, "get_conn", conexion)
        monkeypatch.setattr(cuenta_corriente, "get_conn", conexion)
        yield conexion
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def test_ocultar_es_reversible_y_conserva_cargo(visibilidad_db):
    with visibilidad_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO clientes (cliente_id,email) "
                "VALUES ('WAIMAO','waimao-visibilidad@example.invalid')"
            )
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, destino_pais, dest_nombre,
                    dest_direccion, dest_ciudad, dest_zip, estado, tracking,
                    ambito, courier, label_pdf
                ) VALUES (
                    'WAIMAO', 'Real', 'US', 'Destino real', 'Calle 1',
                    'Miami', '33101', 'GUIA_LISTA', 'REAL-1',
                    'INTERNACIONAL', 'DHL', %s
                ) RETURNING id, visible_cliente
                """,
                (psycopg2.Binary(b"%PDF-1.4\n%%EOF\n"),),
            )
            visible = cur.fetchone()
            assert visible["visible_cliente"] is True
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, destino_pais, dest_nombre,
                    dest_direccion, dest_ciudad, dest_zip, estado, tracking,
                    ambito, courier, label_pdf, visible_cliente
                ) VALUES (
                    'WAIMAO', 'PRUEBA', 'US', 'Destino prueba', 'Calle 2',
                    'Miami', '33101', 'GUIA_LISTA', 'TEST-1',
                    'INTERNACIONAL', 'DHL', %s, FALSE
                ) RETURNING id
                """,
                (psycopg2.Binary(b"%PDF-1.4\n%%EOF\n"),),
            )
            oculto_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, descripcion,
                    tracking, solicitud_id, ambito
                ) VALUES (
                    'WAIMAO', CURRENT_DATE, 1250.50, 'ACTIVO', 'PRUEBA',
                    'TEST-1', %s, 'INTERNACIONAL'
                ) RETURNING id
                """,
                (oculto_id,),
            )
            cargo_id = int(cur.fetchone()["id"])

    listado = solicitudes_guia.listar_solicitudes_cliente("waimao", limite=None)
    assert [fila["id"] for fila in listado] == [int(visible["id"])]
    api, total = solicitudes_guia.listar_envios_api("WAIMAO")
    assert total == 1
    assert [fila["id"] for fila in api] == [int(visible["id"])]
    assert solicitudes_guia.contar_guias_listas("WAIMAO") == 1
    assert solicitudes_guia.obtener_solicitud_de_cliente(oculto_id, "WAIMAO") is None
    assert solicitudes_guia.obtener_label_de_cliente(oculto_id, "WAIMAO") is None
    assert solicitudes_guia.obtener_label_pdf(oculto_id, "WAIMAO") is None

    resumen = cuenta_corriente.resumen_cuenta_por_ambito("WAIMAO")
    assert resumen["consolidado"]["debe_ars"] == Decimal("1250.50")
    movimientos = cuenta_corriente.movimientos_cuenta_paginados("WAIMAO")
    [cargo] = [m for m in movimientos["items"] if m["envio_id"] == cargo_id]
    assert cargo["monto_ars"] == Decimal("1250.50")
    assert cargo["solicitud_id"] is None

    cambio = solicitudes_guia.cambiar_visibilidad_cliente(oculto_id, True)
    assert cambio and cambio["visible_cliente"] is True
    assert solicitudes_guia.obtener_solicitud_de_cliente(oculto_id, "WAIMAO")
    assert solicitudes_guia.contar_guias_listas("WAIMAO") == 2

    solicitudes_guia.cambiar_visibilidad_cliente(oculto_id, False)
    with visibilidad_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT monto_ars, estado FROM envios WHERE id=%s", (cargo_id,)
            )
            cargo_guardado = cur.fetchone()
    assert cargo_guardado == {"monto_ars": Decimal("1250.50"), "estado": "ACTIVO"}
