"""Presentación del control financiero. No escribe ni calcula cargos."""
from urllib.parse import urlencode, urlsplit, parse_qsl

ESTADOS_CONTROL = (
    ('REQUIERE_ACCION', 'Requieren revisión'),
    ('CANCELADO_FACTURADO', 'Cancelado pero facturado'),
    ('REEMPLAZADO_FACTURADO', 'Reemplazado pero facturado'),
    ('SIN_FACTURA_30D', 'Sin factura hace más de 30 días'),
    ('BASE_PENDIENTE', 'Falta costo estimado'),
    ('MATCH_PENDIENTE', 'Vinculación por confirmar'),
    ('EVIDENCIA_PENDIENTE', 'Falta evidencia'),
    ('LISTO_PARA_CALCULAR', 'Listos para comparar'),
    ('DIFERENCIA_PENDIENTE', 'Diferencia por revisar'),
    ('ESPERANDO_FACTURA', 'Esperando factura'),
    ('CONCILIADO', 'Control terminado'),
    ('SIN_CARGO', 'Sin cargo activo'),
)

ESTADOS_FACTURA = (
    ('ABIERTAS', 'Control abierto'),
    ('BANDEJAS_PENDIENTES', 'Con guías en bandeja'),
    ('NO_CUADRA', 'No cuadra con el total'),
    ('GUIAS_SIN_MATCH', 'Con guías sin identificar'),
    ('MATCH_PENDIENTE', 'Vinculación por confirmar'),
    ('TERMINADAS', 'Control terminado'),
)

# Bandejas de una guía facturada (control de facturas de operadores).
BANDEJAS_FACTURA = (
    ('REEMPLAZADA', 'Guía reemplazada'),
    ('CANCELADA', 'Guía cancelada'),
    ('SIN_DUENO', 'Sin dueño'),
    ('YA_FACTURADO', 'Ya facturado'),
)


def numero_pagina(valor):
    try:
        return max(1, min(int(valor), 1000000))
    except (TypeError, ValueError, OverflowError):
        return 1


def retorno_control(valor, predeterminado):
    """Conserva la búsqueda al volver del expediente, sólo a listas internas."""
    if not isinstance(valor, str) or len(valor) > 2000:
        return predeterminado
    try:
        url = urlsplit(valor)
    except ValueError:
        return predeterminado
    if url.scheme or url.netloc or url.path not in {
        '/admin/facturas-internacionales', '/admin/facturas-nacionales',
        '/admin/conciliacion-couriers',
    }:
        return predeterminado
    params = [(k, v) for k, v in parse_qsl(url.query) if k in {
        'vista', 'pagina', 'courier', 'cliente', 'buscar', 'estado', 'bandeja',
    }]
    return url.path + ('?' + urlencode(params) if params else '')


def preparar_presentacion(*, control, facturas, ajustes, filtros, ruta, vista, pagina):
    """Construye enlaces y ventanas sin truncar los totales del universo."""
    totales = control.get('totales', {})
    requieren = sum(v for k, v in totales.items()
                    if k not in ('CONCILIADO', 'SIN_CARGO', 'ESPERANDO_FACTURA'))

    def enlace(**cambios):
        params = {'vista': vista, **{k: v for k, v in filtros.items() if v}}
        nueva_vista = cambios.get('vista', vista)
        if nueva_vista != vista:
            params.pop('buscar', None)
            params.pop('estado', None)
        # Los filtros de cliente pertenecen a envíos y ajustes. Una factura
        # puede contener varios clientes y se conserva completa.
        if nueva_vista in ('resumen', 'facturas'):
            params.pop('cliente', None)
        params.update(cambios)
        return ruta + '?' + urlencode({k: v for k, v in params.items() if v not in ('', None)})

    filas = facturas if vista == 'facturas' else ajustes
    if vista == 'facturas':
        estado = filtros.get('estado', '').upper()
        if estado == 'ABIERTAS':
            filas = [f for f in filas if f.get('estado') not in ('CONCILIADA', 'CERRADA', 'ANULADA')]
        elif estado == 'GUIAS_SIN_MATCH':
            filas = [f for f in filas if (f.get('guias') or 0) > (f.get('con_match') or 0)]
        elif estado == 'MATCH_PENDIENTE':
            filas = [f for f in filas if (f.get('propuestos') or 0) > 0]
        elif estado == 'TERMINADAS':
            filas = [f for f in filas if f.get('estado') in ('CONCILIADA', 'CERRADA')]
        elif estado == 'BANDEJAS_PENDIENTES':
            filas = [f for f in filas if (f.get('bandejas_pendientes') or 0) > 0]
        elif estado == 'NO_CUADRA':
            filas = [f for f in filas if f.get('cuadra_documento') is False and (f.get('lineas') or 0) > 0]
        bandeja = (filtros.get('bandeja') or '').upper()
        if bandeja:
            filas = [f for f in filas if (f.get('bandejas_detalle') or {}).get(bandeja)]
    if vista == 'facturas' and filtros.get('buscar'):
        texto = filtros['buscar'].strip().casefold()
        filas = [f for f in filas if texto in str(f.get('numero', '')).casefold()]
    if vista == 'conciliacion':
        ventana = {k: control.get(k, v) for k, v in {
            'pagina': 1, 'paginas': 1, 'desde': 1 if control.get('total') else 0,
            'hasta': len(control['items']), 'total': control['total'],
        }.items()}
        visibles = control['items']
    else:
        total = len(filas)
        paginas = max(1, (total + 24) // 25)
        actual = min(numero_pagina(pagina), paginas)
        inicio = (actual - 1) * 25
        visibles = filas[inicio:inicio + 25]
        ventana = dict(pagina=actual, paginas=paginas, desde=inicio + 1 if total else 0,
                       hasta=min(inicio + 25, total), total=total)
    return {'enlace': enlace, 'estados_control': ESTADOS_CONTROL, 'estados_factura': ESTADOS_FACTURA,
            'etiquetas_control': dict(ESTADOS_CONTROL),
            'bandejas_factura': BANDEJAS_FACTURA, 'etiquetas_bandeja': dict(BANDEJAS_FACTURA),
            'requieren_revision': requieren, 'ventana': ventana,
            'filas_visibles': visibles}
