# Release candidate e-commerce: Shopify + Tiendanube

Fecha de corte local: 2026-09-25.

Este documento distingue **código listo para UAT** de **integración habilitada
en producción**. Ningún flag productivo, cuenta externa, app listing o tienda
se modifica por preparar este candidato.

## Núcleo compartido

- Pedidos normalizados e idempotentes por plataforma, tienda e ID externo.
- Conversión durable pedido → solicitud TAURO, sin emitir una guía sola.
- Salidas durables para fulfillment/tracking con reintentos, conciliación y
  revisión manual ante resultados ambiguos.
- Cancelaciones recibidas desde la tienda: sólo se resuelven localmente cuando
  todavía no existe ejecución logística; luego de emitir o ante ambigüedad se
  bloquea la automatización y se deriva a revisión manual.
- Secretos fuera del repositorio, webhooks firmados, privacidad y aislamiento
  por cliente.

## Shopify v1

- App pública embebida con App Home real en Shopify Admin.
- App Bridge se carga desde el CDN oficial antes de cualquier otro script; las
  lecturas del backend exigen ID/session token y no dependen de cookies de
  terceros.
- El backend valida firma HS256, `aud`, `iss`, `dest`, `exp` y `nbf`, y mapea
  el dominio a la instalación pública exacta antes de mostrar estado o pedidos.
- La UI embebida limita los pedidos a esa tienda y no expone PII innecesaria.
- Importa pedidos y catálogo; el cliente prepara la guía en TAURO.
- Publica fulfillment y tracking sólo cuando existe un único fulfillment order
  elegible. Multiubicación queda en revisión manual.
- No cotiza en checkout, no pide `write_shipping` y no usa CarrierService.
- Manifiesto y checks locales: `shopify_app/`.

## Tiendanube v1

- OAuth y webhooks operativos registrados por API.
- Webhooks de privacidad configurados por Partners en rutas separadas.
- Shipping API nacional con snapshots de cotización sin PII.
- Labels/OCA con claims, outbox, checkpoints, PDF protegido y cancelación
  fail-closed.
- Tracking exclusivamente por Fulfillment Orders. Si una escritura queda
  ambigua, el siguiente ciclo relee la FO; no existe fallback al POST legacy.
- Todos los gates permanecen apagados hasta tener credenciales y evidencia UAT.

## Gates antes de activar

### Comunes

- Aplicar `sql/schema.sql` en staging con backup y verificar una segunda
  aplicación idempotente.
- Completar pruebas de reinicio, replay, timeout y conciliación manual.
- Verificar métricas/alertas de outbox y procedimientos de soporte.

### Shopify

- Confirmar manifiesto en Dev Dashboard.
- Instalar en una development store limpia.
- Verificar App Home embebida, CSP por tienda, rechazo de ID tokens inválidos y
  retorno seguro a Admin después de OAuth/reinstalación.
- UAT: OAuth → pedido → portal → solicitud → guía de prueba → fulfillment y
  tracking; repetir webhook y simular timeout sin duplicar fulfillment.
- Obtener aprobación Level 2 para protected customer data.
- Obtener confirmación escrita de que el flete físico puede facturarse fuera
  de Shopify; si no, implementar App Pricing/Billing antes de la revisión.
- Preparar tienda demo activa, screencast en inglés/subtitulado, instrucciones
  del revisor y evidencia de los webhooks de compliance.

### Tiendanube + OCA

- Configurar en secreto credenciales y datos contractuales OCA propios.
- Completar App Partner/formulario del Platform Team, aprobar Shipping/Labels y
  confirmar que la tienda demo tenga un plan elegible para Labels.
- Cargar el bundle NubeSDK marcando su uso y registrar DevTools, video y
  evidencias de homologación.
- Configurar las tres URLs de privacidad en Partners.
- UAT: tarifa → pedido → aceptación → guía → PDF → tracking → cancelación.
- Recién con evidencia habilitar, en orden, gates de privacidad, Shipping,
  homologación, OCA QA/producción y worker de Labels.

## Fuera de alcance de este candidato

- Alta o publicación de las apps en los marketplaces.
- Deploy de producción.
- CarrierService/tarifas de checkout en Shopify.
- Adapters neutrales completos de Andreani, Correo Argentino, DHL y FedEx.
