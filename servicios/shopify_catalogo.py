# ============================================================
# Importar el catálogo de una tienda Shopify al portal TAURO
# ============================================================
#
# Objetivo: cero trabajo para el cliente. Apenas conecta su tienda por la
# instalación oficial (OAuth, que nos deja un access_token), TAURO le pide
# los productos a Shopify y le llena el catálogo solo — con nombre, SKU,
# peso y una miniatura de la foto.
#
# Lo que Shopify NO tiene (medidas, HS code, valor declarado) queda en 0/
# vacío: el producto entra como "falta completar (aduana)" y el cliente lo
# termina una sola vez. Nada se emite hasta que Tauro lo valida (activo=TRUE).
#
# La miniatura se guarda como `data:` URI (la bajamos ya reducida por el CDN
# de Shopify y la incrustamos): así el portal no tiene que cargar nada
# externo al renderizar y no hace falta abrir la CSP a dominios de terceros.

import base64
from typing import Optional

import requests

from core.database import get_conn
from servicios.shopify_app import _api, instalacion, API_VERSION
from servicios.catalogo import upsert_producto_importado

# Techos defensivos: no colgar el import por una tienda enorme ni meter
# imágenes gigantes en la base.
_MAX_PAGINAS = 12          # 12 × 250 = 3.000 productos como máximo
_LIMITE_POR_PAGINA = 250
_THUMB_ANCHO = 120         # px que le pedimos al CDN de Shopify
_THUMB_MAX_BYTES = 250_000  # si la miniatura pesa más que esto, se omite


def _thumb_data_uri(src: Optional[str]) -> Optional[str]:
    """Baja la foto ya reducida por el CDN y la devuelve como data: URI."""
    if not src:
        return None
    try:
        sep = "&" if "?" in src else "?"
        url = f"{src}{sep}width={_THUMB_ANCHO}"
        r = requests.get(url, timeout=8)
        if r.status_code != 200 or not r.content:
            return None
        if len(r.content) > _THUMB_MAX_BYTES:
            return None
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        b64 = base64.b64encode(r.content).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"[shopify_catalogo] no pude bajar la miniatura: {e}")
        return None


def _pagina_de_productos(dominio: str, token: str, page_info: Optional[str]):
    """Una página de products.json. Devuelve (items, page_info_siguiente)."""
    if page_info:
        path = f"products.json?limit={_LIMITE_POR_PAGINA}&page_info={page_info}"
    else:
        path = f"products.json?limit={_LIMITE_POR_PAGINA}"
    r = _api(dominio, token, "GET", path)
    if r is None or r.status_code != 200:
        codigo = getattr(r, "status_code", "sin respuesta")
        print(f"[shopify_catalogo] products.json de {dominio} devolvió {codigo}")
        return [], None
    try:
        items = r.json().get("products") or []
    except Exception:
        return [], None

    # Paginación por cursor: Shopify manda el próximo cursor en el header Link.
    siguiente = None
    link = r.headers.get("Link") or r.headers.get("link") or ""
    if 'rel="next"' in link:
        for parte in link.split(","):
            if 'rel="next"' in parte and "page_info=" in parte:
                try:
                    siguiente = parte.split("page_info=")[1].split(">")[0].split("&")[0]
                except Exception:
                    siguiente = None
                break
    return items, siguiente


def traer_productos(dominio: str, token: str) -> list[dict]:
    """
    Lista plana lista para el catálogo: una fila por variante con SKU.
    Cada fila: {sku, nombre, peso_kg, imagen_src}.
    """
    filas: list[dict] = []
    page_info = None
    for _ in range(_MAX_PAGINAS):
        productos, page_info = _pagina_de_productos(dominio, token, page_info)
        for prod in productos:
            titulo = str(prod.get("title") or "").strip()
            # Foto principal del producto (sirve para todas sus variantes).
            imagen_src = None
            img = prod.get("image") or {}
            if isinstance(img, dict):
                imagen_src = img.get("src")
            if not imagen_src:
                imgs = prod.get("images") or []
                if imgs and isinstance(imgs, list):
                    imagen_src = (imgs[0] or {}).get("src")
            for var in (prod.get("variants") or []):
                sku = str(var.get("sku") or "").strip()
                if not sku:
                    continue
                var_titulo = str(var.get("title") or "").strip()
                nombre = titulo
                if var_titulo and var_titulo.lower() != "default title":
                    nombre = f"{titulo} — {var_titulo}".strip(" —")
                try:
                    peso_kg = float(var.get("grams") or 0) / 1000.0
                except Exception:
                    peso_kg = 0.0
                filas.append({
                    "sku": sku,
                    "nombre": nombre or sku,
                    "peso_kg": round(peso_kg, 3),
                    "imagen_src": imagen_src,
                })
        if not page_info:
            break
    return filas


def importar_catalogo(dominio: str, cliente_id: str) -> dict:
    """
    Trae el catálogo de la tienda y lo vuelca en el del cliente.
    Devuelve {ok, creados, actualizados, sin_sku, total}.
    """
    dominio = (dominio or "").strip().lower()
    cliente_id = (cliente_id or "").strip().upper()
    if not dominio or not cliente_id:
        return {"ok": False, "error": "Faltan datos (dominio o cliente)."}

    inst = instalacion(dominio)
    token = (inst or {}).get("access_token")
    if not token:
        return {"ok": False, "error": "Esta tienda no está instalada por la app oficial "
                                      "(no hay token). Reconectala con el botón de instalación "
                                      "para poder importar productos."}

    try:
        filas = traer_productos(dominio, token)
    except Exception as e:
        print(f"[shopify_catalogo] error trayendo productos de {dominio}: {e}")
        return {"ok": False, "error": "No pudimos leer los productos de Shopify."}

    creados = actualizados = 0
    for f in filas:
        try:
            thumb = _thumb_data_uri(f.get("imagen_src"))
            estado = upsert_producto_importado(
                cliente_id, f["sku"], f["nombre"], f.get("peso_kg", 0), thumb,
            )
            if estado == "creado":
                creados += 1
            else:
                actualizados += 1
        except Exception as e:
            print(f"[shopify_catalogo] no pude importar {f.get('sku')}: {e}")

    print(f"[shopify_catalogo] {dominio} → {cliente_id}: "
          f"{creados} nuevos, {actualizados} actualizados de {len(filas)} variantes con SKU")
    return {"ok": True, "creados": creados, "actualizados": actualizados,
            "total": len(filas)}


def _dominio_instalado_de(cliente_id: str) -> Optional[str]:
    """Dominio Shopify (por OAuth, con token) atado a este cliente."""
    cliente_id = (cliente_id or "").strip().upper()
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT dominio FROM shopify_instalaciones
                    WHERE cliente_id = %s AND access_token IS NOT NULL AND access_token <> ''
                    ORDER BY instalada_en DESC LIMIT 1
                    """,
                    (cliente_id,),
                )
                row = cur.fetchone()
        return (row or {}).get("dominio") if row else None
    except Exception as e:
        print(f"[shopify_catalogo] no pude buscar la instalación de {cliente_id}: {e}")
        return None


def sincronizar_para_cliente(cliente_id: str) -> dict:
    """Botón 'sincronizar productos' del portal: encuentra la tienda del
    cliente y reimporta. Sólo funciona con tiendas instaladas por OAuth."""
    dominio = _dominio_instalado_de(cliente_id)
    if not dominio:
        return {"ok": False, "error": "Todavía no tenés una tienda Shopify conectada por la "
                                      "app oficial. Conectala desde el botón de instalación y "
                                      "el catálogo se carga solo."}
    return importar_catalogo(dominio, cliente_id)
