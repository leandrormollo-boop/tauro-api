from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from servicios import incidencias_emision as inc


@pytest.fixture
def registro(monkeypatch):
    audit=Mock()
    monkeypatch.setattr(inc,'registrar_desde_request',audit)
    monkeypatch.setattr(inc,'iniciar_intento',Mock(return_value=1))
    monkeypatch.setattr(inc,'finalizar_intento',Mock())
    return audit


def test_error_guarda_motivo_y_referencia_sin_payload(registro):
    emitir=Mock(return_value={'ok':False,'codigo_error':'TARIFA_GUARDADA_INCONSISTENTE',
        'etapa':'validar_tarifa','error_tipo':'SnapshotInmutableError','error':'Revisá la tarifa.',
        'payload':{'password':'privada'},'_base_interna':{'costo':10}})
    res=inc.ejecutar_emision_registrada(None,123,'cliente','PRUEBA',emitir)
    datos=registro.call_args.kwargs
    assert datos['success'] is False and datos['event']=='portal.emitir_guia'
    assert datos['metadata']['motivo']=='Revisá la tarifa.'
    assert datos['metadata']['error_tipo']=='SnapshotInmutableError'
    assert res['referencia_error'] in res['error']
    assert res['referencia_error']==datos['metadata']['referencia']
    assert 'privada' not in str(datos) and 'costo' not in str(datos)
    emitir.assert_called_once()


def test_excepcion_incierta_se_registra_y_no_reintenta(registro,capsys):
    emitir=Mock(side_effect=RuntimeError('password=secreto-no-imprimir'))
    res=inc.ejecutar_emision_registrada(None,123,'admin','admin',emitir)
    assert not res['ok'] and res['codigo_error']=='RESULTADO_NO_CONFIRMADO'
    assert 'No emitimos' not in res['error']
    assert 'secreto-no-imprimir' not in str(registro.call_args)
    assert 'secreto-no-imprimir' not in capsys.readouterr().out
    emitir.assert_called_once()


def test_exito_no_altera_resultado(registro):
    original={'ok':True,'tracking':'PRUEBA'}
    assert inc.ejecutar_emision_registrada(None,123,'cliente','PRUEBA',lambda:original)==original
    assert registro.call_args.kwargs['success'] is True


def test_no_guarda_correo_token_ni_url():
    texto=inc._mensaje_registro('Error test@example.invalid token=SECRETO https://api.test/?key=privada')
    assert 'test@example' not in texto and 'SECRETO' not in texto and 'key=privada' not in texto


def test_sin_registro_durable_no_llama_al_courier(registro,monkeypatch):
    monkeypatch.setattr(inc,'iniciar_intento',Mock(side_effect=RuntimeError('secreto')))
    emitir=Mock()
    resultado=inc.ejecutar_emision_registrada(None,123,'cliente','PRUEBA',emitir)
    assert resultado['codigo_error']=='REGISTRO_NO_DISPONIBLE'
    emitir.assert_not_called()


def test_registro_previo_y_fallo_posterior_no_duplica_emision(registro,monkeypatch):
    eventos=[]
    monkeypatch.setattr(inc,'iniciar_intento',lambda *a: eventos.append('iniciado') or 1)
    monkeypatch.setattr(inc,'finalizar_intento',Mock(side_effect=RuntimeError('fallo db')))
    resultado=inc.ejecutar_emision_registrada(None,123,'cliente','PRUEBA',
        lambda: eventos.append('emitido') or {'ok':True,'tracking':'PRUEBA'})
    assert eventos==['iniciado','emitido']
    assert resultado=={'ok':True,'tracking':'PRUEBA'}


def test_solicitud_ajena_no_llega_al_courier(registro,monkeypatch):
    monkeypatch.setattr(inc,'iniciar_intento',lambda *a:None)
    emitir=Mock()
    assert inc.ejecutar_emision_registrada(None,123,'cliente','OTRO',emitir)['ok'] is False
    emitir.assert_not_called()


@pytest.mark.parametrize('credencial', [
    'Authorization: Bearer SECRETO', 'password="SECRETO CON ESPACIOS"',
    '\"api_key\": \"SECRETO\"', 'token:\nSECRETO',
])
def test_no_guarda_credenciales_compuestas(credencial):
    assert 'SECRETO' not in inc._mensaje_registro('Error: ' + credencial)


def test_historial_solo_admin(monkeypatch):
    from endpoints import admin
    consulta=Mock()
    monkeypatch.setattr(inc,'listar_intentos',consulta)
    monkeypatch.setattr(admin,'_is_auth',lambda _:False)
    res=admin.admin_incidencias_emision(SimpleNamespace(),admin_token=None)
    assert res.status_code in (302,303,307)
    consulta.assert_not_called()


def test_historial_admin_con_filtros(monkeypatch):
    from endpoints import admin
    consulta=Mock(return_value={'items':[],'siguiente':0})
    monkeypatch.setattr(inc,'listar_intentos',consulta)
    monkeypatch.setattr(admin,'_is_auth',lambda _:True)
    monkeypatch.setattr(admin.templates,'TemplateResponse',lambda **kw:kw)
    res=admin.admin_incidencias_emision(SimpleNamespace(),solicitud_id=123,resultado='todos',antes=50,admin_token='test')
    consulta.assert_called_once_with(solicitud_id=123,resultado='todos',antes=50)
    assert res['name']=='admin/incidencias_emision.html'


@pytest.mark.parametrize('query,esperado', [
    ('resultado=todos', 0),
    ('solicitud_id=&resultado=todos', 0),
    ('solicitud_id=123&resultado=todos', 123),
])
def test_historial_http_acepta_formulario_con_solicitud_opcional(monkeypatch, query, esperado):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import admin

    consulta = Mock(return_value={'items': [], 'siguiente': 0})
    monkeypatch.setattr(inc, 'listar_intentos', consulta)
    monkeypatch.setattr(admin, '_is_auth', lambda _: True)
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        response = client.get('/admin/incidencias-emision?' + query)
    assert response.status_code == 200
    assert 'Intentos de emisión' in response.text
    assert 'No hay intentos registrados para este filtro' in response.text
    consulta.assert_called_once_with(solicitud_id=esperado, antes=0, resultado='todos')


@pytest.mark.parametrize('tracking,enlace', [
    (None, '/admin/pedidos/123/editar'),
    ('PRUEBA123', '/admin/conciliacion-couriers/envios/123'),
])
def test_historial_enlaza_pedido_pendiente_y_control_de_guia_emitida(monkeypatch, tracking, enlace):
    from datetime import datetime
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from endpoints import admin

    intento = {'solicitud_id':123, 'cliente_id':'PRUEBA', 'courier':'DHL',
               'fecha':datetime(2026,10,8,12), 'success':False, 'actor_type':'cliente',
               'tracking':tracking, 'estado_cliente_ui':{'label':'Solicitado'},
               'metadata':{'motivo':'Revisá los datos.', 'referencia':'EMI-PRUEBA'}}
    monkeypatch.setattr(inc, 'listar_intentos', lambda **kw: {'items':[intento], 'siguiente':0})
    monkeypatch.setattr(admin, '_is_auth', lambda _:True)
    app = FastAPI()
    app.include_router(admin.router)
    with TestClient(app) as client:
        response = client.get('/admin/incidencias-emision')
    assert response.status_code == 200
    assert f'href="{enlace}">Solicitud #123</a>' in response.text
