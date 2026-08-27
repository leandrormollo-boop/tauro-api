"""Integración mínima de Meta Ads, cerrada por defecto y sin PII."""

import os
from typing import Optional


META_PIXEL_MARKER = "<!-- META_PIXEL -->"


def obtener_meta_pixel_id() -> Optional[str]:
    """Devuelve únicamente IDs numéricos plausibles; cualquier otro valor apaga el Pixel."""
    valor = os.getenv("META_PIXEL_ID", "").strip()
    if not (5 <= len(valor) <= 32 and valor.isascii() and valor.isdigit()):
        return None
    return valor


def meta_pixel_habilitado(path: str) -> bool:
    """Limita Meta a la home pública, lejos de superficies con datos de clientes."""
    return path == "/web" and obtener_meta_pixel_id() is not None


def construir_content_security_policy(
    nonce: str,
    *,
    pixel_habilitado: bool = False,
) -> str:
    scripts = ["'self'", f"'nonce-{nonce}'"]
    # Las fotos del catálogo se sirven desde el CDN oficial de Shopify. Son
    # imágenes pasivas; scripts y conexiones siguen cerrados a terceros.
    imagenes = ["'self'", "data:", "https://cdn.shopify.com", "https://*.shopifycdn.com"]
    conexiones = ["'self'"]

    # Orígenes exactos que usa el Pixel básico. No se abren comodines de
    # Facebook/Meta y estas excepciones no existen mientras el Pixel esté off.
    if pixel_habilitado:
        scripts.append("https://connect.facebook.net")
        imagenes.append("https://www.facebook.com")
        conexiones.extend(("https://connect.facebook.net", "https://www.facebook.com"))

    return (
        f"default-src 'self'; script-src {' '.join(scripts)}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        f"img-src {' '.join(imagenes)}; connect-src {' '.join(conexiones)}; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-src 'none'"
    )


def inyectar_meta_pixel(html: str, pixel_id: Optional[str]) -> str:
    """Agrega sólo el loader propio; el ID validado se entrega desde el backend."""
    etiqueta = '<script src="/meta-pixel.js" defer></script>' if pixel_id else ""
    return html.replace(META_PIXEL_MARKER, etiqueta, 1)


def javascript_meta_pixel(pixel_id: str) -> str:
    """Loader mínimo: sin advanced matching, PII ni eventos de negocio."""
    if not (5 <= len(pixel_id) <= 32 and pixel_id.isascii() and pixel_id.isdigit()):
        return ""
    return f'''(function (window, document, pixelId) {{
  "use strict";
  if (window.fbq) return;
  var fbq = window.fbq = function () {{
    fbq.callMethod ? fbq.callMethod.apply(fbq, arguments) : fbq.queue.push(arguments);
  }};
  if (!window._fbq) window._fbq = fbq;
  fbq.push = fbq;
  fbq.loaded = true;
  fbq.version = "2.0";
  fbq.queue = [];
  var script = document.createElement("script");
  script.async = true;
  script.src = "https://connect.facebook.net/en_US/fbevents.js";
  var firstScript = document.getElementsByTagName("script")[0];
  firstScript.parentNode.insertBefore(script, firstScript);
  fbq("set", "autoConfig", false, pixelId);
  fbq("init", pixelId);
  fbq("track", "PageView");
}})(window, document, "{pixel_id}");
'''
