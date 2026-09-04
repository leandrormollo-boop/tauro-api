"""Regresiones H1: sólo datos sintéticos en PostgreSQL aislado."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psycopg2
import pytest

from test_conciliacion_couriers_postgres import (
    conciliacion_db, _crear_solicitud, _crear_cargo_activo, _snapshot_basico, DATABASE_URL,
)
from test_bandeja_dhl_postgres import db, recibir, leer, importar, contar, NUMERO
from servicios import bandeja_facturas_dhl as bandeja
from servicios import conciliacion_couriers as conciliacion
from servicios import revision_financiera_courier as financiera

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere PostgreSQL aislado')


@pytest.fixture
def caso(db, monkeypatch):
    monkeypatch.setattr(financiera, 'get_conn', db)
    sid = _crear_solicitud(db, sufijo='REVISION_FINANCIERA', tracking='0123456789', precio=Decimal('20000'))
    _crear_cargo_activo(db, sid, monto='20000')
    _snapshot_basico(sid, costo='10000', precio='20000', margen='10000', coti_id='COTI-REVISION_FINANCIERA')
    datos = {'tipo_documento': 'FC', 'numero': NUMERO, 'moneda': 'USD',
        'fecha_emision': '2026-09-04', 'subtotal': '10', 'impuestos': '0', 'total': '10',
        'items': [{'linea_numero': 1, 'tracking': '0123456789', 'concepto_tipo': 'FLETE',
                   'importe': '10', 'tipo_cambio_ars': '1500', 'peso_facturado_kg': '2', 'peso_base': 'REAL'}]}
    monkeypatch.setattr(bandeja, 'ejecutar_lector_dhl', lambda *a, **kw: {'extraccion': datos, 'observaciones': []})
    eid = recibir()['id']
    leer(eid)
    fid = importar(eid)['id']
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT id FROM factura_courier_item_matches')
        mid = cur.fetchone()['id']
    return {'sid': sid, 'fid': fid, 'mid': mid, 'sha': bandeja.obtener_entrada_dhl(eid)['archivo_sha256']}


def aprobar(caso, **kw):
    return financiera.aprobar_revision_financiera(caso['fid'], **{
        'tipo_cambio_ars': '1500', 'fuente': 'DOCUMENTO',
        'motivo': 'Verificación sintética del cambio correspondiente',
        'archivo_sha256': caso['sha'], 'confirmada': True, 'actor': 'test-finanzas', **kw})


def test_no_confirma_ni_calcula_sin_revision_financiera(db, caso):
    with pytest.raises(conciliacion.ConciliacionCourierError, match='revisión financiera'):
        conciliacion.confirmar_match(caso['mid'], actor='test')
    with pytest.raises(conciliacion.ConciliacionCourierError, match='revisión financiera'):
        conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='test')
    # Simula un match confirmado por la versión previa, antes del bloqueo.
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE factura_courier_item_matches SET estado='CONFIRMADO', confirmado_por='legacy', confirmado_at=NOW()")
    with pytest.raises(conciliacion.ConciliacionCourierError, match='revisión financiera'):
        conciliacion.calcular_conciliacion_envio(caso['sid'], actor='test')
    assert contar(db, 'conciliaciones_envio') == contar(db, 'ajustes_cliente') == 0


@pytest.mark.parametrize('fx,fuente,pdf', [('1500', 'DOCUMENTO', None),
                                         ('1600', 'COMPROBANTE', b'%PDF-respaldo-sintetico')])
def test_aprobacion_no_cobra_y_calculo_usa_tc_elegido_sin_cambiar_original(db, caso, fx, fuente, pdf):
    revision = aprobar(caso, tipo_cambio_ars=fx, fuente=fuente, respaldo_pdf=pdf)
    assert contar(db, 'ajustes_cliente') == contar(db, 'conciliaciones_envio') == 0
    resultado = conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='test')
    assert not resultado['errores']
    calculo = resultado['conciliaciones'][0]
    assert calculo['costo_courier_real_ars'] == Decimal('10') * Decimal(fx)
    assert calculo['precio_cliente_final_ars'] == Decimal('10') * Decimal(fx) + Decimal('10000')
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT tipo_cambio_ars, importe_ars FROM facturas_courier_items')
        assert dict(cur.fetchone()) == {'tipo_cambio_ars': Decimal('1500'), 'importe_ars': Decimal('15000')}
        cur.execute('SELECT monto_asignado_ars FROM factura_courier_item_matches')
        assert cur.fetchone()['monto_asignado_ars'] == Decimal('15000')
        cur.execute('SELECT evidencias FROM conciliaciones_envio')
        assert cur.fetchone()['evidencias'][0]['revision_financiera_id'] == revision['id']
        cur.execute('SELECT estado FROM ajustes_cliente')
        assert cur.fetchone()['estado'] == 'PROPUESTO'
        cur.execute('SELECT monto_ars FROM envios')
        assert cur.fetchone()['monto_ars'] == Decimal('20000')
    detalle = conciliacion.obtener_factura_courier_control(caso['fid'])
    assert detalle['items'][0]['importe_conciliacion_ars'] == Decimal('10') * Decimal(fx)
    assert financiera.obtener_respaldo_financiero(caso['fid']).startswith(b'%PDF')
    assert conciliacion.aprobar_y_aplicar_ajuste_cliente(calculo['ajuste_id'], actor='test')['ok']
    assert conciliacion.aprobar_y_aplicar_ajuste_cliente(calculo['ajuste_id'], actor='test')['duplicado']
    assert contar(db, 'ajustes_cliente') == 1


def test_doble_aprobacion_concurrente_idempotente_y_no_sobrescribe(db, caso):
    with ThreadPoolExecutor(max_workers=2) as workers:
        resultados = list(workers.map(lambda _: aprobar(caso), range(2)))
    assert resultados[0]['id'] == resultados[1]['id']
    assert sorted(r['duplicado'] for r in resultados) == [False, True]
    assert contar(db, 'revisiones_financieras_courier') == 1
    with pytest.raises(conciliacion.ConciliacionCourierError, match='sobrescribirse'):
        aprobar(caso, motivo='Una decisión diferente no puede pisar el historial')


def test_tc_respaldado_sin_diferencia_cierra_sin_crear_cargos(db, caso):
    aprobar(caso, tipo_cambio_ars='1000', fuente='COMPROBANTE', respaldo_pdf=b'%PDF-evidencia-sintetica')
    calculo = conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='test')['conciliaciones'][0]
    assert calculo['ajuste_cliente_ars'] == 0
    assert contar(db, 'ajustes_cliente') == 0
    assert conciliacion.cerrar_conciliacion_sin_diferencia(calculo['id'], actor='test')['ok']
    assert conciliacion.cerrar_conciliacion_sin_diferencia(calculo['id'], actor='test')['duplicado']


@pytest.mark.parametrize('cambio', [
    {'confirmada': False}, {'confirmada': 'si'}, {'actor': ''}, {'motivo': 'corto'},
    {'fuente': ''}, {'tipo_cambio_ars': '0'}, {'tipo_cambio_ars': '-1'},
    {'tipo_cambio_ars': 'NaN'}, {'tipo_cambio_ars': 'Infinity'}, {'tipo_cambio_ars': True},
    {'tipo_cambio_ars': '1,500'}, {'tipo_cambio_ars': '1500.1234567'},
    {'tipo_cambio_ars': '1000000000000'}, {'tipo_cambio_ars': '1600'},
    {'archivo_sha256': 'b'*64}, {'fuente': 'COMPROBANTE'},
    {'fuente': 'COMPROBANTE', 'respaldo_pdf': b'no es pdf'},
    {'fuente': 'COMPROBANTE', 'respaldo_pdf': b'%PDF' + b'x'*(8*1024*1024)},
    {'respaldo_pdf': b'%PDF-sobra'},
])
def test_revision_exige_eleccion_confirmacion_y_evidencia_valida(db, caso, cambio):
    with pytest.raises(conciliacion.ConciliacionCourierError):
        aprobar(caso, **cambio)
    assert contar(db, 'revisiones_financieras_courier') == 0


def test_calculo_anterior_a_aprobacion_no_se_puede_aplicar_ni_cerrar(db, caso, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(conciliacion, '_exigir_revision_financiera', lambda *a: None)
        resultado = conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='simular-version-anterior')
    calculo = resultado['conciliaciones'][0]
    with pytest.raises(conciliacion.ConciliacionCourierError, match='revisión financiera'):
        conciliacion.aprobar_y_aplicar_ajuste_cliente(calculo['ajuste_id'], actor='test')
    aprobar(caso)
    with pytest.raises(conciliacion.ConciliacionCourierError, match='anterior a la revisión'):
        conciliacion.aprobar_y_aplicar_ajuste_cliente(calculo['ajuste_id'], actor='test')
    with pytest.raises(conciliacion.ConciliacionCourierError, match='anterior a la revisión'):
        conciliacion.cerrar_conciliacion_sin_diferencia(calculo['id'], actor='test')
    with pytest.raises(psycopg2.Error, match='revisión financiera'), db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE conciliaciones_envio SET estado='APROBADA', aprobado_por='test', aprobado_at=NOW()")
    nuevo = conciliacion.calcular_conciliacion_envio(caso['sid'], actor='test-recalculo')
    assert nuevo['version'] == 2 and nuevo['id'] != calculo['id']
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado FROM conciliaciones_envio ORDER BY version')
        assert [r['estado'] for r in cur.fetchall()] == ['ANULADA', 'PARA_REVISION']
        cur.execute('SELECT estado FROM ajustes_cliente ORDER BY id')
        assert [r['estado'] for r in cur.fetchall()] == ['ANULADO', 'PROPUESTO']
        cur.execute("SELECT COUNT(*) AS n FROM auditoria_facturas_courier WHERE evento='CALCULO_PREVIO_A_REVISION_ANULADO'")
        assert cur.fetchone()['n'] == 1
    assert conciliacion.aprobar_y_aplicar_ajuste_cliente(nuevo['ajuste_id'], actor='test')['ok']


def test_recalculo_obsoleto_revierte_anulacion_si_falla_auditoria(db, caso, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(conciliacion, '_exigir_revision_financiera', lambda *a: None)
        conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='simular-version-anterior')
    aprobar(caso)
    def fallar(*a, **kw):
        raise RuntimeError('Sin auditoría no se reemplaza el borrador')
    monkeypatch.setattr(conciliacion, '_registrar_auditoria', fallar)
    with pytest.raises(RuntimeError):
        conciliacion.calcular_conciliacion_envio(caso['sid'], actor='test')
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT estado FROM conciliaciones_envio')
        assert cur.fetchone()['estado'] == 'PARA_REVISION'
        cur.execute('SELECT estado FROM ajustes_cliente')
        assert cur.fetchone()['estado'] == 'PROPUESTO'


def test_recalculo_no_anula_conciliaciones_reclamadas(db, caso, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(conciliacion, '_exigir_revision_financiera', lambda *a: None)
        conciliacion.confirmar_y_calcular_factura(caso['fid'], actor='simular-version-anterior')
    aprobar(caso)
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE conciliaciones_envio SET estado='RECLAMADA'")
    with pytest.raises(conciliacion.ConciliacionActivaError):
        conciliacion.calcular_conciliacion_envio(caso['sid'], actor='test')


@pytest.mark.parametrize('sql', [
    "UPDATE facturas_courier SET metadatos_origen=jsonb_set(metadatos_origen, '{revision_financiera_pendiente}', 'false')",
    "UPDATE facturas_courier SET metadatos_origen='{}'",
    "UPDATE facturas_courier SET archivo_pdf='otra evidencia'",
])
def test_no_se_elude_revision_borrando_marca_o_cambiando_evidencia(db, caso, sql):
    with pytest.raises(psycopg2.Error), db() as conn, conn.cursor() as cur:
        cur.execute(sql)


@pytest.mark.parametrize('sql', ['UPDATE revisiones_financieras_courier SET tipo_cambio_ars=1',
                               'DELETE FROM revisiones_financieras_courier'])
def test_revision_aprobada_es_inmutable(db, caso, sql):
    aprobar(caso)
    with pytest.raises(psycopg2.Error), db() as conn, conn.cursor() as cur:
        cur.execute(sql)


def test_error_auditoria_revierte_aprobacion_y_marca(db, caso, monkeypatch):
    def fallar(*a, **kw):
        raise RuntimeError('auditoría no disponible')
    monkeypatch.setattr(financiera, '_registrar_auditoria', fallar)
    with pytest.raises(RuntimeError):
        aprobar(caso)
    assert contar(db, 'revisiones_financieras_courier') == 0
    with db() as conn, conn.cursor() as cur:
        cur.execute('SELECT metadatos_origen FROM facturas_courier')
        assert cur.fetchone()['metadatos_origen']['revision_financiera_pendiente'] is True
