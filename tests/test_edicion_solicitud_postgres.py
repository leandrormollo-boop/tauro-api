"""Edición transaccional, sólo en PostgreSQL descartable. Nunca usa producción."""
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event

import pytest
import psycopg2

from servicios import edicion_solicitud as ed, solicitudes_guia as sg
from servicios.carriers import cotizar_carriers_cliente as cotizador_real
from test_dhl_invoice_multiitems import portal, submit
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


def test_wizard_real_sin_coti_id_guarda_revision_y_nunca_expone_base(conciliacion_db, portal, monkeypatch):
    """Cotizador B2B + pricing reales; sólo la respuesta courier es ficticia."""
    from endpoints import portal_cliente as pc
    from servicios import carriers
    from unittest.mock import Mock
    import json

    conn = conciliacion_db
    monkeypatch.setattr(ed, 'get_conn', conn)
    monkeypatch.setattr(sg, 'get_conn', conn)
    monkeypatch.setattr(carriers, 'cotizar_carriers_cliente', cotizador_real)
    raw = dict(id='dhl', nombre='DHL', logo='', servicio='P', estado='cotizado',
               costo=100, moneda='USD', dias_estimados=4)
    monkeypatch.setattr(carriers, 'costos_carriers', lambda *a, **kw: [raw])
    monkeypatch.setattr('servicios.configuracion_couriers_cliente.configuracion_cotizacion', lambda *a: {
        'pricing_general': {'tipo':'FIJO_ARS','valor':95000},
        'pricing_por_courier':{}, 'couriers_habilitados':{'dhl'}})
    with conn() as c, c.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id,nombre,email) VALUES('WAIMAO','Prueba','test@example.invalid')")
        cur.execute("""INSERT INTO solicitudes_guia(cliente_id,producto_alias,destino_pais,
            dest_nombre,dest_direccion,dest_ciudad,dest_zip,remitente_pais,courier,
            precio_tauro_ars) VALUES('WAIMAO','Prueba','AR','Destino','Calle','CABA',
            '1000','CN','DHL',180000) RETURNING *""")
        initial = dict(cur.fetchone())

    def actual(*args):
        with conn() as c, c.cursor() as cur:
            cur.execute('SELECT * FROM solicitudes_guia WHERE id=%s', (initial['id'],))
            return dict(cur.fetchone())
    monkeypatch.setattr(pc, 'obtener_solicitud_de_cliente', actual)
    emitter = Mock(side_effect=AssertionError('No emitir en edición'))
    monkeypatch.setattr(sg, 'emitir_guia_como_cliente', emitter)
    for amount in (195000, 205000):
        raw['costo'] = 100 if amount == 195000 else 110
        row = actual()
        response = submit(editar_solicitud_id=str(row['id']),
            editar_version=row['updated_at'].isoformat(), precio_cotizado_ars=str(amount))
        assert response.status_code == 303
        assert str(row['id']) in response.headers['location']
        row = actual()
        assert row['precio_tauro_ars'] == amount and row['coti_id']
        with conn() as c, c.cursor() as cur:
            cur.execute('SELECT * FROM cotizaciones WHERE coti_id=%s', (row['coti_id'],))
            quote = cur.fetchone()
            assert quote['cliente_id'] == 'WAIMAO' and quote['courier'] == 'DHL'
            assert quote['precio_final_ars'] == amount
            cur.execute('SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id=%s', (row['id'],))
            snapshot = cur.fetchone()
            assert snapshot['coti_id'] == row['coti_id']
            assert snapshot['moneda_courier'] == 'USD'
            assert snapshot['costo_courier_estimado'] == raw['costo']
            assert snapshot['margen_tauro_protegido_ars'] == 95000

    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT snapshot_anterior FROM solicitud_cotizacion_revisiones WHERE solicitud_id=%s ORDER BY id', (row['id'],))
        history = [r['snapshot_anterior'] for r in cur.fetchall()]
        assert len(history) == 2
        assert Decimal(str(history[0]['precio_cliente_inicial_ars'])) == 180000
        assert history[0]['margen_tauro_protegido_ars'] is None
        assert history[1]['precio_cliente_inicial_ars'] == 195000
        cur.execute("SELECT metadata FROM security_audit WHERE event='portal.solicitud_editada'")
        audit = json.dumps(cur.fetchall(), default=str)
        assert 'costo_courier' not in audit and '_base_interna' not in audit
        cur.execute('SELECT COUNT(*) AS n FROM envios WHERE solicitud_id=%s', (row['id'],))
        assert cur.fetchone()['n'] == 0
    # Un error de confirmación sólo vuelve a mostrar datos públicos del form.
    rejected = submit(editar_solicitud_id=str(row['id']),
        editar_version=row['updated_at'].isoformat(), precio_cotizado_ars='1')
    context = json.dumps(rejected['context'], default=str)
    assert rejected['context']['error']
    assert '_base_interna' not in context and 'costo_courier' not in context
    assert 'margen_tauro_protegido' not in context
    assert actual()['coti_id'] == row['coti_id']
    portal[0].assert_not_called()
    emitter.assert_not_called()


def test_base_privada_inconsistente_revierte_cotizacion_y_edicion(caso):
    conn, sol = caso
    base = dict(moneda_courier='USD', tipo_cambio_ars='1000',
        costo_courier_estimado='80', costo_courier_estimado_ars='80000',
        precio_cliente_inicial_ars='1', margen_tauro_protegido_ars='40000',
        peso_real_cotizado_kg='1', peso_facturable_cotizado_kg='1')
    with pytest.raises(ValueError, match='no coincide'):
        ed.editar_solicitud_cliente(sol['id'], 'TEST', sol['updated_at'].isoformat(),
            dict(dest_nombre='NO', precio_tauro_ars=120000, precio_tauro_usd=120), base_interna=base)
    with conn() as c, c.cursor() as cur:
        cur.execute('SELECT dest_nombre,coti_id FROM solicitudes_guia WHERE id=%s', (sol['id'],))
        assert dict(cur.fetchone()) == dict(dest_nombre='Destino', coti_id='A')
        cur.execute('SELECT COUNT(*) AS n FROM cotizaciones')
        assert cur.fetchone()['n'] == 2
        cur.execute('SELECT COUNT(*) AS n FROM solicitud_cotizacion_revisiones')
        assert cur.fetchone()['n'] == 0
