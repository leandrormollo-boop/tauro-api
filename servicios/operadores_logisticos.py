"""Libro interno de proveedores. No escribe cargos ni pagos de clientes.

Una cabecera por documento; importes monetarios calculados con Decimal/SQL.
Los pagos aquí registrados YA ocurrieron: este módulo no transfiere dinero.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg2

from core.database import get_conn
from servicios.conciliacion_couriers import COURIERS, _registrar_auditoria


class OperadorError(ValueError):
    pass


def hoy_ar():
    return datetime.now(ZoneInfo('America/Argentina/Buenos_Aires')).date()


def operador(valor):
    valor = str(valor or '').strip().upper()
    if valor not in COURIERS:
        raise OperadorError('Operador no soportado.')
    return valor


def texto(valor, campo, minimo=1, maximo=1000):
    valor = str(valor or '').strip()
    if not minimo <= len(valor) <= maximo:
        raise OperadorError(f'{campo}: completá entre {minimo} y {maximo} caracteres.')
    return valor


def positivo(valor, campo='Importe', escala=4):
    try:
        d = Decimal(str(valor))
        if not d.is_finite() or d <= 0 or d >= Decimal('1000000000000'):
            raise ValueError()
        q = d.quantize(Decimal(10) ** -escala, rounding=ROUND_HALF_UP)
        if q != d or not q:
            raise ValueError()
        return q
    except (ValueError, InvalidOperation):
        raise OperadorError(f'{campo}: número positivo con hasta {escala} decimales.') from None


def _clave(valor):
    try:
        return str(UUID(str(valor)))
    except (ValueError, TypeError, AttributeError):
        raise OperadorError('Identificador de operación inválido; recargá el formulario.') from None


def _lock(cur, courier):
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('tauro:operador:' + courier,))


def _factura(cur, courier, factura_id):
    cur.execute('SELECT * FROM facturas_courier WHERE id=%s AND courier=%s FOR UPDATE', (factura_id, courier))
    f = cur.fetchone()
    if not f or f['estado'] == 'ANULADA':
        raise OperadorError('Documento inexistente, anulado o de otro operador.')
    return f


def _auditar(cur, evento, actor, factura_id=None, **metadata):
    _registrar_auditoria(cur, evento=evento, actor=actor, factura_id=factura_id, metadata=metadata)


def _escribir(fn):
    """Rollback antes de convertir errores SQL de dominio en mensajes seguros."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            return fn(cur)
    except psycopg2.IntegrityError:
        raise OperadorError('Operación duplicada o datos incompatibles. Revisá el historial antes de reintentar.') from None
    except psycopg2.errors.NoDataFound:
        raise OperadorError('El pago, NC o documento elegido no existe. Recargá el formulario.') from None
    except psycopg2.errors.RaiseException as exc:
        raise OperadorError(exc.diag.message_primary) from None


def configurar_plazo(courier, plazo_dias, motivo, actor='admin', *, clave=None):
    courier = operador(courier)
    clave = str(uuid4()) if clave is None else _clave(clave)
    if plazo_dias not in (None, ''):
        if not re.fullmatch(r'\d{1,3}', str(plazo_dias)) or not 0 <= int(plazo_dias) <= 365:
            raise OperadorError('El plazo debe ser de 0 a 365 días, o vacío si no está acordado.')
        plazo_dias = int(plazo_dias)
    else:
        plazo_dias = None
    motivo, actor = texto(motivo, 'Respaldo del acuerdo', 5), texto(actor, 'Actor')

    def write(cur):
        _lock(cur, courier)
        cur.execute('SELECT * FROM condiciones_operador WHERE clave=%s',(clave,))
        previo=cur.fetchone()
        if previo:
            if (previo['courier'],previo['plazo_dias'],previo['motivo']) != (courier,plazo_dias,motivo):
                raise OperadorError('La operación de plazo ya existe con otros datos.')
            cur.execute('SELECT max(id) AS id FROM condiciones_operador WHERE courier=%s',(courier,))
            if cur.fetchone()['id']!=previo['id']:
                raise OperadorError('Este plazo ya fue reemplazado por otro; recargá la página.')
            return previo['id']
        cur.execute('INSERT INTO condiciones_operador(courier,plazo_dias,motivo,actor,clave) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                    (courier, plazo_dias, motivo, actor,clave))
        ident = cur.fetchone()['id']
        _auditar(cur, 'OPERADOR_PLAZO_CONFIGURADO', actor, condicion_id=ident, courier=courier)
        return ident
    return _escribir(write)


def fijar_plazo_historico(courier, factura_id, condicion_id, actor='admin'):
    courier, actor = operador(courier), texto(actor, 'Actor')

    def write(cur):
        _lock(cur, courier)
        f = _factura(cur, courier, factura_id)
        cur.execute('SELECT 1 FROM rectificaciones_vencimiento_operador WHERE factura_id=%s',(factura_id,))
        if cur.fetchone():
            raise OperadorError('El vencimiento ya tiene rectificaciones. Usá la opción Rectificar para conservar ese historial.')
        cur.execute('SELECT 1 FROM vencimientos_operador WHERE factura_id=%s', (factura_id,))
        if cur.fetchone():
            return factura_id
        cur.execute('SELECT * FROM condiciones_operador WHERE courier=%s ORDER BY id DESC LIMIT 1', (courier,))
        c = cur.fetchone()
        if c and c['id'] != condicion_id:
            raise OperadorError('El plazo cambió desde que abriste la factura. Recargá antes de confirmar.')
        if not c or c['plazo_dias'] is None or not f['fecha_emision'] or f['fecha_vencimiento'] or f['tipo_documento']=='NC':
            raise OperadorError('Se necesita FC/ND sin vencimiento documental, con fecha de emisión y plazo acordado.')
        cur.execute('INSERT INTO vencimientos_operador(factura_id,condicion_id,fecha,actor) VALUES(%s,%s,%s::date+%s,%s)',
                    (factura_id, c['id'], f['fecha_emision'], c['plazo_dias'], actor))
        _auditar(cur, 'OPERADOR_VENCIMIENTO_FIJADO', actor, factura_id, condicion_id=c['id'])
        return factura_id
    return _escribir(write)


def verificar_historial(courier, factura_id, motivo, actor='admin', *, clave=None):
    courier, motivo, actor = operador(courier), texto(motivo, 'Revisión', 5), texto(actor, 'Actor')
    clave = str(uuid4()) if clave is None else _clave(clave)

    def write(cur):
        _lock(cur, courier)
        _factura(cur, courier, factura_id)
        cur.execute('SELECT * FROM verificaciones_operador WHERE clave=%s',(clave,))
        previo=cur.fetchone()
        if previo:
            if (previo['factura_id'],previo['motivo'])!=(factura_id,motivo):
                raise OperadorError('Esta verificación ya existe con otros datos.')
            return previo['id']
        cur.execute('INSERT INTO verificaciones_operador(factura_id,motivo,actor,clave) VALUES(%s,%s,%s,%s) RETURNING id',
                    (factura_id, motivo, actor,clave))
        ident = cur.fetchone()['id']
        _auditar(cur, 'OPERADOR_HISTORIAL_VERIFICADO', actor, factura_id, verificacion_id=ident)
        return ident
    return _escribir(write)


def registrar_pago(courier, *, fecha, moneda, importe, referencia, pdf, clave, actor='admin'):
    courier, actor, clave = operador(courier), texto(actor, 'Actor'), _clave(clave)
    importe = positivo(importe)
    moneda = str(moneda or '').strip().upper()
    if not re.fullmatch('[A-Z]{3}', moneda):
        raise OperadorError('Indicá la moneda de tres letras del comprobante.')
    try:
        fecha = date.fromisoformat(str(fecha))
        if fecha > hoy_ar():
            raise ValueError()
    except ValueError:
        raise OperadorError('La fecha debe corresponder a un pago ya realizado, no futuro.') from None
    referencia = texto(referencia, 'Referencia de transferencia', 3, 200)
    if not isinstance(pdf, bytes) or not pdf.startswith(b'%PDF-') or len(pdf)>8*1024*1024:
        raise OperadorError('Adjuntá el comprobante original PDF (máximo 8 MB).')
    sha = hashlib.sha256(pdf).hexdigest()

    def write(cur):
        _lock(cur, courier)
        cur.execute('SELECT * FROM pagos_operador WHERE clave=%s', (clave,))
        previo = cur.fetchone()
        if previo:
            cur.execute('SELECT 1 FROM reversiones_pago_operador WHERE pago_id=%s',(previo['id'],))
            if cur.fetchone():
                raise OperadorError('Este pago fue revertido. Abrí un formulario nuevo para registrar la corrección.')
            if (previo['courier'], previo['fecha'], previo['moneda'], previo['importe'],
                previo['referencia'], previo['comprobante_sha256']) != (courier,fecha,moneda,importe,referencia,sha):
                raise OperadorError('Esta operación ya existe con datos diferentes.')
            return previo['id']
        cur.execute('''INSERT INTO pagos_operador(courier,fecha,moneda,importe,referencia,
            comprobante_pdf,comprobante_sha256,actor,clave) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (courier,fecha,moneda,importe,referencia,pdf,sha,actor,clave))
        ident = cur.fetchone()['id']
        _auditar(cur, 'OPERADOR_PAGO_REGISTRADO', actor, pago_id=ident, courier=courier)
        return ident
    return _escribir(write)


def aplicar(courier, factura_id, *, importe, tipo_cambio, motivo, clave, pago_id=None, nc_id=None,
            conversion_confirmada=False, actor='admin'):
    courier, clave, actor = operador(courier), _clave(clave), texto(actor, 'Actor')
    importe, tc = positivo(importe), positivo(tipo_cambio, 'TC de cancelación', 8)
    motivo = texto(motivo, 'Respaldo de la aplicación/TC', 5)
    if (pago_id is None) == (nc_id is None):
        raise OperadorError('Elegí un pago o una NC, nunca ambos.')
    if not isinstance(conversion_confirmada, bool):
        raise OperadorError('La confirmación de conversión debe ser explícita.')
    origen = positivo((importe*tc).quantize(Decimal('.0001'), rounding=ROUND_HALF_UP))

    def write(cur):
        _lock(cur, courier)
        _factura(cur, courier, factura_id)
        cur.execute('SELECT * FROM aplicaciones_operador WHERE clave=%s', (clave,))
        previo = cur.fetchone()
        if previo:
            cur.execute('SELECT 1 FROM reversiones_aplicacion_operador WHERE aplicacion_id=%s',(previo['id'],))
            if cur.fetchone():
                raise OperadorError('Esta aplicación fue revertida. Abrí un formulario nuevo para corregirla.')
            if (previo['factura_id'],previo['pago_id'],previo['nc_id'],previo['importe_documento'],
                previo['tipo_cambio'],previo['motivo'],previo['conversion_confirmada']) != (
                    factura_id,pago_id,nc_id,importe,tc,motivo,conversion_confirmada):
                raise OperadorError('Esta aplicación ya existe con datos diferentes.')
            return previo['id']
        # El trigger valida nuevamente monedas, operador, evidencia y ambos saldos bajo lock.
        cur.execute('''INSERT INTO aplicaciones_operador(factura_id,pago_id,nc_id,importe_documento,
            tipo_cambio,importe_origen,motivo,actor,clave,conversion_confirmada)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (factura_id,pago_id,nc_id,importe,tc,origen,motivo,actor,clave,conversion_confirmada))
        ident = cur.fetchone()['id']
        _auditar(cur, 'OPERADOR_APLICACION_REGISTRADA', actor, factura_id, aplicacion_id=ident)
        return ident
    return _escribir(write)


def revertir(courier, tipo, registro_id, motivo, actor='admin'):
    """Contra-registro completo; nunca UPDATE/DELETE ni devolución bancaria."""
    courier, motivo, actor = operador(courier), texto(motivo, 'Motivo de reversión', 10), texto(actor, 'Actor')
    if tipo not in ('pago','aplicacion'):
        raise OperadorError('Reversión no soportada.')
    tabla, campo = ('reversiones_pago_operador','pago_id') if tipo=='pago' else ('reversiones_aplicacion_operador','aplicacion_id')

    def write(cur):
        _lock(cur,courier)
        if tipo=='pago':
            cur.execute('SELECT id FROM pagos_operador WHERE id=%s AND courier=%s',(registro_id,courier))
        else:
            cur.execute('''SELECT a.id FROM aplicaciones_operador a JOIN facturas_courier f ON f.id=a.factura_id
                WHERE a.id=%s AND f.courier=%s''',(registro_id,courier))
        if not cur.fetchone():
            raise OperadorError('Registro inexistente o de otro operador.')
        cur.execute(f'SELECT id FROM {tabla} WHERE {campo}=%s',(registro_id,))
        previo=cur.fetchone()
        if previo:
            return previo['id']
        cur.execute(f'INSERT INTO {tabla}({campo},motivo,actor) VALUES(%s,%s,%s) RETURNING id',(registro_id,motivo,actor))
        ident=cur.fetchone()['id']
        _auditar(cur,'OPERADOR_REVERSION_'+tipo.upper(),actor,registro_id=registro_id,reversion_id=ident,courier=courier)
        return ident
    return _escribir(write)


def estado_documento(f, hoy=None):
    """Saldo según registros no equivale a deuda histórica verificada."""
    f = dict(f)
    hoy = hoy or hoy_ar()
    emision = f.get('fecha_emision')
    venc = f.get('fecha_vencimiento') or f.get('vencimiento_acordado')
    aplicado = f.get('aplicado') or Decimal(0)
    saldo = f['total'] - aplicado
    f.update(saldo=saldo, vencimiento=venc, dias_emision=(hoy-emision).days if emision else None,
             dias_vencimiento=(venc-hoy).days if venc else None, dias_mora=None, fecha_cancelacion=None)
    if f['estado']=='ANULADA':
        estado='ANULADA'
    elif f['tipo_documento']=='NC':
        estado='CREDITO_AGOTADO' if saldo==0 else 'CREDITO_DISPONIBLE' if f.get('verificado') else 'CREDITO_SIN_VERIFICAR'
    elif saldo==0:
        estado='CANCELADA'
        fechas=[d for d in [f.get('ultima_aplicacion'),emision] if d]
        f['fecha_cancelacion']=max(fechas) if fechas else None
        if venc and f['fecha_cancelacion']:
            f['dias_mora']=max(0,(f['fecha_cancelacion']-venc).days)
    elif not f.get('verificado'):
        estado='SIN_VERIFICAR'
    else:
        estado='PARCIAL' if aplicado else 'IMPAGA'
        if venc:
            f['dias_mora']=max(0,(hoy-venc).days)
    f['estado_pago']=estado
    return f


_DOCUMENTOS = '''
SELECT f.id,f.courier,f.tipo_documento,f.numero,f.fecha_emision,f.fecha_vencimiento,
       f.moneda,f.total,f.estado,f.factura_referenciada_id,
       CASE WHEN rect.id IS NOT NULL THEN rect.fecha ELSE v.fecha END AS vencimiento_acordado,
       rect.id AS rectificacion_id,
       EXISTS(SELECT 1 FROM verificaciones_operador h WHERE h.factura_id=f.id) AS verificado,
       CASE WHEN f.tipo_documento='NC' THEN coalesce(nc.usado,0) ELSE coalesce(a.aplicado,0) END AS aplicado,
       a.ultima_aplicacion,
       (SELECT count(*) FROM facturas_courier_items WHERE factura_id=f.id) AS lineas,
       (SELECT count(DISTINCT s.cliente_id) FROM facturas_courier_items i
        JOIN factura_courier_item_matches m ON m.item_id=i.id AND m.estado='CONFIRMADO'
        JOIN solicitudes_guia s ON s.id=m.solicitud_id WHERE i.factura_id=f.id) AS clientes
FROM facturas_courier f
LEFT JOIN vencimientos_operador v ON v.factura_id=f.id
LEFT JOIN LATERAL (SELECT id,fecha FROM rectificaciones_vencimiento_operador
    WHERE factura_id=f.id ORDER BY id DESC LIMIT 1) rect ON TRUE
LEFT JOIN LATERAL (
    SELECT sum(a.importe_documento) AS aplicado,
           max(coalesce(p.fecha,n.fecha_emision)) AS ultima_aplicacion
    FROM aplicaciones_operador a LEFT JOIN pagos_operador p ON p.id=a.pago_id
    LEFT JOIN facturas_courier n ON n.id=a.nc_id WHERE a.factura_id=f.id
    AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id)
) a ON TRUE
LEFT JOIN LATERAL (SELECT sum(importe_origen) AS usado FROM aplicaciones_operador a WHERE nc_id=f.id
    AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id)) nc ON TRUE
'''


def listar_documentos(courier=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_DOCUMENTOS + (' WHERE f.courier=%s' if courier else '') +
                    ' ORDER BY f.fecha_emision DESC NULLS LAST, f.id DESC', (operador(courier),) if courier else ())
        return [estado_documento(f) for f in cur.fetchall()]


def resumen_documentos(documentos):
    resumen={}
    for f in documentos:
        if f['estado']=='ANULADA':
            continue
        k=f['moneda']
        r=resumen.setdefault(k,dict(moneda=k, pendiente=Decimal(0), vencido=Decimal(0),
                                    sin_verificar=0, credito=Decimal(0), documentos=0))
        r['documentos']+=1
        if f['estado_pago'] in ('SIN_VERIFICAR','CREDITO_SIN_VERIFICAR'):
            r['sin_verificar']+=1
        elif f['tipo_documento']=='NC':
            r['credito']+=f['saldo']
        elif f['saldo']>0:
            r['pendiente']+=f['saldo']
            if f['dias_mora']:
                r['vencido']+=f['saldo']
    return list(resumen.values())


def condicion_actual(courier):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM condiciones_operador WHERE courier=%s ORDER BY id DESC LIMIT 1', (operador(courier),))
        return cur.fetchone()


def listar_pagos(courier):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT p.id,p.fecha,p.moneda,p.importe,p.referencia,r.motivo AS motivo_reversion,
            (r.id IS NOT NULL) AS revertido,
            CASE WHEN r.id IS NOT NULL THEN 0 ELSE p.importe-coalesce(a.usado,0) END AS disponible,
            a.facturas FROM pagos_operador p
            LEFT JOIN reversiones_pago_operador r ON r.pago_id=p.id
            LEFT JOIN LATERAL (SELECT sum(importe_origen) AS usado,array_agg(DISTINCT factura_id) AS facturas
                FROM aplicaciones_operador a WHERE pago_id=p.id
                AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id)) a ON TRUE
            WHERE p.courier=%s ORDER BY p.fecha DESC,p.id DESC''', (operador(courier),))
        return [dict(p) for p in cur.fetchall()]


def comprobante_pago(courier, pago_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT comprobante_pdf FROM pagos_operador WHERE id=%s AND courier=%s', (pago_id,operador(courier)))
        row=cur.fetchone()
        return bytes(row['comprobante_pdf']) if row else None


_LINEAS = '''SELECT f.id AS factura_id,f.numero,f.courier,f.tipo_documento,f.fecha_emision,
    i.id AS item_id,i.linea_numero,i.tracking_raw,i.concepto_tipo,i.importe,i.moneda,
    i.signo * CASE WHEN f.tipo_documento='NC' THEN -1 ELSE 1 END AS signo,i.peso_facturado_kg,
    m.monto_asignado AS importe_asignado,m.id AS match_id,s.id AS solicitud_id,s.cliente_id,s.peso_kg,
    con.peso_cotizado_kg,con.peso_final_facturado_kg,con.ajuste_cliente_ars,con.estado AS conciliacion_estado,
    aj.estado AS ajuste_estado
FROM facturas_courier f JOIN facturas_courier_items i ON i.factura_id=f.id
LEFT JOIN factura_courier_item_matches m ON m.item_id=i.id AND m.estado='CONFIRMADO'
LEFT JOIN solicitudes_guia s ON s.id=m.solicitud_id
LEFT JOIN LATERAL (SELECT * FROM conciliaciones_envio WHERE solicitud_id=s.id ORDER BY version DESC LIMIT 1) con ON TRUE
LEFT JOIN ajustes_cliente aj ON aj.conciliacion_id=con.id
'''


def detalle_documento(courier, factura_id):
    courier=operador(courier)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_DOCUMENTOS+' WHERE f.courier=%s AND f.id=%s', (courier,factura_id))
        f=cur.fetchone()
        if not f:
            return None
        f=estado_documento(f)
        cur.execute(_LINEAS+' WHERE f.id=%s ORDER BY i.linea_numero,m.id', (factura_id,))
        f['items']=[dict(i) for i in cur.fetchall()]
        cur.execute('''SELECT a.*,r.motivo AS motivo_reversion,r.created_at AS fecha_reversion,
            p.referencia,p.moneda AS moneda_pago,p.fecha AS fecha_pago,
            n.numero AS numero_nc,n.fecha_emision AS fecha_nc FROM aplicaciones_operador a
            LEFT JOIN pagos_operador p ON p.id=a.pago_id LEFT JOIN facturas_courier n ON n.id=a.nc_id
            LEFT JOIN reversiones_aplicacion_operador r ON r.aplicacion_id=a.id
            WHERE a.factura_id=%s OR a.nc_id=%s ORDER BY a.id''', (factura_id,factura_id))
        f['aplicaciones']=[dict(a) for a in cur.fetchall()]
        cur.execute('SELECT * FROM verificaciones_operador WHERE factura_id=%s ORDER BY id DESC', (factura_id,))
        f['verificaciones']=[dict(v) for v in cur.fetchall()]
        # Suma de asignaciones, no del total de FC ni de la cotización del envío repetida por cada línea.
        cur.execute('''SELECT s.cliente_id,i.moneda,
            sum(m.monto_asignado*i.signo*CASE WHEN f.tipo_documento='NC' THEN -1 ELSE 1 END) AS asignado,
            count(DISTINCT s.id) AS envios FROM facturas_courier_items i
            JOIN facturas_courier f ON f.id=i.factura_id
            JOIN factura_courier_item_matches m ON m.item_id=i.id AND m.estado='CONFIRMADO'
            JOIN solicitudes_guia s ON s.id=m.solicitud_id WHERE i.factura_id=%s GROUP BY s.cliente_id,i.moneda''', (factura_id,))
        f['por_cliente']=[dict(r) for r in cur.fetchall()]
        cur.execute('''SELECT i.linea_numero,i.tracking_raw,i.moneda,
            i.signo*CASE WHEN f.tipo_documento='NC' THEN -1 ELSE 1 END AS signo,
            i.importe-coalesce(sum(m.monto_asignado),0) AS pendiente
            FROM facturas_courier_items i JOIN facturas_courier f ON f.id=i.factura_id
            LEFT JOIN factura_courier_item_matches m
            ON m.item_id=i.id AND m.estado='CONFIRMADO' WHERE i.factura_id=%s
            GROUP BY i.id,f.tipo_documento HAVING i.importe<>coalesce(sum(m.monto_asignado),0)
            ORDER BY i.linea_numero''',(factura_id,))
        f['pendientes_asignacion']=[dict(r) for r in cur.fetchall()]
        cur.execute('SELECT coalesce(sum(importe*signo),0) AS desglosado FROM facturas_courier_items WHERE factura_id=%s',(factura_id,))
        f['sin_desglosar']=f['total']-cur.fetchone()['desglosado']
        cur.execute('SELECT * FROM rectificaciones_vencimiento_operador WHERE factura_id=%s ORDER BY id DESC',(factura_id,))
        f['rectificaciones']=[dict(r) for r in cur.fetchall()]
        return f


def rectificar_vencimiento(courier,factura_id,fecha,motivo,clave,actor='admin'):
    courier,motivo,clave,actor=operador(courier),texto(motivo,'Respaldo de rectificación',10),_clave(clave),texto(actor,'Actor')
    try:
        fecha=date.fromisoformat(str(fecha)) if fecha else None
    except ValueError:
        raise OperadorError('Fecha de vencimiento inválida.') from None

    def write(cur):
        _lock(cur,courier)
        _factura(cur,courier,factura_id)
        cur.execute('SELECT * FROM rectificaciones_vencimiento_operador WHERE clave=%s',(clave,))
        previo=cur.fetchone()
        if previo:
            if (previo['factura_id'],previo['fecha'],previo['motivo'])!=(factura_id,fecha,motivo):
                raise OperadorError('Esta rectificación ya existe con otros datos.')
            cur.execute('SELECT max(id) AS id FROM rectificaciones_vencimiento_operador WHERE factura_id=%s',(factura_id,))
            if cur.fetchone()['id']!=previo['id']:
                raise OperadorError('Esta rectificación ya fue reemplazada. Recargá la página.')
            return previo['id']
        cur.execute('''INSERT INTO rectificaciones_vencimiento_operador(factura_id,fecha,motivo,clave,actor)
            VALUES(%s,%s,%s,%s,%s) RETURNING id''',(factura_id,fecha,motivo,clave,actor))
        ident=cur.fetchone()['id']
        _auditar(cur,'OPERADOR_VENCIMIENTO_RECTIFICADO',actor,factura_id,rectificacion_id=ident)
        return ident
    return _escribir(write)


def documentos_cliente(cliente_id, pagina=1):
    cliente_id=str(cliente_id or '').strip().upper()
    pagina=max(1,int(pagina))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT cliente_id,nombre FROM clientes WHERE cliente_id=%s',(cliente_id,))
        cliente=cur.fetchone()
        if not cliente:
            return None
        where=" WHERE s.cliente_id=%s AND f.estado<>'ANULADA'"
        cur.execute('SELECT count(*) FROM ('+_LINEAS+where+') filas',(cliente_id,))
        total=cur.fetchone()['count']
        cur.execute(_LINEAS+where+' ORDER BY f.fecha_emision DESC NULLS LAST,f.id DESC,i.linea_numero,m.id LIMIT 100 OFFSET %s',
                    (cliente_id,(pagina-1)*100))
        return dict(cliente=dict(cliente),items=[dict(r) for r in cur.fetchall()],total=total,pagina=pagina)
