# TAURO Solutions Ar en Tiendanube

Estado de trabajo actualizado: 25/09/2026. Pipeline de Shipping/Labels y rutas
de privacidad preparados localmente para UAT; todavía sin desplegar, homologar
ni publicar.

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
- El token de cada callback se acepta únicamente si instalación activa,
  configuración Shipping, mapping TAURO, dueño e `install_generation` coinciden.
- Contrato fail-closed: sin adapter nacional operativo no se publica tarifa.
- Soporte de carrito mixto con `price` y `price_merchant` separados.
- Extensión NubeSDK aislada, sin DOM, en `tiendanube_nube_app/`.
- Fulfillment Orders para despacho/tracking, sin fallback de escritura legacy:
  un timeout se reconcilia leyendo la FO concreta antes de otro PATCH.
- Labels API con secretos separados, validación, idempotencia y outbox; la
  ejecución OCA permanece bloqueada y no responde una aceptación falsa.
- Las operaciones y el outbox de Labels conservan dueño y generación. Alta,
  cancelación y worker vuelven a validar ese contexto bajo el lock de la tienda
  antes de cualquier escritura en el courier.
- Webhooks de privacidad con tombstones anti-replay y bandeja admin de atención.
- Centro de ayuda público y assets candidatos de publicación.

## Bloqueadores externos y comerciales

1. Crear o confirmar la app **TAURO Solutions Ar** en Partners, categoría Shipping.
2. Confirmar con Partners los scopes mínimos candidatos: `write_shipping`,
   `read_orders` y `write_fulfillment_orders`. No pedir `read_customers`: la
   v1 no consulta el recurso Customer y los avisos de privacidad no lo exigen.
3. Pedir al Platform Team de Tiendanube acceso a Shipping API para la cuenta y
   la tienda demo: <https://forms.gle/oqP1BrtwMzNb7xCM9>.
4. Configurar la redirect URL:
   `https://taurosolutions.ar/integraciones/tiendanube/callback`.
5. Completar OCA con credenciales contractuales propias y validar en QA la
   cotización ya implementada. Andreani continúa pendiente.
6. Ejecutar UAT de cotización OCA con una respuesta real anonimizada. Emisión,
   etiqueta, cancelación y tracking ya tienen ejecución durable implementada,
   pero permanecen bloqueados por flags hasta validar credenciales, contrato y
   homologación con operaciones reales de QA.
7. Deshabilitar o redactar en el edge/proxy de Railway todo log del request path
   de Shipping/Labels y rotar los callbacks legacy antes de habilitarlos.
8. Completar los artefactos y solicitar homologación síncrona.

## Variables de producción

```text
BASE_URL=https://taurosolutions.ar
TIENDANUBE_CLIENT_ID=
TIENDANUBE_CLIENT_SECRET=
TIENDANUBE_TOKEN_ENCRYPTION_KEY=
TIENDANUBE_PRIVACY_WEBHOOKS_CONFIRMED=false
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

Todos los flags de aprobación permanecen en `false` hasta que el adapter
nacional, sus tarifas y el UAT estén aprobados. La mera presencia del Client ID
y el Client Secret no habilita el medio de envío.

Railway ejecuta `python scripts/migrate_database.py` como predeploy. El proceso
web sólo verifica readiness y aborta si falta una tabla, columna o trigger del
contrato e-commerce. La migración también cifra access tokens históricos que
estuvieran en texto plano; el runtime los rechaza si el predeploy no los pudo
migrar. Ningún callback de Tiendanube intenta reparar el esquema o los tokens
durante tráfico.

## Ciclo de vida seguro de Shipping y Labels

- Cada OAuth crea un `install_generation` nuevo. En la misma transición, la
  configuración Shipping anterior queda inactiva y pierde el token de Labels;
  por eso una URL emitida para una generación anterior no sigue autorizando.
- El registro/reconciliación compara las URLs remotas con los hashes locales.
  Si generación o callback no coinciden, genera tokens nuevos y actualiza las
  URLs del carrier/option antes de reactivar.
- Tarifas, generación de etiqueta, cancelación y worker exigen el mismo
  `store_id`, dueño TAURO y generación actual. Un contexto nulo/legacy falla
  cerrado hasta completar OAuth/reconciliación.
- Los tokens viajan en el **path** porque así los define el callback registrado.
  Uvicorn ya corre con `--no-access-log`, pero eso no controla los logs del
  balanceador o edge de Railway. Antes de activar hay que verificar allí que el
  path se omita o se redacte; luego se deben rotar todos los callbacks que hayan
  existido antes de esa protección.

Esta rotación no sustituye la homologación: sólo deja preparado el código para
una prueba UAT aislada. Los flags productivos siguen en `false` hasta contar con
evidencia de Partners, OCA QA y observabilidad segura.

## Artefactos

Los seis webhooks de pedidos/ciclo de vida se registran por tienda mediante la
API. Los tres avisos de privacidad se configuran por aplicación en Partners y
usan rutas específicas; no forman parte del POST `/webhooks`. Mantener
`TIENDANUBE_PRIVACY_WEBHOOKS_CONFIRMED=false` hasta publicar las rutas y probar
su HMAC con datos ficticios. Los pasos y URLs están en
[Campos del Portal](tiendanube/PARTNER_PORTAL_FIELDS.md).

- [Checklist de homologación](tiendanube/HOMOLOGACION_CHECKLIST.md)
- [Diagrama de secuencia](tiendanube/SEQUENCE_DIAGRAM.md)
- [Guion de video demo](tiendanube/DEMO_SCRIPT.md)
- [Copy de publicación](tiendanube/LISTING_COPY.md)
- [Campos del Portal de Partners](tiendanube/PARTNER_PORTAL_FIELDS.md)
- [FAQ de Shipping](tiendanube/FAQ_SHIPPING.md)

## Referencias oficiales

- Registro por tienda y contratos de privacidad: <https://tiendanube.github.io/api-documentation/resources/webhook>
- URLs de aplicación en Partners: <https://tiendanube.github.io/api-documentation/authentication#urls>

- Shipping Provider: <https://tiendanube.github.io/api-documentation/guides/shipping-provider>
- Shipping Carrier API: <https://tiendanube.github.io/api-documentation/resources/shipping-carrier>
- Fulfillment Orders y Labels API: <https://tiendanube.github.io/api-documentation/resources/fulfillment-order>
- Requisitos de homologación: <https://dev.tiendanube.com/docs/homologation/requirements>
- Directrices de publicación: <https://dev.tiendanube.com/docs/applications/guidelines>
