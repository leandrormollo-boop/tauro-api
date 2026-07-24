# Tauro Solutions - Contexto rapido

Ultima actualizacion: 2026-06-03

## Resumen del proyecto

Tauro Solutions es una plataforma B2B de logistica internacional para clientes/ecommerce. La idea es ofrecer algo similar a Envia/Boxfly: clientes con login, cotizacion, carga de productos, creacion/gestion de guias y un panel admin de Tauro para operar todo.

Importante: diferenciar bien tres superficies:

- Web publica: cotizador simple sin login para visitantes.
- Portal cliente: dashboard autenticado donde el cliente cotiza, carga productos y crea solicitudes/guias.
- Admin Tauro: panel interno para crear clientes, configurar pricing/markup, validar productos y gestionar operaciones.

## Infraestructura

- Repo GitHub: `leandrormollo-boop/tauro-api`
- Repo local: `/Users/leanrmollo/Desktop/TAURO 2026/API TAURO`
- Produccion Railway: `https://tauro-api-production.up.railway.app`
- Web publica: `https://tauro-api-production.up.railway.app/web`
- Portal cliente: `https://tauro-api-production.up.railway.app/portal/login`
- Admin: `https://tauro-api-production.up.railway.app/admin/login`
- Stack: FastAPI + PostgreSQL Railway + FedEx API + portal/admin HTML + web React estatica.
- Deploy: Railway auto-deploya cuando se pushea a `main`.
- DB: PostgreSQL en Railway. Google Sheets ya fue migrado y dejo de ser la fuente principal.

## Estado actual

- PostgreSQL ya esta agregado en Railway.
- Migracion desde Google Sheets a PostgreSQL completada.
- Panel admin funcionando con CRUD de clientes, facturas, pagos, rutas, productos, migracion y solicitudes de guia.
- Portal cliente usa PostgreSQL.
- API B2B `/cotizar` y `/pedido` usan PostgreSQL para API keys, productos, pricing y cotizaciones.
- Web publica `/cotizar-web` cotiza con FedEx y markup web.
- El endpoint `/pedido` ahora tambien crea una solicitud interna de guia y devuelve `solicitud_id`.
- Railway deploy verificado despues de los ultimos cambios.
- `git status` actual: repo local sincronizado con `origin/main`; queda un archivo doc local sin commit: `docs/CONTEXTO_RAPIDO_TAURO.md`.

## Cambios recientes importantes

- `4655c5e feat: agregar solicitudes de guia`
  - Agrega tabla `solicitudes_guia`.
  - Agrega portal `/portal/envios` y `/portal/envios/nuevo`.
  - Agrega admin `/admin/pedidos`.
  - Permite que Tauro vea solicitudes, cambie estado, cargue tracking y URL/PDF de guia.

- `a1c6580 feat: soportar pricing por cliente`
  - Agrega pricing por cliente con tres modos:
    - `PCT`: porcentaje, ejemplo `25%`.
    - `FIJO_ARS`: fijo por envio, ejemplo MENDEZ `+ ARS 9.100`.
    - `MULTIPLICADOR`: costo x factor, ejemplo TOPOLLAN `1.30`.
  - Admin permite cargar/ver la regla comercial de cada cliente.
  - Cotizador B2B/portal usa la regla del cliente.
  - API devuelve `markup_tipo`, `markup_valor` y `markup_pct_equivalente`.

Commits previos relevantes:

- `0502c45 feat: migracion completa a PostgreSQL + panel admin`
- `4662f06 feat: ejecutar migracion postgres desde admin`
- `4ab05f7 feat: usar postgres en api b2b`

## Modelo funcional deseado

El cotizador publico es una cosa distinta al dashboard del cliente.

En el portal cliente, el cliente debe poder:

- Loguearse.
- Tener remitente predeterminado guardado en base de datos.
- Cargar/guardar destinatarios.
- Cargar productos preguardados.
- Crear envios/guias paso a paso.
- Completar contenido del paquete para commercial invoice.
- Cotizar dentro del flujo autenticado.
- Confirmar solicitud o creacion de guia.
- Ver historial, estado, tracking y guia/PDF.

En el admin Tauro, Tauro debe poder:

- Crear usuarios/clientes.
- Guardar datos fiscales/comerciales del cliente.
- Configurar pricing por cliente.
- Validar productos cargados por clientes.
- Ver solicitudes de guia.
- Cargar tracking, URL/PDF de guia y estados.
- Gestionar facturas/pagos/cuenta corriente.

## Flujo de guia a construir

Objetivo: convertir "Crear solicitud" en un wizard real:

1. Remitente: usar remitente predeterminado del cliente, editable si corresponde.
2. Destinatario: nombre, documento/tax id, email, telefono, pais, ciudad, estado, direccion, codigo postal, observaciones.
3. Paquete: tipo, peso, medidas, cantidad de paquetes.
4. Contenido/invoice: productos, HS code, descripcion en ingles, cantidad, valor unitario, valor declarado.
5. Carrier/rate: FedEx primero; luego DHL/UPS cuando existan credenciales.
6. Pricing Tauro: aplicar regla comercial del cliente.
7. Confirmacion: resumen completo.
8. Operacion: crear solicitud o guia real segun integracion disponible.
9. Seguimiento: tracking, estado, PDF/label.

## Pendientes grandes

- Dominio `taurosolutions.ar`: sigue apuntando a la web vieja/Hostinger. Railway ya tiene dominio configurado, pero falta DNS correcto.
- Hacer wizard real de creacion de guia segun requerimientos FedEx/DHL/UPS.
- Guardar remitentes predeterminados por cliente.
- Guardar libreta de destinatarios.
- Soportar varios paquetes/items por envio.
- Integrar creacion real de labels con FedEx production.
- Agregar DHL/UPS cuando lleguen credenciales.
- Mejorar portal cliente como dashboard completo, no solo formularios simples.
- Eventualmente agregar pagos/cobro por cliente o reseller.
- Pensar arquitectura para que clientes de Tauro puedan revender el servicio a sus propios clientes.
- Integracion Shopify/TiendaNube como fase posterior.

## Dominio

El dominio `taurosolutions.ar` todavia muestra la web vieja porque sigue apuntando a Hostinger:

- IP vieja detectada previamente: `147.93.64.57`
- Nameservers detectados previamente: `ns1.dns-parking.com`, `ns2.dns-parking.com`
- Railway tiene el dominio configurado en el servicio `tauro-api`, puerto `8000`.
- Railway estaba esperando DNS update.

Registros que Railway habia pedido:

```text
CNAME  @  qedh0jvo.up.railway.app
TXT    _railway...  railway-verify=...
```

Pendiente: entrar a Hostinger/hPanel o al DNS vigente, borrar el `A @ 147.93.64.57`, agregar el `CNAME @ qedh0jvo.up.railway.app` y el TXT completo que muestra Railway.

## Seguridad

- No pegar `.env`, passwords, API keys, URLs de Postgres ni app passwords en chats.
- El usuario ya compartio credenciales sensibles en una conversacion; recomendacion: rotarlas antes de usar con clientes reales.
- No repetir secretos en respuestas.
- Mantener `.env` fuera de GitHub.
