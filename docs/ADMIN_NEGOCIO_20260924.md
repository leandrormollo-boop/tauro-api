# Admin por cliente y proveedor — 24/09/2026

Implementación sobre `origin/main` 3b7377a (incluye OCA e IVA).
Este documento registra la validación previa a la publicación.

## Navegación

- Clientes y Proveedores son entradas visibles del menú principal.
- Cada cliente conserva su cuenta corriente y tiene solapas para envíos, pagos,
  facturas emitidas, facturas de proveedores vinculadas, sus destinatarios,
  recolecciones y configuración. Márgenes, permisos y límite de crédito usan el
  formulario existente; datos y suspensión usan la edición del cliente.
- OCA, Andreani, Correo Argentino, DHL y FedEx tienen un espacio propio:
  envíos de clientes, cuenta corriente, pagos al proveedor y condiciones de pago.
- `/admin/control-envios` permite seguir la misma operación entre cliente,
  proveedor, guía y documentos. En móvil cada fila se presenta como una ficha.
- Nombres de clientes en solicitudes, recolecciones y revisión de pagos abren su
  ficha; los documentos y guías enlazan a su origen.

## Regla de lectura

La agrupación por proveedor usa el operador guardado en la solicitud; no depende
de haber recibido su factura. Se muestran solicitudes con tracking o fecha de
emisión, excluyendo pruebas. Borradores se operan en Solicitudes y emisión.

La conciliación existente propone coincidencias exactas por operador y tracking
al cargar una factura. Se conservan **PROPUESTO** y **CONFIRMADO**: la nueva vista
no confirma matches, crea asientos, aplica diferencias ni infiere pagos. Casos
ambiguos siguen requiriendo revisión. No se agregó una conciliación ciega.

- Cobro cliente: sólo aplicaciones efectivas de pagos aprobados. Un saldo global
  a favor no demuestra que un envío específico esté pagado.
- Una factura cliente totalmente cubierta puede acreditar el cobro de sus
  partidas. Un pago parcial se etiqueta a nivel factura, sin prorratearlo a envíos.
- Pago proveedor: estado de la factura vinculada, con su vencimiento documental
  o acordado. Pago parcial no significa que una guía concreta esté saldada.
- NC: crédito, nunca factura pagable. Saldos separados por moneda.
- Cancelados/reemplazados: separados de Vigentes, con cargo vigente cero y
  conservación de documentos del proveedor. Si hay un cargo ACTIVO incoherente,
  se muestra una alerta con acceso a revisión; esta lectura no lo borra ni corrige.
- Las consultas de envíos se paginan de a 25 y enriquecen por lotes en una
  transacción de sólo lectura. Los pagos no cargan PDFs para armar el listado.

## Correo Argentino

Se agregó al libro documental/contable, validaciones, carga nacional y constraints
idempotentes. No implica una API de cotización, emisión o recolección habilitada.
Los operadores con integración pendiente siguen dependiendo de su conexión real.

## Validación

- PostgreSQL local en base exclusiva de QA, schemas descartables.
- Suite financiera y navegación: 141 pruebas aprobadas (incluye 8 nuevas).
- Revisión de cambios posteriores: 59 pruebas de rutas/controles aprobadas.
- Control final de release: 12 pruebas de arranque, migración y negocio aprobadas.
- Browser: proveedor → cliente → pagos → configuración; desktop y 390×844.
  Fichas de envíos sin desborde horizontal de página; sin errores de consola.
- Vista local: `http://127.0.0.1:8789/admin/operadores`, datos ficticios,
  conexión exclusiva a una base de preview; POST y red externa bloqueados.

Contraste en producción (sólo lectura): el saldo de WAIMAO coincide entre el
panel gerencial y su ficha. La ficha de costos contiene asignaciones existentes.
La verificación posterior al despliegue debe repetir ese contraste contra la
base productiva y comprobar el estado saludable de Railway.

Publicación autorizada por el usuario. El resultado del despliegue y su
verificación se registran en el historial de release y en el log de ejecución.
No se usaron credenciales de couriers ni se emitieron envíos durante QA.
