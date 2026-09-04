import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from test_admin_entrada_dhl import auth, request
from endpoints import admin
from servicios import conciliacion_couriers as conciliacion
from servicios import revision_financiera_courier as financiera


def formulario(datos):
    async def form():
        return datos
    return SimpleNamespace(form=form)


def enviar(datos, factura_id=7, token='valido'):
    return asyncio.run(admin.admin_revision_financiera_courier(formulario(datos), factura_id, admin_token=token))


def test_revision_y_respaldo_requieren_admin_antes_de_cualquier_io():
    assert asyncio.run(admin.admin_revision_financiera_courier(None, 7, admin_token='invalido')).status_code == 303
    assert admin.admin_respaldo_financiero_courier(7, admin_token='invalido').status_code == 303


@pytest.mark.parametrize('csrf', ['', 'invalido', 'otra_factura', 'otra_accion'])
def test_csrf_financiero_es_especifico_y_no_lee_pdf(csrf):
    token = {'otra_factura': admin._csrf_dhl('financiera:8'),
             'otra_accion': admin._csrf_dhl('7')}.get(csrf, csrf)
    assert enviar({'csrf_financiero': token, 'respaldo_pdf': object()}).status_code == 403


def test_post_solo_traslada_campos_aprobados_y_actor_admin(monkeypatch):
    llamadas = []
    monkeypatch.setattr(financiera, 'aprobar_revision_financiera', lambda *a, **kw: llamadas.append((a, kw)))
    respuesta = enviar({'csrf_financiero': admin._csrf_dhl('financiera:7'),
        'archivo_sha256': 'a'*64, 'tipo_cambio_ars': '1600', 'fuente': 'COMPROBANTE',
        'motivo': 'Comprobante sintético verificado', 'confirmada': 'si',
        'respaldo_pdf': UploadFile(BytesIO(b'%PDF-sintetico'), filename='test.pdf'),
        'cliente_id': 'ignorar', 'actor': 'cliente', 'monto_ars': '999'})
    assert respuesta.status_code == 303 and 'ok=revision_financiera' in respuesta.headers['location']
    assert llamadas == [((7,), {'tipo_cambio_ars': '1600', 'fuente': 'COMPROBANTE',
        'motivo': 'Comprobante sintético verificado', 'archivo_sha256': 'a'*64,
        'confirmada': True, 'actor': 'admin', 'respaldo_pdf': b'%PDF-sintetico'})]


def test_error_financiero_se_muestra_sin_afirmar_exito(monkeypatch):
    def fallar(*a, **kw):
        raise conciliacion.ConciliacionCourierError('Falta revisar el respaldo')
    monkeypatch.setattr(financiera, 'aprobar_revision_financiera', fallar)
    respuesta = enviar({'csrf_financiero': admin._csrf_dhl('financiera:7')})
    assert 'error=' in respuesta.headers['location'] and 'ok=' not in respuesta.headers['location']


def test_pdf_respaldo_es_privado_y_no_encontrado(monkeypatch):
    monkeypatch.setattr(financiera, 'obtener_respaldo_financiero', lambda _id: b'%PDF-sintetico')
    r = admin.admin_respaldo_financiero_courier(7, admin_token='valido')
    assert r.body == b'%PDF-sintetico'
    assert r.headers['cache-control'] == 'private, no-store'
    assert 'attachment;' in r.headers['content-disposition']
    assert 'sandbox' in r.headers['content-security-policy']
    monkeypatch.setattr(financiera, 'obtener_respaldo_financiero', lambda _id: None)
    assert admin.admin_respaldo_financiero_courier(7, admin_token='valido').status_code == 404


def factura(requerida=True, aprobada=False):
    return {'id': 7, 'numero': 'SINTETICA', 'courier': 'DHL', 'tipo_documento': 'FC',
        'moneda': 'USD', 'total': Decimal('10'), 'estado': 'PARCIAL', 'tiene_evidencia': True,
        'archivo_sha256': 'a'*64, 'revision_financiera_requerida': requerida,
        'tipos_cambio_documentales': [Decimal('1500.000000')],
        'revision_financiera': {'tipo_cambio_ars': Decimal('1600'), 'fuente': 'COMPROBANTE',
            'motivo': '<script>no-ejecutar</script>', 'aprobado_por': 'admin',
            'aprobado_at': datetime.now(timezone.utc)} if aprobada else None,
        'items': [{'id': 1, 'linea_numero': 1, 'tracking_raw': '0123456789',
            'concepto_tipo': 'FLETE', 'descripcion': 'Sintético', 'moneda': 'USD',
            'importe': Decimal('10'), 'importe_ars': Decimal('15000'),
            **({'importe_conciliacion_ars': Decimal('16000')} if aprobada else {}),
            'remanente': Decimal('0'), 'peso_facturado_kg': 2, 'peso_base': 'REAL',
            'matches': [{'id': 3, 'match_estado': 'PROPUESTO', 'cliente_id': 'TEST',
                'solicitud_id': 1, 'metodo': 'EXACTO_TRACKING', 'monto_asignado': Decimal('10')}]}]}


@pytest.mark.parametrize('requerida,aprobada', [(True, False), (True, True), (False, False)])
def test_pantalla_bloquea_acciones_y_separa_importes(monkeypatch, requerida, aprobada):
    monkeypatch.setattr(conciliacion, 'obtener_factura_courier_control', lambda _id: factura(requerida, aprobada))
    r = admin.admin_factura_courier_detalle(request(), 7, admin_token='valido')
    html = r.body.decode()
    pendiente = requerida and not aprobada
    assert ('Aprobar TC, sin aplicar cargos' in html) == pendiente
    assert ('1500.000000 ARS por USD' in html) == pendiente
    assert ('Confirmar y calcular' in html) == (not pendiente)
    assert ('/matches/3/confirmar' in html) == (not pendiente)
    assert ('(TC aprobado)' in html) == aprobada
    assert '<script>no-ejecutar' not in html
    assert r.headers['cache-control'] == 'private, no-store'
