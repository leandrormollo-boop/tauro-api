# Guion del video de homologación

Duración objetivo: 10–15 minutos, grabado en español y sin secretos visibles.

1. Mostrar la ficha de la app en Tiendanube y comenzar la instalación desde
   Tiendanube.
2. Aceptar únicamente los permisos mínimos declarados en la ficha.
3. Completar el alta de un merchant nuevo en TAURO.
4. Mostrar que el medio **TAURO Nacional** quedó disponible y configurable.
5. Repetir con una cuenta TAURO existente y demostrar login/vinculación.
6. Cotizar un carrito AR→AR con peso y medidas válidas.
7. Cotizar un carrito mixto con un producto de envío gratis.
8. Mostrar el comportamiento seguro de un producto sin dimensiones.
9. Finalizar una compra y comprobar la solicitud pendiente en TAURO.
10. Repetir el webhook y demostrar que no duplica el pedido pendiente.
11. Mostrar que emisión, etiqueta y tracking fallan de forma cerrada mientras
    OCA UAT no esté aprobado.
12. Suspender la app y comprobar que no cotiza ni llama al courier.
13. Reactivar/reinstalar y demostrar que no duplica carrier/webhooks.
14. Desinstalar y comprobar revocación.
15. Mostrar política de privacidad, términos y canal de soporte.

La toma debe incluir reloj/log de auditoría sin payloads personales ni tokens.

Este guion parcial sirve para validar la instalación y la integración síncrona.
Antes de grabar y enviar la homologación definitiva hay que completar OCA QA/UAT,
habilitar el worker de Labels y agregar al video la emisión, cancelación,
etiqueta y tracking reales. No se debe representar esa capacidad con mocks.
