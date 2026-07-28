# Publicar TAURO Solutions en el Shopify App Store

Guía para cuando quieras que la app aparezca en el App Store y cualquier
comerciante pueda instalarla con un click (como Envia).

---

## Estado: qué ya está resuelto

- OAuth completo (instalar / desinstalar / reinstalar)
- Webhooks de pedidos con firma verificada
- **Webhooks de privacidad obligatorios** (`customers/data_request`,
  `customers/redact`, `shop/redact`) — verificados: rechazan firmas falsas
- App embebida en el admin con App Bridge
- HTTPS con dominio propio (taurosolutions.ar)
- CarrierService (tarifas en el checkout) para las tiendas cuyo plan lo permite

## Qué falta — y lo tenés que cargar vos

Todo se hace en **partners.shopify.com** → Apps → TAURO Solutions →
*Distribución* → elegir **Public distribution** → completar la ficha.

### 1. Ficha de la app (listing)

| Campo | Qué poner |
|---|---|
| **Nombre** | TAURO Solutions |
| **Ícono** | 1200×1200 px, fondo sólido. Usar la T de la marca sobre violeta `#7c5cf6` |
| **Tagline** | Envíos internacionales puerta a puerta desde Argentina |
| **Categoría** | Shipping and delivery |
| **Idiomas** | Español (agregar inglés si querés alcance regional) |
| **Precio** | Free to install (cobrás el flete por fuera, no por Shopify) |

**Descripción corta** (sugerida):
> Vendé al mundo desde tu tienda argentina. Cada venta con envío
> internacional aparece lista para despachar: cotizás FedEx, UPS y DHL,
> generás la guía con un click y tu comprador recibe su tracking
> automáticamente.

**Descripción larga** — puntos a desarrollar:
- Las ventas entran solas al portal, sin copiar datos a mano
- Comparás los tres couriers y elegís
- La guía se genera con un click y el pedido pasa a "enviado" en Shopify
- El comprador recibe su número de seguimiento sin que hagas nada
- Vos decidís qué envío ve tu comprador: precio real, con margen, fijo o gratis
- Una sola cuenta corriente: pagás todo junto, sin sorpresas

### 2. Capturas de pantalla (mínimo 3, 1600×900)

Sacarlas de:
1. `/portal/tienda` con pedidos pendientes → "Las ventas entran solas"
2. `/portal/envios/nuevo?pedido_tienda=X` → "Todo prellenado, un click"
3. `/web` con el comparador de couriers → "FedEx, UPS y DHL comparados"

### 3. Páginas legales (obligatorias)

Shopify pide URLs públicas:
- **Política de privacidad** — qué datos tomás, para qué, cuánto los guardás
- **Términos del servicio**
- **Email de soporte** — taurosolutionsar@gmail.com
- **URL de ayuda / documentación**

> Faltan estas páginas. Se pueden publicar en taurosolutions.ar/privacidad
> y /terminos.

### 4. Prueba en tienda de desarrollo

Shopify exige que la app funcione en una **development store** limpia.
Se crea gratis desde el Partner Dashboard → Stores → Add store.

### 5. Enviar a revisión

Botón **Submit app** en el Dev Dashboard. A partir de ahí:
- Revisión inicial: **2 a 4 semanas**
- Es normal que pidan cambios en la primera vuelta
- Responder rápido acorta mucho el proceso

---

## Lo que NO hace falta para operar

Podés seguir sumando clientes **sin publicar**: les pasás el link directo

    https://taurosolutions.ar/shopify/install?shop=SUTIENDA.myshopify.com

y la instalan igual. La única diferencia es el cartel de "app no publicada"
y que no aparece en las búsquedas del App Store.

El App Store es un **canal de captación**, no un requisito para funcionar.
