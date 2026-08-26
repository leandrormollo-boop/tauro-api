# ============================================================
# Servicio de catálogo de productos — PostgreSQL
# ============================================================

from datetime import datetime, timezone
from typing import List, Optional

from core.database import get_conn
from modelos.producto import Producto, ProductoNuevo


_imagen_col_lista = False


def _ensure_imagen_col() -> None:
    """Agrega la columna `imagen_url` si la base es vieja. Idempotente."""
    global _imagen_col_lista
    if _imagen_col_lista:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE productos ADD COLUMN IF NOT EXISTS imagen_url TEXT")
            conn.commit()
    except Exception as e:
        print(f"[catalogo] no pude asegurar la columna imagen_url: {e}")
    _imagen_col_lista = True


def _row_a_producto(r: dict) -> Optional[Producto]:
    campos = dict(
        cliente=str(r["cliente_id"]).strip().upper(),
        alias_interno=str(r["alias_interno"]).strip(),
        nombre_invoice=str(r["nombre_invoice"]).strip(),
        hs_code=str(r["hs_code"] or "").strip(),
        largo_cm=float(r["largo_cm"] or 0),
        ancho_cm=float(r["ancho_cm"] or 0),
        alto_cm=float(r["alto_cm"] or 0),
        peso_kg=float(r["peso_kg"] or 0),
        valor_usd_default=float(r["valor_usd_default"] or 0),
        activo=bool(r["activo"]),
        imagen_url=(r.get("imagen_url") if isinstance(r, dict) else None),
    )
    try:
        return Producto(**campos)
    except Exception:
        # Producto importado de Shopify todavía sin completar (sin medidas ni
        # HS): no pasa la validación estricta, pero igual tiene que verse en
        # el catálogo para que el cliente lo complete. `construct` arma el
        # objeto sin validar — sólo lo usamos para mostrar, nunca para emitir
        # (esos productos quedan activo=FALSE y no cotizan).
        try:
            return Producto.construct(**campos)
        except Exception:
            return None


def get_productos(cliente: str, solo_activos: bool = True) -> List[Producto]:
    """Productos del catálogo de un cliente."""
    _ensure_imagen_col()
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
    """
    Admin: aprueba un producto (activo=TRUE) y le avisa al cliente por mail.

    El aviso es una promesa escrita en el portal ("te llega un mail cuando
    está aprobado"): sin él, el cliente carga su primer producto, ve
    "En revisión" y no sabe cuándo puede empezar a usarlo.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE productos SET activo = TRUE WHERE id = %s
                RETURNING alias_interno, cliente_id
            """, (producto_id,))
            fila = cur.fetchone()

    if not fila:
        return

    # El mail nunca puede tumbar la aprobación: si falla, queda en el log.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM clientes WHERE cliente_id = %s",
                            (fila["cliente_id"],))
                c = cur.fetchone()
        destino = (c or {}).get("email")
        if destino:
            from core.email_sender import _enviar_mail_a
            alias = fila["alias_interno"]
            _enviar_mail_a(
                destino,
                f"✓ Tu producto «{alias}» ya está aprobado",
                f"""<html><body style="margin:0;background:#f4f4f6;">
<div style="font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;">
  <div style="background:#0c0a14;padding:28px 24px;text-align:center;">
    <div style="font-size:22px;font-weight:700;letter-spacing:.08em;color:#fff;">
      TAURO <span style="color:#a78bfa;">SOLUTIONS</span>
    </div>
  </div>
  <div style="padding:30px;">
    <h2 style="margin:0 0 12px;font-size:19px;color:#1e1b2e;">Tu producto ya está aprobado</h2>
    <p style="line-height:1.7;color:#4a4a58;margin:0 0 18px;">
      <b>«{alias}»</b> pasó la validación y ya podés usarlo en tus envíos:
      cotizarlo, verlo en el catálogo con tu precio por unidad, y despacharlo.
    </p>
    <p style="text-align:center;margin:26px 0 0;">
      <a href="https://taurosolutions.ar/portal/catalogo"
         style="background:#7c5cf6;color:#fff;padding:13px 30px;text-decoration:none;
                font-weight:600;border-radius:999px;display:inline-block;">Ver mi catálogo</a>
    </p>
  </div>
  <div style="background:#0c0a14;padding:16px;text-align:center;font-size:11px;color:#8b86a0;">
    taurosolutions.ar
  </div>
</div></body></html>""",
            )
            print(f"[catalogo] aviso de aprobación enviado a {destino} ({alias})")
    except Exception as e:
        print(f"[catalogo] producto {producto_id} aprobado pero el mail falló: {e}")


def rechazar_producto(producto_id: int) -> None:
    """Admin: elimina un producto rechazado."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM productos WHERE id = %s", (producto_id,))


def upsert_producto_importado(
    cliente: str,
    sku: str,
    nombre_invoice: str,
    peso_kg: float = 0.0,
    imagen_url: Optional[str] = None,
) -> str:
    """
    Alta/actualización de un producto traído de la tienda (Shopify).

    Se guarda con `alias_interno = SKU` (la llave con la que se cruzan las
    ventas) y queda `activo=FALSE`: la foto y el nombre ya están, pero le
    faltan los datos aduaneros (medidas, HS, valor) que ninguna tienda tiene.
    El cliente los completa una sola vez y recién ahí Tauro lo valida.

    En un re-sync NO se pisa lo que el cliente ya cargó: sólo se refresca la
    miniatura (y el peso si todavía estaba en 0). Devuelve 'creado' o
    'actualizado'.
    """
    _ensure_imagen_col()
    cliente = cliente.strip().upper()
    sku = (sku or "").strip()
    if not sku:
        raise ValueError("Producto sin SKU: no se puede importar (la venta se cruza por SKU).")
    nombre = (nombre_invoice or sku).strip()[:120] or sku
    peso = float(peso_kg or 0)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO productos
                    (cliente_id, alias_interno, nombre_invoice, hs_code,
                     largo_cm, ancho_cm, alto_cm, peso_kg, valor_usd_default,
                     imagen_url, activo)
                VALUES (%s, %s, %s, '', 0, 0, 0, %s, 0, %s, FALSE)
                ON CONFLICT (cliente_id, alias_interno) DO UPDATE SET
                    imagen_url = COALESCE(EXCLUDED.imagen_url, productos.imagen_url),
                    peso_kg    = CASE WHEN productos.peso_kg = 0
                                      THEN EXCLUDED.peso_kg ELSE productos.peso_kg END
                RETURNING (xmax = 0) AS insertado
                """,
                (cliente, sku, nombre, peso, imagen_url),
            )
            fila = cur.fetchone()
        conn.commit()
    insertado = bool(fila["insertado"]) if fila else False
    return "creado" if insertado else "actualizado"
