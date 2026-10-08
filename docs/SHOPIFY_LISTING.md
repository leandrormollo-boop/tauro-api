# Listing en Shopify App Store — Tauro Solutions Ar (borrador para revisar)

Textos y checklist para la ficha pública y para el revisor de Shopify.
Revisar antes de enviar. `client_id` de la app pública: ver `shopify_app/shopify.app.toml`.

## Ficha

- **Nombre:** Tauro Solutions Ar
- **Tagline (≤ 62):** Tus ventas listas para despachar, con guía y seguimiento.
- **Resumen (ES):** Cada venta con envío entra sola a tu portal TAURO, lista para
  convertirse en guía con un click. Elegís el courier (DHL, OCA, Andreani…),
  emitís la guía y TAURO informa a Shopify el fulfillment y el tracking para que
  tu comprador reciba el aviso. Sin planillas ni copiar datos.
- **Summary (EN):** Every order that needs shipping lands in your TAURO portal,
  ready to become a shipping label in one click. Pick the carrier, create the
  label, and TAURO reports fulfillment and tracking back to Shopify so your
  customer gets notified. No spreadsheets, no retyping.
- **Beneficios clave (3):**
  1. Pedidos de Shopify → solicitud logística automática en TAURO.
  2. Multi-courier nacional e internacional desde un solo lugar.
  3. Fulfillment y tracking devueltos a Shopify; el comprador se entera solo.
- **Categoría:** Shipping and delivery → Order and shipping management.
- **Precio:** *Free to install.* El servicio logístico (flete) se contrata y
  factura con TAURO fuera de Shopify. ⚠️ Pendiente confirmación escrita de
  Shopify (ver `SHOPIFY_BILLING_CONSULTA.md`); si lo consideran app charge,
  implementar Billing API antes de enviar.
- **URLs:** app `https://taurosolutions.ar/shopify/app` · privacidad
  `https://taurosolutions.ar/privacidad` · términos `https://taurosolutions.ar/terminos`
  · soporte `cotizaciones@taurosolutions.ar`.

## Justificación de scopes (el revisor la pide)

| Scope | Para qué |
|---|---|
| `read_orders` | Recibir las ventas con envío y armar la solicitud logística |
| `read_products` | Espejo del catálogo (SKU, peso, imagen) para cotizar y documentar |
| `read_inventory`, `read_locations` | Stock por ubicación para preparar el despacho |
| `write_merchant_managed_fulfillment_orders` | Informar fulfillment y tracking cuando se emite la guía |

No se pide `write_shipping`: la app **no** cotiza en el checkout ni usa CarrierService.

## Instrucciones para el revisor

1. Instalar desde la development store (link de instalación o App Store).
2. Aceptar permisos → vuelve al App Home embebido (Inicio / Pedidos / Envíos).
3. Vincular con TAURO: usar la **cuenta de prueba** (crear antes de enviar: usuario
   `DEMOSHOPIFY`, contraseña a definir, sin emisión real de guías). El email de la
   dev store debe coincidir con el email de esa cuenta TAURO.
4. Crear un pedido de prueba con dirección de envío en la dev store → aparece en
   TAURO (Mi tienda → Pendientes) y en la pestaña Pedidos del App Home.
5. En TAURO, generar la solicitud; el fulfillment se informa a Shopify al emitir.
   En la cuenta demo la emisión está deshabilitada (cuesta dinero real): se
   documenta con captura/screencast del flujo productivo.
6. Desinstalar: la app limpia la instalación (webhook `app/uninstalled`).
7. Compliance webhooks (`customers/data_request`, `customers/redact`,
   `shop/redact`) responden 200 firmados; están en `shopify.app.toml`.

## Assets (pendiente)

- Ícono 1200×1200 (PNG sin transparencia) — partir de `static/img/pwa/icon-512.png`.
- 3 capturas 1600×900 del App Home: Inicio, Pedidos, Envíos (staging o prod).
- Screencast ≤ 2 min: instalación → vinculación → pedido → solicitud → fulfillment.
- Texto "Notas para el revisor" = sección anterior + credenciales de la cuenta demo.
