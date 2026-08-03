# RELEVO — estado y reglas del proyecto (act. 02/08/2026)

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

## Estado al 02/08/2026

VIVO en producción: portal completo (cuenta corriente con débito automático,
comprobantes con verificación, emisión por cliente con permiso+tope, Excel
por cliente, catálogo con precio por unidad, login por email o ID), admin
completo (bandeja, edición pre-emisión, pisar tracking con flag, carga de
envíos externos, pagos por verificar, margen por ámbito), app Shopify
(venta→solicitud automática), web pública (cotizador FedEx+DHL vivos, guías
por país, calculadora volumétrica, /estado, captura de leads), espejo a
Google Sheet (apagado hasta `GOOGLE_CREDENTIALS_JSON`).

## Pendientes (en orden)

1. **Técnico**: open redirect + validación de `state` en OAuth de Shopify;
   tienda instalada sin vincular descarta ventas (el centinela avisa, no
   arregla); reserva atómica para guías nacionales (envia.com) — doble click
   emite dos; pickups/recolecciones (0% hecho, FedEx Pickup API); emisión
   UPS (cliente `NotImplementedError`).
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
