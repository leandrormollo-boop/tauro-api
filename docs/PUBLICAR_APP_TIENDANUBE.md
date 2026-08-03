# Conectar TAURO Solutions con Tiendanube

Guía para dejar la integración de Tiendanube operativa. Actualizada 03/08/2026.

---

## Estado del código: LISTO

El backend está completo y cableado al mismo pipeline que Shopify:

- OAuth: canje del `code` por token permanente, alta de la instalación.
- **Anti-CSRF**: el botón del portal siembra un `state` en cookie; el callback
  sólo ata la tienda a tu cuenta si ese state vuelve (un callback disparado por
  un tercero no puede colgar una tienda ajena en tu cuenta).
- Webhooks firmados con HMAC-SHA256 en **hexadecimal** (Tiendanube firma
  distinto a Shopify) verificados con el client_secret.
- El webhook trae sólo el id → se busca el pedido completo en la API.
- Parser propio: dirección, piso, items, peso, y el **flete cobrado**
  (`shipping_cost_customer`) para comparar contra el costo real de la guía.
- Venta → solicitud de guía automática (la guía NO se emite sola).
- Al emitir la guía, el pedido pasa a "enviado" (shipped) con tracking en
  Tiendanube.
- Registro automático de webhooks (order created/updated/cancelled,
  app/uninstalled) y desinstalación.
- El botón "Instalar app de Tiendanube" aparece solo en el portal
  (`/portal/tienda`) cuando la app está configurada.

La integración se **enciende sola** cuando estén las dos env vars.

---

## Qué tenés que cargar vos

### 1. App en el Partners Portal de Tiendanube (partners.tiendanube.com)
- Crear/registrar la app de TAURO y obtener **Client ID** y **Client Secret**.
  (Si figura "en trámite", esperar la aprobación de Tiendanube.)
- **Redirect URI** (exacta):
  `https://taurosolutions.ar/integraciones/tiendanube/callback`
- **Permisos**: leer pedidos + escribir estado de envío (marcar shipped +
  tracking). Sin el write de órdenes, el aviso de "enviado" falla.

### 2. Credenciales (Railway → Variables)
- `TIENDANUBE_CLIENT_ID`
- `TIENDANUBE_CLIENT_SECRET`
- `BASE_URL=https://taurosolutions.ar` (o dejar el default).

Apenas las dos existan, `app_configurada()` da True: el callback deja de
responder 503, el webhook empieza a procesar y en el portal aparece el botón
de instalación.

### 3. Cómo instala un comerciante
Entra a su portal TAURO → **Mi tienda** → **Instalar app de Tiendanube** →
autoriza en Tiendanube → vuelve con la tienda ya atada a su cuenta.

### 4. Primera venta real
Como con cualquier courier nuevo, mirar de cerca la primera venta de Tiendanube
end-to-end: parseo → solicitud automática → al emitir, que el "enviado" con
tracking llegue de vuelta a Tiendanube.

---

## Diferencia con Shopify (para no confundirlas)
- **Shopify**: se conecta manual (dominio + secreto de webhook) o por link
  directo `/shopify/install`. Firma HMAC en base64.
- **Tiendanube**: se conecta por OAuth con un botón. Firma HMAC en hexadecimal,
  header `x-linkedstore-hmac-sha256`. Token que no vence, tienda identificada
  por `store_id` numérico (no por dominio).
