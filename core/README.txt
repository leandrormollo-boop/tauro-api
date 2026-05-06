============================================================
  01_core — Clientes externos
============================================================

QUÉ HAY ACÁ
-----------
Wrappers para los 3 servicios externos que usa el API:

  fedex_client.py    - Comunicación con FedEx API (OAuth2 + rates)
  sheets_client.py   - Lectura/escritura de Google Sheets
  email_sender.py    - Envío de emails con PDFs adjuntos

LÓGICA Y DECISIONES
-------------------

[fedex_client.py]
- Patrón CarrierBase (clase abstracta) para que mañana podamos
  agregar DHL, UPS, etc. sin tocar el resto del código.
- OAuth2: token se renueva automáticamente cuando vence.
- get_rates(): cotización de INTERNATIONAL_PRIORITY.
- create_shipment(): NO IMPLEMENTADO (Phase 2 — generar guía real).
- track(): NO IMPLEMENTADO (Phase 2 — estado en vivo).
- Retry con backoff exponencial (3 intentos: 1s, 2s, 4s).
- Circuit breaker: si falla 3 veces seguidas, devuelve 503
  hasta que el job de health check confirme que volvió.

[sheets_client.py]
- Usa gspread con ServiceAccountCredentials.
- Lazy connection: se conecta solo cuando se usa la primera vez.
- Cache de hojas leídas durante 5 min (evita rate limit).
- Helper get_or_create_sheet(nombre) — crea hoja si no existe.

[email_sender.py]
- SMTP genérico (configurado en .env).
- enviar_email_pedido(): manda confirmación con PDF adjunto.
- enviar_alerta_margen(): aviso cuando margen < 10% (configurable).
- enviar_alerta_error(): NUEVO — manda email a admin si hay error 500.
- PDFs generados con ReportLab.

REGLAS
------
- NUNCA hacer llamadas externas fuera de esta carpeta.
- Si necesitás un nuevo servicio (ej: WhatsApp), creá nuevo cliente acá.
- Toda función debe tener retry + logging.
- Errores se propagan (raise) — el endpoint los loguea.
