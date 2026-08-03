# RELEVO — estado y reglas del proyecto (act. 03/08/2026)

Este documento existe para que CUALQUIER agente (Codex, Claude, humano) pueda
retomar el trabajo sin contexto previo. Leelo entero antes de tocar código.
Regla general: **este repo despliega solo a producción en cada push a main**
(Railway, https://taurosolutions.ar) — no hay staging. Compilá, testeá con
mocks y verificá producción después de cada push (patrón abajo).

## Reglas de negocio INVIOLABLES

1. **Dos superficies de cotización, nunca mezclarlas.**
   - Cotizador WEB (`/web`, `POST /cotizar-web`): público, sin login. Precio
     de vidriera (descuento web + margen por carrier del admin). JAMÁS expone
     el pricing de un cliente ni el costo/ganancia de TAURO (hubo una fuga
     así: commit 18d8007).
   - PORTAL (`/portal/*`, logueado): cada cliente ve SU precio con SU regla
     (`clientes.markup_tipo/valor`, y `markup_nac_*` para nacional).
   - El mismo paquete puede valer distinto en ambas superficies: correcto.

2. **Discreción del canal operativo.** Mientras se negocian cuentas directas,
   los envíos reales salen por un proveedor mayorista. El nombre de ese
   proveedor NO se escribe en ningún campo visible al cliente (las
   `observaciones` de solicitudes SE MUESTRAN en el portal). El alta se hace
   por admin → "+ Cargar envío realizado" (`cargar_envio_externo`).

3. **Emitir guía cuesta plata real y NO es idempotente.**
   `create_shipment` va con `max_retries=1` y reserva atómica
   (`UPDATE … WHERE tracking IS NULL RETURNING`, estado `EMITIENDO`).
   Tocar ese flujo = riesgo de guías dobles facturadas.

4. **La cuenta corriente debita sola al emitir** (`cargar_guia_emitida`,
   idempotente por índice único sobre `envios.solicitud_id`). El cargo es
   `precio_tauro_ars` (lo que TAURO cobra al cliente), NUNCA
   `precio_cliente_final_ars` (lo que el cliente cobra a SU comprador).

5. **Pagos informados por el cliente NO tocan el saldo hasta que el admin
   los aprueba** (`pagos.estado`: PENDIENTE→APROBADO/RECHAZADO;
   `total_pagado()` suma sólo APROBADO; NULL legacy = aprobado).

6. **total_price hacia Shopify va en CENTAVOS (×100). NO tocar** —
   verificado tres veces contra la doc y producción. La app de Shopify ya NO
   cotiza en el checkout (decisión de producto): `POST /shopify/tarifas`
   devuelve `{"rates": []}` a propósito. Su único trabajo: recibir la venta
   (webhook) → `servicios/solicitud_automatica.py` arma la solicitud sola.
   La guía NUNCA se emite sola.

7. **Peso facturable = max(peso real, L×A×H/5000) SIEMPRE** (regla del dueño).
   En carritos de tienda las dimensiones salen del catálogo cruzando el SKU
   contra `productos.alias_interno` (comparación en MAYÚSCULAS ambos lados).

8. **⚠️ ANTES de pasar `FEDEX_ENVIRONMENT=production`**: recalibrar
   `WEB_DESC_FEDEX_PCT` (hoy calibrado contra tarifas de sandbox; ver
   comentario largo en `servicios/carriers.py`) — si no, se vende bajo costo.
   La cache de tarifas guarda `entorno` y se invalida sola en el switch.

9. **Contenido público de aduana**: sólo datos verificados con fuente y
   fecha (`servicios/guias_pais.py`). Dato clave vigente: EEUU eliminó el
   de-minimis de USD 800 el 29/08/2025 — todo envío comercial paga aranceles.

10. **Toda protección va EN EL HANDLER, no en middleware**:
    `core/security.py` NO está montado (dead code local, fuera del repo).
    Ejemplos vivos: `leer_comprobante_con_tope` (8 MB), `validar_comprobante`
    (tipo por firma de contenido, no extensión).

## Disciplina de trabajo (pedida por el dueño)

El dueño pidió estructurar el trabajo en los 11 pasos de la Kabalah
(Keter→Maljut). Traducción operativa mínima: (1) decir qué problema de
negocio resuelve antes de codear; (2) leer el código real y medir producción
antes de afirmar; (3) toda feature nueva sale con su restricción gemela
(tope/validación/idempotencia); (4) reconocer errores en voz alta; (5) nada
está "hecho" hasta estar desplegado y verificado. En tareas grandes,
decirle al dueño en qué paso se está.

## Cómo verificar un deploy (patrón usado siempre)

```bash
# tras el push, esperar ~90s y:
for i in 1 2 3 4 5 6 7 8; do sleep 22; curl -s -o /dev/null -w "[%{http_code}] " -m 10 https://taurosolutions.ar/salud; done
.venv-codex/bin/python scripts/test_checkout_critico.py   # 20+ chequeos contra prod
```
Tests locales: usar `.venv-codex/bin/python` (el venv principal está roto).
Templates: validar con Jinja2 antes de push. JSX de la web pública: validar
con `node -e "require('./node_modules/esbuild').transformSync(...,{loader:'jsx'})"`
y bumpear `?v=N` en `web/Tauro Solutions.html` (Babel standalone, sin build).

## Estado al 03/08/2026

VIVO en producción: portal completo (cuenta corriente con débito automático,
comprobantes con verificación, emisión por cliente con permiso+tope,
RECOLECCIONES, Excel por cliente, catálogo con precio por unidad, login por
email o ID), admin completo (bandeja, edición pre-emisión, pisar tracking con
flag, carga de envíos externos, pagos por verificar, margen por ámbito,
recolecciones del día), app Shopify (venta→solicitud automática, ventas
huérfanas rescatadas al vincular), web pública (cotizador FedEx+DHL vivos,
guías por país, calculadora volumétrica, /estado, captura de leads), espejo a
Google Sheet (apagado hasta `GOOGLE_CREDENTIALS_JSON`).

**La spec original de 23 puntos está COMPLETA.** Los tres couriers
internacionales (FedEx, DHL, UPS) cotizan, emiten guías y trackean; el
despachador de emisión es un registro (`generar_guia_internacional`) donde
sumar un courier es una línea, porque todo el armado del envío se comparte.
Suite: 92 tests, `.venv-codex/bin/python -m pytest tests/ -q` — **sin
`--ignore`**. `test_security.py` y `test_email_security.py` salieron del repo
el 03/08: importaban `core/security.py`, que no está trackeado, así que
explotaban al colectar y se venían salteando a mano en cada corrida.

## Seguridad (03/08/2026)

### Endurecimiento grande de la web ("usa todo tu poder")

Segunda tanda del 03/08, sobre la deuda de fondo. Todo montado, deployado y
verificado en producción (el marcador de "versión nueva viva" es la cabecera
CSP; el deploy booteó pese al cambio a contenedor no-root). Detalle en
`docs/SEGURIDAD.md`. Diseñado con un inventario por superficie (6 agentes) y
validado con una auditoría adversarial de 5 lentes + verificadores (14
agentes): **9 hallazgos, 7 confirmados y arreglados en la misma tanda**, 1
refutado, 1 dependiente de misconfig (ya cerrado).

- **Web compilada con esbuild** → `static/js/app.js` (bundle único, React
  adentro). Se acabó Babel+unpkg en el navegador. El bundle SE COMMITEA
  (Railway no tiene node); para editarla: tocar `web/components/*.jsx` +
  `npm run build:web` + bumpear `?v=`.
- **CSP estricta** `script-src 'self' 'nonce-<req>'` en toda la web/portal/
  admin/páginas-Python. Cero handlers inline (migrados a `data-*`), nonce en
  todos los `<script>`. NO toca `/shopify` (iframe) ni `/docs`.
- **MFA TOTP opcional en el admin** (`ADMIN_TOTP_SECRET`, generar con
  `scripts/generar_totp_admin.py`). Anti-replay por paso de tiempo.
- **api_key B2B hasheada** (sha256), migración en el arranque, botón
  "Regenerar API key" en el admin.
- **Guardas**: Host→421 (sin wildcard railway), tope de tamaño por
  Content-Type, CSRF Origin/Sec-Fetch en /portal|/admin, `client_ip` toma
  CF-Connecting-IP / XFF derecho (el rate limit era evadible), contenedor
  no-root.

Hallazgos de LA PROPIA auditoría de esta tanda, ya arreglados: el no-root
tumbaba el arranque (mkdir en /app al importar tracking) — **ALTA**, era un
self-DoS del deploy; TOTP anti-replay incompleto y "quemable"; rate limit
evadible por XFF spoofing; tope de tamaño rompía subidas del admin. Deuda
que queda anotada en SEGURIDAD.md: `unsafe-inline` en style-src, rate limit
en memoria, chunked evade el tope (lo corta el proxy), `auditoria.py` sin
cablear.

### Primera auditoría del 03/08 (previa)

Se midió producción y se corrió una auditoría adversarial de 26 agentes:
**8 hallazgos confirmados, 14 refutados**. Todo lo confirmado está arreglado,
deployado y verificado en vivo. Detalle completo en `docs/SEGURIDAD.md`, que
ahora sólo lista lo que está montado de verdad.

Lo que se encontró y se cerró:

1. **ALTA — robo de tienda ajena.** `/portal/tienda/reclamar` sólo chequeaba
   que el dominio estuviera en la lista de instalaciones sin dueño, y esa
   lista se le mostraba a TODOS los clientes. Un cliente podía apropiarse de
   la tienda Shopify de otro y quedarse con sus ventas y con los datos de los
   compradores finales. Ahora `es_dueno_de_la_tienda()` le pregunta a la propia
   tienda quién es su dueño (`GET shop.json`) y lo compara contra el mail del
   cliente en TAURO; la lista además se filtra por cliente.
2. **Sin cabeceras de seguridad, `/docs` abierto y `CORS: *`** en producción.
   Cerrado: HSTS, nosniff, referrer, permissions, anti-frame (menos en
   `/shopify/*`, que va en iframe), docs en 404, CORS a los dominios de TAURO.
3. `/cotizar-web` sin rate limit: cada request cotiza en vivo contra los
   couriers. 30 por IP cada 5 minutos.
4. `/cotizacion-lead` era una primitiva para mandar mail a cualquiera con
   nuestro remitente. Un mail por dirección por día.
5. El backup del admin exportaba `clientes.api_key` en claro. Fuera.
6. El webhook GDPR de Shopify logueaba el mail y teléfono del comprador.
7. `pedidos_huerfanos` no vencía nunca (PII de compradores guardada para
   siempre). Se borran a los 90 días.
8. `/portal` y `/admin` sin `Cache-Control`: el HTML quedaba en la caché del
   navegador después de cerrar sesión. `no-store, private`.

**Deuda de seguridad conocida, en `docs/SEGURIDAD.md`:** no hay CSP (la
bloquea Babel en runtime — se destraba compilando React en el build), la
`api_key` se guarda en claro, el contenedor corre como root, el rate limit es
en memoria (con más de un worker el tope se multiplica) y el admin no tiene MFA.

## Pendientes (en orden)

1. **Técnico**: NADA bloqueante. Lo que queda depende de credenciales:
   - **Probar contra las APIs vivas** cuando entren las cuentas. La emisión
     de DHL y UPS está armada contra la documentación y cubierta por tests
     de payload, pero NUNCA se ejecutó contra el sandbox real. La primera
     guía de cada courier hay que mirarla de cerca (mismo criterio para la
     primera recolección: la Pickup API tampoco se probó viva).
   - Recolecciones para couriers nacionales (hoy sólo FedEx; falta
     investigar si envia.com expone pickup).
   *(Cerrados el 02/08: open redirect, `state` del OAuth, ventas de tiendas
   sin vincular, reserva atómica en guías nacionales, recolecciones FedEx.
   El 03/08: los 8 hallazgos de la auditoría — ver la sección de arriba.)*
2. **Del dueño**: cargar `WHATSAPP_TAURO` en /admin/config cuando llegue la
   eSIM (el botón de ayuda del portal aparece solo); SKUs+medidas en
   catálogos de clientes; formularios comerciales Andreani/Correo/OCA (dossier
   en `~/Documents/colab tauro/CARRIERS_NACIONALES_contactos.md`);
   credenciales Tiendanube; backups de Postgres en Railway + UptimeRobot.
3. **Vetado por el dueño (no hacer)**: caso de éxito con cita de Pesca Jacks;
   bloque Data Fiscal/AFIP (por ahora); logos de FedEx o palabra "partner"
   en la web.

## Gotchas técnicos (los que muerden)

- `dotenv.py` en la raíz es un shim local gitignoreado — NO subirlo.
- `pedido_tienda_id` debe viajar en el form dict del wizard o un error de
  validación rompe el vínculo venta↔envío (ya pasó; está arreglado — no
  regresionar).
- El dólar sale de `cotizador.dolar_ars()` (tabla config) en TODOS los
  caminos; `os.getenv` sólo como último recurso documentado.
- Los templates del portal usan globals de Jinja: `url_tracking`,
  `es_nacional`, `ayuda`, `pendientes_menu`, `saldo_menu` — si se instancia
  otro `Jinja2Templates`, hay que re-registrarlos.
- `solicitudes_guia.observaciones` la VE el cliente. Nada interno ahí.
- Cualquier protección que se agregue tiene que quedar montada en `main.py`.
  `core/security.py` era un paquete entero de defensas que NUNCA se montó, y
  `docs/SEGURIDAD.md` las daba por vigentes. Antes de escribir una línea en ese
  documento, medirla contra producción con `curl -I`.
