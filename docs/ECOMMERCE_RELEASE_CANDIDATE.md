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
- Migración bloqueante en Railway antes del proceso web. OAuth, callbacks y
  workers sólo hacen lecturas/DML y fallan cerrado si `/health` detecta un
  esquema parcial; no crean ni alteran tablas durante tráfico.
- La migración predeploy cifra los tokens OAuth históricos que aún estuvieran
  en texto plano. El runtime sólo acepta el formato cifrado y falla cerrado si
  encuentra un token legacy sin migrar.

## Shopify v1

- App pública embebida con App Home real en Shopify Admin.
- App Bridge se carga desde el CDN oficial antes de cualquier otro script; las
  lecturas del backend exigen ID/session token y no dependen de cookies de
  terceros.
- El backend valida firma HS256, `aud`, `iss`, `dest`, `exp` y `nbf`, y mapea
  el dominio a la instalación pública exacta antes de mostrar estado o pedidos.
- Cada OAuth crea una generación nueva **sin dueño TAURO**. La tienda sólo se
  vincula cuando un cliente autenticado reclama el dominio y una consulta
  GraphQL confirma que su email coincide con `shop.email` o
  `shop.contactEmail`; antes de escribir, se vuelve a validar bajo lock la
  generación exacta para impedir carreras con una reinstalación.
- Después de verificar HMAC, cada webhook se contrasta con el recurso remoto
  visible para esa tienda antes del dedupe global. Para órdenes se usa además
  el estado remoto actual: un evento viejo de alta/actualización no puede
  revivir una orden ya cancelada, anulada o reembolsada.
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
- Shipping y Labels exigen que callback, instalación activa, mapping TAURO,
  dueño e `install_generation` pertenezcan al mismo contexto. Un OAuth nuevo
  invalida la configuración anterior y la reconciliación rota los tokens de
  callback cuando el contexto remoto no coincide.
- Tracking exclusivamente por Fulfillment Orders. Si una escritura queda
  ambigua, el siguiente ciclo relee la FO; no existe fallback al POST legacy.
- Todos los gates permanecen apagados hasta tener credenciales y evidencia UAT.

## Gates antes de activar

### Comunes

- Ejecutar `python scripts/migrate_database.py` en staging con backup (Railway
  ya lo declara como `preDeployCommand`) y verificar una segunda aplicación
  idempotente antes de levantar el proceso web.
- Completar pruebas de reinicio, replay, timeout y conciliación manual.
- Verificar métricas/alertas de outbox y procedimientos de soporte.
- Confirmar que el predeploy dejó todos los tokens OAuth históricos cifrados;
  no habilitar tráfico si readiness detecta datos legacy incompatibles.

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
- Cerrar los cuatro puntos externos de la
  [autoevaluación del App Store](SHOPIFY_APP_STORE_SELF_REVIEW.md): decisión de
  billing para el flete físico y verificación TLS del entorno desplegado.

### Tiendanube + OCA

- Configurar en secreto credenciales y datos contractuales OCA propios.
- Completar App Partner/formulario del Platform Team, aprobar Shipping/Labels y
  confirmar que la tienda demo tenga un plan elegible para Labels.
- Cargar el bundle NubeSDK marcando su uso y registrar DevTools, video y
  evidencias de homologación.
- Configurar las tres URLs de privacidad en Partners.
- Los secretos de Shipping/Labels viajan en el path de sus callbacks. Uvicorn
  ya inicia con `--no-access-log`, pero antes de activarlos también se debe
  deshabilitar o redactar el request-path logging del edge/proxy de Railway.
  Luego hay que rotar por OAuth/reconciliación cualquier callback legacy que
  haya podido quedar registrado en logs.
- UAT: tarifa → pedido → aceptación → guía → PDF → tracking → cancelación.
- Recién con evidencia habilitar, en orden, gates de privacidad, Shipping,
  homologación, OCA QA/producción y worker de Labels.

## Fuera de alcance de este candidato

- Alta o publicación de las apps en los marketplaces.
- Deploy de producción.
- CarrierService/tarifas de checkout en Shopify.
- Adapters neutrales completos de Andreani, Correo Argentino, DHL y FedEx.
