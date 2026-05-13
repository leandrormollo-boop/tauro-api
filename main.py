from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
import os
from dotenv import load_dotenv

from core.sheets_client import (
    obtener_cliente_por_api_key,
    obtener_precio_envio,
    obtener_datos_producto,
    obtener_todas_las_combinaciones_coti,
    actualizar_costo_fedex_en_coti,
    obtener_margen_minimo_ars,
)
from core.fedex_client import FedExClient
from core.email_sender import enviar_email_pedido, enviar_alerta_margen
from core.database import init_db
from endpoints.portal_cliente import router as portal_router
from endpoints.admin import router as admin_router

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

fedex = FedExClient()

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
    Endpoint público para que Render / monitores externos verifiquen que la app está viva.
    No requiere auth. Devuelve siempre 200 si el proceso responde.
    """
    return {"status": "ok", "service": "tauro-api", "version": "1.0.0"}


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

    resultado = fedex.get_rates(origen, destino, paquete)
    if not resultado.get("encontrado"):
        raise HTTPException(status_code=502, detail="No se pudo obtener tarifa FedEx en este momento.")

    dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
    markup_pct = float(os.getenv("WEB_MARKUP_PCT", "20"))

    # FedEx sandbox devuelve USD; producción AR devuelve ARS
    if resultado.get("moneda", "USD") == "USD":
        costo_usd = resultado["costo"]
        costo_ars = round(costo_usd * dolar)
    else:
        costo_ars = resultado["costo"]
        costo_usd = round(costo_ars / dolar, 2)

    precio_ars = round(costo_ars * (1 + markup_pct / 100))
    precio_usd = round(precio_ars / dolar, 2)

    return {
        "status": "success",
        "precio_ars": precio_ars,
        "precio_usd": precio_usd,
        "dias_estimados": resultado.get("dias_estimados", "3-5"),
        "servicio": "FedEx International Priority",
        "origen": "Buenos Aires, AR",
        "destino": body.destino_pais.upper(),
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
        "precio_ars": resultado["precio_ars"],
        "precio_usd": resultado["precio_usd"],
        "tipo_cambio_usado": resultado["tipo_cambio_usado"],
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
        # Remitente (del perfil del cliente en PERFILES)
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

        # Producto / Aduana (de PRODUCTOS_CATALOGO)
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

    return {
        "status": "success",
        "mensaje": "Pedido recibido. PDF enviado a logística.",
        "referencia": referencia,
    }


# ─────────────────────────────────────────────
# JOB SEMANAL — ACTUALIZACIÓN DE PRECIOS FEDEX
# ─────────────────────────────────────────────

def job_actualizar_precios_fedex():
    """
    Corre todos los lunes a las 6am (hora Argentina).
    Consulta FedEx por cada combinación en COTI y actualiza COSTO_FEDEX_ARS.
    Si el margen cae por debajo del mínimo, envía alerta por email.
    """
    print("[job] Iniciando actualización semanal de precios FedEx...")
    combinaciones = obtener_todas_las_combinaciones_coti()
    margen_minimo = obtener_margen_minimo_ars()

    alertas = []

    for fila in combinaciones:
        cliente_id = str(fila.get("CLIENTE_ID", "")).strip()
        producto_id = str(fila.get("PRODUCTO_ID", "")).strip()
        destino_pais = str(fila.get("DESTINO_PAIS", "")).strip()
        precio_ars = float(str(fila.get("PRECIO_ARS", 0)).replace(",", ".") or 0)

        if not all([cliente_id, producto_id, destino_pais]):
            continue

        # Origen: Buenos Aires por defecto
        origen = {
            "street": "Av. Corrientes 1234",
            "city": "BUENOS AIRES",
            "state": "B",
            "postal_code": "1043",
            "country": "AR",
        }

        destino = {
            "city": "",
            "state": "",
            "postal_code": str(fila.get("DESTINO_CP", "10001")),
            "country": destino_pais,
        }

        paquete = {
            "peso_kg": float(str(fila.get("PESO_KG", 0.5)).replace(",", ".") or 0.5),
            "largo": float(str(fila.get("LARGO", 30)).replace(",", ".") or 30),
            "ancho": float(str(fila.get("ANCHO", 20)).replace(",", ".") or 20),
            "alto": float(str(fila.get("ALTO", 10)).replace(",", ".") or 10),
            "valor_declarado_usd": float(str(fila.get("VALOR_USD", 100)).replace(",", ".") or 100),
            "descripcion_en": str(fila.get("NOMBRE_EN", "Merchandise")),
            "hs_code": str(fila.get("HS_CODE", "")),
            "unidades": int(fila.get("UNIDADES", 1)),
        }

        resultado = fedex.get_rates(origen, destino, paquete)

        if resultado.get("encontrado"):
            # Producción AR devuelve ARS, sandbox devuelve USD → convertimos
            dolar = float(os.getenv("COTIZACION_DOLAR_ARS", "1450"))
            if resultado.get("moneda", "USD") == "USD":
                nuevo_costo = round(resultado["costo"] * dolar)
            else:
                nuevo_costo = round(resultado["costo"])
            actualizar_costo_fedex_en_coti(cliente_id, producto_id, destino_pais, nuevo_costo)

            margen = precio_ars - nuevo_costo
            if margen < margen_minimo:
                alertas.append({
                    "cliente_id": cliente_id,
                    "producto_id": producto_id,
                    "destino_pais": destino_pais,
                    "precio_ars": precio_ars,
                    "costo_fedex_ars": nuevo_costo,
                    "margen_ars": margen,
                    "margen_minimo_ars": margen_minimo,
                })
        else:
            print(f"[job] No se pudo obtener tarifa: {cliente_id}/{producto_id}/{destino_pais}")

    if alertas:
        enviar_alerta_margen(alertas)

    print(f"[job] Actualización completada. {len(alertas)} alertas de margen enviadas.")


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
