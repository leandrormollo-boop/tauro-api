"""Permisos de cotización común y reseller contra PostgreSQL aislado."""
import io
from decimal import Decimal

import pytest
from pypdf import PdfReader
from test_conciliacion_couriers_postgres import conciliacion_db, DATABASE_URL  # noqa: F401
from servicios import cotizaciones_portal as quotes, cotizaciones_reseller as reseller

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='requiere TAURO_TEST_DATABASE_URL aislada')


def test_7c_snapshot_comun_dueno_vencimiento_y_permiso_reseller(conciliacion_db, monkeypatch):
    monkeypatch.setattr(quotes, 'get_conn', conciliacion_db)
    monkeypatch.setattr(reseller, 'get_conn', conciliacion_db)
    with conciliacion_db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO clientes(cliente_id,email,es_reseller) VALUES "
                    "('COMUN','comun@example.invalid',FALSE),('OTRO','otro@example.invalid',FALSE)")
    op = dict(precio_final_ars=Decimal('100.25'),carrier_nombre='OCA',servicio='Express',
              costo_courier=80,margen_tauro=20.25)
    opciones = quotes.guardar_opciones('COMUN',ruta='AR → AR',bultos=[dict(cantidad=1,peso_kg=2)],
                                      peso_facturable_kg=None,opciones=[op])
    qid = opciones[0]['portal_quote_id']
    row = quotes.obtener('COMUN',qid)
    assert row['precio_base_ars'] == Decimal('100.25') and row['peso_facturable_kg'] is None
    assert quotes.obtener('OTRO',qid) is None
    assert reseller._obtener('COMUN',qid) is None
    with pytest.raises(ValueError,match='no pertenece'):
        reseller.generar_pdf('COMUN',qid,'1000')
    pdf, _ = quotes.generar_pdf('COMUN',qid)
    texto = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert '$ 100,25 ARS' in texto and 'Se confirma al emitir' in texto
    assert 'margen' not in texto.lower() and 'costo' not in texto.lower()
    with conciliacion_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE cotizaciones_reseller SET vigente_hasta=NOW()-INTERVAL '1 minute' WHERE quote_id=%s",(qid,))
    assert quotes.obtener('COMUN',qid) is None
    with pytest.raises(ValueError,match='venció'):
        quotes.generar_pdf('COMUN',qid)
