"""Documentos: acceso propio, lectura sin efectos y render aislado."""
from io import BytesIO
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from endpoints import portal_cliente as portal
from servicios import documentos_portal as docs
from servicios import solicitudes_guia as guias


def pdf_ejemplo():
    salida = BytesIO()
    c = Canvas(salida, pagesize=(200, 300))
    c.drawString(20, 240, 'TAURO - DOCUMENTO DE PRUEBA')
    c.showPage()
    c.drawString(20, 240, 'SEGUNDA PAGINA')
    c.save()
    return salida.getvalue()


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[portal.cliente_actual] = lambda: 'WAIMAO'
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize('tipo,modulo,getter,valor', [
    ('guia', 'servicios.solicitudes_guia', 'obtener_label_pdf', b'%PDF-1.4'),
    ('invoice', 'servicios.solicitudes_guia', 'obtener_factura_comercial_pdf', b'%PDF-1.4'),
    ('factura', 'servicios.facturacion_clientes', 'get_factura_cliente_pdf', (b'%PDF-1.4', 'fc.pdf')),
    ('factura-legacy', 'servicios.cuenta_corriente', 'get_factura_pdf', (b'%PDF-1.4', 'fc.pdf')),
    ('pago', 'servicios.cuenta_corriente', 'get_comprobante', (b'%PDF-1.4', 'application/pdf', 'pago.pdf')),
])
def test_contenido_y_miniatura_exigen_propietario(client, monkeypatch, tipo, modulo, getter, valor):
    llamadas = []
    def leer(id, *, cliente_id):
        llamadas.append((id, cliente_id))
        return valor if id == 1 and cliente_id == 'WAIMAO' else None
    monkeypatch.setattr(modulo + '.' + getter, leer)
    monkeypatch.setattr(portal, 'miniatura_documento', lambda doc: b'jpeg-test')
    for sufijo in ('contenido', 'miniatura'):
        response = client.get(f'/portal/documentos/{tipo}/1/{sufijo}?cliente_id=AJENO')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert client.get(f'/portal/documentos/{tipo}/2/{sufijo}').status_code == 404
    assert all(cliente == 'WAIMAO' for _, cliente in llamadas)


def test_vista_previa_no_registra_descarga_ni_asigna_numero(client, monkeypatch):
    monkeypatch.setattr(guias, 'obtener_label_pdf', lambda id, cliente_id: pdf_ejemplo())
    def prohibido(*args, **kwargs):
        pytest.fail('Una vista previa intentó modificar una guía')
    monkeypatch.setattr(portal, 'marcar_guia_descargada_cliente', prohibido)
    monkeypatch.setattr(portal, 'preparar_documentos_envio_portal', prohibido)
    assert client.get('/portal/documentos/guia/1/contenido').status_code == 200
    assert client.get('/portal/documentos/guia/1/miniatura').status_code == 200


def test_sin_sesion_no_obtiene_documento(monkeypatch):
    app = FastAPI(); app.include_router(portal.router)
    monkeypatch.setattr(portal, 'obtener_documento', lambda *a: pytest.fail('lectura sin sesión'))
    with TestClient(app, follow_redirects=False) as client:
        for sufijo in ('contenido', 'miniatura'):
            response = client.get('/portal/documentos/guia/1/' + sufijo)
            assert response.status_code in (302, 303, 401, 403)


@pytest.mark.parametrize('tipo', ['courier', 'pago-operador', 'factura-courier', 'https://example.com'])
def test_tipos_internos_y_urls_no_resuelven(tipo):
    assert docs.obtener_documento(tipo, 1, 'WAIMAO') is None


@pytest.mark.parametrize('cliente', ['', '  ', None])
def test_no_activa_el_modo_administrador(cliente):
    assert docs.obtener_documento('guia', 1, cliente) is None


@pytest.mark.parametrize('url', ['/admin/facturas/1/pdf', 'https://example.com/portal/pagos/1/comprobante', '/portal/pagos/1/comprobante?cliente=AJENO', '/portal/facturas/0/pdf'])
def test_descriptor_rechaza_rutas_no_permitidas(url):
    assert docs.descriptor_documento(url) is None


def test_miniatura_pdf_real_es_liviana_y_cacheada(monkeypatch):
    documento = docs.Documento(pdf_ejemplo(), 'application/pdf')
    miniatura = docs.miniatura_documento(documento)
    with Image.open(BytesIO(miniatura)) as im:
        assert im.format == 'JPEG'
        assert im.width <= 160 and im.height <= 208
    assert len(miniatura) < 32_000
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('repitió render'))
    assert docs.miniatura_documento(documento) == miniatura


@pytest.mark.parametrize('formato', ['PNG', 'JPEG', 'WEBP'])
def test_miniaturas_imagenes(formato):
    output = BytesIO(); Image.new('RGB', (600, 900), 'violet').save(output, formato)
    data = output.getvalue()
    thumbnail = docs.miniatura_documento(docs.Documento(data, docs.tipo_contenido(data)))
    with Image.open(BytesIO(thumbnail)) as img:
        assert img.height <= 208 and img.width <= 160


def test_documento_danado_no_rompe_servidor(client, monkeypatch):
    monkeypatch.setattr(guias, 'obtener_label_pdf', lambda id, cliente_id: b'%PDF-incorrecto')
    assert client.get('/portal/documentos/guia/1/miniatura').status_code == 422
    monkeypatch.setattr(guias, 'obtener_label_pdf', lambda id, cliente_id: b'<script>bad</script>')
    assert client.get('/portal/documentos/guia/1/contenido').status_code == 422


def test_timeout_de_render_es_recuperable(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('worker', 8)
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(ValueError, match='no disponible'):
        docs.miniatura_documento(docs.Documento(b'%PDF-timeout', 'application/pdf'))
    assert docs._render_slots.acquire(blocking=False)
    docs._render_slots.release()
