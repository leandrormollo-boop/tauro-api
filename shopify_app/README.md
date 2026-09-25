# TAURO Solutions para Shopify

Configuración Shopify CLI para la app pública de TAURO. La app es **externa y
no embebida**: el runtime, OAuth y la experiencia operativa se sirven desde
`https://taurosolutions.ar`. Esta carpeta no despliega el backend ni contiene
una App Home.

## Producto v1

1. Shopify instala TAURO mediante OAuth en `/shopify/install` y vuelve a
   `/shopify/callback`.
2. Los webhooks sincronizan el pedido con el portal TAURO.
3. El cliente revisa el pedido y genera la guía desde el portal.
4. TAURO crea el fulfillment en Shopify, agrega el tracking y solicita que
   Shopify notifique al comprador.

TAURO **no cotiza en el checkout** en v1. No hay CarrierService, extensión de
checkout, App Bridge ni llamadas directas al Admin API desde el navegador. El
comerciante conserva sus tarifas nativas de Shopify y TAURO factura el servicio
logístico por fuera de la plataforma.

## Contenido

- `shopify.app.toml`: URLs, scopes y webhooks de privacidad.
- `tests/app-config.test.mjs`: invariantes locales del producto y manifiesto.
- `docs/PILOT_READINESS.md`: evidencia y gates para el piloto.

El backend canónico está en la raíz de este mismo repositorio. Los webhooks
operativos de pedidos, catálogo, inventario y desinstalación se registran desde
el backend durante OAuth.

## Validación local

```sh
npm ci
npm test
npm run typecheck
npm run build
shopify app config validate --json
```

No ejecutar `shopify app dev`, `shopify app deploy` ni cambiar el Dev Dashboard
sin aprobación explícita. La validación local no prueba OAuth, entrega de
webhooks ni fulfillment contra Shopify: eso requiere una development store.

## Contrato de entorno del backend

- `SHOPIFY_PUBLIC_API_KEY`
- `SHOPIFY_PUBLIC_API_SECRET`
- `SHOPIFY_TOKEN_ENCRYPTION_KEY`
- `BASE_URL=https://taurosolutions.ar`

No se guardan secretos en esta carpeta.
