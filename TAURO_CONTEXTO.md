# TAURO — CONTEXTO COMPLETO DEL SISTEMA

Última revisión documental: 2 de septiembre de 2026.

## 0. Alcance y fuente de verdad de este documento

TAURO es una plataforma B2B de logística para clientes y tiendas e-commerce. Reúne una web pública, un portal autenticado para clientes, un panel administrativo, una API B2B, conexiones con couriers, conexiones con Shopify y Tiendanube, y contabilidad operativa de envíos, facturas y pagos.

Este documento se relevó contra el checkout `.tmp/admin-courier-invoice-control`, commit `823b3bd` del repositorio `leandrormollo-boop/tauro-api`. Ese checkout sigue `origin/main` y, al momento del relevamiento, contiene la historia más reciente. La raíz `/Users/leanrmollo/Documents/TAURO` no es el checkout de la aplicación: es una carpeta de trabajo con auditorías, exportaciones y varios worktrees temporales. Por eso, para entender o modificar la aplicación no debe tomarse el `git log` de la raíz, que todavía no tiene commits.

La definición estructural canónica es `sql/schema.sql`. Los servicios son la fuente de verdad funcional. Los documentos antiguos, especialmente `docs/ARQUITECTURA_TAURO_API.md`, describen el MVP de Sheets/FedEx y no siempre representan producción.

## 1. Stack y estructura

### Stack

- Backend: Python, FastAPI y Uvicorn.
- Validación: Pydantic.
- Persistencia principal: PostgreSQL, accedido con psycopg2. Railway es el entorno de hosting documentado y posee la base productiva.
- Frontend del portal y admin: HTML renderizado en servidor con Jinja2, CSS y JavaScript propio.
- Web pública: React 18 compilado con esbuild a archivos estáticos, servido por la misma aplicación FastAPI.
- PDFs: ReportLab y PDFs devueltos por los couriers; etiquetas y facturas comerciales se almacenan como BYTEA cuando corresponde.
- Jobs: APScheduler y tareas en segundo plano dentro del proceso, con locks de PostgreSQL para impedir dobles ejecuciones.
- Correo: SMTP/TLS mediante transporte propio; recupero de contraseña, cotizaciones y notificaciones usan colas o envío controlado.
- Deploy: contenedor Docker y configuración Railway. La aplicación escucha el puerto provisto por el entorno.
- Integraciones externas: MyDHL API, FedEx API, UPS API en código parcial, Shopify Admin/Carrier Service y Tiendanube Shipping/Labels.

### Organización del repositorio de la aplicación

- `main.py`: crea FastAPI, monta routers, estáticos y scheduler; contiene web pública y API B2B.
- `core/`: PostgreSQL, clientes HTTP de DHL/FedEx/UPS, correo y compatibilidad con Sheets.
- `endpoints/`: routers del portal, admin, Shopify, Tiendanube e integraciones webhook.
- `servicios/`: reglas de negocio. Aquí viven pricing, cuenta corriente, conciliación, guías, tracking, tiendas, catálogo, seguridad y CRM.
- `modelos/`: modelos Pydantic de entrada y salida.
- `jobs/`: tareas periódicas, limpieza y sincronización.
- `sql/schema.sql`: esquema PostgreSQL acumulativo e idempotente, constraints, índices y triggers.
- `scripts/`: migraciones y herramientas operativas; algunas son históricas y destructivas, por lo que deben ejecutarse con preflight y backup.
- `templates/portal/`: portal de clientes.
- `templates/admin/`: panel interno TAURO.
- `static/`: CSS, JS, imágenes, PWA y recursos compartidos.
- `web/`: fuentes React de la web pública y estilos.
- `tests/`: pruebas unitarias, de integración y de contrato; incluye pruebas contra PostgreSQL temporal.
- `docs/`: arquitectura, operación, seguridad, integraciones y procedimientos.
- `tiendanube_nube_app/`: paquete auxiliar de la aplicación Tiendanube.

### Superficies

- Web pública: `/web` y las páginas públicas relacionadas. No requiere cuenta.
- Portal cliente: todo `/portal/*`. Requiere sesión, salvo login, recupero, PWA offline y callbacks de autenticación.
- Admin TAURO: todo `/admin/*`. Requiere cookie administrativa y, en el acceso normal, contraseña más TOTP.
- API B2B: `/cotizar`, `/pedido`, `/stock`, `/envios`, `/pedidos/*` y `/rastrear/*`; autentica con `X-API-Key`.
- Callbacks de plataformas: `/shopify/*` e `/integraciones/*`.

## 2. Modelo de datos

### Convención de tipos monetarios

El tipo correcto actual para dinero es NUMERIC, calculado en Python con Decimal. NUMERIC(14,2) guarda pesos o dólares a centavos; NUMERIC(14,4) guarda markups y valores comerciales con cuatro decimales; NUMERIC(18,4) y NUMERIC(18,6) se usan en conciliación para no perder precisión. `markup_pct` es REAL porque es porcentaje, no saldo. El script idempotente `migrar_dinero_numeric.sql` convierte instalaciones antiguas. La migración productiva fue completada y verificada el 2 de septiembre de 2026: ninguna de las columnas monetarias administradas por el script conserva `REAL` o `DOUBLE PRECISION`.

### Núcleo de clientes y acceso

`clientes`: cuenta comercial y tenant. `cliente_id TEXT` es la clave en mayúsculas; `email TEXT` es único; `api_key TEXT` es legado y debe quedar vacío, mientras la instalación vigente usa hash cuando está disponible; `password_hash TEXT` guarda bcrypt; `nombre`, `cuit`, `direccion`, `cp`, `ciudad`, `pais`, `telefono`, `notas` son datos fiscales y operativos; `activo BOOLEAN` habilita la cuenta; `test BOOLEAN` conserva cuentas de prueba sin incluirlas en dashboards, selectores ni listados operativos; `markup_pct REAL` es el porcentaje legado; `markup_tipo TEXT` y `markup_valor NUMERIC(14,4)` definen pricing internacional general; `markup_nac_tipo TEXT` y `markup_nac_valor NUMERIC(14,4)` definen pricing nacional; `puede_emitir` y `puede_recolectar` son permisos globales legados; `tope_deuda_ars NUMERIC(14,2)` bloquea emisión cuando el saldo excede el límite; `tax_paga` elige DESTINATARIO o CLIENTE; `courier_default` preselecciona courier; `created_at` audita alta.

`cliente_courier_config`: permiso y pricing por cliente/courier. Clave compuesta `cliente_id + courier`; `puede_cotizar`, `puede_emitir`, `puede_recolectar`; `markup_tipo`; `markup_valor NUMERIC(14,4)`; tramo bajo `markup_low_max_usd NUMERIC(14,4)` y `markup_low_ars NUMERIC(14,4)`; tramo alto `markup_high_min_usd NUMERIC(14,4)` y `markup_high_usd NUMERIC(14,4)`; timestamps. La ausencia de fila no autoriza operaciones.

`direcciones`: libreta aislada por cliente. `id`, `cliente_id`, `tipo` REMITENTE/DESTINATARIO, `alias`, `nombre`, `documento`, `email`, `telefono`, `direccion`, `ciudad`, `estado`, `cp`, `pais`, `predeterminada`, `notas`; linaje opcional `origen_plataforma`, `origen_dominio`, `origen_pedido_externo_id`; timestamps.

`sessions`: magic links/sesiones de cliente: `token`, `email`, `cliente_id`, `creado_at`, `expira_at`, `usado`.

`password_reset_tokens`: sólo persiste `token_hash` SHA-256, nunca el secreto; `cliente_id`, creación, expiración, confirmación de email y uso.

`password_reset_requests`: cola durable de recupero: `id`, `cliente_id`, `quote_id`, `estado`, `intentos`, próximo intento, claim y fecha, último código de error, message-id y timestamps. Estados: PENDIENTE, PROCESANDO, ENVIADO, FALLIDO, VERIFICAR_EMAIL.

`admin_recupero`: hashes SHA-256 de accesos de emergencia al admin: `token`, `vence`, `usado`, `creado`.

### Rutas, productos y stock

`rutas`: `ruta_id`, país/ciudad/CP de origen y destino, `dias_estimados`, `activa`. La ruta —no el courier— determina si un envío es nacional o internacional.

`productos`: catálogo por cliente. `id`, `cliente_id`, `alias_interno`, `nombre_invoice`, `hs_code`, dimensiones y peso como REAL, `valor_usd_default NUMERIC(14,2)`, `activo`, `imagen_url`; linaje de tienda (`plataforma`, `tienda_dominio`, IDs externos, SKU, título y variante); `precio_tienda NUMERIC(14,2)`, moneda, HS y país de origen informados por la tienda; stock controlado/disponible/comprometido/físico/entrante; timestamps de stock y fuente, `sync_run_id`, `sync_activo`, `created_at`.

`producto_inventario_ubicaciones`: stock por localización externa: `id`, cliente/producto, plataforma/dominio, ID y nombre de ubicación, cantidades disponible/comprometida/física/entrante y timestamps.

### Cotizaciones, envíos, cajas y guías

No existe una tabla independiente llamada `cajas`. Las cajas viven en `solicitudes_guia.bultos JSONB` y en snapshots. Para compatibilidad hay campos escalares `cantidad`, `peso_kg`, `largo_cm`, `ancho_cm`, `alto_cm`; en multibulto, cada objeto de `bultos` contiene cantidad, peso y dimensiones. La conciliación conserva el arreglo aceptado en `envio_cotizacion_snapshots.bultos`.

`cotizaciones`: log de precio. `id`, `coti_id`, cliente, ruta, ámbito, origen/destino ISO, courier y servicio; peso/dimensiones/peso facturable; `costo_fedex_usd NUMERIC(14,2)` después de migración; `markup_pct REAL`, `markup_tipo`, `markup_valor NUMERIC(14,4)`; `precio_final_usd` y `precio_final_ars NUMERIC(14,2)`; días, vigencia y creación.

`cotizaciones_web`: cotización pública persistida: IDs, referencia, peso/dimensiones NUMERIC, valor declarado USD NUMERIC, opción recomendada, resumen JSONB, creación y vencimiento.

`leads_cotizacion`: email asociado a cotización pública: `id`, email, origen, destino, peso, resumen, `cotizacion_id`, estado/intentos/error/claim/message-id del correo y timestamps.

`solicitudes_guia`: expediente operativo principal. Identidad: `id`, `cliente_id`, `estado`, `ambito`, `courier`, `servicio_courier`, ruta y cotización; `test BOOLEAN` preserva expedientes de QA sin exponerlos en el portal ni mezclarlos con listados operativos. Producto/cajas: alias, cantidad, peso, dimensiones, `bultos JSONB`, `valor_declarado_usd NUMERIC(14,2)` tras migración, seguro. Remitente y destinatario: alias, nombre/contacto, documento, email, teléfono, dirección, ciudad, estado, CP y país. Comercial: `precio_tauro_ars`, `precio_tauro_usd`, `precio_cliente_final_ars`, todos NUMERIC(14,2) tras migración; `tax_paga`. Resultado: tracking, URL, label BYTEA, commercial invoice BYTEA, fecha de generación, referencia/mensaje/error del courier. Idempotencia y linaje: plataforma/dominio/pedido externo, referencia API, hash de idempotencia y fingerprint. Contabilidad: `cargo_pendiente`, `cargo_error`. Tracking persistido: estado TAURO, estado courier, descripción, consultas, actualización/finalización y errores. Timestamps. Antes de lecturas operativas, el servicio cancela y audita automáticamente cualquier solicitud activa cuyo cargo ya esté `CANCELADO`.

`solicitudes_guia_reemisiones`: relación entre una guía anterior y su reemplazo o anulación: IDs, cliente, operación, trackings, campos modificados JSONB, motivo, estado; estado de riesgo y snapshot del tracking viejo; alerta/resolución, timestamps. Permite vigilar durante siete días que una guía reemplazada no empiece a moverse.

`recolecciones`: pickup: cliente, courier, fecha, ventana horaria, bultos, peso, dirección/instrucciones, estado, confirmación/ubicación, solicitud asociada, referencia/error courier y timestamps.

`envios`: libro de cargos a clientes, no el expediente logístico completo. `id`, `cliente_id`, fecha, `nro_fc`, `monto_ars NUMERIC(14,2)`, estado ACTIVO/CANCELADO/NC, descripción, tracking, PDF y nombre de factura, idempotency key, `solicitud_id` único, `ambito`, creación. Una guía genera como máximo un cargo. Una FC normalizada no puede duplicarse globalmente; NC no se trata como FC.

### Pagos, facturación y cuenta corriente

`pagos`: `id`, cliente, fecha, `monto_ars NUMERIC(14,2)`, método, referencia, nota, estado, comprobante BYTEA/tipo/nombre, idempotency key, origen y creación. Un pago informado por cliente nace PENDIENTE; uno cargado por admin nace APROBADO; RECHAZADO no impacta saldo.

`pagos_aplicaciones`: imputación explícita de un pago: `id`, `pago_id`, ámbito NACIONAL/INTERNACIONAL, `monto_ars NUMERIC(14,2)`, estado SOLICITADA/APLICADA y timestamps. La suma no puede superar el pago. Lo no aplicado queda como crédito sin imputar.

No existe una cabecera separada de “factura TAURO” ni una tabla de renglones de factura cliente. La factura de TAURO se representa hoy en `envios`: el admin asigna `nro_fc` y adjunta `factura_pdf` a un cargo individual. Por lo tanto el sistema actual no implementa una factura única que agrupe automáticamente varios envíos; el agrupamiento mensual es de presentación/selección, no un documento contable normalizado. Ésta es una limitación explícita del modelo actual.

### Conciliación de facturas courier y diferencias

`envio_cotizacion_snapshots`: foto inmutable al aceptar la cotización: solicitud/cotización/courier/servicio/moneda; `tipo_cambio_ars NUMERIC(18,6)`; costo estimado original y ARS, precio inicial y margen protegido `NUMERIC(18,4)`; regla aplicada; pesos `NUMERIC(12,3)`; bultos y origen de cálculo JSONB; fechas.

`facturas_courier`: cabecera documental: courier, FC/NC/ND, número crudo y normalizado, factura referenciada, fechas y período, moneda; subtotal/impuestos/total `NUMERIC(18,4)`; estado; mensaje o URI de evidencia, nombre/hash/PDF/MIME y metadatos JSONB; timestamps. Documento más número normalizado y hash impiden duplicados.

`facturas_courier_items`: líneas: factura, número de línea, tracking crudo/normalizado, código/tipo/descripcion de concepto, `importe`, tipo de cambio e `importe_ars NUMERIC(18,4)`, moneda, fecha de envío; pesos NUMERIC, base de peso, dimensiones y datos crudos JSONB, confianza NUMERIC, estado y timestamps.

`factura_courier_item_matches`: asignación total o parcial de una línea a una solicitud: montos original/ARS NUMERIC, método, confianza, estado PROPUESTO/CONFIRMADO/RECHAZADO, evidencia, actores, rechazo y timestamps.

`conciliaciones_envio`: cálculo versionado e inmutable: solicitud/versión/estado; precio inicial, costo estimado, margen protegido, costo real, precio final, ajuste, diferencia de flete y tax `NUMERIC(18,4)`; pesos `NUMERIC(12,3)`; base y motivo; versión/hash de fórmula, evidencias, indicador de evidencia completa, actores y timestamps. Estados: BORRADOR, PARA_REVISION, APROBADA, RECLAMADA, CERRADA, ANULADA.

`ajustes_cliente`: diferencia que puede trasladarse a la cuenta: conciliación/solicitud, DEBITO o CREDITO, monto/precio anterior/precio nuevo NUMERIC(18,4), estado PROPUESTO/APROBADO/APLICADO/ANULADO, idempotencia, motivo, actores, referencia y timestamps.

`auditoria_facturas_courier`: bitácora append-only con evento, IDs relacionados, actor, metadata JSONB y fecha. Los documentos financieros no se borran físicamente.

### Shopify

`shopify_instalaciones`: dominio, tokens cifrados y expiraciones, estado de reautenticación, scopes, cliente, carrier service, app/generación, webhooks y fecha.

`shopify_desinstalaciones`: identidad de instalación, cliente, fecha, purga y acuse shop/redact.

`shopify_shop_redact_pendientes`: cuarentena durable de redacción de tienda.

`shopify_pedidos_redactados`: tombstone de pedidos ya redactados.

`shopify_webhook_recibidos`: deduplicación de webhook por ID, dominio, topic, generación y fecha.

`shopify_huerfanos_cancelados`: tombstone de pedidos huérfanos cancelados.

`shopify_sync_estado`: estado y métricas de sincronización de catálogo.

`shopify_webhook_eventos`: outbox/cola con payload JSONB, estado, intentos y timestamps.

`shopify_gdpr_solicitudes`: solicitudes GDPR, recursos, estado, reintentos, notificación y resolución.

`config_envio_tienda`: política de tarifa mostrada por tienda: real/markup/fijo/gratis; `markup_pct NUMERIC(6,2)`, `precio_fijo_ars NUMERIC(14,2)`, visibilidad y tax default, etiqueta y fecha.

### Tiendanube

`tiendanube_instalaciones`: store, token cifrado, cliente, nombre, lifecycle/generación, webhooks, token de claim y fechas de suspensión/desinstalación/redacción.

`tiendanube_lifecycle_eventos`: deduplicación de eventos de ciclo de vida.

`tiendanube_webhook_eventos`: cola durable de webhooks con payload, estado, intentos y timestamps.

`tiendanube_privacidad_solicitudes`: pedidos de privacidad y su resolución.

`tiendanube_pedidos_redactados`: tombstones de pedidos.

`tiendanube_shipping_config`: hashes de callbacks, IDs de carrier/opción, activa y timestamps.

`tiendanube_labels`: pedido de etiqueta, payload, completitud, estado, operación externa y tracking.

`tiendanube_label_outbox`: cola de generación/cancelación con payload, completitud, estado, reintentos y tiempos.

### Configuración, salud, CRM y auditoría

`config`: clave/valor de configuración dinámica. Guarda dólar, markups públicos y otros parámetros administrables.

`salud_historial`: día y cantidad de fallos del centinela para `/estado`.

`security_audit`: evento, actor, IP, método, ruta, código, éxito, request-id, metadata y fecha; evita guardar secretos/PII innecesaria.

`crm_cuentas`: empresa prospecto, dominio/sitio/país/segmento/estado/fuente, score y breakdown, payloads de descubrimiento/investigación, modelo/response-id, exclusión y fechas.

`crm_contactos`: cuenta, nombre/cargo/email, verificación, fuente, principal/exclusión y fechas.

`crm_fuentes`: URL/título/evidencia/tipo y verificación por cuenta.

`crm_trabajos_agente`: cola de trabajos con tipo, cuenta, payload/resultado, estado/intentos/error/actor y fechas.

`crm_mensajes`: borrador y revisión de mensajes, modelos/response IDs, estado, aprobador/emisor, intentos/error y fechas. El envío requiere aprobación.

`crm_eventos`: auditoría CRM con entidad, actor, metadata y fecha.

## 3. Estados de un envío y transiciones

Hay dos estados relacionados pero distintos: `solicitudes_guia.estado` describe la operación, y `tracking_estado` resume el movimiento físico.

Estados operativos observados y soportados:

- SOLICITADO: se crea al confirmar el wizard, por API B2B o por una venta importada que se convirtió en solicitud. Lo dispara el cliente/API/automatización de tienda. Espera acción TAURO.
- EN_PROCESO: el admin toma o actualiza manualmente la solicitud. Sólo admin.
- EMITIENDO: reserva transaccional antes de llamar al courier, para impedir doble emisión. Automático al iniciar emisión desde portal habilitado o admin.
- VERIFICAR_COURIER: la llamada pudo haber creado una guía, pero TAURO no obtuvo confirmación segura. Automático ante timeout/respuesta incierta. Sólo admin puede resolver como emitida o no emitida; no se reintenta a ciegas.
- GUIA_LISTA: courier confirmó tracking/label, o admin cargó una etiqueta válida. Automático después de emisión o manual por admin. Al llegar aquí se crea el cargo en cuenta corriente una sola vez.
- DESPACHADO: admin marca que la pieza fue entregada al courier o avanzó. Sólo admin en el flujo actual.
- ENTREGADO: estado terminal visible. Puede marcarlo admin y el tracking diario DHL lo refleja automáticamente al detectar evento de entrega.
- CANCELADO: solicitud anulada. El cliente puede cancelar una DHL elegible desde el portal; el admin también. La cancelación exige confirmación del courier y revierte/cancela el cargo cuando corresponde. No se usa como simple borrado.
- REEMPLAZADO: la guía anterior fue cancelada y sustituida por otra. Lo dispara cliente/admin mediante el flujo de corrección DHL; crea vínculo de reemisión y monitoreo de riesgo.

Estados de tracking, independientes del estado operativo:

- PROCESO_ENTREGA: movimiento normal aún no terminal.
- RETENIDO: hold, demora aduanera, excepción, intento fallido, daño u otra señal de intervención.
- ENTREGADO: final; deja de consultarse automáticamente.

El cliente sólo puede emitir si su cuenta está activa, tiene permiso específico para ese courier, la integración está productiva y el saldo no supera `tope_deuda_ars`. El admin puede emitir solicitudes, editar datos antes de emisión, cargar etiqueta faltante y resolver incertidumbres. Los jobs nunca inventan transiciones financieras: actualizan tracking o recuperan colas bajo reglas idempotentes.

## 4. Pricing

### Portal/API autenticada

1. El courier devuelve costo en su moneda, normalmente USD para internacional.
2. Se obtiene `COTIZACION_DOLAR_ARS` desde `config`; si falta se usa la variable de entorno y finalmente un fallback histórico de 1450. El job de dólar oficial consulta la cotización oficial de venta, controla saltos máximos y actualiza config si `DOLAR_AUTO` está activo.
3. Costo ARS = costo USD × dólar.
4. Se busca primero pricing específico en `cliente_courier_config`; si no define regla, hereda el pricing general del cliente. Nacional usa su regla nacional explícita en los callbacks que exigen fail-closed.
5. Reglas base:
   - PCT: precio = costo ARS × (1 + porcentaje / 100).
   - FIJO_ARS: precio = costo ARS + fijo ARS.
   - MULTIPLICADOR: precio = costo ARS × factor, con factor mínimo 1.
6. Tramos por costo real USD, con límites estrictos: si costo < `markup_low_max_usd`, se suma `markup_low_ars`; si costo > `markup_high_min_usd`, se suma `markup_high_usd × dólar`; en los bordes y tramo medio se usa la regla base.
7. Se redondea el precio ARS al peso y el equivalente USD a dos decimales.

### Web pública

Tiene pricing separado. Prioridad: parámetros por courier `WEB_MARKUP_PCT_<COURIER>` en `config`, luego entorno, luego `WEB_MARKUP_PCT` global. También admite descuentos/lista y márgenes fijos específicos; un fijo explícito reemplaza el porcentaje, no se acumula encima. Hay guardas de margen mínimo. Este pricing no debe confundirse con el contrato de un cliente autenticado.

### Costo interno y momento de persistencia

El costo estimado se guarda al cotizar en `cotizaciones.costo_fedex_usd` y, cuando el cliente acepta/crea la solicitud, en `envio_cotizacion_snapshots` con moneda, dólar, costo original, costo ARS, precio y margen. El snapshot es inmutable. La solicitud guarda sólo los precios visibles/comerciales; los endpoints cliente no exponen costo ni markup. El costo final documentado se guarda después, en ítems de `facturas_courier`.

## 5. Conciliación courier

1. Admin entra a `/admin/conciliacion-couriers/nueva`, carga courier, tipo de documento, número, fechas, moneda, totales, PDF/evidencia e ítems. El flujo soporta FC, NC y ND; FC/ND suman costo y NC resta. No se puede duplicar número normalizado ni hash de archivo.
2. Cada línea conserva tracking, concepto, importe, tipo de cambio, importe ARS y pesos. Puede haber varias líneas para el mismo tracking: flete, combustible, impuestos, etc.
3. El matcher propone coincidencias por courier + tracking normalizado. Un match manual exige motivo/evidencia. Admin confirma o rechaza cada match; la suma asignada no puede exceder la línea.
4. Admin confirma la factura sólo con líneas controladas. Luego calcula la conciliación de cada solicitud contra su snapshot.
5. Fórmula: precio final cliente = costo courier real + margen TAURO protegido. Ajuste = precio final − precio inicial. Además se separa `diferencia_flete_ars` de `tax_cliente_ars`, cuya suma debe coincidir con el ajuste.
6. El cálculo crea un `ajustes_cliente` PROPUESTO y no toca el saldo. Admin revisa evidencia y aplica la diferencia. Si es positiva genera débito; si es negativa, crédito. Una nueva factura posterior sólo aplica el incremento respecto del último precio final cerrado.
7. La aplicación se vincula a la solicitud y por ella al cargo `envios`; es idempotente, auditada y no reescribe evidencia anterior.

## 6. Facturación a clientes

Al emitir una guía, TAURO registra automáticamente un cargo ACTIVO en `envios`, con `solicitud_id`, ámbito, tracking, descripción y el precio aceptado. Eso genera el “debe” incluso antes de que exista número fiscal.

El admin puede cargar un envío/cargo histórico o facturar un cargo desde `/admin/clientes/{cliente_id}/envios/{envio_id}/facturar`: asigna número de FC y PDF. La FC normalizada es única en toda la base y no se permite usar una NC como FC. Cancelar/anular conserva historia.

El modelo actual vincula una factura a un solo registro `envios`. No hay tablas `facturas_cliente` y `facturas_cliente_items`, por lo que no existe agrupación nativa de varios envíos bajo una cabecera única. Las pantallas pueden filtrar por mes y mostrar cargos pendientes de facturación, pero la emisión fiscal agrupada debe hacerse fuera del modelo o repetirse por cargo. Normalizar esta relación es deuda técnica prioritaria si TAURO necesita una factura mensual con muchos renglones.

## 7. Cuenta corriente

- Debe: cargos ACTIVO de `envios` más ajustes APLICADO positivos. Un cargo puede estar facturado (`nro_fc`) o pendiente de facturación; ambos integran deuda.
- Haber: pagos APROBADO efectivamente imputados y ajustes APLICADO negativos.
- Subledger nacional/internacional: cada cargo tiene `ambito`; cada aplicación de pago también. Se calculan dos libros separados y un consolidado, sin inferir ámbito por courier.
- Crédito sin imputar: parte de pagos aprobados que todavía no tiene filas APLICADA en `pagos_aplicaciones`. Reduce el consolidado económico, pero no se adjudica arbitrariamente a nacional o internacional.
- Cargos sin clasificar: cargos históricos con `ambito` nulo o inválido. Permanecen visibles aparte y requieren clasificación admin; no se reparten automáticamente.
- Pagos pendientes: comprobantes informados por cliente. Se muestran, pero no son haber hasta aprobación admin.
- Pendiente de facturación: cargos con número de FC vacío. No significa saldo cero; significa servicio debitado aún sin documento fiscal asociado.
- Tope de deuda: `clientes.tope_deuda_ars`. Si no es NULL y el saldo pendiente lo supera, el cliente no puede emitir nuevas guías aunque tenga permiso; TAURO/admin debe emitir o el cliente debe regularizarse.

Saldo consolidado = suma de cargos ACTIVO + suma de ajustes APLICADO − suma de pagos APROBADO. Para cada ámbito se resta sólo la porción APLICADA a ese ámbito. Las NC de `envios` y los cargos CANCELADO se excluyen. Los importes se cuantizan a centavos con Decimal.

## 8. Integraciones y estado real

- DHL Express: integración internacional implementada para cotizar, emitir, descargar etiqueta/factura comercial, pickup, cancelar y tracking. Sólo se considera operativa si hay API key/secret, cuentas de exportación e importación válidas y `DHL_ENVIRONMENT=production`. Tiene reemisión/cancelación segura y tracking diario. Es la integración productiva principal del portal.
- FedEx: cliente HTTP, cotización histórica y herramientas de tracking/auditoría existen, pero el catálogo multioperador la declara `pendiente`; no se habilita emisión/portal productivo desde `cliente_courier_config`. Faltan terminar/homologar emisión, cancelación, tracking y credenciales/UAT bajo el contrato nuevo.
- UPS: cliente y adapter parcial existen, con pruebas de emisión, pero el catálogo la declara pendiente. Faltan credenciales productivas, UAT y habilitación; no ofrece pickup en el contrato actual.
- Andreani: contrato nacional previsto para cotizar, emitir, etiqueta y tracking, pero no hay adapter operativo. Faltan API key, contrato, sucursales/operativas, payloads reales y homologación.
- OCA: existe adapter de cotización y contrato declarativo “operativo” en código, pero el cotizador nacional visible todavía devuelve APIs pendientes porque faltan usuario/password/CUIT/cuenta/operativa, contrato y aprobación productiva independiente. Emisión, etiqueta, cancelación y tracking no están declarados como capacidades actuales.
- Shopify: app pública ampliamente implementada: OAuth, tokens cifrados/refresh, carrier rates, webhooks de pedidos/productos/inventario, espejo de catálogo/stock, lifecycle, desinstalación y GDPR. Hay pruebas y documentación de publicación. Para cada instalación real faltan credenciales/app listing, scopes, webhooks verificados y homologación/App Store; la disponibilidad productiva depende del entorno y configuración, no sólo del código.
- Tiendanube: OAuth/lifecycle, webhooks, privacidad, carrier rates y contratos de labels están implementados en gran parte. Rates nacionales fallan cerrado si falta pricing nacional u OCA; generación de labels se encola y cancelación pública devuelve 503 hasta confirmación de una implementación segura. Faltan credenciales/registro de app, OCA productiva, homologación con tienda demo, material de listing y SLA aprobados.

## 9. Rutas HTTP

Los prefijos ya están incorporados debajo.

### Públicas y API B2B

- GET `/`: redirige al acceso principal.
- GET `/web`: web comercial React.
- GET `/meta-pixel.js`, `/styles.css`: recursos públicos.
- GET `/guias`, `/guias/{slug}`: índice y guía por país.
- GET `/calculadora-volumetrica`: calculadora de peso volumétrico.
- GET `/sitemap.xml`: sitemap.
- GET `/estado`: estado público del servicio.
- GET `/privacidad`, `/terminos`, `/ayuda/tiendanube`: legales y ayuda.
- GET `/health`, `/salud`: liveness y diagnóstico de salud.
- GET `/partners`, `/operadores`, `/paises`: catálogos públicos.
- GET `/api/rastrear`: tracking público acotado.
- POST `/cotizar-web`: cotización pública multicourier.
- POST `/cotizacion-lead`: solicita envío por email de cotización guardada.
- GET `/cotizacion/{quote_id}`: versión imprimible de cotización.
- POST `/cotizar`: cotización B2B autenticada.
- GET `/stock`: catálogo/stock paginado del tenant.
- POST `/pedido`: crea solicitud idempotente.
- GET `/pedidos/{solicitud_id}`: estado privado del pedido.
- GET `/envios`: historial B2B paginado y separado por ámbito.
- GET `/pedidos/{id}/guia.pdf`, `/pedidos/{id}/factura-comercial.pdf`: documentos propios.
- GET `/rastrear/{tracking}`: tracking privado por API key.

### Portal cliente (`/portal`)

- `/login` GET/POST, `/login/send`, `/auth`, `/logout`: acceso por contraseña o link y cierre.
- `/password/forgot`, `/password/reset` GET/POST: recupero seguro.
- `/manifest.webmanifest`, `/sw.js`, `/offline`: PWA del portal, sin cachear datos autenticados.
- `/home`: tablero y embudo.
- `/track`: redirección de búsqueda de tracking.
- `/backup.xlsx`: exportación de cuenta propia.
- `/recolecciones` GET, `/recolecciones/nueva`, `/recolecciones/{id}/cancelar`: pickups.
- `/cuenta`, `/pagos/informar`, `/pagos/{id}/comprobante`: cuenta y pagos.
- `/facturas/{envio_id}/pdf`: factura propia.
- `/cotizar` GET/POST, `/api/precio`, `/api/precio-multi`, `/api/parsear-pedido`: cotizador y helpers del wizard.
- `/envios`: listado; `/envios/nuevo` GET/POST: crear; `/envios/{id}`: detalle; `/envios/{id}/verificacion`: verificación; `/envios/{id}/emitir`: emitir; `/envios/{id}/cancelar`: cancelar/reemplazar; endpoints PDF: documentos.
- `/tienda`: integración e-commerce; `/tienda/reclamar`, `/politica`, `/conectar`, `/desconectar`: ownership/configuración; `/tienda/tiendanube/instalar`, `/tienda/tiendanube/pedidos`: Tiendanube; `/tienda/reiniciar-shopify`, `/limpiar-shopify-legado`, `/pedidos/descartar`, `/sincronizar-catalogo`: operación Shopify.
- `/clientes` GET/POST y `/clientes/{id}/eliminar`: destinatarios frecuentes.
- `/direcciones` GET/POST y `/direcciones/{id}/eliminar`: libreta completa.
- `/catalogo`, `/catalogo/add`, `/catalogo/eliminar`: productos.

### Admin (`/admin`)

- `/login` GET/POST, `/logout`, `/recuperar` GET/POST, `/recuperar/canjear`: autenticación.
- `/home`, `/seguridad`, `/bandeja`, `/bandeja/{cliente_id}`, `/backup.json`: operación y diagnóstico.
- `/clientes`, `/clientes/nuevo` GET/POST, `/clientes/{id}`, `/editar` GET/POST, `/password`, `/api-key`, `/acceso-precios` GET/POST: clientes y permisos.
- `/clientes/{cliente}/envios/{envio}/clasificar`, `/facturar` GET/POST, `/anular`: cuenta por cliente.
- `/envios/nuevo` GET/POST, `/envios/{id}/factura`, `/envios/{id}/cancelar`: cargos históricos/facturas.
- `/pedidos`, `/pedidos/{id}/estado`, `/resolver-courier`, `/liberar-reserva`, `/etiqueta`, `/editar` GET/POST, `/generar-guia`, `/guia.pdf`: solicitudes y emisión.
- `/guias-reemplazadas`, `/{id}/consultar`, `/{id}/cerrar`, `/{id}/reabrir`: vigilancia de reemplazos.
- `/tracking-fedex`, `/tracking-fedex/run`, `/tracking-fedex/reset`: tracking histórico FedEx conservado por compatibilidad, oculto del menú; sus errores públicos se sanitizan para no exponer rutas ni excepciones internas.
- `/recolecciones`, `/recolecciones/{id}/cancelar`, `/resolver`: pickups.
- `/pagos/nuevo` GET/POST, `/pagos/pendientes`, `/pagos/{id}/comprobante`, `/pagos/{id}/resolver`: pagos.
- `/conciliacion-couriers`, `/nueva` GET/POST, `/facturas/{id}`, `/pdf`, rutas de match manual/confirmar/rechazar/confirmar factura, `/envios/{solicitud}`, `/snapshot`, `/calcular`, `/diferencias/{ajuste}/aplicar`, `/conciliaciones/{id}/cerrar`: conciliación completa.
- `/productos` y aprobar/rechazar por ID; `/rutas` y alta/toggle; `/referencia`; `/config` GET/POST.
- `/migracion`, `/migracion/run`, `/migracion/numeric`: migraciones administrativas.
- `/importaciones-historicas`, `/importaciones-historicas/melcior`: importador histórico.
- `/shopify/privacidad` y sus rutas de descarga/resolución; equivalentes `/tiendanube/privacidad` y cuarentena.
- `/comercial` y rutas de cuentas, descubrimiento, investigación, propuesta, contacto, aprobación/envío/cancelación de mensajes: CRM.

### Callbacks externos

- Shopify: GET `/shopify/install`, `/shopify/callback`; POST `/shopify/tarifas`, webhooks GDPR y desinstalación.
- Integraciones Shopify legacy/operativas: POST `/integraciones/shopify/webhook` y topics orders create/update/cancel, products create/update/delete, inventory levels/items update.
- Tiendanube OAuth/webhook: GET `/integraciones/tiendanube/callback`, POST `/integraciones/tiendanube/webhook`.
- Tiendanube Shipping: POST `/integraciones/tiendanube/shipping/rates/{token}`, `/labels/{token}/generate`, `/labels/{token}/cancel`.

## 10. Pendientes y deuda técnica

- Facturación cliente sin cabecera/renglones: hoy una FC se adjunta a un cargo, no agrupa envíos.
- Ingreso de facturas courier desde correo: el módulo admin permite carga y conciliación, pero `docs/CONCILIACION_FACTURAS_COURIER.md` marca pendiente el conector que descarga automáticamente adjuntos de Gmail/Excel/Sheets y los ingresa al expediente.
- `api_key` legado coexiste con hash y el script de Sheets menciona `api_key_hash`, columna que debe verificarse en la base efectiva. Consolidar autenticación API y retirar texto claro.
- Tracking genérico en `servicios/rastreo.py` conserva un TODO para llamar `courier.track()`; DHL tiene job real aparte, pero FedEx/UPS genéricos no están cerrados.
- FedEx y UPS siguen marcados como integración pendiente en el catálogo actual, pese a existir clientes/adapters y tests parciales.
- Cotización nacional visible todavía es preparatoria: OCA/Andreani muestran “APIs pendientes”; Andreani no tiene adapter y OCA necesita credenciales/contrato/UAT.
- Cancelación de labels Tiendanube está deliberadamente bloqueada con 503; generación depende del outbox y del courier nacional.
- Checklist Tiendanube mantiene pendientes homologación real, listing gráfico, SLA y publicación.
- Privacidad de tiendas: la purga PostgreSQL está modelada, pero `docs/PRIVACIDAD_SHOPIFY_OPERACION.md` deja como decisión del responsable el tratamiento del Google Sheet histórico y advierte que copias/exports quedan fuera de la purga automática.
- Shopify requiere completar/verificar publicación productiva por instalación y secretos; los documentos advierten que código listo no equivale a app aprobada.
- Dominio/DNS: la documentación antigua indica que `taurosolutions.ar` seguía en Hostinger y Railway esperaba cambio. No hay evidencia local suficiente para afirmar el estado actual; debe verificarse externamente.
- SMTP, SPF, DKIM, DMARC, alias operativo y smoke productivo figuraban pendientes en agosto. No hay evidencia local concluyente de cierre.
- Backups: la política pide activar snapshots y probar restauración; no consta una restauración productiva reciente.
- Worktrees temporales: hay muchas ramas/checkouts bajo `.tmp` con divergencias. Deben archivarse o documentarse para evitar desarrollar sobre una versión vieja.
- Documentación histórica contradictoria: ROADMAP y arquitectura MVP todavía dicen Sheets, Render o features faltantes que ya existen. Deben rotularse o retirarse.
- Campos escalares y `bultos JSONB` duplican cajas; se necesita una tabla `bultos` si se requieren consultas, constraints o conciliación por caja.
- Estados de `solicitudes_guia.estado` no tienen un CHECK SQL único. Hoy la lista vive repartida entre servicios/UI; conviene centralizarla y validar transiciones en DB o una máquina de estados.
- La web pública y el portal tienen reglas de pricing distintas y varios fallbacks de entorno/config; conviene publicar una matriz formal y alertar configuraciones heredadas.
- Algunos “mock” encontrados están sólo en tests; no son deuda productiva. Los placeholders de formularios son ejemplos de UX, tampoco mocks funcionales.

### Migración Sheets → PostgreSQL

`scripts/migrate_sheets_to_postgres.py` es one-time y migra PERFILES→clientes, RUTAS_DEFAULT→rutas, PRODUCTOS_CATALOGO→productos, PAGOS→pagos, CONFIG→config, sesiones no vencidas y ENVIOS 2026→envios. Usa UPSERT/controles y requiere credenciales Google más DATABASE_URL. `docs/CONTEXTO_RAPIDO_TAURO.md` registra que se ejecutó y que PostgreSQL reemplazó a Sheets como fuente principal. No debe repetirse a ciegas: puede actualizar datos y depende de headers históricos. Sheets permanece como fuente/importador para tareas históricas, no libro maestro.

### Migración REAL → NUMERIC

`scripts/migrar_dinero_numeric.sql` convierte columnas monetarias de pagos, envíos, cotizaciones, solicitudes, productos y clientes a NUMERIC(14,2)/(14,4), sólo si aún son REAL. Es idempotente. El 2 de septiembre de 2026 se verificó producción: todas las columnas monetarias previstas ya eran NUMERIC salvo `productos.valor_usd_default`. Como Railway Hobby no permite snapshots nuevos, se generaron un `pg_dump` custom restaurable, el JSON del servicio de backup y una tabla puntual de los valores afectados. Luego se ejecutó el script: `productos.valor_usd_default` quedó `NUMERIC(14,2)`, no cambió ningún valor y quedaron cero columnas monetarias flotantes. Evidencia, hashes y postflight están en `docs/MIGRACION_NUMERIC_ACTA.md`.

## 11. Historial cronológico resumido

- 24–29 abril 2026: reorganización por capas; portal inicial, modelos/servicios, Sheets, login mágico, pricing por cliente y preparación de deploy.
- Mayo–junio: migración a PostgreSQL, admin CRUD, API B2B, solicitudes de guía y portal conectado a la nueva base. Sheets dejó de ser la fuente principal.
- 10–11 agosto: UX de portal, números ES/EN, seguridad de errores DHL, API/Postman Shopify, web pública con copy comercial y certificación.
- 17 agosto: autogestión y entradas inteligentes; producción/diagnósticos DHL y pickups; separación inicial nacional/internacional.
- 18 agosto: PWA segura del portal, recupero de contraseña, email transaccional verificable, identidad de correo e infraestructura multioperador.
- 24 agosto: preflight monetario por ámbito y cierre de constraints/aplicaciones de pagos.
- 26–27 agosto: plataforma multioperador integrada; API operativa idempotente/paginada; catálogo y stock Shopify; migración a app pública, GDPR, lifecycle y limpieza segura.
- 28 agosto: integración productiva DHL, pricing por tramos USD, multibulto y rediseño de cotizador/wizard.
- 30 agosto: núcleo de conciliación de facturas courier con snapshots, matches, diferencias y auditoría financiera.
- 31 agosto: tracking DHL diario, temas del portal y preparación de app Tiendanube nacional.
- 1 septiembre: conciliación endurecida; factura courier por envío; separación de diferencia/tax; búsqueda y filtros mensuales/semanales; anulación, corrección y reemplazo seguro de guías DHL; mejoras visuales y branding Tiendanube.
- 2 septiembre: verificación inline del envío, seguro y valores por caja, factura comercial y guía PDF unificadas, cotizador en modal, detalle de cuenta corriente organizado e importación histórica de MELCIOR por mes. También se completó en producción la última conversión monetaria pendiente, `productos.valor_usd_default` de REAL a NUMERIC(14,2), con backups y comparación fila por fila.
- 2 septiembre, limpieza operativa: se conservaron cuatro cuentas como `test`, se cancelaron las solicitudes #2, #3 y las pruebas de WAIMAO, y se reparó con auditoría la guía `9802908161` cuyo cargo ya estaba cancelado. Dashboard, bandejas y selectores pasan a excluir datos de prueba; Tracking FedEx queda oculto y con errores sanitizados.

Este historial resume los commits visibles de `origin/main`; no atribuye autoría personal cuando el commit no la documenta. Para una auditoría exacta se debe consultar `git log --date=iso` y el diff de cada hash.
