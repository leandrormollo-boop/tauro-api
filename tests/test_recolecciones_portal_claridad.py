"""El comprobante confirma una reserva propia, nunca una visita o una URL."""
from datetime import date
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import pytest
from starlette.requests import Request

from endpoints import portal_cliente as pc
from servicios import recolecciones as rec
from servicios import configuracion_couriers_cliente as config


def request(query=''):
    return Request({'type':'http','method':'GET','path':'/portal/recolecciones',
                    'query_string':query.encode(),'headers':[],
                    'state':{'csp_nonce':'test'}})


def pickup(**changes):
    return dict(dict(id=71,solicitud_id=81,courier='DHL',estado='AGENDADA',
                     confirmation_code='CBJ-DEMO-71',fecha=date(2026,9,18),
                     ready_time='09:00',close_time='17:00',bultos=1,peso_kg=20,
                     direccion='Dirección de prueba'),**changes)


@pytest.fixture
def page(monkeypatch):
    record=pickup()
    monkeypatch.setattr(rec,'listar',lambda _: [record])
    monkeypatch.setattr(rec,'obtener',lambda c,r: record if r==71 else None)
    monkeypatch.setattr(rec,'obtener_de_solicitud',lambda c,s: record)
    monkeypatch.setattr(config,'mapa_permisos',lambda *_: {'dhl':True})
    monkeypatch.setattr(pc,'courier_default_cliente',lambda _: 'dhl')
    monkeypatch.setattr(pc,'obtener_remitente_para_envio',lambda *_: None)
    monkeypatch.setattr(pc,'obtener_solicitud_de_cliente',lambda *_: {
        'id':81,'tracking':'DEMO-TRACKING','estado':'GUIA_LISTA','courier':'DHL',
        'remitente_pais':'CN','destino_pais':'AR'})
    monkeypatch.setattr(rec,'datos_retiro_desde_solicitud',lambda _: {
        'solicitud_id':81,'courier':'DHL','tracking':'DEMO-TRACKING','bultos':1,
        'peso_kg':20,'origen':dict(nombre='Demo',calle='Calle 1',ciudad='Ciudad',
                                estado='Estado',zip='1000',pais='CN')})
    monkeypatch.setitem(pc.templates.env.globals,'saldo_menu',lambda *_: None)
    monkeypatch.setitem(pc.templates.env.globals,'pendientes_menu',lambda *_: {})
    monkeypatch.setitem(pc.templates.env.globals,'ayuda',lambda: {'mail_url':'mailto:demo@example.invalid'})
    return record


def html(query='',**params):
    return pc.recolecciones_view(request(query),cliente='DEMO',**params).body.decode()


def test_exito_muestra_numero_y_no_invita_a_repetir_retiro(page):
    body=html('ok=1',envio=81,recoleccion=71)
    assert 'DHL confirmó tu recolección' in body
    assert 'Número de recolección DHL' in body
    assert 'CBJ-DEMO-71' in body and '18/09/2026' in body
    assert 'Horario en el origen' in body
    assert 'La reserva no confirma que los paquetes ya hayan sido retirados' in body
    assert 'action="/portal/recolecciones/nueva"' not in body
    assert 'primero emití la guía' not in body


@pytest.mark.parametrize('state', ['AGENDANDO','VERIFICAR_COURIER','CANCELANDO','COMPLETADA'])
def test_no_ofrece_duplicar_retiros_en_curso_o_completados(page,state):
    page.update(estado=state,confirmation_code=None)
    body=html('ok=1',envio=81,recoleccion=71)
    assert 'DHL confirmó tu recolección' not in body
    assert 'action="/portal/recolecciones/nueva"' not in body
    assert 'Recolección programada' not in body


def test_agendada_sin_numero_no_se_presenta_confirmada(page):
    page['confirmation_code']=None
    body=html('ok=1',envio=81,recoleccion=71)
    assert 'pendiente de confirmación' in body
    assert 'Confirmada por DHL' not in body
    assert 'action="/portal/recolecciones/nueva"' not in body


def test_cancelada_permite_nuevo_form_sin_confundir_reserva_anterior(page):
    page['estado']='CANCELADA'
    body=html('ok=1',envio=81,recoleccion=71)
    assert 'Recolección cancelada' in body
    assert 'action="/portal/recolecciones/nueva"' in body
    assert 'DHL confirmó tu recolección' not in body


@pytest.mark.parametrize('query',['ok=1','ok=2'])
def test_url_de_exito_no_fabrica_confirmacion(page,query):
    body=html(query,recoleccion=999)
    assert 'Listo: DHL confirmó' not in body
    assert 'Recolección cancelada. La guía se conserva.' not in body


def test_error_de_lectura_bloquea_nuevo_form_sin_decir_que_no_hay_retiro(page,monkeypatch):
    monkeypatch.setattr(rec,'listar',mock.Mock(side_effect=RuntimeError('offline')))
    body=html()
    assert 'No pudimos consultar' in body
    assert 'action="/portal/recolecciones/nueva"' not in body


def test_numero_se_escapa_como_texto(page):
    page['confirmation_code']='<script>no ejecutar</script>'
    body=html()
    assert '&lt;script&gt;no ejecutar&lt;/script&gt;' in body
    assert '<script>no ejecutar</script>' not in body


def create(**extra):
    return pc.recoleccion_nueva(fecha='2026-09-18',ready_time='09:00',close_time='17:00',
        bultos='1',peso_kg='20',instrucciones='',courier='DHL',solicitud_id='81',cliente='DEMO',**extra)


def test_post_exitoso_vuelve_al_comprobante_propio(monkeypatch):
    create_mock=mock.Mock(return_value={'ok':True,'id':71,'confirmation_code':'CBJ-DEMO-71'})
    monkeypatch.setattr(rec,'crear',create_mock)
    response=create()
    url=urlsplit(response.headers['location'])
    assert response.status_code==303
    assert parse_qs(url.query)=={'envio':['81'],'ok':['1'],'recoleccion':['71']}
    assert url.fragment=='recoleccion-71'
    create_mock.assert_called_once()


def test_error_conserva_guia_para_revisar_reserva_incierta(monkeypatch):
    monkeypatch.setattr(rec,'crear',mock.Mock(return_value={
        'ok':False,'incierto':True,'error':'TAURO está verificando. No la pidas de nuevo.'}))
    response=create()
    query=parse_qs(urlsplit(response.headers['location']).query)
    assert query['envio']==['81']
    assert 'verificando' in query['error'][0]
    assert 'ok' not in query


def test_no_consulta_retiro_de_guia_ajena(page,monkeypatch):
    monkeypatch.setattr(pc,'obtener_solicitud_de_cliente',lambda *_: None)
    lookup=mock.Mock(side_effect=AssertionError('No consultar'))
    monkeypatch.setattr(rec,'obtener_de_solicitud',lookup)
    body=html(envio=999)
    lookup.assert_not_called()
    assert 'guía internacional vigente de tu cuenta' in body
    assert 'action="/portal/recolecciones/nueva"' not in body


def test_detalle_muestra_comprobante_y_quita_boton_programar(page,monkeypatch):
    from servicios.estados_envio import presentar_estados_envio
    from datetime import datetime
    s=dict(id=81,tracking='DEMO-TRACKING',courier='DHL',estado='GUIA_LISTA',
           remitente_pais='CN',destino_pais='AR',dest_nombre='Destinatario DEMO',
           created_at=datetime(2026,9,16),cantidad=1,peso_kg=20,valor_declarado_usd=100,
           precio_tauro_ars=100,tiene_label=True,bultos=[],resumen_pesos={
               'real_total_kg':20,'volumetrico_total_kg':10,'facturable_total_kg':20,
               'divisor':5000,'cobra_por_volumen':False})
    presentar_estados_envio(s)
    monkeypatch.setattr(pc,'obtener_solicitud_de_cliente',lambda *_: s)
    monkeypatch.setattr(pc,'_cliente_puede_emitir_courier',lambda *_: True)
    monkeypatch.setattr(pc,'validar_reemision_cliente',lambda *_: {'ok':False})
    monkeypatch.setattr(pc,'validar_cancelacion_cliente',lambda *_: {'ok':False})
    body=pc.envio_detalle(request(),81,cliente='DEMO').body.decode()
    assert 'CBJ-DEMO-71' in body
    assert 'Recolección programada' in body
    assert 'Programar recolección con DHL' not in body
    page['estado']='CANCELADA'
    body=pc.envio_detalle(request(),81,cliente='DEMO').body.decode()
    assert 'Programar recolección con DHL' in body


# Reutiliza un schema PostgreSQL efímero. No hay credenciales productivas.
from test_numeracion_guias_tauro import guias_db, crear as crear_guia


def test_lecturas_reales_aislan_cuentas_y_eligen_ultima_reserva(guias_db,monkeypatch):
    conexion,_=guias_db
    monkeypatch.setattr(rec,'get_conn',conexion)
    monkeypatch.setattr(rec,'_ensure_tabla',lambda: None)
    propia=crear_guia(conexion,cliente='WAIMAO')
    ajena=crear_guia(conexion,cliente='DEMO')
    ids=[]
    with conexion() as conn:
        with conn.cursor() as cur:
            for cliente,sid,state,code in [('WAIMAO',propia,'CANCELADA','ANTIGUA'),
                                          ('WAIMAO',propia,'AGENDADA','VIGENTE'),
                                          ('DEMO',ajena,'AGENDADA','AJENA')]:
                cur.execute('''INSERT INTO recolecciones
                    (cliente_id,solicitud_id,fecha,ready_time,close_time,bultos,peso_kg,
                     courier,estado,confirmation_code)
                    VALUES(%s,%s,'2026-09-18','09:00','17:00',1,1,'DHL',%s,%s) RETURNING id''',
                    (cliente,sid,state,code))
                ids.append(cur.fetchone()['id'])
    assert rec.obtener('WAIMAO',ids[2]) is None
    assert rec.obtener('waimao',ids[1])['confirmation_code']=='VIGENTE'
    result=rec.listar_de_solicitudes('waimao',[propia,ajena])
    assert list(result)==[propia]
    assert result[propia]['confirmation_code']=='VIGENTE'
    assert rec.obtener_de_solicitud('WAIMAO',propia)['solicitud_id']==propia
    assert rec.obtener_de_solicitud('WAIMAO',ajena) is None


def test_error_local_de_bultos_conserva_guia_sin_llamar_courier(monkeypatch):
    create_mock=mock.Mock(side_effect=AssertionError('no crear'))
    monkeypatch.setattr(rec,'crear',create_mock)
    response=pc.recoleccion_nueva(fecha='2026-09-18',ready_time='09:00',close_time='17:00',
        bultos='inválido',peso_kg='20',instrucciones='',courier='DHL',solicitud_id='81',cliente='DEMO')
    assert parse_qs(urlsplit(response.headers['location']).query)['envio']==['81']
    create_mock.assert_not_called()
