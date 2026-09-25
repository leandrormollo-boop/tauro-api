# Configuración del Portal de Partners

Valores candidatos para **TAURO Solutions Ar**. Los secretos no se copian a este
archivo ni a tickets de homologación.

## Aplicación

- Tipo: Tienda de Aplicaciones.
- Categoría: Shipping / Envíos.
- País e idioma: Argentina / español.
- Tipo de entrega: `ship`.
- Envío internacional: no.
- Multi-CD: no en la primera versión.
- Handle: `tauro-solutions-ar`.

## URLs

- Redirect OAuth: `https://taurosolutions.ar/integraciones/tiendanube/callback`
- Panel: `https://taurosolutions.ar/portal/tienda`
- Preferencias: `https://taurosolutions.ar/portal/tienda`
- Privacidad: `https://taurosolutions.ar/privacidad`
- Términos: `https://taurosolutions.ar/terminos`
- Soporte: `https://taurosolutions.ar/ayuda/tiendanube`
- Webhook general: `https://taurosolutions.ar/integraciones/tiendanube/webhook`
- Store redact: `https://taurosolutions.ar/integraciones/tiendanube/privacidad/store-redact`
- Customer redact: `https://taurosolutions.ar/integraciones/tiendanube/privacidad/customers-redact`
- Customers data request: `https://taurosolutions.ar/integraciones/tiendanube/privacidad/customers-data-request`

Publicar y verificar estas tres rutas antes de cargarlas en Partners. Los
cuerpos de privacidad pueden omitir `event`; cada ruta deriva el evento sólo
después de verificar el HMAC del cuerpo original y validar su contrato. Probar
con datos ficticios, nunca con solicitudes reales de borrado.

## Permisos mínimos candidatos

- `write_shipping`
- `read_orders`
- `write_fulfillment_orders`
- `read_customers`

El Partner Team debe confirmar la disponibilidad de
`write_fulfillment_orders`. Todo write scope implica su read equivalente. No
solicitar Products ni Locations mientras el producto no use esos endpoints.

## Admin links

Crear dos links con el texto **TAURO Solutions Ar - Gestionar envíos**:

1. Detalle de pedido.
2. Acción masiva en el listado de pedidos.

URL para ambos:
`https://taurosolutions.ar/portal/tienda/tiendanube/pedidos`

El endpoint exige sesión TAURO y comprueba que `store` pertenezca al usuario.

## Labels API

El backend ya implementa los endpoints obligatorios `generate` y `cancel`, pero
no registra `callback_labels_url` mientras el worker OCA no haya superado UAT.
Cuando se habilite, usará una URL HTTPS y un secreto independiente por tienda.
No habilitar todavía suspensión ni reactivación opcionales.

## NubeSDK

- Marcar **Uses NubeSDK**.
- Bundle: `tiendanube_nube_app/dist/main.min.js`.
- Validar en la tienda demo con NubeSDK DevTools antes de solicitar aprobación.

## Assets preparados

- Icono: `docs/tiendanube/assets/tauro-nacional-icon-600.png` (PNG 600×600).
- Ayuda: `docs/tiendanube/assets/screenshot-ayuda-1600x800.png` (PNG 1600×800).
- Faltan capturas reales de instalación, checkout y gestión en tienda demo.

## Webhooks registrados por API, por tienda

- `order/created`
- `order/updated`
- `order/cancelled`
- `app/uninstalled`
- `app/suspended`
- `app/resumed`

## Avisos de privacidad configurados en Partners, por aplicación

- `store/redact`
- `customers/redact`
- `customers/data_request`

No intentar crearlos con POST `/webhooks` ni exigir que aparezcan en su
listado. Después de verificar las tres URLs y su recepción firmada, establecer
`TIENDANUBE_PRIVACY_WEBHOOKS_CONFIRMED=true`. Sin esa confirmación, TAURO no
completa una instalación ni reactiva la app.

## Datos que todavía requieren decisión humana

- App ID, Partner ID y Store ID demo.
- Estructura de precio y tratamiento impositivo.
- Horarios y SLA de soporte niveles 1, 2, técnico, comercial y financiero.
- Credenciales de la cuenta demo.
- Fecha objetivo de go-live.
