# Operadores logísticos — producción, 04/09/2026

Estado: código implementado, probado y publicado en producción en `1c458dc`.
El despliegue no cargó pagos/NC reales, no transfirió dinero y no modificó Gmail.

## Qué incluye

- `/admin/operadores`: mundos DHL, FedEx, Andreani y OCA, sobre los mismos documentos.
- Operador → documentos, fechas, antigüedad, vencimientos, saldos por moneda y pagos.
- Documento → clientes/guías por línea, pesos iniciales/facturados, último ajuste del
  envío, importes sin asignar (también matches parciales) y total sin desglosar.
- Admin → cliente → Costos de operadores: sólo las líneas confirmadas de ese cliente.
  El PDF consolidado, costos y márgenes siguen siendo exclusivamente ADMIN.
- Libro propio de pagos de TAURO al proveedor; nunca usa `pagos` de clientes.
- Pagos parciales, un pago distribuido entre varias FC, varias fuentes por FC,
  TC explícito entre monedas, aplicaciones de NC y reversas auditadas.

## Reglas

Una cabecera por operador/tipo/número. Las líneas se vinculan a envíos y éstos a
clientes. Una FC con diez clientes se cuenta **una sola vez** en el saldo proveedor.
No se crean diez copias ni se vuelven a facturar los cargos por abrir esta vista.

El estado de conciliación no prueba pago. Los saldos se calculan con SQL/Decimal
desde aplicaciones vigentes. Las cantidades pequeñas se muestran con hasta cuatro
decimales para no aparentar cero. Los resúmenes nunca mezclan monedas.

Los documentos históricos quedan sin verificar, también las NC. No se consideran
deuda/crédito confirmado sólo porque todavía no hay aplicaciones en este libro.
El Admin debe revisar documentación y completar el historial antes de habilitarlo.
Una FC íntegramente cancelada con aplicaciones documentadas puede mostrarse
cancelada sin otra verificación. NC no verificada no se puede aplicar.

NC no es factura pagable: es una fuente de crédito, requiere PDF y fecha válidos,
mismo operador/moneda y respeta la FC referenciada. Un reclamo no resuelto jamás
se descuenta. Su aplicación en este libro **no genera otro ajuste de cliente**;
la revisión del costo/ajuste sigue por la conciliación versionada existente.

## Fechas

1. Se usa el vencimiento que trae el documento.
2. Si no lo trae: plazo acordado con el operador, en días corridos desde emisión.
   No hay plazo predeterminado. Cero significa vence el día de emisión; vacío,
   plazo desconocido. No se confunde con los plazos de crédito del cliente.
3. El plazo se congela al ingresar una FC/ND emitida desde la fecha del acuerdo.
   Cambiarlo no reescribe anteriores. Una factura de emisión anterior, aunque se
   cargue hoy, exige confirmación específica, atada al acuerdo mostrado.
4. Si faltan los datos necesarios, figura vencimiento sin definir.

Antigüedad = días desde emisión, no mora. Vence hoy no está vencida. La mora sólo
se atribuye a saldo pendiente verificado. Al cancelar se conserva la última fecha
efectiva de pago/NC vigente (no anterior a emisión); no sigue acumulando mora.
Una reversa reabre el saldo y la mora correspondiente.

Si se cargó mal un vencimiento acordado, se puede rectificar su fecha (o dejarla
sin definir) con motivo y evidencia del acuerdo. Se conserva el snapshot y todas
las rectificaciones; nunca se reemplaza el vencimiento del documento original.

## Registrar y corregir pagos (cuando se habilite en producción)

1. En el operador, registrar un pago **ya realizado** con fecha, moneda, importe,
   referencia bancaria y comprobante PDF individual (hasta 8 MB).
2. Abrir cada FC y aplicar el importe de cancelación en la moneda de esa FC.
   Misma moneda exige TC 1. Moneda diferente requiere TC y confirmación adicional
   contra el comprobante, incluso si el TC fuera 1. Escribir TC sin miles, por
   ejemplo `1450,25`. No se reutiliza automáticamente el TC de conciliación.
3. Los dos saldos se validan bajo lock; no hay sobreaplicación ni doble consumo.
   Formularios de pago/aplicación llevan clave idempotente; PDF/referencia ya
   usados por un pago vigente se rechazan.
4. Error de aplicación: revertirla con motivo. El original permanece y se liberan
   los saldos de la factura y del origen. Registrar luego la aplicación correcta.
5. Error del pago: revertir primero sus aplicaciones, luego el pago. Se conserva
   el comprobante/historia, y se permite registrar su versión correcta con la misma
   referencia/PDF. Revertir no hace una devolución bancaria ni toca al cliente.

No hay borrados/ediciones de historia. Las reversas son completas; para corregir
parcialmente, revertir la aplicación y volver a imputar el monto correcto.
Una NC ya usada antes del portal necesita una reconstrucción controlada de sus
aplicaciones antes de considerarla disponible: no habilitarla por su total nominal.
Un extracto que contiene varias transferencias no se reutiliza como comprobante
individual: debe separarse la evidencia de cada pago.

## Seguridad y trazabilidad

Todos los handlers verifican ADMIN antes de leer/escribir. POST con HMAC temporal
por operador/acción/documento y, para reversas, registro exacto; confirmación humana
obligatoria. PDFs descargados como adjuntos, privados/no-store, sandbox y nosniff.
No se agregan endpoints ni links de proveedor en `/portal`.

Aplicaciones, pagos, plazos y reversas son append-only y auditados. Checks/locks
también en PostgreSQL. Se protege la identidad/importe/evidencia de documentos
usados por este libro. La cardinalidad ajustes→conciliación es 1:1 por UNIQUE.

La autenticación actual del proyecto es un Admin compartido: la auditoría registra
ese actor (`admin`), no identifica personas distintas. Si se habilitan varios
administradores con identidad propia, se deberá propagar esa identidad al libro.
No se modificó el sistema de autenticación en este trabajo.

## Archivos y migración

- `servicios/operadores_logisticos.py`: reglas, consultas y transacciones.
- `endpoints/admin_operadores.py`: rutas montadas dentro del router Admin.
- `sql/operadores_logisticos.sql`: bloque de migración, replicado literalmente en
  `sql/schema.sql`; un test exige igualdad. El arranque sigue usando schema.sql.
- `templates/admin/operador*.html`, `static/css/operadores.css`: interfaces.
- Tests: `test_operadores_logisticos.py`, `test_operadores_postgres.py`,
  `test_admin_operadores.py`.

La migración no transforma estados históricos en deuda ni deduce pagos existentes
de clientes. Se ensayó desde b7d1fb1 sobre PostgreSQL aislado, ejecutándola tres
veces, con PDF/total preservados y reversas/pagos corregidos conservados.

## Validación y pendientes reales

Corrida completa integrada: 1.663 tests, 30 advertencias y 5 subtests aprobados (PostgreSQL
local y schemas descartables, DATABASE_URL vacía). QA visual de cinco vistas a
1440, 840 y 390 px, sin desbordes de página. Revisiones independientes con el
CLI real de Claude, modelo principal verificado `claude-fable-5-1`.

Antes de cerrar se integró origin/main con las mejoras de colores/saldos y búsqueda
de envíos (c4a1a4f, 33f8c66), sin conflictos ni cambios descartados. La corrida
anterior al merge tenía 1.653 pruebas; ambas pasaron. No se hizo push.

El servicio fija aislamiento READ COMMITTED antes de escribir. Los triggers de
pagos, aplicaciones, reversas y protección del documento rechazan otros niveles
para no validar saldos con snapshots obsoletos. Hay pruebas concurrentes reales.
Ver el detalle de revisión en `REVISION_CLAUDE_OPERADORES_20260904.md`.

Pendiente antes de uso real: autorización de despliegue, respaldo productivo,
verificación posterior del release y configuración de los plazos efectivamente
acordados. La conexión automática a Gmail continúa pendiente; este cambio no
agrega lectores automáticos de FedEx/Andreani/OCA ni aplica cargos al cliente al
recibir una factura. Se reutiliza carga manual multioperador y lector PDF DHL.

Ruta de control utilizada: skill TAURO de orquestación, router architecture con
financial-impact/security-sensitive → Sol/high. Cálculos deterministas y revisión
independiente; sin datos de clientes/PDFs reales enviados al modelo revisor.
