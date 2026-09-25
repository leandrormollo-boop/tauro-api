# OCA en el portal autenticado

## Flujo implementado

`Nuevo envío > Nacional` y `Cotizar > Nacional` abren `/portal/oca/nuevo`. El cliente completa retiro, destinatario, cantidad de bultos iguales, peso, medidas y valor declarado en ARS. Se consulta OCA y se guarda una cotización privada con vencimiento de 15 minutos. El navegador sólo recibe el precio al cliente, nunca costo ni margen.

Guardar solicitud crea la fila en `solicitudes_guia` y el snapshot de costo/precio/margen en una sola transacción PostgreSQL. Un doble clic devuelve el mismo ID. La solicitud aparece en Mis envíos y en el admin. Todavía no genera cargo.

Emitir reutiliza el bloqueo de crédito por cliente, permisos y reserva atómica del portal. Vuelve a validar configuración, vencimiento y tarifa OCA. Un cambio exige una cotización nueva. Se persiste el remito antes del POST y la orden/tracking antes de recuperar el PDF. El cargo idempotente se registra en Nacional con el precio TAURO aceptado. Si la emisión es incierta, queda VERIFICAR_COURIER sin reintento automático. Los PDFs pendientes se recuperan desde el detalle sin emitir nuevamente. El seguimiento se consulta por la API desde una pantalla autenticada.

## Admin y activación

Acceso y precios incluye OCA con cotización/emisión opt-in por cliente y pricing propio PCT, MULTIPLICADOR o FIJO_ARS. No hereda márgenes internacionales ni tramos USD. No habilita cuentas automáticamente y no cambia permisos existentes de DHL.

La configuración del portal exige `OCA_ENVIRONMENT=production`, configuración válida del adapter y los flags de aprobación/habilitación. Requiere `OCA_CUIT`, `OCA_CUENTA`, `OCA_USUARIO`, `OCA_PASSWORD`, `OCA_OPERATIVA`, `OCA_CENTRO_COSTO`, `OCA_ORIGIN_MODE=domicilio`, `OCA_DESTINATION_MODE=domicilio`, `OCA_ADAPTER_ENABLED`, `OCA_UAT_APPROVED`, `OCA_PRODUCTION_APPROVED`, `OCA_FULFILLMENT_ENABLED`, `OCA_FULFILLMENT_UAT_APPROVED`, `OCA_CONFIRM_WITHDRAWAL`, `OCA_OPERATIVA_SEGURO_CONFIRMADO` y el valor de `OCA_OPERATIVA_ASEGURADA` confirmado por OCA. El formato de etiqueta se elige con `OCA_LABEL_FORMAT`. Las credenciales se cargan en el gestor de secretos, nunca en Git.

La cuenta QA y el usuario test de OCA se rechazan en el portal autenticado. QA conserva el launcher independiente de `OCA_PORTAL_QA.md`.

## Base de datos y publicación

`sql/schema.sql` extiende el CHECK de operadores de `cliente_courier_config` y agrega `oca_portal_cotizaciones`. No borra registros. La tabla de cotizaciones contiene direcciones y debe seguir la política de acceso/backups del resto de solicitudes. La migración fue probada en PostgreSQL local; no se ejecutó sobre producción.

Publicar el código y activar OCA son pasos diferentes. El usuario autorizó publicar esta integración. Las credenciales productivas ya fueron validadas y guardadas en Railway; GetOperativasByUsuario confirma 472095 con ConValorDeclarado=true y 472096=false, y se confirmó centro 1. La publicación mantiene adapter/emisión desactivados y no habilita clientes. Para activar operaciones reales faltan completar UAT de emisión productiva y definir tarifa/permisos por cliente. La evidencia real de QA valida PaP sin seguro, no acredita la emisión productiva asegurada.

## Límites de esta versión

Sólo puerta a puerta, flujo normal y grupos de bultos con iguales medidas/peso. No incluye sucursales, logística inversa, conversión automática de pedidos Shopify/Tiendanube ni anulación productiva con ajuste contable. La anulación de guías OCA emitidas sigue requiriendo gestión de TAURO; no se ofrece un botón que cancele sólo el cargo local. No modifica el seguimiento automático diario DHL. Para corregir una solicitud OCA antes de emitir, crear una cotización nueva; las ediciones genéricas se bloquean para evitar discrepancias con el payload congelado.

## Validación

Resultado: **331 pruebas aprobadas** (24 avisos de deprecación existentes).

Pruebas del adapter, integración HTTP con sesión, PostgreSQL real aislado (solicitud + snapshot + cargo), concurrencia sobre la misma guía y sobre el límite de dos guías, acceso ajeno, cuenta inactiva, permisos, vencimiento, cambio de tarifa/configuración, precio alterado, respuesta incierta y recuperación de PDF. Regresiones DHL, catálogo y admin incluidas.

La revisión visual local recorrió formulario, cotización, guardado y detalle en la sesión OCA-DEMO. La Mac se bloqueó durante la confirmación visual de emisión; la emisión y su cargo se validaron automáticamente sobre PostgreSQL aislado con transporte simulado. La API real de OCA QA fue validada en el piloto previo (orden 20908665 anulada, seguimiento código 54).

El preflight de Tiendanube exige ahora fulfillment_ready de OCA además de capacidades declaradas, para que el código de emisión no sustituya sus controles de configuración y UAT.


## IVA de la tarifa contractual
El usuario confirmó que el tarifario OCA es neto. El portal agrega 21% de IVA una sola vez al costo y al precio de venta, después del margen neto. WAIMAO: neto OCA × 1,20 × 1,21. El costo persistido incluye IVA para compararlo con el importe final cobrado. La huella de configuración invalida cotizaciones previas sin IVA; no se reescriben cargos históricos. Las integraciones ajenas al portal conservan su política actual.
