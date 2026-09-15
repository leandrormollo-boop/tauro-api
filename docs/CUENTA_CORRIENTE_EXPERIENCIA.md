# Cuenta corriente del cliente — experiencia y controles

15 de septiembre de 2026 · Rama `codex/portal-experiencia-cliente`.
**Preparada para revisión local; pendiente de publicar.**

El objetivo es que el cliente pueda entender cuánto debe, qué factura requiere
atención y qué ocurrió con un pago informado. Esta entrega reorganiza
`/portal/cuenta`, agrega lecturas y permite consultar y descargar sus movimientos.
No cambia las reglas de emisión, aprobación de pagos o imputación contable.
No incorpora migraciones ni dependencias nuevas.

## Alcance visible

| Pieza | Comportamiento |
| --- | --- |
| Saldo consolidado | Conserva el resumen contable existente, con el estado A pagar, A favor o Al día y su composición desplegable. Los pagos aprobados se distinguen de otros créditos contables. |
| Vencimientos | Muestra hasta seis facturas emitidas con saldo, priorizadas por vencimiento. Distingue vencida, vence hoy, próximo vencimiento y fecha no informada; muestra pagos parciales y comprobantes en revisión. Los totales abarcan todas las facturas pendientes, no sólo las seis visibles. |
| Informar un pago | Desde una factura abre el formulario y selecciona únicamente un documento propio con importe disponible. La preselección también funciona sin JavaScript. Conserva comprobante obligatorio, validación de archivo, tope de 8 MB y clave de idempotencia. |
| Seguimiento de pagos | Muestra los ocho registros más recientes, su estado, referencia, fecha de registro y documentos asociados. El detalle de cada pago se acota a 24 aplicaciones; el historial permite buscar los movimientos restantes. |
| Capacidad disponible | Se muestra sólo si hay un límite monetario configurado. Considera deuda y reservas según la regla vigente de emisión. No concede permisos ni reemplaza la validación atómica al emitir. |
| Costos logísticos | Presenta seis meses en ARS, separados por ámbito, con importes accesibles en una tabla. Al elegir un mes, el historial muestra los cargos y ajustes del mismo período. |
| Movimientos | Búsqueda, filtros por ámbito/tipo/fecha, paginación y descarga de Excel con los filtros activos. Los ajustes conservan el detalle de valor original, diferencia y costo final. |

El diseño conserva las variables, tipografías y botones del portal TAURO. Usa
violetas de la marca para las series del gráfico, texto para identificar todos
los estados y ámbar para pendientes. El CSS está limitado a `.account-client`
y contempla los temas claro y oscuro; no modifica el diseño del administrador.

## Qué significa cada importe

Un vencimiento es un saldo **documental**, no una segunda deuda que deba sumarse
al saldo consolidado. Se calcula sobre facturas `FC` en estado `EMITIDA` y pagos
aplicados a la factura o a sus envíos. Las aplicaciones a envíos se cuentan una
sola vez, aunque la factura tenga varios ítems.

El bloque de vencimientos utiliza `facturas_cliente`. Los cargos sin factura y
las facturas históricas conservadas en `envios` siguen en el historial; no se les
inventa una fecha de vencimiento. Una factura sin fecha se identifica como tal.

Los pagos disponibles para aplicar ya fueron aprobados y **ya están descontados
del saldo consolidado**. Asignarlos después a una factura no debe descontarlos
otra vez. Los comprobantes pendientes sólo reducen el importe disponible para
informar sobre ese documento, para evitar solicitar dos veces el mismo destino;
no reducen la deuda. Créditos, ajustes y pagos sin imputar pueden explicar una
diferencia entre el pendiente documental y el consolidado.

El cupo sigue el criterio existente de reserva de emisión, basado en cargos,
pagos aprobados y reservas vigentes. No sustituye ese criterio por el resumen
contable con ajustes, por lo que puede diferir del saldo mostrado. No es una
confirmación de que el cliente tenga habilitada la emisión de un transportista.

## Estados de pago y datos disponibles

| Estado registrado | Presentación | Efecto en el saldo |
| --- | --- | --- |
| `PENDIENTE` | En revisión; comprobante recibido o pago registrado. Las aplicaciones `SOLICITADA` se muestran como solicitud. | No acredita dinero. |
| `APROBADO` | Acreditado; se muestran aplicaciones `APLICADA`. El estado nulo histórico conserva la equivalencia con aprobado. | Descuenta el pago según las reglas existentes. |
| `RECHAZADO` | Rechazado, también visible en el filtro Pagos. Se indica consultar y revisar antes de volver a informar. | No modifica el saldo. |

El recorrido visual usa la fecha de registro que existe en la base y el estado
actual. **No presenta una fecha de acreditación o de rechazo inventada**, ni una
secuencia histórica que el esquema no registra. No se publica `pagos.nota` ni
se usa como motivo de rechazo: puede contener información interna. Esta entrega
no agrega un campo de motivo público ni un nuevo circuito de conciliación.

## Gráfico por fecha contable

El período comprende el mes actual hasta hoy y los cinco anteriores. Los cargos
se agrupan por `envios.fecha`. Los ajustes aplicados se agrupan por la fecha de
`aplicado_at` en Argentina, con su signo: débito positivo y crédito negativo.
Se excluyen envíos cancelados/NC y ajustes que no estén aplicados.

Por ejemplo, un cargo de agosto de $100.000 y un crédito de septiembre de
$15.000 aparecen como +$100.000 en agosto y −$15.000 en septiembre. No se vuelve
a sumar el costo final del envío junto con el ajuste. Los cálculos mantienen
Decimal y redondeo a centavos; un mes con créditos netos puede ser negativo y
se representa debajo del cero.

El enlace del gráfico usa el filtro `costos`, que incluye cargos y diferencias,
y restablece el ámbito consolidado porque el gráfico presenta ambos ámbitos.
Su detalle y el Excel usan el mismo criterio de fecha y movimientos. Es una
vista de costos registrados, no una medición de facturación fiscal, rentabilidad
o pagos realizados en el mes.

## Filtros, descarga y fluidez

- La búsqueda admite hasta 120 caracteres. Encuentra guía, factura, referencia,
  detalle y destinatario; también encuentra aplicaciones de pagos por su
  documento relacionado. Usa parámetros SQL y trata `%`, `_` y barras como
  caracteres literales, sin ampliar el alcance de la búsqueda.
- Las fechas son ISO y los extremos son inclusivos. Un período invertido o una
  fecha inválida produce un aviso explícito en la pantalla, que vuelve a mostrar
  movimientos sin búsqueda ni fechas. La descarga responde con error 400 y
  nunca ignora silenciosamente un período inválido.
- El filtro Pagos incluye acreditados, pendientes y rechazados. El filtro En
  revisión permite consultar sólo pendientes. Ninguno cambia su efecto contable.
- `/portal/cuenta/exportar.xlsx` descarga todos los resultados de la consulta,
  hasta 10.000 movimientos. Por encima del límite solicita acotar el período;
  no entrega un archivo truncado. También controla un crecimiento entre la
  consulta de cantidad y la lectura de filas.
- El Excel incluye los filtros y columnas de cargo, crédito e importe informado.
  Las celdas textuales se escriben como texto, incluso si empiezan con `=`, `+`,
  `-` o `@`; fechas y montos conservan sus tipos. No es un estado con saldo de
  apertura/cierre ni un libro auditado.
- El filtro y la paginación piden `X-Tauro-Partial: cuenta`. El servidor devuelve
  sólo `cuenta_movimientos.html`, sin recalcular resumen, vencimientos, pagos,
  cupo o gráfico. La navegación normal GET sigue disponible sin JavaScript.
  El Excel y los enlaces externos a la lista se sincronizan con los filtros.
- Al filtrar se conserva el formulario de pago abierto y se mueve el foco a la
  región actualizada. Volver/avanzar recupera la consulta. Una respuesta fallida
  o una demora de más de 15 segundos conserva la lista anterior y muestra un
  mensaje; una consulta más nueva reemplaza a la anterior.

Los filtros sólo actualizan los movimientos. Los paneles superiores se
actualizan al cargar la página completa; no hay notificaciones en tiempo real
ni consulta automática periódica. Las cuatro lecturas del panel adicional usan
una transacción de sólo lectura con una misma instantánea; el resumen existente
y el historial tienen sus propias consultas. Una modificación contable
concurrente puede requerir recargar para ver todos los bloques actualizados.

## Privacidad y límites de la entrega

La página, la selección de documentos y el Excel toman exclusivamente el
cliente autenticado. Las consultas de facturas, pagos, envíos y aplicaciones
relacionadas validan el dueño. Las descargas de comprobantes y facturas
conservan su control de pertenencia. La respuesta parcial y el Excel llevan
`Cache-Control: private, no-store`.

El panel no consulta transportistas, no emite guías y no expone costos de
proveedores ni márgenes internos. Si falla su lectura, la cuenta principal
continúa visible y el panel muestra que no está disponible: no transforma un
error en cero vencimientos o crédito ilimitado. No se modificaron credenciales,
precios, migraciones, reservas ni reglas de aprobación.

## Validación registrada

- Ronda del portal: **26 pruebas aprobadas**, incluidas 13 nuevas de sesión,
  filtros, preselección propia, degradación del panel, respuesta parcial y Excel.
- Ronda de servicios y cuenta: **50 pruebas aprobadas**, con PostgreSQL 17
  sintético para búsqueda documental, separación de clientes, estados de pago,
  aplicaciones sin duplicados, ajustes por fecha contable, crédito negativo,
  redondeo y correspondencia entre lista y Excel. Las rondas tienen pruebas
  compartidas; no se suman como un total de casos únicos.
- Cierre ampliado de cuenta/facturación: **99 pruebas aprobadas y 4 omitidas**.
  Las cuatro omitidas requieren PostgreSQL, ya cubiertas en la ronda aislada
  anterior. Se actualizó la comprobación del resumen documental al recurso JS
  externo. Estos números no implican una prueba en producción.
- Preview local con plantillas y handlers reales, lecturas ficticias y bloqueo
  de conexiones externas. Página, respuesta parcial y Excel devolvieron 200.
- Revisión visual en escritorio a 1280 px y celular a 390 y 320 px, con temas
  claro y oscuro. Preseleccionar una factura completó su disponible de $250.000;
  consultar septiembre conservó ese monto, la selección y la referencia del
  borrador. La barra de $600.000 abrió cargos de $200.000 y $350.000 más un
  ajuste de $50.000, y el Excel conservó ámbito, tipo y fechas de esa consulta.
  Un rango de fechas invertido informó el error y normalizó el enlace de Excel.
  No hubo errores registrados en la consola del navegador durante el cierre.
- PostgreSQL de pruebas usó esquemas aislados y quedó apagado al finalizar.
  No se consultó la base productiva ni se registraron pagos o guías reales.

La vista de revisión está en [la cuenta local](http://127.0.0.1:8772/portal/cuenta)
mientras el servidor de esta computadora permanezca activo. Sus datos de
WAIMAO están marcados como ficticios. No hay mediciones de rendimiento real de
clientes ni verificación de esta versión en el dominio público.

Archivos centrales: `servicios/experiencia_cuenta.py`, `filtros_cuenta.py`,
`export_cuenta.py`, el lector de movimientos de `cuenta_corriente.py`,
`endpoints/portal_cliente.py`, las dos plantillas de cuenta y los recursos
`static/css/portal-cuenta.css` / `static/js/portal-cuenta.js`.

**Publicación pendiente.** Un push a `main` despliega a producción; esta entrega
no lo realiza. Antes de darla por publicada se debe verificar `/salud` y los
recorridos autenticados de saldo, filtros, pagos y descarga en el dominio real.
