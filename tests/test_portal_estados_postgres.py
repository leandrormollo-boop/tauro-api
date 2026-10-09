"""Estados coherentes entre Inicio, Estadisticas y Mis envios con PostgreSQL real."""

from __future__ import annotations

from decimal import Decimal

import pytest

from servicios import cuenta_corriente, panel_cliente, solicitudes_guia
from test_conciliacion_couriers_postgres import DATABASE_URL, conciliacion_db


pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requiere TAURO_TEST_DATABASE_URL aislada"
)


@pytest.fixture
def estados_db(conciliacion_db, monkeypatch):
    for modulo in (cuenta_corriente, panel_cliente, solicitudes_guia):
        monkeypatch.setattr(modulo, "get_conn", conciliacion_db)
    return conciliacion_db


def _crear_cliente(db, cliente: str) -> None:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES (%s, %s, %s)
            """,
            (cliente, f"{cliente.lower()}@example.invalid", f"Cuenta {cliente}"),
        )


def _crear_envio(
    db,
    *,
    cliente: str,
    referencia: str,
    estado: str,
    cargo_estado: str | None,
    tracking_estado: str | None = None,
    monto: str = "100.00",
    test: bool = False,
    visible: bool = True,
) -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, cantidad,
                remitente_nombre, remitente_ciudad, remitente_pais,
                destino_pais, dest_nombre, dest_direccion, dest_ciudad,
                dest_zip, ambito, courier, tracking, tracking_estado,
                estado, coti_id, precio_tauro_ars, test, visible_cliente
            ) VALUES (
                %s, 'Producto ficticio', 1,
                'Origen ficticio', 'Buenos Aires', 'AR',
                'US', %s, 'Calle ficticia 1', 'Miami',
                '33101', 'INTERNACIONAL', 'DHL', %s, %s,
                %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                cliente,
                f"Destino {referencia}",
                f"TRACK-{referencia}",
                tracking_estado,
                estado,
                f"COTI-{referencia}",
                Decimal(monto),
                test,
                visible,
            ),
        )
        solicitud_id = int(cur.fetchone()["id"])
        if cargo_estado is not None:
            cur.execute(
                """
                INSERT INTO envios (
                    cliente_id, fecha, monto_ars, estado, descripcion,
                    tracking, solicitud_id, ambito
                ) VALUES (
                    %s, CURRENT_DATE, %s, %s, 'Cargo ficticio',
                    %s, %s, 'INTERNACIONAL'
                )
                """,
                (
                    cliente,
                    Decimal(monto),
                    cargo_estado,
                    f"TRACK-{referencia}",
                    solicitud_id,
                ),
            )
    return solicitud_id


def _conteos(pasos: list[dict]) -> dict[str, int]:
    return {paso["clave"]: int(paso["cantidad"]) for paso in pasos}


def _snapshot(db, cliente: str) -> dict:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, estado, tracking_estado, test, visible_cliente,
                   precio_tauro_ars
            FROM solicitudes_guia
            WHERE cliente_id=%s
            ORDER BY id
            """,
            (cliente,),
        )
        solicitudes = [dict(fila) for fila in cur.fetchall()]
        cur.execute(
            """
            SELECT id, solicitud_id, estado, monto_ars, nro_fc
            FROM envios
            WHERE cliente_id=%s
            ORDER BY id
            """,
            (cliente,),
        )
        cargos = [dict(fila) for fila in cur.fetchall()]
    return {
        "solicitudes": solicitudes,
        "cargos": cargos,
        "cuenta": cuenta_corriente.resumen_cuenta_por_ambito(cliente),
    }


def test_reemplazados_y_cancelados_coinciden_en_embudo_historial_y_enlaces(
    estados_db,
):
    """Una baja contable no convierte una guia reemplazada en cancelada."""
    db = estados_db
    cliente = "CLIENTE_ESTADOS"
    otro_cliente = "CLIENTE_AJENO"
    _crear_cliente(db, cliente)
    _crear_cliente(db, otro_cliente)

    reemplazado = _crear_envio(
        db,
        cliente=cliente,
        referencia="REEMPLAZADO",
        estado="REEMPLAZADO",
        cargo_estado="CANCELADO",
        tracking_estado="ENTREGADO",
        monto="710.00",
    )
    cancelado = _crear_envio(
        db,
        cliente=cliente,
        referencia="CANCELADO",
        estado="CANCELADO",
        cargo_estado="CANCELADO",
        tracking_estado="ENTREGADO",
        monto="720.00",
    )
    entregado = _crear_envio(
        db,
        cliente=cliente,
        referencia="ENTREGADO",
        estado="GUIA_LISTA",
        cargo_estado="ACTIVO",
        tracking_estado="ENTREGADO",
        monto="730.00",
    )
    vigente = _crear_envio(
        db,
        cliente=cliente,
        referencia="VIGENTE",
        estado="GUIA_LISTA",
        cargo_estado="ACTIVO",
        monto="740.00",
    )

    _crear_envio(
        db,
        cliente=cliente,
        referencia="PRUEBA",
        estado="REEMPLAZADO",
        cargo_estado="CANCELADO",
        test=True,
    )
    _crear_envio(
        db,
        cliente=cliente,
        referencia="OCULTO",
        estado="CANCELADO",
        cargo_estado="CANCELADO",
        visible=False,
    )
    _crear_envio(
        db,
        cliente=otro_cliente,
        referencia="AJENO",
        estado="REEMPLAZADO",
        cargo_estado="CANCELADO",
    )

    antes = _snapshot(db, cliente)
    historial = solicitudes_guia.listar_solicitudes_cliente(cliente, limite=None)
    vista = panel_cliente.preparar_historial_envios(
        historial, tipo="internacional", por_pagina=50
    )
    embudo = panel_cliente.embudo_envios(cliente)
    despues = _snapshot(db, cliente)

    assert {fila["id"] for fila in historial} == {
        reemplazado,
        cancelado,
        entregado,
        vigente,
    }
    conteos_embudo = _conteos(embudo)
    conteos_historial = _conteos(vista["chips"])
    esperados = {
        "modificados": 1,
        "canceladas": 1,
        "entregados": 1,
        "guia_lista": 1,
    }
    for categoria, cantidad in esperados.items():
        assert conteos_embudo[categoria] == cantidad
        assert conteos_historial[categoria] == cantidad
    assert conteos_embudo == {
        **conteos_historial,
        "por_armar": conteos_embudo["por_armar"],
    }

    por_clave = {paso["clave"]: paso for paso in embudo}
    assert por_clave["modificados"]["url"] == "/portal/envios?paso=modificados"
    assert por_clave["canceladas"]["url"] == "/portal/envios?paso=canceladas"
    assert por_clave["entregados"]["url"] == "/portal/envios?paso=entregados"
    assert por_clave["guia_lista"]["url"] == "/portal/envios?paso=guia_lista"

    assert antes == despues
    assert despues["cuenta"]["consolidado"]["saldo_ars"] == Decimal("1470.00")
    assert {
        (fila["solicitud_id"], fila["estado"], fila["monto_ars"])
        for fila in despues["cargos"]
    } >= {
        (reemplazado, "CANCELADO", Decimal("710.00")),
        (cancelado, "CANCELADO", Decimal("720.00")),
        (entregado, "ACTIVO", Decimal("730.00")),
        (vigente, "ACTIVO", Decimal("740.00")),
    }


def test_cargo_cancelado_respalda_cancelacion_si_la_solicitud_sigue_activa(
    estados_db,
):
    """El respaldo historico sigue cerrando una solicitud activa incoherente."""
    db = estados_db
    cliente = "CLIENTE_RESPALDO"
    _crear_cliente(db, cliente)
    solicitud_id = _crear_envio(
        db,
        cliente=cliente,
        referencia="RESPALDO",
        estado="GUIA_LISTA",
        cargo_estado="CANCELADO",
        tracking_estado="ENTREGADO",
        monto="810.00",
    )

    antes = _snapshot(db, cliente)
    embudo = _conteos(panel_cliente.embudo_envios(cliente))
    despues = _snapshot(db, cliente)

    assert embudo["canceladas"] == 1
    assert embudo["entregados"] == 0
    assert antes == despues
    assert despues["solicitudes"][0]["id"] == solicitud_id
    assert despues["solicitudes"][0]["estado"] == "GUIA_LISTA"
    assert despues["cargos"][0]["estado"] == "CANCELADO"
    assert despues["cuenta"]["consolidado"]["saldo_ars"] == Decimal("0")
