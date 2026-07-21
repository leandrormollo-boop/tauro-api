# ============================================================
# Servicio de catálogo de productos — PostgreSQL
# ============================================================

from datetime import datetime, timezone
from typing import List, Optional

from core.database import get_conn
from modelos.producto import Producto, ProductoNuevo


def _row_a_producto(r: dict) -> Optional[Producto]:
    try:
        return Producto(
            cliente=str(r["cliente_id"]).strip().upper(),
            alias_interno=str(r["alias_interno"]).strip(),
            nombre_invoice=str(r["nombre_invoice"]).strip(),
            hs_code=str(r["hs_code"]).strip(),
            largo_cm=float(r["largo_cm"] or 0),
            ancho_cm=float(r["ancho_cm"] or 0),
            alto_cm=float(r["alto_cm"] or 0),
            peso_kg=float(r["peso_kg"] or 0),
            valor_usd_default=float(r["valor_usd_default"] or 0),
            activo=bool(r["activo"]),
        )
    except Exception:
        return None


def get_productos(cliente: str, solo_activos: bool = True) -> List[Producto]:
    """Productos del catálogo de un cliente."""
    cliente = cliente.strip().upper()
    query = "SELECT * FROM productos WHERE cliente_id = %s"
    params = [cliente]
    if solo_activos:
        query += " AND activo = TRUE"
    query += " ORDER BY alias_interno"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    return [p for r in rows if (p := _row_a_producto(r)) is not None]


def get_producto(cliente: str, alias_interno: str) -> Optional[Producto]:
    cliente = cliente.strip().upper()
    alias = alias_interno.strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM productos
                WHERE cliente_id = %s AND UPPER(alias_interno) = UPPER(%s)
                LIMIT 1
                """,
                (cliente, alias),
            )
            row = cur.fetchone()
    return _row_a_producto(row) if row else None


def get_productos_pendientes() -> List[dict]:
    """Todos los productos pendientes de validación (activo=FALSE) — para admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, c.nombre as cliente_nombre
                FROM productos p
                JOIN clientes c ON c.cliente_id = p.cliente_id
                WHERE p.activo = FALSE
                ORDER BY p.created_at ASC
                """
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_todos_productos() -> List[dict]:
    """Todos los productos — para admin."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, c.nombre as cliente_nombre
                FROM productos p
                JOIN clientes c ON c.cliente_id = p.cliente_id
                ORDER BY p.cliente_id, p.alias_interno
                """
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def agregar_producto(cliente: str, nuevo: ProductoNuevo) -> Producto:
    """Agrega producto al catálogo. Queda activo=FALSE hasta validación."""
    cliente = cliente.strip().upper()

    if get_producto(cliente, nuevo.alias_interno):
        raise ValueError(f"Ya existe un producto con alias '{nuevo.alias_interno}'")

    producto = Producto(
        cliente=cliente,
        alias_interno=nuevo.alias_interno,
        nombre_invoice=nuevo.nombre_invoice,
        hs_code=nuevo.hs_code,
        largo_cm=nuevo.largo_cm,
        ancho_cm=nuevo.ancho_cm,
        alto_cm=nuevo.alto_cm,
        peso_kg=nuevo.peso_kg,
        valor_usd_default=nuevo.valor_usd_default,
        activo=False,
    )

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO productos
                    (cliente_id, alias_interno, nombre_invoice, hs_code,
                     largo_cm, ancho_cm, alto_cm, peso_kg, valor_usd_default, activo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                """,
                (
                    producto.cliente, producto.alias_interno, producto.nombre_invoice,
                    producto.hs_code, producto.largo_cm, producto.ancho_cm,
                    producto.alto_cm, producto.peso_kg, producto.valor_usd_default,
                ),
            )
    return producto


def actualizar_producto_cliente(cliente: str, alias_original: str, nuevo: ProductoNuevo) -> bool:
    """
    El cliente edita un producto propio. Como cambian datos que van a la
    aduana (medidas, peso, valor, HS), el producto VUELVE A REVISIÓN
    (activo=FALSE) hasta que Tauro lo apruebe de nuevo.
    True si actualizó algo.
    """
    cliente = cliente.strip().upper()
    alias_original = (alias_original or "").strip()

    if nuevo.alias_interno != alias_original and get_producto(cliente, nuevo.alias_interno):
        raise ValueError(f"Ya existe un producto con alias '{nuevo.alias_interno}'")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE productos
                SET alias_interno=%s, nombre_invoice=%s, hs_code=%s,
                    largo_cm=%s, ancho_cm=%s, alto_cm=%s, peso_kg=%s,
                    valor_usd_default=%s, activo=FALSE
                WHERE cliente_id = %s AND alias_interno = %s
                """,
                (
                    nuevo.alias_interno, nuevo.nombre_invoice, nuevo.hs_code,
                    nuevo.largo_cm, nuevo.ancho_cm, nuevo.alto_cm, nuevo.peso_kg,
                    nuevo.valor_usd_default, cliente, alias_original,
                ),
            )
            return cur.rowcount > 0


def eliminar_producto_cliente(cliente: str, alias_interno: str) -> bool:
    """El cliente borra un producto propio del catálogo. Las solicitudes
    viejas guardan el alias como texto, así que no se rompen. True si borró."""
    cliente = cliente.strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM productos WHERE cliente_id = %s AND alias_interno = %s",
                (cliente, (alias_interno or "").strip()),
            )
            return cur.rowcount > 0


def aprobar_producto(producto_id: int) -> None:
    """Admin: aprueba un producto (activo=TRUE)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE productos SET activo = TRUE WHERE id = %s", (producto_id,))


def rechazar_producto(producto_id: int) -> None:
    """Admin: elimina un producto rechazado."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM productos WHERE id = %s", (producto_id,))
