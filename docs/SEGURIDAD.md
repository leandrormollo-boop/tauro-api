# Seguridad de TAURO API

Última verificación contra producción: **03/08/2026** (endurecimiento grande).

Este documento sólo lista lo que está **montado y medido**. Regla que se
aprende una vez: una protección que no está agregada en `main.py` (o en el
router que se incluye) NO existe, por más que un archivo la implemente —
`core/security.py` fue un paquete entero de defensas que nunca se montó y este
documento las daba por vigentes. Antes de escribir una línea acá, medirla con
`curl -I`.

## Lo que está montado

### Content-Security-Policy (main.py, `headers_de_seguridad`)

La CSP fue posible recién cuando la web pública dejó de compilar JSX en el
navegador. Ahora carga un bundle propio (ver "Build de la web"). Política:

```
default-src 'self';
script-src 'self' 'nonce-<por-request>';
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
font-src 'self' https://fonts.gstatic.com;
img-src 'self' data:; connect-src 'self'; object-src 'none';
base-uri 'self'; form-action 'self'; frame-src 'none'
```

- **Scripts: candado.** Sólo corren archivos propios y los `<script>` inline
  que llevan el nonce de ESE request (uno nuevo por request, `token_urlsafe`).
  Un HTML inyectado no ejecuta nada. Cero `eval`, cero handlers `on*` inline
  (se migraron todos a `data-*` cableados en `base.html` / `base_admin.html`).
- **Estilos: `'unsafe-inline'` a propósito.** Los templates tienen ~280
  atributos `style=` y React inyecta `<style>` en runtime. Inyectar CSS no
  ejecuta código; el vector que importa es el script y ese está cerrado.
- **Google Fonts** es el único tercero permitido (hoja + woff2).
- **NO se aplica a `/shopify/*`** (su propio `frame-ancestors` dinámico por
  tienda) ni a `/docs|/redoc|/openapi.json`. El `setdefault` respeta headers
  ya puestos por el endpoint.

### Otras cabeceras

Verificado con `curl -I`:
`Strict-Transport-Security`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`
(geolocation/mic/cam/payment en `()`), `X-Frame-Options: SAMEORIGIN` (salvo
`/shopify`). En `/portal/*` y `/admin/*`: `Cache-Control: no-store` +
`Pragma: no-cache` + `X-Robots-Tag: noindex`. Uvicorn con `--no-server-header`.

### Guardas de entrada (main.py)

- **Host permitido.** Host falsificado → `421`. Se aceptan los dominios TAURO,
  `RAILWAY_PUBLIC_DOMAIN` (valor exacto), `*.railway.internal`, localhost y
  `EXTRA_HOSTS`. **NO** el sufijo `*.up.railway.app` (namespace compartido:
  cualquiera tiene su subdominio ahí). `/health` y `/salud` exentos.
- **Tope de tamaño.** `POST/PUT/PATCH` con `Content-Length` > 2 MB → `413`.
  Las subidas de archivo (multipart/form-data) van a 12 MB — se decide por
  Content-Type, no por lista de rutas, así ningún endpoint de upload queda
  afuera por olvido. `/shopify` también a 12 MB (webhooks, ya con HMAC).
- **CSRF en profundidad.** `POST` a `/portal|/admin` con `Sec-Fetch-Site`
  cross-site u `Origin` de otro host → `403`. Segunda tranca sobre la cookie
  `SameSite=Lax`. Clientes no-navegador (tests, la API B2B server-to-server)
  no mandan esos headers y pasan.

### CORS

`allow_origins` desde `CORS_ORIGINS` (default los dos dominios TAURO),
`allow_credentials=False`, métodos `GET`/`POST`.

### Documentación de la API

`/docs`, `/redoc`, `/openapi.json` → `404` en producción (medido). Se habilitan
sólo con `ENV=DEV`.

### Autenticación

- Passwords **bcrypt** (`servicios/auth.py`). Login del portal por email o ID.
- **MFA TOTP opcional en el admin** (`servicios/totp.py`, sólo stdlib, RFC 6238
  verificado contra los vectores oficiales). Se activa si `ADMIN_TOTP_SECRET`
  está cargada — sin ella el login queda sólo con contraseña (imposible dejar
  al dueño afuera). Anti-replay **por paso de tiempo** (un código no entra dos
  veces ni siquiera intercalado), verificación de tiempo constante, fail-closed
  si el secreto está roto, y el código se consume sólo si la contraseña ya es
  correcta (no se lo puede "quemar" con un intento de contraseña basura).
  Alta con `scripts/generar_totp_admin.py`.
- Cookies de sesión `HttpOnly`, `SameSite=Lax`, `Secure` (`COOKIE_SECURE=1`).
- Magic links de un solo uso con vencimiento.

### API keys — hasheadas

Se guarda `sha256(clave)` en `clientes.api_key_hash`, nunca la clave en claro.
Un dump de la base ya no entrega credenciales vivas. sha256 sin salt alcanza
porque las claves son de alta entropía (`tauro_` + `token_urlsafe(32)`).
La migración (hashea las claves viejas en claro y las borra) corre en el
**arranque**, no en el primer request — así el `ALTER TABLE` no serializa
tráfico detrás de su lock; queda idempotente como red. Botón **"Regenerar API
key B2B"** en el detalle del cliente: muestra la clave UNA vez (no se puede
volver a ver) y rota la anterior. El backup excluye `api_key`, `api_key_hash`
y `password_hash`.

### Rate limits (`servicios/rate_limit.py`, en memoria)

| Endpoint | Tope | Ventana |
|---|---|---|
| `POST /portal/login` | 8 | 5 min |
| `POST /admin/login` | 5 | 5 min |
| magic link | 5 | 15 min |
| recuperar password admin | 3 | 1 h |
| `POST /cotizar-web` | 30 | 5 min |
| `POST /cotizacion-lead` | 5 | 15 min |

`/cotizacion-lead` además: un mail por dirección por día.

La IP para la clave sale de `CF-Connecting-IP` (Cloudflare) o, si no está, del
valor **más a la derecha** de `X-Forwarded-For` — nunca del primero, que lo
pone el cliente y permitía evadir el tope rotando el header.

> El contador vive en memoria del proceso. Con más de un worker el tope se
> multiplica. Mover a Redis o Cloudflare antes de escalar horizontalmente.

### Integración con tiendas

- Webhooks Shopify por **HMAC**; sin/mal firma → 401 (lo verifica
  `scripts/test_checkout_critico.py` en cada deploy).
- OAuth con `state` verificado y dominio validado por regex.
- Al vincular una tienda huérfana se le pregunta a la propia tienda quién es su
  dueño (`GET shop.json`) y se compara contra el mail del cliente.
- Pedidos huérfanos se borran a los 90 días.
- Webhook GDPR `customers/data_request` loguea dominio + tamaño, nunca el cuerpo.

### Build de la web

`web/components/*.jsx` se compila con esbuild a `static/js/app.js` (bundle
único, React adentro, minificado). `npm run build:web` o
`node scripts/build_web.mjs`. **El bundle se commitea**: Railway no tiene node.
CI en `.github/workflows/security-checks.yml` corre `npm audit`.

### Contenedor

`Dockerfile` corre como usuario `tauro` (no root). La app no escribe en disco
(todo va a Postgres y a buffers en memoria).

## Deuda conocida

| Falta | Motivo / riesgo |
|---|---|
| `style-src 'unsafe-inline'` | Los ~280 `style=` inline exigirían un refactor masivo a clases para sacarlo. Inyectar CSS no ejecuta JS: riesgo bajo con el script ya cerrado. |
| Rate limit en memoria | Con >1 worker el tope se multiplica. Redis o Cloudflare. |
| Tope de tamaño evadible por `Transfer-Encoding: chunked` | El chequeo mira `Content-Length`; un body chunked no lo manda. Lo corta el proxy de Railway (mitigación externa). Impacto: presión de memoria del worker, no fuga ni bypass de auth. Tradeoff aceptado; cerrarlo en la app exige leer el body con tope byte a byte. |
| `security_audit` / `auditoria.py` | Existe pero sólo lo llama `core/security.py`, que NO está montado. Hoy no se registra nada. Cablearlo o borrarlo. |

## Variables en Railway

Cargar los valores **sólo en Railway**. Las esenciales:

```text
APP_ENV=production
BASE_URL=https://taurosolutions.ar
SESSION_COOKIE_SECURE=1
ADMIN_PASSWORD=<frase única de 16+ caracteres>
DATABASE_URL=${{Postgres.DATABASE_URL}}
CORS_ORIGINS=https://taurosolutions.ar,https://www.taurosolutions.ar
```

Opcionales de seguridad:
`ADMIN_TOTP_SECRET` (segundo factor del admin — generar con el script),
`EXTRA_HOSTS` (hosts extra permitidos). `DATABASE_URL` debe usar la red
**privada** de Railway.

## Cloudflare

1. SSL/TLS `Full (strict)`. 2. `Always Use HTTPS`. 3. TLS mínimo `1.2`.
4. Proxy activado. 5. WAF administrado cuando el plan lo permita.
6. Protección de bots. 7. Rate limit extra para `POST /cotizar-web`.

## Operación

- Rotar cualquier clave que haya viajado por chat/mail/captura (Gmail App
  Password, FedEx, UPS, DHL, base). Las API keys B2B se rotan desde el admin.
- Activar backups/snapshots de PostgreSQL **y probar una restauración**.
- Revisar `/admin/seguridad` y los logs de Railway.
- Una sola persona con acceso de escritura a Railway/Cloudflare/GitHub.

## Auditor de dependencias

`pip-audit` informa avisos para Starlette, python-multipart, Pillow, Requests,
urllib3, python-dotenv (al 24/07/2026 varias versiones corregidas no estaban en
el índice de Railway). Mitigaciones vigentes: TAURO no procesa imágenes de
usuarios (Pillow es sólo dependencia de ReportLab); no se usan las funciones
alcanzadas por los avisos; los clientes de couriers desactivan compresión y
redirecciones y limitan los labels a 10 MB. Actualizar apenas haya releases:

```bash
python -m pip_audit -r requirements.txt
```

## Pendientes

- Sacar `style-src 'unsafe-inline'` (refactor de `style=` a clases).
- Rate limiting en Redis/Cloudflare si se corre más de un worker.
- Cablear `security_audit` de verdad o borrar el código muerto.
- Alertas automáticas ante fallos anormales.
- Pruebas periódicas de restauración y escaneo de dependencias en CI.
