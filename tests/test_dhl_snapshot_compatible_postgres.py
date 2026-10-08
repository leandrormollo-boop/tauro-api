"""Regresión: crear/editar y luego emitir deben reconocer la misma tarifa."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from servicios import solicitudes_guia as sg
from servicios import conciliacion_couriers as cc
from tests.test_conciliacion_couriers_postgres import DATABASE_URL, conciliacion_db

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL aislado requerido")


@pytest.fixture
def caso(conciliacion_db, monkeypatch):
    db = conciliacion_db
    monkeypatch.setattr(sg, "get_conn", db)
    base = dict(moneda_courier="USD", tipo_cambio_ars="1500",
                costo_courier_estimado="100.13", costo_courier_estimado_ars="150195",
                precio_cliente_inicial_ars="250195", margen_tauro_protegido_ars="100000",
                markup_tipo="FIJO_ARS", markup_valor="100000",
                peso_real_cotizado_kg="2", peso_volumetrico_cotizado_kg="3.996",
                peso_facturable_cotizado_kg="3.996")
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id,nombre,email) VALUES('PRUEBA','Prueba','test@example.invalid')")
        cur.execute("""INSERT INTO solicitudes_guia(cliente_id,producto_alias,destino_pais,
            dest_nombre,dest_direccion,dest_ciudad,dest_zip,remitente_pais,courier,
            precio_tauro_ars,coti_id,peso_kg,bultos)
            VALUES('PRUEBA','Muestra','US','Prueba','Calle 1','Miami','33101','AR',
                   'DHL',250195,'COTI-PRUEBA',2,'[]') RETURNING *""")
        sol = dict(cur.fetchone())
        assert sg._congelar_cotizacion_aceptada_con_cursor(cur, sol, base_interna=base)
    return db, sol, base


def snapshot(db, sid):
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id=%s', (sid,))
        return dict(cur.fetchone())


def test_recotiza_tarifa_guardada_por_editor_sin_cambiar_historia(caso, monkeypatch):
    db, sol, base = caso
    anterior = snapshot(db, sol['id'])
    # Simula la reserva previa a la llamada irreversible, como en el portal.
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE solicitudes_guia SET estado='EMITIENDO' WHERE id=%s", (sol['id'],))
    monkeypatch.setattr('servicios.api_b2b.cotizar_couriers_cliente', lambda *a, **kw: {
        'opciones': [{'id':'dhl','precio_ars':250195,'_base_interna':base}]})
    sol.update(cantidad=1,valor_declarado_usd=25,largo_cm=30,ancho_cm=30,alto_cm=20)
    assert sg._recotizar_dhl_antes_de_emitir(sol) == {'ok': True}
    assert snapshot(db, sol['id']) == anterior
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT COUNT(*) AS n FROM envios WHERE solicitud_id=%s', (sol['id'],))
        assert cur.fetchone()['n'] == 0


def test_reintentos_simultaneos_reutilizan_snapshot(caso):
    db, sol, base = caso
    anterior = snapshot(db, sol['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _:sg._congelar_base_recotizada(sol,base), range(2))) == [True,True]
    assert snapshot(db, sol['id']) == anterior


@pytest.mark.parametrize('cambio', [
    {'costo_courier_estimado':'100.14','margen_tauro_protegido_ars':'99985'},
    {'markup_valor':'99999'},
    {'peso_real_cotizado_kg':'2.1'},
    {'peso_volumetrico_cotizado_kg':'4'},
    {'peso_facturable_cotizado_kg':'4'},
])
def test_diferencia_real_sigue_bloqueada(caso,cambio):
    db,sol,base=caso
    anterior=snapshot(db,sol['id'])
    with pytest.raises(cc.SnapshotInmutableError):
        sg._congelar_base_recotizada(sol,{**base,**cambio})
    assert snapshot(db,sol['id']) == anterior


def test_bultos_y_servicio_distintos_siguen_bloqueados(caso):
    _,sol,base=caso
    for cambio in ({'bultos':[{'peso_kg':3}]},{'servicio_courier':'OTRO'}):
        with pytest.raises(cc.SnapshotInmutableError):
            sg._congelar_base_recotizada({**sol,**cambio},base)


def test_precision_de_postgres_no_es_un_cambio_comercial(caso):
    _,sol,base=caso
    assert sg._congelar_base_recotizada(sol,{**base,'peso_real_cotizado_kg':Decimal('2.00000001')})


def test_fallo_portal_queda_consultable_en_admin(caso,monkeypatch):
    from starlette.requests import Request
    from endpoints import portal_cliente as pc
    from servicios import incidencias_emision as inc, auditoria
    db,sol,_=caso
    monkeypatch.setattr(inc,'get_conn',db)
    monkeypatch.setattr(auditoria,'get_conn',db)
    monkeypatch.setattr(sg,'emitir_guia_como_cliente',lambda *a,**kw:{
        'ok':False,'codigo_error':'TARIFA_GUARDADA_INCONSISTENTE',
        'error_tipo':'SnapshotInmutableError','etapa':'validar_tarifa',
        'error':'No pudimos validar la tarifa guardada.'})
    req=Request({'type':'http','method':'POST','path':'/portal/envios/1/emitir',
                 'headers':[], 'client':('127.0.0.1',1234)})
    res=pc.emitir_guia_portal(req,sol['id'],cliente='PRUEBA',gestion_ventana='',revision_envio='')
    assert res.status_code==303 and 'Referencia' in res.headers['location']
    intento=inc.listar_intentos(solicitud_id=sol['id'])['items'][0]
    assert intento['cliente_id']=='PRUEBA' and not intento['success']
    assert intento['metadata']['error_tipo']=='SnapshotInmutableError'
    assert intento['metadata']['referencia'] in res.headers['location']


def test_historial_pagina_y_filtra_resultados(caso,monkeypatch):
    from psycopg2.extras import Json
    from servicios import incidencias_emision as inc
    db,sol,_=caso
    monkeypatch.setattr(inc,'get_conn',db)
    with db() as conn,conn.cursor() as cur:
        cur.executemany("""INSERT INTO security_audit(event,actor_type,success,metadata)
            VALUES('portal.emitir_guia','cliente',FALSE,%s)""",[
                (Json({'solicitud_id':sol['id'],'referencia':f'PRUEBA-{i}'}),) for i in range(52)])
    pagina=inc.listar_intentos(solicitud_id=sol['id'])
    assert len(pagina['items'])==50 and pagina['siguiente']
    siguiente=inc.listar_intentos(solicitud_id=sol['id'],antes=pagina['siguiente'])
    assert len(siguiente['items'])==2 and not siguiente['siguiente']
    assert not inc.listar_intentos(resultado='exitosos')['items']
    assert not inc.listar_intentos(solicitud_id=999999)['items']
