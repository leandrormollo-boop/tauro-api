# TAURO Solutions para Shopify

Configuración Shopify CLI para la app pública embebida de TAURO. App Home se
sirve desde `https://taurosolutions.ar/shopify/app` dentro de Shopify Admin; el
backend FastAPI canónico vive en la raíz de este repositorio.

## Producto v1

1. Shopify abre App Home dentro de Admin y carga App Bridge desde
   `https://cdn.shopify.com/shopifycloud/app-bridge.js`.
2. App Bridge obtiene un ID token (session token) corto para cada lectura del
   backend; TAURO valida firma HS256, `aud`, `iss`, `dest`, `exp` y `nbf` y
   exige una instalación pública exacta de esa tienda.
3. App Home muestra el estado del vínculo y un resumen de pedidos de la tienda
   autenticada, sin datos personales del comprador.
4. Los webhooks sincronizan el pedido con el portal TAURO. El cliente genera la
   guía en el portal y TAURO publica fulfillment + tracking en Shopify.

La autenticación embebida no depende de cookies de terceros. El HTML inicial
es un shell sin datos privados y `/shopify/app/data` sólo responde con un ID
token válido. OAuth conserva un `state` firmado y vuelve a la URL de Admin
derivada del dominio `shop` verificado por Shopify.

TAURO **no cotiza en el checkout** en v1. No hay CarrierService ni
`write_shipping`; el comerciante conserva sus tarifas nativas de Shopify.

## Contenido

- `shopify.app.toml`: App Home, scopes y webhooks de privacidad.
- `tests/app-config.test.mjs`: invariantes locales del manifiesto.
- `docs/PILOT_READINESS.md`: evidencia y gates para el piloto.

Los webhooks operativos de pedidos, catálogo, inventario y desinstalación se
registran desde el backend durante OAuth. No hay extensiones de checkout ni
runtime duplicado dentro de esta carpeta.

## Validación local

```sh
npm ci
npm test
npm run typecheck
npm run build
shopify app config validate --json
```

No ejecutar `shopify app dev`, `shopify app deploy` ni cambiar el Dev Dashboard
sin aprobación explícita. La validación local no prueba OAuth, ID tokens,
webhooks ni fulfillment contra Shopify: eso requiere una development store.

## Contrato de entorno del backend

- `SHOPIFY_PUBLIC_API_KEY`
- `SHOPIFY_PUBLIC_API_SECRET`
- `SHOPIFY_TOKEN_ENCRYPTION_KEY`
- `BASE_URL=https://taurosolutions.ar`

No se guardan secretos en esta carpeta.
