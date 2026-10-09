# Control de envíos desde Admin

Ficha para que el administrador controle el precio al cliente, la cancelación comercial y la recolección de un envío sin perder su historial.

## Acceso

En **Envíos** o en **Clientes → Envíos**, abrir **Gestionar envío**. También está disponible desde Solicitudes y emisión. La ficha identifica cliente, solicitud, guía, destinatario, ruta, precio vigente y tracking del operador.

- Solicitud: `/admin/pedidos/{solicitud_id}/control`.
- Cargo, incluso histórico: `/admin/clientes/{cliente_id}/envios/{envio_id}/control`.
- Los identificadores se resuelven y validan en el servidor. No son intercambiables.

## Precio cobrado al cliente

Se ingresa el nuevo total en ARS y un motivo. El cargo inicial es inmutable: se agrega un débito o crédito por la diferencia, con auditoría. No se cambia el costo de compra del courier ni el precio que el cliente cobra a su comprador.

Un precio cero es una bonificación total: mantiene la guía activa. Cancelar es una operación distinta. Dos pestañas con precios distintos no pueden sobrescribirse sin advertencia. La clave de operación liga envío, importe, motivo y actor para evitar duplicados.

Para cargos históricos sin solicitud asociada, el ajuste de precio permanece deshabilitado: no se crea una solicitud ficticia para eludir la estructura contable existente.

## Cancelación en la cuenta corriente

Se requiere motivo y confirmación. Marca cargo y solicitud como CANCELADO; conserva los importes originales, ajustes y documentos como historia. Los movimientos cancelados y sus ajustes no suman en el saldo vigente. No elimina datos ni llama al courier para anular una etiqueta.

Los pagos imputados requieren regularización previa; no se desimputan ni borran silenciosamente. Si ya hay factura, sólo se permite cerrar cuando las partidas propias de FC y NC están exactamente compensadas, el precio vigente es cero y no hay pagos imputados. Facturas legacy sin vínculo documental siguen requiriendo revisión contable. Un bloqueo muestra su causa y el acceso a los documentos correspondientes.

Las guías canceladas comercialmente conservan el tracking físico hasta ENTREGADO, sin volver a activar su estado comercial. No se confunden con etiquetas descartadas por el cliente. Una recolección activa o una emisión en curso o incierta impiden esta acción.

El trigger de aplicaciones de pagos impide imputar posteriormente a una factura que contiene cargos cancelados. Los locks por cargo serializan la carrera entre imputación y cancelación.

## Recolección

Admin puede programar sobre una guía internacional real GUIA_LISTA con cliente activo y courier publicado, implementado y productivo. No depende del permiso de autoservicio del cliente. Se toma origen y paquetes de la solicitud emitida, sin aceptar reemplazos desde el formulario.

Fecha y ventana usan las mismas validaciones que el portal; la disponibilidad y el corte horario los confirma el operador. La reserva se guarda antes de llamar a la API. Un doble clic no crea otro retiro; una respuesta incierta queda pendiente de verificación. La ficha destaca el número confirmado y enlaza a Recolecciones.

No habilita recolecciones nacionales ni operadores que carezcan de integración operativa.

## Despliegue y verificación

Requiere aplicar el schema al iniciar: agrega el indicador de cancelación comercial, el índice de tracking y actualiza el trigger de aplicaciones. Los datos existentes conservan su estado. Los POST legacy de anulación/precio abren la ficha de control y ya no mutan sin sus verificaciones.

Las pruebas de PostgreSQL usan esquemas descartables y datos inventados; los couriers están simulados. No se ejecutan cancelaciones, ajustes ni retiros reales durante esta tarea.
