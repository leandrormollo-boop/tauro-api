"""Lecturas del Admin por cliente y proveedor, sin asientos ni llamadas a couriers.

Una fila por solicitud. Los cobros por factura no se prorratean entre envíos:
una aplicación parcial a una factura no prueba qué envío quedó pagado.
"""
from decimal import Decimal
from urllib.parse import quote, urlencode

from core.database import get_conn
from servicios import operadores_logisticos as operadores

PROVEEDORES = {'OCA': 'OCA', 'ANDREANI': 'Andreani',
               'CORREO_ARGENTINO': 'Correo Argentino', 'DHL': 'DHL', 'FEDEX': 'FedEx'}
CERO = Decimal('0')
PAGE_SIZE = 25
_BASE = """FROM solicitudes_guia s JOIN clientes c ON c.cliente_id=s.cliente_id
 WHERE NOT c.test AND NOT s.test
 AND (NULLIF(BTRIM(s.tracking),'') IS NOT NULL OR s.guia_generada_at IS NOT NULL)"""
_FILTROS = {
    'vigentes': "s.estado NOT IN ('CANCELADO','REEMPLAZADO')",
    'retenidos': "s.estado NOT IN ('CANCELADO','REEMPLAZADO') AND s.tracking_estado='RETENIDO'",
    'modificados': "s.estado='REEMPLAZADO'",
    'cancelados': "s.estado='CANCELADO'",
}


def cliente_url(cliente_id):
    return '/admin/clientes/' + quote(cliente_id, safe='')


def obtener_cliente(cliente_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT cliente_id,nombre,email,activo,tope_deuda_ars,
                       puede_emitir,puede_recolectar FROM clientes WHERE cliente_id=%s''',
                    (cliente_id,))
        fila = cur.fetchone()
        return dict(fila) if fila else None


def resumen_proveedores():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT UPPER(BTRIM(s.courier)) AS courier,
            COUNT(*) AS envios, COUNT(DISTINCT s.cliente_id) AS clientes,
            COUNT(*) FILTER (WHERE s.tracking_estado='RETENIDO') AS retenidos,
            COUNT(*) FILTER (WHERE NOT EXISTS (
                SELECT 1 FROM factura_courier_item_matches m
                JOIN facturas_courier_items i ON i.id=m.item_id
                JOIN facturas_courier f ON f.id=i.factura_id
                WHERE m.solicitud_id=s.id AND m.estado='CONFIRMADO'
                  AND f.estado<>'ANULADA' AND f.tipo_documento<>'NC')) AS sin_factura
            ''' + _BASE + " AND " + _FILTROS['vigentes'] + ' GROUP BY UPPER(BTRIM(s.courier))')
        return {r['courier']: dict(r) for r in cur.fetchall()}


def _documentos_proveedor(cur, ids):
    cur.execute('''SELECT DISTINCT m.solicitud_id,i.factura_id,m.estado AS match_estado
        FROM factura_courier_item_matches m
        JOIN facturas_courier_items i ON i.id=m.item_id
        JOIN facturas_courier f ON f.id=i.factura_id
        WHERE m.solicitud_id=ANY(%s) AND m.estado IN ('CONFIRMADO','PROPUESTO')
          AND f.estado<>'ANULADA' ORDER BY i.factura_id,m.estado''', (ids,))
    relaciones = [dict(r) for r in cur.fetchall()]
    documentos = {}
    if relaciones:
        cur.execute(operadores._DOCUMENTOS + ' WHERE f.id=ANY(%s)',
                    (list({r['factura_id'] for r in relaciones}),))
        documentos = {r['id']: operadores.estado_documento(r) for r in cur.fetchall()}
    resultado = {sid: [] for sid in ids}
    # Confirmado prevalece sobre otra propuesta de la misma factura y envío.
    relaciones.sort(key=lambda r: r['match_estado'] != 'CONFIRMADO')
    vistos = set()
    for r in relaciones:
        key = (r['solicitud_id'], r['factura_id'])
        if key in vistos:
            continue
        vistos.add(key)
        resultado[r['solicitud_id']].append(dict(documentos[r['factura_id']],
                                                match_estado=r['match_estado']))
    return resultado


def _cobros(cur, ids):
    cur.execute('''SELECT e.id,e.solicitud_id,e.estado,e.monto_ars,e.nro_fc,
        COALESCE((SELECT SUM(pa.monto_ars) FROM pagos_aplicaciones pa
            JOIN pagos p ON p.id=pa.pago_id
            WHERE pa.envio_id=e.id AND pa.estado='APLICADA'
              AND COALESCE(p.estado,'APROBADO')='APROBADO'
              AND p.cliente_id=e.cliente_id),0) AS pagado
        FROM envios e JOIN solicitudes_guia s ON s.id=e.solicitud_id AND s.cliente_id=e.cliente_id
        WHERE e.solicitud_id=ANY(%s) ORDER BY e.id''', (ids,))
    cargos = {sid: [] for sid in ids}
    for r in cur.fetchall():
        cargos[r['solicitud_id']].append(dict(r))
    cur.execute('''SELECT solicitud_id,SUM(monto_ars) AS ajuste FROM ajustes_cliente
        WHERE solicitud_id=ANY(%s) AND estado='APLICADO' GROUP BY solicitud_id''', (ids,))
    ajustes = {r['solicitud_id']: r['ajuste'] for r in cur.fetchall()}
    # DISTINCT evita multiplicar una factura por sus líneas o ajustes.
    cur.execute('''SELECT DISTINCT COALESCE(e.solicitud_id,a.solicitud_id) AS solicitud_id,
            f.id,f.cliente_id,f.tipo,f.punto_venta,f.numero,f.total,f.fecha_vencimiento,
            COALESCE((SELECT SUM(pa.monto_ars) FROM pagos_aplicaciones pa
                JOIN pagos p ON p.id=pa.pago_id
                WHERE pa.estado='APLICADA' AND COALESCE(p.estado,'APROBADO')='APROBADO'
                  AND p.cliente_id=f.cliente_id AND (pa.factura_id=f.id OR pa.envio_id IN (
                      SELECT envio_id FROM facturas_cliente_items WHERE factura_id=f.id))),0) AS pagado
        FROM facturas_cliente_items i JOIN facturas_cliente f ON f.id=i.factura_id
        LEFT JOIN envios e ON e.id=i.envio_id
        LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
        WHERE f.estado='EMITIDA' AND f.tipo='FC'
          AND COALESCE(e.solicitud_id,a.solicitud_id)=ANY(%s)''', (ids,))
    facturas = {sid: [] for sid in ids}
    for r in cur.fetchall():
        facturas[r['solicitud_id']].append(dict(r))
    return cargos, ajustes, facturas


def estado_cobro(envio, cargos, ajuste, facturas):
    """Estado documental, nunca presume pago por el saldo global del cliente."""
    if envio['estado'] in ('CANCELADO', 'REEMPLAZADO'):
        return dict(importe=CERO, label='Sin cargo al cliente', tono='muted',
                    inconsistencia=any(c['estado']=='ACTIVO' and c['monto_ars'] for c in cargos))
    activos = [c for c in cargos if c['estado']=='ACTIVO']
    if not activos:
        return dict(importe=None, label='Cargo pendiente', tono='warn', inconsistencia=False)
    importe = sum((c['monto_ars'] for c in activos), CERO) + ajuste
    directo = sum((c['pagado'] for c in activos), CERO)
    if importe <= 0:
        label, tono = 'Sin importe a cobrar', 'muted'
    elif directo >= importe:
        label, tono = 'Cobrado · imputación al envío', 'ok'
    elif facturas and all(f['pagado'] >= f['total'] for f in facturas):
        # Una FC puede cubrir sólo parte del cargo/ajuste. La cobertura de todas
        # las partidas se verifica con el importe documentado en la consulta.
        if envio['documentado'] >= importe:
            label, tono = 'Cobrado · facturas saldadas', 'ok'
        else:
            label, tono = 'Facturas saldadas · cargo sin facturar', 'warn'
    elif any(f['pagado'] > 0 for f in facturas):
        label, tono = 'Pago parcial de factura', 'warn'
    elif directo > 0:
        label, tono = 'Cobro parcial del envío', 'warn'
    else:
        label, tono = 'Sin pago imputado', 'warn'
    return dict(importe=importe, label=label, tono=tono, inconsistencia=False)


def listar_envios(*, cliente_id=None, courier=None, q='', estado='vigentes', pagina=1):
    pagina = max(1, min(int(pagina), 1000000))
    q = str(q or '').strip()[:100]
    estado = estado if estado in _FILTROS else 'vigentes'
    where, params = _BASE, []
    if cliente_id:
        where += ' AND s.cliente_id=%s'
        params.append(cliente_id)
    if courier:
        where += ' AND UPPER(BTRIM(s.courier))=%s'
        params.append(courier)
    if q:
        where += " AND concat_ws(' ',s.tracking,s.numero_guia_tauro,s.dest_nombre,s.cliente_id,c.nombre,s.dest_ciudad) ILIKE %s"
        # Buscar texto literal, no comodines ingresados por el usuario.
        params.append('%' + q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%')
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cur.execute('SELECT ' + ','.join('COUNT(*) FILTER (WHERE '+expr+') AS '+key
                    for key, expr in _FILTROS.items()) + ' ' + where, params)
        conteos = dict(cur.fetchone())
        total = conteos[estado]
        paginas = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        pagina = min(pagina, paginas)
        cur.execute('''SELECT s.id,s.cliente_id,c.nombre AS cliente_nombre,s.estado,s.courier,
                s.numero_guia_tauro,s.tracking,s.dest_nombre,s.dest_ciudad,s.destino_pais,
                s.remitente_pais,s.created_at,s.tracking_estado,s.ambito,
                COALESCE((SELECT SUM(i.monto) FROM facturas_cliente_items i
                    JOIN facturas_cliente f ON f.id=i.factura_id
                    LEFT JOIN envios e ON e.id=i.envio_id
                    LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
                    WHERE f.estado='EMITIDA' AND f.tipo='FC' AND f.cliente_id=s.cliente_id
                      AND COALESCE(e.solicitud_id,a.solicitud_id)=s.id),0) AS documentado
                ''' + where + ' AND ' + _FILTROS[estado] +
                ' ORDER BY s.created_at DESC,s.id DESC LIMIT %s OFFSET %s',
                    [*params, PAGE_SIZE, (pagina-1)*PAGE_SIZE])
        items = [dict(r) for r in cur.fetchall()]
        ids = [r['id'] for r in items]
        if ids:
            docs = _documentos_proveedor(cur, ids)
            cargos, ajustes, facturas = _cobros(cur, ids)
            for r in items:
                sid = r['id']
                r['cobro'] = estado_cobro(r, cargos[sid], ajustes.get(sid, CERO), facturas[sid])
                r['facturas_cliente'] = facturas[sid]
                r['documentos'] = docs[sid]
                r['cliente_url'] = cliente_url(r['cliente_id'])
                r['courier'] = str(r['courier'] or '').strip().upper()
                r['proveedor_nombre'] = PROVEEDORES.get(r['courier'], r['courier'] or 'Sin operador')
                r['proveedor_url'] = ('/admin/operadores/' + r['courier']) if r['courier'] in PROVEEDORES else None
    def url(n, filtro=estado):
        return '?' + urlencode(dict(vista='envios', pagina=n, estado=filtro, q=q))
    return dict(items=items,total=total,pagina=pagina,paginas=paginas,q=q,estado=estado,
                conteos=conteos,prev_url=url(pagina-1),next_url=url(pagina+1),
                filtros=[dict(clave=k,nombre=n,total=conteos[k],url=url(1,k)) for k,n in (
                    ('vigentes','Vigentes'),('retenidos','Retenidos'),
                    ('modificados','Modificados'),('cancelados','Cancelados'))])


def agenda_cliente(cliente_id, vista, pagina=1):
    """Paginación también para domicilios y retiros; nunca incluye PDFs en memoria."""
    pagina = max(1, min(int(pagina), 1000000))
    with get_conn() as conn, conn.cursor() as cur:
        if vista == 'destinatarios':
            tabla, condicion = 'direcciones', "cliente_id=%s AND tipo='DESTINATARIO'"
            campos, orden = 'id,alias,nombre,pais,ciudad,direccion,email,telefono,cp', 'nombre,id'
        else:
            tabla, condicion = 'recolecciones', 'cliente_id=%s'
            campos, orden = 'id,courier,fecha,ready_time,close_time,estado,confirmation_code,solicitud_id,direccion,ubicacion', 'fecha DESC,id DESC'
        cur.execute('SELECT COUNT(*) AS total FROM '+tabla+' WHERE '+condicion, (cliente_id,))
        total = cur.fetchone()['total']
        paginas = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
        pagina = min(pagina, paginas)
        cur.execute('SELECT '+campos+' FROM '+tabla+' WHERE '+condicion+' ORDER BY '+orden+' LIMIT %s OFFSET %s',
                    (cliente_id,PAGE_SIZE,(pagina-1)*PAGE_SIZE))
        return dict(items=[dict(r) for r in cur.fetchall()],total=total,pagina=pagina,paginas=paginas)


def aplicaciones_pagos(cliente_id, pago_ids):
    """Documentos de los pagos visibles, en una consulta y sin leer adjuntos."""
    resultado = {pid: [] for pid in pago_ids}
    if not pago_ids:
        return resultado
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT pa.pago_id,pa.monto_ars,pa.estado,pa.ambito,pa.envio_id,
            e.solicitud_id,e.tracking,pa.factura_id,f.punto_venta,f.numero
            FROM pagos_aplicaciones pa JOIN pagos p ON p.id=pa.pago_id
            LEFT JOIN envios e ON e.id=pa.envio_id AND e.cliente_id=p.cliente_id
            LEFT JOIN facturas_cliente f ON f.id=pa.factura_id AND f.cliente_id=p.cliente_id
            WHERE p.cliente_id=%s AND p.id=ANY(%s) ORDER BY pa.id''', (cliente_id,pago_ids))
        for fila in cur.fetchall():
            resultado[fila['pago_id']].append(dict(fila))
    return resultado
