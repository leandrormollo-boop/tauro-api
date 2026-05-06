============================================================
  02_endpoints — Rutas FastAPI
============================================================

QUÉ HAY ACÁ
-----------
Cada archivo agrupa endpoints por audiencia:

  api_publica.py      - /cotizar, /pedido (B2B con X-API-Key)
  portal_cliente.py   - /portal/* (cliente final con login)
  admin.py            - /admin/* (solo Tauro)

LÓGICA Y DECISIONES
-------------------

[api_publica.py]
Audiencia: tiendas e-commerce que integran Tauro.
Auth: header X-API-Key (validado contra hoja PERFILES).

  POST /cotizar
    Input: ruta_id (o origen/destino libres), peso, dimensiones
    Output: { precio_usd, precio_ars, dias_estimados, valido_hasta }
    Aplica MARKUP_% del cliente automáticamente.
    Loguea a hoja COTI.

  POST /pedido
    Input: cotización + datos remitente/destinatario + items
    Output: { pedido_id, estado, link_seguimiento }
    Phase 2: generará guía FedEx real (create_shipment).
    Por ahora: solo guarda el pedido y avisa a Tauro por email.

[portal_cliente.py]
Audiencia: el dueño del e-commerce / cliente final.
Auth: link mágico por email (token en hoja SESSIONS, 7 días).

  GET  /portal/login         Form de email
  POST /portal/login/send    Manda link mágico
  GET  /portal/auth?token=X  Valida token y crea sesión
  GET  /portal/home          Saldo + últimos envíos
  GET  /portal/envios        Tabla completa de envíos
  GET  /portal/cuenta        Cuenta corriente detallada
  GET  /portal/factura/{id}  Descarga PDF
  GET  /portal/cotizar       Form simple (dropdowns de ruta)
  POST /portal/cotizar       Ejecuta cotización
  GET  /portal/catalogo      Productos del cliente
  POST /portal/catalogo/add  Agregar nuevo producto
  GET  /portal/guia          (Phase 2) Generar guía con catálogo

[admin.py]
Audiencia: vos (Tauro).
Auth: password simple en .env (ADMIN_PASSWORD).

  GET  /admin/health         Estado FedEx/Sheets/SMTP/Cron
  GET  /admin/cotizaciones   Últimas 100 cotizaciones (todos clientes)
  GET  /admin/pedidos        Pedidos pendientes
  GET  /admin/errores        Log de errores con botón replay
  POST /admin/replay/{id}    Re-ejecutar request fallida
  GET  /admin/clientes       Lista de PERFILES (editable)

REGLAS
------
- Cada endpoint loguea a LOG_REQUESTS (timestamp, cliente, input, status).
- Errores se loguean a LOG_ERRORES con traceback completo.
- Validación con Pydantic (ver 03_modelos/).
- Lógica de negocio en 04_servicios/ — el endpoint solo orquesta.
- NUNCA tocar Sheets directamente desde el endpoint — usar servicios.
