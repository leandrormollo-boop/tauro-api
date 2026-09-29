# Asignación de guías desde el Admin

Las guías históricas atribuidas a un perfil incorrecto pueden quedar **pendientes de asignación**: conservan el tracking, los documentos, el estado operativo y los vínculos con facturas del courier, pero no tienen dueño ni son accesibles desde el portal de clientes.

El Admin accede desde **Pendientes de asignación** o desde **Control de envíos → Abrir envío → Cliente del envío**. Puede indicar el nombre del cliente que aún no tiene perfil, dejar el envío pendiente o asignarlo a un perfil activo existente. Asignar habilita su visibilidad a ese perfil; no crea cargos, emite guías ni envía mensajes.

## Controles

- Sólo guías emitidas, vigentes y sin dependencias comerciales. Cargos (incluso legacy por tracking), ajustes, cotizaciones, bases comerciales aceptadas, tiendas, recolecciones y reemisiones bloquean el cambio. Requieren revisión de sus relaciones antes de poder reasignar: esta entrega no traslada saldos entre clientes.
- Bloqueo de fila y comparación con el propietario esperado impiden que dos pestañas sobrescriban la asignación. Historial y auditoría se guardan en la misma transacción; si falla la auditoría, se revierte todo.
- Una guía pendiente tiene `cliente_id=NULL`, `visible_cliente=FALSE` y `cargo_pendiente=FALSE`, con nombre de cliente indicado. Un CHECK impide hacerla visible mientras carezca de dueño.
- El tracking DHL incluye las guías pendientes. Las vistas de proveedores conservan sus facturas y costos. El filtro de cliente ya no las atribuye al perfil anterior.
- Una base comercial no se puede registrar hasta asignar un cliente.

## Migración y publicación

`sql/asignacion_envios.sql` está incluido al final de `sql/schema.sql`, que se ejecuta al iniciar la aplicación. La migración es repetible, no reasigna datos existentes y no modifica tablas monetarias. Agrega la tabla de historial y permite un propietario nulo sólo bajo las condiciones anteriores.

Después de publicar, las correcciones autorizadas se hacen desde los formularios autenticados. Verificar identidad de guía, destinatario, dependencia contable y cuenta anterior antes de guardar; luego comprobar la bandeja pendiente, la exclusión de la cuenta anterior y la preservación de documentos del proveedor. No ejecutar SQL masivo por nombres.

Un rollback de código anterior no restituye el dueño: las guías pendientes conservan el propietario nulo y el acceso de clientes sigue cerrado. Mantener el Admin de esta versión o aplicar una corrección hacia adelante para administrarlas; no reasignarlas al cliente erróneo para facilitar un rollback.

## Validación

Suite PostgreSQL real con esquemas aislados: ocultamiento por ownership, reasignación a perfil, cargos legacy, protección de documentos, rechazo de dependencias, simultaneidad de dos administradores, reversión ante fallo de auditoría, repetición de migración y autenticación del endpoint. Se verificaron también control de negocio, cuentas, visibilidad del portal, tracking y conciliación del courier. Recorrido visual local con guías ficticias y red externa bloqueada.
