"""Límites financieros del control de envíos ADMIN sobre PostgreSQL real."""

from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
import psycopg2

from servicios import ajustes_precio_admin
from servicios import admin_negocio
from servicios import control_envios_admin as control
from servicios import control_negocio
from servicios import cuenta_corriente
from servicios import experiencia_cuenta
from servicios import periodo_cuenta
from test_conciliacion_couriers_postgres import (
    DATABASE_URL,
    _crear_cargo_activo,
    _crear_solicitud,
    _snapshot_basico,
    conciliacion_db,
)


pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="requiere PostgreSQL aislado"
)


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    for modulo in (
        ajustes_precio_admin, admin_negocio, control, cuenta_corriente,
        experiencia_cuenta, periodo_cuenta,
    ):
        monkeypatch.setattr(modulo, "get_conn", conciliacion_db)
    return conciliacion_db


def _envio(db, sufijo: str, *, monto="1000", estado="GUIA_LISTA"):
    sid = _crear_solicitud(db, sufijo=sufijo, precio=Decimal(monto))
    _crear_cargo_activo(db, sid, monto=monto)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET estado=%s WHERE id=%s",
            (estado, sid),
        )
        cur.execute("SELECT id FROM envios WHERE solicitud_id=%s", (sid,))
        eid = int(cur.fetchone()["id"])
    return sid, eid, f"CLIENTE_{sufijo}"


def _otro_envio_mismo_cliente(db, *, cliente: str, sufijo: str, monto="1000"):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO solicitudes_guia (
                cliente_id, producto_alias, destino_pais, dest_nombre,
                dest_direccion, dest_ciudad, dest_zip, courier, tracking,
                estado, precio_tauro_ars
            ) VALUES (%s, 'Producto', 'US', 'Destino', 'Calle 2', 'Miami',
                      '33101', 'DHL', %s, 'GUIA_LISTA', %s)
            RETURNING id
            """,
            (cliente, f"TRACK-{sufijo}", Decimal(monto)),
        )
        sid = int(cur.fetchone()["id"])
    _crear_cargo_activo(db, sid, monto=monto)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM envios WHERE solicitud_id=%s", (sid,))
        return sid, int(cur.fetchone()["id"])


def _ajustar(*, cliente, envio, nuevo, clave, esperado=None, motivo="Acuerdo comercial documentado"):
    return ajustes_precio_admin.aplicar_nuevo_precio(
        cliente_id=cliente,
        envio_id=envio,
        nuevo_precio_ars=nuevo,
        motivo=motivo,
        actor="admin@test",
        idempotency_key=clave,
        precio_esperado_ars=esperado,
    )


def _factura_cliente(db, *, cliente, tipo, numero, monto, envio_id=None, ajuste_id=None):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO facturas_cliente(
                cliente_id,tipo,punto_venta,numero,fecha_emision,
                subtotal,total,pdf,created_by
            ) VALUES(%s,%s,1,%s,CURRENT_DATE,%s,%s,%s,'qa')
            RETURNING id
            """,
            (cliente, tipo, numero, Decimal(monto), Decimal(monto), b"%PDF-test"),
        )
        factura_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO facturas_cliente_items(
                factura_id,envio_id,ajuste_id,descripcion,monto
            ) VALUES(%s,%s,%s,%s,%s)
            """,
            (
                factura_id, envio_id, ajuste_id,
                "Envío" if envio_id is not None else "Ajuste",
                Decimal(monto),
            ),
        )
    return factura_id


def test_precio_es_comercial_inmutable_idempotente_y_con_stale_guard(db):
    sid, eid, cliente = _envio(db, "PRECIO")
    _snapshot_basico(
        sid, costo="600", precio="1000", margen="400", coti_id="COTI-PRECIO"
    )
    primero = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="750",
        esperado="1000",
        clave="precio_admin_control_000000000001",
    )
    assert primero["tipo"] == "CREDITO"
    assert primero["precio_nuevo_ars"] == Decimal("750.00")
    repetido = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="750",
        esperado="1000",
        clave="precio_admin_control_000000000001",
    )
    assert repetido["duplicado"] is True

    with pytest.raises(
        ajustes_precio_admin.AjustePrecioAdminError, match="cambió desde"
    ):
        _ajustar(
            cliente=cliente,
            envio=eid,
            nuevo="800",
            esperado="1000",
            clave="precio_admin_control_000000000002",
        )
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT monto_ars FROM envios WHERE id=%s", (eid,))
        assert cur.fetchone()["monto_ars"] == Decimal("1000.00")
        cur.execute(
            "SELECT costo_courier_estimado_ars,margen_tauro_protegido_ars FROM envio_cotizacion_snapshots WHERE solicitud_id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {
            "costo_courier_estimado_ars": Decimal("600.0000"),
            "margen_tauro_protegido_ars": Decimal("400.0000"),
        }
        cur.execute(
            "SELECT COUNT(*) AS n, SUM(monto_ars) AS total FROM ajustes_cliente WHERE solicitud_id=%s",
            (sid,),
        )
        assert cur.fetchone() == {"n": 1, "total": Decimal("-250.0000")}
    assert cuenta_corriente.get_facturado_real(cliente) == 750.0


def test_clave_idempotente_no_se_puede_reusar_en_otro_envio_del_cliente(db):
    _sid, eid, cliente = _envio(db, "CLAVE")
    _otro_sid, otro_eid = _otro_envio_mismo_cliente(
        db, cliente=cliente, sufijo="CLAVE-2"
    )
    clave = "precio_admin_control_000000000003"
    _ajustar(cliente=cliente, envio=eid, nuevo="800", clave=clave)
    with pytest.raises(
        ajustes_precio_admin.AjustePrecioAdminError,
        match="usada con otros datos",
    ):
        _ajustar(cliente=cliente, envio=otro_eid, nuevo="800", clave=clave)


def test_dos_cambios_concurrentes_no_partan_del_mismo_precio(db):
    sid, eid, cliente = _envio(db, "CONCURRENTE")
    barrera = threading.Barrier(2)

    def aplicar(nuevo, clave):
        barrera.wait()
        try:
            return _ajustar(
                cliente=cliente,
                envio=eid,
                nuevo=nuevo,
                esperado="1000",
                clave=clave,
            )
        except ajustes_precio_admin.AjustePrecioAdminError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(
            lambda args: aplicar(*args),
            [
                ("800", "precio_admin_control_concurrente_0001"),
                ("900", "precio_admin_control_concurrente_0002"),
            ],
        ))
    assert sum(isinstance(r, dict) for r in resultados) == 1
    assert sum(isinstance(r, ajustes_precio_admin.AjustePrecioAdminError) for r in resultados) == 1
    assert "cambió desde" in str(next(r for r in resultados if not isinstance(r, dict)))
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM ajustes_cliente WHERE solicitud_id=%s",
            (sid,),
        )
        assert cur.fetchone()["n"] == 1


def test_precio_cero_es_bonificacion_total_sin_cancelar_estado(db):
    sid, eid, cliente = _envio(db, "PRECIO_CERO")
    resultado = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_000000000006",
        motivo="Bonificación comercial total documentada",
    )
    assert resultado["tipo"] == "CREDITO"
    assert resultado["precio_nuevo_ars"] == 0
    assert cuenta_corriente.get_facturado_real(cliente) == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.estado,e.estado AS cargo FROM solicitudes_guia s JOIN envios e ON e.solicitud_id=s.id WHERE s.id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {"estado": "GUIA_LISTA", "cargo": "ACTIVO"}


def test_cancelacion_comercial_con_ajustes_deja_cero_y_preserva_historia(db):
    sid, eid, cliente = _envio(db, "CANCELAR")
    _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="750",
        esperado="1000",
        clave="precio_admin_control_000000000004",
    )
    contexto = control.obtener_control_envio_admin(
        solicitud_id=sid, envio_id=eid, cliente_id=cliente
    )
    assert contexto["precio"]["habilitada"] is True
    assert contexto["precio"]["original"] == Decimal("1000.00")
    assert contexto["precio"]["vigente"] == Decimal("750.00")
    assert contexto["precio"]["ajustes"] == Decimal("-250.00")
    assert contexto["cancelacion"]["habilitada"] is True
    resultado = control.cancelar_envio_admin(
        solicitud_id=sid,
        envio_id=eid,
        cliente_id=cliente,
        motivo="Bonificación total aprobada por gerencia",
        actor="admin@test",
        revision=contexto["revision"],
    )
    assert resultado["estado"] == "CANCELADO"
    assert resultado["monto_anulado_ars"] == Decimal("750.00")
    assert resultado["courier_cancelado"] is False
    assert cuenta_corriente.get_facturado_real(cliente) == 0
    assert next(
        item for item in cuenta_corriente.get_resumen_clientes_bulk(False)
        if item["cliente_id"] == cliente
    )["facturado"] == 0
    resumen = cuenta_corriente.resumen_cuenta_por_ambito(cliente)
    assert resumen["consolidado"]["debe_ars"] == 0
    assert resumen["consolidado"]["diferencias_debito_ars"] == 0
    assert resumen["consolidado"]["diferencias_credito_ars"] == 0
    mensual = cuenta_corriente.resumen_mensual_cuenta(
        cliente, date.today().strftime("%Y-%m")
    )
    assert mensual["total_mes_ars"] == 0
    assert mensual["ajustes_ars"] == 0
    assert cuenta_corriente.movimientos_cuenta_paginados(
        cliente, tipo="todos"
    )["total_resultados"] == 0
    assert cuenta_corriente.movimientos_cuenta_paginados(
        cliente, tipo="cancelados"
    )["total_resultados"] == 1
    periodo = periodo_cuenta.obtener_periodo_cuenta(cliente, date(2026, 1, 1))
    assert periodo["saldo_total_ars"] == 0
    experiencia = experiencia_cuenta.obtener_experiencia_cuenta(cliente, resumen)
    assert experiencia["cupo"]["deuda_ars"] == 0
    assert experiencia["costos"]["total_ars"] == 0
    gerencial = control_negocio.obtener_control_negocio(
        date.today().replace(day=1), date.today(), conexion=db
    )
    cuenta = next(c for c in gerencial["cuentas"] if c["cliente_id"] == cliente)
    assert cuenta["cargos"] == 0
    assert cuenta["saldo"] == 0
    cancelado = admin_negocio.listar_envios(
        cliente_id=cliente, estado="cancelados"
    )["items"][0]
    assert cancelado["cobro"]["importe"] == 0

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.estado, s.cancelacion_comercial, e.estado AS cargo_estado,
                   e.monto_ars
              FROM solicitudes_guia s JOIN envios e ON e.solicitud_id=s.id
             WHERE s.id=%s
            """,
            (sid,),
        )
        assert dict(cur.fetchone()) == {
            "estado": "CANCELADO",
            "cancelacion_comercial": True,
            "cargo_estado": "CANCELADO",
            "monto_ars": Decimal("1000.00"),
        }
        cur.execute(
            "SELECT estado, monto_ars FROM ajustes_cliente WHERE solicitud_id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {
            "estado": "APLICADO",
            "monto_ars": Decimal("-250.0000"),
        }
        cur.execute(
            "SELECT metadata FROM security_audit WHERE event='admin.envio_cancelacion_comercial'"
        )
        assert cur.fetchone()["metadata"]["courier_cancelado"] is False


def test_cancela_solicitud_sin_emitir_y_sin_inventar_cargo(db):
    sid = _crear_solicitud(db, sufijo="SIN_CARGO", precio=Decimal("500"))
    cliente = "CLIENTE_SIN_CARGO"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE solicitudes_guia
               SET estado='SOLICITADO', tracking=NULL, guia_url=NULL,
                   label_pdf=NULL, guia_generada_at=NULL, cargo_pendiente=FALSE
             WHERE id=%s
            """,
            (sid,),
        )
    contexto = control.obtener_control_envio_admin(
        solicitud_id=sid, cliente_id=cliente
    )
    assert contexto["cargo"] is None
    assert contexto["cancelacion"]["modo"] == "SOLICITUD_SIN_CARGO"
    control.cancelar_envio_admin(
        solicitud_id=sid,
        cliente_id=cliente,
        motivo="Solicitud duplicada confirmada por administración",
        actor="admin@test",
        revision=contexto["revision"],
    )
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT estado, cancelacion_comercial FROM solicitudes_guia WHERE id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {
            "estado": "CANCELADO",
            "cancelacion_comercial": True,
        }
        cur.execute("SELECT COUNT(*) AS n FROM envios WHERE solicitud_id=%s", (sid,))
        assert cur.fetchone()["n"] == 0


def test_cancela_cargo_historico_sin_solicitud_y_no_ofrece_cambiar_precio(db):
    _sid, _eid, cliente = _envio(db, "HISTORICO_BASE")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO envios(
                cliente_id,fecha,monto_ars,estado,descripcion,tracking,ambito
            ) VALUES(%s,CURRENT_DATE,300,'ACTIVO','Cargo histórico','HIST-1','NACIONAL')
            RETURNING id
            """,
            (cliente,),
        )
        historico_id = int(cur.fetchone()["id"])
    contexto = control.obtener_control_envio_admin(
        envio_id=historico_id, cliente_id=cliente
    )
    assert contexto["solicitud"] is None
    assert contexto["precio"]["habilitada"] is False
    assert contexto["cancelacion"]["habilitada"] is True
    control.cancelar_envio_admin(
        envio_id=historico_id,
        cliente_id=cliente,
        motivo="Cargo histórico duplicado en la cuenta",
        actor="admin@test",
        revision=contexto["revision"],
    )
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT estado,monto_ars FROM envios WHERE id=%s", (historico_id,))
        assert dict(cur.fetchone()) == {
            "estado": "CANCELADO", "monto_ars": Decimal("300.00")
        }


def test_bloquea_factura_pago_recoleccion_y_emision_en_curso(db):
    sid_fc, eid_fc, cliente_fc = _envio(db, "FACTURADO")
    _factura_cliente(
        db, cliente=cliente_fc, tipo="FC", numero=99, monto="1000",
        envio_id=eid_fc,
    )
    fc = control.obtener_control_envio_admin(envio_id=eid_fc)
    assert fc["cancelacion"]["codigo"] == "REQUIERE_NOTA_CREDITO"

    sid_pago, eid_pago, cliente_pago = _envio(db, "PAGADO")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pagos(cliente_id,fecha,monto_ars,metodo,estado)
            VALUES(%s,CURRENT_DATE,500,'TRANSFERENCIA','APROBADO') RETURNING id
            """,
            (cliente_pago,),
        )
        pago_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,envio_id
            ) VALUES(%s,'INTERNACIONAL',500,'APLICADA',%s)
            """,
            (pago_id, eid_pago),
        )
    pagado = control.obtener_control_envio_admin(envio_id=eid_pago)
    assert pagado["cancelacion"]["codigo"] == "PAGO_IMPUTADO"

    sid_pickup, _eid_pickup, cliente_pickup = _envio(db, "RETIRO")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recolecciones(
                cliente_id,solicitud_id,fecha,ready_time,close_time,bultos,
                peso_kg,courier,estado,confirmation_code
            ) VALUES(%s,%s,CURRENT_DATE,'09:00','17:00',1,1,'DHL',
                     'AGENDADA','PICKUP-QA')
            """,
            (cliente_pickup, sid_pickup),
        )
    retiro = control.obtener_control_envio_admin(solicitud_id=sid_pickup)
    assert retiro["cancelacion"]["codigo"] == "RECOLECCION_ACTIVA"

    sid_emision = _crear_solicitud(db, sufijo="EMITIENDO", precio=Decimal("500"))
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE solicitudes_guia SET estado='EMITIENDO' WHERE id=%s",
            (sid_emision,),
        )
    emision = control.obtener_control_envio_admin(solicitud_id=sid_emision)
    assert emision["cancelacion"]["codigo"] == "EMISION_EN_CURSO"


def test_factura_y_nc_exactas_habilitan_cancelacion_con_precio_cero(db):
    sid, eid, cliente = _envio(db, "FC_NC")
    factura_id = _factura_cliente(
        db, cliente=cliente, tipo="FC", numero=201, monto="1000", envio_id=eid
    )
    ajuste = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_fc_nc_exacta_01",
        motivo="Bonificación total con nota de crédito",
    )
    _factura_cliente(
        db, cliente=cliente, tipo="NC", numero=202, monto="1000",
        ajuste_id=ajuste["ajuste_id"],
    )
    assert cuenta_corriente.get_facturado_real(cliente) == 0
    contexto = control.obtener_control_envio_admin(envio_id=eid)
    assert contexto["cancelacion"]["habilitada"] is True
    control.cancelar_envio_admin(
        envio_id=eid,
        cliente_id=cliente,
        motivo="Cierre comercial respaldado por nota de crédito",
        actor="admin@test",
        revision=contexto["revision"],
    )
    assert cuenta_corriente.get_facturado_real(cliente) == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.estado,e.estado AS cargo FROM solicitudes_guia s JOIN envios e ON e.solicitud_id=s.id WHERE s.id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {"estado": "CANCELADO", "cargo": "CANCELADO"}
    with pytest.raises(
        psycopg2.errors.RaiseException, match="cargo cancelado"
    ):
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pagos(cliente_id,fecha,monto_ars,metodo,estado)
                VALUES(%s,CURRENT_DATE,1000,'TRANSFERENCIA','APROBADO') RETURNING id
                """,
                (cliente,),
            )
            pago_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO pagos_aplicaciones(
                    pago_id,ambito,monto_ars,estado,factura_id
                ) VALUES(%s,'INTERNACIONAL',1000,'APLICADA',%s)
                """,
                (pago_id, factura_id),
            )


def test_nc_parcial_u_otro_envio_no_cubren_la_factura(db):
    _sid, eid, cliente = _envio(db, "NC_PARCIAL")
    _factura_cliente(
        db, cliente=cliente, tipo="FC", numero=301, monto="1000", envio_id=eid
    )
    ajuste = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_nc_parcial_0001",
    )
    _factura_cliente(
        db, cliente=cliente, tipo="NC", numero=302, monto="500",
        ajuste_id=ajuste["ajuste_id"],
    )
    parcial = control.obtener_control_envio_admin(envio_id=eid)
    assert parcial["cancelacion"]["codigo"] == "COBERTURA_FISCAL_INCOMPLETA"

    _sid_otro, eid_otro, cliente_otro = _envio(db, "NC_AJENA")
    _factura_cliente(
        db, cliente=cliente_otro, tipo="FC", numero=401, monto="1000",
        envio_id=eid_otro,
    )
    ajuste_otro = _ajustar(
        cliente=cliente_otro,
        envio=eid_otro,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_nc_ajena_0001",
    )
    _otro_sid, otro_eid = _otro_envio_mismo_cliente(
        db, cliente=cliente_otro, sufijo="NC-AJENA-OTRO"
    )
    ajuste_ajeno = _ajustar(
        cliente=cliente_otro,
        envio=otro_eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_nc_ajena_0002",
    )
    _factura_cliente(
        db, cliente=cliente_otro, tipo="NC", numero=402, monto="1000",
        ajuste_id=ajuste_ajeno["ajuste_id"],
    )
    ajena = control.obtener_control_envio_admin(envio_id=eid_otro)
    assert ajena["cancelacion"]["codigo"] == "REQUIERE_NOTA_CREDITO"
    assert all(
        ajuste_ajeno["ajuste_id"] not in (item.get("ajuste_ids") or [])
        for item in ajena["historial"]["facturas"]
    )
    assert ajuste_otro["precio_nuevo_ars"] == 0


def test_pago_imputado_a_factura_bloquea_aun_con_nc_exacta(db):
    _sid, eid, cliente = _envio(db, "FC_NC_PAGADA")
    factura_id = _factura_cliente(
        db, cliente=cliente, tipo="FC", numero=501, monto="1000", envio_id=eid
    )
    ajuste = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_fc_nc_pagada_01",
    )
    _factura_cliente(
        db, cliente=cliente, tipo="NC", numero=502, monto="1000",
        ajuste_id=ajuste["ajuste_id"],
    )
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pagos(cliente_id,fecha,monto_ars,metodo,estado)
            VALUES(%s,CURRENT_DATE,1000,'TRANSFERENCIA','APROBADO') RETURNING id
            """,
            (cliente,),
        )
        pago_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO pagos_aplicaciones(
                pago_id,ambito,monto_ars,estado,factura_id
            ) VALUES(%s,'INTERNACIONAL',1000,'APLICADA',%s)
            """,
            (pago_id, factura_id),
        )
    contexto = control.obtener_control_envio_admin(envio_id=eid)
    assert contexto["cancelacion"]["codigo"] == "PAGO_IMPUTADO"
    assert contexto["historial"]["aplicaciones_pago"][0]["factura_id"] == factura_id


def test_pago_y_cancelacion_concurrentes_no_dejan_cargo_cancelado_imputado(db):
    sid, eid, cliente = _envio(db, "CARRERA_PAGO")
    factura_id = _factura_cliente(
        db, cliente=cliente, tipo="FC", numero=601, monto="1000", envio_id=eid
    )
    ajuste = _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="0",
        esperado="1000",
        clave="precio_admin_control_carrera_pago_01",
    )
    _factura_cliente(
        db, cliente=cliente, tipo="NC", numero=602, monto="1000",
        ajuste_id=ajuste["ajuste_id"],
    )
    contexto = control.obtener_control_envio_admin(envio_id=eid)
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pagos(cliente_id,fecha,monto_ars,metodo,estado)
            VALUES(%s,CURRENT_DATE,1000,'TRANSFERENCIA','APROBADO') RETURNING id
            """,
            (cliente,),
        )
        pago_id = int(cur.fetchone()["id"])
    barrera = threading.Barrier(2)

    def cancelar():
        barrera.wait()
        try:
            control.cancelar_envio_admin(
                envio_id=eid,
                cliente_id=cliente,
                motivo="Cierre comercial respaldado y concurrente",
                actor="admin@test",
                revision=contexto["revision"],
            )
            return "CANCELADO"
        except control.ControlEnvioAdminError as exc:
            return exc.codigo

    def imputar():
        barrera.wait()
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pagos_aplicaciones(
                        pago_id,ambito,monto_ars,estado,factura_id
                    ) VALUES(%s,'INTERNACIONAL',1000,'APLICADA',%s)
                    """,
                    (pago_id, factura_id),
                )
            return "IMPUTADO"
        except psycopg2.Error:
            return "IMPUTACION_RECHAZADA"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futuro_cancelar = pool.submit(cancelar)
        futuro_imputar = pool.submit(imputar)
        resultado_cancelar = futuro_cancelar.result(timeout=5)
        resultado_imputar = futuro_imputar.result(timeout=5)

    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT estado FROM envios WHERE id=%s", (eid,))
        estado = cur.fetchone()["estado"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM pagos_aplicaciones WHERE pago_id=%s",
            (pago_id,),
        )
        aplicaciones = int(cur.fetchone()["n"])
    assert (estado, aplicaciones) in (("CANCELADO", 0), ("ACTIVO", 1))
    assert (resultado_cancelar, resultado_imputar) in (
        ("CANCELADO", "IMPUTACION_RECHAZADA"),
        ("CAMBIO_CONCURRENTE", "IMPUTADO"),
    )


def test_revision_vencida_no_cancela_ni_audita(db):
    sid, eid, cliente = _envio(db, "STALE")
    contexto = control.obtener_control_envio_admin(envio_id=eid)
    _ajustar(
        cliente=cliente,
        envio=eid,
        nuevo="900",
        clave="precio_admin_control_000000000005",
    )
    with pytest.raises(control.ControlEnvioAdminError) as error:
        control.cancelar_envio_admin(
            envio_id=eid,
            cliente_id=cliente,
            motivo="Cancelación comercial documentada",
            actor="admin@test",
            revision=contexto["revision"],
        )
    assert error.value.codigo == "CAMBIO_CONCURRENTE"
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT s.estado,e.estado AS cargo FROM solicitudes_guia s JOIN envios e ON e.solicitud_id=s.id WHERE s.id=%s",
            (sid,),
        )
        assert dict(cur.fetchone()) == {"estado": "GUIA_LISTA", "cargo": "ACTIVO"}
        cur.execute(
            "SELECT COUNT(*) AS n FROM security_audit WHERE event='admin.envio_cancelacion_comercial'"
        )
        assert cur.fetchone()["n"] == 0
