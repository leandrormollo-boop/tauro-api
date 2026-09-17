"""Controles gerenciales contra PostgreSQL aislado; sin dinero ni APIs reales."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from test_conciliacion_couriers_postgres import (
    conciliacion_db, DATABASE_URL, _crear_solicitud, _crear_cargo_activo,
    _snapshot_basico, _confirmar_todos,
)
from servicios import control_negocio as control, conciliacion_couriers as conciliacion
from servicios import tracking_envios as tracking
from endpoints import admin

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requiere PostgreSQL aislado")


def _leer(db, **kw):
    hoy = datetime.now(control.AR).date()
    return control.obtener_control_negocio(hoy.replace(day=1), hoy, conexion=db, **kw)


def _crear(db, sufijo, *, costo=None, precio="10000"):
    sid = _crear_solicitud(db, sufijo=sufijo, precio=D(precio))
    _crear_cargo_activo(db, sid, monto=precio)
    if costo is not None:
        _snapshot_basico(sid, costo=costo, precio=precio,
                         margen=str(D(precio)-D(costo)), coti_id="COTI-"+sufijo)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='DESPACHADO' WHERE id=%s", (sid,))
    return sid


def _facturar(db, sid, importe, *, tipo="FC", nro="FC-DEMO"):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT tracking FROM solicitudes_guia WHERE id=%s", (sid,))
        numero = cur.fetchone()["tracking"]
    fc = conciliacion.registrar_factura_courier(
        courier="DHL", tipo_documento=tipo, numero=nro, moneda="ARS",
        total=importe, actor="qa", archivo_sha256=("a" if tipo=="FC" else "b")*64,
        items=[dict(linea_numero=1,tracking=numero,importe=importe,concepto_tipo="FLETE")])
    conciliacion.matchear_items_exactos(fc["id"], actor="qa")
    _confirmar_todos(db,sid)
    return conciliacion.calcular_conciliacion_envio(sid, actor="qa")


def test_ciclo_real_costo_propuesto_cerrado_credito_y_reapertura(conciliacion_db):
    db=conciliacion_db
    sid=_crear(db,"CONTROL",costo="7000")
    pendiente=_facturar(db,sid,"8000")
    antes=_leer(db)["totales"]
    assert (antes["cargos"],antes["estimados"],antes["confirmados"],antes["margen_estimado"]) == (D("10000"),1,0,D("3000"))
    conciliacion.aprobar_y_aplicar_ajuste_cliente(pendiente["ajuste_id"],actor="qa")
    despues=_leer(db)
    assert (despues["totales"]["cargos"],despues["totales"]["costo_confirmado"],despues["totales"]["margen_confirmado"]) == (D("11000"),D("8000"),D("3000"))
    assert despues["cuentas_resumen"]["deuda"]==D("11000")
    credito=_facturar(db,sid,"500",tipo="NC",nro="NC-DEMO")
    # La última versión abierta hace provisional el costo, sin inventar créditos.
    abierto=_leer(db)
    assert abierto["totales"]["confirmados"]==0
    assert abierto["cuentas_resumen"]["deuda"]==D("11000")
    conciliacion.aprobar_y_aplicar_ajuste_cliente(credito["ajuste_id"],actor="qa")
    final=_leer(db)
    assert final["totales"]["confirmados"]==1
    assert final["totales"]["cargos"]==D("10500")
    assert final["totales"]["costo_confirmado"]==D("7500")
    assert final["totales"]["margen_confirmado"]==D("3000")
    assert final["cuentas_resumen"]["deuda"]==D("10500")
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n,SUM(monto_ars) AS total FROM envios")
        assert cur.fetchone()==dict(n=1,total=D("10000"))
        cur.execute("SELECT COUNT(*) AS n FROM ajustes_cliente WHERE estado='APLICADO'")
        assert cur.fetchone()["n"]==2
    _crear(db,"OTRO_SIN_COSTO")
    parcial=_leer(db)["totales"]
    assert parcial["cargos"]==D("20500")
    assert parcial["porcentaje"]==D("28.6")  # 3000 / 10500, misma cohorte.
    assert parcial["cobertura"]==50


def test_costos_desconocidos_cancelados_pruebas_y_saldos_separados(conciliacion_db):
    db=conciliacion_db
    unknown=_crear(db,"DESCONOCIDO")
    estimated=_crear(db,"ESTIMADO",costo="7000")
    cancelled=_crear(db,"CANCELADO",costo="7000")
    replaced=_crear(db,"REEMPLAZADO",costo="7000")
    test=_crear(db,"PRUEBA",costo="1")
    inactive=_crear(db,"INACTIVO",precio="2000")
    paid=_crear(db,"CREDITO",precio="1000")
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE id=%s",(cancelled,))
        cur.execute("UPDATE solicitudes_guia SET estado='REEMPLAZADO' WHERE id=%s",(replaced,))
        cur.execute("UPDATE envios SET estado='CANCELADO' WHERE solicitud_id=%s",(replaced,))
        cur.execute("UPDATE clientes SET test=TRUE WHERE cliente_id='CLIENTE_PRUEBA'")
        cur.execute("UPDATE clientes SET activo=FALSE WHERE cliente_id='CLIENTE_INACTIVO'")
        cur.execute("""INSERT INTO pagos(cliente_id,fecha,monto_ars,estado) VALUES
            ('CLIENTE_CREDITO',CURRENT_DATE,3000,'APROBADO'),
            ('CLIENTE_ESTIMADO',CURRENT_DATE,500,'PENDIENTE'),
            ('CLIENTE_ESTIMADO',CURRENT_DATE,9999,'RECHAZADO')""")
    panel=_leer(db)
    assert panel["totales"]["envios"]==5
    assert panel["totales"]["cargos"]==D("33000")
    assert panel["totales"]["confirmados"]==0
    assert panel["totales"]["margen_confirmado"]==0
    assert panel["totales"]["porcentaje"] is None
    assert panel["totales"]["estimados"]==1
    assert panel["totales"]["sin_costo"]==4
    assert panel["operacion"]["cancelados_con_cargo"]==1
    assert panel["cuentas_resumen"]==dict(deuda=D("32000"),favor=D("2000"),deudores=4,pendiente=D("500"))
    assert next(c for c in panel["cuentas"] if c["cliente_id"]=="CLIENTE_INACTIVO")["saldo"]==D("2000")
    # Filtrar el resultado por fecha no oculta deuda actual ni altera el libro.
    antiguo=control.obtener_control_negocio(date(2020,1,1),date(2020,1,2),conexion=db)
    assert antiguo["totales"]["envios"]==0
    assert antiguo["cuentas_resumen"]==panel["cuentas_resumen"]


def test_vigilancia_paginada_no_trunca_totales_ni_incluye_finalizados(conciliacion_db):
    db=conciliacion_db
    with db() as conn,conn.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id,email) VALUES('VIGILANCIA','vigilancia@example.invalid')")
        cur.execute("""INSERT INTO solicitudes_guia(
            cliente_id,producto_alias,destino_pais,dest_nombre,dest_direccion,dest_ciudad,dest_zip,
            courier,tracking,estado,tracking_estado,tracking_vigilancia_desde)
            SELECT 'VIGILANCIA','Producto','US','Destino '||n,'Calle','Ciudad','1234',
              'DHL','TRACK'||n,'DESPACHADO',
              CASE WHEN n<=30 THEN 'RETENIDO' ELSE 'PROCESO_ENTREGA' END,
              NOW()-INTERVAL '2 days'
            FROM generate_series(1,55) n""")
        cur.execute("UPDATE solicitudes_guia SET estado='ENTREGADO',tracking_estado='ENTREGADO' WHERE tracking='TRACK55'")
        cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE tracking='TRACK54'")
        cur.execute("UPDATE solicitudes_guia SET estado='REEMPLAZADO' WHERE tracking='TRACK53'")
        cur.execute("UPDATE solicitudes_guia SET test=TRUE WHERE tracking='TRACK52'")
        cur.execute("UPDATE solicitudes_guia SET tracking_error='DHL sin respuesta' WHERE tracking='TRACK1'")
        cur.execute("UPDATE solicitudes_guia SET created_at=NOW()-INTERVAL '2 days'")
    primera=_leer(db)
    ultima=_leer(db,pagina=999)
    held=_leer(db,vigilancia="retenidos")
    released=_leer(db,vigilancia="liberados")
    errors=_leer(db,vigilancia="errores")
    assert primera["operacion"]["vigilados"]==51
    assert primera["vigilancia"]["total"]==51
    assert len(primera["vigilancia"]["items"])==25
    assert ultima["vigilancia"]["pagina"]==3
    assert len(ultima["vigilancia"]["items"])==1
    assert held["vigilancia"]["total"]==30
    assert released["vigilancia"]["total"]==21
    assert errors["vigilancia"]["total"]==1
    assert not ({r["id"] for r in primera["vigilancia"]["items"]}&{r["id"] for r in ultima["vigilancia"]["items"]})
    assert all(r["atrasado"] for r in primera["vigilancia"]["items"])
    assert _leer(db,pagina_cargos=999)["cargos_control"]["pagina"]==3
    assert len(_leer(db,pagina_cargos=999)["alertas"])==2  # Incluye la guía entregada sin cargo.
    assert _leer(db,vigilancia="sin_actualizar")["vigilancia"]["total"]==51
    nuevo=_crear(db,"RASTREO_NUEVO")
    # Una guía recién emitida sin eventos todavía no está atrasada.
    assert _leer(db,vigilancia="sin_actualizar")["vigilancia"]["total"]==51


def test_acceso_no_autorizado_no_consulta_el_panel(monkeypatch):
    monkeypatch.setattr(admin,"_is_auth",lambda _:False)
    monkeypatch.setattr(admin,"get_conn",lambda:pytest.fail("No debe leer datos"))
    response=admin.admin_home(SimpleNamespace(),admin_token=None)
    assert response.status_code==303 and response.headers["location"]=="/admin/login"


def test_panel_vacio_y_lectura_no_repara_estados(conciliacion_db,monkeypatch):
    from starlette.requests import Request
    db=conciliacion_db
    monkeypatch.setattr(admin,"get_conn",db)
    monkeypatch.setattr(admin,"_is_auth",lambda _:True)
    monkeypatch.setattr(admin,"contar_solicitudes_pendientes",lambda:pytest.fail("Lectura con efectos"))
    monkeypatch.setitem(admin.templates.env.globals,"pendientes_admin",lambda:0)
    monkeypatch.setitem(admin.templates.env.globals,"alertas_guias_reemplazadas",lambda:0)
    request=Request(dict(type="http",method="GET",path="/admin/home",headers=[],query_string=b""))
    response=admin.admin_home(request,admin_token="qa")
    assert response.status_code==200
    assert "No hay envíos con cargo" in response.body.decode()
    assert "No hay envíos en este filtro" in response.body.decode()
    sid=_crear(db,"SIN_REPARACION")
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE envios SET estado='CANCELADO' WHERE solicitud_id=%s",(sid,))
    response=admin.admin_home(request,admin_token="qa")
    assert response.status_code==200
    with db() as conn,conn.cursor() as cur:
        cur.execute("SELECT estado FROM solicitudes_guia WHERE id=%s",(sid,))
        assert cur.fetchone()["estado"]=="DESPACHADO"


@pytest.mark.parametrize("desde,hasta",[
    ("no-es-fecha","2026-09-17"),("2026-09-18","2026-09-17"),
    ("2024-01-01","2026-09-17"),("2026-09-01","2026-09-18")])
def test_periodo_invalido_no_afecta_control_actual(desde,hasta):
    p=control.periodo_control(desde,hasta,hoy=date(2026,9,17))
    assert p["error"] and p["desde"]==date(2026,9,1) and p["hasta"]==date(2026,9,17)


@pytest.mark.parametrize("texto,estado",[
    ("Shipment not delivered","RETENIDO"),("Shipment not yet delivered","RETENIDO"),
    ("Undelivered shipment","RETENIDO"),("Shipment will be delivered tomorrow","PROCESO_ENTREGA"),
    ("Delivered","ENTREGADO"),("Shipment delivered","ENTREGADO"),
    ("Out for delivery","PROCESO_ENTREGA")])
def test_entrega_no_se_infiere_por_texto_ambiguo(texto,estado):
    assert tracking.normalizar_respuesta_dhl(dict(encontrado=True,
        ultimo_evento=dict(typeCode="ZZ",description=texto)))["estado"]==estado


def test_ventanas_argentinas_y_horarios(monkeypatch):
    monkeypatch.delenv("DHL_TRACKING_CRON_HOUR",raising=False)
    monkeypatch.delenv("DHL_TRACKING_CRON_MINUTE",raising=False)
    ar=control.AR
    assert tracking.ventana_vigilancia(datetime(2026,9,17,11,59,tzinfo=ar)).hour==0
    assert tracking.ventana_vigilancia(datetime(2026,9,17,12,0,tzinfo=ar)).hour==12
    assert tracking.proximo_control_vigilancia(datetime(2026,9,17,5,20,tzinfo=ar))==datetime(2026,9,17,17,20,tzinfo=ar)
    assert tracking.proximo_control_vigilancia(datetime(2026,9,17,23,59,tzinfo=ar))==datetime(2026,9,18,5,20,tzinfo=ar)
    monkeypatch.setenv("DHL_TRACKING_CRON_HOUR","20")
    assert tracking.proximo_control_vigilancia(datetime(2026,9,17,7,tzinfo=ar)).hour==8
    monkeypatch.setenv("DHL_TRACKING_CRON_HOUR","99")
    monkeypatch.setenv("DHL_TRACKING_CRON_MINUTE","abc")
    assert tracking.horarios_tracking()==(5,20)


def test_retenido_liberado_error_entregado_y_cambio_de_tracking(conciliacion_db,monkeypatch):
    db=conciliacion_db
    monkeypatch.setattr(tracking,"get_conn",db)
    sid=_crear(db,"PERSISTENTE")
    class DHL:
        codigo="OH"
        error=False
        reemplazo=False
        def track(self,numero):
            if self.reemplazo:
                with db() as conn,conn.cursor() as cur:
                    cur.execute("UPDATE solicitudes_guia SET tracking='NUEVA' WHERE id=%s",(sid,))
            return (dict(encontrado=False,error="Sin respuesta") if self.error else
                    dict(encontrado=True,ultimo_evento=dict(typeCode=self.codigo,description=self.codigo)))
    dhl=DHL()
    def fila():
        with db() as conn,conn.cursor() as cur:
            cur.execute("SELECT * FROM solicitudes_guia WHERE id=%s",(sid,))
            return cur.fetchone()
    tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)
    inicio=fila()["tracking_vigilancia_desde"]
    assert inicio is not None
    dhl.codigo="DF"
    tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)
    liberado=fila()
    assert liberado["tracking_vigilancia_desde"]==inicio and liberado["tracking_estado"]=="PROCESO_ENTREGA"
    dhl.error=True
    tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)
    assert fila()["tracking_actualizado_at"]==liberado["tracking_actualizado_at"]
    assert fila()["tracking_vigilancia_desde"]==inicio
    dhl.error=False;dhl.codigo="OK";dhl.reemplazo=True
    assert tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)["guardado"] is False
    assert fila()["tracking_estado"]=="PROCESO_ENTREGA"
    dhl.reemplazo=False
    tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)
    assert tracking.actualizar_tracking_dhl(sid,cliente_dhl=dhl)["omitido"] is True
    assert fila()["tracking_vigilancia_desde"]==inicio
    assert _leer(db)["vigilancia"]["total"]==0


def test_rondas_solo_vigilancia_idempotentes_error_ocupa_ventana(conciliacion_db,monkeypatch):
    db=conciliacion_db
    vigilado=_crear(db,"RONDA")
    normal=_crear(db,"NORMAL")
    with db() as conn,conn.cursor() as cur:
        cur.execute("""UPDATE solicitudes_guia SET tracking_vigilancia_desde=NOW()-INTERVAL '2 days',
            tracking_estado='PROCESO_ENTREGA',tracking_consultado_at=NOW()-INTERVAL '1 day'
            WHERE id=%s""",(vigilado,))
    llamadas=[]
    class DHL:
        def _error_configuracion(self):return None
        def track(self,numero):
            llamadas.append(numero)
            return dict(encontrado=False,error="Sin eventos")
    monkeypatch.setattr(tracking,"get_conn",db)
    monkeypatch.setattr(tracking,"DHLClient",DHL)
    assert tracking.actualizar_trackings_diarios_dhl(solo_vigilancia=True)["consultados"]==1
    assert tracking.actualizar_trackings_diarios_dhl(solo_vigilancia=True)["consultados"]==0
    assert llamadas==["TRACK-RONDA"]
    assert tracking.actualizar_trackings_diarios_dhl()["consultados"]==1
    assert tracking.actualizar_trackings_diarios_dhl()["consultados"]==0
    # Adelantar sólo el intento a la ventana anterior habilita la segunda ronda.
    ventana=tracking.ventana_vigilancia()
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET tracking_consultado_at=%s WHERE id=%s",
                    (ventana-timedelta(seconds=1),vigilado))
    assert tracking.actualizar_trackings_diarios_dhl(solo_vigilancia=True)["consultados"]==1
    assert tracking.actualizar_trackings_diarios_dhl(solo_vigilancia=True)["consultados"]==0
    assert llamadas==["TRACK-RONDA","TRACK-NORMAL","TRACK-RONDA"]


def test_lock_entre_workers_y_exclusion_de_pruebas_finalizados(conciliacion_db,monkeypatch):
    db=conciliacion_db
    monkeypatch.setattr(tracking,"get_conn",db)
    ids={estado:_crear(db,"EXCLUIR_"+estado) for estado in (
        "CANCELADO","REEMPLAZADO","ENTREGADO","TEST","CLIENTE_TEST","FEDEX")}
    with db() as conn,conn.cursor() as cur:
        for estado in ("CANCELADO","REEMPLAZADO","ENTREGADO"):
            cur.execute("UPDATE solicitudes_guia SET estado=%s WHERE id=%s",(estado,ids[estado]))
        cur.execute("UPDATE solicitudes_guia SET test=TRUE WHERE id=%s",(ids["TEST"],))
        cur.execute("UPDATE clientes SET test=TRUE WHERE cliente_id='CLIENTE_EXCLUIR_CLIENTE_TEST'")
        cur.execute("UPDATE solicitudes_guia SET courier='FEDEX' WHERE id=%s",(ids["FEDEX"],))
    class DHL:
        def _error_configuracion(self):return None
        def track(self,_):pytest.fail("No debe consultar guía excluida")
    monkeypatch.setattr(tracking,"DHLClient",DHL)
    assert all(tracking._candidato_dhl(sid) is None for sid in ids.values())
    with db() as conn,conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(hashtext(%s))",(tracking._LOCK_NAME,))
        try:
            assert tracking.actualizar_trackings_diarios_dhl()["motivo"]=="job_en_curso"
            assert tracking.actualizar_trackings_diarios_dhl(solo_vigilancia=True)["motivo"]=="job_en_curso"
        finally:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))",(tracking._LOCK_NAME,))
    assert tracking.actualizar_trackings_diarios_dhl()["consultados"]==0


def test_migracion_recupera_retenidos_y_no_reinicia_historia(conciliacion_db):
    from pathlib import Path
    db=conciliacion_db
    sid=_crear(db,"MIGRACION")
    with db() as conn,conn.cursor() as cur:
        cur.execute("""UPDATE solicitudes_guia SET tracking_estado='RETENIDO',
            tracking_actualizado_at=NOW()-INTERVAL '5 days' WHERE id=%s""",(sid,))
    def migrar():
        with db() as conn,conn.cursor() as cur:
            cur.execute((Path(__file__).resolve().parents[1]/"sql/schema.sql").read_text())
            cur.execute("SELECT tracking_vigilancia_desde,tracking_actualizado_at FROM solicitudes_guia WHERE id=%s",(sid,))
            return cur.fetchone()
    inicial=migrar()
    assert inicial["tracking_vigilancia_desde"]==inicial["tracking_actualizado_at"]
    assert migrar()==inicial
