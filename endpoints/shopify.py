# ============================================================
# Endpoints públicos de la app de Shopify
# ============================================================
#   GET  /shopify/install    → arranca la instalación (OAuth)
#   GET  /shopify/callback   → Shopify vuelve acá con el permiso dado
#   POST /shopify/tarifas    → tarifas en vivo para el checkout
#   POST /shopify/webhook/desinstalada → limpieza al desinstalar
# ============================================================
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from servicios.shopify_app import (
    app_configurada, url_instalacion, validar_hmac_query, dominio_valido,
    canjear_token, guardar_instalacion, registrar_webhooks,
    registrar_carrier_service, cotizar_para_checkout, desinstalar, nuevo_state,
)

router = APIRouter(prefix="/shopify", tags=["shopify"])


_PAGINA = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo} · TAURO Solutions</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c0a14;color:#f4f5f7;font-family:'Helvetica Neue',system-ui,sans-serif;text-align:center;padding:24px}}
.box{{max-width:520px}}
h1{{font-size:34px;margin:0 0 14px;letter-spacing:-.02em}}
p{{color:#b9bfc7;line-height:1.65;margin:0 0 26px;font-size:16px}}
a{{display:inline-block;padding:14px 30px;border-radius:999px;text-decoration:none;font-weight:700;
color:#efe9ff;border:1px solid rgba(167,139,250,.5);
background:linear-gradient(180deg,#35206b,#180c33);box-shadow:0 0 22px rgba(124,92,246,.4)}}
small{{display:block;margin-top:26px;color:#7a828c;font-size:13px}}
</style></head><body><div class="box">
<h1>{titulo}</h1><p>{texto}</p>{boton}
<small>Tauro Solutions · logística internacional</small>
</div></body></html>"""


def _pagina(titulo: str, texto: str, boton: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGINA.format(titulo=titulo, texto=texto, boton=boton), status_code=status)


@router.get("/install", response_class=HTMLResponse)
def install(request: Request, shop: str = ""):
    """
    Shopify abre esta URL cuando alguien instala la app desde el App Store
    (o cuando el comerciante entra al link directo).
    """
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "La app de TAURO para Shopify está en revisión. "
            "Mientras tanto podés conectar tu tienda desde el portal con el modo manual.",
            '<a href="https://taurosolutions.ar/portal/tienda">Ir al portal</a>',
        )
    if not dominio_valido(shop):
        return _pagina(
            "Falta tu tienda",
            "Entrá desde el App Store de Shopify o agregá tu dominio: "
            "/shopify/install?shop=tutienda.myshopify.com",
            status=400,
        )
    destino = url_instalacion(shop.strip().lower(), nuevo_state())
    return RedirectResponse(url=destino, status_code=303)


@router.get("/callback", response_class=HTMLResponse)
def callback(request: Request):
    """Shopify vuelve con el permiso: canjeamos el token y dejamos todo listo."""
    params = dict(request.query_params)
    shop = (params.get("shop") or "").strip().lower()
    code = params.get("code") or ""

    if not app_configurada():
        return _pagina("App en preparación", "Todavía no está habilitada la instalación automática.", status=503)
    if not dominio_valido(shop) or not code:
        return _pagina("Instalación inválida", "Faltan datos de la tienda. Probá instalar de nuevo.", status=400)
    if not validar_hmac_query(params):
        # Firma inválida = alguien intentó hacerse pasar por Shopify.
        return _pagina("No pudimos verificar la instalación",
                       "La firma de Shopify no coincide. Por seguridad no continuamos.", status=401)

    data = canjear_token(shop, code)
    if not data or not data.get("access_token"):
        return _pagina("No pudimos conectar", "Shopify no nos dio el permiso. Probá de nuevo.", status=502)

    guardar_instalacion(shop, data["access_token"], data.get("scope", ""))
    topics = registrar_webhooks(shop, data["access_token"])
    carrier = registrar_carrier_service(shop, data["access_token"])

    print(f"[shopify] instalada {shop} · webhooks {topics} · carrier {carrier or 'no disponible'}")

    extra = ("Además vas a poder mostrar la tarifa TAURO en tu checkout."
             if carrier else
             "Tu plan de Shopify no permite tarifas de transportistas externos, "
             "así que tus envíos siguen con la tarifa que ya tenés configurada.")
    return _pagina(
        "¡Tienda conectada!",
        f"Listo: desde ahora cada venta con envío aparece en tu portal TAURO "
        f"lista para generar la guía con un click. {extra}",
        '<a href="https://taurosolutions.ar/portal/tienda">Ver mis pedidos</a>',
    )


@router.post("/tarifas")
async def tarifas(request: Request):
    """
    Shopify llama acá durante el checkout del comprador. Si algo falla,
    devolvemos lista vacía: la tienda muestra sus otras opciones y la
    venta nunca se traba por nosotros.
    """
    try:
        payload = await request.json()
    except Exception:
        return {"rates": []}
    # Shopify no manda la tienda en el body: viene por header. La pasamos
    # dentro del payload para saber qué política de flete aplicar.
    payload["_dominio"] = request.headers.get("x-shopify-shop-domain", "")
    try:
        return cotizar_para_checkout(payload)
    except Exception as e:
        print(f"[shopify] /tarifas error: {e}")
        return {"rates": []}


@router.post("/webhook/desinstalada")
async def desinstalada(request: Request):
    """El comerciante desinstaló la app: borramos su token."""
    from servicios.integraciones_tienda import verificar_hmac_shopify

    cuerpo = await request.body()
    shop = request.headers.get("x-shopify-shop-domain", "")
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    secreto = os.getenv("SHOPIFY_API_SECRET", "")

    if not secreto or not verificar_hmac_shopify(secreto, cuerpo, firma):
        return JSONResponse({"ok": False}, status_code=401)

    desinstalar(shop)
    print(f"[shopify] app desinstalada de {shop}")
    return {"ok": True}
