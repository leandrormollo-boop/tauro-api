# Configuración del Portal de Partners

Valores candidatos para **TAURO Nacional**. Los secretos no se copian a este
archivo ni a tickets de homologación.

## Aplicación

- Tipo: Tienda de Aplicaciones.
- Categoría: Shipping / Envíos.
- País e idioma: Argentina / español.
- Tipo de entrega: `ship`.
- Envío internacional: no.
- Multi-CD: no en la primera versión.
- Handle candidato: `tauro-nacional` (confirmar disponibilidad en el portal).

## URLs

- Redirect OAuth: `https://taurosolutions.ar/integraciones/tiendanube/callback`
- Panel: `https://taurosolutions.ar/portal/tienda`
- Preferencias: `https://taurosolutions.ar/portal/tienda`
- Privacidad: `https://taurosolutions.ar/privacidad`
- Términos: `https://taurosolutions.ar/terminos`
- Soporte: `https://taurosolutions.ar/ayuda/tiendanube`
- Webhook general: `https://taurosolutions.ar/integraciones/tiendanube/webhook`
- Store redact: `https://taurosolutions.ar/integraciones/tiendanube/webhook`
- Customer redact: `https://taurosolutions.ar/integraciones/tiendanube/webhook`
- Customers data request: `https://taurosolutions.ar/integraciones/tiendanube/webhook`

## Permisos mínimos candidatos

- `write_shipping`
- `read_orders`
- `write_fulfillment_orders`
- `read_customers`

El Partner Team debe confirmar la disponibilidad de
`write_fulfillment_orders`. Todo write scope implica su read equivalente. No
solicitar Products ni Locations mientras el producto no use esos endpoints.

## Admin links

Crear dos links con el texto **TAURO Nacional - Gestionar envíos**:

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

## Webhooks esperados

- `order/created`
- `order/updated`
- `order/cancelled`
- `app/uninstalled`
- `app/suspended`
- `app/resumed`
- `store/redact`
- `customers/redact`
- `customers/data_request`

## Datos que todavía requieren decisión humana

- App ID, Partner ID y Store ID demo.
- Estructura de precio y tratamiento impositivo.
- Horarios y SLA de soporte niveles 1, 2, técnico, comercial y financiero.
- Credenciales de la cuenta demo.
- Fecha objetivo de go-live.
