# ============================================================
# Endpoints públicos de la app de Shopify
# ============================================================
#   GET  /shopify/app        → App Home embebida (shell sin PII)
#   GET  /shopify/app/data   → estado/pedidos autenticados por ID token
#   GET  /shopify/install    → arranca la instalación (OAuth)
#   GET  /shopify/callback   → Shopify vuelve acá con el permiso dado
#   POST /shopify/webhook/desinstalada → limpieza al desinstalar
# ============================================================
from __future__ import annotations

import html
import json
import secrets
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from servicios.shopify_app import (
    app_configurada, url_instalacion, validar_hmac_query, dominio_valido,
    canjear_token, guardar_instalacion, registrar_webhooks,
    desinstalar, ShopifyWebhookVerificationError,
    confirmar_shop_redact, confirmar_webhooks_verificados,
    webhooks_requeridos, api_key_publica,
)
from servicios.shopify_embedded import (
    APP_BRIDGE_CDN,
    POLARIS_CDN,
    ShopifyEmbeddedAuthError,
    bearer_token,
    crear_estado_oauth,
    host_embebido_para_shop,
    instalacion_para_session,
    shop_desde_host_embebido,
    url_admin_app,
    validar_session_token,
    verificar_estado_oauth,
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
    # Pantallas de error/compatibilidad fuera de App Home no son embebibles.
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


def _app_home_html(api_key: str, nonce: str, reconnect_url: str) -> str:
    """Shell sin datos privados; App Bridge autentica la lectura posterior."""
    api_key = html.escape(api_key, quote=True)
    reconnect_js = json.dumps(reconnect_url)
    return f"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="shopify-api-key" content="{api_key}">
<title>TAURO Solutions</title>
<style nonce="{nonce}">
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#f6f6f7; color:#202223;
          font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:980px; margin:0 auto; padding:24px; }}
  .grid {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(280px,.55fr);
           gap:16px; align-items:start; }}
  .card {{ background:#fff; border:1px solid #e1e3e5; border-radius:12px;
           padding:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  h1,h2 {{ margin:0 0 8px; }} h1 {{ font-size:24px; }} h2 {{ font-size:16px; }}
  p {{ margin:6px 0; color:#616161; }}
  .status {{ display:inline-flex; align-items:center; gap:7px; font-weight:650; }}
  .dot {{ width:9px; height:9px; border-radius:50%; background:#8c9196; }}
  .dot.ok {{ background:#29845a; }} .dot.warn {{ background:#b98900; }}
  table {{ width:100%; border-collapse:collapse; margin-top:14px; }}
  th,td {{ padding:11px 8px; border-top:1px solid #e1e3e5; text-align:left; }}
  th {{ color:#616161; font-size:12px; }}
  .mono {{ font-variant-numeric:tabular-nums; }}
  .button {{ display:inline-block; margin-top:14px; padding:10px 14px;
             border-radius:8px; border:0; background:#303030; color:#fff;
             font-weight:650; cursor:pointer; text-decoration:none; }}
  .hidden {{ display:none; }}
  #message {{ min-height:22px; }}
  @media (max-width:720px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style>
<script nonce="{nonce}" src="{APP_BRIDGE_CDN}"></script>
<script nonce="{nonce}" src="{POLARIS_CDN}"></script>
<script nonce="{nonce}">
  const reconnectUrl = {reconnect_js};
  const byId = (id) => document.getElementById(id);
  const text = (id, value) => {{ byId(id).textContent = String(value ?? ""); }};

  function showReconnect(message) {{
    text("message", message);
    byId("state-dot").className = "dot warn";
    byId("reconnect").classList.remove("hidden");
  }}

  function render(data) {{
    byId("state-dot").className = data.linked ? "dot ok" : "dot warn";
    text("state", data.linked ? "Instalada y vinculada" : "Instalada · falta vincular a TAURO");
    text("shop", data.shop);
    text("message", data.linked
      ? "Los pedidos se sincronizan con el portal TAURO."
      : "Iniciá sesión en el portal TAURO para completar el vínculo.");
    const tbody = byId("orders");
    tbody.replaceChildren();
    for (const order of data.orders) {{
      const row = document.createElement("tr");
      for (const value of [
        order.number,
        order.status,
        order.total ? `${{order.currency || ""}} ${{order.total}}`.trim() : "—",
        order.created_at || "—",
      ]) {{
        const cell = document.createElement("td");
        cell.textContent = value || "—";
        row.appendChild(cell);
      }}
      tbody.appendChild(row);
    }}
    byId("empty").classList.toggle("hidden", data.orders.length > 0);
  }}

  async function load() {{
    try {{
      const token = await shopify.idToken();
      const response = await fetch("/shopify/app/data", {{
        cache: "no-store",
        headers: {{Authorization: `Bearer ${{token}}`}},
      }});
      const data = await response.json();
      if (response.status === 409 && data.code === "REAUTHORIZE") {{
        showReconnect("La instalación necesita autorización nuevamente.");
        return;
      }}
      if (!response.ok) throw new Error(data.detail || "No pudimos validar la sesión.");
      render(data);
    }} catch (_error) {{
      showReconnect("No pudimos validar esta sesión de Shopify. Volvé a abrir la app.");
    }}
  }}

  window.addEventListener("DOMContentLoaded", () => {{
    byId("reconnect").addEventListener("click", () => window.open(reconnectUrl, "_top"));
    load();
  }});
</script>
</head><body>
<ui-title-bar title="TAURO Solutions"></ui-title-bar>
<main>
  <div class="grid">
    <section class="card" aria-labelledby="orders-title">
      <h1 id="orders-title">Pedidos de Shopify</h1>
      <p>Resumen operativo sin datos personales del comprador.</p>
      <p id="empty">No hay pedidos sincronizados para mostrar.</p>
      <table aria-label="Pedidos Shopify">
        <thead><tr><th>Pedido</th><th>Estado</th><th>Total</th><th>Recibido</th></tr></thead>
        <tbody id="orders"></tbody>
      </table>
    </section>
    <aside class="card" aria-labelledby="state-title">
      <h2 id="state-title">Estado de la integración</h2>
      <div class="status"><span id="state-dot" class="dot"></span><span id="state">Validando sesión…</span></div>
      <p id="shop"></p><p id="message">Cargando estado seguro…</p>
      <button id="reconnect" class="button hidden" type="button">Reconectar con Shopify</button>
      <a class="button" href="https://taurosolutions.ar/portal/tienda" target="_blank" rel="noopener noreferrer">Abrir portal TAURO</a>
    </aside>
  </div>
</main>
</body></html>"""


@router.get("/app", response_class=HTMLResponse)
def app_home(request: Request, shop: str = "", host: str = ""):
    """App Home embebida: shell público, datos sólo detrás de ID token."""
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "TAURO todavía no tiene configuradas sus credenciales Shopify.",
            status=503,
        )
    shop = str(shop or "").strip().lower()
    shop_host = shop_desde_host_embebido(host)
    if not dominio_valido(shop):
        shop = shop_host
    elif shop_host and shop_host != shop:
        return _pagina("Contexto inválido", "El host no corresponde a la tienda.", status=400)
    if not dominio_valido(shop):
        return _pagina("Contexto inválido", "Shopify no identificó una tienda válida.", status=400)
    try:
        host = host_embebido_para_shop(shop, host)
    except ValueError:
        return _pagina("Contexto inválido", "El host no corresponde a la tienda.", status=400)
    request_state = getattr(request, "state", None)
    nonce = str(getattr(request_state, "csp_nonce", "") or secrets.token_urlsafe(16))
    reconnect_url = f"/shopify/install?shop={quote(shop)}&host={quote(host)}"
    response = HTMLResponse(_app_home_html(api_key_publica(), nonce, reconnect_url))
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' https://cdn.shopify.com; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; "
        f"frame-ancestors https://{shop} https://admin.shopify.com;"
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/app/data")
def app_home_data(request: Request):
    """Estado y pedidos mínimos, seleccionados exclusivamente por el JWT."""
    payload = None
    try:
        payload = validar_session_token(
            bearer_token(request.headers.get("authorization", "")),
        )
        inst = instalacion_para_session(payload)
    except ShopifyEmbeddedAuthError as exc:
        if exc.code == "REAUTHORIZE" and payload:
            shop = str(payload["shop"])
            return JSONResponse(
                {"detail": "La instalación requiere autorización.", "code": exc.code},
                status_code=409,
            )
        return JSONResponse(
            {"detail": "Sesión Shopify inválida.", "code": exc.code},
            status_code=401,
            headers={"X-Shopify-Retry-Invalid-Session-Request": "1"},
        )

    shop = str(payload["shop"])
    cliente_id = str(inst.get("cliente_id") or "").strip().upper()
    pedidos = []
    if cliente_id:
        try:
            from servicios.integraciones_tienda import listar_resumen_pedidos_shopify_embebido

            filas = listar_resumen_pedidos_shopify_embebido(shop, cliente_id)
            for fila in filas:
                creado = fila.get("created_at")
                pedidos.append({
                    "number": str(fila.get("numero") or fila.get("pedido_externo_id") or ""),
                    "status": str(fila.get("estado") or ""),
                    "total": str(fila["valor_total"]) if fila.get("valor_total") is not None else "",
                    "currency": str(fila.get("moneda") or ""),
                    "created_at": creado.isoformat() if hasattr(creado, "isoformat") else str(creado or ""),
                })
        except Exception as exc:
            print(f"[shopify] app home no pudo leer pedidos: {type(exc).__name__}")
            return JSONResponse(
                {"detail": "No pudimos cargar los pedidos."}, status_code=503,
            )
    return {
        "shop": shop,
        "installed": True,
        "linked": bool(cliente_id),
        "orders": pedidos,
    }


@router.get("/install", response_class=HTMLResponse)
def install(request: Request, shop: str = "", host: str = ""):
    """
    Inicia OAuth en navegación principal y devuelve a App Home embebida.
    """
    if not app_configurada():
        return _pagina(
            "App en preparación",
            "La app de TAURO para Shopify todavía no está disponible. "
            "Cuando quede habilitada, vas a poder instalarla únicamente desde Shopify Apps.",
            '<a href="https://admin.shopify.com">Volver a Shopify</a>',
        )
    shop = (shop or "").strip().lower()
    host = str(host or request.query_params.get("host", "") or "").strip()

    # `host` sólo puede seleccionar la misma tienda codificada por Shopify.
    if not dominio_valido(shop):
        shop = shop_desde_host_embebido(host) or shop

    if not dominio_valido(shop):
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
        return _redirect_oauth(shop, host)

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
        return _redirect_oauth(shop, host)

    if inst and inst.get("access_token"):
        # Una fila histórica puede tener todos los scopes correctos y aun así
        # pertenecer a la app vieja. Con la app pública activa, esa tienda debe
        # pasar una vez por SU OAuth; mostrar el panel legado acá impediría la
        # migración para siempre.
        app_instalada = str(inst.get("app_client_id") or "").strip()
        if app_instalada != api_key_publica():
            print("[shopify] instalación histórica → nuevo consentimiento")
            return _redirect_oauth(shop, host)
        if not inst.get("token_rotativo"):
            print("[shopify] instalación pública sin refresh → nuevo consentimiento")
            return _redirect_oauth(shop, host)
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
            return _redirect_oauth(shop, host)
        return RedirectResponse(
            url=url_admin_app(shop),
            status_code=303,
            headers={"Cache-Control": "private, no-store", "Pragma": "no-cache"},
        )

    return _redirect_oauth(shop, host)


def _redirect_oauth(shop: str, host: str = "") -> RedirectResponse:
    """
    Manda al consentimiento de Shopify guardando el `state` en una cookie
    corta. Hasta ahora el state se generaba, viajaba... y nadie lo comparaba
    a la vuelta — o sea, teatro. El callback ahora exige que coincida
    (anti-CSRF del flujo OAuth, y Shopify lo revisa para el App Store).
    """
    state = crear_estado_oauth(shop, host)
    url = url_instalacion(shop, state)
    resp = RedirectResponse(url=url, status_code=303)
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
    if state_verificado:
        try:
            verificar_estado_oauth(state_query, shop)
        except (ValueError, ShopifyEmbeddedAuthError):
            state_verificado = False
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

    # El destino se deriva del `shop` cubierto por el HMAC de Shopify y por el
    # state firmado; no se acepta una URL de retorno aportada por el navegador.
    respuesta = RedirectResponse(
        url=url_admin_app(shop),
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
