# TAURO Solutions Ar en Tiendanube

Estado de trabajo actualizado: 01/09/2026.

La integración ya no se considera lista por tener OAuth y webhooks de pedidos.
Para publicarla como solución nacional debe completar también el contrato de
**Shipping Carrier**, cotizar en el checkout con tarifas contractuales y pasar
la homologación síncrona de Tiendanube.

## Arquitectura preparada

- OAuth y almacenamiento por `store_id`.
- Webhooks de pedidos y ciclo de vida.
- Ingreso del pedido al portal TAURO sin emitir una guía automáticamente.
- Contrato de Fulfillment Orders preparado para actualizar tracking; la
  ejecución OCA permanece bloqueada hasta UAT.
- Callback de tarifas `TAURO Solutions Ar` protegido por token por tienda.
- Contrato fail-closed: sin adapter nacional operativo no se publica tarifa.
- Soporte de carrito mixto con `price` y `price_merchant` separados.
- Extensión NubeSDK aislada, sin DOM, en `tiendanube_nube_app/`.
- Fulfillment Orders para despacho/tracking, con fallback legacy acotado.
- Labels API con secretos separados, validación, idempotencia y outbox; la
  ejecución OCA permanece bloqueada y no responde una aceptación falsa.
- Webhooks de privacidad con tombstones anti-replay y bandeja admin de atención.
- Centro de ayuda público y assets candidatos de publicación.

## Bloqueadores externos y comerciales

1. Crear o confirmar la app **TAURO Solutions Ar** en Partners, categoría Shipping.
2. Confirmar con Partners los scopes mínimos candidatos: `write_shipping`,
   `read_orders`, `write_fulfillment_orders` y `read_customers`.
3. Pedir al Platform Team de Tiendanube acceso a Shipping API para la cuenta y
   la tienda demo: <https://forms.gle/oqP1BrtwMzNb7xCM9>.
4. Configurar la redirect URL:
   `https://taurosolutions.ar/integraciones/tiendanube/callback`.
5. Completar OCA con credenciales contractuales propias y validar en QA la
   cotización ya implementada. Andreani continúa pendiente.
6. Ejecutar UAT de cotización OCA con una respuesta real anonimizada. Emisión,
   etiqueta, cancelación y tracking siguen bloqueados hasta cerrar su contrato
   neutral e idempotencia.
7. Completar los artefactos y solicitar homologación síncrona.

## Variables de producción

```text
BASE_URL=https://taurosolutions.ar
TIENDANUBE_CLIENT_ID=
TIENDANUBE_CLIENT_SECRET=
TIENDANUBE_TOKEN_ENCRYPTION_KEY=
TIENDANUBE_SHIPPING_ACCESS_APPROVED=false
TIENDANUBE_DEMO_STORE_ID=
TIENDANUBE_SHIPPING_ENABLED=false
TAURO_NACIONAL_RATES_READY=false
TIENDANUBE_HOMOLOGATION_APPROVED=false
OCA_ADAPTER_ENABLED=false
OCA_UAT_APPROVED=false
OCA_ENVIRONMENT=qa
OCA_PRODUCTION_APPROVED=false
```

Los dos flags permanecen en `false` hasta que el adapter nacional, sus tarifas
y el UAT estén aprobados. La mera presencia del Client ID y el Client Secret no
habilita el medio de envío.

## Artefactos

- [Checklist de homologación](tiendanube/HOMOLOGACION_CHECKLIST.md)
- [Diagrama de secuencia](tiendanube/SEQUENCE_DIAGRAM.md)
- [Guion de video demo](tiendanube/DEMO_SCRIPT.md)
- [Copy de publicación](tiendanube/LISTING_COPY.md)
- [Campos del Portal de Partners](tiendanube/PARTNER_PORTAL_FIELDS.md)
- [FAQ de Shipping](tiendanube/FAQ_SHIPPING.md)

## Referencias oficiales

- Shipping Provider: <https://tiendanube.github.io/api-documentation/guides/shipping-provider>
- Shipping Carrier API: <https://tiendanube.github.io/api-documentation/resources/shipping-carrier>
- Fulfillment Orders y Labels API: <https://tiendanube.github.io/api-documentation/resources/fulfillment-order>
- Requisitos de homologación: <https://dev.tiendanube.com/docs/homologation/requirements>
- Directrices de publicación: <https://dev.tiendanube.com/docs/applications/guidelines>
