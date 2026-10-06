"""Adjuntar un comprobante desde Admin no vuelve a registrar ni aprobar el pago."""
from datetime import date
from decimal import Decimal
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from endpoints import admin
from servicios import cuenta_corriente as cc
from test_pagos_documentales_postgres import DATABASE_URL, cuenta_db

URL = '/admin/pagos/23/adjuntar-comprobante'


@pytest.fixture
def web(monkeypatch):
    pago = dict(id=23, cliente_id='DEMO', fecha='06/10/2026', monto_ars=Decimal('1000000.00'),
                referencia='DEMO', estado_label='Acreditado', tiene_comprobante=False,
                imputacion_label='Pago imputado a 2 envíos')
    app = FastAPI()
    app.include_router(admin.router)
    @app.middleware('http')
    async def nonce(request, call_next):
        request.state.csp_nonce = 'test'
        return await call_next(request)
    monkeypatch.setattr(admin, '_is_auth', lambda token: token == 'sesion-de-prueba')
    lookup = Mock(side_effect=lambda ident: pago if ident == 23 else None)
    monkeypatch.setattr(admin, '_pago_comprobante_admin', lookup)
    monkeypatch.setattr(admin, 'check_rate', lambda *a, **k: True)
    for key in ('pendientes_admin', 'alertas_guias_reemplazadas', 'pagos_pendientes_count', 'productos_pendientes_count'):
        monkeypatch.setitem(admin.templates.env.globals, key, lambda: 0)
    guardar = Mock(return_value=True)
    monkeypatch.setattr(cc, 'adjuntar_comprobante_pago_admin', guardar)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set('admin_token', 'sesion-de-prueba')
    return client, pago, guardar, lookup


def post(client, *, csrf=None, path=URL, content=b'%PDF-1.4\nprueba', extra=None):
    data = dict(csrf_comprobante=admin._csrf_dhl('pago-comprobante:23:DEMO') if csrf is None else csrf)
    data.update(extra or {})
    return client.post(path, data=data, files={'comprobante': ('pago.pdf', content, 'application/pdf')})


def test_formulario_muestra_cuenta_pago_y_regreso_correctos(web):
    client, _, _, _ = web
    r = client.get(URL)
    assert r.status_code == 200 and r.headers['cache-control'] == 'private, no-store'
    assert '$ 1.000.000,00' in r.text and 'Pago imputado a 2 envíos' in r.text
    assert '/admin/clientes/DEMO?vista=pagos#pago-23' in r.text
    assert 'name="csrf_comprobante"' in r.text and 'name="comprobante"' in r.text
    assert 'name="monto_ars"' not in r.text and 'name="estado"' not in r.text
    assert 'name="cliente_id"' not in r.text


@pytest.mark.parametrize('method', ['get', 'post'])
@pytest.mark.parametrize('cookies', [{}, {'token': 'solo-cliente'}, {'admin_token': 'sesion-vencida'}])
def test_requiere_sesion_admin_antes_de_leer_o_escribir(web, method, cookies):
    client, _, guardar, lookup = web
    client.cookies.clear()
    client.cookies.update(cookies)
    r = getattr(client, method)(URL)
    assert r.status_code == 303 and r.headers['location'] == '/admin/login'
    lookup.assert_not_called()
    guardar.assert_not_called()


@pytest.mark.parametrize('method', ['get', 'post'])
def test_pago_inexistente_es_404(web, method):
    client, _, guardar, _ = web
    assert getattr(client, method)(URL.replace('/23/', '/24/')).status_code == 404
    guardar.assert_not_called()


@pytest.mark.parametrize('scope', ['', 'pago-comprobante:24:DEMO', 'pago-comprobante:23:OTRO'])
def test_csrf_faltante_o_de_otro_pago_cuenta_no_adjunta(web, scope):
    client, _, guardar, _ = web
    r = post(client, csrf=admin._csrf_dhl(scope) if scope else '')
    assert r.status_code == 303 and 'error=' in r.headers['location']
    guardar.assert_not_called()


def test_resuelve_propietario_en_servidor_y_actor_admin_no_en_form(web):
    client, _, guardar, _ = web
    r = post(client, extra={'cliente_id': 'OTRO', 'admin_user': 'cliente', 'monto_ars': '99999'})
    assert r.status_code == 303 and r.headers['location'] == URL + '?adjuntado=1'
    guardar.assert_called_once_with('DEMO', 23, b'%PDF-1.4\nprueba', 'pago.pdf', admin_user='admin')


@pytest.mark.parametrize('content', [b'', b'%PDF' + b'x' * cc.COMPROBANTE_MAX_BYTES])
def test_vacio_o_grande_no_llega_al_servicio(web, content):
    client, _, guardar, _ = web
    assert 'error=' in post(client, content=content).headers['location']
    guardar.assert_not_called()


@pytest.mark.parametrize('error', [LookupError(), ValueError('Ya tiene otro comprobante'), RuntimeError('detalle privado')])
def test_conflicto_y_error_interno_no_muestran_exito(web, error):
    client, _, guardar, _ = web
    guardar.side_effect = error
    r = post(client)
    if isinstance(error, LookupError):
        assert r.status_code == 404
    else:
        assert r.status_code == 303 and 'error=' in r.headers['location']
        message = parse_qs(urlsplit(r.headers['location']).query)['error'][0]
        assert 'detalle privado' not in message
        assert 'adjuntado' not in r.headers['location']


def test_reintento_exitoso_no_ofrece_reemplazar_archivo(web):
    client, pago, guardar, _ = web
    guardar.return_value = False
    r = post(client, path=URL+'?volver=pendientes')
    assert r.headers['location'] == URL+'?volver=pendientes&adjuntado=1'
    pago['tiene_comprobante'] = True
    html = client.get(r.headers['location']).text
    assert 'Comprobante adjuntado al pago #23' in html
    assert 'name="comprobante"' not in html
    assert '/admin/pagos/23/comprobante' in html and 'Pagos por verificar' in html
    assert 'https://ajeno.example' not in client.get(URL+'?volver=https://ajeno.example').text


def test_limite_no_escribe(web, monkeypatch):
    client, _, guardar, _ = web
    monkeypatch.setattr(admin, 'check_rate', lambda *a, **k: False)
    assert 'error=' in post(client).headers['location']
    guardar.assert_not_called()


@pytest.mark.parametrize('headers', [{'Origin': 'https://ajeno.example'}, {'Sec-Fetch-Site': 'cross-site'}])
def test_csrf_global_bloquea_origen_ajeno_aun_con_token_valido(web, monkeypatch, headers):
    import main
    from servicios import auditoria
    client, _, guardar, lookup = web
    monkeypatch.setattr(main, '_host_permitido', lambda host: host == 'testserver')
    monkeypatch.setattr(auditoria, 'registrar_desde_request', Mock())
    client.app.middleware('http')(main.headers_de_seguridad)
    r = client.post(URL, headers=headers, data={'csrf_comprobante': admin._csrf_dhl('pago-comprobante:23:DEMO')},
                    files={'comprobante': ('pago.pdf', b'%PDF-1.4', 'application/pdf')})
    assert r.status_code == 403
    lookup.assert_not_called()
    guardar.assert_not_called()


def test_enlaces_en_ficha_y_pendientes(web):
    from test_admin_pago_revision_ui import _render_pago
    assert '/admin/pagos/99/adjuntar-comprobante' in _render_pago(en_revision=False)
    client, _, _, _ = web
    from starlette.requests import Request
    req = Request({'type': 'http', 'path': '/admin/pagos/pendientes', 'headers': [], 'query_string': b''})
    req.state.csp_nonce = 'test'
    p = dict(id=23, cliente_id='DEMO', monto_ars=1000000, tiene_comprobante=False)
    template = admin.templates.env.get_template('admin/pagos_pendientes.html')
    html = template.render(request=req, pagos=[p])
    assert URL+'?volver=pendientes' in html
    p['tiene_comprobante'] = True
    assert URL not in template.render(request=req, pagos=[p])


@pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL local aislado')
def test_http_real_adjunta_y_se_ve_en_admin_y_cliente_sin_cambio_contable(cuenta_db, monkeypatch):
    from servicios import experiencia_cuenta as ec
    from endpoints import portal_cliente as pc
    monkeypatch.setattr(admin, 'get_conn', cuenta_db)
    monkeypatch.setattr(ec, 'get_conn', cuenta_db)
    monkeypatch.setattr(admin, '_is_auth', lambda token: token == 'sesion-de-prueba')
    monkeypatch.setattr(admin, 'check_rate', lambda *a, **k: True)
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes(cliente_id,email) VALUES ('DEMO','demo@example.invalid')")
            cur.execute("INSERT INTO pagos(cliente_id,fecha,monto_ars,estado,referencia) VALUES ('DEMO',CURRENT_DATE,1000000,'APROBADO','REFERENCIA-PRUEBA') RETURNING id")
            ident = cur.fetchone()['id']
    app = FastAPI(); app.include_router(admin.router); app.include_router(pc.router)
    app.dependency_overrides[pc.cliente_actual] = lambda: 'DEMO'
    client = TestClient(app, follow_redirects=False)
    client.cookies.set('admin_token', 'sesion-de-prueba')
    before = cc.total_pagado('DEMO')
    path = f'/admin/pagos/{ident}/adjuntar-comprobante'
    r = client.post(path, data={'csrf_comprobante': admin._csrf_dhl(f'pago-comprobante:{ident}:DEMO')},
                    files={'comprobante': ('evidencia.pdf', b'%PDF-1.4\nprueba-admin', 'application/pdf')})
    assert r.status_code == 303 and r.headers['location'] == path+'?adjuntado=1'
    assert cc.total_pagado('DEMO') == before
    for prefix in ('admin', 'portal'):
        r = client.get(f'/{prefix}/pagos/{ident}/comprobante')
        assert r.status_code == 200 and r.content == b'%PDF-1.4\nprueba-admin'
        assert r.headers['content-type'] == 'application/pdf'
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM pagos")
            assert cur.fetchone()['n'] == 1
