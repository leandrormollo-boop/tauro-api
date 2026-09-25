"""El nuevo control une documentos sin crear asientos ni inventar cobros."""
from decimal import Decimal
from pathlib import Path

import pytest
from starlette.requests import Request

from servicios import admin_negocio as negocio
from servicios import cuenta_corriente
from servicios import operadores_logisticos as op
from servicios import conciliacion_couriers as cc
from endpoints import admin, admin_operadores
from test_conciliacion_couriers_postgres import (
    conciliacion_db, DATABASE_URL, _crear_solicitud, _crear_cargo_activo, _confirmar_todos,
)
from test_operadores_postgres import fc, pago, aplicar

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    for modulo in (negocio, op, admin, cuenta_corriente):
        monkeypatch.setattr(modulo, 'get_conn', conciliacion_db)
    monkeypatch.setattr(admin, '_is_auth', lambda token: token=='valido')
    return conciliacion_db


def req(url):
    path, _, query = url.partition('?')
    return Request(dict(type='http', method='GET', path=path, query_string=query.encode(),
        headers=[], scheme='http', server=('testserver',80), state={'csp_nonce':'test'}))


def crear(db, sufijo, courier='DHL', monto='100'):
    sid=_crear_solicitud(db,sufijo=sufijo,courier=courier)
    _crear_cargo_activo(db,sid,monto=monto)
    return sid


def pago_cliente(db, cliente, *, total='100', estado='APROBADO', envio=None, factura=None, aplicado=None):
    with db() as conn,conn.cursor() as cur:
        cur.execute('''INSERT INTO pagos(cliente_id,fecha,monto_ars,metodo,estado)
            VALUES(%s,CURRENT_DATE,%s,'TRANSFERENCIA',%s) RETURNING id''',(cliente,total,estado))
        pid=cur.fetchone()['id']
        if envio or factura:
            cur.execute('''INSERT INTO pagos_aplicaciones(pago_id,ambito,monto_ars,estado,envio_id,factura_id)
                VALUES(%s,'INTERNACIONAL',%s,%s,%s,%s)''',
                (pid,aplicado or total,'APLICADA' if estado=='APROBADO' else 'SOLICITADA',envio,factura))
    return pid


def test_agrupacion_automatica_sin_factura_y_cliente_aislado(db):
    a=crear(db,'UNO','OCA'); b=crear(db,'DOS','DHL')
    datos=negocio.listar_envios(courier='OCA')
    assert [s['id'] for s in datos['items']]==[a]
    assert not datos['items'][0]['documentos']
    assert datos['items'][0]['cobro']['label']=='Sin pago imputado'
    assert negocio.listar_envios(cliente_id='CLIENTE_DOS')['items'][0]['id']==b
    assert negocio.resumen_proveedores()['OCA']['sin_factura']==1
    assert negocio.listar_envios(q='%')['total']==0
    assert negocio.listar_envios(q="' OR TRUE --")['total']==0


def test_saldo_global_y_pago_solicitado_no_prueban_cobro_del_envio(db):
    sid=crear(db,'COBRO')
    pago_cliente(db,'CLIENTE_COBRO',total='200')
    with db() as conn,conn.cursor() as cur:
        cur.execute('SELECT id FROM envios WHERE solicitud_id=%s',(sid,));eid=cur.fetchone()['id']
    pendiente=pago_cliente(db,'CLIENTE_COBRO',estado='PENDIENTE',envio=eid)
    assert negocio.listar_envios()['items'][0]['cobro']['label']=='Sin pago imputado'
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE pagos SET estado='APROBADO' WHERE id=%s",(pendiente,))
        cur.execute("UPDATE pagos_aplicaciones SET estado='APLICADA' WHERE pago_id=%s",(pendiente,))
    assert negocio.listar_envios()['items'][0]['cobro']['label']=='Cobrado · imputación al envío'


def test_cancelados_reemplazados_sin_impacto_y_alerta_inconsistencia(db):
    for key,estado in [('cancelados','CANCELADO'),('modificados','REEMPLAZADO')]:
        sid=crear(db,key.upper())
        with db() as conn,conn.cursor() as cur:
            cur.execute('UPDATE solicitudes_guia SET estado=%s WHERE id=%s',(estado,sid))
        s=negocio.listar_envios(estado=key)['items'][0]
        assert s['cobro']['importe']==0 and s['cobro']['inconsistencia']
    assert negocio.listar_envios()['total']==0
    with db() as conn,conn.cursor() as cur:
        cur.execute('SELECT SUM(monto_ars) AS total FROM envios')
        assert cur.fetchone()['total']==200  # La lectura no corrige ni borra historia.


def test_factura_con_varias_lineas_no_duplica_envios_y_pago_proveedor_no_es_cobro(db):
    sid=crear(db,'LINEAS')
    f=fc(items=[dict(linea_numero=n,tracking='TRACK-LINEAS',concepto_tipo='FLETE',importe='50',tipo_cambio_ars='1000') for n in [1,2]])
    cc.matchear_items_exactos(f)
    s=negocio.listar_envios()['items'][0]
    assert len(s['documentos'])==1 and s['documentos'][0]['match_estado']=='PROPUESTO'
    _confirmar_todos(db,sid)
    op.verificar_historial('DHL',f,'Historial verificado contra estado de cuenta')
    pid=pago(); aplicar(f,pago_id=pid)
    s=negocio.listar_envios()['items'][0]
    assert len(s['documentos'])==1
    assert s['documentos'][0]['estado_pago']=='CANCELADA'
    assert s['cobro']['label']=='Sin pago imputado'
    assert s['cobro']['importe']==100


def test_factura_cliente_parcial_no_se_prorratea_y_pago_total_cubre(db):
    a=crear(db,'FACTURA')
    b=crear(db,'OTRO')
    with db() as conn,conn.cursor() as cur:
        # Ambos envíos pertenecen al mismo cliente, como una factura por lote.
        cur.execute("UPDATE solicitudes_guia SET cliente_id='CLIENTE_FACTURA' WHERE id=%s",(b,))
        cur.execute("UPDATE envios SET cliente_id='CLIENTE_FACTURA' WHERE solicitud_id=%s",(b,))
        cur.execute('''INSERT INTO facturas_cliente(cliente_id,tipo,punto_venta,numero,fecha_emision,
            subtotal,total,pdf,created_by) VALUES('CLIENTE_FACTURA','FC',1,1,CURRENT_DATE,200,200,%s,'test')
            RETURNING id''',(b'%PDF-test',));fid=cur.fetchone()['id']
        cur.execute('''INSERT INTO facturas_cliente_items(factura_id,envio_id,descripcion,monto)
            SELECT %s,id,'Envío',100 FROM envios WHERE solicitud_id=ANY(%s)''',(fid,[a,b]))
    pago_cliente(db,'CLIENTE_FACTURA',total='50',factura=fid)
    datos=negocio.listar_envios(cliente_id='CLIENTE_FACTURA')
    assert len(datos['items'])==2
    assert all(s['cobro']['label']=='Pago parcial de factura' for s in datos['items'])
    pago_cliente(db,'CLIENTE_FACTURA',total='150',factura=fid)
    assert all(s['cobro']['label']=='Cobrado · facturas saldadas' for s in negocio.listar_envios()['items'])


def test_correo_admite_documentos_pagos_y_migracion_idempotente(db):
    f=fc(courier='CORREO_ARGENTINO')
    pid=pago(courier='CORREO_ARGENTINO')
    assert op.listar_documentos('CORREO_ARGENTINO')[0]['id']==f
    assert op.listar_pagos('CORREO_ARGENTINO')[0]['id']==pid
    with db() as conn,conn.cursor() as cur:
        cur.execute((Path(__file__).parents[1]/'sql/schema.sql').read_text())
    assert op.listar_documentos('CORREO_ARGENTINO')[0]['id']==f


def test_rutas_autenticadas_plantillas_y_agendas(db):
    sid=crear(db,'UI')
    for vista in ('cuenta','pagos','envios','destinatarios','recolecciones','configuracion'):
        r=admin.admin_cliente_detail(req('/admin/clientes/CLIENTE_UI?vista='+vista),'CLIENTE_UI',admin_token='valido')
        assert r.status_code==200
        if vista not in ('cuenta','pagos'):
            assert r.headers['cache-control']=='private, no-store'
        assert b'Sus clientes' in r.body and b'Cuenta corriente' in r.body
    for vista in ('envios','cuenta','pagos','configuracion'):
        r=admin_operadores.mundo(req('/admin/operadores/DHL?vista='+vista),'DHL',admin_token='valido')
        assert r.status_code==200 and r.headers['cache-control']=='private, no-store'
        assert b'Proveedores' in r.body
    assert admin_operadores.inicio(req('/admin/operadores'),admin_token='valido').status_code==200
    for vista in ('envios','cuenta','pagos','configuracion'):
        assert admin_operadores.mundo(req('/admin/operadores/DHL?vista='+vista),'DHL',admin_token='otro').status_code==303
    assert admin.admin_cliente_detail(req('/admin/clientes/CLIENTE_UI?vista=envios'),'CLIENTE_UI',admin_token='otro').status_code==303


def test_paginacion_no_repite_y_excluye_pruebas(db):
    for n in range(27):
        crear(db,f'P{n:02d}')
    a=negocio.listar_envios(pagina=1); b=negocio.listar_envios(pagina=2)
    assert (a['total'],len(a['items']),len(b['items']))==(27,25,2)
    assert not ({s['id'] for s in a['items']} & {s['id'] for s in b['items']})
    assert negocio.listar_envios(pagina=99999)['pagina']==2
    with db() as conn,conn.cursor() as cur:
        cur.execute("UPDATE clientes SET test=TRUE WHERE cliente_id='CLIENTE_P00'")
    assert negocio.listar_envios()['total']==26
