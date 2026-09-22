"""El cliente vincula crédito existente sin acreditar dinero ni duplicarlo."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_pagos_documentales_postgres import DATABASE_URL, cuenta_db
from servicios import cuenta_corriente as cc, experiencia_cuenta as ec
from servicios import imputacion_pagos_portal as ip

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requiere PostgreSQL aislado")


@pytest.fixture
def datos(cuenta_db, monkeypatch):
    monkeypatch.setattr(ip, "get_conn", cuenta_db)
    monkeypatch.setattr(ec, "get_conn", cuenta_db)
    with cuenta_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO clientes(cliente_id,email) VALUES ('DEMO','demo@example.invalid'),('OTRO','otro@example.invalid')")
            cur.execute("""INSERT INTO envios(cliente_id,fecha,monto_ars,estado,tracking,descripcion,ambito)
                VALUES ('DEMO',CURRENT_DATE,100,'ACTIVO','D1','Toronto','INTERNACIONAL'),
                       ('DEMO',CURRENT_DATE,100,'ACTIVO','D2','Shanghai','INTERNACIONAL'),
                       ('DEMO',CURRENT_DATE,100,'CANCELADO','DC','Cancelado','INTERNACIONAL'),
                       ('OTRO',CURRENT_DATE,100,'ACTIVO','OTRO','Ajeno','INTERNACIONAL') RETURNING id""")
            ids = [r['id'] for r in cur.fetchall()]
    return cuenta_db, ids


def pago(estado="APROBADO", monto="250"):
    return cc.registrar_pago(cliente_id="DEMO",fecha="2026-09-22",monto_ars=monto,
                            metodo="Transferencia",estado=estado,comprobante=b"%PDF-1.4\n%%EOF")


@pytest.mark.parametrize("estado", ["APROBADO", "PENDIENTE"])
def test_imputa_varios_sin_cambiar_saldo_y_respeta_aprobacion(datos, estado):
    conn, ids = datos
    id = pago(estado)
    antes = cc.resumen_cuenta_por_ambito("DEMO")['consolidado']['saldo_ars']
    assert ip.imputar_pago_cliente("DEMO",id,[f"E:{i}" for i in ids[:2]],"250") == (
        "APLICADA" if estado == "APROBADO" else "SOLICITADA")
    despues = cc.resumen_cuenta_por_ambito("DEMO")
    assert despues['consolidado']['saldo_ars'] == antes
    detalle = ec.obtener_pago_cliente("DEMO",id)
    assert detalle['cantidad_envios'] == 2
    assert detalle['sin_imputar_ars'] == Decimal('50.00')
    assert sum(a['monto_ars'] for a in detalle['aplicaciones']) == Decimal('200.00')
    assert ('en revisión' in detalle['imputacion_label']) == (estado == "PENDIENTE")
    if estado == "PENDIENTE":
        assert cc.resolver_pago(id,aprobar=True,aplicaciones=None)
        assert cc.resumen_cuenta_por_ambito("DEMO")['consolidado']['saldo_ars'] == antes - Decimal('250')
        assert ec.obtener_pago_cliente("DEMO",id)['imputacion_label'] == 'Pago imputado a 2 envíos'


def test_pago_parcial_centavos_y_reintento_no_reescriben(datos):
    conn, ids = datos
    id = pago(monto='150.25')
    ip.imputar_pago_cliente('DEMO',id,[f'E:{i}' for i in ids[:2]],'150.25')
    assert [a['monto_ars'] for a in ec.obtener_pago_cliente('DEMO',id)['aplicaciones']] == [Decimal('100'),Decimal('50.25')]
    with pytest.raises(ValueError,match='ya fue imputado'):
        ip.imputar_pago_cliente('DEMO',id,[f'E:{i}' for i in ids[:2]],'150.25')
    with conn() as db:
        with db.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM pagos_aplicaciones WHERE pago_id=%s',(id,))
            assert cur.fetchone()['n'] == 2


@pytest.mark.parametrize('caso',['pago_ajeno','documento_ajeno','cancelado','rechazado','importe_viejo','duplicado','faltante','sin_seleccion'])
def test_rechaza_sin_escribir_ninguna_aplicacion(datos, caso):
    conn, ids = datos
    id = pago('RECHAZADO' if caso=='rechazado' else 'APROBADO')
    cliente = 'OTRO' if caso=='pago_ajeno' else 'DEMO'
    elegidos = [ids[3] if caso=='documento_ajeno' else ids[2] if caso=='cancelado' else 999999 if caso=='faltante' else ids[0]]
    if caso=='duplicado': elegidos *= 2
    if caso=='sin_seleccion': elegidos=[]
    with pytest.raises(ValueError):
        ip.imputar_pago_cliente(cliente,id,[f'E:{i}' for i in elegidos], '200' if caso=='importe_viejo' else '250')
    with conn() as db:
        with db.cursor() as cur:
            cur.execute('SELECT COUNT(*) AS n FROM pagos_aplicaciones WHERE pago_id=%s',(id,))
            assert cur.fetchone()['n'] == 0
    assert ec.obtener_pago_cliente('OTRO',id) is None


def test_concurrencia_un_solo_remanente(datos):
    _, ids = datos
    id = pago(monto='100')
    def intentar(envio):
        try:
            ip.imputar_pago_cliente('DEMO',id,[f'E:{envio}'],'100')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(intentar,ids[:2]))
    assert sorted(resultados) == [False,True]
    assert len(ec.obtener_pago_cliente('DEMO',id)['aplicaciones']) == 1


def test_conteo_y_remanente_no_se_truncan_en_24_envios(datos):
    conn, _ = datos
    with conn() as db:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO envios(cliente_id,fecha,monto_ars,estado,tracking,ambito)
                SELECT 'DEMO',CURRENT_DATE,10,'ACTIVO','LOTE-'||n,'INTERNACIONAL'
                FROM generate_series(1,26) n RETURNING id""")
            ids = [r['id'] for r in cur.fetchall()]
    id = pago(monto='300')
    ip.imputar_pago_cliente('DEMO',id,[f'E:{i}' for i in ids],'300')
    detalle = ec.obtener_pago_cliente('DEMO',id)
    assert len(detalle['aplicaciones']) == 26
    with conn() as db:
        with db.cursor() as cur:
            cur.execute(ec._PAGOS_SQL, ('DEMO',None,None,None,None))
            tarjeta = ec._presentar_pago(dict(cur.fetchone()))
    assert len(tarjeta['aplicaciones']) == 24
    assert tarjeta['cantidad_envios'] == 26
    assert tarjeta['sin_imputar_ars'] == Decimal('40')
    assert tarjeta['imputacion_label'] == 'Pago imputado a 26 envíos'


@pytest.mark.parametrize('estado,visible,test', [
    ('CANCELADO',True,False), ('REEMPLAZADO',True,False),
    ('GUIA_LISTA',False,False), ('GUIA_LISTA',True,True),
])
def test_no_ofrece_ni_admite_guias_fuera_de_operacion(datos, estado, visible, test):
    conn, ids = datos
    with conn() as db:
        with db.cursor() as cur:
            cur.execute("""INSERT INTO solicitudes_guia
                (cliente_id,estado,producto_alias,destino_pais,dest_nombre,dest_direccion,
                 dest_ciudad,dest_zip,visible_cliente,test)
                VALUES ('DEMO',%s,'DEMO','CA','Cliente','Calle','Toronto','M5V',%s,%s)
                RETURNING id""", (estado,visible,test))
            solicitud = cur.fetchone()['id']
            cur.execute('UPDATE envios SET solicitud_id=%s WHERE id=%s',(solicitud,ids[0]))
    assert f'E:{ids[0]}' not in {d['clave'] for d in cc.listar_destinos_pago('DEMO')}
    id = pago()
    with pytest.raises(ValueError,match='ya no está disponible'):
        ip.imputar_pago_cliente('DEMO',id,[f'E:{ids[0]}'],'250')
    assert ec.obtener_pago_cliente('DEMO',id)['sin_imputar_ars'] == Decimal('250')


def test_http_detalle_ajeno_404_y_post_no_acredita(datos, monkeypatch):
    from endpoints import portal_cliente as portal
    _, ids = datos
    id = pago('PENDIENTE')
    app=FastAPI();app.include_router(portal.router)
    app.dependency_overrides[portal.cliente_actual]=lambda:'OTRO'
    client=TestClient(app)
    assert client.get(f'/portal/pagos/{id}/imputar').status_code==404
    response=client.post(f'/portal/pagos/{id}/imputar',data={'destinos':f'E:{ids[0]}','disponible_esperado':'250'},follow_redirects=False)
    assert response.status_code==303 and 'error=' in response.headers['location']
    app.dependency_overrides[portal.cliente_actual]=lambda:'DEMO'
    response=client.post(f'/portal/pagos/{id}/imputar',data={'destinos':f'E:{ids[0]}','disponible_esperado':'250'},follow_redirects=False)
    assert response.status_code==303 and 'guardado=1' in response.headers['location']
    assert ec.obtener_pago_cliente('DEMO',id)['estado']=='PENDIENTE'
