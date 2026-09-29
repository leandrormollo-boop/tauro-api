"""Reasignación real, aislamiento del portal y preservación de cuentas/courier."""
from pathlib import Path
from datetime import datetime

import psycopg2
import pytest
from starlette.requests import Request
from test_conciliacion_couriers_postgres import (
    conciliacion_db, DATABASE_URL, _crear_solicitud, _crear_cargo_activo,
)
from servicios import asignacion_envios as servicio
from servicios import solicitudes_guia, admin_negocio, tracking_envios, control_negocio
from servicios import conciliacion_couriers as courier
from endpoints import admin

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    for modulo in (servicio, solicitudes_guia, admin_negocio, tracking_envios):
        monkeypatch.setattr(modulo, 'get_conn', conciliacion_db)
    return conciliacion_db


def historico(db, sufijo='ORIGEN'):
    sid = _crear_solicitud(db, sufijo=sufijo)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET coti_id=NULL, estado='GUIA_LISTA', precio_tauro_ars=NULL, label_pdf=%s WHERE id=%s",
                    (psycopg2.Binary(b'pdf-prueba'), sid))
    return sid


def pendiente(sid, origen='CLIENTE_ORIGEN', **kw):
    return servicio.cambiar_asignacion(sid, cliente_esperado=origen,
        cliente_nuevo=None, cliente_indicado='Cliente sin portal',
        motivo='Asignación corregida por el administrador', **kw)


def leer(db, sid):
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM solicitudes_guia WHERE id=%s', (sid,))
        return dict(cur.fetchone())


def test_pendiente_sin_saldo_conserva_documentos_courier_y_rastreo(db):
    sid = historico(db)
    antes = leer(db, sid)
    fc = courier.registrar_factura_courier(courier='DHL', tipo_documento='FC',
        numero='FC-ORIGEN', moneda='ARS', total='100', actor='qa',
        archivo_sha256='a'*64, items=[dict(linea_numero=1,
        tracking=antes['tracking'], importe='100', concepto_tipo='FLETE')])
    courier.matchear_items_exactos(fc['id'], actor='qa')
    assert admin_negocio.listar_envios(cliente_id='CLIENTE_ORIGEN')['total'] == 1
    pendiente(sid)
    despues = leer(db, sid)
    assert despues['cliente_id'] is None and not despues['visible_cliente']
    for campo in ('estado','tracking','label_pdf','created_at','courier','precio_tauro_ars'):
        assert despues[campo] == antes[campo]
    assert solicitudes_guia.obtener_solicitud_de_cliente(sid, 'CLIENTE_ORIGEN') is None
    assert solicitudes_guia.obtener_solicitud(sid)['id'] == sid
    assert admin_negocio.listar_envios(cliente_id='CLIENTE_ORIGEN')['total'] == 0
    cola = admin_negocio.listar_envios(estado='pendientes', q='Cliente sin portal')
    assert cola['total'] == 1 and cola['items'][0]['documentos'][0]['id'] == fc['id']
    assert cola['items'][0]['cobro']['label'] == 'Sin cliente asignado'
    assert admin_negocio.listar_envios(courier='DHL',estado='pendientes')['total']==1
    assert admin_negocio.resumen_proveedores()['DHL']['envios']==1
    assert tracking_envios._candidato_dhl(sid)['id'] == sid
    hoy=datetime.now(control_negocio.AR).date()
    control = control_negocio.obtener_control_negocio(hoy.replace(day=1),hoy,conexion=db)
    assert control['cuentas_resumen']['deuda'] == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) AS n FROM envios'); assert cur.fetchone()['n']==0
        cur.execute('SELECT COUNT(*) AS n FROM pagos'); assert cur.fetchone()['n']==0
        cur.execute('SELECT COUNT(*) AS n FROM asignaciones_envio'); assert cur.fetchone()['n']==1


def test_asignar_a_perfil_correcto_sin_emitir_ni_cobrar(db):
    sid=historico(db); historico(db,'CORRECTO'); pendiente(sid)
    servicio.cambiar_asignacion(sid,cliente_esperado='',cliente_nuevo='CLIENTE_CORRECTO',
        cliente_indicado='Cliente correcto',motivo='Perfil creado y validado')
    assert solicitudes_guia.obtener_solicitud_de_cliente(sid,'CLIENTE_ORIGEN') is None
    assert solicitudes_guia.obtener_solicitud_de_cliente(sid,'CLIENTE_CORRECTO')['id']==sid
    assert leer(db,sid)['visible_cliente']
    assert admin_negocio.listar_envios(estado='pendientes')['total']==0
    assert len(servicio.panel_asignacion(sid)['historial'])==2
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) AS n FROM envios'); assert cur.fetchone()['n']==0


@pytest.mark.parametrize('legacy', [False,True])
def test_no_traslada_cargos_ni_facturas_ni_pagos(db,legacy):
    sid=historico(db)
    _crear_cargo_activo(db,sid,monto='120.50')
    if legacy:
        with db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE envios SET solicitud_id=NULL WHERE solicitud_id=%s',(sid,))
    with pytest.raises(ValueError,match='cargos o ajustes'):
        pendiente(sid)
    assert leer(db,sid)['cliente_id']=='CLIENTE_ORIGEN'
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT cliente_id,monto_ars FROM envios')
        assert cur.fetchone()['cliente_id']=='CLIENTE_ORIGEN'


def test_reenvio_pestana_obsoleta_no_duplica_asignacion(db):
    sid=historico(db); pendiente(sid)
    with pytest.raises(ValueError,match='otra pestaña'):
        pendiente(sid)
    assert len(servicio.panel_asignacion(sid)['historial'])==1


def test_dos_administradores_no_pueden_pisar_asignacion(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    sid=historico(db); historico(db,'DESTINO')
    barrera=Barrier(2)
    def cambiar(destino):
        barrera.wait()
        try:
            servicio.cambiar_asignacion(sid,cliente_esperado='CLIENTE_ORIGEN',
                cliente_nuevo=destino,cliente_indicado='Cliente correcto',
                motivo='Corrección concurrente de prueba')
            return 'ok'
        except ValueError:
            return 'conflicto'
    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados=list(pool.map(cambiar,[None,'CLIENTE_DESTINO']))
    assert sorted(resultados)==['conflicto','ok']
    assert len(servicio.panel_asignacion(sid)['historial'])==1


def test_auditoria_falla_revierte_asignacion(db,monkeypatch):
    sid=historico(db)
    def falla(*a,**k): raise RuntimeError('auditoría no disponible')
    monkeypatch.setattr(servicio,'registrar_evento_con_cursor',falla)
    with pytest.raises(RuntimeError): pendiente(sid)
    assert leer(db,sid)['cliente_id']=='CLIENTE_ORIGEN'
    assert not servicio.panel_asignacion(sid)['historial']


def test_no_expone_pendiente_y_migracion_repetible(db):
    sid=historico(db); pendiente(sid)
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db() as conn, conn.cursor() as cur:
            cur.execute('UPDATE solicitudes_guia SET visible_cliente=TRUE WHERE id=%s',(sid,))
    with db() as conn, conn.cursor() as cur:
        cur.execute((Path(__file__).resolve().parents[1]/'sql/asignacion_envios.sql').read_text())
    assert leer(db,sid)['cliente_id'] is None


def test_endpoint_autenticado_y_formulario_renderiza(db,monkeypatch):
    sid=historico(db); pendiente(sid)
    monkeypatch.setattr(admin,'_is_auth',lambda token:token=='ok')
    request=Request(dict(type='http',method='GET',path=f'/admin/pedidos/{sid}/editar',
        query_string=b'',headers=[],scheme='http',server=('testserver',80),state={'csp_nonce':'qa'}))
    response=admin.admin_pedido_editar_form(request,sid,admin_token='ok')
    body=response.body.decode()
    assert 'Pendiente de asignación' in body and 'Asignar al perfil' in body
    response=admin.admin_pedido_asignacion(sid,cliente_esperado='',cliente_nuevo='CLIENTE_ORIGEN',
        cliente_indicado='Original',motivo='Restituir al perfil',accion='asignar',admin_token=None)
    assert response.status_code==303 and '/login' in response.headers['location']
    assert leer(db,sid)['cliente_id'] is None


@pytest.mark.parametrize('campo,valor', [('coti_id','COTI-1'),('origen_plataforma','shopify'),('estado','CANCELADO')])
def test_bloquea_operaciones_que_requieren_conciliacion(db,campo,valor):
    sid=historico(db)
    with db() as conn, conn.cursor() as cur:
        cur.execute(f'UPDATE solicitudes_guia SET {campo}=%s WHERE id=%s',(valor,sid))
    with pytest.raises(ValueError): pendiente(sid)
    assert leer(db,sid)['cliente_id']=='CLIENTE_ORIGEN'
