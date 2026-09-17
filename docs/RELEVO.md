# RELEVO — estado y reglas del proyecto (act. 17/09/2026)

Este documento existe para que CUALQUIER agente (Codex, Claude, humano) pueda
retomar el trabajo sin contexto previo. Leelo entero antes de tocar código.
Regla general: **este repo despliega solo a producción en cada push a main**
(Railway, https://taurosolutions.ar) — no hay staging. Compilá, testeá con
mocks y verificá producción después de cada push (patrón abajo).

## 17/09/2026 — Publicación autorizada de navegación y solapas históricas

Rama `codex/navigation-history-20260917`, base `c1ad266`. Cotizador, nueva guía
internacional y formularios operativos del Admin conservan el avance al volver.
Borradores por cuenta/flujo en la pestaña, cuatro horas de inactividad; tarifas
recalculadas, archivos se vuelven a adjuntar. Acuse de éxito limpia sólo el
borrador guardado y evita reenviarlo al regresar desde la caché del navegador.
Mis envíos y Cuenta corriente separan Todos / Modificados / Cancelados. Los dos
últimos conservan historia sin cargo; no se enmascara deuda histórica incoherente.
Cancelación Admin atómica entre solicitud/cargo, pagos conservados, protección
de facturas del módulo actual y bloqueo de cambios de estado que eludan el
circuito de anulación o reemplazo. Sin migraciones ni escrituras en producción.
2.356 pruebas + 5 subpruebas y 8 pruebas JS aprobadas; recorridos locales con
datos ficticios y comprobación móvil. Modelo efectivo Astra por preferencia del
usuario; piso del router Sol/high. Sin delegación. El usuario autorizó esta
versión con «ok publica», después de aclarar el alcance de los borradores.
Se publica `1db8340` sobre `c1ad266`, junto con este registro de aprobación;
verificar Railway, recursos, sesiones reales y saldos sin operaciones de prueba.
Ver criterios y evidencia en
[Navegación y envíos históricos](NAVEGACION_Y_ENVIOS_HISTORICOS.md).

## 17/09/2026 — Publicación autorizada del control gerencial y vigilancia DHL

Rama `codex/admin-control-20260917`, base `d88ffc08`. Nuevo `/admin/home`:
atención operativa, rentabilidad por cliente y mes, cobertura de costos,
cuentas corrientes y vigilancia. Márgenes confirmados separados de estimados;
costos desconocidos nunca se consideran cero. Totales Decimal/SQL, lectura
coherente sin reparación automática de estados, créditos separados de deuda.
Retenidos DHL: campo persistente `tracking_vigilancia_desde`, migración y
readiness; siguen vigilados al liberarse hasta entrega/cancelación/reemplazo.
Dos rondas, por defecto 05:20 y 17:20 Argentina, ventanas por media jornada y
lock compartido entre workers. No confundir “not delivered” con entrega.
Listas paginadas de vigilancia y excepciones con totales completos.
2.345 pruebas + 5 subpruebas aprobadas; QA local 1280/390/320 con datos ficticios.
El usuario autorizó esta versión con «ok perfecto. publicalo» después de revisar
la vista local. Se publica `6a776c4` sobre `d88ffc08`, sin divergencia con main,
junto con este registro de aprobación. Verificar Railway, readiness, panel
autenticado, saldos actuales y los dos jobs; no generar envíos o pagos de prueba.
Modelo efectivo Astra por preferencia del usuario y riesgo de
arquitectura, por encima del piso del router. No hubo delegación.
Detalle, criterios contables, límites y próxima etapa:
[Control del negocio y vigilancia](CONTROL_NEGOCIO_Y_VIGILANCIA.md).

## 17/09/2026 — Publicación autorizada de identificación de envíos y cancelados

Rama `codex/shipment-identity-20260917`, base `d2f5887`. El usuario pidió
identificar cada envío y hacer visible CANCELADO sin costo en ambas pantallas.
Mis envíos y Cuenta corriente: destinatario, referencia TAURO, ruta y tracking;
filas separadas, tarjetas en móvil. Cancelados/reemplazados sin importes también
en el detalle. La cuenta incorpora historia informativa sin impacto contable,
con filtro propio; nunca oculta un cargo activo por una inconsistencia de estado.
Sin migración ni cambios de saldo. 2.324 pruebas + 5 subpruebas aprobadas;
QA visual local en ambos temas y tamaños 1280/390/320. El usuario autorizó
«publicalo». Se publica `7414c12` sobre `d2f5887`, sin divergencia con main;
verificar Railway, salud y ambas vistas en la sesión existente de WAIMAO.
Detalle: [Identificación y cancelados](IDENTIFICACION_ENVIOS_Y_CANCELADOS.md).

## 17/09/2026 — Publicación autorizada de textos y ayudas contextuales

Rama `codex/portal-copy-help-20260917`, base `3ca332d`. Se simplificó la
redacción de 40 plantillas y de la web pública. Ayudas ⓘ junto a conceptos y
campos; operan por puntero, teclado y clic. Importes, avisos de aprobación,
errores y acciones requeridas siguen visibles. Sin cambios de negocio ni DB.
Bundle público recompilado, versión 17. Suite completa: 2.318 pruebas y
5 subpruebas aprobadas. QA local sin escrituras reales.
El usuario autorizó publicar con «publicalo». Se publica el cambio `2a9088c`
sobre `3ca332d`, sin divergencia. Comprobar Railway, salud, recursos y ayudas
en las sesiones existentes; sin operaciones reales de prueba.
Detalle y criterio editorial: [Textos y ayudas](TEXTOS_Y_AYUDAS_PORTAL.md).

## 16/09/2026 — Confirmación visible de recolecciones

Rama `codex/pickup-confirmation-20260916`, base publicada `d2fe4a1`.
Publicación autorizada por el usuario: «publicalo». Se publica `31a7f5b`
sin divergencia con main. Verificar salud, CSS y comprobante real de WAIMAO
tras el despliegue, sin crear ni cancelar retiros.
La reserva aparece con número del courier, fecha, horario del origen y dirección
en Recolecciones y en el detalle de la guía. Mis envíos muestra su estado y
evita invitar a programar un retiro existente. El retorno tras crear conserva
la guía y abre su comprobante; los éxitos se contrastan con el registro propio.
Ante una lectura fallida o una reserva incierta no se ofrece otro formulario.

DHL ya devuelve `dispatchConfirmationNumbers` y se verificó una reserva real
en lectura; no es el tracking ni prueba de retiro físico. Se endureció la
validación de respuestas malformadas sin alterar reservas ni reintentos.
Sin migraciones, dependencias nuevas ni operaciones reales de prueba.
2.318 pruebas y 5 subpruebas aprobadas; QA visual en escritorio/celular y ambos
temas. Detalle: [Confirmación de recolecciones](RECOLECCIONES_CONFIRMACION.md).

## 16/09/2026 — Publicación autorizada del portal, HS, DHL y numeración

El usuario autorizó expresamente «publica todo». Esta autorización comprende
las cuatro entregas `5cd977b`, `df32b56`, `468a59e` y `8c94d8b`, sobre
`origin/main` `2249e48`, sin divergencia. Reemplaza el estado pendiente de las
notas de preparación siguientes. Incluye la migración aditiva de numeración;
no habilita emisiones de prueba, pagos ni cambios de importes históricos.

Validación del código que se publica: 2.300 pruebas y 5 subpruebas aprobadas,
sin fallos ni omisiones, con PostgreSQL aislado. El despliegue se verifica por
el estado Railway del commit, salud de aplicación y base, recursos publicados
y sesiones reales de WAIMAO y administrador. Referencia previa: saldo visible
de WAIMAO $2.855.643 y 610 registros en conciliación. La secuencia comienza
en 50300 y no se reinicia en despliegues posteriores. No se emiten guías reales
para verificar la publicación. Shopify y Tienda Nube conservan pendiente su
piloto real de extremo a extremo.

## 16/09/2026 — Descargas con cuenta, origen y número TAURO desde 50300

Rama `codex/guide-download-names-20260916`, basada en `468a59e` y con todas
las entregas locales previas. El cliente descarga
`TAURO - WAIMAO - DESTINATARIO - CN - 50300.pdf`. Se usa la cuenta y el
país de origen del envío. Número interno global, estable en reintentos y
descargas, distinto del tracking. Históricos sin número se numeran al
descargarlos; las nuevas guías, al guardar la confirmación del courier.

Migración aditiva: columna nullable, secuencia desde 50300 e índice único.
No reiniciar la secuencia. 2.300 pruebas y 5 subpruebas aprobadas, incluidas
24 regresiones nuevas y pruebas concurrentes con PostgreSQL aislado.
**Pendiente de publicación; sin migraciones ni escrituras en producción.**
Detalle: [Nombre y numeración de descargas](NOMBRES_DESCARGA_GUIAS.md).

## 16/09/2026 — Motivos accionables al emitir con DHL

Rama `codex/dhl-emission-errors-20260916`, basada en `df32b56`; conserva las
entregas previas del portal y HS. Propaga motivos de recotización y validación,
traduce errores MyDHL al español con campo y corrección, informa caja/totales/
diferencia de invoice, elimina truncamiento y carteles duplicados. Ante una
respuesta incierta se conserva el bloqueo de emisión y la verificación.

2.276 pruebas y 5 subpruebas aprobadas; 39 regresiones nuevas. Sin migraciones,
dependencias nuevas ni operaciones reales. **Pendiente de publicación.**
Detalle: [Errores DHL](DHL_ERRORES_ACCIONABLES.md).

## 16/09/2026 — Asistente HS en catálogo e invoice

Rama `codex/hs-code-assistant-20260916`, basada en `5cd977b` e incluyendo toda
la entrega del portal pendiente de publicar. Búsqueda automática de HS6 desde
la descripción, selección explícita y preguntas por familia. Base local de
5.612 códigos, derivada de USITC 2026 R19 (dominio público en su catálogo) y
contrastada con el universo H6 de Naciones Unidas. No es un clasificador
aduanero general ni completa extensiones nacionales automáticamente.

2.237 pruebas y 5 subpruebas aprobadas. Sin migraciones, nuevas dependencias,
llamadas IA pagas ni cambios en operaciones reales. **Pendiente de publicación.**
Detalle: [Asistente HS](HS_CODE_ASISTIDO.md).

## 16/09/2026 — Portal y control de facturas preparados localmente

El usuario autorizó implementar las mejoras de la auditoría. Rama
`codex/portal-readiness-20260916`, base `2249e48`. **Pendiente de autorización
para publicar esta nueva entrega.** Sin migraciones ni dependencias nuevas.

Incluye continuidad cotización→solicitud, remitente coherente con origen,
legibilidad móvil/claro, pagos a cuenta con vinculación documental opcional,
incidencias de seguimiento, Excel con precio inicial explícito y control admin
paginado con filtros, expedientes y recuperación ante fallos de red.
No altera las reglas de acreditación ni los importes históricos.

Validación: 2.202 pruebas y 5 subpruebas aprobadas, sin fallos ni omisiones,
PostgreSQL aislado; revisión de navegador con datos simulados y escrituras
bloqueadas. Cinco pruebas de Excel repetidas después del ajuste final.
Detalle, fuentes y límites: [Preparación del portal](PORTAL_READINESS_20260916.md).
Shopify/Tienda Nube aún necesitan piloto real; no se incorporan ramas ajenas.
La aprobación de publicación del apartado siguiente corresponde al 15/09,
no a estos cambios nuevos. Push a main continúa siendo despliegue automático.

## 15/09/2026 — Publicación autorizada del portal y cuenta corriente

El usuario autorizó expresamente la salida a producción: «publicalo hermano».
Se publican los cambios revisados `42e1be4`, `9186952` y `ad9bbd3`, sobre
`origin/main` `5f91386`, sin divergencia ni migraciones. Incluye experiencia
del portal, cuenta corriente y control visible de WAIMAO desde septiembre.
Esta autorización reemplaza las solicitudes de aprobación de las notas previas.

Validación previa: 124 pruebas de cuenta aprobadas (PostgreSQL aislado incluido),
240 pruebas previas de experiencia del portal, sintaxis compatible con Python
3.11, nueve plantillas Jinja y ambos recursos JS válidos. La comprobación del
despliegue debe contrastar los recursos publicados con Git y verificar salud,
sesión real de WAIMAO, saldo sin cambios, corte, filtros y exportación propia.
El chequeo se realiza sin emitir guías ni informar pagos. Las notas siguientes
conservan el estado de preparación anterior a esta autorización.

## 15/09/2026 — WAIMAO comienza el control visible desde septiembre

El usuario pidió quitar los envíos anteriores a septiembre de la cuenta del
cliente para ordenar el control. Se implementa un corte de **presentación**
desde el 01/09/2026, sólo para WAIMAO: lista, Excel, gráfico y pagos recientes.
El saldo anterior se agrupa por separado, calculado con estados actuales.
El saldo real, las facturas pendientes, los documentos pagables, la emisión y
los registros históricos se conservan. No es una cancelación de deuda ni un
cierre histórico auditado. Se mantiene pendiente de publicar, sin migraciones.
Validación conjunta: 124 pruebas aprobadas con PostgreSQL sintético real y
revisión del navegador. La base local de pruebas quedó apagada al finalizar.
Detalle y límites: [Cuenta corriente](CUENTA_CORRIENTE_EXPERIENCIA.md).

## 15/09/2026 — Cuenta corriente: operación y diseño del cliente

Rama `codex/portal-experiencia-cliente`. **Preparada localmente, pendiente de
publicar; sin migraciones ni dependencias nuevas.** Alcance y limitaciones:
[Experiencia de cuenta corriente](CUENTA_CORRIENTE_EXPERIENCIA.md).

- Saldo consolidado, vencimientos con pago parcial y acceso a informar el
  documento propio, seguimiento de pagos y cupo monetario cuando está configurado.
- Pendientes y rechazados visibles sin acreditar dinero. El crédito sin imputar
  ya está descontado del consolidado. No se inventan fechas de aprobación ni se
  publican notas internas como motivos de rechazo.
- Gráfico de seis meses por fecha contable: cargo en su fecha, ajuste aplicado
  en su fecha de Argentina, créditos negativos; sin duplicar el costo final.
- Filtros por guía/factura/referencia, ámbito y fecha. Respuesta parcial sin
  recalcular paneles, historial conservado ante fallos y Excel de la consulta
  propia hasta 10.000 filas, sin truncamiento ni fórmulas en textos del usuario.
- Diseño con las variables y tipografías TAURO, temas claro/oscuro y controles
  adaptados a móvil. Lecturas autenticadas; no cambia emisión ni aprobación.
- Validación enfocada: 26 pruebas de portal y 50 de servicios/cuenta aprobadas,
  con PostgreSQL sintético real en la segunda ronda. Los conjuntos se superponen.
  Cierre ampliado: 99 aprobadas y 4 omitidas (ya cubiertas con PostgreSQL).
  Revisión visual y funcional local en escritorio y celular, temas claro/oscuro.
  Preview ficticio `/portal/cuenta` en puerto 8772; PostgreSQL de pruebas apagado.
  Pendiente verificación autenticada de esta versión tras su publicación.

## 14/09/2026 — Experiencia del cliente: revisión WAIMAO y mejoras locales

Rama `codex/portal-experiencia-cliente`, base `origin/main` `5f91386`.
**Preparada para revisión, sin publicar.** El usuario pidió analizar WAIMAO
y avanzar con las mejoras de operación y diseño. El checkout previo
`dhl-invoice-items` (tracking-claridad-cliente) y `portal-paquetes` se conservan.
Informe: [Experiencia del portal](EXPERIENCIA_PORTAL_CLIENTE.md).

- Estado principal coherente en inicio/lista/detalle. Cancelado, reemplazado
  y entrega operativa confirmada prevalecen; en los demás casos se presenta
  el seguimiento reconocido. No se reescriben estados históricos.
- Separación de retenidos y entregados en filtros y contadores. En tránsito
  reemplaza el texto ambiguo Proceso de entrega; mensaje del courier y hora
  de sincronización visibles. Se mantiene la consulta DHL diaria.
- Inicio más compacto con la pieza original del avión, búsqueda visible,
  contadores y actividad antes de cuenta. Fechas y resumen desplegables,
  año 0 eliminado, importes no repetidos, anulaciones fuera del total visible.
- Ver envío y PDF prioritarios; Verificar/Repetir en Más opciones. Retiro
  accesible desde todas las guías internacionales elegibles, sin ofrecerlo
  cuando el seguimiento ya informa movimientos. Enlace de recolecciones
  corregido al filtro `tipo=internacional&paso=guia_lista`.
- Campos legibles y etiquetas completas en móvil. Contraste del cotizador
  claro corregido. CSS adicional limitado al cuerpo del portal autenticado.
- Filtros devuelven fragmento con `X-Tauro-Partial: envios`; navegación GET
  normal sigue disponible sin JS. Timeout de 15 s y errores conservan la lista.
  Historial, desplazamiento y foco se recuperan al filtrar.
- Validación final: 240 passed, 1 skipped (PostgreSQL aislado no configurado),
  1 deselected (husos horarios Windows; misma falla probada en `5f91386`).
  Pruebas de invoice, cotización, cuenta, paquetes, retiros, repetición,
  cancelación, presentación y filtrado. JS y diff check correctos.
- Navegador con datos ficticios: escritorio/móvil, oscuro/claro, filtros,
  búsqueda global, volver atrás, acciones y formulario hasta invoice con
  artículo adicional. No se emitieron guías ni se consultaron tarifas reales.
- Respuestas locales HTML: reducción 35,6% con diez filas y 68,7% con una.
  No se midió todavía latencia de producción ni de DHL. Se observaron dos
  avisos de Chrome por transición omitida; sin bloqueo de navegación.
- Ejecución por el agente principal, sin delegación. Controles: trabajo
  aislado, registros de prueba, sin secretos, sin cambios en facturación,
  reservas de emisión, credenciales, migraciones o dependencias.

Prioridad operativa pendiente: contrastar el aviso de retención del último
envío WAIMAO con los eventos DHL. Esta mejora no confirma esa retención ni
cambia el clasificador del carrier; no inferir ubicación física por un
evento de despacho aduanero. Confirmar autorización antes de publicar y
seguir la verificación de `/salud` y recorridos autenticados en producción.

Nota de continuidad: la invoice de varios artículos ya está incluida en
`origin/main` (`da36e53`) y visible en el portal revisado el 14/09. La entrada
del 09/09 más abajo conserva la foto anterior a su publicación.

## 14/09/2026 — Publicación de embalajes guardados y tarifas de tienda

Rama `codex/paquetes-preguardados`, basada en `origin/main` `da36e53`.
Desarrollo `b7929e3`. **Publicación autorizada por el usuario el 14/09/2026**:
«ok, publicalo. Luego lo probamos si está ok». El piloto real queda pendiente;
esta publicación no activa CarrierService ni modifica cuentas, productos,
permisos o ventas reales de Pesca Jacks. Comprobar salud, archivos estáticos,
acceso autenticado a la sección y camino crítico después del despliegue.
Detalle y procedimiento: [Paquetes y tarifas](PAQUETES_Y_TARIFAS.md).

- Nueva sección `/portal/paquetes`: embalajes, asociaciones por variante,
  combinaciones físicas confirmadas, políticas por tienda y simulador.
- Un mismo motor determina cajas, contenido, peso bruto y volumen para
  portal, API, Shopify, Tiendanube nacional y solicitudes de ventas.
  Sin combinación confirmada se usan las cajas individuales; no se infieren
  dimensiones a partir de la suma de volúmenes. No emite guías automáticamente.
- El usuario solicitó ofrecer tarifas en checkout el 11/09. Esto actualiza
  la decisión histórica de retirar CarrierService de la regla 6: el endpoint
  anterior sigue retirado y la nueva integración es opcional por tienda,
  con permiso `write_shipping`, secreto ligado a instalación y generación,
  CCS habilitado y activación explícita. No se activa una tienda existente.
- Precios de checkout en ARS y centavos Shopify, también internacional;
  políticas tarifa real, porcentaje, fijo o gratis. Se mantienen separados
  el precio de la cuenta TAURO y el cobrado al comprador.
- Nacional depende de operadores habilitados, cobertura y credenciales.
  OCA sólo cotiza cuando pasa su configuración/homologación; su emisión
  sigue pendiente. Tiendanube mantiene el alcance nacional existente.
- Migración idempotente `sql/paquetes.sql` incluida también en `schema.sql`.
  Sin dependencias Python nuevas. Tablas y consultas separadas por cliente.
- Validación aislada: 392 pruebas aprobadas de paquetes, Shopify, Tiendanube,
  invoices, ámbitos y no fuga de precios. Incluye PostgreSQL 17 local real
  para persistencia, migración repetida, claves de cliente y reintentos de
  CarrierService simulados. Tras los últimos ajustes, 18 pruebas enfocadas
  de callback y PostgreSQL volvieron a pasar.
- Navegador con datos ficticios: asociación/combinación persistida,
  1 reel en 15×15×10 cm y 1 reel + 2 señuelos en una caja mediana;
  política gratis nacional y tarifa internacional con importes simulados.
  No se usaron credenciales ni transportistas reales en la validación.

Antes de activar Pesca Jacks: confirmar sus embalajes, pesos y capacidades
físicas; autorizar tarifas y agregar TAURO a las zonas de Shopify; revisar
operadores; probar checkout, pedido recibido y correspondencia con cajas.
Guardar una política en el portal no reemplaza esos pasos de la tienda.

## 09/09/2026 — Invoice DHL con varios artículos por caja (historial anterior)

Rama local `codex/dhl-invoice-items`, basada en `origin/main` `f60b8a2`.
**Todavía no publicada.** El usuario ingresó en la cuenta real de WAIMAO.
Se verificó el formulario internacional en la sesión autenticada: la versión
publicada conserva un único artículo por caja y no tiene el botón de agregar
artículos. No se guardaron solicitudes ni se emitieron guías reales.

- El paso 4 permite agregar y quitar artículos dentro de cada tipo de caja:
  descripción, unidades, precio USD, HS, país de fabricación y peso neto
  total del artículo. Para varias cajas iguales, las unidades comerciales
  representan el contenido de todas esas cajas, igual que en el contrato previo.
- `bultos[].items_invoice` conserva la lista completa en el JSONB existente;
  no requiere migración. Los campos escalares conservan el primer artículo
  para compatibilidad. Cotización, alta, repetición, reemisión, verificación
  y detalle preservan todos los artículos. Los adaptadores sin soporte no
  pueden emitir una declaración truncada: esta modalidad requiere DHL.
- DHL recibe una `content.packages` por caja física y una
  `exportDeclaration.lineItems` por artículo. Se validan cantidades, valores,
  país, HS, peso neto y límite de 100 artículos por envío. Los importes se
  validan con Decimal contra el valor total declarado; el seguro conserva
  ese total. No se modificaron reservas, referencias, reintentos ni cargos.
- Cada artículo nuevo informa su propio peso neto, sin repetir el peso
  de la caja. [MyDHL API](https://developer.dhl.com/api-reference/dhl-express-mydhl-api)
  permite enviar sólo netValue por línea (contrato desde 3.0.0;
  adaptador actual 3.3.1).

Validación local, Python 3.12 / Windows, sin DB ni APIs reales:

- 136 pruebas de invoice, DHL, seguridad financiera, piloto WAIMAO y
  reemisión aprobadas, incluidas 30 nuevas pruebas de varios artículos.
- Suite completa: 1881 aprobadas, 133 omitidas, 3 fallos y 4 errores
  (antes de agregar las dos pruebas finales de rechazo de otros couriers).
  Comparación contra `f60b8a2` sin cambios: 1853 aprobadas y exactamente
  los mismos fallos/errores, por rutas Windows, zonas horarias, CRLF y
  parámetros de pytest que exceden el tamaño de una variable de entorno.
- Navegador con datos sintéticos WAIMAO: una caja de 4 kg, 4 camisas ×
  USD 25 y 2 pantalones × USD 50 = USD 200. Verificados agregar/quitar
  artículos, agregar/quitar otra caja sin perder el contenido y POST con
  ambos artículos. Las tarifas del preview son simuladas.

Pendiente: autorización de publicación y verificación del formulario real
con sesión de WAIMAO tras el deploy. Un push a `main` dispara producción.

## Reglas de negocio INVIOLABLES

Actualización publicada en producción el 04/09/2026 (`1c458dc`): ver
[Operadores logísticos](OPERADORES_LOGISTICOS.md). Nueva vista por operador y
cliente, vencimientos y libro separado de pagos/NC proveedor con reversas
auditadas. No interpretar estados históricos como pagos/deudas confirmados.

1. **Dos superficies de cotización, nunca mezclarlas.**
   - Cotizador WEB (`/web`, `POST /cotizar-web`): público, sin login. Precio
     de vidriera (descuento web + margen por carrier del admin). JAMÁS expone
     el pricing de un cliente ni el costo/ganancia de TAURO (hubo una fuga
     así: commit 18d8007).
   - PORTAL (`/portal/*`, logueado): cada cliente ve SU precio con SU regla
     (`clientes.markup_tipo/valor`, y `markup_nac_*` para nacional).
   - El mismo paquete puede valer distinto en ambas superficies: correcto.

2. **Discreción del canal operativo.** Mientras se negocian cuentas directas,
   los envíos reales salen por un proveedor mayorista. El nombre de ese
   proveedor NO se escribe en ningún campo visible al cliente (las
   `observaciones` de solicitudes SE MUESTRAN en el portal). El alta se hace
   por admin → "+ Cargar envío realizado" (`cargar_envio_externo`).

3. **Emitir guía cuesta plata real y NO es idempotente.**
   `create_shipment` va con `max_retries=1` y reserva atómica
   (`UPDATE … WHERE tracking IS NULL RETURNING`, estado `EMITIENDO`).
   Tocar ese flujo = riesgo de guías dobles facturadas.

4. **La cuenta corriente debita sola al emitir** (`cargar_guia_emitida`,
   idempotente por índice único sobre `envios.solicitud_id`). El cargo es
   `precio_tauro_ars` (lo que TAURO cobra al cliente), NUNCA
   `precio_cliente_final_ars` (lo que el cliente cobra a SU comprador).

5. **Pagos informados por el cliente NO tocan el saldo hasta que el admin
   los aprueba** (`pagos.estado`: PENDIENTE→APROBADO/RECHAZADO;
   `total_pagado()` suma sólo APROBADO; NULL legacy = aprobado).

6. **total_price hacia Shopify va en CENTAVOS (×100). NO tocar** —
   verificado tres veces contra la doc y producción. La app de Shopify ya NO
   cotiza en el checkout (decisión de producto): `POST /shopify/tarifas`
   devuelve `{"rates": []}` a propósito. Su único trabajo: recibir la venta
   (webhook) → `servicios/solicitud_automatica.py` arma la solicitud sola.
   La guía NUNCA se emite sola.

7. **Peso facturable = max(peso real, L×A×H/5000) SIEMPRE** (regla del dueño).
   En carritos de tienda las dimensiones salen del catálogo cruzando el SKU
   contra `productos.alias_interno` (comparación en MAYÚSCULAS ambos lados).

8. **⚠️ ANTES de pasar `FEDEX_ENVIRONMENT=production`**: recalibrar
   `WEB_DESC_FEDEX_PCT` (hoy calibrado contra tarifas de sandbox; ver
   comentario largo en `servicios/carriers.py`) — si no, se vende bajo costo.
   La cache de tarifas guarda `entorno` y se invalida sola en el switch.

9. **Contenido público de aduana**: sólo datos verificados con fuente y
   fecha (`servicios/guias_pais.py`). Dato clave vigente: EEUU eliminó el
   de-minimis de USD 800 el 29/08/2025 — todo envío comercial paga aranceles.

10. **Toda protección va EN EL HANDLER, no en middleware**:
    `core/security.py` NO está montado (dead code local, fuera del repo).
    Ejemplos vivos: `leer_comprobante_con_tope` (8 MB), `validar_comprobante`
    (tipo por firma de contenido, no extensión).

## Disciplina de trabajo (pedida por el dueño)

El dueño pidió estructurar el trabajo en los 11 pasos de la Kabalah
(Keter→Maljut). Traducción operativa mínima: (1) decir qué problema de
negocio resuelve antes de codear; (2) leer el código real y medir producción
antes de afirmar; (3) toda feature nueva sale con su restricción gemela
(tope/validación/idempotencia); (4) reconocer errores en voz alta; (5) nada
está "hecho" hasta estar desplegado y verificado. En tareas grandes,
decirle al dueño en qué paso se está.

## Cómo verificar un deploy (patrón usado siempre)

```bash
# tras el push, esperar ~90s y:
for i in 1 2 3 4 5 6 7 8; do sleep 22; curl -s -o /dev/null -w "[%{http_code}] " -m 10 https://taurosolutions.ar/salud; done
.venv-codex/bin/python scripts/test_checkout_critico.py   # 20+ chequeos contra prod
```
Tests locales: usar `.venv-codex/bin/python` (el venv principal está roto).
Templates: validar con Jinja2 antes de push. JSX de la web pública: validar
con `node -e "require('./node_modules/esbuild').transformSync(...,{loader:'jsx'})"`
y bumpear `?v=N` en `web/Tauro Solutions.html` (Babel standalone, sin build).

## Estado al 03/08/2026

VIVO en producción: portal completo (cuenta corriente con débito automático,
comprobantes con verificación, emisión por cliente con permiso+tope,
RECOLECCIONES, Excel por cliente, catálogo con precio por unidad, login por
email o ID), admin completo (bandeja, edición pre-emisión, pisar tracking con
flag, carga de envíos externos, pagos por verificar, margen por ámbito,
recolecciones del día), app Shopify (venta→solicitud automática, ventas
huérfanas rescatadas al vincular), web pública (cotizador FedEx+DHL vivos,
guías por país, calculadora volumétrica, /estado, captura de leads), espejo a
Google Sheet (apagado hasta `GOOGLE_CREDENTIALS_JSON`).

**La spec original de 23 puntos está COMPLETA.** Los tres couriers
internacionales (FedEx, DHL, UPS) cotizan, emiten guías y trackean; el
despachador de emisión es un registro (`generar_guia_internacional`) donde
sumar un courier es una línea, porque todo el armado del envío se comparte.
Suite: 92 tests, `.venv-codex/bin/python -m pytest tests/ -q` — **sin
`--ignore`**. `test_security.py` y `test_email_security.py` salieron del repo
el 03/08: importaban `core/security.py`, que no está trackeado, así que
explotaban al colectar y se venían salteando a mano en cada corrida.

### Actualización API B2B — 27/08/2026

- `POST /pedido` acepta `Idempotency-Key`; un retry concurrente devuelve la
  misma solicitud y una clave reutilizada con otro body responde `409`.
- `GET /pedidos/{id}` y `GET /pedidos/{id}/guia.pdf` permiten consultar y
  descargar la guía con API key, siempre filtrando por dueño.
- `GET /envios` devuelve el historial paginado y filtrable por ámbito/estado,
  sin mezclar nacional con internacional ni publicar costos internos.
- `GET /rastrear/{tracking}` consulta sólo envíos propios e intenta refrescar
  DHL/FedEx/UPS; si el courier falla conserva el estado TAURO sin filtrar el
  error interno.
- Shopify Pesca Jacks: el OAuth exige también `read_locations` porque el espejo
  muestra el nombre de cada depósito. La vinculación OAuth confirmada puede
  migrar una tienda histórica de `TEST_CLIENT` al dueño real, mientras que el
  alta manual sigue sin poder apropiarse de dominios ajenos. La tabla durable
  de pedidos huérfanos ahora se crea en la migración base antes de leerla.
- Shopify TAURO pública: las instalaciones nuevas usan
  `SHOPIFY_PUBLIC_API_KEY` / `SHOPIFY_PUBLIC_API_SECRET`; el par genérico
  histórico sigue validando Pesca Jacks durante la reautorización. Cada token
  registra qué app lo emitió y un webhook tardío de desinstalación de la app
  vieja no puede borrar una instalación nueva. Los tokens offline públicos son
  expirables, se guardan cifrados junto con su refresh token y rotan de forma
  atómica antes del vencimiento.
- Una instalación no queda operativa hasta verificar por GraphQL el conjunto
  exacto de webhooks de esa generación. La migración deja los bindings OAuth
  históricos inactivos y obliga a una reautorización única; no se opera con
  suscripciones incompletas ni con evidencia heredada.
- La Admin API de Shopify se usa exclusivamente por GraphQL. Se eliminó el
  helper REST y el CarrierService retirado; TAURO no agrega cargos ni modifica
  el orden de opciones del checkout.
- El portal ya no pide escribir un dominio para instalar Shopify: la
  instalación empieza en una superficie de Shopify y OAuth identifica la
  tienda. Los reintentos de pedidos instalados siguen usando el dominio ya
  verificado por el servidor.
- Cada solicitud creada desde una venta conserva en su INSERT inicial
  plataforma, dominio y pedido externo. Una clave SHA-256 determinística evita
  duplicados aunque falle el UPDATE posterior que vincula el pedido.
- Los tres webhooks obligatorios de privacidad validan HMAC y dominio
  body/header. `customers/data_request` tiene cola durable y panel admin;
  `customers/redact` y `shop/redact` anonimizan también guías, direcciones y
  labels derivados antes de cualquier cascade. Las obligaciones pendientes no
  se borran al desinstalar.
- El espejo operativo nuevo de Google Sheets es `PLATAFORMA_SIN_PII`; la
  decisión sobre la pestaña histórica y la retención de backups está documentada
  en `docs/PRIVACIDAD_SHOPIFY_OPERACION.md` y sigue siendo un control externo.
- Suite actual: **972 tests OK + 5 subtests**, sin exclusiones manuales. Los
  tres tests de integración que pytest omite sin una URL aislada pasaron además
  contra PostgreSQL real; también pasaron el schema fresco idempotente y la
  migración desde la estructura productiva anterior.

## Seguridad (03/08/2026)

### Endurecimiento grande de la web ("usa todo tu poder")

Segunda tanda del 03/08, sobre la deuda de fondo. Todo montado, deployado y
verificado en producción (el marcador de "versión nueva viva" es la cabecera
CSP; el deploy booteó pese al cambio a contenedor no-root). Detalle en
`docs/SEGURIDAD.md`. Diseñado con un inventario por superficie (6 agentes) y
validado con una auditoría adversarial de 5 lentes + verificadores (14
agentes): **9 hallazgos, 7 confirmados y arreglados en la misma tanda**, 1
refutado, 1 dependiente de misconfig (ya cerrado).

- **Web compilada con esbuild** → `static/js/app.js` (bundle único, React
  adentro). Se acabó Babel+unpkg en el navegador. El bundle SE COMMITEA
  (Railway no tiene node); para editarla: tocar `web/components/*.jsx` +
  `npm run build:web` + bumpear `?v=`.
- **CSP estricta** `script-src 'self' 'nonce-<req>'` en toda la web/portal/
  admin/páginas-Python. Cero handlers inline (migrados a `data-*`), nonce en
  todos los `<script>`. NO toca `/shopify` (iframe) ni `/docs`.
- **MFA TOTP opcional en el admin** (`ADMIN_TOTP_SECRET`, generar con
  `scripts/generar_totp_admin.py`). Anti-replay por paso de tiempo.
- **api_key B2B hasheada** (sha256), migración en el arranque, botón
  "Regenerar API key" en el admin.
- **Guardas**: Host→421 (sin wildcard railway), tope de tamaño por
  Content-Type, CSRF Origin/Sec-Fetch en /portal|/admin, `client_ip` toma
  CF-Connecting-IP / XFF derecho (el rate limit era evadible), contenedor
  no-root.
- **Registro de auditoría** (`/admin/seguridad`): quién entró (y quién falló)
  y qué se tocó de dinero/credenciales/acceso, con IP. `servicios/auditoria.py`
  cableado de verdad (antes sólo lo llamaba el código muerto `core/security.py`,
  que se borró). Tabla `security_audit` en el schema, job diario de poda.

Hallazgos de LA PROPIA auditoría de esta tanda, ya arreglados: el no-root
tumbaba el arranque (mkdir en /app al importar tracking) — **ALTA**, era un
self-DoS del deploy; TOTP anti-replay incompleto y "quemable"; rate limit
evadible por XFF spoofing; tope de tamaño rompía subidas del admin. Deuda
que queda anotada en SEGURIDAD.md: `unsafe-inline` en style-src, rate limit
en memoria, chunked evade el tope (lo corta el proxy), `auditoria.py` sin
cablear.

### Dólar oficial automático (03/08)

`COTIZACION_DOLAR_ARS` (la ÚNICA fuente de verdad de todos los precios) se
actualiza sola desde el oficial (dolarapi.com, venta) cada 6h y al arrancar —
antes se cargaba a mano. Verificado en prod: cotiza a $1515 (el oficial del
día). `servicios/dolar_oficial.py`. Guardas: rango sano, tope de salto (no
pisa un salto >50% — manda MAIL de alerta a Leandro, urgente si es hacia
arriba = vender bajo costo), fallback de referencia si no hay valor previo.
Cuando el dólar cambia, refresca `tarifas_cache` en background (el checkout lee
de ahí, no cotiza en vivo). Se apaga con `DOLAR_AUTO=0`. Muestra origen
(auto/manual) en `/admin/config`. **La ALERTA de la regla 8 (revisar
WEB_DESC_FEDEX_PCT antes de FedEx producción) sigue vigente** — el dólar
automático no la reemplaza, son cosas distintas.

Modelo de precios en Shopify/Tiendanube (definido con Leandro 03/08): el portal
cotiza con el dólar del día; el comerciante define su política (precio + tax)
por tienda; en Shopify **Basic** el precio se muestra con tabla de tarifas por
peso (el comprador NO ve cotización en vivo — eso necesita Advanced/Plus). La
tabla con números reales espera a las cuentas FedEx/UPS operativas; la
maquinaria (peso volumétrico, dólar auto, política) ya está lista.

### Integraciones de tienda — finalizadas (03/08)

Inventario con 2 agentes. Todo lo de código cerrado; lo que falta es de Leandro.

- **Shopify**: LISTA y en producción (verificada e2e 28/07). Se sacó el scope
  `write_shipping` (era del CarrierService retirado; pedir de más = rechazo en
  el App Store). Para publicar en el App Store: `docs/PUBLICAR_APP_SHOPIFY.md`
  (credenciales + Partner Dashboard + compliance webhooks + submit). Ojo: la
  app YA NO cotiza en el checkout — no prometerlo en la ficha.
- **Tiendanube**: backend completo y ahora **instalable desde el portal**
  (botón "Instalar app de Tiendanube", aparece con la app configurada; antes
  decía "próximamente" y no había cómo conectarla). OAuth con `state` anti-CSRF
  en cookie (state:cliente, un solo uso): el callback sólo vincula si el state
  vuelve. parsear_pedido captura el flete cobrado. Guía:
  `docs/PUBLICAR_APP_TIENDANUBE.md` (app en Partners Portal + CLIENT_ID/SECRET
  en Railway + redirect URI + permisos). Se auto-activa con las 2 env vars.
- `.env.example` actualizado con DHL, UPS, Shopify, Tiendanube, TOTP, CORS.

### Primera auditoría del 03/08 (previa)

Se midió producción y se corrió una auditoría adversarial de 26 agentes:
**8 hallazgos confirmados, 14 refutados**. Todo lo confirmado está arreglado,
deployado y verificado en vivo. Detalle completo en `docs/SEGURIDAD.md`, que
ahora sólo lista lo que está montado de verdad.

Lo que se encontró y se cerró:

1. **ALTA — robo de tienda ajena.** `/portal/tienda/reclamar` sólo chequeaba
   que el dominio estuviera en la lista de instalaciones sin dueño, y esa
   lista se le mostraba a TODOS los clientes. Un cliente podía apropiarse de
   la tienda Shopify de otro y quedarse con sus ventas y con los datos de los
   compradores finales. Ahora `es_dueno_de_la_tienda()` le pregunta a la propia
   tienda quién es su dueño (`GET shop.json`) y lo compara contra el mail del
   cliente en TAURO; la lista además se filtra por cliente.
2. **Sin cabeceras de seguridad, `/docs` abierto y `CORS: *`** en producción.
   Cerrado: HSTS, nosniff, referrer, permissions, anti-frame (menos en
   `/shopify/*`, que va en iframe), docs en 404, CORS a los dominios de TAURO.
3. `/cotizar-web` sin rate limit: cada request cotiza en vivo contra los
   couriers. 30 por IP cada 5 minutos.
4. `/cotizacion-lead` era una primitiva para mandar mail a cualquiera con
   nuestro remitente. Un mail por dirección por día.
5. El backup del admin exportaba `clientes.api_key` en claro. Fuera.
6. El webhook GDPR de Shopify logueaba el mail y teléfono del comprador.
7. `pedidos_huerfanos` no vencía nunca (PII de compradores guardada para
   siempre). Se borran a los 90 días.
8. `/portal` y `/admin` sin `Cache-Control`: el HTML quedaba en la caché del
   navegador después de cerrar sesión. `no-store, private`.

**Deuda de seguridad conocida, en `docs/SEGURIDAD.md`:** no hay CSP (la
bloquea Babel en runtime — se destraba compilando React en el build), la
`api_key` se guarda en claro, el contenedor corre como root, el rate limit es
en memoria (con más de un worker el tope se multiplica) y el admin no tiene MFA.

## Pendientes (en orden)

La separación funcional y contable Nacional/Internacional está especificada
en `docs/AMBITOS_NACIONAL_INTERNACIONAL.md`. El selector, historial y
dual-write inicial ya están en el worktree; la imputación separada de pagos
requiere migración y conciliación antes de mostrar dos saldos.

1. **Técnico**: NADA bloqueante. Lo que queda depende de credenciales:
   - **Probar contra las APIs vivas** cuando entren las cuentas. La emisión
     de DHL y UPS está armada contra la documentación y cubierta por tests
     de payload, pero NUNCA se ejecutó contra el sandbox real. La primera
     guía de cada courier hay que mirarla de cerca (mismo criterio para la
     primera recolección: la Pickup API tampoco se probó viva).
   - Cotización, emisión y recolecciones nacionales mediante APIs directas
     de Andreani/OCA. La integración agregadora anterior fue retirada.
   *(Cerrados el 02/08: open redirect, `state` del OAuth, ventas de tiendas
   sin vincular, reserva atómica en guías nacionales, recolecciones FedEx.
   El 03/08: los 8 hallazgos de la auditoría — ver la sección de arriba.)*
2. **Del dueño**: cargar `WHATSAPP_TAURO` en /admin/config cuando llegue la
   eSIM (el botón de ayuda del portal aparece solo); SKUs+medidas en
   catálogos de clientes; formularios comerciales Andreani/Correo/OCA (dossier
   en `~/Documents/colab tauro/CARRIERS_NACIONALES_contactos.md`);
   credenciales Tiendanube; backups de Postgres en Railway + UptimeRobot.
3. **Vetado por el dueño (no hacer)**: caso de éxito con cita de Pesca Jacks;
   bloque Data Fiscal/AFIP (por ahora); logos de FedEx o palabra "partner"
   en la web.

## Gotchas técnicos (los que muerden)

- `dotenv.py` en la raíz es un shim local gitignoreado — NO subirlo.
- `pedido_tienda_id` debe viajar en el form dict del wizard o un error de
  validación rompe el vínculo venta↔envío (ya pasó; está arreglado — no
  regresionar).
- El dólar sale de `cotizador.dolar_ars()` (tabla config) en TODOS los
  caminos; `os.getenv` sólo como último recurso documentado.
- Los templates del portal usan globals de Jinja: `url_tracking`,
  `es_nacional`, `ayuda`, `pendientes_menu`, `saldo_menu` — si se instancia
  otro `Jinja2Templates`, hay que re-registrarlos.
- `solicitudes_guia.observaciones` la VE el cliente. Nada interno ahí.
- Cualquier protección que se agregue tiene que quedar montada en `main.py`.
  `core/security.py` era un paquete entero de defensas que NUNCA se montó, y
  `docs/SEGURIDAD.md` las daba por vigentes. Antes de escribir una línea en ese
  documento, medirla contra producción con `curl -I`.
