from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse, JSONResponse
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
from servicios.api_b2b import (
    obtener_cliente_por_api_key,
    obtener_precio_envio,
    obtener_datos_producto,
)
from servicios.solicitudes_guia import crear_solicitud_guia

load_dotenv()

app = FastAPI(
    title="Tauro Solutions API",
    description="API de cotización y gestión de envíos internacionales para eCommerce argentino.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Inicializar base de datos PostgreSQL al arrancar
try:
    init_db()
except Exception as _db_err:
    print(f"[startup] DB init error: {_db_err}")

# Static files (CSS, JS, imágenes), portal del cliente y admin
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(portal_router)
app.include_router(admin_router)

WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))


@app.middleware("http")
async def revalidar_assets_web(request: Request, call_next):
    """
    Los assets de la web pública (HTML, styles.css, los .jsx) NO tienen
    versión en la URL, así que el browser los cacheaba de más y la gente
    seguía viendo la versión vieja tras un deploy. Con `no-cache` el
    navegador revalida cada vez: el ETag hace que si no cambió devuelva
    304 (barato), y si cambió trae lo nuevo al instante. Nunca más una
    versión pegada.
    """
    response = await call_next(request)
    path = request.url.path
    if (path in ("/web", "/styles.css", "/tweaks-panel.jsx")
            or path.startswith("/components")
            or path.endswith(".jsx")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/web", include_in_schema=False)
def servir_web():
    return FileResponse(os.path.join(WEB_DIR, "Tauro Solutions.html"))

app.mount("/components", StaticFiles(directory=os.path.join(WEB_DIR, "components")), name="web-components")

@app.get("/styles.css", include_in_schema=False)
def servir_css():
    return FileResponse(os.path.join(WEB_DIR, "styles.css"))

@app.get("/tweaks-panel.jsx", include_in_schema=False)
def servir_tweaks():
    return FileResponse(os.path.join(WEB_DIR, "tweaks-panel.jsx"))


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
    destino_pais: str = Field(..., description="ISO-2: US, BR, CL, UY")
    peso_kg: float = Field(..., gt=0, le=70)
    largo_cm: float = Field(..., gt=0)
    ancho_cm: float = Field(..., gt=0)
    alto_cm: float = Field(..., gt=0)
    valor_declarado_usd: float = Field(default=100.0, gt=0)


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


@app.post("/cotizar-web", tags=["public"])
def cotizar_web(body: CotizarWebRequest):
    """
    Cotización pública para taurosolutions.ar — sin auth.
    Llama directo a FedEx y aplica el markup web de Tauro.
    """
    DESTINOS = {
        "US": {"city": "MIAMI",      "state": "FL", "postal_code": "33101"},
        "BR": {"city": "SAO PAULO",  "state": "SP", "postal_code": "01310100"},
        "CL": {"city": "SANTIAGO",   "state": "RM", "postal_code": "8320000"},
        "UY": {"city": "MONTEVIDEO", "state": "MO", "postal_code": "11000"},
        "MX": {"city": "MEXICO",     "state": "DF", "postal_code": "06600"},
        "ES": {"city": "MADRID",     "state": "M",  "postal_code": "28001"},
    }

    destino_info = DESTINOS.get(body.destino_pais.upper())
    if not destino_info:
        raise HTTPException(status_code=400, detail=f"País '{body.destino_pais}' no soportado aún.")

    origen = {
        "street": "Av. Corrientes 1234",
        "city": "BUENOS AIRES",
        "state": "B",
        "postal_code": "1043",
        "country": "AR",
    }
    destino = {**destino_info, "country": body.destino_pais.upper()}
    paquete = {
        "peso_kg": body.peso_kg,
        "largo": body.largo_cm,
        "ancho": body.ancho_cm,
        "alto": body.alto_cm,
        "valor_declarado_usd": body.valor_declarado_usd,
        "descripcion_en": "Merchandise",
    }

    dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
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
        "origen": "Buenos Aires, AR",
        "destino": body.destino_pais.upper(),
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

    return {
        "status": "success",
        "producto_id": body.producto_id,
        "destino_pais": body.destino_pais,
        "ruta_id": resultado["ruta_id"],
        "coti_id": resultado["coti_id"],
        "precio_ars": resultado["precio_ars"],
        "precio_usd": resultado["precio_usd"],
        "tipo_cambio_usado": resultado["tipo_cambio_usado"],
        "markup_tipo": resultado["markup_tipo"],
        "markup_valor": resultado["markup_valor"],
        "markup_pct_equivalente": resultado["markup_pct_equivalente"],
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

scheduler.start()

print(f"[scheduler] Job semanal precios FedEx: {CRON_DIA} {CRON_HORA}:00 (Argentina)")
print(f"[scheduler] Job diario limpiar_sessions: 3:00 (Argentina)")
