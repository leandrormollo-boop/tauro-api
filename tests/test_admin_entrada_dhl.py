import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from endpoints import admin
from servicios import bandeja_facturas_dhl as bandeja
from servicios.entrada_facturas_dhl import preparar_factura_dhl_manual
from test_entrada_facturas_dhl import ejemplo


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    monkeypatch.setattr(admin, '_is_auth', lambda token: token == 'valido')
    monkeypatch.setitem(admin.templates.env.globals, 'pendientes_admin', lambda: 0)
    monkeypatch.setitem(admin.templates.env.globals, 'alertas_guias_reemplazadas', lambda: 0)


def request():
    r = Request({'type': 'http', 'method': 'GET', 'path': '/admin/conciliacion-couriers/entrada-dhl',
        'raw_path': b'/', 'query_string': b'', 'headers': [], 'scheme': 'http',
        'server': ('testserver', 80), 'client': ('testclient', 1234), 'root_path': ''})
    r.state.csp_nonce = 'test'
    return r


def entrada(estado='PARA_REVISION'):
    return {'id': 1, 'numero_esperado': '1700A00000001', 'cuit_esperado': '20123456786',
        'archivo_nombre': '<script>no-ejecutar</script>.pdf', 'archivo_sha256': 'a'*64,
        'estado': estado, 'extraccion': ejemplo() if estado in ('PARA_REVISION','IMPORTADA') else None,
        'observaciones': ['<script>dato-del-pdf</script>'], 'error_lectura': None, 'intentos': 1,
        'revision_sha256': 'b'*64, 'lector_version': bandeja.LECTOR_VERSION,
        'factura_id': 2, 'created_at': datetime.now(timezone.utc)}


def test_carga_manual_no_inventa_mail():
    datos = preparar_factura_dhl_manual(ejemplo(), archivo_pdf=b'%PDF-test', archivo_nombre='test.pdf').datos_registro
    assert datos['metadatos_origen']['canal'] == 'admin_pdf_dhl'
    assert 'mensaje_origen_id' not in datos
    assert 'cuenta_sha256' not in datos['metadatos_origen']


@pytest.mark.parametrize('fn,args', [
    (admin.admin_bandeja_dhl, {'request': None}),
    (admin.admin_detalle_dhl, {'request': None, 'entrada_id': 1}),
    (admin.admin_pdf_entrada_dhl, {'entrada_id': 1}),
    (admin.admin_leer_dhl, {'entrada_id': 1}),
    (admin.admin_importar_dhl, {'entrada_id': 1}),
])
def test_todas_las_rutas_exigen_admin_antes_de_leer_o_escribir(fn, args):
    respuesta = fn(**args, admin_token='invalido')
    assert respuesta.status_code == 303 and '/login' in respuesta.headers['location']


def test_upload_sin_auth_no_lee_formulario():
    assert asyncio.run(admin.admin_recibir_dhl(None, admin_token='invalido')).status_code == 303


@pytest.mark.parametrize('fn', [admin.admin_leer_dhl, admin.admin_importar_dhl])
def test_csrf_se_exige_antes_de_escribir(fn):
    assert fn(entrada_id=1, csrf_dhl='', admin_token='valido').status_code == 403
    assert fn(entrada_id=2, csrf_dhl=admin._csrf_dhl('1'), admin_token='valido').status_code == 403


def test_upload_exige_csrf_antes_de_leer_pdf():
    async def form():
        return {'csrf_dhl': 'invalido'}
    assert asyncio.run(admin.admin_recibir_dhl(SimpleNamespace(form=form), admin_token='valido')).status_code == 403


def test_token_csrf_vence_y_no_es_auth_token(monkeypatch):
    monkeypatch.setattr(admin.time, 'time', lambda: 10000)
    token = admin._csrf_dhl('nueva')
    assert admin._csrf_dhl_valido(token, 'nueva')
    assert admin._ADMIN_TOKEN not in token
    assert not admin._csrf_dhl_valido(token, 'otro')
    assert not admin._csrf_dhl_valido(token + 'x', 'nueva')
    monkeypatch.setattr(admin.time, 'time', lambda: 14000)
    assert not admin._csrf_dhl_valido(token, 'nueva')


@pytest.mark.parametrize('estado', ['RECIBIDA','REVISION_MANUAL','REINTENTAR','PARA_REVISION','IMPORTADA'])
def test_detalle_renderiza_estados_escapa_pdf_y_no_cachea(monkeypatch, estado):
    monkeypatch.setattr(bandeja, 'obtener_entrada_dhl', lambda _id: entrada(estado))
    respuesta = admin.admin_detalle_dhl(request(), 1, admin_token='valido')
    html = respuesta.body.decode()
    assert 'private, no-store' == respuesta.headers['cache-control']
    assert '<script>no-ejecutar' not in html
    assert '&lt;script&gt;no-ejecutar' in html
    assert ('Registrar factura, sin aplicar cargos' in html) == (estado == 'PARA_REVISION')
    assert ('name="csrf_dhl"' in html) == (estado != 'IMPORTADA')


def test_bandeja_renderiza_sin_promesa_de_gmail_activo(monkeypatch):
    monkeypatch.setattr(bandeja, 'listar_entradas_dhl', lambda **kw: {'items': [entrada()], 'pagina': 1, 'hay_mas': False})
    respuesta = admin.admin_bandeja_dhl(request(), admin_token='valido')
    assert 'Gmail todavía no está conectado' in respuesta.body.decode()
    assert respuesta.headers['cache-control'] == 'private, no-store'


def test_pdf_se_descarga_privado_con_nombre_controlado(monkeypatch):
    monkeypatch.setattr(bandeja, 'obtener_entrada_dhl', lambda *a, **kw: entrada() | {'archivo_pdf': b'%PDF-test'})
    respuesta = admin.admin_pdf_entrada_dhl(1, admin_token='valido')
    assert respuesta.body == b'%PDF-test'
    assert respuesta.headers['content-disposition'] == 'attachment; filename="DHL-entrada-1.pdf"'
    assert respuesta.headers['cache-control'] == 'private, no-store'
    assert 'sandbox' in respuesta.headers['content-security-policy']


def test_importar_no_acepta_importes_ni_cliente_desde_formulario(monkeypatch):
    llamadas = []
    def guardar(entrada_id, **kw):
        llamadas.append((entrada_id, kw))
        return {'id': 10}
    monkeypatch.setattr(bandeja, 'importar_entrada_dhl', guardar)
    respuesta = admin.admin_importar_dhl(1, csrf_dhl=admin._csrf_dhl('1'), revision_sha256='b'*64,
        revisado='si', admin_token='valido')
    assert respuesta.status_code == 303
    assert llamadas == [(1, {'revision_sha256': 'b'*64, 'revision_confirmada': True, 'actor': 'admin'})]


def test_no_encontrado_devuelve_404(monkeypatch):
    monkeypatch.setattr(bandeja, 'obtener_entrada_dhl', lambda *a, **kw: None)
    assert admin.admin_pdf_entrada_dhl(1, admin_token='valido').status_code == 404
    assert admin.admin_detalle_dhl(request(), 1, admin_token='valido').status_code == 404


def test_lectura_obsoleta_no_ofrece_importar_y_si_releer(monkeypatch):
    monkeypatch.setattr(bandeja, 'obtener_entrada_dhl', lambda *a: entrada() | {'lector_version': bandeja.LECTOR_VERSION-1})
    html = admin.admin_detalle_dhl(request(), 1, admin_token='valido').body.decode()
    assert 'ya no está vigente' in html
    assert 'Reintentar lectura' in html
    assert 'Registrar factura, sin aplicar cargos' not in html
