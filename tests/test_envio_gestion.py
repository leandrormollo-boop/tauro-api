"""Ventana operativa: revisión explícita, misma solicitud y precio vigente."""
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from endpoints import portal_cliente as pc
from servicios import edicion_solicitud as ed, solicitudes_guia as sg
from servicios.meta_ads import construir_content_security_policy
from test_dhl_invoice_multiitems import portal, submit


def pendiente(**extra):
    return dict(id=81, cliente_id='WAIMAO', estado='SOLICITADO',
                ambito='INTERNACIONAL', remitente_pais='CN', destino_pais='AR',
                courier='DHL', updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc), **extra)


@pytest.mark.parametrize('cambio', [dict(estado='EMITIENDO'), dict(estado='VERIFICAR_COURIER'),
    dict(estado='CANCELADO'), dict(estado='REEMPLAZADO'), dict(estado='GUIA_LISTA'),
    dict(tracking='123'), dict(guia_url='x'), dict(tiene_label=True), dict(cargo_pendiente=True),
    dict(reemplaza_solicitud_id=3), dict(reemplazada_por_solicitud_id=4),
    dict(test=True), dict(visible_cliente=False), dict(ambito='NACIONAL')])
def test_no_edita_estados_o_historia_protegidos(cambio):
    assert ed.puede_editar_solicitud(pendiente())
    assert not ed.puede_editar_solicitud({**pendiente(), **cambio})


def test_crear_abre_revision_sin_emitir(portal, monkeypatch):
    emitir = Mock()
    monkeypatch.setattr(sg, 'emitir_guia_como_cliente', emitir)
    response = submit()
    assert response.headers['location'] == '/portal/envios/99?gestionar=1'
    emitir.assert_not_called()


def test_identidad_de_edicion_invalida_no_se_convierte_en_alta(portal):
    created, quotes = portal
    response = submit(editar_solicitud_id='no-es-un-id')
    assert 'identificar' in response['context']['error']
    created.assert_not_called()
    quotes.assert_not_called()


def test_edicion_recotiza_y_guarda_mismo_id(portal, monkeypatch):
    created, quotes = portal
    quotes.return_value[0]['_base_interna'] = {'origen': 'cotizador_privado'}
    sol = pendiente()
    monkeypatch.setattr(pc, 'obtener_solicitud_de_cliente', lambda *a: sol)
    editar = Mock(return_value=sol)
    monkeypatch.setattr(pc, 'editar_solicitud_cliente', editar)
    response = submit(editar_solicitud_id='81', editar_version=sol['updated_at'].isoformat(), gestion_ventana='1')
    assert response.headers['location'] == '/portal/envios/81/gestion?ventana=1&ok=editado'
    assert editar.call_args.args[:3] == (81, 'WAIMAO', sol['updated_at'].isoformat())
    assert editar.call_args.args[3]['precio_tauro_ars'] == 195000
    assert len(editar.call_args.args[3]['bultos'][0]['items_invoice']) == 2
    quotes.assert_called_once()
    assert quotes.call_args.kwargs['incluir_base_interna'] is True
    assert editar.call_args.kwargs['base_interna'] == {'origen': 'cotizador_privado'}
    assert '_base_interna' not in editar.call_args.args[3]
    created.assert_not_called()


@pytest.mark.parametrize('modo', ['ajeno', 'emitido', 'precio', 'version'])
def test_error_edicion_no_crea_otra_solicitud(portal, monkeypatch, modo):
    created, quotes = portal
    quotes.return_value[0]['_base_interna'] = {'origen': 'cotizador_privado'}
    sol = None if modo == 'ajeno' else pendiente(tracking='123') if modo == 'emitido' else pendiente()
    monkeypatch.setattr(pc, 'obtener_solicitud_de_cliente', lambda *a: sol)
    editar = Mock(side_effect=ValueError('El envío cambió mientras lo editabas.'))
    monkeypatch.setattr(pc, 'editar_solicitud_cliente', editar)
    response = submit(editar_solicitud_id='81', editar_version='old', precio_cotizado_ars='1' if modo == 'precio' else '195000')
    assert response['context']['error']
    assert response['context']['form']['editar_solicitud_id'] == '81'
    created.assert_not_called()
    if modo != 'version':
        editar.assert_not_called()


def request(path='/portal/envios/81/gestion'):
    return Request(dict(type='http', method='GET', path=path, headers=[], query_string=b'ventana=1', server=('testserver', 80)))


@pytest.mark.parametrize('revision', ['', '2026-10-01T00:00:00+00:00'])
def test_confirmar_exige_revision_y_regresa_a_ventana(monkeypatch, revision):
    monkeypatch.setattr('servicios.incidencias_emision.iniciar_intento',lambda *a:1)
    monkeypatch.setattr('servicios.incidencias_emision.finalizar_intento',lambda *a:None)
    emitir = Mock(return_value=dict(ok=True))
    monkeypatch.setattr(sg, 'emitir_guia_como_cliente', emitir)
    monkeypatch.setattr('servicios.auditoria.registrar_desde_request', Mock())
    resp = pc.emitir_guia_portal(request(), 81, cliente='WAIMAO', gestion_ventana='1', revision_envio=revision)
    assert resp.headers['location'].startswith('/portal/envios/81/gestion?ventana=1&')
    if revision:
        emitir.assert_called_once_with(81, 'WAIMAO', revision=revision)
    else:
        emitir.assert_not_called()
        assert 'error=' in resp.headers['location']


def test_ventana_ajena_no_expone_datos(monkeypatch):
    lookup = Mock(return_value=None)
    monkeypatch.setattr(pc, 'obtener_solicitud_de_cliente', lookup)
    resp = pc.envio_detalle(request(), 81, cliente='OTRO')
    lookup.assert_called_once_with(81, 'OTRO')
    assert resp.status_code == 303


def test_iframe_solo_origen_propio_y_portal():
    assert "frame-src 'none'" in construir_content_security_policy('x')
    csp = construir_content_security_policy('x', marco_portal=True)
    assert "frame-src 'self'" in csp
    assert "form-action 'self'" in csp and "object-src 'none'" in csp


@pytest.mark.parametrize('estado', ['SOLICITADO', 'GUIA_LISTA', 'EMITIENDO', 'VERIFICAR_COURIER', 'CANCELADO', 'REEMPLAZADO'])
def test_ventana_muestra_solo_acciones_validas(monkeypatch, estado):
    from servicios.estados_envio import presentar_estados_envio
    sol = {**pendiente(), 'estado': estado, 'dest_nombre': '<script>cliente</script>',
           'precio_tauro_ars': 74000, 'precio_final_cliente_ars': 76000, 'precio_cliente_final_ars': 999999,
           'tracking': 'TEST' if estado != 'SOLICITADO' else None,
           'tiene_label': estado == 'GUIA_LISTA', 'bultos': []}
    for name, value in dict(saldo_menu=lambda *_: None, pendientes_menu=lambda *_: {}, ayuda=lambda: {}).items():
        monkeypatch.setitem(pc.templates.env.globals, name, value)
    req = request()
    req.state.csp_nonce = 'test'
    body = pc.templates.TemplateResponse(request=req, name='portal/envio_gestion.html', context=dict(
        cliente='WAIMAO', s=presentar_estados_envio(sol), puede_editar=ed.puede_editar_solicitud(sol),
        puede_emitir=True, recoleccion=None, recoleccion_error=False, retiro_form=None)).body.decode()
    assert '&lt;script&gt;cliente&lt;/script&gt;' in body and '<script>cliente</script>' not in body
    assert '999.999' not in body
    assert ('Confirmar y emitir guía' in body) == (estado == 'SOLICITADO')
    assert ('Editar envío' in body) == (estado == 'SOLICITADO')
    assert ('Descargar PDF' in body) == (estado == 'GUIA_LISTA')
    if estado == 'SOLICITADO':
        assert 'revision_envio' in body and sol['updated_at'].isoformat() in body


def test_recoleccion_regresa_a_la_misma_guia(monkeypatch):
    crear = Mock(return_value=dict(ok=True, id=50))
    monkeypatch.setattr('servicios.recolecciones.crear', crear)
    resp = pc.recoleccion_nueva(fecha='2026-10-02', ready_time='09:00', close_time='17:00',
        bultos='1', peso_kg='4', instrucciones='', courier='DHL', solicitud_id='81', cliente='WAIMAO', gestion_ventana='1')
    assert resp.headers['location'] == '/portal/envios/81/gestion?ventana=1&retiro=1&ok=recoleccion'
    assert crear.call_args.kwargs['solicitud_id'] == 81
