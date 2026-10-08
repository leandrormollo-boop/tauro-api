"""Contrato compartido para las acciones pendientes del cliente."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from pypdf import PdfWriter

from servicios import acciones_cliente as acciones


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()


def _envio(**cambios):
    envio = {
        "estado": "GUIA_LISTA",
        "tracking_estado": None,
        "cargo_estado": "ACTIVO",
        "guia_descargada_at": None,
        "tiene_label": True,
        "guia_url": None,
        "coti_id": "COTI-PORTAL",
        "test": False,
        "visible_cliente": True,
    }
    envio.update(cambios)
    return envio


def test_accion_descarga_es_concreta_y_no_comparte_estado_mutable():
    primera = acciones.accion_pendiente_cliente(_envio())
    segunda = acciones.accion_pendiente_cliente(_envio())

    assert primera == {
        "codigo": "descargar_guia",
        "titulo": "Descargar guía",
        "detalle": "Descargá la guía para preparar el envío.",
    }
    assert segunda == primera
    assert segunda is not primera


@pytest.mark.parametrize(
    "cambios",
    [
        {"guia_descargada_at": "2026-10-08T10:00:00Z"},
        {"coti_id": "EXT-carga-manual"},
        {"tracking_estado": "RETENIDO"},
        {"tracking_estado": "ENTREGADO"},
        {"estado": "ENTREGADO"},
        {"estado": "CANCELADO"},
        {"cargo_estado": "CANCELADO"},
        {"estado": "REEMPLAZADO"},
        {"tiene_label": False, "guia_url": ""},
        {"test": True},
        {"visible_cliente": False},
        {
            "reemplaza_solicitud_id": 90,
            "reemision_estado": "PENDIENTE",
            "cargo_pendiente": False,
        },
        {
            "reemplaza_solicitud_id": 90,
            "reemision_estado": "EMITIDA",
            "cargo_pendiente": True,
        },
    ],
)
def test_no_pide_accion_fuera_del_unico_caso_modelado(cambios):
    envio = _envio(**cambios)

    assert acciones.requiere_accion_cliente(envio) is False
    assert acciones.accion_pendiente_cliente(envio) is None


def test_tracking_vacio_y_pdf_descargable_si_requieren_accion():
    envio = _envio(
        tracking_estado="  ",
        tiene_label=True,
    )

    assert acciones.requiere_accion_cliente(envio) is True


def test_url_sin_pdf_guardado_no_es_una_accion_resoluble():
    envio = _envio(
        tiene_label=False,
        guia_url="https://documentos.example.invalid/guia.pdf",
    )

    assert acciones.requiere_accion_cliente(envio) is False
    assert acciones.accion_pendiente_cliente(envio) is None


def test_listado_ya_filtrado_puede_omitir_test_y_visibilidad():
    envio = _envio()
    envio.pop("test")
    envio.pop("visible_cliente")

    assert acciones.requiere_accion_cliente(envio) is True


def test_reemision_emitida_con_cargo_confirmado_si_habilita_la_accion():
    envio = _envio(
        reemplaza_solicitud_id=90,
        reemision_estado="EMITIDA",
        cargo_pendiente=False,
    )

    assert acciones.requiere_accion_cliente(envio) is True


def test_no_infiere_envio_realizado_por_courier_nombre_o_antiguedad():
    envio = _envio(
        courier="DHL",
        dest_nombre="Carga manual histórica",
        created_at="2020-01-01T00:00:00Z",
        coti_id="COTI-REAL",
    )

    assert acciones.requiere_accion_cliente(envio) is True


@pytest.mark.parametrize(
    "alias",
    ["s; DROP TABLE solicitudes_guia", "s.envio", "s envio", "", 7, None],
)
def test_alias_sql_rechaza_identificadores_inseguros(alias):
    with pytest.raises(ValueError, match="alias SQL"):
        acciones.accion_cliente_sql(alias)


def test_predicado_sql_admite_alias_simple():
    predicado = acciones.accion_cliente_sql("solicitud")

    assert "solicitud.estado" in predicado
    assert "accion_cargo_cancelado.cliente_id = solicitud.cliente_id" in predicado
    assert "solicitudes_guia_reemisiones accion_reemision" in predicado
    assert "solicitud.cargo_pendiente = TRUE" in predicado
    assert "NOT EXISTS" in predicado


@pytest.fixture
def acciones_db(monkeypatch):
    if not DATABASE_URL:
        pytest.skip("requiere TAURO_TEST_DATABASE_URL aislada")

    schema = f"test_acciones_cliente_{uuid.uuid4().hex}"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "sql" / "schema.sql"
    ).read_text(encoding="utf-8")
    admin = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    admin.set_client_encoding("UTF8")
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
            conn.set_client_encoding("UTF8")
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

        monkeypatch.setattr(acciones, "get_conn", get_conn_aislada)
        yield get_conn_aislada
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _crear_cliente(cur, cliente_id):
    cur.execute(
        """
        INSERT INTO clientes (cliente_id, email, nombre)
        VALUES (%s, %s, %s)
        """,
        (cliente_id, f"{cliente_id.lower()}@example.invalid", cliente_id),
    )


def _crear_solicitud(cur, *, cliente_id="WAIMAO", **cambios):
    datos = {
        "estado": "GUIA_LISTA",
        "tracking_estado": None,
        "guia_descargada_at": None,
        "label_pdf": b"%PDF-guia",
        "guia_url": None,
        "coti_id": f"COTI-{uuid.uuid4().hex}",
        "test": False,
        "visible_cliente": True,
        "cargo_pendiente": False,
    }
    datos.update(cambios)
    cur.execute(
        """
        INSERT INTO solicitudes_guia (
            cliente_id, producto_alias, destino_pais, dest_nombre,
            dest_direccion, dest_ciudad, dest_zip, estado, tracking_estado,
            guia_descargada_at, label_pdf, guia_url, coti_id, test,
            visible_cliente, cargo_pendiente
        ) VALUES (
            %(cliente_id)s, 'Producto', 'US', 'Destinatario',
            'Calle 1', 'Miami', '33101', %(estado)s, %(tracking_estado)s,
            %(guia_descargada_at)s, %(label_pdf)s, %(guia_url)s,
            %(coti_id)s, %(test)s, %(visible_cliente)s,
            %(cargo_pendiente)s
        )
        RETURNING id
        """,
        {"cliente_id": cliente_id, **datos},
    )
    return int(cur.fetchone()["id"])


def _pdf_minimo() -> bytes:
    salida = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(salida)
    return salida.getvalue()


def test_python_y_sql_seleccionan_las_mismas_acciones(acciones_db, monkeypatch):
    with acciones_db() as conn:
        with conn.cursor() as cur:
            _crear_cliente(cur, "WAIMAO")
            _crear_cliente(cur, "OTRO")
            ids = {
                "label": _crear_solicitud(cur),
                "solo_url": _crear_solicitud(
                    cur,
                    label_pdf=None,
                    guia_url="https://example.invalid/guia",
                ),
                "descargada": _crear_solicitud(cur, guia_descargada_at="2026-10-08 10:00:00+00"),
                "externa": _crear_solicitud(cur, coti_id="EXT-manual"),
                "retenida": _crear_solicitud(cur, tracking_estado="RETENIDO"),
                "entregada": _crear_solicitud(cur, estado="ENTREGADO"),
                "cancelada": _crear_solicitud(cur, estado="CANCELADO"),
                "reemplazada": _crear_solicitud(cur, estado="REEMPLAZADO"),
                "sin_pdf": _crear_solicitud(cur, label_pdf=None, guia_url="  "),
                "prueba": _crear_solicitud(cur, test=True),
                "oculta": _crear_solicitud(cur, visible_cliente=False),
                "cargo_cancelado": _crear_solicitud(cur),
                "cancelado_otro_cliente": _crear_solicitud(cur),
                "reemision_anterior_pendiente": _crear_solicitud(
                    cur, estado="REEMPLAZADO", label_pdf=None,
                ),
                "reemision_pendiente": _crear_solicitud(cur),
                "reemision_anterior_emitida": _crear_solicitud(
                    cur, estado="REEMPLAZADO", label_pdf=None,
                ),
                "reemision_emitida": _crear_solicitud(
                    cur, label_pdf=_pdf_minimo(),
                ),
                "reemision_anterior_cargo": _crear_solicitud(
                    cur, estado="REEMPLAZADO", label_pdf=None,
                ),
                "reemision_emitida_cargo_pendiente": _crear_solicitud(
                    cur, cargo_pendiente=True,
                ),
            }
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, descripcion,
                    solicitud_id
                ) VALUES
                    ('WAIMAO', CURRENT_DATE, 1, 'CANCELADO', 'Cancelado', %s),
                    ('OTRO', CURRENT_DATE, 1, 'CANCELADO', 'Cruce inválido', %s)
                """,
                (ids["cargo_cancelado"], ids["cancelado_otro_cliente"]),
            )
            cur.execute(
                """
                INSERT INTO solicitudes_guia_reemisiones (
                    cliente_id, solicitud_anterior_id, solicitud_nueva_id,
                    tracking_anterior, estado
                ) VALUES
                    ('WAIMAO', %s, %s, 'OLD-PENDING', 'PENDIENTE'),
                    ('WAIMAO', %s, %s, 'OLD-EMITTED', 'EMITIDA'),
                    ('WAIMAO', %s, %s, 'OLD-CARGO', 'EMITIDA')
                """,
                (
                    ids["reemision_anterior_pendiente"],
                    ids["reemision_pendiente"],
                    ids["reemision_anterior_emitida"],
                    ids["reemision_emitida"],
                    ids["reemision_anterior_cargo"],
                    ids["reemision_emitida_cargo_pendiente"],
                ),
            )

            cur.execute(
                f"""
                SELECT s.id
                FROM solicitudes_guia s
                WHERE s.cliente_id = 'WAIMAO'
                  AND {acciones.accion_cliente_sql('s')}
                ORDER BY s.id
                """
            )
            ids_sql = {int(fila["id"]) for fila in cur.fetchall()}

            cur.execute(
                """
                SELECT s.estado, s.tracking_estado, s.guia_descargada_at,
                       s.guia_url, s.coti_id, s.test, s.visible_cliente,
                       s.cargo_pendiente,
                       (s.label_pdf IS NOT NULL) AS tiene_label,
                       re.solicitud_anterior_id AS reemplaza_solicitud_id,
                       re.estado AS reemision_estado,
                       (
                           SELECT e.estado
                           FROM envios e
                           WHERE e.solicitud_id=s.id
                             AND e.cliente_id=s.cliente_id
                           LIMIT 1
                       ) AS cargo_estado,
                       s.id
                FROM solicitudes_guia s
                LEFT JOIN solicitudes_guia_reemisiones re
                  ON re.solicitud_nueva_id=s.id
                WHERE s.cliente_id='WAIMAO'
                ORDER BY s.id
                """
            )
            ids_python = {
                int(fila["id"])
                for fila in map(dict, cur.fetchall())
                if acciones.requiere_accion_cliente(fila)
            }

    esperados = {
        ids["label"],
        ids["cancelado_otro_cliente"],
        ids["reemision_emitida"],
    }
    assert ids_sql == ids_python == esperados
    assert acciones.contar_acciones_cliente(" waimao ") == len(esperados)
    from servicios import solicitudes_guia
    from servicios.panel_cliente import preparar_historial_envios, resumen_inicio_cliente
    monkeypatch.setattr(solicitudes_guia, 'get_conn', acciones_db)
    assert solicitudes_guia.contar_guias_listas(' waimao ') == len(esperados)
    listado = solicitudes_guia.listar_solicitudes_cliente(" waimao ", limite=None)
    vista = preparar_historial_envios(listado, paso="requieren_accion")
    resumen = resumen_inicio_cliente(listado, [], hoy=None)
    assert {fila["id"] for fila in vista["solicitudes"]} == esperados
    assert vista["total_requieren_accion"] == len(esperados)
    assert resumen["requieren_accion"] == len(esperados)
    assert solicitudes_guia.preparar_documentos_envio_portal(
        ids["reemision_pendiente"], "WAIMAO",
    ) is None
    assert solicitudes_guia.preparar_documentos_envio_portal(
        ids["reemision_emitida_cargo_pendiente"], "WAIMAO",
    ) is None
    assert solicitudes_guia.preparar_documentos_envio_portal(
        ids["reemision_emitida"], "WAIMAO",
    ) is not None
