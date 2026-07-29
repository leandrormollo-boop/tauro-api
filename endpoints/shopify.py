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
    canjear_token, guardar_instalacion, registrar_webhooks, vincular_cliente,
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


def _pagina(titulo: str, texto: str, boton: str = "", status: int = 200,
            shop: str = "") -> HTMLResponse:
    # frame-ancestors SIEMPRE: estas páginas se abren dentro del admin de
    # Shopify. Sin este header el navegador bloquea el iframe y el
    # comerciante ve una pantalla en blanco sin ninguna explicación.
    permitido = f"https://{shop} " if shop else ""
    return HTMLResponse(
        _PAGINA.format(titulo=titulo, texto=texto, boton=boton),
        status_code=status,
        headers={"Content-Security-Policy":
                 f"frame-ancestors {permitido}https://admin.shopify.com https://*.myshopify.com;"},
    )


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
    shop = (shop or "").strip().lower()

    # Cuando Shopify abre la app YA INSTALADA dentro de su admin, no
    # siempre manda `shop`: el contexto viaja en `host` (base64 de
    # "admin.shopify.com/store/LA-TIENDA"). Sin esto devolvíamos 400 y
    # Shopify reintentaba en loop → pantalla en blanco eterna.
    if not dominio_valido(shop):
        shop = _shop_desde_host(request.query_params.get("host", "")) or shop

    if not dominio_valido(shop):
        # Última vía: dentro del admin, App Bridge sabe cuál es la tienda
        # aunque Shopify no la haya puesto en la URL. Le pedimos el dato al
        # navegador y recargamos con él. El flag `reintento` corta cualquier
        # posibilidad de loop si App Bridge tampoco lo tiene.
        if not request.query_params.get("reintento"):
            return _pagina_autodeteccion()
        # 200 y no 400: un error acá hace que Shopify reintente sin parar.
        return _pagina(
            "Abrí TAURO desde tu tienda",
            "No pudimos detectar tu tienda automáticamente. Entrá desde el "
            "panel de Shopify (Apps → TAURO Solutions) o agregá tu dominio a "
            "la dirección: /shopify/install?shop=tutienda.myshopify.com",
            '<a href="https://taurosolutions.ar/portal/tienda" target="_top">Ir a mi portal</a>',
        )

    # Si la tienda YA instaló la app, Shopify abre esta misma URL cada vez
    # que el comerciante hace click en TAURO desde su admin. Mandarlo de
    # nuevo al OAuth sería absurdo: le mostramos su panel.
    from servicios.shopify_app import instalacion
    try:
        inst = instalacion(shop)
    except Exception as e:
        print(f"[shopify] no pude leer la instalación de {shop}: {e}")
        inst = None

    if inst and inst.get("access_token"):
        # PERMISOS DESACTUALIZADOS: el token guardado sirve sólo para los
        # scopes con los que se autorizó. Si desde entonces la app pide más
        # (pasó al arreglar los de fulfillment orders), el token viejo sigue
        # funcionando para lo de antes y falla EN SILENCIO para lo nuevo —
        # exactamente lo que hacía que no se pudiera marcar "enviado".
        # La única salida es volver a pasar por el consentimiento.
        from servicios.shopify_app import SCOPES
        guardados = {s.strip() for s in (inst.get("scopes") or "").split(",") if s.strip()}
        pedidos = {s.strip() for s in SCOPES.split(",") if s.strip()}
        faltantes = pedidos - guardados
        if faltantes:
            print(f"[shopify] {shop} tiene permisos viejos, faltan {sorted(faltantes)} "
                  f"→ se reenvía al consentimiento para actualizarlos")
            return RedirectResponse(url=url_instalacion(shop, nuevo_state()),
                                    status_code=303)
        return _panel_tienda(shop, inst, request.query_params.get("host", ""))

    destino = url_instalacion(shop, nuevo_state())
    return RedirectResponse(url=destino, status_code=303)


def _pagina_autodeteccion() -> HTMLResponse:
    """
    Pantalla puente: le pregunta a App Bridge cuál es la tienda y recarga
    con ese dato. Dura un parpadeo y evita que el comerciante tenga que
    escribir su dominio a mano.
    """
    api_key = os.getenv("SHOPIFY_API_KEY", "")
    return HTMLResponse(
        headers={"Content-Security-Policy":
                 "frame-ancestors https://admin.shopify.com https://*.myshopify.com;"},
        content=f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>TAURO Solutions</title>
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"
        data-api-key="{api_key}"></script>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0c0a14; color:#8b86a0;
         font-family:'Helvetica Neue',Helvetica,Arial,sans-serif; font-size:14px; }}
</style></head>
<body>
  <div>Abriendo TAURO…</div>
  <script>
    (function () {{
      // App Bridge expone la tienda en window.shopify.config.shop.
      // Si no está, probamos con el host de la URL padre.
      var shop = "";
      try {{ shop = (window.shopify && window.shopify.config && window.shopify.config.shop) || ""; }} catch (e) {{}}
      if (!shop) {{
        try {{
          var h = new URLSearchParams(location.search).get("host");
          if (h) {{
            var d = atob(h.replace(/-/g, "+").replace(/_/g, "/"));
            var m = d.match(/\\/store\\/([^\\/?#]+)/);
            if (m) shop = m[1] + ".myshopify.com";
          }}
        }} catch (e) {{}}
      }}
      var destino = "/shopify/install?reintento=1" + (shop ? "&shop=" + encodeURIComponent(shop) : "");
      location.replace(destino);
    }})();
  </script>
</body></html>""")


def _shop_desde_host(host_b64: str) -> str:
    """
    Shopify manda `host` en base64: "admin.shopify.com/store/mitienda".
    De ahí sacamos el dominio .myshopify.com que necesitamos.
    """
    if not host_b64:
        return ""
    try:
        import base64
        # base64 url-safe sin padding: se lo agregamos nosotros.
        faltante = "=" * (-len(host_b64) % 4)
        crudo = base64.urlsafe_b64decode(host_b64 + faltante).decode("utf-8", "ignore")
    except Exception:
        return ""
    marca = "/store/"
    if marca not in crudo:
        return ""
    tienda = crudo.split(marca, 1)[1].split("/")[0].split("?")[0].strip().lower()
    return f"{tienda}.myshopify.com" if tienda else ""


def _panel_tienda(shop: str, inst: dict, host: str = "") -> HTMLResponse:
    """
    Lo que el comerciante ve al abrir TAURO desde su admin de Shopify:
    su estado de un vistazo y el acceso al portal donde opera.
    """
    cliente = (inst or {}).get("cliente_id") or ""
    pendientes = 0
    if cliente:
        try:
            from servicios.integraciones_tienda import contar_pendientes
            pendientes = contar_pendientes(cliente)
        except Exception as e:
            print(f"[shopify] no pude contar pendientes de {cliente}: {e}")

    if not cliente:
        estado = ("Tu tienda está conectada, pero todavía no la vinculaste a tu cuenta "
                  "de TAURO. Entrá al portal, sección <b>Mi tienda</b>, y tocá "
                  "«Es mi tienda — vincular».")
        cta = "Vincular mi tienda"
    elif pendientes:
        estado = (f"Tenés <b>{pendientes} venta{'s' if pendientes != 1 else ''}</b> "
                  f"esperando que generes el envío.")
        cta = "Ver mis pedidos"
    else:
        estado = ("Todo al día: no hay ventas pendientes de envío. Cuando entre una "
                  "nueva, aparece sola acá y en tu portal.")
        cta = "Abrir mi portal"

    # Para que la pantalla se vea DENTRO del admin de Shopify (y no en una
    # ventana aparte) hacen falta tres cosas:
    #   1. App Bridge, el puente oficial que Shopify espera en apps embebidas
    #   2. frame-ancestors permitiendo a admin.shopify.com y a la tienda
    #   3. NO mandar X-Frame-Options, que bloquearía el iframe
    api_key = os.getenv("SHOPIFY_API_KEY", "")
    headers = {
        "Content-Security-Policy":
            f"frame-ancestors https://{shop} https://admin.shopify.com;",
    }

    return HTMLResponse(headers=headers, content=f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAURO Solutions</title>
<script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"
        data-api-key="{api_key}"></script>
<style>
  body {{ margin:0; padding:40px 24px; background:#0c0a14; color:#f4f5f7;
         font-family:'Helvetica Neue',Helvetica,Arial,sans-serif; }}
  .box {{ max-width:560px; margin:0 auto; text-align:center; }}
  .marca {{ font-size:22px; font-weight:700; letter-spacing:.08em; margin-bottom:6px; }}
  .marca span {{ color:#a78bfa; }}
  .sub {{ font-size:11px; letter-spacing:.14em; color:#8b86a0;
          text-transform:uppercase; margin-bottom:34px; }}
  .card {{ background:#151221; border:1px solid #2a2540; border-radius:16px;
           padding:30px 26px; }}
  .num {{ font-size:52px; font-weight:700; line-height:1;
          color:#a78bfa; margin-bottom:10px; }}
  p {{ color:#b9bfc7; line-height:1.7; margin:0 0 26px; font-size:15px; }}
  a.btn {{ display:inline-block; background:#7c5cf6; color:#fff; padding:14px 32px;
           border-radius:999px; text-decoration:none; font-weight:600; }}
  .pie {{ margin-top:26px; font-size:12.5px; color:#6f6a85; line-height:1.7; }}
  .pie b {{ color:#9a94b0; }}
</style></head><body>
<div class="box">
  <div class="marca">TAURO <span>SOLUTIONS</span></div>
  <div class="sub">Logística internacional</div>
  <div class="card">
    {f'<div class="num">{pendientes}</div>' if pendientes else ''}
    <p>{estado}</p>
    <a class="btn" href="https://taurosolutions.ar/portal/tienda" target="_blank">{cta} →</a>
  </div>
  <div class="pie">
    Tienda conectada: <b>{shop}</b><br>
    Cada venta con envío al exterior aparece en tu portal lista para generar la guía.
  </div>
</div></body></html>""")


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

    # Si el comerciante instaló con su sesión del portal abierta (el caso
    # normal), la tienda queda atada a su cuenta acá mismo. Si no, la
    # reclama después desde /portal/tienda.
    dueno = None
    try:
        from servicios.auth import validar_token
        dueno = validar_token(request.cookies.get("token") or "")
        if dueno:
            vincular_cliente(shop, dueno)
    except Exception as e:
        print(f"[shopify] no pude vincular {shop} al instalar: {e}")

    print(f"[shopify] instalada {shop} · webhooks {topics} · "
          f"carrier {carrier or 'no disponible'} · cliente {dueno or 'sin vincular'}")

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

    # DOS PROTECCIONES QUE NO SON OPCIONALES ACÁ:
    #
    # 1. run_in_threadpool — cotizar_para_checkout es 100% síncrona (psycopg2,
    #    requests). Llamarla derecho desde un handler async bloquea el event
    #    loop: un checkout lento dejaría sin atender a TODOS los demás
    #    compradores, de todas las tiendas, hasta que termine.
    #
    # 2. wait_for — Shopify corta cerca de los 10s. Sin deadline propio, un
    #    courier colgado puede tardar minutos y el comprador se queda sin
    #    opción de envío. Preferimos la tarifa de emergencia a tiempo antes
    #    que la tarifa exacta tarde.
    import asyncio
    from starlette.concurrency import run_in_threadpool

    try:
        return await asyncio.wait_for(
            run_in_threadpool(cotizar_para_checkout, payload),
            timeout=PRESUPUESTO_CHECKOUT_S,
        )
    except asyncio.TimeoutError:
        print(f"[shopify] /tarifas superó {PRESUPUESTO_CHECKOUT_S}s → tarifa de emergencia")
        return _tarifas_de_emergencia(payload)
    except Exception as e:
        print(f"[shopify] /tarifas error: {e}")
        return _tarifas_de_emergencia(payload)


# Presupuesto propio, con aire respecto del corte de Shopify (~10s).
PRESUPUESTO_CHECKOUT_S = 6.0


def _tarifas_de_emergencia(payload: dict) -> dict:
    """
    Última red: si el camino normal no llegó a tiempo o falló, el comprador
    igual ve una opción de envío. Nunca devolvemos vacío desde acá.
    """
    try:
        import os
        from servicios.tarifas_cache import tarifa_emergencia

        rate = (payload or {}).get("rate") or {}
        destino = rate.get("destination") or {}
        pais = (destino.get("country") or "").upper()
        if not pais or pais == "AR":
            return {"rates": []}

        items = rate.get("items") or []
        peso = sum((it.get("grams") or 0) * (it.get("quantity") or 1) for it in items) / 1000.0
        peso = max(round(peso, 2), 0.5)
        try:
            from servicios.cotizador import dolar_ars
            dolar = dolar_ars()
        except Exception:
            # Este es el camino de ÚLTIMA instancia (ya falló todo lo demás):
            # si hasta la base está caída, el entorno es lo único que queda.
            dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))

        rates = []
        for c in tarifa_emergencia(pais, peso, dolar):
            rates.append({
                "service_name": f"{c['nombre']} — {c['servicio']}",
                "service_code": f"TAURO_{c['id'].upper()}",
                "total_price": str(int(round(float(c["precio_ars"]) * 100))),
                "currency": "ARS",
                "description": f"Entrega estimada {c.get('dias_estimados', '5-9')} días hábiles · vía TAURO Solutions",
            })
        return {"rates": rates}
    except Exception as e:
        print(f"[shopify] ni la tarifa de emergencia salió: {e}")
        return {"rates": []}


# ── Webhooks de privacidad (obligatorios para el App Store) ─────
# Shopify exige estos tres endpoints a toda app publicada, y los prueba
# durante la revisión. Todos verifican la firma con el API secret de la
# app: sin eso, cualquiera podría pedir o borrar datos de un comercio.

def _firma_valida_app(cuerpo: bytes, firma: str) -> bool:
    from servicios.integraciones_tienda import verificar_hmac_shopify
    secreto = os.getenv("SHOPIFY_API_SECRET", "")
    return bool(secreto) and verificar_hmac_shopify(secreto, cuerpo, firma)


@router.post("/webhook/customers/data_request")
async def gdpr_data_request(request: Request):
    """
    Un comprador pidió ver los datos que la tienda tiene sobre él.
    TAURO guarda datos de compradores sólo dentro del pedido que el
    comercio nos manda, así que dejamos constancia y el comercio responde
    con lo que le mostramos en su portal.
    """
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)
    print(f"[gdpr] data_request de {request.headers.get('x-shopify-shop-domain', '?')}: "
          f"{cuerpo[:400]!r}")
    return {"ok": True}


@router.post("/webhook/customers/redact")
async def gdpr_customer_redact(request: Request):
    """
    Un comprador pidió que borren sus datos. Anonimizamos su información
    personal en los pedidos que tengamos de esa tienda, conservando el
    registro comercial (montos y fechas) que hace falta por contabilidad.
    """
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    dominio = request.headers.get("x-shopify-shop-domain", "")
    try:
        import json as _json
        datos = _json.loads(cuerpo.decode("utf-8"))
        ids = [str(o) for o in (datos.get("orders_to_redact") or [])]
        from servicios.integraciones_tienda import anonimizar_pedidos
        n = anonimizar_pedidos(dominio, ids)
        print(f"[gdpr] redact de comprador en {dominio}: {n} pedido(s) anonimizados")
    except Exception as e:
        print(f"[gdpr] error procesando customers/redact: {e}")
    return {"ok": True}


@router.post("/webhook/shop/redact")
async def gdpr_shop_redact(request: Request):
    """
    Pasaron 48 hs desde que un comercio desinstaló la app: Shopify pide
    que borremos todo lo suyo.
    """
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    dominio = request.headers.get("x-shopify-shop-domain", "")
    try:
        from servicios.integraciones_tienda import borrar_datos_tienda
        n = borrar_datos_tienda(dominio)
        desinstalar(dominio)
        print(f"[gdpr] shop_redact de {dominio}: {n} registro(s) borrados")
    except Exception as e:
        print(f"[gdpr] error procesando shop/redact: {e}")
    return {"ok": True}


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
