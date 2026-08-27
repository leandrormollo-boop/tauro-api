# ============================================================
# Endpoints públicos de la app de Shopify
# ============================================================
#   GET  /shopify/install    → arranca la instalación (OAuth)
#   GET  /shopify/callback   → Shopify vuelve acá con el permiso dado
#   POST /shopify/tarifas    → tarifas en vivo para el checkout
#   POST /shopify/webhook/desinstalada → limpieza al desinstalar
# ============================================================
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from servicios.shopify_app import (
    app_configurada, url_instalacion, validar_hmac_query, dominio_valido,
    canjear_token, guardar_instalacion, registrar_webhooks,
    desinstalar, nuevo_state, ShopifyWebhookVerificationError,
    confirmar_shop_redact, confirmar_webhooks_verificados,
    webhooks_requeridos, api_key_publica,
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
    # La app pública está declarada como externa (`embedded = false`). Ninguna
    # pantalla Shopify de TAURO debe poder cargarse dentro de un iframe.
    return HTMLResponse(
        _PAGINA.format(titulo=titulo, texto=texto, boton=boton),
        status_code=status,
        headers={
            "Content-Security-Policy": "frame-ancestors 'none';",
            "X-Frame-Options": "DENY",
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


@router.get("/install", response_class=HTMLResponse)
def install(request: Request, shop: str = ""):
    """
    Shopify abre esta URL desde su superficie de instalación o desde Apps.
    TAURO no ofrece un formulario ni un link propio para escribir el dominio.
    """
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "La app de TAURO para Shopify todavía no está disponible. "
            "Cuando quede habilitada, vas a poder instalarla únicamente desde Shopify Apps.",
            '<a href="https://admin.shopify.com">Volver a Shopify</a>',
        )
    shop = (shop or "").strip().lower()

    # Shopify puede abrir una app externa con el contexto en `host`. Se decodifica
    # sólo en el servidor; no se carga App Bridge ni se confía en este dato para
    # mostrar estadísticas (eso exige además una sesión TAURO coincidente).
    if not dominio_valido(shop):
        shop = _shop_desde_host(request.query_params.get("host", "")) or shop

    if not dominio_valido(shop):
        # Una app externa recibe `shop` desde Shopify. No se intenta reconstruir
        # el comercio en el navegador con App Bridge ni se pide que lo escriban.
        return _pagina(
            "Abrí TAURO desde tu tienda",
            "Shopify no incluyó una tienda válida. Volvé al panel "
            "de Shopify y abrí TAURO Solutions desde Apps.",
            '<a href="https://admin.shopify.com">Volver a Shopify</a>',
        )

    # Reintento explícito después de una autorización incompleta. No hacemos
    # llamadas al Admin API desde cada apertura del panel: este link inicia
    # nuevamente OAuth y sólo Shopify puede completar el callback firmado.
    if request.query_params.get("reautorizar") == "1":
        return _redirect_oauth(shop)

    # Si la tienda YA instaló la app, Shopify abre esta misma URL cada vez
    # que el comerciante hace click en TAURO desde su admin. Mandarlo de
    # nuevo al OAuth sería absurdo: le mostramos su panel.
    from servicios.shopify_app import instalacion
    try:
        inst = instalacion(shop)
    except Exception as e:
        print(f"[shopify] no pude leer la instalación: {type(e).__name__}")
        inst = None

    if inst and not inst.get("webhooks_ready"):
        print("[shopify] instalación pendiente de verificar webhooks → nuevo consentimiento")
        return _redirect_oauth(shop)

    if inst and inst.get("access_token"):
        # Una fila histórica puede tener todos los scopes correctos y aun así
        # pertenecer a la app vieja. Con la app pública activa, esa tienda debe
        # pasar una vez por SU OAuth; mostrar el panel legado acá impediría la
        # migración para siempre.
        app_instalada = str(inst.get("app_client_id") or "").strip()
        if app_instalada != api_key_publica():
            print("[shopify] instalación histórica → nuevo consentimiento")
            return _redirect_oauth(shop)
        if not inst.get("token_rotativo"):
            print("[shopify] instalación pública sin refresh → nuevo consentimiento")
            return _redirect_oauth(shop)
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
            print(f"[shopify] permisos desactualizados: {len(faltantes)} faltante(s)")
            return _redirect_oauth(shop)
        return _panel_tienda(shop, inst, _cliente_sesion_tauro(request))

    return _redirect_oauth(shop)


def _redirect_oauth(shop: str) -> RedirectResponse:
    """
    Manda al consentimiento de Shopify guardando el `state` en una cookie
    corta. Hasta ahora el state se generaba, viajaba... y nadie lo comparaba
    a la vuelta — o sea, teatro. El callback ahora exige que coincida
    (anti-CSRF del flujo OAuth, y Shopify lo revisa para el App Store).
    """
    state = nuevo_state()
    resp = RedirectResponse(url=url_instalacion(shop, state), status_code=303)
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.set_cookie(
        key="shopify_state", value=state,
        max_age=600, httponly=True, secure=True,
        # Lax: la vuelta de Shopify es una navegación top-level GET, la
        # cookie viaja. Strict la dejaría afuera y rompería el flujo.
        samesite="lax",
    )
    return resp


def _cliente_sesion_tauro(request: Request) -> str:
    """Identidad TAURO autenticada; nunca se infiere de la URL de Shopify."""
    token = str(request.cookies.get("token") or "")
    if not token:
        return ""
    try:
        from servicios.auth import validar_token
        return str(validar_token(token) or "").strip().upper()
    except Exception:
        return ""


def _shop_desde_host(host_b64: str) -> str:
    """Obtiene el dominio del contexto externo que Shopify agrega a la URL."""
    if not host_b64:
        return ""
    try:
        import base64
        faltante = "=" * (-len(host_b64) % 4)
        crudo = base64.urlsafe_b64decode(host_b64 + faltante).decode("utf-8", "ignore")
    except Exception:
        return ""
    marca = "/store/"
    if marca not in crudo:
        return ""
    tienda = crudo.split(marca, 1)[1].split("/", 1)[0].split("?", 1)[0].strip().lower()
    dominio = f"{tienda}.myshopify.com" if tienda else ""
    return dominio if dominio_valido(dominio) else ""


def _panel_tienda(shop: str, inst: dict, cliente_sesion: str = "") -> HTMLResponse:
    """
    Lo que el comerciante ve al abrir TAURO desde su admin de Shopify:
    su estado de un vistazo y el acceso al portal donde opera.
    """
    cliente = str((inst or {}).get("cliente_id") or "").strip().upper()
    cliente_sesion = str(cliente_sesion or "").strip().upper()
    sesion_coincide = bool(cliente and cliente_sesion == cliente)
    pendientes = 0
    sync_estado = None
    stock = None
    if sesion_coincide:
        try:
            from servicios.integraciones_tienda import contar_pendientes
            pendientes = contar_pendientes(cliente)
            from servicios.catalogo import estado_sincronizacion_cliente, resumen_stock_cliente
            sync_estado = estado_sincronizacion_cliente(cliente)
            stock = resumen_stock_cliente(cliente)
        except Exception as e:
            print(f"[shopify] no pude armar panel: {type(e).__name__}")

    if not cliente:
        estado = ("Tu tienda está conectada, pero todavía no la vinculaste a tu cuenta "
                  "de TAURO. Entrá al portal, sección <b>Mi tienda</b>, y tocá "
                  "«Es mi tienda — vincular».")
        cta = "Vincular mi tienda"
        cta_url = "https://taurosolutions.ar/portal/tienda"
    elif not sesion_coincide:
        # `shop` es un parámetro público del OAuth. Nunca alcanza para revelar
        # pedidos, stock ni la identidad del cliente dueño de la instalación.
        estado = ("TAURO está instalada. Para ver pedidos, catálogo y stock, "
                  "iniciá sesión con la cuenta TAURO vinculada a esta tienda.")
        cta = "Iniciar sesión"
        cta_url = "https://taurosolutions.ar/portal/login"
    elif sync_estado and sync_estado.get("estado") == "REAUTORIZAR":
        estado = ("Tu tienda está conectada, pero necesita que autorices una vez "
                  "el catálogo y el inventario para mostrar el stock en TAURO.")
        cta = "Autorizar catálogo y stock"
        cta_url = f"/shopify/install?shop={shop}&reautorizar=1"
    elif pendientes:
        estado = (f"Tenés <b>{pendientes} venta{'s' if pendientes != 1 else ''}</b> "
                  f"esperando que generes el envío.")
        cta = "Ver mis pedidos"
        cta_url = "https://taurosolutions.ar/portal/tienda"
    else:
        variantes = int((stock or {}).get("variantes") or 0)
        unidades = int((stock or {}).get("unidades_disponibles") or 0)
        estado = ("Todo al día: no hay ventas pendientes de envío. "
                  f"TAURO está siguiendo <b>{variantes} variantes</b> y "
                  f"<b>{unidades} unidades disponibles</b> en Shopify.")
        cta = "Abrir mi portal"
        cta_url = "https://taurosolutions.ar/portal/catalogo"

    # Arquitectura inequívocamente externa: coincide con `embedded = false`.
    headers = {
        "Content-Security-Policy": "frame-ancestors 'none';",
        "X-Frame-Options": "DENY",
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "Vary": "Cookie",
    }
    pie = (
        f"Tienda conectada: <b>{shop}</b><br>"
        "Cada venta con envío al exterior aparece en tu portal lista para generar la guía."
        if sesion_coincide
        else "La información operativa se muestra únicamente después de iniciar sesión."
    )

    return HTMLResponse(headers=headers, content=f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAURO Solutions</title>
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
    <a class="btn" href="{cta_url}">{cta} →</a>
  </div>
  <div class="pie">{pie}</div>
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

    # `state` vincula este callback al navegador que inició OAuth. El HMAC sólo
    # prueba que Shopify firmó la respuesta y no reemplaza este control CSRF.
    # Cookie o query ausentes, expirados o distintos se rechazan siempre.
    state_cookie = request.cookies.get("shopify_state") or ""
    state_query = str(params.get("state") or "")
    state_verificado = bool(
        state_cookie
        and state_query
        and secrets.compare_digest(state_query, state_cookie)
    )
    if not state_verificado:
        respuesta = _pagina("La instalación expiró",
                            "Por seguridad, empezá de nuevo desde el link de instalación.",
                            f'<a href="/shopify/install?shop={shop}">Reintentar instalación</a>',
                            status=403)
        respuesta.delete_cookie("shopify_state")
        return respuesta

    data = canjear_token(shop, code)
    if not data or not data.get("access_token"):
        return _pagina("No pudimos conectar", "Shopify no nos dio el permiso. Probá de nuevo.", status=502)

    from servicios.shopify_app import SCOPES
    scopes_requeridos = {scope.strip() for scope in SCOPES.split(",") if scope.strip()}
    scopes_otorgados = {
        scope.strip() for scope in str(data.get("scope") or "").split(",")
        if scope.strip()
    }
    faltantes = scopes_requeridos - scopes_otorgados
    if faltantes:
        print(f"[shopify] OAuth incompleto: {len(faltantes)} scope(s) faltante(s)")
        respuesta = _pagina(
            "Faltan permisos de Shopify",
            "Shopify no confirmó todos los permisos necesarios para recibir ventas y "
            "devolver el tracking. Volvé a instalar la app desde tu tienda.",
            f'<a href="/shopify/install?shop={shop}">Reintentar instalación</a>',
            status=502,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    # Límite temporal de esta generación. Se persiste ANTES de tocar las
    # suscripciones: mientras Shopify registra webhooks ya no existe una
    # ventana en la que una entrega nueva pueda caer sobre el owner anterior.
    oauth_activada_desde = datetime.now(timezone.utc)

    # El claim se deriva antes de crear la generación pendiente. Token, owner y
    # binding se escriben juntos; sin sesión TAURO el owner queda NULL y el
    # binding anterior inactivo, sin una ventana donde las ventas vuelvan a A.
    dueno = ""
    try:
        dueno = _cliente_sesion_tauro(request)
    except Exception as exc:
        print(f"[shopify] no pude validar sesión de claim: {type(exc).__name__}")
        dueno = ""

    try:
        generation = guardar_instalacion(
            shop,
            data["access_token"],
            data.get("scope", ""),
            oauth_activada_desde,
            cliente_claim=dueno,
            refresh_token=data.get("refresh_token", ""),
            expires_in=data.get("expires_in"),
            refresh_token_expires_in=data.get("refresh_token_expires_in"),
        )
    except Exception as exc:
        print(f"[shopify] no pude persistir la generación: {type(exc).__name__}")
        respuesta = _pagina(
            "No pudimos terminar la vinculación",
            "Shopify autorizó la app, pero TAURO no pudo guardar la conexión "
            "de forma segura. Reintentá en unos minutos.",
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    try:
        topics = registrar_webhooks(shop, data["access_token"])
    except ShopifyWebhookVerificationError:
        respuesta = _pagina(
            "No pudimos verificar Shopify",
            "La conexión no respondió a tiempo. La tienda quedó aislada de "
            "cualquier cuenta anterior; reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta
    except Exception as exc:
        print(f"[shopify] error registrando webhooks: {type(exc).__name__}")
        respuesta = _pagina(
            "No pudimos verificar Shopify",
            "La conexión no respondió de forma segura. La tienda quedó pendiente "
            "y no puede operar; reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta
    esperados = webhooks_requeridos()
    if set(topics) != esperados:
        respuesta = _pagina(
            "Conexión incompleta",
            "Shopify autorizó la app, pero no confirmó todos los avisos automáticos "
            "de ventas, productos e inventario. La tienda quedó pendiente y no puede "
            "operar. Reintentá en "
            "unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar instalación</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    try:
        lista = list(topics)
        if not confirmar_webhooks_verificados(shop, generation, lista):
            raise RuntimeError("generación OAuth reemplazada")
    except Exception as exc:
        print(f"[shopify] no pude habilitar webhooks: {type(exc).__name__}")
        respuesta = _pagina(
            "Conexión pendiente",
            "Los avisos de Shopify quedaron verificados, pero TAURO no pudo "
            "habilitar esta generación de forma segura. Reintentá en unos minutos.",
            f'<a href="/shopify/install?shop={shop}&reautorizar=1">Reintentar conexión</a>',
            status=503,
        )
        respuesta.delete_cookie("shopify_state")
        return respuesta

    # Importar catálogo + stock en segundo plano. El wrapper captura cualquier
    # fallo y lo deja visible en shopify_sync_estado; nunca rompe el OAuth.
    if dueno:
        try:
            from servicios.shopify_catalogo import lanzar_sincronizacion
            lanzar_sincronizacion(shop, dueno)
        except Exception as e:
            print(f"[shopify] no pude lanzar sincronización: {type(e).__name__}")

    print(f"[shopify] generación habilitada · {len(topics)} webhook(s) · "
          f"{'con claim' if dueno else 'ownerless'}")

    # Shopify exige que el OAuth termine dentro de la interfaz de la app. La
    # siguiente apertura ya reconoce la instalación y muestra su panel; no se
    # deja al comercio en una pantalla intermedia de éxito.
    respuesta = RedirectResponse(
        url=f"/shopify/install?shop={shop}",
        status_code=303,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )
    respuesta.delete_cookie("shopify_state")
    return respuesta


@router.post("/tarifas")
async def tarifas(request: Request):
    """
    RETIRADO (28/07). La app ya no cotiza el envío dentro del checkout.

    El precio que ve el comprador lo define el comerciante con sus propias
    tarifas de Shopify; TAURO sólo toma la venta cuando entra y la carga como
    solicitud en el portal. El endpoint sigue vivo, y devolviendo lista vacía
    a propósito, por dos motivos:

      1. Si a alguna tienda le quedó un carrier service colgado, Shopify va a
         seguir llamando acá. Contestar 200 con [] hace que simplemente no
         aparezca nuestra opción; devolver un error o 404 le mete un timeout
         al checkout del comprador.
      2. Deja registro en el log si alguna tienda todavía nos está llamando,
         que es la señal de que hay un carrier service sin dar de baja.

    Toda la maquinaria de cotización (cascada de resiliencia, cache de
    tarifas, peso facturable) sigue en pie: la usa el portal y el cotizador
    público de la web, que son los que sí cotizan.
    """
    print("[shopify] endpoint de tarifas legado invocado; respuesta vacía")
    return {"rates": []}


# ── Webhooks de privacidad (obligatorios para el App Store) ─────
# Shopify exige estos tres endpoints a toda app publicada, y los prueba
# durante la revisión. Todos verifican la firma con el API secret de la
# app: sin eso, cualquiera podría pedir o borrar datos de un comercio.

def _firma_valida_app(cuerpo: bytes, firma: str) -> bool:
    from servicios.shopify_app import firma_valida_webhook_app
    return firma_valida_webhook_app(cuerpo, firma)


def _topic_exacto(request: Request, esperado: str) -> bool:
    return request.headers.get("x-shopify-topic", "").strip().lower() == esperado


def _payload_gdpr_y_dominio(
    cuerpo: bytes,
    dominio_header: str,
    contrato: str = "",
) -> tuple[dict, str]:
    """Parsea el cuerpo firmado y ata la operacion al dominio que contiene.

    El HMAC cubre el body, no los headers. Confiar solamente en
    ``X-Shopify-Shop-Domain`` permitiria cambiar ese header al reproducir un
    webhook valido y operar sobre otra tienda. Por eso ambos dominios deben
    coincidir antes de leer, anonimizar o borrar datos.
    """
    import json as _json

    datos = _json.loads(cuerpo.decode("utf-8"))
    if not isinstance(datos, dict):
        raise ValueError("payload GDPR invalido")
    dominio_body = str(datos.get("shop_domain") or "").strip().lower()
    dominio_header = str(dominio_header or "").strip().lower()
    if (
        not dominio_valido(dominio_body)
        or dominio_header != dominio_body
    ):
        raise ValueError("dominio GDPR inconsistente")
    if not str(datos.get("shop_id") or "").strip():
        raise ValueError("shop_id faltante")

    customer = datos.get("customer")
    tiene_customer = isinstance(customer, dict) and bool(
        str(customer.get("id") or "").strip()
    )
    if contrato == "customers/data_request":
        if (
            not tiene_customer
            or "orders_requested" not in datos
            or not isinstance(datos.get("orders_requested"), list)
            or "orders_to_redact" in datos
        ):
            raise ValueError("contrato customers/data_request inválido")
    elif contrato == "customers/redact":
        if (
            not tiene_customer
            or "orders_to_redact" not in datos
            or not isinstance(datos.get("orders_to_redact"), list)
            or "orders_requested" in datos
        ):
            raise ValueError("contrato customers/redact inválido")
    elif contrato == "shop/redact":
        if any(
            clave in datos
            for clave in ("customer", "orders_requested", "orders_to_redact")
        ):
            raise ValueError("contrato shop/redact inválido")
    return datos, dominio_body


@router.post("/webhook/customers/data_request")
async def gdpr_data_request(request: Request):
    """
    Un comprador pidió ver los datos que la tienda tiene sobre él.
    TAURO guarda datos de compradores sólo dentro del pedido que el
    comercio nos manda, así que dejamos constancia y el comercio responde
    con lo que le mostramos en su portal.
    """
    if not _topic_exacto(request, "customers/data_request"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "customers/data_request",
        )
        from servicios.shopify_gdpr import (
            SolicitudGDPRInvalida, encolar_data_request,
            normalizar_payload_data_request, resolver_order_ids_por_email,
        )
        referencia = normalizar_payload_data_request(datos)
        # Defensa en profundidad: el normalizador no decide ownership.
        if referencia["dominio"] != dominio:
            raise SolicitudGDPRInvalida("dominio inconsistente")
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        email_memoria = referencia.pop("customer_email_memoria", "")
        if not referencia["orders_requested"] and email_memoria:
            referencia["orders_requested"] = resolver_order_ids_por_email(
                dominio, email_memoria,
            )
        resultado = encolar_data_request(**referencia)
    except Exception as e:
        # Nunca incluir el body ni PII en el log. Un fallo de persistencia
        # devuelve 503 para que Shopify conserve y reintente la obligacion.
        print(f"[gdpr] no pude persistir data_request: {type(e).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    print("[gdpr] data_request persistido")
    return {"ok": True}


@router.post("/webhook/customers/redact")
async def gdpr_customer_redact(request: Request):
    """
    Un comprador pidió que borren sus datos. Anonimizamos su información
    personal en los pedidos que tengamos de esa tienda, conservando el
    registro comercial (montos y fechas) que hace falta por contabilidad.
    """
    if not _topic_exacto(request, "customers/redact"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    if not _firma_valida_app(cuerpo, request.headers.get("x-shopify-hmac-sha256", "")):
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "customers/redact",
        )
        from servicios.shopify_gdpr import normalizar_order_ids
        ids = normalizar_order_ids(
            datos.get("orders_to_redact"), campo="orders_to_redact",
        )
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        if not ids:
            from servicios.shopify_gdpr import resolver_order_ids_por_email
            customer = datos.get("customer") if isinstance(datos.get("customer"), dict) else {}
            # El email firmado vive solamente durante esta consulta. No se
            # persiste ni se incluye en logs, auditoria o respuesta.
            ids = resolver_order_ids_por_email(
                dominio, str(customer.get("email") or ""),
            )
        from servicios.integraciones_tienda import anonimizar_pedidos
        n = anonimizar_pedidos(dominio, ids)
        print(f"[gdpr] redact de comprador: {n} registro(s) sanitizados")
    except Exception as e:
        print(f"[gdpr] error procesando customers/redact: {type(e).__name__}")
        # Un 200 confirmaría un borrado que no ocurrió y Shopify dejaría de
        # reintentarlo. El 503 conserva la obligación pendiente sin filtrar el
        # detalle interno.
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@router.post("/webhook/shop/redact")
async def gdpr_shop_redact(request: Request):
    """
    Pasaron 48 hs desde que un comercio desinstaló la app: Shopify pide
    que borremos todo lo suyo.
    """
    if not _topic_exacto(request, "shop/redact"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    from servicios.shopify_app import (
        cliente_app_instalada, cliente_app_para_webhook,
    )
    app_client_id = cliente_app_para_webhook(cuerpo, firma)
    if not app_client_id:
        return JSONResponse({"ok": False}, status_code=401)

    try:
        datos, dominio = _payload_gdpr_y_dominio(
            cuerpo, request.headers.get("x-shopify-shop-domain", ""),
            "shop/redact",
        )
        shop_id = str(datos.get("shop_id") or "").strip()
        if not shop_id:
            raise ValueError("shop_id faltante")
    except (ValueError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        if confirmar_shop_redact(dominio, shop_id, app_client_id):
            # app/uninstalled ya purgó de forma atómica. El ACK se ata a esa
            # generación histórica y una reinstalación queda fuera del UPDATE.
            print("[gdpr] shop_redact confirmado sobre tombstone purgado")
            return {"ok": True}

        app_actual = cliente_app_instalada(dominio)
        if app_actual:
            # Sin tombstone no existe evidencia que permita atribuir este
            # evento a la instalación activa (menos aún si es la misma app).
            # Fail closed: no se toca token, mapping ni datos nuevos. El 200
            # sólo se entrega después de persistir la obligación operativa.
            from servicios.shopify_app import registrar_shop_redact_pendiente
            if not registrar_shop_redact_pendiente(
                dominio, shop_id, app_client_id,
            ):
                return JSONResponse({"ok": False}, status_code=503)
            print("[gdpr] shop_redact pendiente de verificar generación")
            return {"ok": True, "estado": "VERIFICAR_GENERACION"}

        # Compatibilidad con instalaciones históricas previas al tombstone:
        # sólo se purga por dominio cuando ya no existe NINGÚN token activo.
        from servicios.integraciones_tienda import borrar_datos_tienda
        n = borrar_datos_tienda(dominio)
        print(f"[gdpr] shop_redact legado: {n} registro(s) procesados")
    except Exception as e:
        # El error de DB puede incluir parámetros del payload; registrar sólo
        # su clase evita copiar PII del comercio o comprador a los logs.
        print(f"[gdpr] error procesando shop/redact: {type(e).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    return {"ok": True}


@router.post("/webhook/desinstalada")
async def desinstalada(request: Request):
    """Purga la generación que Shopify identificó en el body firmado."""
    from servicios.shopify_app import cliente_app_para_webhook

    if not _topic_exacto(request, "app/uninstalled"):
        return JSONResponse({"ok": False}, status_code=400)
    cuerpo = await request.body()
    shop_header = request.headers.get("x-shopify-shop-domain", "")
    firma = request.headers.get("x-shopify-hmac-sha256", "")
    app_client_id = cliente_app_para_webhook(cuerpo, firma)
    if not app_client_id:
        return JSONResponse({"ok": False}, status_code=401)

    try:
        import json as _json
        datos = _json.loads(cuerpo.decode("utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("payload inválido")
        # Contrato exclusivo app/uninstalled. El HMAC no firma la URL ni el
        # topic: aceptar aliases GDPR permitiría relabelar shop/redact y usarlo
        # para purgar una instalación activa.
        if any(
            clave in datos
            for clave in (
                "shop_id", "shop_domain", "customer",
                "orders_requested", "orders_to_redact",
            )
        ):
            raise ValueError("contrato app/uninstalled inválido")
        shop = str(datos.get("myshopify_domain") or "").strip().lower()
        shop_id = str(datos.get("id") or "").strip()
        if (
            not dominio_valido(shop)
            or shop != str(shop_header or "").strip().lower()
            or not shop_id
        ):
            raise ValueError("identidad de tienda inconsistente")
    except (ValueError, TypeError, UnicodeError):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        borrada = desinstalar(
            shop,
            app_client_id,
            shop_id,
            request.headers.get("x-shopify-triggered-at", ""),
        )
    except Exception as exc:
        print(f"[shopify] error procesando uninstall: {type(exc).__name__}")
        return JSONResponse({"ok": False}, status_code=503)
    print("[shopify] app/uninstalled procesado · "
          f"{'generación purgada' if borrada else 'evento antiguo/duplicado preservado'}")
    return {"ok": True}
