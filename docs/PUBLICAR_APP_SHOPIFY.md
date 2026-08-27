# Publicar TAURO Solutions en el Shopify App Store

Guía para que la app aparezca en el App Store y cualquier comerciante la
instale con un click. Actualizada 03/08/2026.

---

## Estado del código: LISTO PARA DESPLEGAR Y REAUTORIZAR

El flujo histórico de pedidos y tracking fue verificado e2e en producción el
28/07 con Pesca Jacks. El espejo nuevo de catálogo y stock está validado por
suite automatizada, PostgreSQL real y colección Postman; su prueba e2e contra
Shopify requiere desplegar y aceptar una vez los permisos nuevos:

- OAuth completo (instalar / desinstalar / reinstalar), con `state` anti-CSRF y
  HMAC verificado; dominio validado con regex estricta (sin open-redirect). Si
  el navegador embebido bloquea la cookie de `state`, se instala sin vincular
  y el dueño debe reclamarla luego desde su portal.
- Scopes mínimos: `read_orders`, `read_products`, `read_inventory`,
  `write_merchant_managed_fulfillment_orders`
  (`write` ya incluye lectura del mismo recurso y Shopify omite el `read` al
  devolver los permisos otorgados).
  (Se sacó `write_shipping` el 03/08 — era del CarrierService, ya retirado.)
- Webhooks de pedidos, productos e inventario con firma verificada, cola durable,
  idempotencia y reintentos.
- Catálogo espejado por variante, incluso sin SKU: imagen, precio, peso, HS code,
  país de origen y stock por ubicación. Shopify sigue siendo la fuente de verdad.
- API B2B `GET /stock`, paginada y autenticada con `X-API-Key`, para leer el
  catálogo propio sin consultar Shopify en cada request ni exponer márgenes.
- Sincronización completa inicial y reconciliación periódica; las bajas sólo se
  aplican después de una lectura completa exitosa para no vaciar catálogos ante
  una caída de Shopify.
- **Webhooks de privacidad obligatorios** (`customers/data_request`,
  `customers/redact`, `shop/redact`) — verificados: rechazan firmas falsas y no
  loguean datos personales.
- App embebida con App Bridge y `frame-ancestors` por tienda.
- Venta → solicitud de guía automática (la guía NO se emite sola).
- Cierre del círculo: al emitir la guía en TAURO, el pedido queda "enviado" en
  Shopify con el tracking y se avisa al comprador.
- Llamadas activas al Admin API migradas a GraphQL 2026-07 (identidad de la
  tienda, suscripción de webhooks y fulfillment/tracking).
- Páginas legales YA servidas: taurosolutions.ar/privacidad y /terminos.

> **Importante — NO es del checkout:** la app ya **no cotiza en el checkout**
> (el CarrierService se retiró el 28/07). El precio del envío lo pone el
> comerciante con sus tarifas de Shopify; TAURO despacha y cobra el flete por
> fuera. No prometer "cotizás en el checkout" en la ficha: es motivo de rechazo.

---

## Qué tenés que cargar vos

### 1. Credenciales (Railway → Variables)
- `SHOPIFY_API_KEY` y `SHOPIFY_API_SECRET` (de la app en el Partner Dashboard).
- `SHOPIFY_TOKEN_ENCRYPTION_KEY`: secreto largo y exclusivo para cifrar en reposo
  los tokens de las tiendas. Si se omite, TAURO deriva una clave del API secret
  para mantener compatibilidad, pero conviene configurarlo antes de producción.
  Puede agregarse después sin cortar tokens anteriores: el descifrado conserva
  el API secret como clave transitoria mientras los nuevos ya usan la exclusiva.
- `BASE_URL=https://taurosolutions.ar` (o dejar el default).

### 2. Partner Dashboard (partners.shopify.com → Apps → TAURO → Configuration)
- **App URL**: `https://taurosolutions.ar/shopify/install`
- **Allowed redirection URL(s)**: `https://taurosolutions.ar/shopify/callback`
- **Access scopes**:
  `read_orders,read_products,read_inventory,write_merchant_managed_fulfillment_orders`
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
Si el revisor lo objeta: el cargo es por un servicio de logística externo
(excepción válida).

### 6. Revisión
Submit → primera vuelta suele tardar **2-4 semanas**; es normal que pidan
cambios. Responder rápido acorta el proceso.

---

## No hace falta publicar para operar
Podés sumar clientes con el link directo:

    https://taurosolutions.ar/shopify/install?shop=SUTIENDA.myshopify.com

La instalan igual; sólo no aparece en las búsquedas del App Store. El App Store
es un canal de captación, no un requisito para funcionar.

### Tiendas que ya estaban instaladas

Al sumar catálogo e inventario, las tiendas existentes deben aceptar los nuevos
permisos una sola vez. TAURO detecta automáticamente los scopes viejos y las
redirige al consentimiento. No se les pide un token ni un secreto manual.

---

## Ojo antes de producción (no bloquea publicar, pero sí operar)
Antes de pasar `FEDEX_ENVIRONMENT=production`, recalibrar `WEB_DESC_FEDEX_PCT`
(ver RELEVO.md, regla 8) o las cotizaciones que alimentan las solicitudes salen
bajo costo. La primera guía real de cada courier, mirarla de cerca.
