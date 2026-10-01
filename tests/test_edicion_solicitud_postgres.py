"""Edición transaccional, sólo en PostgreSQL descartable. Nunca usa producción."""
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
import psycopg2

from servicios import edicion_solicitud as ed, solicitudes_guia as sg
from test_conciliacion_couriers_postgres import conciliacion_db

pytestmark = pytest.mark.skipif(not os.getenv('TAURO_TEST_DATABASE_URL'), reason='requiere PostgreSQL aislado')


@pytest.fixture
def caso(conciliacion_db, monkeypatch):
    conn = conciliacion_db
    monkeypatch.setattr(ed, 'get_conn', conn)
    monkeypatch.setattr(sg, 'get_conn', conn)
    with conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id, nombre, email) VALUES('TEST', 'Prueba', 'test@example.invalid')")
        for coti, precio in [('A', 100000), ('B', 120000)]:
            cur.execute("""INSERT INTO cotizaciones(coti_id, cliente_id, ruta_id, peso_kg, peso_usado_kg,
                costo_fedex_usd, precio_final_usd, precio_final_ars) VALUES(%s,'TEST','AR-US',1,1,80,%s,%s)""", (coti, precio/1000, precio))
        cur.execute("""INSERT INTO solicitudes_guia(cliente_id, producto_alias, destino_pais, dest_nombre,
            dest_direccion, dest_ciudad, dest_zip, remitente_pais, courier, coti_id, precio_tauro_ars,
            origen_plataforma, origen_pedido_externo_id)
            VALUES('TEST','Prueba','US','Destino','Calle','Miami','33131','AR','DHL','A',100000,'shopify','pedido-test') RETURNING *""")
        sol = dict(cur.fetchone())
        assert sg._congelar_cotizacion_aceptada_con_cursor(cur, sol)
    return conn, sol


def editar(sol, **campos):
    return ed.editar_solicitud_cliente(sol['id'], 'TEST', sol['updated_at'].isoformat(),
        dict(dest_nombre='Corregido', coti_id='B', precio_tauro_ars=120000, bultos=[], **campos))


def test_misma_identidad_snapshot_y_sin_cargo(caso):
    conn, sol = caso
    actual = editar(sol, cliente_id='OTRO', tracking='NO', estado='GUIA_LISTA', origen_pedido_externo_id='NO')
    assert actual['id'] == sol['id'] and actual['cliente_id'] == 'TEST'
    assert actual['origen_pedido_externo_id'] == 'pedido-test'
    assert actual['tracking'] is None and actual['estado'] == 'SOLICITADO'
    assert actual['dest_nombre'] == 'Corregido'
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id=%s', (sol['id'],))
        snap = cur.fetchone()
        assert snap['coti_id'] == 'B' and snap['precio_cliente_inicial_ars'] == Decimal('120000')
        assert snap['margen_tauro_protegido_ars'] == Decimal('40000')
        cur.execute('SELECT snapshot_anterior, cotizacion_nueva FROM solicitud_cotizacion_revisiones WHERE solicitud_id=%s', (sol['id'],))
        revision = cur.fetchone()
        assert revision['snapshot_anterior']['precio_cliente_inicial_ars'] == 100000
        assert revision['cotizacion_nueva'] == 'B'
        cur.execute('SELECT COUNT(*) AS n FROM envios WHERE solicitud_id=%s', (sol['id'],))
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT metadata FROM security_audit WHERE event='portal.solicitud_editada'")
        audit = cur.fetchone()['metadata']
        assert audit['cotizacion_anterior'] == 'A' and audit['cotizacion_nueva'] == 'B'
        assert 'Corregido' not in str(audit) and 'precio_tauro_ars' in audit['campos']
    with pytest.raises(ValueError, match='cambió'):
        editar(sol)


@pytest.mark.parametrize('campo,valor', [('estado','EMITIENDO'), ('estado','GUIA_LISTA'), ('tracking','TEST'), ('test',True), ('visible_cliente',False)])
def test_rechaza_operaciones_protegidas(caso, campo, valor):
    conn, sol = caso
    with conn() as c, c.cursor() as cur:
        cur.execute(f'UPDATE solicitudes_guia SET {campo}=%s WHERE id=%s', (valor, sol['id']))
    with pytest.raises(ValueError, match='no se puede editar'):
        editar(sol)


def test_ajeno_y_snapshot_invalido_no_escriben(caso):
    conn, sol = caso
    with pytest.raises(ValueError):
        ed.editar_solicitud_cliente(sol['id'], 'OTRO', sol['updated_at'].isoformat(), dict(dest_nombre='NO'))
    with pytest.raises(ValueError, match='tarifa'):
        ed.editar_solicitud_cliente(sol['id'], 'TEST', sol['updated_at'].isoformat(), dict(coti_id='NO', dest_nombre='NO'))
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT dest_nombre, coti_id FROM solicitudes_guia WHERE id=%s', (sol['id'],))
        assert dict(cur.fetchone()) == dict(dest_nombre='Destino', coti_id='A')


def test_no_confirma_precio_visto_antes_de_editar(caso):
    conn, sol = caso
    editar(sol)
    result = sg._reservar_credito_cliente(sol['id'], 'TEST', revision=sol['updated_at'].isoformat())
    assert not result['ok'] and 'precio cambió' in result['error']
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT estado,cargo_pendiente FROM solicitudes_guia WHERE id=%s', (sol['id'],))
        actual = cur.fetchone()
        assert actual['estado'] == 'SOLICITADO' and not actual['cargo_pendiente']


def test_emision_gana_lock_edicion_espera_y_se_rechaza(caso):
    conn, sol = caso
    started = Event()
    def concurrente():
        started.set()
        return editar(sol)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with conn() as c, c.cursor() as cur:
            cur.execute("UPDATE solicitudes_guia SET estado='EMITIENDO' WHERE id=%s", (sol['id'],))
            future = pool.submit(concurrente)
            assert started.wait(2)
            assert not future.done()
        with pytest.raises(ValueError, match='no se puede editar'):
            future.result(timeout=5)


def test_tarifa_y_revisiones_no_admiten_mutaciones_genericas(caso):
    conn, sol = caso
    editar(sol)
    for query in [
        "UPDATE envio_cotizacion_snapshots SET margen_tauro_protegido_ars=margen_tauro_protegido_ars",
        "DELETE FROM envio_cotizacion_snapshots",
        "UPDATE solicitud_cotizacion_revisiones SET cotizacion_nueva='NO'",
        "DELETE FROM solicitud_cotizacion_revisiones",
    ]:
        with pytest.raises(psycopg2.errors.RaiseException):
            with conn() as c, c.cursor() as cur:
                cur.execute(query)


def test_ni_con_marca_de_edicion_se_puede_cambiar_guia_emitida(caso):
    conn, sol = caso
    with conn() as c, c.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='GUIA_LISTA',tracking='TEST' WHERE id=%s", (sol['id'],))
    with pytest.raises(psycopg2.errors.RaiseException, match='inmutable'):
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT set_config('tauro.edicion_solicitud', %s, true)", (str(sol['id']),))
            cur.execute('UPDATE envio_cotizacion_snapshots SET aceptado_at=NOW() WHERE solicitud_id=%s', (sol['id'],))
