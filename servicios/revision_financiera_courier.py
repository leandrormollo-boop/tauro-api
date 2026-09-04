"""Decisión administrativa de TC, separada de la conversión del documento.

No decide una política cambiaria ni cobra: conserva la elección y su evidencia.
Una revisión aprobada no se edita ni se reemplaza silenciosamente.
"""
import hashlib
from decimal import Decimal

from servicios.conciliacion_couriers import (
    ConciliacionCourierError, _decimal, _texto, _registrar_auditoria,
    _requiere_revision_financiera, SEIS_DECIMALES,
)
from core.database import get_conn


def aprobar_revision_financiera(factura_id, *, tipo_cambio_ars, fuente, motivo,
                                archivo_sha256, confirmada, actor, respaldo_pdf=None):
    actor, motivo, fuente = _texto(actor), _texto(motivo), _texto(fuente)
    if confirmada is not True or not actor or not 12 <= len(motivo) <= 1000:
        raise ConciliacionCourierError('Confirmá la revisión financiera e indicá su fundamento (12 a 1000 caracteres).')
    fx = _decimal(tipo_cambio_ars, 'Tipo de cambio aprobado', minimo=Decimal('0'), permite_cero=False)
    if fx > Decimal('999999999999') or fx != fx.quantize(SEIS_DECIMALES):
        raise ConciliacionCourierError('Tipo de cambio fuera de rango o con más de 6 decimales.')
    if fuente not in ('DOCUMENTO', 'COMPROBANTE'):
        raise ConciliacionCourierError('Elegí expresamente el respaldo del tipo de cambio.')
    if fuente == 'COMPROBANTE' and (
        not isinstance(respaldo_pdf, bytes) or not respaldo_pdf.startswith(b'%PDF')
        or not 4 <= len(respaldo_pdf) <= 8 * 1024 * 1024
    ):
        raise ConciliacionCourierError('Adjuntá el comprobante que respalda el cambio, en PDF de hasta 8 MB.')
    if fuente == 'DOCUMENTO' and respaldo_pdf:
        raise ConciliacionCourierError('Para adjuntar otro respaldo elegí la fuente Comprobante.')
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM facturas_courier WHERE id=%s FOR UPDATE', (int(factura_id),))
        factura = cur.fetchone()
        if not factura or factura['estado'] in ('ANULADA', 'CERRADA'):
            raise ConciliacionCourierError('La factura no existe o está cerrada/anulada.')
        if not _requiere_revision_financiera(factura['metadatos_origen']):
            raise ConciliacionCourierError('Esta factura no pertenece al circuito de revisión financiera DHL.')
        if not archivo_sha256 or factura['archivo_sha256'] != archivo_sha256:
            raise ConciliacionCourierError('La evidencia cambió. Volvé a abrir la factura.')
        original = bytes(factura['archivo_pdf'] or b'')
        if hashlib.sha256(original).hexdigest() != archivo_sha256:
            raise ConciliacionCourierError('La huella del documento original no coincide.')
        cur.execute('SELECT moneda, tipo_cambio_ars FROM facturas_courier_items WHERE factura_id=%s', (int(factura_id),))
        items = list(cur.fetchall())
        if not items or any(i['moneda'] != factura['moneda'] for i in items):
            raise ConciliacionCourierError('La factura tiene monedas incompatibles para una conversión única.')
        if factura['moneda'] == 'ARS' and fx != 1:
            raise ConciliacionCourierError('Una factura en ARS debe usar tipo de cambio 1.')
        if fuente == 'DOCUMENTO' and any(i['tipo_cambio_ars'] != fx for i in items):
            raise ConciliacionCourierError('El cambio indicado no coincide con el documento. Adjuntá un comprobante para utilizar otro.')
        respaldo_sha = hashlib.sha256(respaldo_pdf).hexdigest() if respaldo_pdf else archivo_sha256
        cur.execute('SELECT * FROM revisiones_financieras_courier WHERE factura_id=%s', (int(factura_id),))
        existente = cur.fetchone()
        if existente:
            if (existente['tipo_cambio_ars'], existente['fuente'], existente['motivo'], existente['respaldo_sha256']) == (fx, fuente, motivo, respaldo_sha):
                return {'id': existente['id'], 'duplicado': True}
            raise ConciliacionCourierError('Ya existe una revisión financiera aprobada. No puede sobrescribirse.')
        cur.execute('''INSERT INTO revisiones_financieras_courier
            (factura_id, moneda, tipo_cambio_ars, fuente, motivo, archivo_sha256,
             respaldo_pdf, respaldo_sha256, aprobado_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (int(factura_id), factura['moneda'], fx, fuente, motivo, archivo_sha256,
             respaldo_pdf, respaldo_sha, actor))
        revision_id = cur.fetchone()['id']
        cur.execute('''UPDATE facturas_courier SET metadatos_origen=
            jsonb_set(metadatos_origen, '{revision_financiera_pendiente}', 'false'), updated_at=NOW()
            WHERE id=%s''', (int(factura_id),))
        _registrar_auditoria(cur, evento='REVISION_FINANCIERA_APROBADA', actor=actor,
            factura_id=int(factura_id), metadata={'revision_financiera_id': revision_id,
                'tipo_cambio_ars': str(fx), 'fuente': fuente, 'respaldo_sha256': respaldo_sha})
        return {'id': revision_id, 'duplicado': False}


def obtener_respaldo_financiero(factura_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT CASE WHEN r.fuente='DOCUMENTO' THEN f.archivo_pdf
            ELSE r.respaldo_pdf END AS pdf FROM revisiones_financieras_courier r
            JOIN facturas_courier f ON f.id=r.factura_id WHERE r.factura_id=%s''', (int(factura_id),))
        fila = cur.fetchone()
        return bytes(fila['pdf']) if fila and fila['pdf'] else None
