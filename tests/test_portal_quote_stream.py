"""Resultados progresivos privados, sin repetir snapshots ni cargar la página."""
import asyncio
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from endpoints import portal_cliente as portal
from servicios import cotizador, cotizaciones_reseller, rate_limit


HEADERS = {'X-Requested-With': 'TauroQuoteWindow', 'Accept': 'application/x-ndjson'}
FORM = {
    'ambito': 'internacional', 'origen_pais': 'AR', 'destino_pais': 'US',
    'origen_ciudad': 'Buenos Aires', 'origen_cp_internacional': '1000',
    'destino_ciudad_internacional': 'Miami', 'destino_cp_internacional': '33101',
    'bulto_cantidad': '1', 'bulto_peso': '2', 'bulto_largo': '30',
    'bulto_ancho': '20', 'bulto_alto': '10', 'valor_declarado_usd': '100',
}


def option(carrier='dhl'):
    return {'carrier_id': carrier, 'carrier_nombre': carrier.upper(),
            'servicio': 'Servicio <seguro>', 'precio_final_ars': 100000,
            'dias_estimados': 3}


def comparison(options=(), complete=True):
    return {'opciones': list(options), 'no_disponibles': [],
            'encontrado': bool(options), 'completo': complete,
            'resumen': {'ruta': 'AR-US', 'peso_usado_kg': 2}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rate_limit, 'check_rate', lambda *a, **k: True)
    monkeypatch.setattr(cotizaciones_reseller, 'cliente_es_reseller', lambda _: False)
    # AJAX/stream must not pay for (or expose) full-page data.
    def forbidden(*a, **k):
        raise AssertionError('Un resultado parcial no consulta contexto de página')
    for name in ('obtener_rutas_frecuentes', '_operadores_cliente',
                 '_paises_con_nacional', 'opciones_provincias',
                 'referencias_paises_formulario'):
        monkeypatch.setattr(portal, name, forbidden)
    app = FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[portal.cliente_actual] = lambda: 'CLIENTE_SESION'
    with TestClient(app) as client:
        yield client


def set_stream(monkeypatch, factory):
    monkeypatch.setattr(cotizador, 'iterar_cotizar_referencia_couriers', factory, raising=False)


def test_stream_publica_parciales_escapados_y_usa_cliente_de_sesion(client, monkeypatch):
    calls = []
    privada = option()
    privada.update({
        'costo_courier_estimado': 987654321.23,
        'margen_tauro_protegido_ars': 876543210.12,
    })
    def run(**kwargs):
        calls.append(kwargs)
        yield comparison([privada], False)
        yield comparison([option(), option('otro')], True)
    set_stream(monkeypatch, run)
    response = client.post('/portal/cotizar', data={**FORM, 'cliente': 'AJENO'}, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/x-ndjson')
    assert response.headers['x-accel-buffering'] == 'no'
    assert 'private' in response.headers['cache-control']
    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert [c['complete'] for c in chunks] == [False, True]
    assert all(set(c) == {'html', 'complete'} for c in chunks)
    assert chunks[0]['html'].count('class="uq-price"') == 1
    assert chunks[1]['html'].count('class="uq-price"') == 2
    assert 'Servicio &lt;seguro&gt;' in response.text
    assert all('costo' not in c['html'] and 'markup' not in c['html'] for c in chunks)
    assert '987654321' not in response.text and '876543210' not in response.text
    assert calls[0]['cliente'] == 'CLIENTE_SESION'
    assert 'cajas=' in chunks[0]['html'] and 'valor_cotizado=100' in chunks[0]['html']


def test_stream_no_expone_error_interno_y_conserva_tarifa_recibida(client, monkeypatch):
    def run(**kwargs):
        yield comparison([option()], False)
        raise ValueError('SECRET_ACCOUNT_PASSWORD=hidden')
    set_stream(monkeypatch, run)
    response = client.post('/portal/cotizar', data=FORM, headers=HEADERS)
    chunks = [json.loads(line) for line in response.text.splitlines()]
    assert chunks[-1]['complete'] is True
    assert 'No pudimos completar' in chunks[-1]['html']
    assert 'class="uq-price"' in chunks[-1]['html']
    assert 'SECRET' not in response.text


def test_sin_habilitados_finaliza_sin_opciones(client, monkeypatch):
    set_stream(monkeypatch, lambda **k: iter([comparison()]))
    response = client.post('/portal/cotizar', data=FORM, headers=HEADERS)
    chunk = json.loads(response.text)
    assert chunk['complete'] is True
    assert 'Ningún courier' in chunk['html']
    assert 'class="uq-price"' not in chunk['html']


def test_reseller_no_repite_snapshot_de_la_tarifa_parcial(client, monkeypatch):
    monkeypatch.setattr(cotizaciones_reseller, 'cliente_es_reseller', lambda _: True)
    written = []
    def save(cliente, *, opciones, **kwargs):
        written.extend((cliente, o['carrier_id']) for o in opciones)
        return [{**o, 'reseller_quote_id': 'RQ-' + o['carrier_id']} for o in opciones]
    monkeypatch.setattr(cotizaciones_reseller, 'guardar_opciones', save)
    set_stream(monkeypatch, lambda **k: iter([
        comparison([option()], False), comparison([option(), option('otro')], True),
    ]))
    response = client.post('/portal/cotizar', data=FORM, headers=HEADERS)
    assert written == [('CLIENTE_SESION', 'dhl'), ('CLIENTE_SESION', 'otro')]
    assert all('RQ-dhl' in json.loads(line)['html'] for line in response.text.splitlines())


def test_ajax_html_conserva_fallback_y_evitar_contexto_innecesario(client, monkeypatch):
    monkeypatch.setattr(portal, 'cotizar_referencia_couriers', lambda **k: comparison([option()]))
    response = client.post('/portal/cotizar', data=FORM, headers={'X-Requested-With': 'TauroQuoteWindow'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert 'data-quote-response' in response.text


def test_datos_invalidos_no_inician_apis_y_conservan_error_legible(client, monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError('No llamar operadores con cajas inválidas')
    set_stream(monkeypatch, forbidden)
    response = client.post('/portal/cotizar', data={**FORM, 'bulto_peso': '0'}, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    assert 'uq-error' in response.text


def test_primera_tarifa_sale_antes_de_ejecutar_el_resto_y_cierra_consumidor(monkeypatch):
    steps = []
    def run(**kwargs):
        try:
            yield comparison([option()], False)
            steps.append('second')
            yield comparison([option(), option('otro')], True)
        finally:
            steps.append('closed')
    set_stream(monkeypatch, run)
    request = Request({'type':'http', 'method':'POST', 'path':'/portal/cotizar', 'headers':[]})
    response = portal._stream_cotizacion_internacional(
        request, cliente='A', parametros={}, form=FORM,
        paquetes=[], es_reseller=False,
    )
    async def consume():
        first = await anext(response.body_iterator)
        assert json.loads(first)['complete'] is False
        assert steps == []
        await response.body_iterator.aclose()
    asyncio.run(consume())
    assert steps == ['closed']


def test_cancelar_durante_next_no_cierra_generador_en_ejecucion(monkeypatch):
    """El ASGI se libera; el HTTP activo termina bajo su timeout y se autocierra."""
    entro_en_red = threading.Event()
    liberar_red = threading.Event()
    cerrado = threading.Event()
    cancelado = []

    def run(**kwargs):
        cancelado.append(kwargs['_cancel_event'])
        try:
            yield comparison([option()], False)
            entro_en_red.set()
            # Representa requests ya dentro de DNS/connect/read: no es matable.
            liberar_red.wait(timeout=2)
            yield comparison([option(), option('otro')], True)
        finally:
            cerrado.set()

    set_stream(monkeypatch, run)
    request = Request({'type':'http', 'method':'POST', 'path':'/portal/cotizar', 'headers':[]})
    response = portal._stream_cotizacion_internacional(
        request, cliente='A', parametros={}, form=FORM,
        paquetes=[], es_reseller=False,
    )

    async def consume():
        first = await anext(response.body_iterator)
        assert json.loads(first)['complete'] is False
        pending = asyncio.create_task(anext(response.body_iterator))
        assert await asyncio.to_thread(entro_en_red.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cancelado[0].is_set()
        # Nadie llama close() desde el event loop mientras next() sigue activo.
        assert not cerrado.is_set()
        liberar_red.set()
        assert await asyncio.to_thread(cerrado.wait, 1)

    asyncio.run(consume())


def test_stream_sigue_protegido_por_la_sesion():
    app = FastAPI()
    app.include_router(portal.router)
    with TestClient(app) as client:
        response = client.post('/portal/cotizar', data=FORM, headers=HEADERS, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/portal/login'
