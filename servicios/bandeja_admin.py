# ============================================================
# Bandeja operativa del admin, agrupada por cliente
# ============================================================
# Lo que Tauro necesita ver de un vistazo: qué cliente tiene trabajo
# esperando y de qué tipo, para decidir si lo gestiona Tauro o lo deja
# en manos del cliente.
#
# Dos tipos de pendiente distintos:
#   · solicitudes SOLICITADO  → esperan que TAURO emita la guía
#   · pedidos de tienda       → ventas que el cliente todavía no armó
# ============================================================
from __future__ import annotations

from core.database import get_conn


def resumen_por_cliente() -> list[dict]:
    """Una fila por cliente con su carga de trabajo pendiente."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.cliente_id,
                       c.email,
                       COALESCE(s.solicitadas, 0)  AS solicitadas,
                       COALESCE(s.en_proceso, 0)   AS en_proceso,
                       COALESCE(s.guias_listas, 0) AS guias_listas,
                       COALESCE(p.pendientes, 0)   AS pedidos_tienda,
                       s.ultima
                FROM clientes c
                LEFT JOIN (
                    SELECT cliente_id,
                           COUNT(*) FILTER (WHERE estado = 'SOLICITADO')  AS solicitadas,
                           COUNT(*) FILTER (WHERE estado = 'EN_PROCESO')  AS en_proceso,
                           COUNT(*) FILTER (WHERE estado = 'GUIA_LISTA')  AS guias_listas,
                           MAX(created_at) AS ultima
                    FROM solicitudes_guia
                    GROUP BY cliente_id
                ) s ON s.cliente_id = c.cliente_id
                LEFT JOIN (
                    SELECT cliente_id, COUNT(*) AS pendientes
                    FROM pedidos_tienda
                    WHERE estado = 'PENDIENTE'
                    GROUP BY cliente_id
                ) p ON p.cliente_id = c.cliente_id
                WHERE c.test = FALSE
                ORDER BY
                    (COALESCE(s.solicitadas, 0) + COALESCE(p.pendientes, 0)) DESC,
                    c.cliente_id
            """)
            filas = [dict(r) for r in cur.fetchall()]

    for f in filas:
        # "Requiere acción de Tauro" = guías por emitir. Los pedidos de
        # tienda son del cliente, pero los mostramos para poder ofrecerle
        # gestionarlos nosotros.
        f["pendiente_tauro"] = int(f["solicitadas"] or 0)
        f["pendiente_cliente"] = int(f["pedidos_tienda"] or 0)
        f["total_pendiente"] = f["pendiente_tauro"] + f["pendiente_cliente"]
    return filas


def detalle_cliente(cliente_id: str) -> dict:
    """Todo lo pendiente de un cliente, para la vista de su pestaña."""
    cliente_id = (cliente_id or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM solicitudes_guia
                WHERE cliente_id = %s AND estado IN ('SOLICITADO', 'EN_PROCESO')
                ORDER BY created_at DESC
                LIMIT 200
            """, (cliente_id,))
            solicitudes = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT * FROM pedidos_tienda
                WHERE cliente_id = %s AND estado = 'PENDIENTE'
                ORDER BY id DESC
                LIMIT 200
            """, (cliente_id,))
            pedidos = [dict(r) for r in cur.fetchall()]

    return {"solicitudes": solicitudes, "pedidos_tienda": pedidos}


def total_pendiente_tauro() -> int:
    """Para el globo del menú del admin."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS n
                    FROM solicitudes_guia s
                    JOIN clientes c ON c.cliente_id=s.cliente_id
                    WHERE s.estado='SOLICITADO'
                      AND s.test=FALSE AND c.test=FALSE
                """)
                return int(cur.fetchone()["n"])
    except Exception as e:
        print(f"[bandeja_admin] no pude contar pendientes: {e}")
        return 0
