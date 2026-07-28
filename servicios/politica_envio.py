# ============================================================
# Política de flete: qué precio de envío ve el COMPRADOR final
# en el checkout de la tienda del cliente.
# ============================================================
# El comerciante decide, por tienda:
#   real   → el comprador paga la tarifa TAURO tal cual (default)
#   markup → tarifa TAURO + un % (el comerciante gana con el flete)
#   fijo   → un precio fijo en ARS, sin importar la tarifa real
#   gratis → $0 (envío gratis: el comerciante absorbe el costo)
#
# Y aparte puede mostrar (o no) una estimación de impuestos de destino.
# El impuesto sale del catálogo: cada producto puede declarar cuánto
# paga aprox. de tax, y se matchea con el SKU que manda Shopify. Si un
# item no matchea, cae al % por defecto de la tienda sobre su valor.
#
# Regla de oro: si el comerciante no configuró nada, se comporta como
# hasta ahora (tarifa real, sin impuestos). Cero sorpresas.
# ============================================================
from __future__ import annotations

from typing import Optional

from core.database import get_conn

POLITICAS = ("real", "markup", "fijo", "gratis")

DEFAULTS = {
    "politica": "real",
    "markup_pct": 0.0,
    "precio_fijo_ars": 0.0,
    "mostrar_tax": False,
    "tax_pct_default": 0.0,
    "etiqueta": "",
}

_tabla_lista = False


def _ensure_tabla() -> None:
    global _tabla_lista
    if _tabla_lista:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config_envio_tienda (
                    dominio          TEXT PRIMARY KEY,
                    cliente_id       TEXT,
                    politica         TEXT NOT NULL DEFAULT 'real',
                    markup_pct       NUMERIC(6,2) NOT NULL DEFAULT 0,
                    precio_fijo_ars  NUMERIC(14,2) NOT NULL DEFAULT 0,
                    mostrar_tax      BOOLEAN NOT NULL DEFAULT FALSE,
                    tax_pct_default  NUMERIC(6,2) NOT NULL DEFAULT 0,
                    etiqueta         TEXT DEFAULT '',
                    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                ALTER TABLE productos
                    ADD COLUMN IF NOT EXISTS tax_estimado_usd NUMERIC(12,2) NOT NULL DEFAULT 0;
            """)
        conn.commit()
    _tabla_lista = True


def obtener_config(dominio: str) -> dict:
    """Config de la tienda; si nunca se tocó, devuelve los defaults."""
    _ensure_tabla()
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return dict(DEFAULTS)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM config_envio_tienda WHERE dominio = %s", (dominio,))
            row = cur.fetchone()
    if not row:
        return dict(DEFAULTS)
    return {
        "politica": row["politica"] or "real",
        "markup_pct": float(row["markup_pct"] or 0),
        "precio_fijo_ars": float(row["precio_fijo_ars"] or 0),
        "mostrar_tax": bool(row["mostrar_tax"]),
        "tax_pct_default": float(row["tax_pct_default"] or 0),
        "etiqueta": row["etiqueta"] or "",
    }


def guardar_config(dominio: str, cliente_id: str, politica: str,
                   markup_pct: float = 0, precio_fijo_ars: float = 0,
                   mostrar_tax: bool = False, tax_pct_default: float = 0,
                   etiqueta: str = "") -> dict:
    # Validar ANTES de tocar la base: un formulario mal cargado no tiene
    # por qué abrir una conexión.
    dominio = (dominio or "").strip().lower()
    if not dominio:
        return {"ok": False, "error": "Falta el dominio de la tienda."}
    politica = (politica or "real").strip().lower()
    if politica not in POLITICAS:
        return {"ok": False, "error": "Política de envío inválida."}

    markup_pct = max(0.0, min(float(markup_pct or 0), 300.0))
    precio_fijo_ars = max(0.0, float(precio_fijo_ars or 0))
    tax_pct_default = max(0.0, min(float(tax_pct_default or 0), 100.0))

    # "Precio fijo" en 0 es casi siempre un descuido (el campo quedó vacío),
    # y regalaría todos los envíos internacionales sin que nadie lo note.
    # Si de verdad quiere envío gratis, existe esa política.
    if politica == "fijo" and precio_fijo_ars <= 0:
        return {"ok": False,
                "error": "Poné un precio fijo mayor a cero. "
                         "Si querés que el envío salga $0, elegí la opción 'Envío gratis'."}
    if politica == "markup" and markup_pct <= 0:
        return {"ok": False,
                "error": "El porcentaje a sumar tiene que ser mayor a cero. "
                         "Si no querés sumar nada, elegí 'Lo que cotiza TAURO'."}

    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO config_envio_tienda
                    (dominio, cliente_id, politica, markup_pct, precio_fijo_ars,
                     mostrar_tax, tax_pct_default, etiqueta, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (dominio) DO UPDATE SET
                    cliente_id      = EXCLUDED.cliente_id,
                    politica        = EXCLUDED.politica,
                    markup_pct      = EXCLUDED.markup_pct,
                    precio_fijo_ars = EXCLUDED.precio_fijo_ars,
                    mostrar_tax     = EXCLUDED.mostrar_tax,
                    tax_pct_default = EXCLUDED.tax_pct_default,
                    etiqueta        = EXCLUDED.etiqueta,
                    updated_at      = now()
            """, (dominio, cliente_id, politica, markup_pct, precio_fijo_ars,
                  mostrar_tax, tax_pct_default, etiqueta.strip()[:60]))
        conn.commit()
    return {"ok": True}


def guardar_tax_producto(cliente_id: str, alias_interno: str, tax_usd: float) -> None:
    """
    Impuesto estimado de destino de un producto. Vive aparte del alta/edición
    del catálogo para no tocar ese flujo (ya probado) por un campo opcional.
    """
    _ensure_tabla()
    try:
        tax = max(0.0, float(tax_usd or 0))
    except (TypeError, ValueError):
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE productos SET tax_estimado_usd = %s
                WHERE cliente_id = %s AND alias_interno = %s
            """, (tax, cliente_id.strip().upper(), alias_interno.strip()))
        conn.commit()


def tax_de_productos(cliente_id: str) -> dict:
    """{alias_interno → tax estimado} para pintar el catálogo."""
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT alias_interno, tax_estimado_usd FROM productos
                WHERE cliente_id = %s
            """, (cliente_id.strip().upper(),))
            return {r["alias_interno"]: float(r["tax_estimado_usd"] or 0)
                    for r in cur.fetchall()}


def _tax_por_sku(cliente_id: str, skus: list[str]) -> dict:
    """{sku (alias_interno) → tax estimado en USD} para los SKU del carrito."""
    if not cliente_id or not skus:
        return {}
    _ensure_tabla()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Comparación en MAYÚSCULAS de los dos lados: el SKU viene de
            # Shopify y el alias lo escribió el comerciante a mano. Exigir
            # que coincidan letra por letra hacía que el tax por producto
            # casi nunca matcheara ("RJ-5000" vs "rj-5000").
            cur.execute("""
                SELECT alias_interno, tax_estimado_usd
                FROM productos
                WHERE cliente_id = %s AND UPPER(TRIM(alias_interno)) = ANY(%s)
            """, (cliente_id.strip().upper(), skus))
            return {r["alias_interno"].strip().upper(): float(r["tax_estimado_usd"] or 0)
                    for r in cur.fetchall()}


def calcular_tax_estimado(cliente_id: str, items: list[dict],
                          config: dict, dolar: float) -> float:
    """
    Impuestos de destino estimados para el carrito, EN ARS (la moneda con
    la que se le responde a Shopify).

    Ojo con las monedas, que acá se mezclan: el tax que el comerciante
    carga por producto está en USD (se convierte), mientras que el % por
    defecto se aplica sobre el precio del item, que Shopify manda en
    centavos de la moneda de la tienda — o sea que ya sale en ARS.
    """
    if not config.get("mostrar_tax"):
        return 0.0

    skus = [str(it.get("sku") or "").strip().upper() for it in items if it.get("sku")]
    try:
        tabla = _tax_por_sku(cliente_id, skus) if (skus and cliente_id) else {}
    except Exception as e:
        print(f"[politica_envio] no pude leer tax por SKU: {e}")
        tabla = {}
    pct_default = float(config.get("tax_pct_default") or 0) / 100.0

    total_ars = 0.0
    for it in items:
        cantidad = int(it.get("quantity") or 1)
        sku = str(it.get("sku") or "").strip().upper()
        if sku and sku in tabla and tabla[sku] > 0:
            total_ars += tabla[sku] * cantidad * dolar      # USD → ARS
        elif pct_default > 0:
            valor_item_ars = (float(it.get("price") or 0) / 100.0) * cantidad
            total_ars += valor_item_ars * pct_default       # ya viene en ARS
    return round(total_ars, 2)


def aplicar_politica(precio_real_ars: float, config: dict) -> Optional[float]:
    """
    Precio de envío que ve el comprador, según la política del comerciante.
    None = esta opción no se muestra.
    """
    politica = config.get("politica", "real")
    if politica == "gratis":
        return 0.0
    if politica == "fijo":
        return float(config.get("precio_fijo_ars") or 0)
    if politica == "markup":
        return round(precio_real_ars * (1 + float(config.get("markup_pct") or 0) / 100.0), 2)
    return float(precio_real_ars)
