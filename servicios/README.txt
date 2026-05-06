============================================================
  04_servicios — Lógica de negocio
============================================================

QUÉ HAY ACÁ
-----------
Acá vive TODA la lógica del negocio. Los endpoints solo orquestan
(reciben request → llaman al servicio → devuelven response).

  cuenta_corriente.py   - Cálculo de saldos y movimientos
  catalogo.py           - Catálogo de productos por cliente
  rutas.py              - Rutas predefinidas (cotización rápida)
  cotizador.py          - Lógica de cotización + markup
  auth.py               - Login mágico (token + sesión)
  logger.py             - Log estructurado a Sheets
  health.py             - Chequeo de salud de servicios externos
  circuit_breaker.py    - Anti-cascada cuando FedEx se cae

LÓGICA Y DECISIONES
-------------------

[cuenta_corriente.py]
- saldo(cliente) = SUM(facturado en ENVIOS 2026) - SUM(pagado en PAGOS)
- movimientos(cliente, desde, hasta): timeline mezclado de FCs + pagos
- Lectura cacheada 60 segundos (evita rate limit en refrescos del portal).

[catalogo.py]
- get_productos(cliente): filtra PRODUCTOS_CATALOGO por CLIENTE + ACTIVO
- agregar_producto(cliente, datos): valida HS Code + manda email a Tauro
  para revisión antes de marcarlo como ACTIVO=TRUE.
- Estructura: alias interno (UI), nombre invoice (aduana), HS code,
  medidas default, valor default.

[rutas.py]
- get_rutas_activas(): RUTAS_DEFAULT donde ACTIVA=TRUE.
- get_ruta(ruta_id): devuelve origen_zip, origen_city, destino_zip, etc.
- El cliente NO ingresa zip/ciudad — solo elige ruta del dropdown.

[cotizador.py]
- cotizar(cliente, ruta_id, peso, dim):
    1. Lee ruta de RUTAS_DEFAULT
    2. Llama fedex_client.get_rates() con ZIP/ciudad de la ruta
    3. Calcula peso volumétrico = (L×A×H)/5000
    4. Toma el mayor entre real y volumétrico
    5. Aplica MARKUP_% del cliente (de PERFILES)
    6. Loguea en COTI
    7. Devuelve precio final USD + ARS (con cotización del día)

[auth.py]
- generar_token(email): crea token random, lo guarda en SESSIONS
- enviar_link_magico(email): manda email con tauro.app/portal/auth?token=X
- validar_token(token): devuelve cliente o None
- Tokens expiran en 7 días.
- Cookie de sesión httponly, secure (en prod).

[logger.py]
- log_request(endpoint, cliente, input, output, duracion_ms, status)
- log_error(endpoint, cliente, input, exception)
- Escritura async — no bloquea la response del endpoint.
- Si Sheets está caído, fallback a print() local.

[health.py]
- check_fedex(): verifica OAuth token válido
- check_sheets(): lectura ping en CONFIG
- check_smtp(): conexión TCP al servidor
- check_cron(): última ejecución del job semanal

[circuit_breaker.py]
- Si fedex.get_rates() falla 3 veces en 60 seg → CIRCUITO ABIERTO
- En estado abierto, /cotizar devuelve 503 con mensaje amigable
- Cada 60 seg intenta cerrar (half-open) con un test request
- Si pasa, vuelve a CERRADO.

REGLAS
------
- Servicios NO conocen FastAPI (no importan Request, Response).
- Reciben argumentos planos, devuelven objetos/dicts.
- Toda llamada externa va por 01_core/.
- Servicios pueden llamarse entre sí (cotizador usa rutas + circuit_breaker).
