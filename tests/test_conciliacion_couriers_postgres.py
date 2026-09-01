"""Controles financieros sobre PostgreSQL real y un schema descartable."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from core import database
from servicios import conciliacion_couriers as conciliacion


DATABASE_URL = os.getenv("TAURO_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)


@pytest.fixture
def conciliacion_db(monkeypatch):
    schema = f"test_conciliacion_{uuid.uuid4().hex}"
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
            assert all(database._verificar_readiness_contable(cur).values())

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

        monkeypatch.setattr(conciliacion, "get_conn", get_conn_aislada)
        yield get_conn_aislada
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _crear_solicitud(
    get_conn,
    *,
    sufijo: str,
    courier: str = "DHL",
    tracking: str | None = None,
    precio: Decimal = Decimal("10000"),
) -> int:
    cliente_id = f"CLIENTE_{sufijo}"
    tracking = tracking or f"TRACK-{sufijo}"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO clientes (cliente_id, email, nombre)
                VALUES (%s, %s, %s)
                """,
                (cliente_id, f"{sufijo.lower()}@example.invalid", cliente_id),
            )
            cur.execute(
                """
                INSERT INTO solicitudes_guia (
                    cliente_id, producto_alias, destino_pais, dest_nombre,
                    dest_direccion, dest_ciudad, dest_zip, courier, tracking,
                    coti_id, precio_tauro_ars
                ) VALUES (
                    %s, 'Producto', 'US', 'Destinatario', 'Calle 1',
                    'Miami', '33101', %s, %s, %s, %s
                )
                RETURNING id
                """,
                (cliente_id, courier, tracking, f"COTI-{sufijo}", precio),
            )
            return int(cur.fetchone()["id"])


def _snapshot_basico(
    solicitud_id: int,
    *,
    costo: str,
    precio: str,
    margen: str,
    coti_id: str,
):
    return conciliacion.registrar_snapshot_cotizacion(
        solicitud_id=solicitud_id,
        coti_id=coti_id,
        courier="DHL",
        moneda_courier="ARS",
        tipo_cambio_ars="1",
        costo_courier_estimado=costo,
        precio_cliente_inicial_ars=precio,
        margen_tauro_protegido_ars=margen,
        peso_real_cotizado_kg="1",
        peso_volumetrico_cotizado_kg="2",
        peso_facturable_cotizado_kg="2",
        actor="test",
    )


def _crear_cargo_activo(get_conn, solicitud_id: int, *, monto: str = "10000"):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cliente_id, tracking FROM solicitudes_guia WHERE id = %s
                """,
                (int(solicitud_id),),
            )
            solicitud = cur.fetchone()
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, descripcion,
                    tracking, solicitud_id, ambito
                ) VALUES (%s, CURRENT_DATE, %s, 'ACTIVO', 'Envío de prueba',
                          %s, %s, 'INTERNACIONAL')
                """,
                (
                    solicitud["cliente_id"], Decimal(monto),
                    solicitud["tracking"], int(solicitud_id),
                ),
            )


def _confirmar_todos(get_conn, solicitud_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM factura_courier_item_matches
                 WHERE solicitud_id = %s AND estado = 'PROPUESTO'
                 ORDER BY id
                """,
                (solicitud_id,),
            )
            ids = [int(fila["id"]) for fila in cur.fetchall()]
    for match_id in ids:
        conciliacion.confirmar_match(match_id, actor="auditor@test")
    return ids


def test_flujo_completo_preserva_margen_y_no_aplica_el_ajuste(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="WAIMAO",
        tracking="DHL-0001",
        precio=Decimal("10000"),
    )
    snapshot = _snapshot_basico(
        solicitud_id,
        costo="5000",
        precio="10000",
        margen="5000",
        coti_id="COTI-WAIMAO",
    )
    assert snapshot["duplicado"] is False

    datos_factura = {
        "courier": "DHL",
        "tipo_documento": "FC",
        "numero": "FC 000-001",
        "moneda": "ARS",
        "total": "15000",
        "actor": "parser@test",
        "archivo_sha256": "a" * 64,
        "items": [{
            "linea_numero": 1,
            "tracking": "DHL0001",
            "concepto_tipo": "FLETE",
            "importe": "15000",
            "peso_real_kg": "2.5",
            "peso_volumetrico_kg": "3.2",
            "peso_facturado_kg": "3.2",
            "peso_base": "VOLUMETRICO",
        }],
    }
    factura = conciliacion.registrar_factura_courier(**datos_factura)
    assert factura["duplicado"] is False
    assert conciliacion.registrar_factura_courier(**datos_factura) == {
        "id": factura["id"],
        "duplicado": True,
        "evidencia_actualizada": False,
    }
    factura_mismo_numero_otro_pdf = dict(datos_factura)
    factura_mismo_numero_otro_pdf["archivo_sha256"] = "f" * 64
    with pytest.raises(
        conciliacion.DocumentoCourierDuplicadoError,
        match="archivo es diferente",
    ):
        conciliacion.registrar_factura_courier(
            **factura_mismo_numero_otro_pdf
        )

    assert conciliacion.matchear_items_exactos(
        factura["id"], actor="matcher@test"
    ) == {"propuestos": 1, "sin_match": 0}
    assert len(_confirmar_todos(conciliacion_db, solicitud_id)) == 1

    resultado = conciliacion.calcular_conciliacion_envio(
        solicitud_id, actor="auditor@test"
    )
    assert resultado["estado"] == "PARA_REVISION"
    assert resultado["costo_courier_real_ars"] == Decimal("15000.0000")
    assert resultado["margen_tauro_protegido_ars"] == Decimal("5000.0000")
    assert resultado["precio_cliente_final_ars"] == Decimal("20000.0000")
    assert resultado["ajuste_cliente_ars"] == Decimal("10000.0000")
    assert resultado["ajuste_id"] is not None

    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT estado, tipo, monto_ars FROM ajustes_cliente"
            )
            ajuste = cur.fetchone()
            assert ajuste == {
                "estado": "PROPUESTO",
                "tipo": "DEBITO",
                "monto_ars": Decimal("10000.0000"),
            }
            cur.execute("SELECT monto_ars FROM envios")
            assert cur.fetchall() == []
            cur.execute(
                "SELECT motivo_diferencia FROM conciliaciones_envio"
            )
            assert cur.fetchone()["motivo_diferencia"] == "PESO_VOLUMETRICO"


def test_fc_nc_y_lineas_repetidas_por_tracking_se_suman_con_signo(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="NOTA_CREDITO",
        tracking="DHL-NC-1",
        precio=Decimal("12000"),
    )
    _snapshot_basico(
        solicitud_id,
        costo="10000",
        precio="12000",
        margen="2000",
        coti_id="COTI-NOTA_CREDITO",
    )
    factura = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-2",
        moneda="ARS",
        subtotal="12000",
        total="12000",
        actor="parser@test",
        archivo_sha256="b" * 64,
        items=[
            {
                "linea_numero": 1,
                "tracking": "DHLNC1",
                "concepto_tipo": "FLETE",
                "importe": "10000",
            },
            {
                "linea_numero": 2,
                "tracking": "DHL-NC-1",
                "concepto_tipo": "COMBUSTIBLE",
                "importe": "2000",
            },
        ],
    )
    nota = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="NC",
        numero="NC-2",
        moneda="ARS",
        total="1000",
        actor="parser@test",
        archivo_sha256="c" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "DHLNC1",
            "concepto_tipo": "DESCUENTO",
            "importe": "1000",
        }],
    )
    assert conciliacion.matchear_items_exactos(factura["id"])["propuestos"] == 2
    assert conciliacion.matchear_items_exactos(nota["id"])["propuestos"] == 1
    assert len(_confirmar_todos(conciliacion_db, solicitud_id)) == 3

    resultado = conciliacion.calcular_conciliacion_envio(
        solicitud_id, actor="auditor@test"
    )
    assert resultado["costo_courier_real_ars"] == Decimal("11000.0000")
    assert resultado["precio_cliente_final_ars"] == Decimal("13000.0000")
    assert resultado["ajuste_cliente_ars"] == Decimal("1000.0000")


def test_db_bloquea_mutar_snapshot_borrar_factura_y_sobreasignar_item(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="CONTROL",
        tracking="DHL-CONTROL",
        precio=Decimal("10000"),
    )
    _snapshot_basico(
        solicitud_id,
        costo="5000",
        precio="10000",
        margen="5000",
        coti_id="COTI-CONTROL",
    )
    factura = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-CONTROL",
        moneda="ARS",
        total="100",
        actor="test",
        archivo_sha256="d" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "DHL-CONTROL",
            "importe": "100",
        }],
    )
    conciliacion.matchear_items_exactos(factura["id"])

    with pytest.raises(psycopg2.errors.RaiseException, match="ítem con match"):
        with conciliacion_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE facturas_courier_items
                       SET importe = 1, importe_ars = 1
                     WHERE factura_id = %s
                    """,
                    (factura["id"],),
                )

    with pytest.raises(psycopg2.errors.RaiseException, match="append-only"):
        with conciliacion_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auditoria_facturas_courier SET actor = 'otro'"
                )

    with pytest.raises(psycopg2.errors.RaiseException, match="inmutable"):
        with conciliacion_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE envio_cotizacion_snapshots
                       SET margen_tauro_protegido_ars = 1
                     WHERE solicitud_id = %s
                    """,
                    (solicitud_id,),
                )

    with pytest.raises(psycopg2.errors.RaiseException, match="no se eliminan"):
        with conciliacion_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM facturas_courier WHERE id = %s",
                    (factura["id"],),
                )

    otra_solicitud = _crear_solicitud(
        conciliacion_db,
        sufijo="CONTROL_2",
        tracking="OTRO-TRACKING",
        precio=Decimal("10000"),
    )
    with pytest.raises(
        psycopg2.errors.RaiseException,
        match="exceden el importe",
    ):
        with conciliacion_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, importe, importe_ars FROM facturas_courier_items"
                )
                item = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO factura_courier_item_matches (
                        item_id, solicitud_id, monto_asignado,
                        monto_asignado_ars, metodo, estado,
                        evidencia_uri, creado_por
                    ) VALUES (%s, %s, %s, %s, 'MANUAL', 'PROPUESTO',
                              'evidencia:test', 'auditor@test')
                    """,
                    (
                        item["id"], otra_solicitud,
                        item["importe"], item["importe_ars"],
                    ),
                )


def test_match_manual_exige_cargo_evidencia_y_confirmacion(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="MANUAL",
        tracking="DHL-REAL-99",
        precio=Decimal("10000"),
    )
    _crear_cargo_activo(conciliacion_db, solicitud_id)
    factura = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-MANUAL",
        moneda="ARS",
        total="15000",
        actor="parser@test",
        archivo_sha256="e" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "TRACKING-ERRONEO",
            "importe": "15000",
        }],
    )
    assert conciliacion.matchear_items_exactos(factura["id"])["sin_match"] == 1
    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM facturas_courier_items WHERE factura_id=%s",
                (factura["id"],),
            )
            item_id = int(cur.fetchone()["id"])

    propuesta = conciliacion.proponer_match_manual(
        item_id,
        factura_id_esperada=factura["id"],
        identificador_envio=f"#{solicitud_id}",
        motivo="Tracking incorrecto informado por courier",
        actor="admin@test",
    )
    assert propuesta["solicitud_id"] == solicitud_id
    assert propuesta["monto_asignado"] == Decimal("15000.0000")
    confirmado = conciliacion.confirmar_match(
        propuesta["id"], actor="auditor@test"
    )
    assert confirmado["duplicado"] is False

    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT estado, metodo, evidencia_uri FROM factura_courier_item_matches"
            )
            match = cur.fetchone()
            assert match["estado"] == "CONFIRMADO"
            assert match["metodo"] == "MANUAL"
            assert match["evidencia_uri"].startswith("admin://match-manual/")
            cur.execute(
                """
                SELECT evento FROM auditoria_facturas_courier
                 WHERE solicitud_id=%s ORDER BY id
                """,
                (solicitud_id,),
            )
            assert [fila["evento"] for fila in cur.fetchall()] == [
                "MATCH_MANUAL_PROPUESTO", "MATCH_CONFIRMADO"
            ]


def test_rechazo_de_match_conserva_historial_y_libera_el_importe(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="RECHAZO",
        tracking="DHL-RECHAZO",
    )
    factura = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-RECHAZO",
        moneda="ARS",
        total="100",
        actor="parser@test",
        archivo_sha256="f" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "DHL-RECHAZO",
            "importe": "100",
        }],
    )
    conciliacion.matchear_items_exactos(factura["id"], actor="matcher@test")
    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM factura_courier_item_matches")
            match_id = int(cur.fetchone()["id"])
    conciliacion.rechazar_match(
        match_id,
        actor="admin@test",
        motivo="La factura corresponde a otra operación",
    )
    detalle = conciliacion.obtener_factura_courier_control(factura["id"])
    assert detalle["items"][0]["remanente"] == Decimal("100.0000")
    assert detalle["items"][0]["matches"][0]["match_estado"] == "RECHAZADO"


def test_diferencia_posterior_genera_solo_movimiento_incremental(
    conciliacion_db,
):
    solicitud_id = _crear_solicitud(
        conciliacion_db,
        sufijo="INCREMENTAL",
        tracking="DHL-INCREMENTAL",
        precio=Decimal("10000"),
    )
    _crear_cargo_activo(conciliacion_db, solicitud_id)
    _snapshot_basico(
        solicitud_id,
        costo="5000",
        precio="10000",
        margen="5000",
        coti_id="COTI-INCREMENTAL",
    )
    primera = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-INCREMENTAL-1",
        moneda="ARS",
        total="15000",
        actor="parser@test",
        archivo_sha256="1" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "DHL-INCREMENTAL",
            "importe": "15000",
        }],
    )
    conciliacion.matchear_items_exactos(primera["id"])
    _confirmar_todos(conciliacion_db, solicitud_id)
    calculo_1 = conciliacion.calcular_conciliacion_envio(
        solicitud_id, actor="auditor@test"
    )
    conciliacion.aprobar_y_aplicar_ajuste_cliente(
        calculo_1["ajuste_id"], actor="admin@test", referencia="FC-1"
    )

    adicional = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="ND",
        numero="ND-INCREMENTAL-2",
        moneda="ARS",
        total="2000",
        actor="parser@test",
        archivo_sha256="2" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "DHL-INCREMENTAL",
            "importe": "2000",
            "concepto_tipo": "MANEJO",
        }],
    )
    conciliacion.matchear_items_exactos(adicional["id"])
    _confirmar_todos(conciliacion_db, solicitud_id)
    calculo_2 = conciliacion.calcular_conciliacion_envio(
        solicitud_id, actor="auditor@test"
    )

    assert calculo_2["precio_cliente_final_ars"] == Decimal("22000.0000")
    assert calculo_2["ajuste_cliente_ars"] == Decimal("12000.0000")
    assert calculo_2["movimiento_cliente_ars"] == Decimal("2000.0000")
    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT monto_ars, precio_anterior_ars, precio_nuevo_ars
                  FROM ajustes_cliente WHERE id=%s
                """,
                (calculo_2["ajuste_id"],),
            )
            ajuste = cur.fetchone()
            assert ajuste == {
                "monto_ars": Decimal("2000.0000"),
                "precio_anterior_ars": Decimal("20000.0000"),
                "precio_nuevo_ars": Decimal("22000.0000"),
            }


def test_linea_prorrateada_no_habilita_revision_hasta_completar_asignacion(
    conciliacion_db,
):
    primero = _crear_solicitud(
        conciliacion_db,
        sufijo="PRORRATEO_A",
        tracking="DHL-PRORRATEO-A",
        precio=Decimal("100"),
    )
    segundo = _crear_solicitud(
        conciliacion_db,
        sufijo="PRORRATEO_B",
        tracking="DHL-PRORRATEO-B",
        precio=Decimal("100"),
    )
    _crear_cargo_activo(conciliacion_db, primero, monto="100")
    _crear_cargo_activo(conciliacion_db, segundo, monto="100")
    _snapshot_basico(
        primero,
        costo="50",
        precio="100",
        margen="50",
        coti_id="COTI-PRORRATEO_A",
    )
    factura = conciliacion.registrar_factura_courier(
        courier="DHL",
        tipo_documento="FC",
        numero="FC-PRORRATEO",
        moneda="ARS",
        total="100",
        actor="parser@test",
        archivo_sha256="3" * 64,
        items=[{
            "linea_numero": 1,
            "tracking": "AGRUPADO-SIN-COINCIDENCIA",
            "importe": "100",
        }],
    )
    with conciliacion_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM facturas_courier_items WHERE factura_id=%s",
                (factura["id"],),
            )
            item_id = int(cur.fetchone()["id"])
    match_a = conciliacion.proponer_match_manual(
        item_id,
        identificador_envio=f"#{primero}",
        monto_asignado="40",
        motivo="Prorrateo documentado entre dos envíos",
        actor="admin@test",
    )
    match_b = conciliacion.proponer_match_manual(
        item_id,
        identificador_envio=f"#{segundo}",
        monto_asignado="60",
        motivo="Prorrateo documentado entre dos envíos",
        actor="admin@test",
    )
    conciliacion.confirmar_match(match_a["id"], actor="auditor@test")
    borrador = conciliacion.calcular_conciliacion_envio(
        primero, actor="auditor@test"
    )
    assert borrador["estado"] == "BORRADOR"

    conciliacion.confirmar_match(match_b["id"], actor="auditor@test")
    listo = conciliacion.calcular_conciliacion_envio(
        primero, actor="auditor@test"
    )
    assert listo["duplicado"] is True
    assert listo["estado"] == "PARA_REVISION"
