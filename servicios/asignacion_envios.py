"""Asignación administrativa de guías emitidas sin movimientos contables.

Pendiente significa cliente_id NULL, nunca un cliente ficticio o una cancelación.
Los cargos, pagos, documentos del courier y datos de la guía no se reescriben.
"""
from core.database import get_conn
from servicios.auditoria import registrar_evento_con_cursor


def _bloqueo(cur, s):
    if s.get('test'):
        return 'Los casos de prueba no se reasignan desde esta pantalla.'
    if not s.get('tracking') or s['estado'] in (
        'EMITIENDO', 'VERIFICAR_COURIER', 'CANCELADO', 'REEMPLAZADO'
    ):
        return 'Sólo se puede asignar una guía emitida y vigente.'
    if s.get('origen_plataforma') or s.get('coti_id'):
        return 'Tiene una cotización o tienda vinculada. Revisá su origen antes de cambiar de cliente.'
    cur.execute('''SELECT EXISTS(SELECT 1 FROM envios
        WHERE solicitud_id=%s OR (cliente_id=%s AND
          NULLIF(REGEXP_REPLACE(UPPER(BTRIM(tracking)), '[^A-Z0-9]', '', 'g'),'') =
          NULLIF(REGEXP_REPLACE(UPPER(BTRIM(%s)), '[^A-Z0-9]', '', 'g'),''))) AS cargos,
        EXISTS(SELECT 1 FROM ajustes_cliente WHERE solicitud_id=%s) AS ajustes,
        EXISTS(SELECT 1 FROM envio_cotizacion_snapshots WHERE solicitud_id=%s) AS base_comercial,
        EXISTS(SELECT 1 FROM recolecciones WHERE solicitud_id=%s) AS retiros,
        EXISTS(SELECT 1 FROM solicitudes_guia_reemisiones
          WHERE solicitud_anterior_id=%s OR solicitud_nueva_id=%s) AS reemisiones''',
        (s['id'], s['cliente_id'], s['tracking'], s['id'], s['id'], s['id'], s['id'], s['id']))
    relaciones = cur.fetchone()
    if relaciones['cargos'] or relaciones['ajustes']:
        return 'Tiene cargos o ajustes registrados. Conciliá su cuenta antes de cambiar el cliente; no se trasladan pagos ni facturas automáticamente.'
    if relaciones['base_comercial']:
        return 'Tiene una base comercial aceptada. Revisá el precio y margen antes de cambiar de cliente.'
    if relaciones['retiros'] or relaciones['reemisiones']:
        return 'Tiene una recolección o reemisión vinculada. Revisá esa operación antes de cambiar de cliente.'
    return ''


def panel_asignacion(solicitud_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('SELECT * FROM solicitudes_guia WHERE id=%s', (solicitud_id,))
        s = cur.fetchone()
        if not s:
            return None
        bloqueo = _bloqueo(cur, s)
        cur.execute('''SELECT cliente_id,nombre FROM clientes
            WHERE NOT test AND activo ORDER BY cliente_id''')
        clientes = [dict(r) for r in cur.fetchall()]
        cur.execute('''SELECT cliente_anterior,cliente_nuevo,cliente_indicado,
                motivo,created_at FROM asignaciones_envio
            WHERE solicitud_id=%s ORDER BY id DESC LIMIT 12''', (solicitud_id,))
        return dict(bloqueo=bloqueo, clientes=clientes,
                    historial=[dict(r) for r in cur.fetchall()])


def cambiar_asignacion(solicitud_id, *, cliente_esperado, cliente_nuevo,
                       cliente_indicado, motivo, actor='admin'):
    """CAS + bloqueo de fila + auditoría atómica. Nunca emite, cobra ni notifica."""
    esperado = str(cliente_esperado or '').strip().upper() or None
    nuevo = str(cliente_nuevo or '').strip().upper() or None
    indicado = str(cliente_indicado or '').strip()
    motivo = str(motivo or '').strip()
    if not 5 <= len(motivo) <= 500:
        raise ValueError('Indicá el motivo del cambio (entre 5 y 500 caracteres).')
    if len(indicado) > 120 or (nuevo is None and not indicado):
        raise ValueError('Indicá a qué cliente corresponde (hasta 120 caracteres).')
    with get_conn() as conn, conn.cursor() as cur:
        # Bloquea también inserciones de hijos con FK hasta terminar el control.
        cur.execute('SELECT * FROM solicitudes_guia WHERE id=%s FOR UPDATE', (solicitud_id,))
        s = cur.fetchone()
        if not s:
            raise ValueError('El envío no existe.')
        if s['cliente_id'] != esperado:
            raise ValueError('La asignación cambió en otra pestaña. Actualizá la página antes de continuar.')
        bloqueo = _bloqueo(cur, s)
        if bloqueo:
            raise ValueError(bloqueo)
        if nuevo:
            cur.execute('''SELECT cliente_id FROM clientes WHERE cliente_id=%s
                AND activo AND NOT test FOR SHARE''', (nuevo,))
            if not cur.fetchone():
                raise ValueError('Elegí un perfil de cliente activo.')
        if nuevo == esperado:
            raise ValueError('El envío ya tiene esa asignación.')
        # La visibilidad se habilita expresamente al asignar al perfil correcto.
        # No se crea ni se modifica ningún cargo por esta acción.
        cur.execute('''UPDATE solicitudes_guia SET cliente_id=%s,
            cliente_pendiente_nombre=%s, visible_cliente=%s,
            cargo_pendiente=FALSE, updated_at=NOW() WHERE id=%s''',
            (nuevo, indicado or None, bool(nuevo), solicitud_id))
        cur.execute('''INSERT INTO asignaciones_envio
            (solicitud_id,cliente_anterior,cliente_nuevo,cliente_indicado,motivo,actor)
            VALUES (%s,%s,%s,%s,%s,%s)''',
            (solicitud_id, esperado, nuevo, indicado or None, motivo, actor))
        registrar_evento_con_cursor(cur, event='admin.asignacion_envio',
            actor_type='admin', actor_ref=actor, ip=None, method='POST',
            path=f'/admin/pedidos/{solicitud_id}/asignacion', status_code=303,
            success=True, request_id=None,
            metadata={'solicitud_id':solicitud_id,'cliente_anterior':esperado,
                      'cliente_nuevo':nuevo,'pendiente':nuevo is None})
        return dict(id=solicitud_id, cliente_id=nuevo)
