# Seguridad de TAURO API

Última verificación contra producción: **03/08/2026**.

Este documento sólo lista lo que está **montado y medido**. La versión anterior
describía un paquete de protecciones (`core/security.py`) que nunca llegó a
agregarse a la app: el archivo existía, pero `main.py` jamás lo montaba, así
que el documento prometía defensas que no corrían. Si algo no está acá, no
está en la app. Antes de agregar una línea, verificarla contra producción.

## Lo que está montado

### Transporte y cabeceras (`main.py`, middleware `headers_de_seguridad`)

Verificado con `curl -I https://taurosolutions.ar/web`:

- `Strict-Transport-Security: max-age=31536000; includeSubDomains`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()`
- `X-Frame-Options: SAMEORIGIN` — **salvo en `/shopify/*`**, que se abre dentro
  del admin de Shopify y manda su propio `frame-ancestors`. Ponerlo ahí deja al
  comerciante mirando un iframe en blanco.
- `Cache-Control: no-store` + `Pragma: no-cache` en `/portal/*` y `/admin/*`:
  el HTML con saldo, envíos y direcciones de compradores no queda en la caché
  del navegador después de cerrar sesión.

### CORS

`allow_origins` sale de `CORS_ORIGINS`; por defecto `taurosolutions.ar` y
`www.taurosolutions.ar`. `allow_credentials=False`, métodos `GET`/`POST`.
(Estuvo en `*` hasta el 03/08/2026.)

### Documentación de la API

`/docs`, `/redoc` y `/openapi.json` devuelven **404 en producción** (medido).
Se habilitan sólo con `ENV=DEV`. La variable es `ENV` — no existe
`ENABLE_API_DOCS`, aunque el doc viejo la nombraba.

### Autenticación

- Passwords con **bcrypt** (`servicios/auth.py`).
- Login del portal por email **o por ID de cliente**.
- Cookies de sesión `HttpOnly`, `SameSite=Lax`, `Secure` cuando
  `COOKIE_SECURE=1`.
- Magic links de un solo uso con vencimiento.

### Rate limits (`servicios/rate_limit.py`, en memoria)

| Endpoint | Tope | Ventana |
|---|---|---|
| `POST /portal/login` | 8 | 5 min |
| `POST /admin/login` | 5 | 5 min |
| magic link | 5 | 15 min |
| recuperar password admin | 3 | 1 h |
| `POST /cotizar-web` | 30 | 5 min |
| `POST /cotizacion-lead` | 5 | 15 min |

Además, `/cotizacion-lead` manda **un mail por dirección por día**: sin eso es
una primitiva para enviarle correo a cualquiera con nuestro remitente.

> El contador vive en memoria del proceso. Con más de un worker cada uno lleva
> el suyo y el tope efectivo se multiplica. Mover a Redis o a Cloudflare antes
> de escalar horizontalmente.

### Integración con tiendas

- Webhooks de Shopify validados por **HMAC**; sin firma o con firma inválida →
  401 (lo verifica `scripts/test_checkout_critico.py` en cada deploy).
- OAuth con parámetro `state` verificado y dominio validado contra
  `^[a-z0-9][a-z0-9\-]*\.myshopify\.com$`.
- Al vincular una tienda huérfana se le pregunta **a la propia tienda** quién es
  su dueño (`GET shop.json`) y se compara contra el mail del cliente en TAURO.
  Sin coincidencia no se vincula. Antes alcanzaba con que el dominio estuviera
  en la lista de huérfanas — y esa lista se le mostraba a todos los clientes.
- Los pedidos huérfanos se borran a los **90 días**: son órdenes completas con
  nombre, dirección y teléfono del comprador final.
- El webhook GDPR `customers/data_request` loguea dominio y tamaño, nunca el
  cuerpo.

### Datos

- El backup del admin excluye `password_hash` y `api_key`.
- Los textos que cargan los clientes se escapan antes de armar PDFs o mails.
- `security_audit` registra logins y mutaciones sensibles
  (`servicios/auditoria.py`).

## Lo que NO está (y por qué)

| Falta | Motivo / riesgo |
|---|---|
| **Content-Security-Policy** | La web pública compila JSX en el navegador con Babel standalone desde unpkg. Una CSP estricta la rompe entera. El `<script>` de Babel sí tiene `integrity` (SRI), así que unpkg no puede servir otro archivo. Se pone CSP cuando se compile el bundle en el build. |
| **React compilado en el build** | Hoy es Babel en runtime, con `?v=N` para romper caché. Es la deuda técnica que bloquea la CSP. |
| **`TrustedHostMiddleware` / validación de `Host`** | No está montado. Mitigado en parte porque Railway y Cloudflare resuelven por dominio. |
| **Límite global de tamaño de request** | No hay tope global. Sí hay uno de 8 MB en la subida de comprobantes, que es la única superficie que recibe archivos. |
| **Chequeo de `Origin`/`Referer`/`Sec-Fetch-Site` en formularios** | No está. La defensa de CSRF hoy es `SameSite=Lax` en la cookie. |
| **`api_key` hasheada** | Se guarda en claro: la API B2B compara el valor directo. Rotarla es barato, pero un dump de base entrega las claves vivas. |
| **Contenedor no-root** | El `Dockerfile` no tiene `USER`; corre como root. |
| **MFA en el admin** | Un solo password compartido. |

## Variables en Railway

Cargar los valores **sólo en Railway**, nunca en el repo. Usar `.env.example`
como lista. Las esenciales:

```text
APP_ENV=production
BASE_URL=https://taurosolutions.ar
SESSION_COOKIE_SECURE=1
ADMIN_PASSWORD=<frase única de 16+ caracteres>
DATABASE_URL=${{Postgres.DATABASE_URL}}
CORS_ORIGINS=https://taurosolutions.ar,https://www.taurosolutions.ar
```

`DATABASE_URL` debe usar la red **privada** de Railway. La URL pública de
Postgres se reserva para una migración manual desde una computadora y no debe
quedar configurada en el servicio web.

## Cloudflare

1. SSL/TLS en `Full (strict)`.
2. `Always Use HTTPS` activado.
3. TLS mínimo `1.2`.
4. Registros web con proxy activado.
5. Reglas administradas de WAF cuando el plan las permita.
6. Protección de bots activada.
7. Rate limit adicional para `POST /cotizar-web` (frena el tráfico antes de que
   consuma llamadas pagas a los couriers).

## Operación

- Rotar cualquier clave que haya viajado por chat, mail o captura: Gmail App
  Password, FedEx, UPS, DHL y credenciales de base.
- Activar backups/snapshots de PostgreSQL **y probar una restauración**.
- Revisar `/admin/seguridad` y los logs de Railway.
- Una sola persona con acceso de escritura a Railway, Cloudflare y GitHub
  mientras no haya roles ni MFA en el admin.

## Auditor de dependencias

`pip-audit` informa avisos para Starlette, python-multipart, Pillow, Requests,
urllib3 y python-dotenv. Al 24/07/2026 varias de las versiones corregidas no
estaban publicadas en el índice que usa Railway.

Mitigaciones que sí están vigentes:

- TAURO no procesa imágenes de usuarios; Pillow es sólo dependencia de ReportLab
  para documentos que genera la app.
- No se usan `extract_zipped_paths`, `ProxyManager` ni las funciones de escritura
  de `.env` que alcanzan los avisos.
- Los clientes de couriers desactivan compresión HTTP y redirecciones de API, y
  limitan los labels descargados a 10 MB.

Ninguno se marcó como ignorado. Actualizar apenas haya versiones corregidas y
volver a correr:

```bash
python -m pip_audit -r requirements.txt
```

## Pendientes

- Compilar React en el build → habilita la CSP.
- Hashear `api_key`.
- Rate limiting en Redis o Cloudflare si se corre más de un worker.
- MFA y usuarios administrativos individuales.
- `USER` no-root en el Dockerfile.
- Alertas automáticas ante una cantidad anormal de fallos.
- Pruebas periódicas de restauración y escaneo de dependencias en CI.
