from contextlib import contextmanager
import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

from servicios import rutas_frecuentes as rf


@pytest.fixture
def history(monkeypatch):
    url = os.getenv('TAURO_TEST_DATABASE_URL')
    if not url:
        pytest.skip('requiere PostgreSQL aislado')
    schema = 'test_rutas_' + uuid.uuid4().hex
    admin = psycopg2.connect(url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        cur.execute('''CREATE TABLE rutas(ruta_id TEXT PRIMARY KEY, origen_pais TEXT);
            CREATE TABLE solicitudes_guia(id SERIAL PRIMARY KEY, cliente_id TEXT,
                remitente_pais TEXT, destino_pais TEXT, ruta_id TEXT, estado TEXT,
                tracking TEXT, test BOOLEAN DEFAULT FALSE, visible_cliente BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE envios(solicitud_id INTEGER, cliente_id TEXT, estado TEXT);
            CREATE TABLE solicitudes_guia_reemisiones(solicitud_anterior_id INTEGER,
                cliente_id TEXT, estado TEXT);''')

    @contextmanager
    def read_only():
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.set_session(readonly=True)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def add(origin='CN', destination='AR', client='WAIMAO', days=1, **kwargs):
        row = dict(cliente_id=client, remitente_pais=origin, destino_pais=destination,
                   estado='DESPACHADO', tracking=uuid.uuid4().hex, test=False, visible_cliente=True,
                   ruta_id=None)
        row.update(kwargs)
        with admin.cursor() as cur:
            columns = ', '.join(row)
            placeholders = ', '.join(['%s'] * len(row))
            cur.execute(f'INSERT INTO solicitudes_guia ({columns}, created_at) '
                        f"VALUES ({placeholders}, NOW() - %s * INTERVAL '1 day') RETURNING id",
                        (*row.values(), days))
            return cur.fetchone()[0]

    monkeypatch.setattr(rf, 'get_conn', read_only)
    try:
        yield add, admin
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def test_ranking_privado_normalizado_y_ambos_sentidos(history):
    add, _ = history
    add(); add(); add(origin='China', destination='Argentina')
    add(origin='AR', destination='CN', days=3)
    add(origin='IN', destination='AR', days=1)
    for _ in range(10):
        add(origin='US', client='OTHER')
    routes = rf.obtener_rutas_frecuentes('WAIMAO')
    assert [(r['origen'], r['destino'], r['envios']) for r in routes] == [
        ('CN', 'AR', 3), ('IN', 'AR', 1), ('AR', 'CN', 1)]
    assert routes[0]['origen_nombre'] == 'China'
    assert rf.obtener_rutas_frecuentes('NUEVO') == []
    assert rf.obtener_rutas_frecuentes('') == []


def test_no_usa_cancelaciones_pruebas_reemplazos_ocultos_ni_rutas_inciertas(history):
    add, conn = history
    add()
    for status in ['CANCELADO', 'REEMPLAZADO', 'SOLICITADO', 'EN_PROCESO', 'EMITIENDO', 'VERIFICAR_COURIER']:
        add(estado=status)
    add(test=True); add(visible_cliente=False); add(tracking=' ')
    add(days=400); add(days=-1); add(origin='XX'); add(origin=None)
    add(origin='AR', destination='AR')
    canceled = add()
    replaced = add()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO envios VALUES (%s,'WAIMAO','CANCELADO')", (canceled,))
        cur.execute("INSERT INTO solicitudes_guia_reemisiones VALUES (%s,'WAIMAO','EMITIDA')", (replaced,))
    assert rf.obtener_rutas_frecuentes('WAIMAO')[0]['envios'] == 1


def test_origen_solo_se_recupera_de_una_ruta_identificada(history):
    add, conn = history
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rutas VALUES ('IN-AR','IN')")
    add(origin=None, ruta_id='IN-AR')
    add(origin='XX', ruta_id='IN-AR')
    add(origin=None, ruta_id='INEXISTENTE')
    routes = rf.obtener_rutas_frecuentes('WAIMAO')
    assert len(routes) == 1 and routes[0]['origen'] == 'IN' and routes[0]['envios'] == 1


def test_muestra_seis_rutas_y_nunca_inventa_el_sentido_inverso(history):
    add, _ = history
    for index, origin in enumerate(['CN', 'BD', 'IN', 'US', 'MX', 'BR', 'UY', 'PA']):
        for _ in range(index + 1):
            add(origin=origin)
    routes = rf.obtener_rutas_frecuentes('WAIMAO')
    assert len(routes) == 6
    assert [r['origen'] for r in routes] == ['PA', 'UY', 'BR', 'MX', 'US', 'IN']
    assert all(r['destino'] == 'AR' for r in routes)


def test_falla_de_historial_no_impide_cotizar(monkeypatch):
    def unavailable():
        raise RuntimeError('no disponible')
    monkeypatch.setattr(rf, 'get_conn', unavailable)
    assert rf.obtener_rutas_frecuentes('WAIMAO') == []


def test_endpoint_usa_la_cuenta_de_la_sesion_en_get_y_post(monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from endpoints import portal_cliente as pc
    from servicios import cotizaciones_reseller

    seen = []
    monkeypatch.setattr(pc, 'validar_token', lambda token: 'WAIMAO' if token == 'sesion-propia' else None)
    monkeypatch.setattr(pc, 'obtener_rutas_frecuentes', lambda client: seen.append(client) or [])
    monkeypatch.setattr(pc, '_operadores_cliente', lambda *args: [])
    monkeypatch.setattr(cotizaciones_reseller, 'cliente_es_reseller', lambda _: False)
    monkeypatch.setattr(pc.templates, 'TemplateResponse', lambda **kw: JSONResponse({
        'cliente': kw['context']['cliente'], 'form': kw['context']['form'],
        'rutas': kw['context'].get('rutas_frecuentes'),
    }))
    app = FastAPI(); app.include_router(pc.router)
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as client:
        assert client.get('/portal/cotizar?ambito=internacional').status_code == 303
        assert not seen
        client.cookies.set('token', 'sesion-propia')
        response = client.get('/portal/cotizar?ambito=internacional&cliente=OTHER')
        assert response.json()['cliente'] == 'WAIMAO' and seen == ['WAIMAO']
        response = client.post('/portal/cotizar?cliente=OTHER', data={
            'ambito': 'internacional', 'origen_pais': 'CN', 'destino_pais': 'AR',
            'peso_kg': 'invalido', 'cliente': 'OTHER',
        })
        assert response.status_code == 200 and seen == ['WAIMAO', 'WAIMAO']
        assert response.json()['form']['origen_pais'] == 'CN'
        client.get('/portal/cotizar?ambito=nacional')
        assert seen == ['WAIMAO', 'WAIMAO']
