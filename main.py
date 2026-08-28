from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from apscheduler.schedulers.background import BackgroundScheduler
import hashlib
import json
import os
import secrets
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
from servicios.solicitudes_guia import crear_solicitud_guia, IdempotencyConflictError
from servicios.meta_ads import (
    construir_content_security_policy,
    inyectar_meta_pixel,
    javascript_meta_pixel,
    meta_pixel_habilitado,
    obtener_meta_pixel_id,
)
from servicios.numeros_humanos import (
    parse_configuracion_numerica,
    parse_float_formulario,
)


def _decimal_json(valor):
    """Borde JSON común: 5,5 y 5.5 llegan al mismo valor validado."""

    return parse_float_formulario(valor, "Peso o medida")


def _decimal_json_no_negativo(valor):
    return parse_float_formulario(valor, "Peso", minimo=0)


def _importe_json_opcional(valor):
    return parse_float_formulario(
        valor, "Importe", importe=True, requerido=False, minimo=0,
    )


def _importe_json(valor):
    return parse_float_formulario(valor, "Valor declarado", importe=True, minimo=0)

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
    - Google Fonts es el ÚNICO tercero permanente (hoja CSS + woff2). En
      `/web`, y sólo si existe un `META_PIXEL_ID` válido, se habilitan los dos
      orígenes exactos que necesita el Pixel de Meta.

    La app pública de Shopify es externa (`embedded = false`): sus páginas se
    abren como navegación principal y declaran su propia CSP. Nunca deben poder
    incrustarse en un iframe; por eso /shopify recibe X-Frame-Options: DENY.
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
            construir_content_security_policy(
                nonce,
                pixel_habilitado=meta_pixel_habilitado(path),
            ),
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

    if path.startswith("/shopify"):
        response.headers.setdefault("X-Frame-Options", "DENY")
    else:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")

    # Las páginas privadas no se guardan en disco. Sin esto, el HTML del
    # portal —saldo, envíos, direcciones de los compradores— queda en la
    # caché del navegador después de cerrar sesión: el que agarra esa
    # computadora aprieta "atrás" y lo lee sin loguearse. Y si algún día
    # hay un proxy o CDN adelante, tampoco lo guarda ni se lo sirve a otro.
    api_privada = (
        path in {"/cotizar", "/stock", "/pedido", "/envios"}
        or path.startswith(("/pedidos/", "/rastrear/"))
    )
    shopify_privada = path.startswith(
        ("/shopify", "/integraciones/shopify/")
    )
    if path.startswith(("/portal", "/admin")) or api_privada or shopify_privada:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        if api_privada:
            vary = {
                parte.strip() for parte in response.headers.get("Vary", "").split(",")
                if parte.strip()
            }
            vary.add("X-API-Key")
            response.headers["Vary"] = ", ".join(sorted(vary))
        # Que Google ni loco indexe páginas privadas (aunque estén tras login,
        # el título "Cuenta corriente · MELCIOR" en resultados ya es una fuga).
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")

    return response

# Inicializar base de datos PostgreSQL al arrancar
_db_init_error = None
try:
    init_db()
except Exception as _db_err:
    _db_init_error = type(_db_err).__name__
    print(f"[startup] DB init error: {type(_db_err).__name__}")
    # En producción no alcanza con dejar /health en 503: si el proceso sigue
    # vivo, antes de que Railway descarte el candidato puede arrancar jobs,
    # threads o llamadas a couriers con un schema incompleto. Fallar el boot
    # impide cualquier side effect del release defectuoso y conserva la
    # versión sana anterior. Sin DATABASE_URL (desarrollo/tests), se mantiene
    # el import tolerante para poder trabajar offline.
    if os.getenv("DATABASE_URL"):
        raise RuntimeError(
            "El schema de PostgreSQL no quedó listo; se aborta el arranque."
        ) from _db_err

# Migrar api_key → api_key_hash UNA vez, en el arranque y no en el primer
# request. La migración hace ALTER TABLE (lock exclusivo sobre `clientes`)
# seguido de los UPDATE: hacerlo en el request-path serializaba cualquier
# lectura de clientes detrás de ese lock. Sigue siendo idempotente, así que
# queda como red si el arranque no llegó a correrla.
try:
    from servicios.api_b2b import _ensure_hash_migrado
    _ensure_hash_migrado()
except Exception as _mig_err:
    print(
        "[startup] migración de api_key diferida al primer uso: "
        f"{type(_mig_err).__name__}"
    )

# Static files (CSS, JS, imágenes), portal del cliente y admin
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(portal_router)
app.include_router(admin_router)
app.include_router(integraciones_router)
app.include_router(shopify_router)

WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
_WEB_HTML_PATH = os.path.join(WEB_DIR, "Tauro Solutions.html")


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
    pixel_id = obtener_meta_pixel_id()
    if not pixel_id:
        # Camino default: mismo FileResponse estático, sin loader ni llamadas
        # a Meta y conservando ETag/Last-Modified.
        return FileResponse(_WEB_HTML_PATH)

    with open(_WEB_HTML_PATH, encoding="utf-8") as archivo:
        html = inyectar_meta_pixel(archivo.read(), pixel_id)
    return HTMLResponse(html)


@app.get("/meta-pixel.js", include_in_schema=False)
def servir_meta_pixel():
    pixel_id = obtener_meta_pixel_id()
    if not pixel_id:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        content=javascript_meta_pixel(pixel_id),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )

# Los .jsx ya NO se sirven: la web carga el JS compilado desde /static/js/web
# (ver scripts/build_web.sh). Los fuentes quedan en web/components/ como
# fuente de verdad, pero exponerlos era regalar el código sin minificar.

@app.get("/styles.css", include_in_schema=False)
def servir_css():
    return FileResponse(os.path.join(WEB_DIR, "styles.css"))


class LeadCotizacionRequest(BaseModel):
    email: str = Field(..., max_length=160)
    # El navegador ya no reenvía precios ni carriers. Sólo presenta la
    # referencia opaca del snapshot creado por /cotizar-web.
    quote_id: str = Field(..., min_length=22, max_length=70)

    class Config:
        extra = "forbid"


@app.post("/cotizacion-lead", tags=["public"])
def cotizacion_lead(body: LeadCotizacionRequest, request: Request):
    """
    "Recibí esta cotización por mail": captura el contacto DESPUÉS de mostrar
    el precio — el cotizador sigue gratis y sin login, ese diferencial no se
    toca. Rate limit porque es público y manda mails.
    """
    import hashlib

    from core.email_transport import canonical_email_address
    from servicios.leads import guardar_lead
    from servicios.rate_limit import check_rate, client_ip

    if not check_rate(f"lead:{client_ip(request)}", max_attempts=5, window_seconds=900):
        return JSONResponse({"ok": False, "error": "Demasiados pedidos. Probá en unos minutos."},
                            status_code=429)
    email_canonico = canonical_email_address(body.email)
    if not email_canonico:
        return JSONResponse(
            {"ok": False, "error": "Ese email no parece válido."},
            status_code=400,
        )
    identidad = hashlib.sha256(email_canonico.encode("utf-8")).hexdigest()[:24]
    if not check_rate(f"lead_email:{identidad}", max_attempts=5, window_seconds=3600):
        return JSONResponse(
            {"ok": False, "error": "Ese correo ya hizo varios intentos. Probá más tarde."},
            status_code=429,
        )
    try:
        return guardar_lead(email_canonico, body.quote_id)
    except Exception as e:
        print(f"[leads] error procesando cotización: {type(e).__name__}")
        return JSONResponse({"ok": False, "error": "No pudimos enviar la cotización. Probá de nuevo."},
                            status_code=200)


@app.get("/cotizacion/{quote_id}", response_class=HTMLResponse, include_in_schema=False)
def cotizacion_publica(quote_id: str):
    """Copia web imprimible de la estimación enviada por correo."""
    from servicios.leads import obtener_cotizacion, renderizar_cotizacion_publica

    cotizacion = obtener_cotizacion(quote_id)
    if not cotizacion:
        return HTMLResponse(
            "<html><body style='background:#0c0a14;color:#fff;font-family:Arial;"
            "padding:48px;text-align:center'><h1>Cotización no encontrada</h1>"
            "<p>Volvé al cotizador para generar una nueva.</p>"
            "<a style='color:#a78bfa' href='/web'>Ir al cotizador</a></body></html>",
            status_code=404,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
        )
    return HTMLResponse(
        renderizar_cotizacion_publica(cotizacion),
        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


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

    _normalizar_precio = validator(
        "precio_cliente_final_ars", pre=True, allow_reuse=True,
    )(_importe_json_opcional)


class CotizarWebRequest(BaseModel):
    # El cotizador web compara envíos INTERNACIONALES en ambos sentidos y
    # también entre terceros países (AR→CN, CN→AR, CN→IN). AR→AR pertenece al
    # circuito nacional de OCA/Andreani y se rechaza antes de llamar carriers.
    # `origen_pais` es lo que manda; `destino_pais` + `sentido` quedan por
    # retrocompatibilidad con el widget viejo y sus consumidores existentes.
    origen_pais: str = Field(
        default="",
        description="ISO-2 del origen internacional. Vacío = se deduce del sentido",
    )
    destino_pais: str = Field(
        ...,
        description="ISO-2 del destino internacional (o del país exterior si va sentido)",
    )
    peso_kg: float = Field(..., gt=0, le=70)
    largo_cm: float = Field(..., gt=0, le=330)
    ancho_cm: float = Field(..., gt=0, le=330)
    alto_cm: float = Field(..., gt=0, le=330)
    valor_declarado_usd: float = Field(..., gt=0)
    # TAURO opera los dos sentidos. El campo es opcional y cae en exportación
    # para no romper a nadie que ya esté llamando este endpoint sin él.
    # Ojo: `destino_pais` es SIEMPRE el país del exterior; en una importación
    # ese país es el ORIGEN y el destino es Argentina.
    sentido: str = Field(default="exportacion", description="exportacion | importacion")

    _normalizar_decimales = validator(
        "peso_kg", "largo_cm", "ancho_cm", "alto_cm",
        pre=True, allow_reuse=True,
    )(_decimal_json)
    _normalizar_importe = validator(
        "valor_declarado_usd", pre=True, allow_reuse=True,
    )(_importe_json)

    @validator("alto_cm")
    def suma_dimensiones(cls, v, values):
        if all(k in values for k in ("largo_cm", "ancho_cm")):
            total = values["largo_cm"] + values["ancho_cm"] + v
            if total > 330:
                raise ValueError(
                    f"La suma de las tres medidas ({total:g} cm) no puede superar 330 cm."
                )
        return v


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
    # Si Railway sí configuró DATABASE_URL pero la migración del release
    # falló (por ejemplo por trackings históricos duplicados), el proceso no
    # puede declararse sano: servir el código nuevo sobre un schema viejo
    # rompería emisión y conciliación. El healthcheck 503 mantiene la versión
    # anterior atendiendo hasta resolver la migración.
    if os.getenv("DATABASE_URL") and _db_init_error:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "service": "tauro-api",
                     "detail": "database_schema_not_ready"},
        )
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
    import secrets as _secrets

    referencia = _secrets.token_hex(6)
    route = request.scope.get("route")
    ruta_log = getattr(route, "path", "<sin-ruta>")
    print(
        f"[500] ref={referencia} {request.method} {ruta_log} "
        f"→ {type(exc).__name__}"
    )
    acepta_html = "text/html" in (request.headers.get("accept") or "")
    if acepta_html and request.url.path.startswith(("/portal", "/admin", "/web")):
        return HTMLResponse(
            _HTML_ERROR_500,
            status_code=500,
            headers={
                "Cache-Control": "private, no-store",
                "X-Request-ID": referencia,
            },
        )
    return JSONResponse(
        {"ok": False, "error": "Error interno. Ya quedó registrado, probá de nuevo."},
        status_code=500,
        headers={
            "Cache-Control": "private, no-store",
            "X-Request-ID": referencia,
        },
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


@app.get("/operadores", tags=["public"])
def operadores_publicos():
    """Mapa seguro del producto: no expone credenciales, cuentas ni errores.

    La web puede explicar qué integraciones existen o están en proceso sin
    confundir "hay código" con "este cliente está habilitado".
    """
    from servicios.carrier_contract import Ambito, public_catalog
    return {
        "internacionales": public_catalog(Ambito.INTERNACIONAL),
        "nacionales": public_catalog(Ambito.NACIONAL),
    }


@app.get("/api/rastrear", tags=["public"])
def api_rastrear(nro: str, request: Request):
    """
    Rastreo público: el cliente pega su número y ve dónde está su envío, sin
    login. Devuelve sólo datos logísticos (courier, ciudad origen/destino,
    estado, fecha) — NUNCA datos personales.

    Rate limit anti-enumeración: aunque no expone PII, no queremos que alguien
    barra números probando de a miles. 20 consultas / 5 min por IP alcanza de
    sobra para un cliente real chequeando sus envíos.
    """
    from servicios.rate_limit import check_rate, client_ip

    if not check_rate(f"rastrear:{client_ip(request)}", max_attempts=20, window_seconds=300):
        return JSONResponse(
            {"ok": False, "error": "Demasiadas consultas. Esperá un minuto."},
            status_code=429)
    try:
        from servicios.rastreo import rastrear_publico
        return rastrear_publico(nro)
    except Exception as e:
        print(f"[rastrear] error: {type(e).__name__}")
        return JSONResponse(
            {"ok": False, "error": "No pudimos rastrear ahora. Probá de nuevo."},
            status_code=200)


@app.get("/paises", tags=["public"])
def paises_disponibles():
    """
    Los países que el cotizador acepta, como ORIGEN y como DESTINO.
    Una sola fuente para la web, el portal y la libreta: si se agrega un
    país acá, aparece en los tres sin tocar nada más.
    """
    from servicios.paises import opciones
    return {"paises": [{"iso": iso, "nombre": nombre} for iso, nombre in opciones()]}


@app.post("/cotizar-web", tags=["public"])
def cotizar_web(body: CotizarWebRequest, request: Request):
    """
    Cotización internacional pública para taurosolutions.ar — sin auth.
    Normaliza y valida ambos ISO-2 antes de comparar los couriers habilitados;
    AR→AR se deriva al futuro circuito nacional directo de OCA/Andreani.

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

    from servicios.paises import nombre as nombre_pais, normalizar_iso2, referencia

    destino_entrada = (body.destino_pais or "").strip()
    origen_entrada = (body.origen_pais or "").strip()

    # Sin origen explícito se deduce del sentido, que es como llamaba el
    # widget viejo: exportación = sale de Argentina; importación = entra.
    if not origen_entrada:
        if (body.sentido or "").lower().startswith("impo"):
            origen_entrada, destino_entrada = destino_entrada, "AR"
        else:
            origen_entrada = "AR"

    origen_iso = normalizar_iso2(origen_entrada)
    destino_iso = normalizar_iso2(destino_entrada)
    for iso, entrada, cual in (
        (origen_iso, origen_entrada, "origen"),
        (destino_iso, destino_entrada, "destino"),
    ):
        if not iso:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"País de {cual} '{str(entrada).strip().upper()}' "
                    "no soportado todavía."
                ),
            )

    if origen_iso == "AR" and destino_iso == "AR":
        raise HTTPException(
            status_code=409,
            detail=(
                "Los envíos dentro de Argentina se habilitarán con OCA y "
                "Andreani. Todavía no se pueden cotizar desde este formulario."
            ),
        )

    # Dirección de REFERENCIA de cada país: alcanza para una estimación
    # pública. En el portal, con destinatario cargado, se cotiza contra el
    # CP real porque los recargos por zona remota dependen de él.
    origen = {**referencia(origen_iso), "street": "Main St 100"}
    destino = {**referencia(destino_iso), "street": "Main St 100"}

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
    try:
        markup_pct = float(
            parse_configuracion_numerica(
                "WEB_MARKUP_PCT", os.getenv("WEB_MARKUP_PCT", "20")
            )
        )
    except (TypeError, ValueError):
        markup_pct = 20.0

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

    from servicios.leads import guardar_cotizacion

    snapshot = guardar_cotizacion(
        origen=origen_iso,
        destino=destino_iso,
        peso_kg=body.peso_kg,
        largo_cm=body.largo_cm,
        ancho_cm=body.ancho_cm,
        alto_cm=body.alto_cm,
        valor_declarado_usd=body.valor_declarado_usd,
        carriers=carriers,
        recomendado=recomendado,
    )

    return {
        "status": "success",
        "origen": f"{nombre_pais(origen_iso)} ({origen_iso})",
        "destino": f"{nombre_pais(destino_iso)} ({destino_iso})",
        "origen_pais": origen_iso,
        "destino_pais": destino_iso,
        "peso_kg": body.peso_kg,
        "recomendado": recomendado,
        "carriers": carriers,
        "quote_id": snapshot["quote_id"],
        "referencia": snapshot["referencia"],
        "emitida_en": snapshot["emitida_en"],
        "vigente_hasta": snapshot["vigente_hasta"],
    }


@app.post("/cotizar")
def cotizar(body: CotizarRequest, x_api_key: str = Header(default=None)):
    """
    Retorna el precio de envío para un producto hacia un destino.
    El precio USD se calcula dividiendo el ARS por el tipo de cambio oficial (no multiplicado).
    """
    perfil = autenticar(x_api_key)
    cliente_id = perfil["cliente_id"]

    from servicios.paises import normalizar_iso2
    origen_iso = normalizar_iso2(perfil.get("pais") or "AR")
    destino_iso = normalizar_iso2(body.destino_pais)
    if not origen_iso or not destino_iso:
        raise HTTPException(
            status_code=400,
            detail="El país de origen o destino no es válido.",
        )
    if origen_iso == "AR" and destino_iso == "AR":
        raise HTTPException(
            status_code=409,
            detail=("Los envíos dentro de Argentina se habilitarán con las "
                    "APIs directas de Andreani y OCA."),
        )

    resultado = obtener_precio_envio(
        cliente_id, body.producto_id, destino_iso, origen_pais=origen_iso,
    )

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
        "destino_pais": destino_iso,
        "ruta_id": resultado["ruta_id"],
        "coti_id": resultado["coti_id"],
        "precio_ars": resultado["precio_ars"],
        "precio_usd": resultado["precio_usd"],
        "tipo_cambio_usado": resultado["tipo_cambio_usado"],
        "dias_estimados": resultado["dias_estimados"],
        "valida_hasta": resultado["valida_hasta"],
    }


@app.get("/stock", tags=["catalogo"])
def stock_cliente(
    x_api_key: str = Header(default=None),
    limite: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Devuelve el catálogo y stock del dueño de la API key.

    Shopify sigue siendo la fuente de verdad. Esta lectura usa el espejo
    PostgreSQL de TAURO, por lo que responde rápido y no consume el límite de
    la tienda. Nunca mezcla clientes ni publica costos/márgenes logísticos.
    """
    perfil = autenticar(x_api_key)
    from servicios.catalogo import estado_sincronizacion_cliente, listar_stock_cliente

    productos, total = listar_stock_cliente(perfil["cliente_id"], limite, offset)
    estado = estado_sincronizacion_cliente(perfil["cliente_id"]) or {}

    def _fecha(valor):
        return valor.isoformat() if hasattr(valor, "isoformat") else valor

    filas = []
    for producto in productos:
        filas.append({
            "alias": producto.alias_interno,
            "sku": producto.sku_tienda or producto.alias_interno,
            "producto": producto.titulo_tienda or producto.nombre_invoice,
            "variante": producto.variante_tienda or "",
            "imagen_url": producto.imagen_url,
            "shopify_product_id": producto.external_product_id,
            "shopify_variant_id": producto.external_variant_id,
            "precio_tienda": producto.precio_tienda,
            "moneda_tienda": producto.moneda_tienda,
            "stock_controlado": producto.stock_controlado,
            "stock_disponible": producto.stock_disponible,
            "stock_comprometido": producto.stock_comprometido,
            "stock_fisico": producto.stock_fisico,
            "stock_entrante": producto.stock_entrante,
            "stock_actualizado_en": _fecha(producto.stock_actualizado_at),
            "listo_para_envio": producto.activo,
            "ubicaciones": [
                {
                    "id": ubicacion.get("external_location_id"),
                    "nombre": ubicacion.get("ubicacion_nombre"),
                    "disponible": ubicacion.get("disponible"),
                    "comprometido": ubicacion.get("comprometido"),
                    "fisico": ubicacion.get("fisico"),
                    "entrante": ubicacion.get("entrante"),
                    "actualizado_en": _fecha(ubicacion.get("source_updated_at")),
                }
                for ubicacion in producto.ubicaciones
            ],
        })

    return {
        "status": "success",
        "total": total,
        "limite": limite,
        "offset": offset,
        "sincronizacion": {
            "estado": estado.get("estado") or "SIN_TIENDA",
            "ultima_sincronizacion_en": _fecha(estado.get("ultima_sincronizacion_at")),
            "ultimo_error_codigo": estado.get("ultimo_error_codigo"),
        },
        "productos": filas,
    }


@app.post("/pedido")
def registrar_pedido(
    body: PedidoRequest,
    x_api_key: str = Header(default=None),
    idempotency_key: str = Header(default=None, alias="Idempotency-Key"),
):
    """
    Registra un pedido confirmado.
    Combina los datos del comprador con el perfil del cliente (remitente) 
    y genera el PDF de armado de guía para logística.
    """
    perfil = autenticar(x_api_key)
    cliente_id = perfil["cliente_id"]

    idempotency_key = (idempotency_key or "").strip()
    if idempotency_key and not 8 <= len(idempotency_key) <= 200:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key debe tener entre 8 y 200 caracteres.",
        )
    body_dict = body.model_dump(mode="json") if hasattr(body, "model_dump") else body.dict()
    request_fingerprint = hashlib.sha256(
        json.dumps(body_dict, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    idempotency_key_hash = (
        hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        if idempotency_key else ""
    )

    from servicios.paises import normalizar_iso2
    origen_iso = normalizar_iso2(perfil.get("pais") or "AR")
    destino_iso = normalizar_iso2(body.destino_pais)
    pais_direccion_iso = normalizar_iso2(body.pais)
    if not origen_iso or not destino_iso or not pais_direccion_iso:
        raise HTTPException(
            status_code=400,
            detail="El país de origen o destino no es válido.",
        )
    if pais_direccion_iso != destino_iso:
        raise HTTPException(
            status_code=400,
            detail="El país de la dirección debe coincidir con el destino cotizado.",
        )
    if origen_iso == "AR" and destino_iso == "AR":
        raise HTTPException(
            status_code=409,
            detail=("Los envíos dentro de Argentina se habilitarán con las "
                    "APIs directas de Andreani y OCA."),
        )

    # Validar que el precio exista
    precio = obtener_precio_envio(
        cliente_id, body.producto_id, destino_iso, origen_pais=origen_iso,
    )
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
        "remitente_pais": origen_iso,
        "remitente_telefono": perfil["telefono"],
        "remitente_email": perfil["email"],

        # Destinatario (del body del request)
        "dest_nombre": body.nombre_comprador,
        "dest_direccion": body.direccion_exacta,
        "dest_ciudad": body.ciudad,
        "dest_estado": body.estado,
        "dest_zip": body.zip_code,
        "dest_pais": destino_iso,
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

    # La referencia es estable cuando el integrador trae Idempotency-Key.
    # Sin ella conservamos compatibilidad, pero agregamos aleatoriedad para
    # evitar colisiones entre dos pedidos del mismo producto en el mismo minuto.
    from datetime import datetime
    fecha = datetime.now().strftime("%Y%m%d-%H%M")
    sufijo = idempotency_key_hash[:12] if idempotency_key_hash else secrets.token_hex(4)
    referencia = f"{cliente_id}-{body.producto_id}-{fecha}-{sufijo}"
    datos_pedido["referencia"] = referencia

    # Primero persistimos; el correo es una notificación auxiliar. Antes un
    # fallo SMTP hacía perder el pedido entero aunque el cliente ya lo hubiera
    # confirmado. El admin ve la solicitud incluso si el mail necesita revisión.
    try:
        solicitud = crear_solicitud_guia(
            cliente_id=cliente_id,
            producto_alias=producto["nombre_es"],
            cantidad=int(producto["unidades"] or 1),
            destino_pais=destino_iso,
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
            remitente_pais=origen_iso,
            api_referencia=referencia,
            idempotency_key_hash=idempotency_key_hash,
            request_fingerprint=request_fingerprint,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    replay = bool(solicitud.get("_idempotent_replay"))
    referencia = solicitud.get("api_referencia") or referencia
    notificacion = "omitida_reintento"
    if not replay:
        notificacion = "enviada" if enviar_email_pedido(datos_pedido) else "fallida_no_bloqueante"

    return {
        "status": "success",
        "mensaje": (
            "Pedido ya recibido anteriormente; se devuelve la misma solicitud."
            if replay else "Pedido recibido y solicitud creada."
        ),
        "referencia": referencia,
        "solicitud_id": solicitud["id"],
        "estado": solicitud.get("estado") or "SOLICITADO",
        "idempotent_replay": replay,
        "idempotencia_protegida": bool(idempotency_key),
        "notificacion_logistica": notificacion,
        "estado_url": f"/pedidos/{solicitud['id']}",
    }


def _fecha_api(valor):
    return valor.isoformat() if hasattr(valor, "isoformat") else valor


@app.get("/pedidos/{solicitud_id}", tags=["envios"])
def estado_pedido(solicitud_id: int, x_api_key: str = Header(default=None)):
    """Estado operativo de una solicitud, aislado por cliente."""
    perfil = autenticar(x_api_key)
    from servicios.couriers_urls import url_tracking
    from servicios.solicitudes_guia import obtener_solicitud_de_cliente

    solicitud = obtener_solicitud_de_cliente(solicitud_id, perfil["cliente_id"])
    if not solicitud:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    tracking = (solicitud.get("tracking") or "").strip()
    courier = (solicitud.get("courier") or "").strip().upper()
    return {
        "status": "success",
        "solicitud_id": solicitud["id"],
        "referencia": solicitud.get("api_referencia"),
        "estado": solicitud.get("estado"),
        "ambito": (solicitud.get("ambito") or "").lower(),
        "courier": courier or None,
        "servicio": solicitud.get("servicio_courier"),
        "tracking": tracking or None,
        "tracking_url": url_tracking(courier, tracking) if tracking else None,
        "guia_disponible": bool(solicitud.get("tiene_label")),
        "guia_url": (
            f"/pedidos/{solicitud['id']}/guia.pdf"
            if solicitud.get("tiene_label") else solicitud.get("guia_url")
        ),
        "factura_comercial_disponible": bool(
            solicitud.get("tiene_factura_comercial")
        ),
        "factura_comercial_url": (
            f"/pedidos/{solicitud['id']}/factura-comercial.pdf"
            if solicitud.get("tiene_factura_comercial") else None
        ),
        "creado_en": _fecha_api(solicitud.get("created_at")),
        "actualizado_en": _fecha_api(solicitud.get("updated_at")),
    }


@app.get("/envios", tags=["envios"])
def listar_envios_b2b(
    x_api_key: str = Header(default=None),
    limite: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ambito: str = Query(..., description="NACIONAL o INTERNACIONAL"),
    estado: str = Query(""),
):
    """Historial B2B paginado, separado por ámbito y dueño de la API key."""
    perfil = autenticar(x_api_key)
    ambito = (ambito or "").strip().upper()
    estado = (estado or "").strip().upper()
    if ambito not in {"NACIONAL", "INTERNACIONAL"}:
        raise HTTPException(
            status_code=400, detail="Ámbito inválido: usá NACIONAL o INTERNACIONAL."
        )
    from servicios.solicitudes_guia import ESTADOS_VALIDOS, listar_envios_api
    if estado and estado not in ESTADOS_VALIDOS:
        raise HTTPException(status_code=400, detail="Estado de envío inválido.")

    filas, total = listar_envios_api(
        perfil["cliente_id"], limite=limite, offset=offset,
        ambito=ambito, estado=estado,
    )
    envios = []
    for envio in filas:
        tracking = (envio.get("tracking") or "").strip()
        courier = (envio.get("courier") or "").strip().upper()
        envios.append({
            "solicitud_id": envio["id"],
            "referencia": envio.get("api_referencia"),
            "estado": envio.get("estado"),
            "ambito": (envio.get("ambito") or "").lower(),
            "courier": courier or None,
            "servicio": envio.get("servicio_courier"),
            "producto": envio.get("producto_alias"),
            "cantidad": envio.get("cantidad"),
            "destino": {
                "pais": envio.get("destino_pais"),
                "nombre": envio.get("dest_nombre"),
                "ciudad": envio.get("dest_ciudad"),
                "estado": envio.get("dest_estado"),
            },
            "peso_kg": envio.get("peso_kg"),
            "valor_declarado_usd": envio.get("valor_declarado_usd"),
            "precio_tauro_ars": envio.get("precio_tauro_ars"),
            "precio_tauro_usd": envio.get("precio_tauro_usd"),
            "tracking": tracking or None,
            "guia_disponible": bool(envio.get("tiene_label")),
            "estado_url": f"/pedidos/{envio['id']}",
            "guia_url": f"/pedidos/{envio['id']}/guia.pdf" if envio.get("tiene_label") else None,
            "factura_comercial_disponible": bool(
                envio.get("tiene_factura_comercial")
            ),
            "factura_comercial_url": (
                f"/pedidos/{envio['id']}/factura-comercial.pdf"
                if envio.get("tiene_factura_comercial") else None
            ),
            "creado_en": _fecha_api(envio.get("created_at")),
            "actualizado_en": _fecha_api(envio.get("updated_at")),
        })

    siguiente = offset + limite if offset + len(envios) < total else None
    anterior = max(0, offset - limite) if offset > 0 else None
    return {
        "status": "success",
        "total": total,
        "limite": limite,
        "offset": offset,
        "siguiente_offset": siguiente,
        "anterior_offset": anterior,
        "filtros": {"ambito": ambito or None, "estado": estado or None},
        "envios": envios,
    }


@app.get("/pedidos/{solicitud_id}/guia.pdf", tags=["envios"])
def descargar_guia_api(solicitud_id: int, x_api_key: str = Header(default=None)):
    """Descarga autenticada de la etiqueta sin depender de la sesión web."""
    perfil = autenticar(x_api_key)
    from servicios.solicitudes_guia import obtener_label_de_cliente

    pdf = obtener_label_de_cliente(solicitud_id, perfil["cliente_id"])
    if not pdf:
        raise HTTPException(status_code=404, detail="La guía todavía no está disponible.")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="guia-{solicitud_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/pedidos/{solicitud_id}/factura-comercial.pdf", tags=["envios"])
def descargar_factura_comercial_api(
    solicitud_id: int, x_api_key: str = Header(default=None),
):
    """Descarga autenticada de la invoice, siempre filtrada por dueño."""
    perfil = autenticar(x_api_key)
    from servicios.solicitudes_guia import obtener_factura_comercial_pdf

    pdf = obtener_factura_comercial_pdf(
        solicitud_id, cliente_id=perfil["cliente_id"],
    )
    if not pdf:
        raise HTTPException(
            status_code=404,
            detail="La factura comercial todavía no está disponible.",
        )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="factura-comercial-{solicitud_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/rastrear/{tracking}", tags=["envios"])
def rastrear_envio_api(
    tracking: str,
    x_api_key: str = Header(default=None),
    actualizar: bool = Query(True),
):
    """Rastreo privado: sólo permite consultar envíos de la propia cuenta."""
    perfil = autenticar(x_api_key)
    from servicios.rate_limit import check_rate
    from servicios.rastreo import rastrear_cliente

    if not check_rate(
        f"rastrear-api:{perfil['cliente_id']}", max_attempts=60, window_seconds=300
    ):
        raise HTTPException(status_code=429, detail="Demasiadas consultas de tracking.")
    resultado = rastrear_cliente(
        perfil["cliente_id"], tracking, actualizar=actualizar,
    )
    if not resultado.get("encontrado"):
        raise HTTPException(status_code=404, detail="Envío no encontrado en tu cuenta.")
    return {"status": "success", **resultado}


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

_CRON_DIAS = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu",
    "fri": "fri", "sat": "sat", "sun": "sun",
}
_cron_dia_raw = os.getenv("CRON_DIA", "mon").strip().lower()
CRON_DIA = _CRON_DIAS.get(_cron_dia_raw, "mon")
if _cron_dia_raw not in _CRON_DIAS:
    print(f"[scheduler] CRON_DIA={_cron_dia_raw!r} inválido; se usa 'mon'.")
CRON_HORA = int(os.getenv("CRON_HORA", 6))

scheduler = BackgroundScheduler(timezone="America/Argentina/Buenos_Aires")
scheduler.add_job(
    job_actualizar_precios_fedex,
    trigger="cron",
    day_of_week=CRON_DIA,
    hour=CRON_HORA,
    minute=0,
)

# Entrega de presupuestos: sólo reintenta fallos SMTP transitorios y nunca
# filas legacy, rechazadas o vencidas. El primer intento sigue siendo síncrono
# para que la web no diga “enviado” antes de la aceptación real.
from servicios.leads import procesar_reintentos_email
scheduler.add_job(
    procesar_reintentos_email,
    trigger="interval",
    minutes=5,
    max_instances=1,
    coalesce=True,
)

# Recuperación de contraseña: el request público sólo encola. Este worker
# genera el token en memoria y espera la aceptación SMTP fuera del request,
# por lo que una cuenta existente e inexistente reciben la misma respuesta.
from servicios.password_reset_queue import procesar_password_reset_requests
scheduler.add_job(
    procesar_password_reset_requests,
    trigger="interval",
    minutes=1,
    max_instances=1,
    coalesce=True,
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
        print(f"[scheduler] poda de auditoría falló: {type(e).__name__}")


scheduler.add_job(job_limpiar_auditoria, trigger="cron", hour=3, minute=30)


# Retención de PII del cotizador y snapshots huérfanos. Nunca elimina una
# cotización todavía referenciada por un lead conservado.
def job_limpiar_cotizaciones_email():
    try:
        from servicios.leads import limpiar_retencion_cotizaciones
        from servicios.password_reset_queue import limpiar_retencion_password_reset
        resultado = limpiar_retencion_cotizaciones()
        reset = limpiar_retencion_password_reset()
        if any(resultado.values()) or any(reset.values()):
            print(
                "[scheduler] correo: "
                f"{resultado['leads_eliminados']} leads y "
                f"{resultado['cotizaciones_eliminadas']} cotizaciones, "
                f"{reset['solicitudes_eliminadas']} recuperos y "
                f"{reset['tokens_eliminados']} tokens podados"
            )
    except Exception as e:
        print(f"[scheduler] poda de correo falló: {type(e).__name__}")


scheduler.add_job(
    job_limpiar_cotizaciones_email,
    trigger="cron",
    hour=3,
    minute=45,
    max_instances=1,
    coalesce=True,
)

# Shopify: los webhooks de catálogo/stock se confirman rápido y quedan en
# una cola PostgreSQL. Este worker durable completa lo pendiente aunque el
# proceso se haya reiniciado. La reconciliación de 30 minutos cubre un webhook
# que Shopify no haya podido entregar.
from servicios.shopify_catalogo import (
    limpiar_eventos as limpiar_eventos_shopify,
    procesar_cola_eventos as procesar_webhooks_shopify,
    reconciliar_tiendas_pendientes as reconciliar_shopify,
)
scheduler.add_job(
    procesar_webhooks_shopify,
    trigger="interval",
    seconds=15,
    max_instances=1,
    coalesce=True,
)
scheduler.add_job(
    reconciliar_shopify,
    trigger="interval",
    minutes=5,
    max_instances=1,
    coalesce=True,
)
scheduler.add_job(
    limpiar_eventos_shopify,
    trigger="cron",
    hour=3,
    minute=55,
    max_instances=1,
    coalesce=True,
)

# Privacidad Shopify: el webhook solo confirma despues del commit. Este
# worker notifica a Operaciones desde la cola durable y la poda conserva 90
# dias las solicitudes ya resueltas, nunca las obligaciones pendientes.
from servicios.shopify_gdpr import (
    limpiar_resueltas as limpiar_gdpr_shopify,
    procesar_solicitudes as procesar_gdpr_shopify,
)
scheduler.add_job(
    procesar_gdpr_shopify,
    trigger="interval",
    seconds=30,
    max_instances=1,
    coalesce=True,
)
scheduler.add_job(
    limpiar_gdpr_shopify,
    trigger="cron",
    hour=3,
    minute=50,
    max_instances=1,
    coalesce=True,
)


# La poda oportunista al recibir/vincular órdenes no alcanza para tiendas
# dormidas. Este job garantiza la retención máxima de 90 días aun cuando no
# llegue ningún webhook nuevo.
def job_limpiar_pedidos_huerfanos():
    try:
        from servicios.integraciones_tienda import limpiar_pedidos_huerfanos_vencidos
        eliminados = limpiar_pedidos_huerfanos_vencidos()
        if eliminados:
            print(f"[scheduler] Shopify: {eliminados} pedido(s) huérfano(s) vencido(s) podados")
    except Exception as e:
        print(f"[scheduler] poda de pedidos huérfanos falló: {type(e).__name__}")


scheduler.add_job(
    job_limpiar_pedidos_huerfanos,
    trigger="cron",
    hour=3,
    minute=52,
    max_instances=1,
    coalesce=True,
)


# Dólar oficial automático: el tipo de cambio mueve TODOS los precios. En vez
# de cargarlo a mano, se actualiza solo desde el oficial. Corre cada 6 h (el
# oficial se mueve un par de veces al día) y también al arrancar (más abajo).
# Se apaga con DOLAR_AUTO=0 para volver al control manual.
def job_actualizar_dolar():
    try:
        from servicios.dolar_oficial import actualizar_dolar_auto
        r = actualizar_dolar_auto()
        # El checkout NO cotiza en vivo: lee precio_ars ya calculado de
        # tarifas_cache, congelado con el dólar del último refresco. Si el
        # dólar cambió, hay que recalcular la cache o el checkout —la ruta que
        # factura— sigue vendiendo con el dólar viejo hasta las 4am. Se hace en
        # un hilo porque son ~66 cotizaciones y no puede trabar el scheduler.
        if r and r.get("motivo") == "actualizado":
            def _refrescar():
                try:
                    from servicios.tarifas_cache import refrescar_cache
                    refrescar_cache()
                    print("[scheduler] tarifas del checkout recalculadas con el dólar nuevo")
                except Exception as e:
                    print(
                        "[scheduler] no pude refrescar tarifas tras el dólar: "
                        f"{type(e).__name__}"
                    )
            import threading as _t
            _t.Thread(target=_refrescar, daemon=True).start()
    except Exception as e:
        print(f"[scheduler] actualización de dólar falló: {type(e).__name__}")


scheduler.add_job(job_actualizar_dolar, trigger="interval", hours=6)


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
        print(f"[scheduler] refresco de tarifas falló: {type(e).__name__}")


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

# Espejo sin PII en el Google Sheet TAURO 2026 (pestaña
# PLATAFORMA_SIN_PII, propiedad del sistema; el histórico PLATAFORMA queda
# intacto hasta una decisión documentada). Sólo corre si
# GOOGLE_CREDENTIALS_JSON está cargada; cada 30 min alcanza para control.
from jobs.sync_sheet_tauro import sincronizar_seguro, configurado as _sheet_conf
if _sheet_conf():
    scheduler.add_job(sincronizar_seguro, trigger="interval", minutes=30)
    print("[scheduler] Espejo en Google Sheet: cada 30 min (PLATAFORMA_SIN_PII)")
else:
    print("[scheduler] Espejo en Google Sheet APAGADO (falta GOOGLE_CREDENTIALS_JSON)")

# Cola comercial: apagada por default. El panel puede preparar trabajos sin
# que ningun agente se ejecute; para procesarlos hacen falta la key y el flag
# explicito. Los correos siguen necesitando dos acciones humanas en /admin.
#
# ⚠️ Import TOLERANTE a propósito: jobs/agentes_comerciales.py es un WIP de
# otra sesión que todavía no está commiteado (necesita `openai` en
# requirements y sus credenciales). Este import directo TUMBÓ los deploys
# del 05/08: en local bootea porque el archivo existe untracked, en el
# contenedor no existe → ModuleNotFoundError → Railway descarta el deploy y
# sigue sirviendo el build viejo. Un feature opcional apagado por default
# JAMÁS puede tener poder de veto sobre el arranque.
try:
    from jobs.agentes_comerciales import (
        procesar_cola_comercial, habilitado as _crm_agents_on,
    )
except ImportError:
    procesar_cola_comercial = None

    def _crm_agents_on() -> bool:
        return False

if _crm_agents_on():
    scheduler.add_job(
        procesar_cola_comercial,
        trigger="interval",
        minutes=1,
        max_instances=1,
        coalesce=True,
    )
    print("[scheduler] Agentes comerciales ACTIVOS: cola cada 1 min")
else:
    print("[scheduler] Agentes comerciales APAGADOS (flag, key o módulo faltante)")

scheduler.start()


def _tarifas_al_arrancar():
    """
    Auto-sanación: si la tabla de tarifas está vacía (primer deploy, base
    nueva, o alguien la borró), la llena sola en segundo plano en vez de
    esperar al job de las 4am. Sin esto, el checkout depende de cotizar en
    vivo — que es justo lo que queremos evitar.
    """
    # Primero el dólar (las tarifas se calculan con él): que arranque fresco.
    try:
        from servicios.dolar_oficial import actualizar_dolar_auto
        actualizar_dolar_auto()
    except Exception as e:
        print(f"[startup] no pude actualizar el dólar: {type(e).__name__}")
    try:
        from servicios.tarifas_cache import estado_cache, refrescar_cache
        estado = estado_cache()
        if estado.get("tarifas", 0) > 0:
            print(f"[startup] tarifas del checkout: {estado['tarifas']} cargadas")
            return
        print("[startup] tabla de tarifas vacía → llenando en segundo plano")
        refrescar_cache()
    except Exception as e:
        print(f"[startup] no pude precargar tarifas: {type(e).__name__}")


# En un hilo aparte: el arranque no puede esperar ~66 cotizaciones, y
# Railway mata el deploy si el healthcheck no responde a tiempo.
import threading
threading.Thread(target=_tarifas_al_arrancar, daemon=True).start()

print(f"[scheduler] Job semanal precios FedEx: {CRON_DIA} {CRON_HORA}:00 (Argentina)")
print(f"[scheduler] Job diario limpiar_sessions: 3:00 (Argentina)")
print(f"[scheduler] Job diario tarifas del checkout: 4:00 (Argentina)")
print(f"[scheduler] Centinela del checkout: cada 15 min")
