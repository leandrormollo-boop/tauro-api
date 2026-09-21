"""Atajos privados de cotización a partir de guías vigentes del cliente."""
import logging

from core.database import get_conn
from servicios.paises import nombre, normalizar_iso2

logger = logging.getLogger(__name__)


def obtener_rutas_frecuentes(cliente_id: str) -> list[dict]:
    """Sólo lectura. Hasta seis sentidos reales de los últimos doce meses.

    No cuenta borradores, pruebas, guías ocultas, anuladas o reemplazadas.
    La identidad proviene de la sesión; nunca se toman datos de otra cuenta
    para completar una lista vacía. No presupone Argentina cuando falta origen.
    """
    if not isinstance(cliente_id, str) or not cliente_id.strip():
        return []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(NULLIF(BTRIM(s.remitente_pais), ''), r.origen_pais) AS origen,
                       s.destino_pais AS destino, COUNT(*) AS envios,
                       MAX(s.created_at) AS ultima_vez
                FROM solicitudes_guia s
                LEFT JOIN rutas r ON r.ruta_id = s.ruta_id
                WHERE s.cliente_id = %s
                  AND s.test = FALSE AND s.visible_cliente = TRUE
                  AND s.estado IN ('GUIA_LISTA', 'DESPACHADO', 'ENTREGADO')
                  AND NULLIF(BTRIM(s.tracking), '') IS NOT NULL
                  AND s.created_at >= NOW() - INTERVAL '12 months'
                  AND s.created_at <= NOW()
                  AND NOT EXISTS (
                      SELECT 1 FROM envios e
                      WHERE e.solicitud_id = s.id AND e.cliente_id = s.cliente_id
                        AND e.estado <> 'ACTIVO'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM solicitudes_guia_reemisiones h
                      WHERE h.solicitud_anterior_id = s.id AND h.cliente_id = s.cliente_id
                        AND h.estado = 'EMITIDA'
                  )
                GROUP BY 1, 2
            """, (cliente_id.strip().upper(),))
            rows = cur.fetchall()
    except Exception as exc:
        # Una sugerencia opcional no debe impedir cotizar. Sin datos ajenos
        # ni detalles de conexión en logs; el formulario manual sigue usable.
        logger.warning('rutas_frecuentes_no_disponibles: %s', type(exc).__name__)
        return []

    routes = {}
    for row in rows:
        origin = normalizar_iso2(row['origen'])
        destination = normalizar_iso2(row['destino'])
        if not origin or not destination or origin == destination:
            continue
        key = (origin, destination)
        last = row['ultima_vez'].timestamp()
        if key not in routes:
            routes[key] = {'origen': origin, 'destino': destination,
                           'origen_nombre': nombre(origin), 'destino_nombre': nombre(destination),
                           'envios': 0, '_ultima': last}
        routes[key]['envios'] += int(row['envios'])
        routes[key]['_ultima'] = max(routes[key]['_ultima'], last)
    ordered = sorted(routes.values(), key=lambda r: (-r['envios'], -r['_ultima'], r['origen'], r['destino']))
    return [{k: v for k, v in route.items() if k != '_ultima'} for route in ordered[:6]]
