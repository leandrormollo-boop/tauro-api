"""Control de facturas de operadores sobre PostgreSQL real.

Una factura trae guías de varios clientes; cada guía cae en una bandeja y
cada renglón termina en un solo destino. Datos inventados.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest

from servicios import conciliacion_couriers as conciliacion
from servicios import control_facturas_operadores as control
from tests.test_conciliacion_couriers_postgres import (  # noqa: F401
    DATABASE_URL,
    _confirmar_todos,
    _crear_cargo_activo,
    conciliacion_db,
)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="requiere TAURO_TEST_DATABASE_URL aislada",
)

D = Decimal


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cliente(db, cliente_id: str, *, regla: str | None = None) -> str:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO clientes (cliente_id, email, nombre)
            VALUES (%s, %s, %s)
            ON CONFLICT (cliente_id) DO NOTHING
            """,
            (cliente_id, f"{cliente_id.lower()}@example.invalid", cliente_id),
        )
        if regla:
            cur.execute(
                "UPDATE clientes SET regla_diferencia_courier=%s WHERE cliente_id=%s",
                (regla, cliente_id),
            )
    return cliente_id


def _envio(
    db, cliente_id: str, tracking: str, *, precio: str = "100000",
    costo: str | None = None, cargo: bool = True, estado: str = "DESPACHADO",
    created_at: date | None = None,
) -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, destino_pais, dest_nombre,
                dest_direccion, dest_ciudad, dest_zip, courier, tracking,
                coti_id, precio_tauro_ars, estado
            ) VALUES (%s, 'Ropa', 'US', 'Destinatario', 'Calle 1', 'Miami',
                      '33101', 'DHL', %s, %s, %s, %s)
            RETURNING id
            """,
            (cliente_id, tracking, f"COTI-{tracking}", D(precio), estado),
        )
        sid = int(cur.fetchone()["id"])
        if created_at:
            cur.execute(
                "UPDATE solicitudes_guia SET created_at=%s, updated_at=%s WHERE id=%s",
                (created_at, created_at, sid),
            )
    if cargo:
        _crear_cargo_activo(db, sid, monto=precio)
    if costo is not None:
        conciliacion.registrar_snapshot_cotizacion(
            solicitud_id=sid, coti_id=f"COTI-{tracking}", courier="DHL",
            moneda_courier="ARS", tipo_cambio_ars="1",
            costo_courier_estimado=costo, precio_cliente_inicial_ars=precio,
            margen_tauro_protegido_ars=str(D(precio) - D(costo)),
            peso_facturable_cotizado_kg="2", actor="test",
        )
    return sid


def _cancelar(db, sid: int) -> None:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cliente_id, tracking FROM solicitudes_guia WHERE id=%s", (sid,)
        )
        s = cur.fetchone()
        cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE id=%s", (sid,))
        cur.execute("UPDATE envios SET estado='CANCELADO' WHERE solicitud_id=%s", (sid,))
        cur.execute(
            """
            INSERT INTO solicitudes_guia_reemisiones (
                cliente_id, solicitud_anterior_id, operacion, tracking_anterior,
                estado, riesgo_estado
            ) VALUES (%s, %s, 'CANCELACION', %s, 'EMITIDA', 'VIGILAR')
            """,
            (s["cliente_id"], sid, s["tracking"]),
        )


def _reemplazar(db, sid_vieja: int, tracking_nueva: str, *, costo: str = "30000",
                precio: str = "60000") -> int:
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cliente_id, tracking FROM solicitudes_guia WHERE id=%s", (sid_vieja,)
        )
        s = cur.fetchone()
    nueva = _envio(db, s["cliente_id"], tracking_nueva, precio=precio, costo=costo)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='REEMPLAZADO' WHERE id=%s", (sid_vieja,))
        cur.execute("UPDATE envios SET estado='CANCELADO' WHERE solicitud_id=%s", (sid_vieja,))
        cur.execute(
            """
            INSERT INTO solicitudes_guia_reemisiones (
                cliente_id, solicitud_anterior_id, solicitud_nueva_id, operacion,
                tracking_anterior, tracking_nuevo, estado
            ) VALUES (%s, %s, %s, 'REEMPLAZO', %s, %s, 'EMITIDA')
            """,
            (s["cliente_id"], sid_vieja, nueva, s["tracking"], tracking_nueva),
        )
    return nueva


def _factura(numero: str, lineas, *, total: str, subtotal: str = "0",
             impuestos: str = "0", sha: str = "a", tipo: str = "FC",
             tc: str | None = None):
    items = []
    for n, (tracking, concepto, importe) in enumerate(lineas, start=1):
        item = {"linea_numero": n, "tracking": tracking, "concepto_tipo": concepto,
                "importe": importe}
        if tc:
            item["tipo_cambio_ars"] = tc
        items.append(item)
    return conciliacion.registrar_factura_courier(
        courier="DHL", tipo_documento=tipo, numero=numero,
        moneda="USD" if tc else "ARS", total=total, subtotal=subtotal,
        impuestos=impuestos, actor="parser@test", archivo_sha256=sha * 64,
        items=items,
    )


def _resoluciones(db, factura_id: int) -> dict[str, dict]:
    with db() as conn, conn.cursor() as cur:
        return control.listar_resoluciones_factura(cur, factura_id)


def _estado_factura(db, factura_id: int) -> str:
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT estado FROM facturas_courier WHERE id=%s", (factura_id,))
        return cur.fetchone()["estado"]


def _cuadres(db, factura_id: int) -> dict:
    with db() as conn, conn.cursor() as cur:
        return control.cuadres_factura(cur, factura_id)


def _ajuste(db, solicitud_id: int):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.tipo, a.monto_ars, a.estado, c.motivo_diferencia,
                      c.ahorro_tauro_ars, c.regla_aplicada, c.estado AS conciliacion_estado
                 FROM conciliaciones_envio c
                 LEFT JOIN ajustes_cliente a ON a.conciliacion_id=c.id
                WHERE c.solicitud_id=%s ORDER BY c.version DESC LIMIT 1""",
            (solicitud_id,),
        )
        fila = cur.fetchone()
        return dict(fila) if fila else None


# ── 1 + 2: factura de $300.000 con 5 guías de 3 clientes ─────────────────────

def test_factura_mixta_reparte_cada_renglon_en_un_destino_y_cierra(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    _cliente(db, "MELCIOR")
    a = _envio(db, "WAIMAO", "3100000001", precio="100000", costo="80000")
    b = _envio(db, "WAIMAO", "3100000002", precio="60000", costo="46000")
    melcior = _envio(db, "MELCIOR", "3100000003", precio="90000")  # sin costo inicial
    vieja = _envio(db, "WAIMAO", "3100000004", precio="60000", costo="30000")
    nueva = _reemplazar(db, vieja, "3100000009", costo="30000", precio="60000")

    fc = _factura("1700A00000300", [
        ("3100000001", "FLETE", "82000"),
        ("3100000002", "FLETE", "45000"),
        ("3100000003", "FLETE", "58000"),
        ("3100000004", "FLETE", "35000"),
        ("3100000005", "FLETE", "28000"),
        ("", "IMPUESTO", "52000"),
    ], total="300000", subtotal="248000", impuestos="52000")
    assert conciliacion.matchear_items_exactos(fc["id"]) == {"propuestos": 3, "sin_match": 3}

    res = _resoluciones(db, fc["id"])
    assert res["3100000004"]["bandeja"] == "REEMPLAZADA"
    assert res["3100000004"]["detalle"]["tracking_nuevo"] == "3100000009"
    assert res["3100000004"]["detalle"]["nueva_facturada"] is False
    assert res["3100000005"]["bandeja"] == "SIN_DUENO"
    assert set(res) == {"3100000004", "3100000005"}

    cuadres = _cuadres(db, fc["id"])
    assert cuadres["cuadre_documento_ok"] is True
    assert cuadres["suma_renglones"] == D("300000")
    assert cuadres["cargos_generales"] == D("52000")
    assert cuadres["pendiente_bandeja"] == D("63000")
    assert cuadres["cuadre_destinos_ok"] is False
    assert _estado_factura(db, fc["id"]) == "PARCIAL"

    # 2) Sin cliente en una factura mixta, falla con un mensaje claro.
    with pytest.raises(conciliacion.ConciliacionCourierError, match="varios clientes"):
        conciliacion.confirmar_y_calcular_factura(fc["id"], actor="admin@test")
    with pytest.raises(conciliacion.ConciliacionCourierError, match="no tiene guías"):
        conciliacion.confirmar_y_calcular_factura(fc["id"], actor="admin@test", cliente_id="OTRO")

    waimao = conciliacion.confirmar_y_calcular_factura(fc["id"], actor="admin@test", cliente_id="WAIMAO")
    assert waimao["matches_confirmados"] == 2 and waimao["errores"] == []
    # MELCIOR sigue propuesto: confirmar WAIMAO no confirma MELCIOR.
    clientes = {c["cliente_id"]: c for c in conciliacion.clientes_de_factura(fc["id"])}
    assert int(clientes["MELCIOR"]["propuestas"]) == 1 and int(clientes["MELCIOR"]["confirmadas"]) == 0
    assert int(clientes["WAIMAO"]["confirmadas"]) == 2

    # A: DHL cobró más → débito propuesto, nunca aplicado solo.
    aj_a = _ajuste(db, a)
    assert aj_a["tipo"] == "DEBITO" and aj_a["monto_ars"] == D("2000") and aj_a["estado"] == "PROPUESTO"
    # B: DHL cobró menos → sin crédito, ahorro de TAURO, lista para cerrar.
    aj_b = _ajuste(db, b)
    assert aj_b["tipo"] is None
    assert aj_b["motivo_diferencia"] == "AHORRO_TAURO"
    assert aj_b["ahorro_tauro_ars"] == D("1000") and aj_b["regla_aplicada"] == "COBRAR_SOLO_SI_MAYOR"
    assert aj_b["conciliacion_estado"] == "PARA_REVISION"

    # MELCIOR: sin costo inicial. Se confirma y se cierra con la acción explícita.
    melcior_res = conciliacion.confirmar_y_calcular_factura(fc["id"], actor="admin@test", cliente_id="MELCIOR")
    assert melcior_res["matches_confirmados"] == 1
    assert melcior_res["errores"] and "snapshot" in melcior_res["errores"][0]["error"]
    cierre = conciliacion.cerrar_sin_costo_inicial(melcior, actor="admin@test", motivo="Envío histórico sin costo cotizado")
    assert cierre["ok"] is True and cierre["costo_real_ars"] == D("58000")
    assert _ajuste(db, melcior)["conciliacion_estado"] == "CERRADA"

    # Bandejas: la reemplazada se traslada a la guía nueva; la sin dueño la absorbe TAURO.
    traslado = control.resolver_bandeja(
        res["3100000004"]["id"], actor="admin@test", destino="CLIENTE",
        motivo="La guía vieja fue corregida; el costo es del envío nuevo",
        identificador_envio=f"#{nueva}",
    )
    assert traslado["solicitud_id"] == nueva and len(traslado["match_ids"]) == 1
    control.resolver_bandeja(
        res["3100000005"]["id"], actor="admin@test", destino="ABSORBE_TAURO",
        motivo="No se encontró el dueño en planilla ni en chats",
    )
    # La factura no cierra mientras quede algo propuesto.
    assert _estado_factura(db, fc["id"]) == "PARCIAL"
    conciliacion.confirmar_y_calcular_factura(fc["id"], actor="admin@test", cliente_id="WAIMAO")
    aj_nueva = _ajuste(db, nueva)
    assert aj_nueva["tipo"] == "DEBITO" and aj_nueva["monto_ars"] == D("5000")

    cuadres = _cuadres(db, fc["id"])
    assert cuadres["asignado_clientes"] == D("220000")
    assert cuadres["absorbido_tauro"] == D("28000")
    assert cuadres["reclamado_operador"] == D("0")
    assert cuadres["cargos_generales"] == D("52000")
    assert cuadres["diferencia_destinos"] == D("0")
    assert cuadres["cuadre_destinos_ok"] is True
    assert _estado_factura(db, fc["id"]) == "CERRADA"

    # Nada se cobró al cliente sin aprobación: los débitos siguen propuestos.
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM ajustes_cliente WHERE estado='APLICADO'")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT COUNT(*) AS n FROM ajustes_cliente WHERE tipo='CREDITO'")
        assert cur.fetchone()["n"] == 0


# ── 3: guía cancelada facturada ──────────────────────────────────────────────

def test_guia_cancelada_facturada_queda_bloqueada_y_se_reclama(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    sid = _envio(db, "WAIMAO", "3200000001", precio="50000", costo="40000")
    _cancelar(db, sid)
    fc = _factura("1700A00000301", [("3200000001", "FLETE", "41000")], total="41000", sha="b")
    assert conciliacion.matchear_items_exactos(fc["id"]) == {"propuestos": 0, "sin_match": 1}
    res = _resoluciones(db, fc["id"])["3200000001"]
    assert res["bandeja"] == "CANCELADA" and res["estado"] == "PENDIENTE"
    assert res["detalle"]["riesgo_estado"] == "VIGILAR"
    assert res["solicitud_referencia_id"] == sid

    envios = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1)
    assert envios["totales"].get("CANCELADO_FACTURADO") == 1
    assert envios["items"][0]["control_estado"] == "CANCELADO_FACTURADO"

    # Un match manual sobre una guía bloqueada se rechaza: la decisión es la bandeja.
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM facturas_courier_items WHERE factura_id=%s", (fc["id"],))
        item_id = int(cur.fetchone()["id"])
    with pytest.raises(conciliacion.ConciliacionCourierError, match="bandeja"):
        conciliacion.proponer_match_manual(
            item_id, identificador_envio=f"#{sid}", actor="admin@test",
            motivo="Intento de vincular igual",
        )
    # 'Cobro correcto' no aplica a una cancelada.
    with pytest.raises(conciliacion.ConciliacionCourierError, match="ya facturada"):
        control.resolver_bandeja(res["id"], actor="admin@test", destino="ES_CORRECTO",
                                 motivo="No corresponde este destino")
    control.resolver_bandeja(
        res["id"], actor="admin@test", destino="RECLAMO_OPERADOR",
        motivo="Guía anulada el mismo día; sin movimiento en el tracking",
    )
    assert _resoluciones(db, fc["id"])["3200000001"]["estado"] == "RESUELTA"
    envios = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1)
    assert "CANCELADO_FACTURADO" not in envios["totales"]
    cuadres = _cuadres(db, fc["id"])
    assert cuadres["reclamado_operador"] == D("41000") and cuadres["cuadre_destinos_ok"] is True
    assert _estado_factura(db, fc["id"]) == "CERRADA"


def test_guia_cancelada_usada_se_reactiva_y_se_cobra_al_cliente(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    sid = _envio(db, "WAIMAO", "3200000011", precio="50000", costo="40000")
    _cancelar(db, sid)
    fc = _factura("1700A00000316", [("3200000011", "FLETE", "43000")], total="43000", sha="7")
    conciliacion.matchear_items_exactos(fc["id"])
    res = _resoluciones(db, fc["id"])["3200000011"]
    assert res["bandeja"] == "CANCELADA"
    # Sólo una cancelada se reactiva.
    reemplazada = _envio(db, "WAIMAO", "3200000012", precio="50000", costo="40000")
    _reemplazar(db, reemplazada, "3200000013")
    fc2 = _factura("1700A00000317", [("3200000012", "FLETE", "43000")], total="43000", sha="8")
    conciliacion.matchear_items_exactos(fc2["id"])
    with pytest.raises(conciliacion.ConciliacionCourierError, match="sólo aplica a una guía cancelada"):
        control.resolver_bandeja(_resoluciones(db, fc2["id"])["3200000012"]["id"], actor="admin@test",
                                 destino="REACTIVAR_GUIA", motivo="Intento sobre reemplazada")

    ok = control.resolver_bandeja(
        res["id"], actor="admin@test", destino="REACTIVAR_GUIA",
        motivo="El tracking muestra entrega: el cliente usó la etiqueta igual",
    )
    assert ok["solicitud_id"] == sid and ok["cliente_id"] == "WAIMAO" and len(ok["match_ids"]) == 1
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT estado, visible_cliente FROM solicitudes_guia WHERE id=%s", (sid,))
        s = cur.fetchone()
        assert s["estado"] == "ENTREGADO" and s["visible_cliente"] is True
        cur.execute("SELECT estado, monto_ars FROM envios WHERE solicitud_id=%s", (sid,))
        e = cur.fetchone()
        assert e["estado"] == "ACTIVO" and e["monto_ars"] == D("50000")
        cur.execute("SELECT riesgo_estado, riesgo_resuelto_nota FROM solicitudes_guia_reemisiones WHERE solicitud_anterior_id=%s", (sid,))
        r = cur.fetchone()
        assert r["riesgo_estado"] == "CERRADA" and "facturada" in r["riesgo_resuelto_nota"]
        cur.execute("SELECT COUNT(*) AS n FROM auditoria_facturas_courier WHERE evento='GUIA_CANCELADA_REACTIVADA' AND solicitud_id=%s", (sid,))
        assert cur.fetchone()["n"] == 1
    # Ya no es alerta: sigue el circuito normal (vinculación por confirmar).
    envios = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1)
    estados = {e["solicitud_id"]: e["control_estado"] for e in envios["items"]}
    assert estados[sid] == "MATCH_PENDIENTE"
    _confirmar_todos(db, sid)
    calculo = conciliacion.calcular_conciliacion_envio(sid, actor="admin@test")
    assert calculo["ajuste_cliente_ars"] == D("3000")
    assert _ajuste(db, sid)["tipo"] == "DEBITO"
    # Idempotencia: la bandeja ya está resuelta.
    with pytest.raises(conciliacion.ConciliacionCourierError, match="ya fue resuelta"):
        control.resolver_bandeja(res["id"], actor="admin@test", destino="RECLAMO_OPERADOR", motivo="Segundo intento")


# ── 4: guía reemplazada, con la nueva facturada o no ─────────────────────────

def test_guia_reemplazada_traslada_el_costo_solo_si_la_nueva_no_fue_facturada(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    _cliente(db, "OTRO")
    vieja = _envio(db, "WAIMAO", "3300000001", precio="60000", costo="30000")
    nueva = _reemplazar(db, vieja, "3300000002")
    ajeno = _envio(db, "OTRO", "3300000003", precio="60000", costo="30000")

    fc_vieja = _factura("1700A00000302", [("3300000001", "FLETE", "35000")], total="35000", sha="c")
    conciliacion.matchear_items_exactos(fc_vieja["id"])
    res = _resoluciones(db, fc_vieja["id"])["3300000001"]
    assert res["bandeja"] == "REEMPLAZADA"
    envios = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1)
    assert envios["totales"].get("REEMPLAZADO_FACTURADO") == 1

    # El costo no puede ir a un envío de otro cliente.
    with pytest.raises(conciliacion.ConciliacionCourierError, match="era de WAIMAO"):
        control.resolver_bandeja(res["id"], actor="admin@test", destino="CLIENTE",
                                 motivo="Traslado equivocado", identificador_envio=f"#{ajeno}")
    # Traslado a la guía nueva: funciona porque la nueva no fue facturada.
    traslado = control.resolver_bandeja(
        res["id"], actor="admin@test", destino="CLIENTE",
        motivo="Guía corregida; el operador facturó la vieja", identificador_envio="3300000002",
    )
    assert traslado["solicitud_id"] == nueva

    # Si la nueva también fue facturada, el traslado se rechaza.
    vieja2 = _envio(db, "WAIMAO", "3300000011", precio="60000", costo="30000")
    nueva2 = _reemplazar(db, vieja2, "3300000012")
    fc_nueva2 = _factura("1700A00000303", [("3300000012", "FLETE", "33000")], total="33000", sha="d")
    assert conciliacion.matchear_items_exactos(fc_nueva2["id"])["propuestos"] == 1
    fc_vieja2 = _factura("1700A00000304", [("3300000011", "FLETE", "35000")], total="35000", sha="e")
    conciliacion.matchear_items_exactos(fc_vieja2["id"])
    res2 = _resoluciones(db, fc_vieja2["id"])["3300000011"]
    assert res2["detalle"]["nueva_facturada"] is True
    with pytest.raises(conciliacion.ConciliacionCourierError, match="ya tiene su flete"):
        control.resolver_bandeja(res2["id"], actor="admin@test", destino="CLIENTE",
                                 motivo="Intento de trasladar igual", identificador_envio=f"#{nueva2}")
    control.resolver_bandeja(res2["id"], actor="admin@test", destino="RECLAMO_OPERADOR",
                             motivo="El operador cobró las dos guías del mismo envío")
    assert _resoluciones(db, fc_vieja2["id"])["3300000011"]["destino"] == "RECLAMO_OPERADOR"


# ── 5: ya facturado: segundo flete bloqueado, ajuste posterior pasa ──────────

def test_segundo_flete_se_bloquea_y_un_recargo_posterior_pasa_como_ajuste(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    sid = _envio(db, "WAIMAO", "3400000001", precio="50000", costo="30000")
    fc1 = _factura("1700A00000305", [("3400000001", "FLETE", "31000")], total="31000", sha="f")
    assert conciliacion.matchear_items_exactos(fc1["id"])["propuestos"] == 1

    # Un ajuste de peso posterior (sin FLETE) se vincula como siempre.
    nd = _factura("1700A00000306", [("3400000001", "COMBUSTIBLE", "2000")], total="2000", sha="1", tipo="ND")
    assert conciliacion.matchear_items_exactos(nd["id"]) == {"propuestos": 1, "sin_match": 0}
    assert _resoluciones(db, nd["id"]) == {}

    # El mismo FLETE otra vez: posible doble cobro.
    fc2 = _factura("1700A00000307", [("3400000001", "FLETE", "31000")], total="31000", sha="2")
    assert conciliacion.matchear_items_exactos(fc2["id"]) == {"propuestos": 0, "sin_match": 1}
    res = _resoluciones(db, fc2["id"])["3400000001"]
    assert res["bandeja"] == "YA_FACTURADO"
    assert res["detalle"]["factura_anterior_id"] == fc1["id"]
    # Una nota de crédito nunca es doble cobro.
    nc = _factura("1700A00000308", [("3400000001", "FLETE", "500")], total="500", sha="3", tipo="NC")
    assert conciliacion.matchear_items_exactos(nc["id"])["propuestos"] == 1

    # Se acepta como cobro correcto: el costo se suma al envío y queda propuesto.
    ok = control.resolver_bandeja(res["id"], actor="admin@test", destino="ES_CORRECTO",
                                  motivo="Re-pesaje confirmado por DHL con nueva guía de flete")
    assert ok["solicitud_id"] == sid and len(ok["match_ids"]) == 1
    confirmados = _confirmar_todos(db, sid)
    assert len(confirmados) == 4
    calculo = conciliacion.calcular_conciliacion_envio(sid, actor="admin@test")
    assert calculo["costo_courier_real_ars"] == D("63500")


# ── 6: factura que no cuadra ─────────────────────────────────────────────────

def test_factura_que_no_cuadra_queda_observada_y_no_cierra(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    sid = _envio(db, "WAIMAO", "3500000001", precio="50000", costo="30000")
    # subtotal + impuestos ≠ total: los renglones suman el total, el encabezado no.
    fc = _factura("1700A00000309", [("3500000001", "FLETE", "31000")], total="31000",
                  subtotal="30000", impuestos="500", sha="4")
    conciliacion.matchear_items_exactos(fc["id"])
    cuadres = _cuadres(db, fc["id"])
    assert cuadres["cuadre_documento_ok"] is False
    _confirmar_todos(db, sid)
    assert _estado_factura(db, fc["id"]) == "OBSERVADA"
    conciliacion.calcular_conciliacion_envio(sid, actor="admin@test")
    assert _estado_factura(db, fc["id"]) == "OBSERVADA"


# ── 7: posibles duplicadas ───────────────────────────────────────────────────

def test_detecta_posibles_duplicadas_por_tipo_ceros_pdf_y_control_historico(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    _envio(db, "WAIMAO", "3600000001", precio="50000", costo="30000")
    base = _factura("0700A00918787", [("3600000001", "FLETE", "31000")], total="31000", sha="5")
    otro_tipo = _factura("0700A00918787", [("3600000001", "FLETE", "31000")], total="31000", sha="6", tipo="ND")
    sin_ceros = _factura("700A00918787", [("3600000001", "FLETE", "31000")], total="31000", sha="7")
    control_hist = _factura("CONTROL-0700A00918787-3600000001", [("3600000001", "FLETE", "31000")],
                            total="31000", sha="8")
    ajena = _factura("1700A00099999", [("3600000001", "FLETE", "999")], total="999", sha="9")
    with db() as conn, conn.cursor() as cur:
        dup = {d["id"]: set(d["motivos"]) for d in control.posibles_duplicadas(cur, base["id"])}
    assert set(dup) == {otro_tipo["id"], sin_ceros["id"], control_hist["id"]}
    assert "MISMO_NUMERO_OTRO_TIPO" in dup[otro_tipo["id"]]
    assert "MISMO_NUMERO_SIN_CEROS" in dup[sin_ceros["id"]]
    assert "CONTROL_HISTORICO_MISMA_FC" in dup[control_hist["id"]]
    assert "MISMAS_GUIAS_E_IMPORTES" in dup[otro_tipo["id"]]
    assert ajena["id"] not in dup
    # El mismo PDF con otro número lo frena el registro.
    with pytest.raises(conciliacion.DocumentoCourierDuplicadoError):
        _factura("1700A00000399", [("3600000001", "FLETE", "31000")], total="31000", sha="5")
    detalle = conciliacion.obtener_factura_courier_control(base["id"])
    assert {d["id"] for d in detalle["posibles_duplicadas"]} == set(dup)


# ── 8: COBRAR_SIEMPRE conserva el crédito ────────────────────────────────────

def test_cobrar_siempre_conserva_el_credito_al_cliente(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "PRETE", regla="COBRAR_SIEMPRE")
    sid = _envio(db, "PRETE", "3700000001", precio="60000", costo="46000")
    fc = _factura("1700A00000310", [("3700000001", "FLETE", "45000")], total="45000", sha="a")
    conciliacion.matchear_items_exactos(fc["id"])
    _confirmar_todos(db, sid)
    calculo = conciliacion.calcular_conciliacion_envio(sid, actor="admin@test")
    assert calculo["regla_aplicada"] == "COBRAR_SIEMPRE"
    assert calculo["ahorro_tauro_ars"] == D("0")
    aj = _ajuste(db, sid)
    assert aj["tipo"] == "CREDITO" and aj["monto_ars"] == D("-1000")


def test_tax_se_traslada_aunque_el_flete_haya_sido_mas_barato(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    sid = _envio(db, "WAIMAO", "3700000002", precio="60000", costo="46000")
    fc = _factura("1700A00000311", [
        ("3700000002", "FLETE", "45000"),
        ("3700000002", "IMPUESTO", "10000"),
    ], total="55000", sha="b")
    conciliacion.matchear_items_exactos(fc["id"])
    _confirmar_todos(db, sid)
    calculo = conciliacion.calcular_conciliacion_envio(sid, actor="admin@test")
    assert calculo["ahorro_tauro_ars"] == D("1000")
    assert calculo["diferencia_flete_ars"] == D("0")
    assert calculo["tax_cliente_ars"] == D("10000")
    assert calculo["ajuste_cliente_ars"] == D("10000")
    aj = _ajuste(db, sid)
    assert aj["tipo"] == "DEBITO" and aj["monto_ars"] == D("10000") and aj["motivo_diferencia"] == "IMPUESTOS"


def test_cierre_sin_costo_inicial_exige_costo_menor_al_precio_y_sin_tax(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "MELCIOR")
    caro = _envio(db, "MELCIOR", "3800000001", precio="50000")
    fc = _factura("1700A00000312", [("3800000001", "FLETE", "52000")], total="52000", sha="c")
    conciliacion.matchear_items_exactos(fc["id"])
    with pytest.raises(conciliacion.ConciliacionCourierError, match="Confirmá primero"):
        conciliacion.cerrar_sin_costo_inicial(caro, actor="admin@test", motivo="Cierre apurado")
    _confirmar_todos(db, caro)
    with pytest.raises(conciliacion.ConciliacionCourierError, match="supera el precio"):
        conciliacion.cerrar_sin_costo_inicial(caro, actor="admin@test", motivo="Cierre sin diferencia")
    con_tax = _envio(db, "MELCIOR", "3800000002", precio="50000")
    fc2 = _factura("1700A00000313", [("3800000002", "FLETE", "30000"), ("3800000002", "IMPUESTO", "5000")],
                   total="35000", sha="d")
    conciliacion.matchear_items_exactos(fc2["id"])
    _confirmar_todos(db, con_tax)
    with pytest.raises(conciliacion.ConciliacionCourierError, match="TAX"):
        conciliacion.cerrar_sin_costo_inicial(con_tax, actor="admin@test", motivo="Cierre sin diferencia")


# ── 9: concurrencia ──────────────────────────────────────────────────────────

def test_dos_resoluciones_simultaneas_de_la_misma_guia_gana_una(conciliacion_db):
    db = conciliacion_db
    fc = _factura("1700A00000314", [("3900000001", "FLETE", "10000")], total="10000", sha="e")
    conciliacion.matchear_items_exactos(fc["id"])
    res = _resoluciones(db, fc["id"])["3900000001"]
    resultados: list = []
    barrera = threading.Barrier(2)

    def intentar(destino):
        barrera.wait()
        try:
            control.resolver_bandeja(res["id"], actor=f"admin-{destino}", destino=destino,
                                     motivo="Decisión simultánea de prueba")
            resultados.append(("ok", destino))
        except conciliacion.ConciliacionCourierError as exc:
            resultados.append(("error", str(exc)))

    hilos = [threading.Thread(target=intentar, args=(d,)) for d in ("RECLAMO_OPERADOR", "ABSORBE_TAURO")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    assert sorted(r[0] for r in resultados) == ["error", "ok"]
    assert "resuelta por otra persona" in next(r[1] for r in resultados if r[0] == "error")
    final = _resoluciones(db, fc["id"])["3900000001"]
    assert final["estado"] == "RESUELTA"
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM auditoria_facturas_courier WHERE evento='BANDEJA_RESUELTA'")
        assert cur.fetchone()["n"] == 1


# ── 10: el cliente no ve costos, márgenes ni bandejas ────────────────────────

def test_portal_del_cliente_no_expone_costo_margen_ni_bandejas():
    from pathlib import Path
    raiz = Path(__file__).resolve().parents[1]
    portal = (raiz / "endpoints" / "portal_cliente.py").read_text(encoding="utf-8")
    assert "control_facturas_operadores" not in portal
    assert "factura_courier_guia_resoluciones" not in portal
    assert "ahorro_tauro" not in portal
    templates_portal = list((raiz / "templates" / "portal").rglob("*.html"))
    assert templates_portal
    for plantilla in templates_portal:
        texto = plantilla.read_text(encoding="utf-8")
        assert "bandeja" not in texto.lower(), plantilla
        assert "costo_courier_real" not in texto, plantilla
        assert "margen_tauro" not in texto, plantilla
        assert "ahorro_tauro" not in texto, plantilla


# ── 11: envío vigente sin factura a los 30 días ──────────────────────────────

def test_envio_vigente_sin_factura_alerta_al_dia_31(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    hoy = date(2026, 10, 7)
    viejo = _envio(db, "WAIMAO", "4000000001", precio="50000", costo="30000",
                   created_at=hoy - timedelta(days=31))
    justo = _envio(db, "WAIMAO", "4000000002", precio="50000", costo="30000",
                   created_at=hoy - timedelta(days=30))
    sin_base = _envio(db, "WAIMAO", "4000000003", precio="50000",
                      created_at=hoy - timedelta(days=45))
    cancelado = _envio(db, "WAIMAO", "4000000004", precio="50000", costo="30000",
                       created_at=hoy - timedelta(days=90))
    _cancelar(db, cancelado)

    paginado = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1, hoy=hoy)
    estados = {e["solicitud_id"]: e["control_estado"] for e in paginado["items"]}
    assert estados[viejo] == "SIN_FACTURA_30D"
    assert estados[justo] == "ESPERANDO_FACTURA"
    assert estados[sin_base] == "SIN_FACTURA_30D"
    assert estados[cancelado] == "SIN_CARGO"
    assert paginado["totales"]["SIN_FACTURA_30D"] == 2

    plano = conciliacion.listar_control_envios(cliente="WAIMAO", hoy=hoy)
    assert {e["solicitud_id"]: e["control_estado"] for e in plano["items"]} == estados

    # En cuanto llega la factura, deja de ser alerta.
    fc = _factura("1700A00000315", [("4000000001", "FLETE", "31000")], total="31000", sha="f")
    conciliacion.matchear_items_exactos(fc["id"])
    despues = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1, hoy=hoy)
    assert {e["solicitud_id"]: e["control_estado"] for e in despues["items"]}[viejo] == "MATCH_PENDIENTE"


# ── Peso inicial vs facturado ────────────────────────────────────────────────

def test_peso_inicial_es_el_mayor_entre_real_y_volumetrico_en_caminos_manuales(conciliacion_db):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    # Caja liviana y grande: 2 kg reales, 60×40×30 → 14,4 kg volumétricos.
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, destino_pais, dest_nombre, dest_direccion,
                dest_ciudad, dest_zip, courier, tracking, coti_id, precio_tauro_ars,
                estado, peso_kg, largo_cm, ancho_cm, alto_cm
            ) VALUES ('WAIMAO','Ropa','US','Dest','Calle 1','Miami','33101','DHL',
                      '4100000001','COTI-VOL',100000,'DESPACHADO',2,60,40,30)
            RETURNING id
            """
        )
        sid = int(cur.fetchone()["id"])
    _crear_cargo_activo(db, sid, monto="100000")
    pesos = conciliacion.pesos_iniciales_guia({"peso_kg": 2, "largo_cm": 60, "ancho_cm": 40, "alto_cm": 30, "courier": "DHL"})
    assert pesos["real"] == D("2.00") and pesos["volumetrico"] == D("14.40") and pesos["facturable"] == D("14.40")
    # Base histórica manual: congela 14,4 kg como peso inicial, no 2 kg.
    conciliacion.registrar_snapshot_manual_ars(sid, costo_estimado_ars="60000", actor="admin@test")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT peso_real_cotizado_kg, peso_volumetrico_cotizado_kg, peso_facturable_cotizado_kg FROM envio_cotizacion_snapshots WHERE solicitud_id=%s", (sid,))
        snap = cur.fetchone()
    assert snap["peso_real_cotizado_kg"] == D("2.000")
    assert snap["peso_volumetrico_cotizado_kg"] == D("14.400")
    assert snap["peso_facturable_cotizado_kg"] == D("14.400")

    # La factura trae 16 kg: la ficha compara 14,4 → 16 y marca el excedente.
    fc = conciliacion.registrar_factura_courier(
        courier="DHL", tipo_documento="FC", numero="1700A00000318", moneda="ARS",
        total="70000", actor="parser@test", archivo_sha256="9" * 64,
        items=[{"linea_numero": 1, "tracking": "4100000001", "concepto_tipo": "FLETE",
                "importe": "70000", "peso_real_kg": "2", "peso_volumetrico_kg": "16",
                "peso_facturado_kg": "16", "peso_base": "VOLUMETRICO"}],
    )
    conciliacion.matchear_items_exactos(fc["id"])
    detalle = conciliacion.obtener_factura_courier_control(fc["id"])
    pi = detalle["envios_facturados"][0]["peso_inicial"]
    assert pi["facturable_kg"] == D("14.40") and pi["cobra_por_volumen"] is True
    assert pi["facturado_kg"] == D("16.00") and pi["excedente_kg"] == D("1.60") and pi["facturado_mayor"] is True
    control_envios = conciliacion.listar_control_envios(cliente="WAIMAO", pagina=1)
    fila = control_envios["items"][0]
    assert fila["peso_facturable_cotizado_kg"] == D("14.400")

    # Sin medidas, el peso inicial es el declarado y nunca menor.
    assert conciliacion.pesos_iniciales_guia({"peso_kg": 3, "courier": "DHL"}) == {
        "real": D("3.00"), "volumetrico": None, "facturable": D("3.00"),
    }
    # Caja pesada y chica: manda el real.
    assert conciliacion.pesos_iniciales_guia({"peso_kg": 20, "largo_cm": 20, "ancho_cm": 20, "alto_cm": 20, "courier": "DHL"})["facturable"] == D("20.00")


def test_cargar_envio_externo_con_medidas_congela_el_peso_volumetrico(conciliacion_db, monkeypatch):
    db = conciliacion_db
    _cliente(db, "WAIMAO")
    from servicios import solicitudes_guia as sg
    monkeypatch.setattr(sg, "get_conn", db)
    monkeypatch.setattr(sg, "dolar_ars", lambda: 1500.0, raising=False)
    import servicios.cotizador as cotizador
    monkeypatch.setattr(cotizador, "dolar_ars", lambda: 1500.0, raising=False)
    resultado = sg.cargar_envio_externo(
        cliente_id="WAIMAO", dest_nombre="Dest", dest_ciudad="Miami", destino_pais="US",
        producto="Ropa", cantidad=1, peso_kg=2, tracking="4200000001", precio_tauro_ars=100000,
        courier="DHL", costo_courier_estimado_ars=60000, largo_cm=60, ancho_cm=40, alto_cm=30,
    )
    assert resultado.get("ok"), resultado
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT peso_real_cotizado_kg, peso_volumetrico_cotizado_kg, peso_facturable_cotizado_kg FROM envio_cotizacion_snapshots WHERE solicitud_id=%s", (resultado["solicitud_id"],))
        snap = cur.fetchone()
    assert snap is not None
    assert snap["peso_facturable_cotizado_kg"] == D("14.400")
    assert snap["peso_volumetrico_cotizado_kg"] == D("14.400")
    assert snap["peso_real_cotizado_kg"] == D("2.000")
