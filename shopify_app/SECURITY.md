# Política de seguridad

Esta carpeta contiene un manifiesto público y pruebas locales. No debe incluir
secretos, access tokens, refresh tokens, dominios de tiendas piloto ni datos de
clientes.

## Controles esperados en el backend

- Validación HMAC y deduplicación durable de webhooks.
- OAuth con `state` anti-CSRF, dominio Shopify validado y tokens cifrados.
- Scopes mínimos iguales a los declarados en `shopify.app.toml`.
- Webhooks GDPR obligatorios y redacción trazable.
- Operaciones idempotentes de pedidos y fulfillment/tracking.
- Ningún secreto ni dato operativo expuesto en el navegador.

Validar el manifiesto no reemplaza las pruebas de seguridad del backend ni la
homologación en una development store controlada.
