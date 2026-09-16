# Confirmación visible de recolecciones — 16/09/2026

## Problema y resultado

Una guía con retiro ya agendado seguía ofreciendo «Coordinar retiro». La pantalla general escondía el número del courier bajo la dirección y terminaba con instrucciones para emitir una guía. El cliente no podía distinguir claramente una reserva exitosa de una operación todavía pendiente.

La recolección ahora tiene un comprobante visible en la sección Recolecciones y en el detalle de la guía: estado, número de reserva del courier, fecha, horario local del origen, dirección y bultos. Mis envíos muestra «Recolección programada» y el número cuando existe una confirmación. El formulario para otro retiro queda separado; no se ofrece repetir una reserva activa, incierta o completada.

## Número DHL

La integración MyDHL lee `dispatchConfirmationNumbers` de `POST /pickups` y guarda el primer número en `recolecciones.confirmation_code`. Es la referencia de la reserva; no es el tracking y no acredita que los paquetes hayan sido retirados físicamente. Se verificó en modo lectura una reserva real de WAIMAO que ya contiene ese número.

Referencia oficial consultada el 16/09/2026: [MyDHL API](https://developer.dhl.com/api-reference/dhl-express-mydhl-api), servicio Pickup, y sus operaciones de actualización/cancelación por `dispatchConfirmationNumber`. El código existente ya implementaba lectura y persistencia; esta entrega mejora su visibilidad y rechaza respuestas malformadas o sin número utilizable como confirmación.

## Recorrido

1. Desde una guía vigente, elegir Programar retiro y revisar fecha y horario en el origen.
2. Confirmar una sola vez. Si el courier confirma, volver al comprobante exacto de la cuenta con su número.
3. Si la respuesta es incierta, mostrar «Pendiente de confirmación» y bloquear un nuevo formulario para esa guía.
4. Una cancelación mantiene su antecedente y habilita el formulario para una nueva reserva; no cambia la guía.

El mensaje de éxito depende del registro propio leído del servidor, no sólo de `?ok=1`. Los errores conservan el envío seleccionado. Fallos al consultar retiros no se interpretan como ausencia de reserva. Las consultas nuevas filtran cliente e id; el listado de envíos resuelve sus reservas en una única consulta por página.

## Alcance y validación

Sin migraciones ni dependencias nuevas. Se conservan las reservas atómicas y los bloqueos de concurrencia. No se crearon ni cancelaron retiros reales durante estas pruebas.

Suite completa: **2.318 pruebas y 5 subpruebas aprobadas**, 33 advertencias existentes, sin fallos ni omisiones. Pruebas de acceso entre cuentas con PostgreSQL aislado; comprobación de confirmadas, canceladas, inciertas, falta de número, fallos de lectura, escapes HTML y retorno al envío tras errores. Revisión con navegador y datos ficticios en escritorio y celular, temas claro y oscuro. Evidencia de suite final en `qa_portal_readiness_20260916/evidence/pickup-full-final.log`.

Preparado en `codex/pickup-confirmation-20260916`, sobre `d2fe4a1`. Pendiente de autorización para publicar esta entrega nueva.
