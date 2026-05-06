# ============================================================
# TAURO API — Inicializar hojas nuevas en Google Sheets
# ============================================================
# Crea las hojas necesarias para el portal del cliente y logs.
# Si la hoja ya existe NO la pisa — solo asegura los headers.
#
# Hojas que crea (si no existen):
#   - PAGOS               (cuenta corriente)
#   - SESSIONS            (login mágico)
#   - RUTAS_DEFAULT       (rutas predefinidas para cotizar)
#   - PRODUCTOS_CATALOGO  (catálogo por cliente — si no existe)
#   - LOG_REQUESTS        (log de todas las requests)
#   - LOG_ERRORES         (log de errores con traceback)
#   - LOG_HEALTH          (estado de servicios)
#   - LOG_JOBS            (ejecución de cron jobs)
#
# Uso:
#   cd "API TAURO"
#   source venv/bin/activate
#   python scripts/inicializar_hojas.py
# ============================================================

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
CREDS = "credenciales.json"

# ── Definición de hojas ─────────────────────────────────────
HOJAS = {
    "PAGOS": [
        "FECHA", "CLIENTE", "MONTO_ARS", "METODO", "REFERENCIA", "NOTA"
    ],
    "SESSIONS": [
        "TOKEN", "EMAIL", "CLIENTE", "CREADO", "EXPIRA", "USADO"
    ],
    "RUTAS_DEFAULT": [
        "RUTA_ID", "ORIGEN_PAIS", "ORIGEN_CITY", "ORIGEN_ZIP",
        "DESTINO_PAIS", "DESTINO_CITY", "DESTINO_ZIP",
        "DIAS_ESTIMADOS", "ACTIVA"
    ],
    "PRODUCTOS_CATALOGO": [
        "CLIENTE", "ALIAS_INTERNO", "NOMBRE_INVOICE", "HS_CODE",
        "LARGO_CM", "ANCHO_CM", "ALTO_CM", "PESO_KG", "VALOR_USD_DEFAULT",
        "ACTIVO", "CREADO"
    ],
    "LOG_REQUESTS": [
        "TIMESTAMP", "CLIENTE", "ENDPOINT", "METHOD", "INPUT",
        "OUTPUT", "STATUS", "DURACION_MS", "IP"
    ],
    "LOG_ERRORES": [
        "TIMESTAMP", "CLIENTE", "ENDPOINT", "INPUT",
        "ERROR", "TRACEBACK", "RESUELTO", "NOTAS"
    ],
    "LOG_HEALTH": [
        "TIMESTAMP", "SERVICIO", "ESTADO", "DURACION_MS", "DETALLE"
    ],
    "LOG_JOBS": [
        "TIMESTAMP", "JOB", "ESTADO", "DURACION_S", "DETALLE"
    ],
}

# ── Datos iniciales para RUTAS_DEFAULT ──────────────────────
RUTAS_INICIALES = [
    ["AR-US",  "Argentina",      "Buenos Aires", "C1000", "Estados Unidos", "Miami",       "33166", 5, "TRUE"],
    ["US-AR",  "Estados Unidos", "Miami",        "33166", "Argentina",      "Buenos Aires","C1000", 5, "TRUE"],
    ["AR-ES",  "Argentina",      "Buenos Aires", "C1000", "España",         "Madrid",      "28001", 7, "FALSE"],
    ["AR-BR",  "Argentina",      "Buenos Aires", "C1000", "Brasil",         "São Paulo",   "01310", 4, "FALSE"],
]

# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  TAURO API — Inicializar hojas")
    print("=" * 60)

    if not SHEET_URL:
        print("❌ GOOGLE_SHEET_URL no está en .env"); sys.exit(1)
    if not os.path.exists(CREDS):
        print(f"❌ {CREDS} no encontrado"); sys.exit(1)

    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS, SCOPE)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SHEET_URL)
    print(f"📋 Conectado: {sh.title}\n")

    existentes = {ws.title for ws in sh.worksheets()}
    print(f"   Hojas actuales: {sorted(existentes)}\n")

    creadas = []
    actualizadas = []
    intactas = []

    for nombre, headers in HOJAS.items():
        if nombre in existentes:
            ws = sh.worksheet(nombre)
            row1 = ws.row_values(1)
            if row1 == headers:
                intactas.append(nombre)
                print(f"   ✓ {nombre}: ya existe con headers correctos")
            else:
                # No pisamos datos — solo avisamos diferencia
                print(f"   ⚠️  {nombre}: existe pero headers difieren")
                print(f"       actual:    {row1}")
                print(f"       esperado:  {headers}")
                actualizadas.append(nombre)
        else:
            ws = sh.add_worksheet(title=nombre, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
            creadas.append(nombre)
            print(f"   ➕ {nombre}: creada con {len(headers)} columnas")

    # Cargar rutas iniciales si RUTAS_DEFAULT recién se creó
    if "RUTAS_DEFAULT" in creadas:
        ws = sh.worksheet("RUTAS_DEFAULT")
        ws.update("A2", RUTAS_INICIALES)
        print(f"\n   📍 {len(RUTAS_INICIALES)} rutas iniciales cargadas")

    print("\n" + "=" * 60)
    print(f"  ✅ Resumen")
    print("=" * 60)
    print(f"  Creadas:        {len(creadas)}  {creadas}")
    print(f"  Con diferencias: {len(actualizadas)}  {actualizadas}")
    print(f"  Intactas:       {len(intactas)}  {intactas}")

if __name__ == "__main__":
    main()
