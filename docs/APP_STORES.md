# TAURO en las tiendas — Play Store y App Store

**Estado (26/08/2026):** el portal ya es una PWA completa (manifest, service
worker, íconos, standalone, offline). Eso significa que **hoy ya se instala**
en celular y escritorio desde el navegador, sin tienda ni costo. Las tiendas
son el paso siguiente: envolver esa misma PWA en una cáscara nativa.

Pendiente aparte, no bloquea la app: integraciones Andreani / OCA / FedEx.
La app abre la web; lo que se sume a la web aparece en la app solo.

## Fases

| Fase | Qué | Costo | Riesgo de rechazo |
|---|---|---|---|
| 0 | PWA instalable (**ya está**) | 0 | — |
| 1 | Play Store (TWA) | USD 25 único | Bajo |
| 2 | App Store (wrapper + push) | USD 99/año | Medio (regla 4.2) |

## Qué ya está en el código

- `GET /.well-known/assetlinks.json` — Android. Lee `ANDROID_PACKAGE_NAME`
  y `ANDROID_ASSETLINKS_SHA256` (huellas separadas por coma). Vacío = `[]`.
- `GET /.well-known/apple-app-site-association` — iOS, fase 2. Lee
  `APPLE_TEAM_ID` e `IOS_BUNDLE_ID`. Vacío = sin apps.
- Tests: `tests/test_app_stores.py`.

Nombre de paquete acordado: **`ar.taurosolutions.portal`** (mismo para
Android e iOS).

## Fase 1 — Play Store, paso a paso

**Leandro (identidad y plata):**
1. Cuenta de desarrollador en https://play.google.com/console — USD 25,
   pago único. Con la cuenta de Google de la empresa.

**Claude / quien empaquete:**
2. Ir a https://www.pwabuilder.com → pegar `https://taurosolutions.ar/portal/home`.
   PWABuilder valida la PWA (ya pasa) y genera el paquete Android.
3. En "Package for stores → Android": package ID `ar.taurosolutions.portal`,
   nombre "TAURO", firmar con clave nueva (PWABuilder la crea). **Guardar el
   .keystore y su contraseña** — sin eso no se pueden publicar
   actualizaciones nunca más.
4. PWABuilder entrega: `app-release.aab` (sube a Play), `assetlinks.json`
   con la **huella SHA-256** y el keystore.

**Leandro (Railway, un solo dato):**
5. Railway → Variables → `ANDROID_ASSETLINKS_SHA256` = la huella SHA-256 que
   dio PWABuilder. Sin redeploy: el endpoint la lee al vuelo.
   Verificar: `curl https://taurosolutions.ar/.well-known/assetlinks.json`
   tiene que mostrar la huella.

**Leandro (Play Console):**
6. Crear app → subir el `.aab` → completar ficha (descripción, capturas,
   ícono 512, categoría "Negocios"), política de privacidad
   (`https://taurosolutions.ar/privacidad` — ver pendiente abajo),
   cuestionario de contenido → enviar a revisión. Suele aprobarse en días.

> Si Play muestra la app con barra de navegador arriba, es que la huella no
> coincide con la que sirve `assetlinks.json`. Revisar paso 5.

## Fase 2 — App Store (cuando la 1 esté andando)

- Cuenta Apple Developer: USD 99/año, requiere D-U-N-S para empresa
  (gratis, tarda ~1 semana) o cuenta individual.
- Wrapper con Capacitor (proyecto iOS que abre la web) + **push
  notifications** ("entró una venta", "guía lista"): es lo que hace que Apple
  no lo rechace como "sólo una web" (guideline 4.2).
- Cargar `APPLE_TEAM_ID` en Railway para los universal links.
- Se compila y sube desde la Mac con Xcode → App Store Connect → revisión
  (más estricta y lenta que Google).

## Pendientes que las tiendas van a pedir

- **Página de privacidad pública** (`/privacidad`): ambas tiendas la exigen
  con URL. No existe todavía.
- **Capturas** de la app (celular): 2–8 imágenes. Se sacan del portal en
  modo standalone.
- **Ícono 512×512 sin transparencia** para la ficha (el maskable ya sirve).
