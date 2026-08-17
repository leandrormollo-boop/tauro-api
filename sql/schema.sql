-- ============================================================
-- TAURO SOLUTIONS — Schema PostgreSQL
-- ============================================================
-- Reemplaza completamente las hojas de Google Sheets:
--   PERFILES         → clientes
--   SESSIONS         → sessions
--   RUTAS_DEFAULT    → rutas
--   PRODUCTOS_CATALOGO → productos
--   PAGOS            → pagos
--   ENVIOS 2026      → envios
--   CONFIG           → config
--   COTI             → cotizaciones (log)
--   SOLICITUDES_GUIA → pedidos de guía desde portal/API
--   DIRECCIONES      → remitentes/destinatarios guardados
-- ============================================================

-- ── Clientes (ex PERFILES) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS clientes (
    cliente_id   TEXT PRIMARY KEY,        -- UPPERCASE, ej "MENDEZ"
    email        TEXT UNIQUE NOT NULL,
    api_key      TEXT,
    markup_pct   REAL    NOT NULL DEFAULT 25.0,
    markup_tipo  TEXT    NOT NULL DEFAULT 'PCT', -- PCT | FIJO_ARS | MULTIPLICADOR
    markup_valor REAL,
    activo       BOOLEAN NOT NULL DEFAULT TRUE,
    nombre       TEXT,
    cuit         TEXT,
    direccion    TEXT,
    cp           TEXT,
    ciudad       TEXT,
    pais         TEXT    NOT NULL DEFAULT 'AR',
    telefono     TEXT,
    notas        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS markup_tipo TEXT NOT NULL DEFAULT 'PCT';
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS markup_valor REAL;
-- Margen por ÁMBITO (decisión de Leandro 28/07): sumar el margen
-- internacional (ej. +$14.500) a un envío nacional de $8.000 casi triplica
-- el precio. Si estas columnas están vacías, el envío nacional usa la regla
-- internacional de siempre — nada se rompe para los clientes ya cargados.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS markup_nac_tipo TEXT;
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS markup_nac_valor REAL;
-- Emisión por el cliente (decisión de Leandro 28/07): apagada por defecto,
-- se habilita POR CLIENTE. Emitir cuesta plata real e irreversible, y con
-- tope_deuda_ars el cliente moroso no puede seguir generando costo: si su
-- saldo pendiente supera el tope, emite TAURO o se pone al día. NULL = sin
-- tope (sólo el flag manda).
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS puede_emitir BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS tope_deuda_ars REAL;
-- Recolecciones también generan una operación real en la cuenta del courier.
-- Se habilitan por cliente y permanecen apagadas por defecto, separadas del
-- permiso de emisión de guías.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS puede_recolectar BOOLEAN NOT NULL DEFAULT FALSE;
-- Quién paga los impuestos de destino (decisión de Leandro 01/08/2026).
-- El CLIENTE elige, y lo deja predefinido en su cuenta: Prete Rosso los
-- paga siempre, así que no lo tilda envío por envío. Se puede pisar por
-- envío desde el wizard.
--   DESTINATARIO = los abona quien recibe (incoterm DAP)
--   CLIENTE      = los prepaga TAURO y se los factura al cliente (DDP)
-- Default DESTINATARIO: es la opción que NO expone plata de TAURO, y hasta
-- hoy el código mandaba SENDER con la cuenta de TAURO sin que nadie lo
-- hubiera decidido — o sea que pagábamos los derechos de todos los envíos.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS tax_paga TEXT NOT NULL DEFAULT 'DESTINATARIO';
-- Courier preferido del cliente (Leandro 05/08): "el cliente tiene que
-- especificar por qué empresa realiza sus envíos. Puede dejarlo
-- configurado". WAIMAO opera por DHL: lo deja fijado y el wizard arranca
-- con DHL preseleccionado. Vacío = elige en cada envío.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS courier_default TEXT NOT NULL DEFAULT '';
-- Password hasheado con bcrypt (login email + password)
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- Autogestión y pricing por cliente + courier. Sin fila no se habilita
-- ninguna operación: cotizar, emitir y pedir pickups son opt-in explícito.
-- Hoy sólo DHL está habilitable; FedEx/UPS siguen pendientes.
-- Una fila existente permite, por ejemplo, MELCIOR DHL + ARS 14.000 sin tocar
-- lo que ve o paga ese mismo cliente en FedEx.
CREATE TABLE IF NOT EXISTS cliente_courier_config (
    cliente_id        TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    courier           TEXT NOT NULL,
    puede_cotizar     BOOLEAN NOT NULL DEFAULT FALSE,
    puede_emitir      BOOLEAN NOT NULL DEFAULT FALSE,
    puede_recolectar  BOOLEAN NOT NULL DEFAULT FALSE,
    markup_tipo       TEXT,
    markup_valor      NUMERIC(14,4),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cliente_id, courier),
    CHECK (courier IN ('fedex', 'dhl', 'ups')),
    CHECK (NOT puede_emitir OR puede_cotizar),
    CHECK (courier IN ('fedex', 'dhl') OR NOT puede_recolectar),
    CONSTRAINT ck_cliente_courier_markup CHECK (
        (markup_tipo IS NULL AND markup_valor IS NULL)
        OR (
            markup_tipo IS NOT NULL
            AND markup_tipo IN ('PCT', 'FIJO_ARS', 'MULTIPLICADOR')
            AND markup_valor IS NOT NULL
            AND markup_valor::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND (
                (markup_tipo = 'MULTIPLICADOR' AND markup_valor >= 1)
                OR (markup_tipo IN ('PCT', 'FIJO_ARS') AND markup_valor >= 0)
            )
        )
    )
);
ALTER TABLE IF EXISTS cliente_courier_config
    ALTER COLUMN puede_cotizar SET DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_cliente_courier_config_cliente
    ON cliente_courier_config(cliente_id);

-- ── Libreta de direcciones del portal ──────────────────────
CREATE TABLE IF NOT EXISTS direcciones (
    id             SERIAL PRIMARY KEY,
    cliente_id     TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    tipo           TEXT NOT NULL DEFAULT 'DESTINATARIO', -- REMITENTE | DESTINATARIO
    alias          TEXT,
    nombre         TEXT NOT NULL,
    documento      TEXT,
    email          TEXT,
    telefono       TEXT,
    direccion      TEXT NOT NULL,
    ciudad         TEXT NOT NULL,
    estado         TEXT,
    cp             TEXT NOT NULL,
    pais           TEXT NOT NULL DEFAULT 'AR',
    predeterminada BOOLEAN NOT NULL DEFAULT FALSE,
    notas          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_direcciones_cliente_tipo
    ON direcciones(cliente_id, tipo, predeterminada DESC, updated_at DESC);

-- ── Sesiones (ex SESSIONS) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    cliente_id   TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    creado_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expira_at    TIMESTAMPTZ NOT NULL,
    usado        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_sessions_cliente ON sessions(cliente_id);

-- ── Rutas predefinidas (ex RUTAS_DEFAULT) ──────────────────
CREATE TABLE IF NOT EXISTS rutas (
    ruta_id        TEXT PRIMARY KEY,      -- ej "AR-US"
    origen_pais    TEXT NOT NULL,
    origen_ciudad  TEXT NOT NULL,
    origen_zip     TEXT NOT NULL,
    destino_pais   TEXT NOT NULL,
    destino_ciudad TEXT NOT NULL,
    destino_zip    TEXT NOT NULL,
    dias_estimados INTEGER NOT NULL DEFAULT 5,
    activa         BOOLEAN NOT NULL DEFAULT TRUE
);

-- ── Catálogo de productos (ex PRODUCTOS_CATALOGO) ───────────
CREATE TABLE IF NOT EXISTS productos (
    id               SERIAL PRIMARY KEY,
    cliente_id       TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    alias_interno    TEXT NOT NULL,
    nombre_invoice   TEXT NOT NULL,
    hs_code          TEXT NOT NULL,
    largo_cm         REAL NOT NULL,
    ancho_cm         REAL NOT NULL,
    alto_cm          REAL NOT NULL,
    peso_kg          REAL NOT NULL,
    valor_usd_default REAL NOT NULL DEFAULT 0,
    activo           BOOLEAN NOT NULL DEFAULT FALSE,  -- pendiente validación Tauro
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(cliente_id, alias_interno)
);
CREATE INDEX IF NOT EXISTS idx_productos_cliente ON productos(cliente_id);

-- ── Pagos recibidos (ex PAGOS) ──────────────────────────────
CREATE TABLE IF NOT EXISTS pagos (
    id           SERIAL PRIMARY KEY,
    cliente_id   TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    fecha        DATE NOT NULL,
    monto_ars    REAL NOT NULL,
    metodo       TEXT NOT NULL DEFAULT 'transferencia',
    referencia   TEXT,
    nota         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pagos_cliente ON pagos(cliente_id);
-- Pagos informados por el CLIENTE con comprobante (decisión de Leandro
-- 28/07): entran como PENDIENTE y NO tocan el saldo hasta que el admin los
-- aprueba. Los que carga el admin nacen APROBADO (las filas viejas, con
-- estado NULL, cuentan como aprobadas — eran cargas del admin).
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS estado TEXT DEFAULT 'APROBADO';
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante BYTEA;
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante_tipo TEXT;
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante_nombre TEXT;
-- Historial de salud para la página de estado pública (/estado). El
-- centinela (cada 15 min) suma acá: checks del día y cuántos fallaron.
CREATE TABLE IF NOT EXISTS salud_historial (
    dia     DATE PRIMARY KEY,
    checks  INTEGER NOT NULL DEFAULT 0,
    fallos  INTEGER NOT NULL DEFAULT 0
);

-- ── Leads del cotizador publico ────────────────────────────
-- Antes se creaba dentro del primer request. Queda en el schema para que el
-- CRM pueda leerla desde el arranque y para que la migracion sea auditable.
CREATE TABLE IF NOT EXISTS leads_cotizacion (
    id         SERIAL PRIMARY KEY,
    email      TEXT NOT NULL,
    origen     TEXT,
    destino    TEXT,
    peso_kg    REAL,
    resumen    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS origen TEXT;
CREATE INDEX IF NOT EXISTS idx_leads_cotizacion_email_fecha
    ON leads_cotizacion (LOWER(email), created_at DESC);

-- ── Envíos / Facturas (ex ENVIOS 2026) ─────────────────────
CREATE TABLE IF NOT EXISTS envios (
    id           SERIAL PRIMARY KEY,
    cliente_id   TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    fecha        DATE NOT NULL,
    nro_fc       TEXT,
    monto_ars    REAL NOT NULL DEFAULT 0,
    estado       TEXT NOT NULL DEFAULT 'ACTIVO',  -- ACTIVO | CANCELADO | NC
    descripcion  TEXT,
    tracking     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Factura emitida con su PDF adjunto (cargada por el admin). Estos ALTER van
-- después del CREATE para que una base nueva también reciba las columnas.
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_pdf BYTEA;
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_nombre TEXT;
CREATE INDEX IF NOT EXISTS idx_envios_cliente ON envios(cliente_id);
-- Cargo automático: cuando se emite una guía, el débito entra solo a la
-- cuenta corriente (decisión de Leandro 28/07 — antes era doble carga manual
-- y el saldo mentía si el admin se olvidaba). solicitud_id ata el cargo a su
-- guía, y el índice único garantiza que UNA guía debite UNA sola vez aunque
-- el proceso se reinicie o la función se llame dos veces.
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS solicitud_id INTEGER;
-- Ámbito contable del cargo. Nullable sólo para historia aún no conciliada;
-- todo cargo automático nuevo lo copia de la solicitud que lo originó.
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS ambito TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_envios_solicitud
    ON envios(solicitud_id) WHERE solicitud_id IS NOT NULL;
-- Índice compuesto para queries de facturación (cliente + filtro de estado)
CREATE INDEX IF NOT EXISTS idx_envios_cliente_estado ON envios(cliente_id, estado);
CREATE INDEX IF NOT EXISTS idx_envios_cliente_ambito_fecha
    ON envios(cliente_id, ambito, fecha DESC);
-- Índice por fecha para listados ordenados
CREATE INDEX IF NOT EXISTS idx_envios_fecha_desc ON envios(fecha DESC);
CREATE INDEX IF NOT EXISTS idx_pagos_fecha_desc ON pagos(fecha DESC);

-- ── Log de cotizaciones (ex COTI) ───────────────────────────
CREATE TABLE IF NOT EXISTS cotizaciones (
    id               SERIAL PRIMARY KEY,
    coti_id          TEXT,
    cliente_id       TEXT NOT NULL,
    ruta_id          TEXT NOT NULL,
    peso_kg          REAL NOT NULL,
    dimensiones      TEXT,
    peso_usado_kg    REAL NOT NULL,
    costo_fedex_usd  REAL,
    markup_pct       REAL,
    markup_tipo      TEXT,
    markup_valor     REAL,
    precio_final_usd REAL,
    precio_final_ars REAL,
    dias_estimados   INTEGER,
    valida_hasta     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS coti_id TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS markup_tipo TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS markup_valor REAL;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS ambito TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS origen_iso TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS destino_iso TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS courier TEXT;
ALTER TABLE IF EXISTS cotizaciones ADD COLUMN IF NOT EXISTS servicio_courier TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_cotizaciones_coti_id
    ON cotizaciones(coti_id)
    WHERE coti_id IS NOT NULL;

-- ── Solicitudes de guía desde portal ────────────────────────
CREATE TABLE IF NOT EXISTS solicitudes_guia (
    id                       SERIAL PRIMARY KEY,
    cliente_id               TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    estado                   TEXT NOT NULL DEFAULT 'SOLICITADO',
    producto_alias           TEXT NOT NULL,
    cantidad                 INTEGER NOT NULL DEFAULT 1,
    remitente_alias          TEXT,
    remitente_nombre         TEXT,
    remitente_documento      TEXT,
    remitente_email          TEXT,
    remitente_telefono       TEXT,
    remitente_direccion      TEXT,
    remitente_ciudad         TEXT,
    remitente_estado         TEXT,
    remitente_zip            TEXT,
    remitente_pais           TEXT,
    ambito                   TEXT,
    destino_pais             TEXT NOT NULL,
    dest_nombre              TEXT NOT NULL,
    dest_documento           TEXT,
    dest_email               TEXT,
    dest_telefono            TEXT,
    dest_direccion           TEXT NOT NULL,
    dest_ciudad              TEXT NOT NULL,
    dest_estado              TEXT,
    dest_zip                 TEXT NOT NULL,
    observaciones            TEXT,
    peso_kg                  REAL,
    largo_cm                 REAL,
    ancho_cm                 REAL,
    alto_cm                  REAL,
    valor_declarado_usd      REAL,
    ruta_id                  TEXT,
    coti_id                  TEXT,
    precio_tauro_ars         REAL,
    precio_tauro_usd         REAL,
    precio_cliente_final_ars REAL,
    tracking                 TEXT,
    guia_url                 TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_solicitudes_guia_cliente
    ON solicitudes_guia(cliente_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_solicitudes_guia_estado
    ON solicitudes_guia(estado, created_at DESC);
-- Instalaciones anteriores a la columna de auditoría deben migrar antes de
-- que emisión/conciliación la use. El CREATE TABLE no modifica una tabla ya
-- existente, por eso este ALTER idempotente es obligatorio en producción.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS updated_at
    TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Guía emitida (FedEx Ship API): número, label PDF y courier.
-- El label se guarda como BYTEA en Postgres (el filesystem de Railway es efímero).
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS courier TEXT NOT NULL DEFAULT 'FEDEX';
-- Una guía real no puede quedar asociada a dos solicitudes del mismo courier.
-- Si una base histórica trae duplicados, esta migración debe frenar para que
-- se concilien antes del piloto; continuar sin la garantía permitiría cobrar
-- dos veces el mismo tracking.
CREATE UNIQUE INDEX IF NOT EXISTS uq_solicitudes_guia_courier_tracking
    ON solicitudes_guia (UPPER(courier), UPPER(BTRIM(tracking)))
    WHERE tracking IS NOT NULL AND BTRIM(tracking) <> '';
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS label_pdf BYTEA;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS guia_generada_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_alias TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_nombre TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_documento TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_email TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_telefono TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_direccion TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_ciudad TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_estado TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_zip TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_pais TEXT;
-- La ruta manda: AR→AR=NACIONAL; cualquier ruta entre países=INTERNACIONAL.
-- No tiene DEFAULT para que una fila vieja sin evidencia no se disfrace.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS ambito TEXT;
CREATE INDEX IF NOT EXISTS idx_solicitudes_cliente_ambito_fecha
    ON solicitudes_guia(cliente_id, ambito, created_at DESC);

-- Código de servicio nativo del courier. Las filas nacionales históricas
-- pueden conservar el formato compuesto legado (ej: "oca/oca_SP").
-- No borrar ni reescribir esa historia al conectar Andreani/OCA directo.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS servicio_courier TEXT;

-- Multi-bulto: lista JSON de cajas del envío. Cada elemento:
--   {producto_alias, cantidad, peso_kg, largo_cm, ancho_cm, alto_cm,
--    valor_unitario_usd, hs_code, descripcion_en}
-- cantidad = cajas IDÉNTICAS de ese producto (cada caja viaja como pieza
-- con su propio label). Los campos legacy (producto_alias, cantidad,
-- peso_kg, ...) guardan el primer bulto + totales para retrocompat.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS bultos JSONB;
-- Quién paga los impuestos de destino EN ESTE ENVÍO. Se copia del default
-- del cliente al crear la solicitud y puede pisarse desde el wizard. Queda
-- guardado porque define el incoterm de la guía: si el cliente cambia su
-- default mañana, los envíos ya despachados no pueden cambiar de manos.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS tax_paga TEXT;
-- Empresa y CONTACTO separados (guía real de HAILU, 05/08): los couriers
-- piden companyName (razón social) Y personName (quién atiende). Antes un
-- solo campo forzaba a elegir, y la emisión ponía como empresa al cliente
-- de TAURO en vez del shipper real del envío.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS remitente_contacto TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS dest_contacto TEXT;
-- Conciliación de operaciones irreversibles. Si el courier recibe el POST
-- pero la respuesta se pierde, la solicitud queda bloqueada con su referencia
-- hasta que TAURO compruebe en el portal del courier si la guía existe.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS courier_message_reference TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS courier_error TEXT;
-- La guía puede existir aunque falle el asiento de cuenta corriente. Este
-- flag convierte el antiguo print("FACTURAR A MANO") en una tarea persistente.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS cargo_pendiente BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS cargo_error TEXT;

-- ── Recolecciones de couriers ──────────────────────────────
-- Esquema canónico: no se crea ni se modifica desde el primer request. Las
-- operaciones externas no son idempotentes, por eso los estados transitorios
-- y los índices parciales forman parte de la garantía anti duplicados.
CREATE TABLE IF NOT EXISTS recolecciones (
    id                         SERIAL PRIMARY KEY,
    cliente_id                 TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    courier                    TEXT NOT NULL DEFAULT 'FEDEX',
    fecha                      DATE NOT NULL,
    ready_time                 TEXT NOT NULL DEFAULT '09:00',
    close_time                 TEXT NOT NULL DEFAULT '17:00',
    bultos                     INTEGER NOT NULL DEFAULT 1,
    peso_kg                    REAL NOT NULL DEFAULT 1,
    direccion                  TEXT,
    instrucciones              TEXT,
    estado                     TEXT NOT NULL DEFAULT 'AGENDADA',
    confirmation_code          TEXT,
    ubicacion                  TEXT,
    solicitud_id               INTEGER REFERENCES solicitudes_guia(id),
    courier_message_reference  TEXT,
    error_operativo            TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS recolecciones ADD COLUMN IF NOT EXISTS solicitud_id
    INTEGER REFERENCES solicitudes_guia(id);
ALTER TABLE IF EXISTS recolecciones ADD COLUMN IF NOT EXISTS courier_message_reference TEXT;
ALTER TABLE IF EXISTS recolecciones ADD COLUMN IF NOT EXISTS error_operativo TEXT;
ALTER TABLE IF EXISTS recolecciones ADD COLUMN IF NOT EXISTS updated_at
    TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE INDEX IF NOT EXISTS ix_recolecciones_cliente
    ON recolecciones (cliente_id, fecha DESC);
-- Un cliente no puede reservar dos visitas para el mismo día mientras una
-- creación/cancelación siga abierta o la respuesta del courier sea incierta.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recoleccion_cliente_fecha_abierta_v2
    ON recolecciones (cliente_id, fecha)
    WHERE estado IN ('AGENDANDO', 'AGENDADA', 'CANCELANDO', 'VERIFICAR_COURIER');
-- La misma guía tampoco puede tener dos retiros abiertos en días distintos.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recoleccion_solicitud_abierta_v2
    ON recolecciones (solicitud_id)
    WHERE solicitud_id IS NOT NULL
      AND estado IN ('AGENDANDO', 'AGENDADA', 'CANCELANDO', 'VERIFICAR_COURIER');

-- ── Configuración global (ex CONFIG) ────────────────────────
CREATE TABLE IF NOT EXISTS config (
    parametro TEXT PRIMARY KEY,
    valor     TEXT NOT NULL
);

-- ── CRM comercial y agentes ────────────────────────────────
-- Se mantiene separado de `clientes`: una cuenta prospectada no se convierte
-- en cliente operativo hasta que exista una decision comercial humana.
CREATE TABLE IF NOT EXISTS crm_cuentas (
    id                   BIGSERIAL PRIMARY KEY,
    empresa              TEXT NOT NULL,
    dominio              TEXT UNIQUE,
    sitio_web            TEXT,
    pais                 TEXT,
    segmento             TEXT NOT NULL DEFAULT 'OTRO',
    estado               TEXT NOT NULL DEFAULT 'NUEVO',
    fuente               TEXT NOT NULL DEFAULT 'MANUAL',
    score                INTEGER NOT NULL DEFAULT 0,
    score_breakdown      JSONB NOT NULL DEFAULT '{}'::jsonb,
    discovery_payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    discovery_job_id     BIGINT,
    research_summary     TEXT,
    research_payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    research_model       TEXT,
    research_response_id TEXT,
    excluida             BOOLEAN NOT NULL DEFAULT FALSE,
    investigado_at       TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (score BETWEEN 0 AND 100),
    CHECK (estado IN ('NUEVO', 'INVESTIGADO', 'CALIFICADO', 'DESCARTADO', 'CLIENTE'))
);
CREATE INDEX IF NOT EXISTS idx_crm_cuentas_estado_score
    ON crm_cuentas (estado, score DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS crm_contactos (
    id            BIGSERIAL PRIMARY KEY,
    cuenta_id     BIGINT NOT NULL REFERENCES crm_cuentas(id) ON DELETE CASCADE,
    nombre        TEXT,
    cargo         TEXT,
    email         TEXT NOT NULL UNIQUE,
    estado_email  TEXT NOT NULL DEFAULT 'NO_VERIFICADO',
    fuente_url    TEXT,
    es_principal  BOOLEAN NOT NULL DEFAULT FALSE,
    excluido      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (estado_email IN ('NO_VERIFICADO', 'VERIFICADO', 'REBOTADO', 'BAJA'))
);
CREATE INDEX IF NOT EXISTS idx_crm_contactos_cuenta
    ON crm_contactos (cuenta_id, es_principal DESC);

CREATE TABLE IF NOT EXISTS crm_fuentes (
    id            BIGSERIAL PRIMARY KEY,
    cuenta_id     BIGINT NOT NULL REFERENCES crm_cuentas(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    titulo        TEXT,
    evidencia     TEXT,
    tipo          TEXT NOT NULL DEFAULT 'INVESTIGACION',
    verificado_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (cuenta_id, url)
);

CREATE TABLE IF NOT EXISTS crm_trabajos_agente (
    id            BIGSERIAL PRIMARY KEY,
    tipo          TEXT NOT NULL,
    cuenta_id     BIGINT REFERENCES crm_cuentas(id) ON DELETE CASCADE,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    resultado     JSONB NOT NULL DEFAULT '{}'::jsonb,
    estado        TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    creado_por    TEXT NOT NULL DEFAULT 'admin',
    iniciado_at   TIMESTAMPTZ,
    finalizado_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (tipo IN ('DESCUBRIR', 'INVESTIGAR', 'PROPUESTA')),
    CHECK (estado IN ('PENDIENTE', 'EJECUTANDO', 'COMPLETADO', 'FALLIDO', 'CANCELADO'))
);
CREATE INDEX IF NOT EXISTS idx_crm_trabajos_cola
    ON crm_trabajos_agente (estado, created_at);

CREATE TABLE IF NOT EXISTS crm_mensajes (
    id                 BIGSERIAL PRIMARY KEY,
    cuenta_id          BIGINT NOT NULL REFERENCES crm_cuentas(id) ON DELETE CASCADE,
    contacto_id        BIGINT NOT NULL REFERENCES crm_contactos(id) ON DELETE RESTRICT,
    tipo               TEXT NOT NULL DEFAULT 'PRIMER_CONTACTO',
    asunto             TEXT NOT NULL,
    cuerpo_texto       TEXT NOT NULL,
    estado             TEXT NOT NULL DEFAULT 'BORRADOR',
    draft_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    draft_model        TEXT,
    review_model       TEXT,
    draft_response_id  TEXT,
    review_response_id TEXT,
    checksum           TEXT NOT NULL,
    aprobado_por       TEXT,
    aprobado_at        TIMESTAMPTZ,
    enviado_por        TEXT,
    enviado_at         TIMESTAMPTZ,
    envio_intentos     INTEGER NOT NULL DEFAULT 0,
    ultimo_error       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (estado IN ('BORRADOR', 'OBSERVADO', 'APROBADO', 'ENVIANDO', 'ENVIADO', 'CANCELADO'))
);
CREATE INDEX IF NOT EXISTS idx_crm_mensajes_estado
    ON crm_mensajes (estado, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_crm_mensaje_activo
    ON crm_mensajes (cuenta_id, contacto_id, tipo)
    WHERE estado IN ('BORRADOR', 'APROBADO', 'ENVIANDO');

CREATE TABLE IF NOT EXISTS crm_eventos (
    id          BIGSERIAL PRIMARY KEY,
    event       TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   BIGINT,
    actor       TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crm_eventos_fecha
    ON crm_eventos (created_at DESC);

-- ── Auditoría de seguridad ──────────────────────────────────
-- Quién entró al panel (y quién falló al intentarlo) y qué acción sensible se
-- ejecutó, con IP y momento. NUNCA guarda cuerpos de request ni credenciales
-- (lo garantiza servicios/auditoria.py). Es el registro que mira /admin/seguridad.
CREATE TABLE IF NOT EXISTS security_audit (
    id          BIGSERIAL PRIMARY KEY,
    event       TEXT NOT NULL,
    actor_type  TEXT NOT NULL,
    actor_ref   TEXT,
    ip          TEXT,
    method      TEXT,
    path        TEXT,
    status_code INTEGER,
    success     BOOLEAN NOT NULL DEFAULT TRUE,
    request_id  TEXT,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_security_audit_created ON security_audit (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_security_audit_event   ON security_audit (event);

-- Valores default de config
INSERT INTO config (parametro, valor) VALUES
    ('COTIZACION_DOLAR_ARS', '1450'),
    ('WEB_MARKUP_PCT', '20'),
    ('MARGEN_MINIMO_ARS', '5000'),
    -- Margen de la web POR COURIER. Cada uno tiene el suyo: no es lo mismo
    -- vender un DHL que llega en 2 días que un FedEx que llega en 5, ni son
    -- iguales las tarifas que negociamos con cada uno. Se editan desde
    -- /admin/config sin deploy. Si se borran, cae al WEB_MARKUP_PCT general.
    ('WEB_MARKUP_PCT_FEDEX', '35'),
    ('WEB_MARKUP_PCT_DHL', '20'),
    ('WEB_MARKUP_PCT_UPS', '20'),
    -- Ganancia FIJA en ARS por courier (decisión de Leandro 04/08): cuando
    -- está, el precio de ese courier es COSTO + este monto (ignora el markup %).
    -- DHL va con ganancia fija de $135.000. Editable en /admin/config.
    ('WEB_MARGEN_FIJO_DHL_ARS', '135000')
ON CONFLICT (parametro) DO NOTHING;
