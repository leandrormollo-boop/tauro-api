# Publicar TAURO Solutions en el Shopify App Store

Guía para que la app aparezca en el App Store y cualquier comerciante la
instale con un click. Actualizada 27/08/2026.

---

## Estado del código: LISTO PARA REVISIÓN Y REAUTORIZACIÓN

El flujo histórico de pedidos y tracking fue verificado e2e en producción el
28/07 con Pesca Jacks. El espejo nuevo de catálogo y stock está validado por
suite automatizada, PostgreSQL real y colección Postman; su prueba e2e contra
Shopify requiere desplegar y aceptar una vez los permisos nuevos:

- OAuth completo (instalar / desinstalar / reinstalar), con `state` anti-CSRF y
  HMAC verificado; dominio validado con regex estricta (sin open-redirect). Un
  callback sin la cookie de `state`, con estado ausente o distinto se rechaza y
  debe reiniciarse desde Shopify.
- Token offline expirable y renovable: el canje pide `expiring=1`, TAURO guarda
  cifrados el access/refresh token y rota el par antes de vencer, como exige
  Shopify para apps públicas nuevas desde el 01/04/2026.
- Scopes mínimos: `read_orders`, `read_products`, `read_inventory`, `read_locations`,
  `write_merchant_managed_fulfillment_orders`
  (`write` ya incluye lectura del mismo recurso y Shopify omite el `read` al
  devolver los permisos otorgados).
  (Se sacó `write_shipping` el 03/08 — era del CarrierService, ya retirado.)
- Webhooks de pedidos, productos e inventario con firma verificada, cola durable,
  idempotencia y reintentos. La vinculación sólo se activa después de consultar
  por GraphQL y verificar el conjunto exacto de suscripciones de esa generación;
  ante un alta parcial o una carrera queda cerrada y el OAuth puede reintentarse.
- Catálogo espejado por variante, incluso sin SKU: imagen, precio, peso, HS code,
  país de origen y stock por ubicación. Shopify sigue siendo la fuente de verdad.
- API B2B `GET /stock`, paginada y autenticada con `X-API-Key`, para leer el
  catálogo propio sin consultar Shopify en cada request ni exponer márgenes.
- Sincronización completa inicial y reconciliación periódica; las bajas sólo se
  aplican después de una lectura completa exitosa para no vaciar catálogos ante
  una caída de Shopify.
- **Webhooks de privacidad obligatorios** (`customers/data_request`,
  `customers/redact`, `shop/redact`): rechazan firmas falsas, atan body/header
  al mismo dominio y no loguean datos personales. `data_request` se confirma
  sólo después de persistir la obligación; el admin permite exportarla y
  resolverla. Las redacciones alcanzan pedidos, guías, direcciones y labels.
- App externa (`embedded = false`): OAuth vuelve al acceso TAURO en una
  navegación principal. El dominio de la tienda sólo inicia OAuth; los datos
  operativos se muestran exclusivamente con una sesión TAURO del cliente
  vinculado. No usa App Bridge ni se ejecuta dentro de un iframe.
- Venta → solicitud de guía automática (la guía NO se emite sola).
- Cierre del círculo: al emitir la guía en TAURO, el pedido queda "enviado" en
  Shopify con el tracking y se avisa al comprador.
- Admin API exclusivamente por GraphQL 2026-07 (identidad de la tienda,
  suscripción de webhooks, catálogo, inventario y fulfillment/tracking). El
  helper REST y el CarrierService retirado fueron eliminados del código.
- Páginas legales YA servidas: taurosolutions.ar/privacidad y /terminos.

> **Importante — NO es del checkout:** la app ya **no cotiza en el checkout**
> (el CarrierService se retiró el 28/07). El precio del envío lo pone el
> comerciante con sus tarifas de Shopify; TAURO despacha y cobra el flete por
> fuera. No prometer "cotizás en el checkout" en la ficha: es motivo de rechazo.

---

## Configuración operativa

### 1. Credenciales (Railway → Variables) — cargadas
- `SHOPIFY_PUBLIC_API_KEY` y `SHOPIFY_PUBLIC_API_SECRET` (app pública TAURO):
  toda instalación nueva usa este par.
- Durante la migración de Pesca Jacks, conservar `SHOPIFY_API_KEY` y
  `SHOPIFY_API_SECRET` con la app histórica. Sus webhooks siguen aceptándose
  hasta que la tienda reautorice la app pública, sin mezclar aplicaciones.
- Opcionalmente, después renombrar el par histórico a
  `SHOPIFY_LEGACY_API_KEY` / `SHOPIFY_LEGACY_API_SECRET`.
- `SHOPIFY_TOKEN_ENCRYPTION_KEY`: secreto largo y exclusivo para cifrar en reposo
  los tokens de las tiendas. Si se omite, TAURO deriva una clave del API secret
  para mantener compatibilidad, pero conviene configurarlo antes de producción.
  Puede agregarse después sin cortar tokens anteriores: el descifrado conserva
  ambos API secrets como claves transitorias mientras los nuevos ya usan la
  exclusiva.
- `BASE_URL=https://taurosolutions.ar` (o dejar el default).

### 2. Dev Dashboard → Apps → TAURO → Configuration — desplegada por CLI
- **App URL**: `https://taurosolutions.ar/shopify/install`
- **Allowed redirection URL(s)**: `https://taurosolutions.ar/shopify/callback`
- **Access scopes**:
  `read_orders,read_products,read_inventory,read_locations,write_merchant_managed_fulfillment_orders`
  (exactamente los mismos que pide el OAuth, sin `write_shipping`).
- **Compliance webhooks** (van SÍ o SÍ acá, no por API — Shopify los prueba):
  - customers/data_request → `https://taurosolutions.ar/shopify/webhook/customers/data_request`
  - customers/redact → `https://taurosolutions.ar/shopify/webhook/customers/redact`
  - shop/redact → `https://taurosolutions.ar/shopify/webhook/shop/redact`

### 3. Distribution → Public distribution — ficha del listing

| Campo | Qué poner |
|---|---|
| **Nombre** | TAURO Solutions |
| **Ícono** | 1200×1200 px, la T de la marca sobre violeta `#7c5cf6` |
| **Tagline** | Envíos internacionales puerta a puerta desde Argentina |
| **Categoría** | Shipping and delivery |
| **Precio** | Free to install (cobrás el flete por fuera, no por Shopify) |

**Descripción corta** (sin prometer checkout):
> Vendé al mundo desde tu tienda argentina. Cada venta con envío aparece lista
> para despachar: generás la guía con un click y tu comprador recibe su tracking
> automáticamente.

**Descripción larga** — puntos:
- Las ventas entran solas al portal, sin copiar datos a mano.
- La guía se genera con un click y el pedido pasa a "enviado" en Shopify.
- El comprador recibe su número de seguimiento sin que hagas nada.
- Una sola cuenta corriente: pagás todo junto.

**Capturas** (mínimo 3, 1600×900): `/portal/tienda` con pedidos · wizard de
envío prellenado desde un pedido · comparador de couriers en `/web`.

### 4. Probar en una development store limpia
(Partner Dashboard → Stores → Add store) Instalar → venta de prueba → ver la
solicitud automática → emitir guía → ver "enviado" con tracking en Shopify.
Recién ahí, **Submit**.

> El 27/08/2026 la development store usada por Pesca Jacks devolvió
> `Store unavailable`. No declarar el catálogo/stock como verificado e2e hasta
> que Shopify reactive la tienda y se complete el consentimiento OAuth nuevo.

### 5. Facturación
Free to install, flete cobrado por fuera de Shopify → no se usa la Billing API.
En la ficha y en las notas de revisión debe quedar claro que la app es gratuita
y que TAURO factura un servicio logístico real, no una función digital ni una
suscripción de la app. Confirmar esta clasificación con Shopify durante la
revisión; no presentar una excepción como aprobada antes de que la validen.

### 6. Revisión
Enviar la ficha sólo después de la prueba completa en una tienda activa y
responder dentro del mismo hilo si Shopify pide evidencia o cambios.

---

## Instalación antes del listing público

Para una prueba controlada, usar siempre **Instalar app** dentro del Dev
Dashboard o la superficie de distribución que genere Shopify. No pedir al
comerciante que escriba su dominio ni construir un link propio con `?shop=`:
la instalación debe iniciarse en Shopify y OAuth identifica la tienda.

### Tiendas que ya estaban instaladas

Al sumar catálogo e inventario, las tiendas existentes deben aceptar los nuevos
permisos una sola vez. TAURO detecta automáticamente los scopes viejos y las
redirige al consentimiento. La migración también deja cerradas las vinculaciones
que no tienen evidencia durable de haber verificado todos los webhooks. No se
les pide un token ni un secreto manual: se abre la app nuevamente desde Shopify
y se completa el OAuth una sola vez.

---

## Ojo antes de producción (no bloquea publicar, pero sí operar)
Antes de pasar `FEDEX_ENVIRONMENT=production`, recalibrar `WEB_DESC_FEDEX_PCT`
(ver RELEVO.md, regla 8) o las cotizaciones que alimentan las solicitudes salen
bajo costo. La primera guía real de cada courier, mirarla de cerca.
