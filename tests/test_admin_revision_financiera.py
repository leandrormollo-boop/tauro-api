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
from servicios import referencias_tauro_2026 as referencias_tauro


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
    resultado = {'id': 7, 'numero': 'SINTETICA', 'courier': 'DHL', 'tipo_documento': 'FC',
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
                'solicitud_id': 1, 'metodo': 'EXACTO_TRACKING',
                'monto_asignado': Decimal('10'), 'monto_asignado_ars': Decimal('15000'),
                'cantidad_lineas': 1}]}]}
    resultado['envios_facturados'] = [{
        'tracking_raw': '0123456789', 'fecha_envio': None,
        'items': resultado['items'], 'total_importe': Decimal('10'),
        'total_importe_ars': Decimal('15000'),
        'total_conciliacion_ars': Decimal('16000'),
        'remanente': Decimal('0'), 'peso_facturado_kg': 2,
        'peso_base': 'REAL', 'matches': resultado['items'][0]['matches'],
        'item_referencia_id': 1,
    }]
    return resultado


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


def test_pantalla_distingue_match_tauro_de_envio_importado(monkeypatch):
    datos = factura(requerida=False, aprobada=False)
    datos['cantidad_guias_referenciadas_tauro_2026'] = 1
    guia = datos['envios_facturados'][0]
    guia['matches'] = []
    guia['referencias_tauro_2026'] = [{
        'nro_fc': '0700A00918787', 'tracking': '0123456789',
        'empresa': 'DHL', 'cliente': 'JONA', 'concepto': 'FLETE',
        'remitente': 'JONA', 'destinatario': 'JONATHAN LORENZO',
        'pais': 'ARG - CR', 'fecha_envio': '2026-04-27',
        'fuente_fila': 1615,
    }]
    monkeypatch.setattr(
        conciliacion, 'obtener_factura_courier_control', lambda _id: datos
    )
    r = admin.admin_factura_courier_detalle(
        request(), 7, admin_token='valido'
    )
    html = r.body.decode()
    assert '1 identificadas en TAURO 2026' in html
    assert 'Match TAURO 2026' in html
    assert 'JONA' in html
    assert 'Envío no importado al portal' in html
    assert 'No coincide con el portal ni con TAURO 2026' not in html


def test_cruce_tauro_2026_exige_admin_y_csrf_especifico(monkeypatch):
    assert admin.admin_sincronizar_factura_tauro_2026(
        7, admin_token='invalido'
    ).status_code == 303
    assert admin.admin_sincronizar_factura_tauro_2026(
        7, csrf_tauro_2026='invalido', admin_token='valido'
    ).status_code == 403
    llamadas = []
    monkeypatch.setattr(
        referencias_tauro, 'sincronizar_referencias_factura',
        lambda factura_id, actor: llamadas.append((factura_id, actor))
            or {'estado': 'OK'},
    )
    respuesta = admin.admin_sincronizar_factura_tauro_2026(
        7, csrf_tauro_2026=admin._csrf_dhl('tauro-2026:7'),
        admin_token='valido',
    )
    assert llamadas == [(7, 'admin')]
    assert respuesta.status_code == 303
    assert 'ok=tauro_2026_ok' in respuesta.headers['location']
