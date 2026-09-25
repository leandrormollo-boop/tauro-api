# Readiness del piloto Shopify

Fecha de preparación local: 2026-09-25.

## Arquitectura cerrada para v1

- Aplicación pública embebida (`embedded = true`).
- App Home: `https://taurosolutions.ar/shopify/app` dentro de Shopify Admin.
- App Bridge CDN es el primer script y Polaris usa el canal estable 1.x.
- Datos de App Home protegidos por ID/session token, sin cookies de terceros.
- JWT validado con HS256, `aud`, `iss`, `dest`, `exp`, `nbf` y dominio
  `*.myshopify.com`; luego se exige el match exacto de la instalación pública.
- CSP `frame-ancestors` dinámica para la tienda autenticable y
  `https://admin.shopify.com`.
- UI mínima: estado de instalación/vínculo y pedidos de esa tienda sin nombre,
  email, teléfono, dirección ni detalle de ítems.
- Sin tarifas TAURO en checkout, `write_shipping` ni CarrierService.
- Flujo: pedido Shopify → portal TAURO → guía → fulfillment con tracking.
- Un solo fulfillment order elegible por pedido en el piloto; cualquier caso
  multiubicación queda en revisión manual, sin mutar Shopify.

## Gate local repetible

```sh
npm ci
npm test
npm run typecheck
npm run build
shopify app config validate --json
```

La CLI debe ejecutarse con telemetría desactivada y la atribución exigida por el
entorno. Ninguno de estos comandos despliega o cambia la aplicación remota.

Evidencia local del 25/09/2026:

- Contratos Python Shopify: 106 aprobados (la prueba ajena de portal/cotizador
  se excluye porque el venv legado es Python 3.9 y ese módulo requiere 3.10+).
- Contratos Node del manifiesto: 5 aprobados; `typecheck` aprobado.
- `shopify app build`: aprobado, sin despliegue.
- `shopify app config validate --json`: `valid: true`, sin issues.

Referencias primarias revisadas:

- [App Home y App Bridge](https://shopify.dev/docs/api/app-home/latest)
- [ID tokens de aplicaciones embebidas](https://shopify.dev/docs/apps/build/authentication-authorization/id-tokens)
- [Token exchange y autenticación embebida](https://shopify.dev/docs/apps/build/authentication-authorization/implement-token-exchange)
- [Configuración de aplicaciones con Shopify CLI](https://shopify.dev/docs/apps/build/cli-for-apps/app-configuration)
- [Protección de iframes con CSP](https://shopify.dev/docs/apps/build/security/set-up-iframe-protection)

## Bloqueos externos antes del piloto real

- Configurar credenciales públicas y clave de cifrado en un entorno controlado.
- Confirmar en Dev Dashboard `embedded`, App URL, redirect, scopes y webhooks.
- Instalar en una development store limpia y activa.
- Verificar que App Home obtiene un ID token sin cookies de terceros y que un
  token vencido, de otra audiencia o de otra tienda recibe `401`.
- Completar OAuth/reinstalación y confirmar el retorno a la superficie embebida.
- Crear un pedido de prueba, comprobar su ingreso al portal, emitir una guía de
  prueba y confirmar fulfillment + tracking.
- Registrar evidencia del recorrido y del consentimiento de scopes.
- Obtener aprobación Level 2 de protected customer data en Dev Dashboard.
- Obtener confirmación escrita sobre el cobro externo del flete físico o, si
  Shopify lo clasifica como cargo de app, implementar App Pricing/Billing.
- Preparar screencast en inglés/subtitulado, instrucciones del revisor y prueba
  de los tres webhooks de compliance por HTTPS.

No desplegar ni presentar al App Store hasta que esos puntos tengan evidencia.
