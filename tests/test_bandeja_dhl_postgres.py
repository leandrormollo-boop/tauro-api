"""Intake y controles transaccionales con datos sintéticos y PostgreSQL aislado."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import psycopg2
import pytest

from test_conciliacion_couriers_postgres import (
    conciliacion_db, _crear_solicitud, DATABASE_URL,
)
from test_entrada_facturas_dhl import ejemplo
from servicios import bandeja_facturas_dhl as bandeja
from servicios.entrada_facturas_dhl import ExtraccionDHLInvalida
from servicios.conciliacion_couriers import DocumentoCourierDuplicadoError

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')
NUMERO = '1700A00000001'
CUIT = '20123456786'
PDF = b'%PDF-evidencia-sintetica'


@pytest.fixture
def db(conciliacion_db, monkeypatch):
    monkeypatch.setattr(bandeja, 'get_conn', conciliacion_db)
    datos = ejemplo() | {'numero': NUMERO}
    # Impuesto general sin atribuir cliente.
    datos['items'][1]['tracking'] = None
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl',
                        lambda *a, **kw: {'extraccion': deepcopy(datos), 'observaciones': []})
    return conciliacion_db


def recibir(**kw):
    return bandeja.recibir_pdf_dhl(**{'pdf': PDF, 'nombre': 'original.pdf',
        'numero': NUMERO, 'cuit': CUIT, 'actor': 'test', **kw})


def leer(entrada_id, **kw):
    return bandeja.leer_entrada_dhl(entrada_id, **{'numero': NUMERO, 'cuit': CUIT, 'actor': 'test', **kw})


def importar(entrada_id, **kw):
    entrada = bandeja.obtener_entrada_dhl(entrada_id)
    return bandeja.importar_entrada_dhl(entrada_id, **{
        'revision_sha256': entrada['revision_sha256'], 'revision_confirmada': True, 'actor': 'test', **kw})


def contar(db, tabla):
    with db() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS n FROM {tabla}')
        return cur.fetchone()['n']


def test_flujo_solo_evidencia_matches_propuestos_y_sin_saldos(db):
    solicitud = _crear_solicitud(db, sufijo='DHL_INTAKE', tracking='0123456789')
    entrada_id = recibir()['id']
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'RECIBIDA'
    assert contar(db, 'facturas_courier') == 0
    assert leer(entrada_id) == 'PARA_REVISION'
    assert contar(db, 'facturas_courier') == 0
    factura = importar(entrada_id)
    assert not factura['duplicado']
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'IMPORTADA'
    assert importar(entrada_id) == {'id': factura['id'], 'duplicado': True}
    assert contar(db, 'facturas_courier') == 1
    assert contar(db, 'facturas_courier_items') == 2
    for tabla in ('envios', 'pagos', 'ajustes_cliente', 'conciliaciones_envio'):
        assert contar(db, tabla) == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT solicitud_id, estado FROM factura_courier_item_matches')
        assert [dict(r) for r in cur.fetchall()] == [{'solicitud_id': solicitud, 'estado': 'PROPUESTO'}]
        cur.execute('SELECT metadatos_origen, archivo_pdf FROM facturas_courier')
        f = cur.fetchone()
        assert bytes(f['archivo_pdf']) == PDF
        assert f['metadatos_origen']['canal'] == 'admin_pdf_dhl'
        assert 'mensaje_id' not in f['metadatos_origen']
        assert f['metadatos_origen']['revision_financiera_pendiente'] is True
        cur.execute('SELECT precio_tauro_ars FROM solicitudes_guia WHERE id=%s', (solicitud,))
        assert cur.fetchone()['precio_tauro_ars'] == 10000


def test_pdf_repetido_y_referencia_incompatible(db):
    primero = recibir()
    assert recibir(nombre='otro-nombre.pdf') == {'id': primero['id'], 'duplicado': True}
    with pytest.raises(ExtraccionDHLInvalida, match='otra referencia'):
        recibir(numero='1700A00000002')
    assert contar(db, 'entradas_pdf_dhl') == 1


def test_doble_upload_concurrente(db):
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(lambda _: recibir(), range(2)))
    assert resultados[0]['id'] == resultados[1]['id']
    assert sorted(r['duplicado'] for r in resultados) == [False, True]


def test_doble_importacion_concurrente(db):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(lambda _: importar(entrada_id), range(2)))
    assert resultados[0]['id'] == resultados[1]['id']
    assert contar(db, 'facturas_courier') == 1


def test_error_lector_persiste_evidencia_y_permite_reintento(db, monkeypatch):
    entrada_id = recibir()['id']
    real = bandeja.ejecutar_lector_dhl
    def fallar(*a, **kw):
        raise ExtraccionDHLInvalida('Formato desconocido')
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', fallar)
    assert leer(entrada_id) == 'REVISION_MANUAL'
    e = bandeja.obtener_entrada_dhl(entrada_id, con_pdf=True)
    assert bytes(e['archivo_pdf']) == PDF and e['extraccion'] is None
    assert e['intentos'] == 1 and e['error_lectura'] == 'Formato desconocido'
    with pytest.raises(ExtraccionDHLInvalida):
        importar(entrada_id)
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', real)
    assert leer(entrada_id) == 'PARA_REVISION'
    assert bandeja.obtener_entrada_dhl(entrada_id)['intentos'] == 2


def test_referencia_admin_se_puede_corregir_antes_de_extraer(db):
    entrada_id = recibir(numero='1700A00000002')['id']
    assert leer(entrada_id) == 'PARA_REVISION'
    assert bandeja.obtener_entrada_dhl(entrada_id)['numero_esperado'] == NUMERO


@pytest.mark.parametrize('cambio', [{'revision_confirmada': False}, {'revision_confirmada': 'si'},
                                 {'revision_sha256': '0'*64}, {'revision_sha256': ''}])
def test_no_importa_sin_revision_explicita_y_exacta(db, cambio):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with pytest.raises(ExtraccionDHLInvalida):
        importar(entrada_id, **cambio)
    assert contar(db, 'facturas_courier') == 0


def test_conflicto_de_fc_no_duplica_ni_marca_importada(db):
    uno = recibir()['id']
    leer(uno)
    importar(uno)
    dos = recibir(pdf=PDF + b'otro-contenido')['id']
    leer(dos)
    with pytest.raises(DocumentoCourierDuplicadoError):
        importar(dos)
    assert bandeja.obtener_entrada_dhl(dos)['estado'] == 'PARA_REVISION'
    assert contar(db, 'facturas_courier') == 1


def test_fallo_despues_de_registrar_hace_rollback_total(db, monkeypatch):
    entrada_id = recibir()['id']
    leer(entrada_id)
    def fallar(*a, **kw):
        raise RuntimeError('falla simulada después del INSERT de factura')
    monkeypatch.setattr(bandeja, 'matchear_items_exactos', fallar)
    with pytest.raises(RuntimeError):
        importar(entrada_id)
    assert contar(db, 'facturas_courier') == 0
    assert contar(db, 'facturas_courier_items') == 0
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'


@pytest.mark.parametrize('sql', ["UPDATE entradas_pdf_dhl SET archivo_pdf='otro'",
                               "UPDATE entradas_pdf_dhl SET extraccion='{}'",
                               "DELETE FROM entradas_pdf_dhl"])
def test_evidencia_y_revision_no_se_alteran(db, sql):
    entrada_id = recibir()['id']
    leer(entrada_id)
    with pytest.raises(psycopg2.Error), db() as conn, conn.cursor() as cur:
        cur.execute(sql)
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'


def test_listado_no_carga_pdfs_y_pagina_acotada(db):
    recibir()
    filas = bandeja.listar_entradas_dhl(pagina=-1)
    assert filas['pagina'] == 1 and not filas['hay_mas']
    assert 'archivo_pdf' not in filas['items'][0]
    assert 'archivo_pdf' not in bandeja.obtener_entrada_dhl(filas['items'][0]['id'])


def test_migracion_idempotente_preserva_entrada(db):
    from pathlib import Path
    entrada_id = recibir()['id']
    leer(entrada_id)
    with db() as conn, conn.cursor() as cur:
        cur.execute((Path(__file__).resolve().parents[1] / 'sql/schema.sql').read_text())
    assert bandeja.obtener_entrada_dhl(entrada_id)['estado'] == 'PARA_REVISION'
