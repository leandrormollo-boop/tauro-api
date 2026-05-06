============================================================
  TAURO API — Visión general
============================================================

QUÉ ES
------
API + Portal de cotización y envíos internacionales (FedEx).
Diseñado para que tiendas e-commerce o clientes B2B coticen
y generen guías sin tener que hablar con un humano.

Modelo: B2B white-label.
- La tienda llama a nuestra API (X-API-Key) → cotiza/genera guía.
- El comprador final no nos ve a nosotros, ve a la tienda.
- Cada cliente tiene su MARKUP_% configurado en Sheets.

PRINCIPIOS DE DISEÑO
--------------------
1. Sheets como base de datos (no servidor SQL).
   - Backups automáticos de Google.
   - Vos editás directo si hay un error.
   - Cero dependencia de infraestructura propia.

2. Logging total a Sheets.
   - Cada request se loguea (LOG_REQUESTS).
   - Cada error se loguea con traceback (LOG_ERRORES).
   - Lo abrís desde el celular.

3. Validación estricta para evitar errores de tipeo.
   - Cotización: dropdowns de ruta (no escribe país/ciudad/zip).
   - Guía: dropdown de catálogo del cliente (no escribe HS Code).

ESTRUCTURA DE CARPETAS
----------------------
Orden lógico de lectura (cómo fluye un request):

  1. endpoints/    Rutas FastAPI — punto de entrada (/cotizar, /portal/*, /admin/*)
  2. modelos/      Schemas Pydantic — validación de inputs/outputs
  3. servicios/    Lógica de negocio — saldos, catálogo, cotizador, auth
  4. core/         Clientes externos — FedEx, Sheets, Email
  5. jobs/         Tareas programadas — reporte semanal, health check
  6. templates/    HTML del portal del cliente (Jinja2)
  7. static/       CSS, JS, imágenes
  8. scripts/      Scripts auxiliares (no parte del API)
  9. docs/         Documentación detallada

CADA CARPETA TIENE SU README.txt explicando qué hace y la lógica.

FLUJO TÍPICO DE UN REQUEST
--------------------------
  Cliente → endpoints/ → modelos/ (valida) → servicios/ (lógica) → core/ (FedEx/Sheets) → response

CÓMO CORRER
-----------
1. cd "API TAURO"
2. source venv/bin/activate
3. uvicorn main:app --reload
4. Abrir http://localhost:8000

ENDPOINTS PRINCIPALES
---------------------
PÚBLICOS (con X-API-Key):
  POST /cotizar          - Cotizar envío
  POST /pedido           - Crear pedido (futuro: generar guía)

PORTAL DEL CLIENTE:
  GET  /portal/login     - Login mágico por email
  GET  /portal/home      - Saldo + últimos envíos
  GET  /portal/cotizar   - Form de cotización con dropdowns
  GET  /portal/catalogo  - Catálogo de productos del cliente
  GET  /portal/guia      - Generar guía (Phase 2)

ADMIN (solo vos):
  GET  /admin            - Panel principal
  GET  /admin/health     - Estado de servicios
  GET  /admin/errores    - Log de errores con replay

HOJAS DE GOOGLE SHEETS
----------------------
Ver 09_docs/ESTRUCTURA_SHEETS.txt para detalle de columnas.

  CONFIG               - Parámetros globales
  PERFILES             - Clientes (API_KEY, MARKUP_%, EMAIL)
  COTI                 - Histórico de cotizaciones
  PEDIDOS              - Pedidos generados
  PRODUCTOS_CATALOGO   - Catálogo por cliente (alias, HS code, medidas)
  RUTAS_DEFAULT        - Rutas predefinidas para cotización rápida
  PAGOS                - Pagos recibidos (cuenta corriente)
  SESSIONS             - Sesiones de login mágico
  LOG_REQUESTS         - Log de todas las requests
  LOG_ERRORES          - Log de errores con traceback

PRÓXIMOS PASOS
--------------
Ver 09_docs/ROADMAP.txt
