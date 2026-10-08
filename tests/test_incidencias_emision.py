from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from servicios import incidencias_emision as inc


@pytest.fixture
def registro(monkeypatch):
    audit=Mock()
    monkeypatch.setattr(inc,'registrar_desde_request',audit)
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
