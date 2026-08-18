# PWA del portal de clientes

El **portal** (`/portal/…`) es instalable como app en el teléfono. El admin y
la web pública quedan afuera a propósito: `templates/base.html` (exclusivo del
portal) es el único que declara el manifest y registra el service worker.

## Piezas

| Pieza | Dónde | Qué hace |
|---|---|---|
| Manifest | `GET /portal/manifest.webmanifest` (`endpoints/portal_cliente.py`) | Identidad de la app: `id`/`scope` `/portal/`, `start_url` `/portal/home`, `display: standalone`, `lang es-AR`, íconos 192/512 `any` + `maskable`. |
| Íconos | `static/img/pwa/icon-*.png` | Isotipo TAURO blanco sobre `#0c0a14`, generados desde `static/img/logo-mark-white.png`. Los `maskable` llevan margen extra (zona segura). Si se regeneran, subir `_PWA_ICONOS_V`. |
| Service worker | `static/js/portal-sw.js`, servido en `GET /portal/sw.js` | Red-only para todo lo autenticado. Precachea SOLO `/portal/offline`. Limpia únicamente versiones viejas `tauro-portal-*` al activar. Sin push todavía. |
| Registro + instalación | `static/js/portal-pwa.js` | Registra el SW (`scope /portal/`), activa versiones nuevas en la próxima carga, maneja la CTA de instalación (beforeinstallprompt / guía iOS). |
| Pantalla offline | `templates/portal/offline.html` → `GET /portal/offline` | Pública, neutra y autocontenida (sin `/static` ni fuentes externas). Reintenta al volver la conexión. |
| Barra móvil | `base.html` (`.tabbar`) + `static/css/tauro.css` | Inicio · Cotizar · Nuevo envío · Mis envíos · Cuenta. Sólo shell autenticado, ≤900px, respeta safe-area, no sale en impresión. |

## Contrato de seguridad (vigilado por `tests/test_portal_pwa.py`)

- El SW **no escribe ninguna respuesta**: no hay `cache.put`; el único
  precache es la pantalla offline pública. HTML autenticado, APIs, PDFs,
  etiquetas, facturas, comprobantes y tokens: **red-only siempre**.
- Métodos ≠ GET y subrecursos ni se interceptan.
- Sin red, una navegación del portal recibe únicamente la pantalla offline
  neutra (o un HTML mínimo equivalente si la caché fue purgada).
- Al actualizar, sólo se eliminan cachés anteriores con prefijo
  `tauro-portal-`; las cachés de otras aplicaciones del dominio no se tocan.
- Las rutas nuevas heredan `Cache-Control: no-store` y la CSP del middleware
  (`main.headers_de_seguridad`); nada del contrato de cookies/auth cambió.
- Después de un logout no queda nada de la sesión en Cache API, localStorage
  ni IndexedDB (el único dato local es el timestamp de "no volver a mostrar"
  de la CTA de instalación).

## Cómo instalar

**Android (Chrome/Edge):** entrar a `https://…/portal/login`, iniciar sesión y
tocar **Instalar** en la tarjeta que aparece al inicio del portal (o menú ⋮ → *Agregar a la
pantalla principal*). El botón usa `beforeinstallprompt`; si ya corre como
app, no aparece nunca.

**iPhone / iPad (Safari):** abrir el portal en Safari → botón **Compartir** →
**Agregar a inicio**. La tarjeta del portal muestra esta guía sola (Safari no
emite `beforeinstallprompt`). Ojo: el link mágico de login abre en Safari, no
dentro de la app instalada — con usuario y contraseña se entra directo.

## Cómo validar

1. `python -m pytest tests/test_portal_pwa.py` (y la suite completa).
2. Chrome DevTools → **Application**:
   - *Manifest*: sin warnings, íconos 192/512 y maskable visibles.
   - *Service workers*: `/portal/sw.js` activado con scope `/portal/`.
   - *Cache storage*: `tauro-portal-vN` contiene **sólo** `/portal/offline`
     — antes y después de navegar logueado (si aparece otra entrada, es un
     bug de seguridad).
3. Lighthouse (móvil) sobre `/portal/login`: check de instalabilidad.
4. DevTools → Network → **Offline** y navegar: debe aparecer la pantalla
   offline neutra; al volver la red, reintenta sola.
5. Responsive 360×800 y 390×844: tabbar sin overflow horizontal y sin tapar
   los botones del wizard. En el último paso, Atrás vive dentro de la misma
   submit-bar que Cancelar/Crear; la tarjeta de instalación está en el flujo
   al inicio de la página y nunca flota sobre controles.

## Actualizaciones

- Cambió `offline.html` → subir `VERSION` en `static/js/portal-sw.js`.
- Cambiaron los íconos → regenerarlos y subir `_PWA_ICONOS_V` en
  `endpoints/portal_cliente.py`.
- El SW se sirve con `no-store`, así que cada navegación chequea si hay
  worker nuevo; la versión nueva se activa en la carga siguiente y borra los
  cachés viejos.
- Push/notificaciones: la base queda anclada en el SW y `portal-pwa.js`,
  pero NO hay permisos ni suscripciones en esta fase.
