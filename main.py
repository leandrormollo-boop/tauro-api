from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
import os
from dotenv import load_dotenv
from typing import Optional

from servicios.carriers import cotizar_carriers
from core.email_sender import enviar_email_pedido
from core.database import init_db
from endpoints.portal_cliente import router as portal_router
from endpoints.admin import router as admin_router
from endpoints.integraciones import router as integraciones_router
from endpoints.shopify import router as shopify_router
from servicios.api_b2b import (
    obtener_cliente_por_api_key,
    obtener_precio_envio,
    obtener_datos_producto,
)
from servicios.solicitudes_guia import crear_solicitud_guia

load_dotenv()

# Documentación interactiva SÓLO fuera de producción. Estaba abierta al
# público: /docs le daba a cualquiera el mapa completo de la API — rutas de
# admin, de webhooks, y la forma exacta de cada payload. No es un secreto
# criptográfico, pero es regalarle el trabajo de reconocimiento a un atacante.
_ES_PROD = os.getenv("ENV", "").strip().upper() != "DEV"

app = FastAPI(
    title="Tauro Solutions API",
    description="API de cotización y gestión de envíos internacionales para eCommerce argentino.",
    version="1.0.0",
    docs_url=None if _ES_PROD else "/docs",
    redoc_url=None if _ES_PROD else "/redoc",
    openapi_url=None if _ES_PROD else "/openapi.json",
)

# CORS: sólo los endpoints públicos necesitan cross-origin (el cotizador
# puede embeberse). El portal y el admin viajan same-origin, así que abrir
# "*" no les sumaba nada y ampliaba la superficie. Sin allow_credentials
# (default False) las cookies nunca viajan cross-origin — eso ya estaba bien
# y se mantiene explícito para que nadie lo "arregle" a futuro.
_ORIGENES = [o.strip() for o in os.getenv(
    "CORS_ORIGINS",
    "https://taurosolutions.ar,https://www.taurosolutions.ar",
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGENES,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── Hosts permitidos ────────────────────────────────────────
# Un Host falsificado envenena todo lo que se construye con la URL del
# request (links absolutos, redirects). Se aceptan los dominios de TAURO,
# el dominio de Railway y localhost para desarrollo. EXTRA_HOSTS suma más
# sin tocar código. /health y /salud quedan exentos: los monitores y el
# healthcheck de Railway pegan con hosts internos y no deben caerse nunca.
_HOSTS_OK = {
    "taurosolutions.ar", "www.taurosolutions.ar",
    "localhost", "127.0.0.1", "testserver",
}
_HOSTS_OK.update(h.strip().lower() for h in os.getenv("EXTRA_HOSTS", "").split(",") if h.strip())
_railway = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().lower()
if _railway:
    _HOSTS_OK.add(_railway)


def _host_permitido(host: str) -> bool:
    host = (host or "").split(":")[0].strip().lower()
    # NO se acepta el sufijo `.up.railway.app`: es un namespace compartido
    # (cualquiera tiene su `loquesea.up.railway.app`) y confiar en él dejaba
    # envenenar el Host. El dominio propio de Railway entra por su valor
    # EXACTO, que Railway inyecta en RAILWAY_PUBLIC_DOMAIN (ya en _HOSTS_OK).
    # `.railway.internal` sí: es la red privada, no llega desde afuera.
    return host in _HOSTS_OK or host.endswith(".railway.internal")


# Tope de tamaño por request. Los forms y JSON son chicos (2 MB de sobra);
# las subidas de archivos (comprobantes, facturas, guías: hasta 8 MB en el
# handler) llegan como multipart/form-data y se les da 12 MB. Se decide por
# Content-Type, NO por lista de rutas: cualquier endpoint de subida —del
# portal o del admin— hereda el límite ancho sin mantener una lista frágil
# que se olvida de una ruta y corta un PDF legítimo con 413 (pasó: la lista
# sólo tenía /portal/pagos y rompía las 3 subidas del admin).
# /shopify aparte: sus webhooks (venta con muchos ítems) no pueden caerse por
# tamaño y ya se validan por HMAC.
_MAX_BODY = 2 * 1024 * 1024            # 2 MB general
_MAX_BODY_UPLOAD = 12 * 1024 * 1024    # margen sobre los 8 MB de archivo
_RUTAS_UPLOAD = ("/shopify",)


@app.middleware("http")
async def headers_de_seguridad(request: Request, call_next):
    """
    Guardas de entrada + headers de seguridad + CSP (03/08/2026).

    La CSP fue posible recién cuando la web pública dejó de compilar JSX en
    el navegador (ahora carga JS pre-compilado desde /static — ver
    scripts/build_web.sh). Diseño, basado en el inventario completo de las
    superficies:

    - SCRIPTS: candado. `script-src 'self' 'nonce-…'` — sólo corren los
      archivos propios y los bloques inline que llevan el nonce de ESTE
      request. Un atacante que logre inyectar HTML no puede ejecutar nada.
      No hay eval ni handlers inline (se migraron todos a data-attrs).
    - ESTILOS: pragmático. Los templates tienen ~280 atributos style= y
      React inyecta <style> en runtime → 'unsafe-inline'. Inyectar CSS no
      ejecuta código; el riesgo que importa es el script, y ése está cerrado.
    - Google Fonts es el ÚNICO tercero permitido (hoja CSS + woff2).

    X-Frame-Options y esta CSP NO tocan /shopify/*: esas páginas viven en un
    iframe del admin de Shopify y mandan su propio `frame-ancestors` dinámico
    por tienda (endpoints/shopify.py). El setdefault respeta ese header;
    pisarlo = pantalla en blanco para el comerciante y cadena de ventas rota.
    """
    import secrets as _secrets
    path = request.scope.get("path", "")

    # 1) Host: si no es nuestro, 421 y listo — salvo los healthchecks.
    if path not in ("/health", "/salud") and not _host_permitido(request.headers.get("host", "")):
        return JSONResponse({"detail": "Host no permitido"}, status_code=421)

    # 2) Tamaño: el Content-Length se chequea ANTES de leer el cuerpo.
    #    (Un body chunked sin Content-Length lo frena igual el límite del
    #    proxy de Railway; esto corta el 99% de los abusos gratis.)
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            largo = int(request.headers.get("content-length") or 0)
        except ValueError:
            largo = 0
        es_upload = (path.startswith(_RUTAS_UPLOAD)
                     or "multipart/form-data" in (request.headers.get("content-type") or "").lower())
        tope = _MAX_BODY_UPLOAD if es_upload else _MAX_BODY
        if largo > tope:
            return JSONResponse({"detail": "El archivo o el pedido es demasiado grande."},
                                status_code=413)

    # 3) CSRF en el portal y el admin: un POST que un navegador manda desde
    #    OTRO sitio llega con Origin/Sec-Fetch-Site delatores. La cookie ya
    #    es SameSite=Lax; esto es la segunda tranca (defensa en profundidad).
    #    Los clientes no-navegador (tests, scripts) no mandan estos headers
    #    y pasan. /shopify queda afuera: sus webhooks se validan por HMAC.
    if request.method == "POST" and path.startswith(("/portal", "/admin")):
        def _rechazo_csrf():
            try:
                from servicios.auditoria import registrar_desde_request
                actor = "admin" if path.startswith("/admin") else "cliente"
                registrar_desde_request(request, event=f"{actor}.csrf_rejected",
                                        actor_type=actor, success=False, status_code=403)
            except Exception:
                pass
            return JSONResponse({"detail": "Origen del pedido no permitido."}, status_code=403)

        sfs = (request.headers.get("sec-fetch-site") or "").lower()
        if sfs and sfs not in ("same-origin", "same-site", "none"):
            return _rechazo_csrf()
        origin = (request.headers.get("origin") or "").strip().rstrip("/")
        if origin and origin.lower() != "null":
            host_origin = origin.split("://", 1)[-1]
            if not _host_permitido(host_origin):
                return _rechazo_csrf()

    nonce = _secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce

    response = await call_next(request)

    if not path.startswith("/shopify") and not path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-src 'none'"
        )

    # HSTS: Railway ya sirve por HTTPS; esto impide el downgrade a HTTP.
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Nada de adivinar tipos: un .txt subido no se ejecuta como script.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # No filtrar la URL completa (con query) a sitios externos.
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")

    if not path.startswith("/shopify"):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")

    # Las páginas privadas no se guardan en disco. Sin esto, el HTML del
    # portal —saldo, envíos, direcciones de los compradores— queda en la
    # caché del navegador después de cerrar sesión: el que agarra esa
    # computadora aprieta "atrás" y lo lee sin loguearse. Y si algún día
    # hay un proxy o CDN adelante, tampoco lo guarda ni se lo sirve a otro.
    if path.startswith(("/portal", "/admin")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        # Que Google ni loco indexe páginas privadas (aunque estén tras login,
        # el título "Cuenta corriente · MELCIOR" en resultados ya es una fuga).
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")

    return response

# Inicializar base de datos PostgreSQL al arrancar
try:
    init_db()
except Exception as _db_err:
    print(f"[startup] DB init error: {_db_err}")

# Migrar api_key → api_key_hash UNA vez, en el arranque y no en el primer
# request. La migración hace ALTER TABLE (lock exclusivo sobre `clientes`)
# seguido de los UPDATE: hacerlo en el request-path serializaba cualquier
# lectura de clientes detrás de ese lock. Sigue siendo idempotente, así que
# queda como red si el arranque no llegó a correrla.
try:
    from servicios.api_b2b import _ensure_hash_migrado
    _ensure_hash_migrado()
except Exception as _mig_err:
    print(f"[startup] migración de api_key diferida al primer uso: {_mig_err}")

# Static files (CSS, JS, imágenes), portal del cliente y admin
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(portal_router)
app.include_router(admin_router)
app.include_router(integraciones_router)
app.include_router(shopify_router)

WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))


@app.middleware("http")
async def revalidar_assets_web(request: Request, call_next):
    """
    El HTML y el CSS de la web pública NO tienen versión en la URL, así que
    el browser los cacheaba de más y la gente seguía viendo la versión vieja
    tras un deploy. Con `no-cache` el navegador revalida cada vez: el ETag
    hace que si no cambió devuelva 304 (barato), y si cambió trae lo nuevo
    al instante. El JS compilado va con ?v=N, así que no necesita esto.
    """
    response = await call_next(request)
    path = request.url.path
    if path in ("/web", "/styles.css"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/web", include_in_schema=False)
def servir_web():
    return FileResponse(os.path.join(WEB_DIR, "Tauro Solutions.html"))

# Los .jsx ya NO se sirven: la web carga el JS compilado desde /static/js/web
# (ver scripts/build_web.sh). Los fuentes quedan en web/components/ como
# fuente de verdad, pero exponerlos era regalar el código sin minificar.

@app.get("/styles.css", include_in_schema=False)
def servir_css():
    return FileResponse(os.path.join(WEB_DIR, "styles.css"))


class LeadCotizacionRequest(BaseModel):
    email: str = Field(..., max_length=160)
    destino: str = Field("", max_length=4)
    peso_kg: float = Field(0, ge=0, le=100)
    carriers: list = Field(default_factory=list)


@app.post("/cotizacion-lead", tags=["public"])
def cotizacion_lead(body: LeadCotizacionRequest, request: Request):
    """
    "Recibí esta cotización por mail": captura el contacto DESPUÉS de mostrar
    el precio — el cotizador sigue gratis y sin login, ese diferencial no se
    toca. Rate limit porque es público y manda mails.
    """
    from servicios.leads import guardar_lead
    from servicios.rate_limit import check_rate, client_ip

    if not check_rate(f"lead:{client_ip(request)}", max_attempts=5, window_seconds=900):
        return JSONResponse({"ok": False, "error": "Demasiados pedidos. Probá en unos minutos."},
                            status_code=429)
    try:
        return guardar_lead(body.email, body.destino, body.peso_kg, body.carriers)
    except Exception as e:
        print(f"[leads] error guardando lead: {e}")
        return JSONResponse({"ok": False, "error": "No pudimos guardar tu email. Probá de nuevo."},
                            status_code=200)


@app.get("/guias", include_in_schema=False)
def servir_guias_indice():
    """Guías por país — contenido SEO: lo que la pyme googlea antes de exportar."""
    from servicios.guias_pais import pagina_indice
    return HTMLResponse(pagina_indice())


@app.get("/guias/{slug}", include_in_schema=False)
def servir_guia_pais(slug: str):
    from servicios.guias_pais import pagina_guia
    html = pagina_guia(slug)
    if html is None:
        return RedirectResponse(url="/guias", status_code=303)
    return HTMLResponse(html)


@app.get("/calculadora-volumetrica", include_in_schema=False)
def servir_calculadora():
    """Herramienta gratis: imán SEO que termina en el cotizador."""
    from servicios.guias_pais import pagina_calculadora
    return HTMLResponse(pagina_calculadora())


@app.get("/sitemap.xml", include_in_schema=False)
def servir_sitemap():
    """Para que Google indexe las guías sin esperar a que alguien las linkee."""
    from servicios.guias_pais import sitemap_xml
    return Response(content=sitemap_xml(), media_type="application/xml")


@app.get("/estado", include_in_schema=False)
def servir_estado():
    """Página de estado pública: 'no le tenemos miedo a que nos midan'."""
    from servicios.estado_publico import pagina_estado
    return HTMLResponse(pagina_estado())


@app.get("/privacidad", include_in_schema=False)
def servir_privacidad():
    """Política de privacidad — Shopify la exige para publicar la app."""
    from servicios.paginas_legales import pagina_privacidad
    return HTMLResponse(pagina_privacidad())


@app.get("/terminos", include_in_schema=False)
def servir_terminos():
    from servicios.paginas_legales import pagina_terminos
    return HTMLResponse(pagina_terminos())


@app.get("/", include_in_schema=False)
def root():
    """Redirige el root a la web pública."""
    return RedirectResponse(url="/web")

# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

class CotizarRequest(BaseModel):
    producto_id: str
    destino_pais: str


class PedidoRequest(BaseModel):
    producto_id: str
    destino_pais: str
    nombre_comprador: str
    direccion_exacta: str
    ciudad: str
    estado: str
    zip_code: str
    pais: str
    telefono: str
    email_comprador: str
    precio_cliente_final_ars: Optional[float] = None


class CotizarWebRequest(BaseModel):
    destino_pais: str = Field(..., description="ISO-2 del país del exterior: US, BR, CL, UY, MX, ES")
    peso_kg: float = Field(..., gt=0, le=70)
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)
    valor_declarado_usd: float = Field(default=100.0, gt=0)
    # TAURO opera los dos sentidos. El campo es opcional y cae en exportación
    # para no romper a nadie que ya esté llamando este endpoint sin él.
    # Ojo: `destino_pais` es SIEMPRE el país del exterior; en una importación
    # ese país es el ORIGEN y el destino es Argentina.
    sentido: str = Field(default="exportacion", description="exportacion | importacion")


# ─────────────────────────────────────────────
# AUTH HELPER
# ─────────────────────────────────────────────

def autenticar(api_key: str) -> dict:
    """Valida la API Key y retorna el perfil del cliente o lanza 403."""
    if not api_key:
        raise HTTPException(status_code=403, detail="X-API-Key header requerido.")
    perfil = obtener_cliente_por_api_key(api_key)
    if not perfil.get("encontrado"):
        raise HTTPException(status_code=403, detail="API Key inválida.")
    return perfil


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/health", include_in_schema=True, tags=["health"])
def health_check():
    """
    Chequeo LIVIANO: ¿el proceso responde? Lo usa el healthcheck de Railway
    para gatear deploys (un deploy que no bootea nunca reemplaza al sano).
    A propósito NO toca la DB: un parpadeo de Postgres no debe hacer que
    Railway reinicie la app en loop.
    """
    return {"status": "ok", "service": "tauro-api", "version": "1.0.0"}


@app.get("/salud", include_in_schema=True, tags=["health"])
def salud_check():
    """
    Chequeo PROFUNDO para monitores externos (UptimeRobot, etc.):
    app viva + base de datos respondiendo. 503 si la DB no contesta,
    para que el monitor avise aunque la web siga sirviendo HTML.
    """
    from core.database import get_conn
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
        return {"status": "ok", "db": "ok", "service": "tauro-api"}
    except Exception as e:
        return JSONResponse(
            {"status": "degraded", "db": f"error: {type(e).__name__}", "service": "tauro-api"},
            status_code=503,
        )


# ── Red de seguridad global: un error inesperado nunca muestra un 500
# pelado al cliente. Página con la marca para las superficies HTML,
# JSON para las APIs. El traceback queda en los logs de Railway.
_HTML_ERROR_500 = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Ups · Tauro Solutions</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0c0a14;color:#f4f5f7;font-family:'Helvetica Neue',system-ui,sans-serif;text-align:center;}
.box{max-width:440px;padding:40px 28px;}
h1{font-size:44px;margin:0 0 10px;}p{color:#b9bfc7;line-height:1.6;margin:0 0 26px;}
a{display:inline-block;padding:12px 26px;background:#a78bfa;color:#fff;border-radius:8px;
text-decoration:none;font-weight:600;}
small{display:block;margin-top:22px;color:#7a828c;font-size:12px;}
</style></head><body><div class="box">
<h1>Ups, algo salió mal</h1>
<p>Tuvimos un problema procesando tu pedido. Ya quedó registrado
y lo estamos mirando. Probá de nuevo en un momento.</p>
<a href="/portal/home">Volver al portal</a>
<small>Tauro Solutions · si sigue pasando, escribinos.</small>
</div></body></html>"""


@app.exception_handler(Exception)
async def error_global(request: Request, exc: Exception):
    import traceback
    print(f"[500] {request.method} {request.url.path} → {type(exc).__name__}: {exc}")
    traceback.print_exc()
    acepta_html = "text/html" in (request.headers.get("accept") or "")
    if acepta_html and request.url.path.startswith(("/portal", "/admin", "/web")):
        return HTMLResponse(_HTML_ERROR_500, status_code=500)
    return JSONResponse(
        {"ok": False, "error": "Error interno. Ya quedó registrado, probá de nuevo."},
        status_code=500,
    )


@app.get("/partners", tags=["public"])
def partners_activos():
    """
    Los couriers habilitados hoy, para la barra de partners de la web pública.
    Sale de las credenciales cargadas, así que la web nunca promete un partner
    que no puede cotizar.
    """
    from servicios.carriers import carriers_activos
    return {"partners": carriers_activos()}


@app.post("/cotizar-web", tags=["public"])
def cotizar_web(body: CotizarWebRequest, request: Request):
    """
    Cotización pública para taurosolutions.ar — sin auth.
    Llama directo a FedEx y aplica el markup web de Tauro.

    RATE LIMIT (03/08): cada request cotiza EN VIVO contra los couriers —
    tarda segundos, ocupa un hilo del pool y consume cuota real. Sin límite,
    cualquiera abría 40 conexiones en paralelo y dejaba sin hilos al portal,
    al admin y a los webhooks: el sitio caído, gratis y desde el anonimato.
    El tope es generoso a propósito: comparar 4-5 destinos es uso normal.
    """
    from servicios.rate_limit import check_rate, client_ip

    if not check_rate(f"cotweb:{client_ip(request)}", max_attempts=30,
                      window_seconds=300):
        return JSONResponse(
            {"status": "error",
             "detail": "Estás cotizando muy seguido. Esperá un minuto y "
                       "volvé a probar."},
            status_code=429,
        )
    DESTINOS = {
        "US": {"city": "MIAMI",      "state": "FL", "postal_code": "33101"},
        "BR": {"city": "SAO PAULO",  "state": "SP", "postal_code": "01310100"},
        "CL": {"city": "SANTIAGO",   "state": "RM", "postal_code": "8320000"},
        "UY": {"city": "MONTEVIDEO", "state": "MO", "postal_code": "11000"},
        "MX": {"city": "MEXICO",     "state": "DF", "postal_code": "06600"},
        "ES": {"city": "MADRID",     "state": "M",  "postal_code": "28001"},
    }

    pais_exterior = body.destino_pais.upper()
    info_exterior = DESTINOS.get(pais_exterior)
    if not info_exterior:
        raise HTTPException(status_code=400, detail=f"País '{body.destino_pais}' no soportado aún.")

    ARGENTINA = {
        "street": "Av. Corrientes 1234",
        "city": "BUENOS AIRES",
        "state": "B",
        "postal_code": "1043",
        "country": "AR",
    }
    exterior = {**info_exterior, "street": "Main St 100", "country": pais_exterior}

    # En una importación la caja viaja al revés: sale del exterior y entra a
    # Argentina. No es sólo cosmético — cada courier cotiza distinto según el
    # sentido y DHL directamente factura contra otra cuenta (ver dhl_client).
    importacion = (body.sentido or "").lower().startswith("impo")
    origen, destino = (exterior, ARGENTINA) if importacion else (ARGENTINA, exterior)
    paquete = {
        "peso_kg": body.peso_kg,
        "largo": body.largo_cm,
        "ancho": body.ancho_cm,
        "alto": body.alto_cm,
        "valor_declarado_usd": body.valor_declarado_usd,
        "descripcion_en": "Merchandise",
    }

    from servicios.cotizador import dolar_ars
    dolar = dolar_ars()          # tabla `config`, la misma que usa el portal
    markup_pct = float(os.getenv("WEB_MARKUP_PCT", "20"))

    # Compara FedEx, UPS y DHL. Cada carrier cotiza si tiene credenciales;
    # si no, sale con su logo en "próximamente". Ver servicios/carriers.py.
    carriers = cotizar_carriers(origen, destino, paquete, dolar, markup_pct)

    cotizados = [c for c in carriers if c["estado"] == "cotizado"]
    if not cotizados:
        raise HTTPException(
            status_code=502,
            detail="No se pudo obtener tarifas en este momento. Probá de nuevo en un momento.",
        )

    # Recomendado = el más barato de los que cotizaron.
    recomendado = min(cotizados, key=lambda c: c["precio_usd"])["id"]

    return {
        "status": "success",
        "sentido": "importacion" if importacion else "exportacion",
        "origen": f"{exterior['city'].title()}, {pais_exterior}" if importacion else "Buenos Aires, AR",
        "destino": "AR" if importacion else pais_exterior,
        "peso_kg": body.peso_kg,
        "recomendado": recomendado,
        "carriers": carriers,
    }


@app.post("/cotizar")
def cotizar(body: CotizarRequest, x_api_key: str = Header(default=None)):
    """
    Retorna el precio de envío para un producto hacia un destino.
    El precio USD se calcula dividiendo el ARS por el tipo de cambio oficial (no multiplicado).
    """
    perfil = autenticar(x_api_key)
    cliente_id = perfil["cliente_id"]

    resultado = obtener_precio_envio(cliente_id, body.producto_id, body.destino_pais)

    if not resultado.get("encontrado"):
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró precio para producto '{body.producto_id}' hacia '{body.destino_pais}'."
        )

    # ⚠️ NUNCA agregar acá una clave que empiece con costo_, margen_ o markup_.
    # Este endpoint lo llama el CLIENTE con su propia API key. Hasta hoy devolvía
    # markup_tipo / markup_valor / markup_pct_equivalente, o sea que le estábamos
    # diciendo cuánto le ganamos: con FIJO_ARS el costo salía de una resta
    # (costo = precio_ars − markup_valor) y con PCT de una división. El
    # diccionario que llega de api_b2b trae además costo_fedex_ars y margen_ars,
    # así que las claves se listan UNA POR UNA a propósito — un `{**resultado}`
    # acá filtraría el costo entero. Hay un test que lo vigila:
    # tests/test_no_fuga_costo.py
    return {
        "status": "success",
        "producto_id": body.producto_id,
        "destino_pais": body.destino_pais,
        "ruta_id": resultado["ruta_id"],
        "coti_id": resultado["coti_id"],
        "precio_ars": resultado["precio_ars"],
        "precio_usd": resultado["precio_usd"],
        "tipo_cambio_usado": resultado["tipo_cambio_usado"],
        "dias_estimados": resultado["dias_estimados"],
        "valida_hasta": resultado["valida_hasta"],
    }


@app.post("/pedido")
def registrar_pedido(body: PedidoRequest, x_api_key: str = Header(default=None)):
    """
    Registra un pedido confirmado.
    Combina los datos del comprador con el perfil del cliente (remitente) 
    y genera el PDF de armado de guía para logística.
    """
    perfil = autenticar(x_api_key)
    cliente_id = perfil["cliente_id"]

    # Validar que el precio exista
    precio = obtener_precio_envio(cliente_id, body.producto_id, body.destino_pais)
    if not precio.get("encontrado"):
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró precio para producto '{body.producto_id}' hacia '{body.destino_pais}'."
        )

    # Obtener datos aduanales del producto
    producto = obtener_datos_producto(cliente_id, body.producto_id)
    if not producto.get("encontrado"):
        raise HTTPException(
            status_code=404,
            detail=f"Producto '{body.producto_id}' no encontrado en catálogo."
        )

    # Construir datos completos del pedido para el PDF
    datos_pedido = {
        # Remitente (del perfil del cliente en PostgreSQL)
        "remitente_nombre": perfil["nombre"],
        "remitente_cuit": perfil["cuit"],
        "remitente_direccion": perfil["direccion"],
        "remitente_cp": perfil["cp"],
        "remitente_ciudad": perfil["ciudad"],
        "remitente_pais": perfil["pais"],
        "remitente_telefono": perfil["telefono"],
        "remitente_email": perfil["email"],

        # Destinatario (del body del request)
        "dest_nombre": body.nombre_comprador,
        "dest_direccion": body.direccion_exacta,
        "dest_ciudad": body.ciudad,
        "dest_estado": body.estado,
        "dest_zip": body.zip_code,
        "dest_pais": body.pais,
        "dest_telefono": body.telefono,
        "dest_email": body.email_comprador,

        # Producto / Aduana (del catálogo en PostgreSQL)
        "producto_nombre_es": producto["nombre_es"],
        "producto_nombre_en": producto["nombre_en"],
        "producto_hs_code": producto["hs_code"],
        "producto_valor_usd": producto["valor_usd"],
        "producto_unidades": producto["unidades"],
        "producto_peso_kg": producto["peso_kg"],
        "producto_largo": producto["largo"],
        "producto_ancho": producto["ancho"],
        "producto_alto": producto["alto"],

        # Financiero
        "precio_cobrado_ars": precio["precio_ars"],
        "precio_cobrado_usd": precio["precio_usd"],
        "tipo_cambio": precio["tipo_cambio_usado"],
        "costo_fedex_ars": precio.get("costo_fedex_ars", 0),
        "margen_ars": precio.get("margen_ars", 0),
    }

    # Generar referencia única
    from datetime import datetime
    fecha = datetime.now().strftime("%Y%m%d-%H%M")
    referencia = f"{cliente_id}-{body.producto_id}-{fecha}"
    datos_pedido["referencia"] = referencia

    # Enviar PDF por email a logística
    ok = enviar_email_pedido(datos_pedido)
    if not ok:
        raise HTTPException(status_code=500, detail="Error al generar o enviar el PDF de pedido.")

    solicitud = crear_solicitud_guia(
        cliente_id=cliente_id,
        producto_alias=producto["nombre_es"],
        cantidad=int(producto["unidades"] or 1),
        destino_pais=body.destino_pais,
        dest_nombre=body.nombre_comprador,
        dest_documento="",
        dest_email=body.email_comprador,
        dest_telefono=body.telefono,
        dest_direccion=body.direccion_exacta,
        dest_ciudad=body.ciudad,
        dest_estado=body.estado,
        dest_zip=body.zip_code,
        observaciones=f"Pedido API {referencia}",
        peso_kg=producto["peso_kg"],
        largo_cm=producto["largo"],
        ancho_cm=producto["ancho"],
        alto_cm=producto["alto"],
        valor_declarado_usd=producto["valor_usd"],
        ruta_id=precio["ruta_id"],
        coti_id=precio["coti_id"],
        precio_tauro_ars=precio["precio_ars"],
        precio_tauro_usd=precio["precio_usd"],
        precio_cliente_final_ars=body.precio_cliente_final_ars,
    )

    return {
        "status": "success",
        "mensaje": "Pedido recibido. PDF enviado a logística y solicitud creada.",
        "referencia": referencia,
        "solicitud_id": solicitud["id"],
    }


# ─────────────────────────────────────────────
# JOB SEMANAL — ACTUALIZACIÓN DE PRECIOS FEDEX
# ─────────────────────────────────────────────

def job_actualizar_precios_fedex():
    """
    Job legado. La API B2B ahora cotiza en vivo contra FedEx y guarda el log
    en PostgreSQL, así que ya no actualiza la hoja COTI de Google Sheets.
    """
    print("[job] Saltado: cotización en vivo con PostgreSQL activa.")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

CRON_DIA = os.getenv("CRON_DIA", "monday")
CRON_HORA = int(os.getenv("CRON_HORA", 6))

scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
scheduler.add_job(
    job_actualizar_precios_fedex,
    trigger="cron",
    day_of_week=CRON_DIA,
    hour=CRON_HORA,
    minute=0,
)

# Job diario: limpiar sesiones expiradas a las 3am
from jobs.limpiar_sessions import limpiar_sessions_expiradas
scheduler.add_job(
    limpiar_sessions_expiradas,
    trigger="cron",
    hour=3,
    minute=0,
)


# Job diario: podar el registro de auditoría (retención configurable, 1 año
# por default) para que la tabla no crezca sin fin.
def job_limpiar_auditoria():
    try:
        from servicios.auditoria import limpiar_auditoria_antigua
        borrados = limpiar_auditoria_antigua()
        if borrados:
            print(f"[scheduler] auditoría: {borrados} eventos viejos podados")
    except Exception as e:
        print(f"[scheduler] poda de auditoría falló: {e}")


scheduler.add_job(job_limpiar_auditoria, trigger="cron", hour=3, minute=30)


def job_refrescar_tarifas():
    """
    Deja las tarifas del checkout frescas. Corre de madrugada porque
    tarda (son ~66 cotizaciones) y ahí una demora no le cuesta una venta
    a nadie: durante el día el checkout lee esta tabla en milisegundos.
    """
    try:
        from servicios.tarifas_cache import refrescar_cache
        refrescar_cache()
    except Exception as e:
        print(f"[scheduler] refresco de tarifas falló: {e}")


scheduler.add_job(
    job_refrescar_tarifas,
    trigger="cron",
    hour=4,
    minute=0,
)

# Centinela: cada 15 min se pregunta si un comprador vería su opción de
# envío en este momento. Dos fallos seguidos → mail de alerta a Tauro.
from jobs.centinela_checkout import revisar_checkout
scheduler.add_job(
    revisar_checkout,
    trigger="interval",
    minutes=15,
)

# Espejo en el Google Sheet TAURO 2026 (pestaña PLATAFORMA, propiedad del
# sistema — las pestañas manuales no se tocan). Sólo corre si
# GOOGLE_CREDENTIALS_JSON está cargada; cada 30 min alcanza para control.
from jobs.sync_sheet_tauro import sincronizar_seguro, configurado as _sheet_conf
if _sheet_conf():
    scheduler.add_job(sincronizar_seguro, trigger="interval", minutes=30)
    print("[scheduler] Espejo en Google Sheet: cada 30 min (pestaña PLATAFORMA)")
else:
    print("[scheduler] Espejo en Google Sheet APAGADO (falta GOOGLE_CREDENTIALS_JSON)")

scheduler.start()


def _tarifas_al_arrancar():
    """
    Auto-sanación: si la tabla de tarifas está vacía (primer deploy, base
    nueva, o alguien la borró), la llena sola en segundo plano en vez de
    esperar al job de las 4am. Sin esto, el checkout depende de cotizar en
    vivo — que es justo lo que queremos evitar.
    """
    try:
        from servicios.tarifas_cache import estado_cache, refrescar_cache
        estado = estado_cache()
        if estado.get("tarifas", 0) > 0:
            print(f"[startup] tarifas del checkout: {estado['tarifas']} cargadas")
            return
        print("[startup] tabla de tarifas vacía → llenando en segundo plano")
        refrescar_cache()
    except Exception as e:
        print(f"[startup] no pude precargar tarifas: {e}")


# En un hilo aparte: el arranque no puede esperar ~66 cotizaciones, y
# Railway mata el deploy si el healthcheck no responde a tiempo.
import threading
threading.Thread(target=_tarifas_al_arrancar, daemon=True).start()

print(f"[scheduler] Job semanal precios FedEx: {CRON_DIA} {CRON_HORA}:00 (Argentina)")
print(f"[scheduler] Job diario limpiar_sessions: 3:00 (Argentina)")
print(f"[scheduler] Job diario tarifas del checkout: 4:00 (Argentina)")
print(f"[scheduler] Centinela del checkout: cada 15 min")
