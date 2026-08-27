# ============================================================
# Servicio de catálogo de productos — PostgreSQL
# ============================================================

import re
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
        plataforma=r.get("plataforma"),
        tienda_dominio=r.get("tienda_dominio"),
        external_product_id=r.get("external_product_id"),
        external_variant_id=r.get("external_variant_id"),
        external_inventory_item_id=r.get("external_inventory_item_id"),
        sku_tienda=r.get("sku_tienda"),
        titulo_tienda=r.get("titulo_tienda"),
        variante_tienda=r.get("variante_tienda"),
        precio_tienda=(float(r["precio_tienda"])
                       if r.get("precio_tienda") is not None else None),
        moneda_tienda=r.get("moneda_tienda"),
        hs_code_tienda=r.get("hs_code_tienda"),
        pais_origen_tienda=r.get("pais_origen_tienda"),
        stock_controlado=bool(r.get("stock_controlado")),
        stock_disponible=r.get("stock_disponible"),
        stock_comprometido=r.get("stock_comprometido"),
        stock_fisico=r.get("stock_fisico"),
        stock_entrante=r.get("stock_entrante"),
        stock_actualizado_at=r.get("stock_actualizado_at"),
        source_updated_at=r.get("source_updated_at"),
        sync_activo=bool(r.get("sync_activo", True)),
        ubicaciones=list(r.get("ubicaciones") or []),
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
    query += " AND COALESCE(sync_activo, TRUE) = TRUE ORDER BY alias_interno"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]

            ids = [int(r["id"]) for r in rows if r.get("id") is not None]
            ubicaciones: dict[int, list[dict]] = {}
            if ids:
                cur.execute(
                    """
                    SELECT producto_id, external_location_id, ubicacion_nombre,
                           disponible, comprometido, fisico, entrante,
                           source_updated_at
                    FROM producto_inventario_ubicaciones
                    WHERE cliente_id = %s AND producto_id = ANY(%s)
                    ORDER BY ubicacion_nombre
                    """,
                    (cliente, ids),
                )
                for u in cur.fetchall():
                    ubicaciones.setdefault(int(u["producto_id"]), []).append(dict(u))

    for row in rows:
        row["ubicaciones"] = ubicaciones.get(int(row["id"]), [])

    return [p for r in rows if (p := _row_a_producto(r)) is not None]


def listar_stock_cliente(
    cliente: str, limite: int = 100, offset: int = 0,
) -> tuple[List[Producto], int]:
    """Catálogo operativo paginado para la API B2B, aislado por tenant.

    Incluye variantes Shopify todavía pendientes de completar en TAURO porque
    el inventario sirve para identificarlas aunque aún no puedan cotizarse ni
    emitirse. Las variantes archivadas por Shopify no se publican.
    """
    _ensure_imagen_col()
    cliente = (cliente or "").strip().upper()
    limite = max(1, min(int(limite), 200))
    offset = max(0, int(offset))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *, COUNT(*) OVER() AS total_filtrado
                FROM productos
                WHERE cliente_id=%s AND COALESCE(sync_activo, TRUE)=TRUE
                ORDER BY alias_interno
                LIMIT %s OFFSET %s
                """,
                (cliente, limite, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
            total = int(rows[0].get("total_filtrado") or 0) if rows else 0
            if not rows and offset:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total FROM productos
                    WHERE cliente_id=%s AND COALESCE(sync_activo, TRUE)=TRUE
                    """,
                    (cliente,),
                )
                total = int((cur.fetchone() or {}).get("total") or 0)

            ids = [int(r["id"]) for r in rows if r.get("id") is not None]
            ubicaciones: dict[int, list[dict]] = {}
            if ids:
                cur.execute(
                    """
                    SELECT producto_id, external_location_id, ubicacion_nombre,
                           disponible, comprometido, fisico, entrante,
                           source_updated_at
                    FROM producto_inventario_ubicaciones
                    WHERE cliente_id=%s AND producto_id=ANY(%s)
                    ORDER BY ubicacion_nombre
                    """,
                    (cliente, ids),
                )
                for ubicacion in cur.fetchall():
                    ubicaciones.setdefault(int(ubicacion["producto_id"]), []).append(
                        dict(ubicacion)
                    )

    for row in rows:
        row["ubicaciones"] = ubicaciones.get(int(row["id"]), [])
    return [p for row in rows if (p := _row_a_producto(row)) is not None], total


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


def get_producto_por_variante(cliente: str, external_variant_id: str) -> Optional[Producto]:
    """Busca una variante Shopify dentro del tenant; nunca cruza catálogos."""
    cliente = (cliente or "").strip().upper()
    variante = (external_variant_id or "").strip()
    if not cliente or not variante:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM productos
                WHERE cliente_id = %s AND plataforma = 'shopify'
                  AND external_variant_id = %s
                LIMIT 1
                """,
                (cliente, variante),
            )
            row = cur.fetchone()
    return _row_a_producto(dict(row)) if row else None


def enriquecer_items_catalogo(cliente: str, items: list[dict]) -> list[dict]:
    """Añade imagen/alias/stock a una orden sin consultar Shopify en el request."""
    cliente = (cliente or "").strip().upper()
    salida = [dict(item) for item in (items or []) if isinstance(item, dict)]
    if not cliente or not salida:
        return salida

    variantes = {
        str(item.get("external_variant_id") or item.get("variant_id") or "").strip()
        for item in salida
    }
    variantes.discard("")
    skus = {str(item.get("sku") or "").strip().upper() for item in salida}
    skus.discard("")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_variant_id, alias_interno, sku_tienda, imagen_url,
                       titulo_tienda, variante_tienda, stock_controlado,
                       stock_disponible, sync_activo
                FROM productos
                WHERE cliente_id = %s
                  AND (
                    external_variant_id = ANY(%s)
                    OR UPPER(COALESCE(sku_tienda, alias_interno)) = ANY(%s)
                  )
                """,
                (cliente, list(variantes), list(skus)),
            )
            filas = [dict(r) for r in cur.fetchall()]

    por_variante = {str(r.get("external_variant_id") or ""): r for r in filas}
    por_sku = {
        str(r.get("sku_tienda") or r.get("alias_interno") or "").upper(): r
        for r in filas
    }
    for item in salida:
        clave = str(item.get("external_variant_id") or item.get("variant_id") or "")
        producto = por_variante.get(clave) or por_sku.get(str(item.get("sku") or "").upper())
        if not producto:
            continue
        item.update({
            "producto_alias": producto.get("alias_interno"),
            "imagen_url": producto.get("imagen_url"),
            "titulo_catalogo": producto.get("titulo_tienda"),
            "variante_catalogo": producto.get("variante_tienda"),
            "stock_controlado": bool(producto.get("stock_controlado")),
            "stock_disponible": producto.get("stock_disponible"),
            "producto_activo_tienda": bool(producto.get("sync_activo", True)),
        })
    return salida


def estado_sincronizacion_cliente(cliente: str) -> Optional[dict]:
    cliente = (cliente or "").strip().upper()
    if not cliente:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM shopify_sync_estado
                WHERE cliente_id = %s
                ORDER BY updated_at DESC LIMIT 1
                """,
                (cliente,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def resumen_stock_cliente(cliente: str) -> dict:
    cliente = (cliente or "").strip().upper()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FILTER (WHERE plataforma = 'shopify') AS variantes,
                       COUNT(*) FILTER (WHERE stock_controlado) AS controladas,
                       COUNT(*) FILTER (WHERE stock_controlado AND COALESCE(stock_disponible, 0) <= 0) AS agotadas,
                       COUNT(*) FILTER (WHERE stock_controlado AND stock_disponible BETWEEN 1 AND 5) AS stock_bajo,
                       COALESCE(SUM(stock_disponible) FILTER (WHERE stock_controlado), 0) AS unidades_disponibles
                FROM productos
                WHERE cliente_id = %s AND COALESCE(sync_activo, TRUE) = TRUE
                """,
                (cliente,),
            )
            row = cur.fetchone()
    return dict(row or {})


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
    *,
    tienda_dominio: str = "",
    external_product_id: str = "",
    external_variant_id: str = "",
    external_inventory_item_id: str = "",
    variante_tienda: str = "",
    precio_tienda: Optional[float] = None,
    moneda_tienda: str = "",
    hs_code_tienda: str = "",
    pais_origen_tienda: str = "",
    stock_controlado: bool = False,
    stock_disponible: Optional[int] = None,
    stock_comprometido: Optional[int] = None,
    stock_fisico: Optional[int] = None,
    stock_entrante: Optional[int] = None,
    source_updated_at=None,
    source_observed_at=None,
    sync_run_id: str = "",
    ubicaciones: Optional[list[dict]] = None,
    inventario_completo: bool = False,
) -> str:
    """
    Alta/actualización de un producto traído de la tienda (Shopify).

    La identidad estable es ``external_variant_id``; el SKU puede cambiar o
    incluso venir vacío. Los datos aduaneros que el cliente/Tauro ya
    completaron nunca se pisan. Devuelve ``creado`` o ``actualizado``.
    """
    _ensure_imagen_col()
    cliente = cliente.strip().upper()
    sku = (sku or "").strip()[:160]
    dominio = (tienda_dominio or "").strip().lower()
    variante_id = (external_variant_id or "").strip()
    if not dominio or not variante_id:
        # Compatibilidad con el importador inicial. El flujo GraphQL nuevo
        # siempre manda dominio e ID global de variante.
        if not sku:
            raise ValueError("Producto Shopify sin SKU ni ID de variante.")
        dominio = dominio or "legacy.myshopify.com"
        variante_id = variante_id or f"legacy:{sku.upper()}"

    nombre = (nombre_invoice or sku or "Producto Shopify").strip()[:120]
    peso = float(peso_kg or 0)
    precio = float(precio_tienda) if precio_tienda not in (None, "") else None
    # El precio de venta de la tienda no siempre es el valor declarado para
    # aduana. Sólo lo sugerimos si Shopify confirma USD: copiar ARS como USD
    # (por ejemplo 44.500 ARS → USD 44.500) sería un error operativo grave.
    precio_declarado_sugerido = (
        precio if str(moneda_tienda or "").strip().upper() == "USD" else None
    )
    hs_crudo = re.sub(r"\D", "", str(hs_code_tienda or ""))[:12]
    hs_tauro = (f"{hs_crudo[:4]}.{hs_crudo[4:6]}.{hs_crudo[6:8]}"
                if len(hs_crudo) == 8 else "")
    sufijo = re.sub(r"\D", "", variante_id)[-8:] or "VARIANTE"
    alias_base = (sku or f"SHOPIFY-{sufijo}").strip()[:60]
    ubicaciones = list(ubicaciones or [])

    def _alias_libre(cur) -> str:
        cur.execute(
            "SELECT external_variant_id FROM productos WHERE cliente_id=%s AND UPPER(alias_interno)=UPPER(%s)",
            (cliente, alias_base),
        )
        usada = cur.fetchone()
        if not usada or str(usada.get("external_variant_id") or "") == variante_id:
            return alias_base
        raiz = alias_base[: max(2, 59 - len(sufijo))].rstrip(" -")
        return f"{raiz}-{sufijo}"[:60]

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Un webhook y una conciliación completa pueden llegar juntos.
            # El lock vive sólo durante esta transacción y se segmenta por
            # tenant/tienda/alias: evita altas duplicadas sin frenar a otros
            # clientes ni a otros productos del mismo catálogo.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (f"{cliente}:{dominio}", alias_base.upper()),
            )
            cur.execute(
                """
                SELECT id,
                       (%s::timestamptz IS NOT NULL AND
                        (source_updated_at IS NULL OR
                         %s::timestamptz > source_updated_at OR
                         (%s::timestamptz = source_updated_at AND
                          (source_deleted_at IS NULL OR
                           %s::timestamptz > source_deleted_at)))) AS aplicar_fuente,
                       (%s::timestamptz IS NOT NULL AND
                        (source_observed_at IS NULL OR
                         %s::timestamptz > source_observed_at OR
                         (%s::timestamptz = source_observed_at AND
                          source_deleted_at IS NULL))
                        AND (source_deleted_at IS NULL OR
                             %s::timestamptz > source_deleted_at)) AS aplicar_presencia
                FROM productos
                WHERE cliente_id=%s AND plataforma='shopify'
                  AND tienda_dominio=%s AND external_variant_id=%s
                FOR UPDATE
                """,
                (
                    source_updated_at, source_updated_at, source_updated_at,
                    source_observed_at,
                    source_observed_at, source_observed_at, source_observed_at,
                    source_updated_at,
                    cliente, dominio, variante_id,
                ),
            )
            existente = cur.fetchone()
            if not existente and sku:
                cur.execute(
                    """
                    SELECT id,
                           (%s::timestamptz IS NOT NULL AND
                            (source_updated_at IS NULL OR
                             %s::timestamptz > source_updated_at OR
                             (%s::timestamptz = source_updated_at AND
                              (source_deleted_at IS NULL OR
                               %s::timestamptz > source_deleted_at)))) AS aplicar_fuente,
                           (%s::timestamptz IS NOT NULL AND
                            (source_observed_at IS NULL OR
                             %s::timestamptz > source_observed_at OR
                             (%s::timestamptz = source_observed_at AND
                              source_deleted_at IS NULL))
                            AND (source_deleted_at IS NULL OR
                                 %s::timestamptz > source_deleted_at)) AS aplicar_presencia
                    FROM productos
                    WHERE cliente_id=%s AND UPPER(alias_interno)=UPPER(%s)
                      AND (external_variant_id IS NULL OR external_variant_id='')
                    FOR UPDATE
                    """,
                    (
                        source_updated_at, source_updated_at, source_updated_at,
                        source_observed_at,
                        source_observed_at, source_observed_at, source_observed_at,
                        source_updated_at,
                        cliente, sku,
                    ),
                )
                existente = cur.fetchone()

            if existente:
                producto_id = int(existente["id"])
                presencia_aplicada = bool(existente.get("aplicar_presencia"))
                fuente_aplicada = bool(
                    existente.get("aplicar_fuente") and presencia_aplicada
                )
                cur.execute(
                    """
                    UPDATE productos AS p SET
                        plataforma='shopify', tienda_dominio=%s,
                        external_product_id=%s, external_variant_id=%s,
                        external_inventory_item_id=%s,
                        sku_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.sku_tienda END,
                        titulo_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.titulo_tienda END,
                        variante_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.variante_tienda END,
                        imagen_url=CASE WHEN incoming.aplicar
                            THEN COALESCE(%s, p.imagen_url) ELSE p.imagen_url END,
                        precio_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.precio_tienda END,
                        moneda_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.moneda_tienda END,
                        hs_code_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.hs_code_tienda END,
                        pais_origen_tienda=CASE WHEN incoming.aplicar THEN %s ELSE p.pais_origen_tienda END,
                        nombre_invoice=CASE
                            WHEN incoming.aplicar AND NOT p.activo THEN %s
                            ELSE p.nombre_invoice END,
                        hs_code=CASE
                            WHEN incoming.aplicar AND COALESCE(p.hs_code,'')='' AND %s<>'' THEN %s
                            ELSE p.hs_code END,
                        peso_kg=CASE
                            WHEN incoming.aplicar AND COALESCE(p.peso_kg,0)<=0 AND %s>0 THEN %s
                            ELSE p.peso_kg END,
                        valor_usd_default=CASE
                            WHEN incoming.aplicar
                             AND COALESCE(p.valor_usd_default,0)<=0
                             AND COALESCE(%s,0)>0 THEN %s
                            ELSE p.valor_usd_default END,
                        stock_controlado=CASE WHEN incoming.aplicar THEN %s ELSE p.stock_controlado END,
                        stock_disponible=CASE WHEN incoming.aplicar THEN %s ELSE p.stock_disponible END,
                        stock_comprometido=CASE WHEN incoming.aplicar THEN %s ELSE p.stock_comprometido END,
                        stock_fisico=CASE WHEN incoming.aplicar THEN %s ELSE p.stock_fisico END,
                        stock_entrante=CASE WHEN incoming.aplicar THEN %s ELSE p.stock_entrante END,
                        stock_actualizado_at=CASE
                            WHEN incoming.aplicar AND %s THEN NOW()
                            ELSE p.stock_actualizado_at END,
                        source_updated_at=CASE WHEN incoming.aplicar
                            THEN %s::timestamptz ELSE p.source_updated_at END,
                        source_deleted_at=CASE WHEN incoming.presencia
                            THEN NULL ELSE p.source_deleted_at END,
                        source_observed_at=CASE WHEN incoming.presencia
                            THEN %s::timestamptz ELSE p.source_observed_at END,
                        sync_run_id=CASE WHEN incoming.presencia
                            THEN %s ELSE p.sync_run_id END,
                        sync_activo=CASE WHEN incoming.presencia
                            THEN TRUE ELSE p.sync_activo END
                    FROM (SELECT %s::boolean AS aplicar,
                                 %s::boolean AS presencia) AS incoming
                    WHERE p.id=%s AND p.cliente_id=%s
                    """,
                    (
                        dominio, external_product_id, variante_id,
                        external_inventory_item_id, sku, nombre, variante_tienda,
                        imagen_url, precio, moneda_tienda, hs_crudo,
                        pais_origen_tienda, nombre, hs_tauro, hs_tauro,
                        peso, peso, precio_declarado_sugerido,
                        precio_declarado_sugerido, bool(stock_controlado),
                        stock_disponible, stock_comprometido, stock_fisico,
                        stock_entrante, bool(stock_controlado), source_updated_at,
                        source_observed_at, sync_run_id, fuente_aplicada,
                        presencia_aplicada, producto_id, cliente,
                    ),
                )
                estado = "actualizado"
            else:
                alias = _alias_libre(cur)
                cur.execute(
                    """
                    INSERT INTO productos
                        (cliente_id, alias_interno, nombre_invoice, hs_code,
                         largo_cm, ancho_cm, alto_cm, peso_kg, valor_usd_default,
                         imagen_url, plataforma, tienda_dominio,
                         external_product_id, external_variant_id,
                         external_inventory_item_id, sku_tienda, titulo_tienda,
                         variante_tienda, precio_tienda, moneda_tienda,
                         hs_code_tienda, pais_origen_tienda, stock_controlado,
                         stock_disponible, stock_comprometido, stock_fisico,
                         stock_entrante, stock_actualizado_at, source_updated_at,
                         source_observed_at, sync_run_id, sync_activo, activo)
                    VALUES
                        (%s,%s,%s,%s,0,0,0,%s,%s,%s,'shopify',%s,%s,%s,%s,
                         %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                         CASE WHEN %s THEN NOW() ELSE NULL END,%s,%s,%s,TRUE,FALSE)
                    RETURNING id
                    """,
                    (
                        cliente, alias, nombre, hs_tauro, peso,
                        precio_declarado_sugerido or 0,
                        imagen_url, dominio, external_product_id, variante_id,
                        external_inventory_item_id, sku, nombre, variante_tienda,
                        precio, moneda_tienda, hs_crudo, pais_origen_tienda,
                        bool(stock_controlado), stock_disponible,
                        stock_comprometido, stock_fisico, stock_entrante,
                        bool(stock_controlado), source_updated_at,
                        source_observed_at, sync_run_id,
                    ),
                )
                producto_id = int(cur.fetchone()["id"])
                presencia_aplicada = True
                fuente_aplicada = True
                estado = "creado"

            location_ids: list[str] = []
            for ubicacion in (ubicaciones if fuente_aplicada else []):
                location_id = str(ubicacion.get("external_location_id") or "").strip()
                if not location_id:
                    continue
                location_ids.append(location_id)
                cur.execute(
                    """
                    INSERT INTO producto_inventario_ubicaciones
                        (cliente_id, producto_id, plataforma, tienda_dominio,
                         external_location_id, ubicacion_nombre, disponible,
                         comprometido, fisico, entrante, source_updated_at)
                    VALUES (%s,%s,'shopify',%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (producto_id, external_location_id) DO UPDATE SET
                        ubicacion_nombre=EXCLUDED.ubicacion_nombre,
                        disponible=EXCLUDED.disponible,
                        comprometido=EXCLUDED.comprometido,
                        fisico=EXCLUDED.fisico,
                        entrante=EXCLUDED.entrante,
                        source_updated_at=EXCLUDED.source_updated_at,
                        updated_at=NOW()
                    WHERE producto_inventario_ubicaciones.source_updated_at IS NULL
                       OR (EXCLUDED.source_updated_at IS NOT NULL AND
                           EXCLUDED.source_updated_at >=
                           producto_inventario_ubicaciones.source_updated_at)
                    """,
                    (
                        cliente, producto_id, dominio, location_id,
                        str(ubicacion.get("ubicacion_nombre") or "Ubicación Shopify")[:160],
                        ubicacion.get("disponible"), ubicacion.get("comprometido"),
                        ubicacion.get("fisico"), ubicacion.get("entrante"),
                        ubicacion.get("source_updated_at"),
                    ),
                )
            if inventario_completo and fuente_aplicada:
                cur.execute(
                    """
                    DELETE FROM producto_inventario_ubicaciones
                     WHERE producto_id=%s AND cliente_id=%s
                       AND NOT (external_location_id = ANY(%s::text[]))
                    """,
                    (producto_id, cliente, location_ids),
                )
        conn.commit()
    return estado


def desactivar_ausentes_shopify(
    cliente: str,
    dominio: str,
    sync_run_id: str,
    sincronizacion_iniciada_at,
) -> int:
    """Archiva ausentes y avanza su reloj aunque ya estuvieran inactivos."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidatos AS MATERIALIZED (
                    SELECT id, sync_activo AS estaba_activo
                      FROM productos
                     WHERE cliente_id=%s AND plataforma='shopify'
                       AND tienda_dominio=%s
                       AND COALESCE(sync_run_id,'')<>%s
                       AND (source_observed_at IS NULL OR
                            source_observed_at < %s::timestamptz)
                     FOR UPDATE
                ), actualizados AS (
                    UPDATE productos AS p SET sync_activo=FALSE,
                        stock_disponible=NULL, stock_comprometido=NULL,
                        stock_fisico=NULL, stock_entrante=NULL,
                        source_observed_at=%s::timestamptz
                      FROM candidatos AS c
                     WHERE p.id=c.id
                    RETURNING c.estaba_activo
                )
                SELECT COUNT(*) FILTER (WHERE estaba_activo)::integer
                       AS desactivados
                  FROM actualizados
                """,
                (
                    (cliente or "").strip().upper(),
                    (dominio or "").strip().lower(),
                    sync_run_id,
                    sincronizacion_iniciada_at,
                    sincronizacion_iniciada_at,
                ),
            )
            fila = cur.fetchone() or {}
            return int(fila.get("desactivados") or 0)


def desactivar_producto_shopify(
    cliente: str,
    dominio: str,
    external_product_id: str,
    evento_at,
) -> int:
    """Aplica un delete sólo si no existe una mutación Shopify posterior."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE productos SET sync_activo=FALSE,
                    stock_disponible=NULL, stock_comprometido=NULL,
                    stock_fisico=NULL, stock_entrante=NULL,
                    source_updated_at=GREATEST(source_updated_at, %s::timestamptz),
                    source_deleted_at=%s::timestamptz,
                    source_observed_at=GREATEST(
                        source_observed_at, %s::timestamptz
                    )
                WHERE cliente_id=%s AND plataforma='shopify' AND tienda_dominio=%s
                  AND external_product_id=%s
                  AND %s::timestamptz IS NOT NULL
                  AND (source_updated_at IS NULL OR %s::timestamptz >= source_updated_at)
                  AND (source_observed_at IS NULL OR %s::timestamptz >= source_observed_at)
                """,
                (
                    evento_at,
                    evento_at,
                    evento_at,
                    (cliente or "").strip().upper(),
                    (dominio or "").strip().lower(),
                    (external_product_id or "").strip(),
                    evento_at,
                    evento_at,
                    evento_at,
                ),
            )
            return cur.rowcount
