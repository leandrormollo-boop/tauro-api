"""Permisos reales en PostgreSQL: miniaturas y visor no cambian el histórico."""
import pytest
from servicios import documentos_portal as docs
from servicios import cuenta_corriente, facturacion_clientes
from test_numeracion_guias_tauro import guias_db, crear
from test_portal_pdf_unificado import _pdf_con_paginas


def test_preview_no_consume_contador_y_cache_no_elude_baja(guias_db):
    conn_factory, _ = guias_db
    id = crear(conn_factory, invoice=_pdf_con_paginas((200, 300)))
    doc = docs.obtener_documento('guia', id, 'WAIMAO')
    assert doc and docs.miniatura_documento(doc)
    assert docs.obtener_documento('invoice', id, 'WAIMAO')
    assert docs.obtener_documento('guia', id, 'DEMO') is None
    assert docs.obtener_documento('invoice', id, 'DEMO') is None
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT numero_guia_tauro,guia_descargada_at FROM solicitudes_guia WHERE id=%s', (id,))
            assert dict(cur.fetchone()) == {'numero_guia_tauro': None, 'guia_descargada_at': None}
            cur.execute("UPDATE solicitudes_guia SET estado='CANCELADO' WHERE id=%s", (id,))
    assert docs.obtener_documento('guia', id, 'WAIMAO') is None
    assert docs.obtener_documento('invoice', id, 'WAIMAO') is None


@pytest.mark.parametrize('cambio', [{'estado':'CANCELADO'}, {'estado':'REEMPLAZADO'}, {'test': True}, {'visible':False}])
def test_no_expone_guias_no_vigentes(guias_db, cambio):
    conn_factory, _ = guias_db
    id = crear(conn_factory, invoice=_pdf_con_paginas((200, 300)), **cambio)
    for tipo in ('guia', 'invoice'):
        assert docs.obtener_documento(tipo, id, 'WAIMAO') is None


def test_facturas_y_pagos_aislados_y_sin_cambios_contables(guias_db, monkeypatch):
    conn_factory, _ = guias_db
    monkeypatch.setattr(cuenta_corriente, 'get_conn', conn_factory)
    monkeypatch.setattr(facturacion_clientes, 'get_conn', conn_factory)
    pdf = _pdf_con_paginas((200, 300))
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pagos (cliente_id,fecha,monto_ars,estado,comprobante,comprobante_tipo)
                VALUES ('WAIMAO','2026-09-22',100,'PENDIENTE',%s,'application/pdf') RETURNING id""", (pdf,))
            pago = cur.fetchone()['id']
            cur.execute("""INSERT INTO facturas_cliente
                (cliente_id,tipo,punto_venta,numero,fecha_emision,subtotal,total,pdf,created_by)
                VALUES ('WAIMAO','FC',1,1,'2026-09-22',100,100,%s,'qa') RETURNING id""", (pdf,))
            factura = cur.fetchone()['id']
            cur.execute("""INSERT INTO envios (cliente_id,fecha,monto_ars,estado,ambito,descripcion)
                VALUES ('WAIMAO','2026-09-22',100,'ACTIVO','INTERNACIONAL','qa') RETURNING id""")
            envio = cur.fetchone()['id']
            cur.execute("""INSERT INTO facturas_cliente_items (factura_id,envio_id,descripcion,monto)
                VALUES (%s,%s,'qa',100)""", (factura, envio))
    for tipo, id in [('pago',pago), ('factura',factura)]:
        assert docs.obtener_documento(tipo, id, 'WAIMAO').contenido == pdf
        assert docs.obtener_documento(tipo, id, 'DEMO') is None
    with conn_factory() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT estado,monto_ars FROM pagos WHERE id=%s',(pago,))
            assert dict(cur.fetchone()) == {'estado':'PENDIENTE', 'monto_ars':100}
            cur.execute('SELECT count(*) AS n FROM pagos_aplicaciones')
            assert cur.fetchone()['n'] == 0
