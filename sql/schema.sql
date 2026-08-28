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
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS markup_nac_valor NUMERIC(14,4);
-- Emisión por el cliente (decisión de Leandro 28/07): apagada por defecto,
-- se habilita POR CLIENTE. Emitir cuesta plata real e irreversible, y con
-- tope_deuda_ars el cliente moroso no puede seguir generando costo: si su
-- saldo pendiente supera el tope, emite TAURO o se pone al día. NULL = sin
-- tope (sólo el flag manda).
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS puede_emitir BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS tope_deuda_ars NUMERIC(14,2);
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
    -- Overrides opcionales por costo real del courier en USD. El tramo entre
    -- ambos límites conserva markup_tipo/markup_valor como regla base.
    markup_low_max_usd    NUMERIC(14,4),
    markup_low_ars        NUMERIC(14,4),
    markup_high_min_usd   NUMERIC(14,4),
    markup_high_usd       NUMERIC(14,4),
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
    ),
    CONSTRAINT ck_cliente_courier_tramos CHECK (
        (markup_low_max_usd IS NULL AND markup_low_ars IS NULL)
        OR (
            markup_low_max_usd IS NOT NULL AND markup_low_ars IS NOT NULL
            AND markup_low_max_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND markup_low_ars::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND markup_low_max_usd > 0 AND markup_low_ars >= 0
        )
    ),
    CONSTRAINT ck_cliente_courier_tramo_alto CHECK (
        (markup_high_min_usd IS NULL AND markup_high_usd IS NULL)
        OR (
            markup_high_min_usd IS NOT NULL AND markup_high_usd IS NOT NULL
            AND markup_high_min_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND markup_high_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
            AND markup_high_min_usd > 0 AND markup_high_usd >= 0
        )
    ),
    CONSTRAINT ck_cliente_courier_orden_tramos CHECK (
        markup_low_max_usd IS NULL OR markup_high_min_usd IS NULL
        OR markup_low_max_usd < markup_high_min_usd
    )
);
ALTER TABLE IF EXISTS cliente_courier_config
    ALTER COLUMN puede_cotizar SET DEFAULT FALSE;
ALTER TABLE IF EXISTS cliente_courier_config
    ADD COLUMN IF NOT EXISTS markup_low_max_usd NUMERIC(14,4);
ALTER TABLE IF EXISTS cliente_courier_config
    ADD COLUMN IF NOT EXISTS markup_low_ars NUMERIC(14,4);
ALTER TABLE IF EXISTS cliente_courier_config
    ADD COLUMN IF NOT EXISTS markup_high_min_usd NUMERIC(14,4);
ALTER TABLE IF EXISTS cliente_courier_config
    ADD COLUMN IF NOT EXISTS markup_high_usd NUMERIC(14,4);
DO $$ BEGIN
    ALTER TABLE cliente_courier_config
        ADD CONSTRAINT ck_cliente_courier_tramos
        CHECK (
            (markup_low_max_usd IS NULL AND markup_low_ars IS NULL)
            OR (
                markup_low_max_usd IS NOT NULL AND markup_low_ars IS NOT NULL
                AND markup_low_max_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
                AND markup_low_ars::text NOT IN ('NaN', 'Infinity', '-Infinity')
                AND markup_low_max_usd > 0 AND markup_low_ars >= 0
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE cliente_courier_config
        ADD CONSTRAINT ck_cliente_courier_tramo_alto
        CHECK (
            (markup_high_min_usd IS NULL AND markup_high_usd IS NULL)
            OR (
                markup_high_min_usd IS NOT NULL AND markup_high_usd IS NOT NULL
                AND markup_high_min_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
                AND markup_high_usd::text NOT IN ('NaN', 'Infinity', '-Infinity')
                AND markup_high_min_usd > 0 AND markup_high_usd >= 0
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE cliente_courier_config
        ADD CONSTRAINT ck_cliente_courier_orden_tramos
        CHECK (
            markup_low_max_usd IS NULL OR markup_high_min_usd IS NULL
            OR markup_low_max_usd < markup_high_min_usd
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
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
    origen_plataforma         TEXT,
    origen_dominio            TEXT,
    origen_pedido_externo_id  TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS direcciones
    ADD COLUMN IF NOT EXISTS origen_plataforma TEXT;
ALTER TABLE IF EXISTS direcciones
    ADD COLUMN IF NOT EXISTS origen_dominio TEXT;
ALTER TABLE IF EXISTS direcciones
    ADD COLUMN IF NOT EXISTS origen_pedido_externo_id TEXT;
CREATE INDEX IF NOT EXISTS idx_direcciones_cliente_tipo
    ON direcciones(cliente_id, tipo, predeterminada DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_direcciones_origen_tienda
    ON direcciones(origen_plataforma, origen_dominio, origen_pedido_externo_id)
    WHERE origen_plataforma IS NOT NULL AND origen_dominio IS NOT NULL;

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

-- ── Restablecimiento de contraseña del portal ──
-- El secreto que viaja por email NUNCA se persiste: sólo guardamos su
-- SHA-256. `email_enviado_at` funciona como seguro fail-closed: hasta que el
-- SMTP confirma el envío, el token existe pero no puede canjearse.
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_hash       TEXT PRIMARY KEY,
    cliente_id       TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    creado_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expira_at        TIMESTAMPTZ NOT NULL,
    email_enviado_at TIMESTAMPTZ,
    usado_at         TIMESTAMPTZ,
    CHECK (char_length(token_hash) = 64),
    CHECK (expira_at > creado_at)
);
CREATE INDEX IF NOT EXISTS idx_password_reset_cliente_activo
    ON password_reset_tokens (cliente_id, expira_at DESC)
    WHERE usado_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_password_reset_expira
    ON password_reset_tokens (expira_at);

-- El request web nunca espera SMTP. Encola sólo la identidad interna del
-- cliente; opcionalmente conserva una referencia pública de cotización para
-- retomar el flujo. El worker genera el secreto en memoria y el email no se
-- persiste.
CREATE TABLE IF NOT EXISTS password_reset_requests (
    id                  BIGSERIAL PRIMARY KEY,
    cliente_id          TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    quote_id            TEXT,
    estado              TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos            INTEGER NOT NULL DEFAULT 0,
    proximo_intento_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_id            TEXT,
    claimed_at          TIMESTAMPTZ,
    ultimo_error_code   TEXT,
    email_message_id    TEXT,
    creado_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    enviado_at          TIMESTAMPTZ,
    CONSTRAINT ck_password_reset_request_estado CHECK (
        estado IN ('PENDIENTE','PROCESANDO','ENVIADO','FALLIDO','VERIFICAR_EMAIL')
    ),
    CONSTRAINT ck_password_reset_request_intentos CHECK (intentos BETWEEN 0 AND 3),
    CONSTRAINT ck_password_reset_request_claim CHECK (
        (estado = 'PROCESANDO' AND claim_id IS NOT NULL AND claimed_at IS NOT NULL)
        OR
        (estado <> 'PROCESANDO' AND claim_id IS NULL AND claimed_at IS NULL)
    )
);
ALTER TABLE password_reset_requests
    ADD COLUMN IF NOT EXISTS quote_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_password_reset_request_activa
    ON password_reset_requests (cliente_id)
    WHERE estado IN ('PENDIENTE','PROCESANDO');
CREATE INDEX IF NOT EXISTS idx_password_reset_request_cola
    ON password_reset_requests (proximo_intento_at, creado_at, id)
    WHERE estado = 'PENDIENTE';
CREATE INDEX IF NOT EXISTS idx_password_reset_request_claim
    ON password_reset_requests (claimed_at)
    WHERE estado = 'PROCESANDO';

-- Acceso de emergencia del panel admin. La columna histórica se conserva
-- como `token`, pero sólo almacena SHA-256 hexadecimal; el secreto nunca va
-- a PostgreSQL ni a la ruta HTTP.
CREATE TABLE IF NOT EXISTS admin_recupero (
    token   TEXT PRIMARY KEY,
    vence   TIMESTAMPTZ NOT NULL,
    usado   BOOLEAN NOT NULL DEFAULT FALSE,
    creado  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Invalida y elimina los bearer legacy que alguna versión guardó en claro.
DELETE FROM admin_recupero
 WHERE token !~ '^[0-9a-f]{64}$';
CREATE INDEX IF NOT EXISTS idx_admin_recupero_vence
    ON admin_recupero (vence);
CREATE INDEX IF NOT EXISTS idx_admin_recupero_creado
    ON admin_recupero (creado);

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
    imagen_url       TEXT,                            -- imagen de la tienda/CDN
    plataforma       TEXT,                            -- shopify / tiendanube / manual
    tienda_dominio   TEXT,
    external_product_id       TEXT,
    external_variant_id       TEXT,
    external_inventory_item_id TEXT,
    sku_tienda       TEXT,
    titulo_tienda    TEXT,
    variante_tienda  TEXT,
    precio_tienda    NUMERIC(14,2),
    moneda_tienda    TEXT,
    hs_code_tienda   TEXT,
    pais_origen_tienda TEXT,
    stock_controlado BOOLEAN NOT NULL DEFAULT FALSE,
    stock_disponible INTEGER,
    stock_comprometido INTEGER,
    stock_fisico     INTEGER,
    stock_entrante   INTEGER,
    stock_actualizado_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    source_deleted_at TIMESTAMPTZ,
    source_observed_at TIMESTAMPTZ,
    sync_run_id      TEXT,
    sync_activo      BOOLEAN NOT NULL DEFAULT TRUE,
    activo           BOOLEAN NOT NULL DEFAULT FALSE,  -- pendiente validación Tauro
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(cliente_id, alias_interno)
);
CREATE INDEX IF NOT EXISTS idx_productos_cliente ON productos(cliente_id);
-- Columnas de catálogo externo. Los ALTER mantienen upgrades idempotentes.
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS imagen_url TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS plataforma TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS tienda_dominio TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS external_product_id TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS external_variant_id TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS external_inventory_item_id TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS sku_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS titulo_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS variante_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS precio_tienda NUMERIC(14,2);
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS moneda_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS hs_code_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS pais_origen_tienda TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_controlado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_disponible INTEGER;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_comprometido INTEGER;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_fisico INTEGER;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_entrante INTEGER;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS stock_actualizado_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS source_deleted_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS source_observed_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS sync_run_id TEXT;
ALTER TABLE IF EXISTS productos ADD COLUMN IF NOT EXISTS sync_activo BOOLEAN NOT NULL DEFAULT TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS uq_productos_catalogo_externo_variante
    ON productos (cliente_id, plataforma, tienda_dominio, external_variant_id)
    WHERE external_variant_id IS NOT NULL AND external_variant_id <> '';
CREATE INDEX IF NOT EXISTS ix_productos_catalogo_externo
    ON productos (cliente_id, plataforma, tienda_dominio, sync_activo);

-- Stock por depósito/ubicación. `productos` mantiene los totales para que el
-- portal responda rápido; esta tabla conserva el desglose que viene de Shopify.
CREATE TABLE IF NOT EXISTS producto_inventario_ubicaciones (
    id                   BIGSERIAL PRIMARY KEY,
    cliente_id           TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    producto_id          INTEGER NOT NULL REFERENCES productos(id) ON DELETE CASCADE,
    plataforma           TEXT NOT NULL,
    tienda_dominio       TEXT NOT NULL,
    external_location_id TEXT NOT NULL,
    ubicacion_nombre     TEXT NOT NULL,
    disponible           INTEGER,
    comprometido         INTEGER,
    fisico               INTEGER,
    entrante             INTEGER,
    source_updated_at    TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (producto_id, external_location_id)
);
CREATE INDEX IF NOT EXISTS ix_inventario_ubicaciones_cliente
    ON producto_inventario_ubicaciones (cliente_id, tienda_dominio, producto_id);

-- Instalación OAuth pública por tienda. `install_generation` cambia después
-- de una desinstalación real y permite que el SHOP_REDACT tardío de la
-- generación anterior jamás borre un token recién autorizado.
CREATE TABLE IF NOT EXISTS shopify_instalaciones (
    id                 SERIAL PRIMARY KEY,
    dominio            TEXT NOT NULL UNIQUE,
    access_token       TEXT NOT NULL,
    refresh_token      TEXT,
    access_token_expires_at TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    token_reauth_required BOOLEAN NOT NULL DEFAULT FALSE,
    token_refresh_failed_at TIMESTAMPTZ,
    webhooks_ready     BOOLEAN NOT NULL DEFAULT FALSE,
    webhooks_verified_at TIMESTAMPTZ,
    scopes             TEXT,
    cliente_id         TEXT,
    carrier_id         TEXT,
    app_client_id      TEXT,
    install_generation TEXT NOT NULL,
    instalada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS app_client_id TEXT;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS install_generation TEXT;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS refresh_token TEXT;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS access_token_expires_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS refresh_token_expires_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS token_reauth_required BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS token_refresh_failed_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS webhooks_ready BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS shopify_instalaciones
    ADD COLUMN IF NOT EXISTS webhooks_verified_at TIMESTAMPTZ;
UPDATE shopify_instalaciones
   SET install_generation = md5(
       dominio || ':' || instalada_en::text || ':' || random()::text
   )
 WHERE install_generation IS NULL OR BTRIM(install_generation) = '';
ALTER TABLE IF EXISTS shopify_instalaciones
    ALTER COLUMN install_generation SET NOT NULL;

-- Las filas anteriores no tienen evidencia durable de haber verificado el
-- conjunto completo de suscripciones. Quedan cerradas hasta completar un OAuth
-- nuevo; el owner se preserva, pero su binding no puede operar mientras tanto.
DO $$
BEGIN
    IF to_regclass('public.tiendas_conectadas') IS NOT NULL THEN
        UPDATE tiendas_conectadas t
           SET activa = FALSE
          FROM shopify_instalaciones i
         WHERE LOWER(t.dominio) = LOWER(i.dominio)
           AND t.plataforma = 'shopify'
           AND t.secreto = 'oauth:shopify-app'
           AND i.webhooks_ready = FALSE
           AND t.activa = TRUE;
    END IF;
END $$;

-- Evidencia no sensible de que una generación fue cerrada y purgada. Se
-- conserva para ACKear el SHOP_REDACT que Shopify manda 48 h después sin
-- tocar una reinstalación posterior de la misma app.
CREATE TABLE IF NOT EXISTS shopify_desinstalaciones (
    id                 BIGSERIAL PRIMARY KEY,
    dominio            TEXT NOT NULL,
    shop_id            TEXT NOT NULL DEFAULT '',
    app_client_id      TEXT NOT NULL,
    install_generation TEXT NOT NULL,
    cliente_id         TEXT,
    desinstalada_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    purge_completado_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    shop_redact_ack_at TIMESTAMPTZ,
    UNIQUE (dominio, app_client_id, install_generation)
);
CREATE INDEX IF NOT EXISTS ix_shopify_desinstalaciones_redact
    ON shopify_desinstalaciones(dominio, shop_id, app_client_id, desinstalada_at DESC);

-- Si llega shop/redact para una tienda activa y no existe un tombstone que
-- identifique la generación desinstalada, no se borra a ciegas: la obligación
-- queda persistida para conciliación y alerta operativa.
CREATE TABLE IF NOT EXISTS shopify_shop_redact_pendientes (
    dominio                   TEXT NOT NULL,
    shop_id                   TEXT NOT NULL,
    app_client_id             TEXT NOT NULL,
    install_generation_activa TEXT NOT NULL,
    estado                    TEXT NOT NULL DEFAULT 'VERIFICAR_GENERACION',
    recibido_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ultimo_intento_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dominio, shop_id, app_client_id)
);
CREATE INDEX IF NOT EXISTS ix_shop_redact_pendientes_estado
    ON shopify_shop_redact_pendientes(estado, recibido_at);

-- Una redacción de comprador debe seguir bloqueando copias tardías aunque
-- un form estuviera abierto o un worker reintentara después del webhook.
CREATE TABLE IF NOT EXISTS shopify_pedidos_redactados (
    dominio           TEXT NOT NULL,
    pedido_externo_id TEXT NOT NULL,
    redactado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dominio, pedido_externo_id)
);
CREATE TABLE IF NOT EXISTS shopify_webhook_recibidos (
    webhook_id        TEXT PRIMARY KEY,
    dominio           TEXT NOT NULL,
    topic             TEXT NOT NULL,
    install_generation TEXT NOT NULL,
    procesado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_shopify_webhook_recibidos_fecha
    ON shopify_webhook_recibidos(procesado_at);
CREATE TABLE IF NOT EXISTS shopify_huerfanos_cancelados (
    dominio            TEXT NOT NULL,
    pedido_externo_id  TEXT NOT NULL,
    install_generation TEXT NOT NULL,
    cancelado_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dominio, pedido_externo_id, install_generation)
);
CREATE INDEX IF NOT EXISTS ix_shopify_huerfanos_cancelados_fecha
    ON shopify_huerfanos_cancelados(cancelado_at);
-- Los payloads sin owner quedan ligados a la generación que los recibió;
-- jamás pueden volcarse sobre una reinstalación posterior del mismo dominio.
ALTER TABLE IF EXISTS pedidos_huerfanos
    ADD COLUMN IF NOT EXISTS install_generation TEXT;

-- Estado visible de la sincronización. No guarda tokens ni payloads sensibles.
CREATE TABLE IF NOT EXISTS shopify_sync_estado (
    dominio               TEXT PRIMARY KEY,
    cliente_id            TEXT REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    estado                TEXT NOT NULL DEFAULT 'PENDIENTE',
    ultimo_intento_at     TIMESTAMPTZ,
    ultima_sincronizacion_at TIMESTAMPTZ,
    ultimo_error_codigo   TEXT,
    ultimo_error          TEXT,
    productos_total       INTEGER NOT NULL DEFAULT 0,
    variantes_total       INTEGER NOT NULL DEFAULT 0,
    creados               INTEGER NOT NULL DEFAULT 0,
    actualizados          INTEGER NOT NULL DEFAULT 0,
    desactivados          INTEGER NOT NULL DEFAULT 0,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_shopify_sync_cliente
    ON shopify_sync_estado (cliente_id, ultima_sincronizacion_at);

-- Cola durable de webhooks de catálogo/inventario. Permite contestar 200 a
-- Shopify rápido y terminar el trabajo aunque el proceso reinicie.
CREATE TABLE IF NOT EXISTS shopify_webhook_eventos (
    webhook_id       TEXT PRIMARY KEY,
    dominio          TEXT NOT NULL,
    topic            TEXT NOT NULL,
    triggered_at     TIMESTAMPTZ,
    install_generation TEXT NOT NULL,
    payload          JSONB NOT NULL,
    estado           TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos         INTEGER NOT NULL DEFAULT 0,
    ultimo_error     TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at       TIMESTAMPTZ,
    processed_at     TIMESTAMPTZ
);
ALTER TABLE IF EXISTS shopify_webhook_eventos
    ADD COLUMN IF NOT EXISTS install_generation TEXT;
-- Las entregas anteriores a la migración no pueden atribuirse con seguridad
-- a una instalación. Se conservan como evidencia pero el worker las trata
-- como obsoletas; el catálogo se recupera con la reconciliación completa.
UPDATE shopify_webhook_eventos
   SET install_generation = 'legacy-sin-generacion'
 WHERE install_generation IS NULL OR BTRIM(install_generation) = '';
ALTER TABLE IF EXISTS shopify_webhook_eventos
    ALTER COLUMN install_generation SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_shopify_webhook_pendientes
    ON shopify_webhook_eventos (estado, created_at);

-- Solicitudes obligatorias de acceso a datos de compradores (Shopify GDPR).
-- El webhook contiene email y telefono, pero esta cola guarda unicamente las
-- referencias imprescindibles para preparar la respuesta bajo demanda. El
-- cuerpo crudo nunca se persiste ni se copia a logs/correos.
CREATE TABLE IF NOT EXISTS shopify_gdpr_solicitudes (
    id                  BIGSERIAL PRIMARY KEY,
    request_id          TEXT NOT NULL,
    dominio             TEXT NOT NULL,
    shop_id             TEXT NOT NULL,
    orders_requested    JSONB NOT NULL DEFAULT '[]'::jsonb,
    estado              TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos            INTEGER NOT NULL DEFAULT 0,
    proximo_intento_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_id            TEXT,
    claimed_at          TIMESTAMPTZ,
    ultimo_error_code   TEXT,
    message_id          TEXT,
    notificado_at       TIMESTAMPTZ,
    resuelto_at         TIMESTAMPTZ,
    creado_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        jsonb_typeof(orders_requested) = 'array'
        AND jsonb_array_length(orders_requested) <= 500
    ),
    CHECK (intentos >= 0),
    CHECK (estado IN (
        'PENDIENTE', 'PROCESANDO', 'NOTIFICADO',
        'VERIFICAR_EMAIL', 'FALLIDO', 'RESUELTO'
    )),
    -- Shopify documenta el id dentro de cada shop; no se asume unicidad
    -- global entre comercios.
    UNIQUE (shop_id, request_id)
);
CREATE INDEX IF NOT EXISTS ix_shopify_gdpr_pendientes
    ON shopify_gdpr_solicitudes (estado, proximo_intento_at, creado_at)
    WHERE estado <> 'RESUELTO';
CREATE INDEX IF NOT EXISTS ix_shopify_gdpr_resueltas_retencion
    ON shopify_gdpr_solicitudes (resuelto_at)
    WHERE estado = 'RESUELTO';

-- Política de precio de envío por tienda. También existe un ensure local en
-- politica_envio.py; declararla en startup garantiza que los webhooks GDPR
-- puedan purgarla incluso si el cliente nunca abrió esa configuración.
CREATE TABLE IF NOT EXISTS config_envio_tienda (
    dominio          TEXT PRIMARY KEY,
    cliente_id       TEXT,
    politica         TEXT NOT NULL DEFAULT 'real',
    markup_pct       NUMERIC(6,2) NOT NULL DEFAULT 0,
    precio_fijo_ars  NUMERIC(14,2) NOT NULL DEFAULT 0,
    mostrar_tax      BOOLEAN NOT NULL DEFAULT FALSE,
    tax_pct_default  NUMERIC(6,2) NOT NULL DEFAULT 0,
    etiqueta         TEXT DEFAULT '',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Pagos recibidos (ex PAGOS) ──────────────────────────────
CREATE TABLE IF NOT EXISTS pagos (
    id           SERIAL PRIMARY KEY,
    cliente_id   TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE RESTRICT,
    fecha        DATE NOT NULL,
    monto_ars    NUMERIC(14,2) NOT NULL,
    metodo       TEXT NOT NULL DEFAULT 'transferencia',
    referencia   TEXT,
    nota         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Preserva el libro contable al borrar clientes. CREATE TABLE IF NOT EXISTS no
-- modifica FKs legacy. Se identifican por tablas + columnas, no por nombre:
-- una FK custom CASCADE también debe desaparecer. Si ya hay exactamente una
-- canónica, validada y RESTRICT, el bloque no ejecuta ningún DDL.
DO $$
DECLARE
    cantidad_exactas INTEGER;
    todas_canonicas  BOOLEAN;
    fk               RECORD;
BEGIN
    SELECT COUNT(*), COALESCE(BOOL_AND(
               c.conname = 'pagos_cliente_id_fkey'
               AND c.confdeltype = 'r'
               AND c.convalidated
           ), FALSE)
      INTO cantidad_exactas, todas_canonicas
      FROM pg_constraint c
     WHERE c.contype = 'f'
       AND c.conrelid = 'pagos'::regclass
       AND c.confrelid = 'clientes'::regclass
       AND c.conkey = ARRAY[(
           SELECT a.attnum
             FROM pg_attribute a
            WHERE a.attrelid = 'pagos'::regclass
              AND a.attname = 'cliente_id'
              AND NOT a.attisdropped
       )]::SMALLINT[]
       AND c.confkey = ARRAY[(
           SELECT a.attnum
             FROM pg_attribute a
            WHERE a.attrelid = 'clientes'::regclass
              AND a.attname = 'cliente_id'
              AND NOT a.attisdropped
       )]::SMALLINT[];

    IF cantidad_exactas <> 1 OR NOT todas_canonicas THEN
        FOR fk IN
            SELECT c.conrelid, c.conname
              FROM pg_constraint c
             WHERE c.contype = 'f'
               AND c.conrelid = 'pagos'::regclass
               AND c.confrelid = 'clientes'::regclass
               AND c.conkey = ARRAY[(
                   SELECT a.attnum FROM pg_attribute a
                    WHERE a.attrelid = 'pagos'::regclass
                      AND a.attname = 'cliente_id' AND NOT a.attisdropped
               )]::SMALLINT[]
               AND c.confkey = ARRAY[(
                   SELECT a.attnum FROM pg_attribute a
                    WHERE a.attrelid = 'clientes'::regclass
                      AND a.attname = 'cliente_id' AND NOT a.attisdropped
               )]::SMALLINT[]
        LOOP
            EXECUTE FORMAT(
                'ALTER TABLE %s DROP CONSTRAINT %I',
                fk.conrelid::regclass,
                fk.conname
            );
        END LOOP;

        ALTER TABLE pagos
            ADD CONSTRAINT pagos_cliente_id_fkey
            FOREIGN KEY (cliente_id) REFERENCES clientes(cliente_id)
            ON DELETE RESTRICT;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_pagos_cliente ON pagos(cliente_id);
-- Pagos informados por el CLIENTE con comprobante (decisión de Leandro
-- 28/07): entran como PENDIENTE y NO tocan el saldo hasta que el admin los
-- aprueba. Los que carga el admin nacen APROBADO (las filas viejas, con
-- estado NULL, cuentan como aprobadas — eran cargas del admin).
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS estado TEXT DEFAULT 'APROBADO';
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante BYTEA;
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante_tipo TEXT;
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS comprobante_nombre TEXT;
-- Token opaco de la operación de alta. NULL conserva compatibilidad con
-- historia/importaciones; los flujos interactivos nuevos siempre lo envían.
ALTER TABLE IF EXISTS pagos ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pagos'::regclass
          AND conname = 'ck_pagos_idempotency_key'
    ) THEN
        ALTER TABLE pagos ADD CONSTRAINT ck_pagos_idempotency_key
            CHECK (
                idempotency_key IS NULL
                OR idempotency_key ~ '^[A-Za-z0-9_-]{32,128}$'
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE pagos VALIDATE CONSTRAINT ck_pagos_idempotency_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pagos_cliente_idempotency
    ON pagos(cliente_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Aplicación contable explícita del pago por ÁMBITO. Un pago APROBADO puede
-- distribuirse entre NACIONAL e INTERNACIONAL y conservar el resto como
-- crédito sin imputar. Deliberadamente no hay backfill: los pagos históricos
-- aprobados arrancan sin filas acá y, por lo tanto, 100% sin imputar.
CREATE TABLE IF NOT EXISTS pagos_aplicaciones (
    id          SERIAL PRIMARY KEY,
    pago_id     INTEGER NOT NULL REFERENCES pagos(id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    ambito      TEXT NOT NULL,
    monto_ars   NUMERIC(14,2) NOT NULL,
    estado      TEXT NOT NULL DEFAULT 'APLICADA',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pago_id, ambito),
    CONSTRAINT ck_pagos_aplicaciones_ambito
        CHECK (ambito IN ('NACIONAL', 'INTERNACIONAL')),
    CONSTRAINT ck_pagos_aplicaciones_monto
        CHECK (monto_ars > 0),
    CONSTRAINT ck_pagos_aplicaciones_estado
        CHECK (estado IN ('SOLICITADA', 'APLICADA'))
);
CREATE INDEX IF NOT EXISTS idx_pagos_aplicaciones_pago
    ON pagos_aplicaciones(pago_id);

-- Serializa todas las aplicaciones de un pago sobre la fila padre. Así dos
-- decisiones concurrentes no pueden acreditar, entre ambas, más que el pago.
-- Las solicitudes de un PENDIENTE se guardan, pero no son haber hasta que el
-- admin las confirme al aprobar; aun así, tampoco pueden superar el pago.
CREATE OR REPLACE FUNCTION validar_pago_aplicacion()
RETURNS TRIGGER AS $$
DECLARE
    pago_monto NUMERIC(14,2);
    pago_estado TEXT;
    ya_aplicado NUMERIC(14,2);
BEGIN
    IF TG_OP = 'UPDATE'
       AND (NEW.pago_id <> OLD.pago_id OR NEW.ambito <> OLD.ambito) THEN
        RAISE EXCEPTION 'No se puede cambiar pago/ámbito de una aplicación; reemplácela';
    END IF;

    SELECT monto_ars::numeric, COALESCE(estado, 'APROBADO')
      INTO pago_monto, pago_estado
      FROM pagos
     WHERE id = NEW.pago_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'El pago % no existe', NEW.pago_id;
    END IF;
    IF NEW.estado = 'APLICADA' AND pago_estado <> 'APROBADO' THEN
        RAISE EXCEPTION 'El pago % no está aprobado', NEW.pago_id;
    END IF;
    IF NEW.estado = 'SOLICITADA' AND pago_estado <> 'PENDIENTE' THEN
        RAISE EXCEPTION 'Sólo un pago pendiente admite una imputación solicitada';
    END IF;

    SELECT COALESCE(SUM(monto_ars), 0)
      INTO ya_aplicado
     FROM pagos_aplicaciones
     WHERE pago_id = NEW.pago_id
       AND id <> COALESCE(NEW.id, -1);

    IF ya_aplicado + NEW.monto_ars > pago_monto THEN
        RAISE EXCEPTION
            'Las aplicaciones (%) superan el monto (%) del pago %',
            ya_aplicado + NEW.monto_ars, pago_monto, NEW.pago_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_pago_aplicacion ON pagos_aplicaciones;
CREATE TRIGGER trg_validar_pago_aplicacion
BEFORE INSERT OR UPDATE ON pagos_aplicaciones
FOR EACH ROW EXECUTE FUNCTION validar_pago_aplicacion();

-- Evita que una edición posterior achique o desapruebe un pago ya imputado.
CREATE OR REPLACE FUNCTION validar_pago_con_aplicaciones()
RETURNS TRIGGER AS $$
DECLARE
    aplicado NUMERIC(14,2);
BEGIN
    SELECT COALESCE(SUM(monto_ars), 0)
      INTO aplicado
      FROM pagos_aplicaciones
     WHERE pago_id = OLD.id
       AND estado = 'APLICADA';

    IF aplicado > NEW.monto_ars::numeric THEN
        RAISE EXCEPTION 'El pago % tiene % aplicado y no puede reducirse a %',
            OLD.id, aplicado, NEW.monto_ars;
    END IF;
    IF aplicado > 0 AND COALESCE(NEW.estado, 'APROBADO') <> 'APROBADO' THEN
        RAISE EXCEPTION 'El pago % tiene aplicaciones y debe seguir aprobado', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_pago_con_aplicaciones ON pagos;
CREATE TRIGGER trg_validar_pago_con_aplicaciones
BEFORE UPDATE OF monto_ars, estado ON pagos
FOR EACH ROW EXECUTE FUNCTION validar_pago_con_aplicaciones();

-- Historial de salud para la página de estado pública (/estado). El
-- centinela (cada 15 min) suma acá: checks del día y cuántos fallaron.
CREATE TABLE IF NOT EXISTS salud_historial (
    dia     DATE PRIMARY KEY,
    checks  INTEGER NOT NULL DEFAULT 0,
    fallos  INTEGER NOT NULL DEFAULT 0
);

-- ── Cotizaciones públicas y entrega por correo ─────────────
-- El navegador recibe un identificador opaco y nunca vuelve a enviar precios.
-- El presupuesto por mail/web se reconstruye desde este snapshot del servidor.
CREATE TABLE IF NOT EXISTS cotizaciones_web (
    id                  BIGSERIAL PRIMARY KEY,
    public_id           TEXT NOT NULL UNIQUE,
    referencia          TEXT NOT NULL UNIQUE,
    origen              CHAR(2) NOT NULL,
    destino             CHAR(2) NOT NULL,
    peso_kg             NUMERIC(10,3) NOT NULL,
    largo_cm            NUMERIC(10,2) NOT NULL,
    ancho_cm            NUMERIC(10,2) NOT NULL,
    alto_cm             NUMERIC(10,2) NOT NULL,
    valor_declarado_usd NUMERIC(14,2) NOT NULL,
    recomendado         TEXT NOT NULL DEFAULT '',
    resumen             JSONB NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vigente_hasta       TIMESTAMPTZ NOT NULL,
    CHECK (public_id ~ '^Q-[A-Za-z0-9_-]{20,64}$'),
    CHECK (referencia ~ '^TW-[0-9]{8}-[A-F0-9]{6}$'),
    CHECK (origen ~ '^[A-Z]{2}$' AND destino ~ '^[A-Z]{2}$'),
    CHECK (NOT (origen = 'AR' AND destino = 'AR')),
    CHECK (peso_kg > 0 AND largo_cm > 0 AND ancho_cm > 0 AND alto_cm > 0),
    CHECK (valor_declarado_usd > 0),
    CHECK (vigente_hasta > created_at),
    CHECK (
        CASE WHEN jsonb_typeof(resumen) = 'array'
             THEN jsonb_array_length(resumen) > 0
             ELSE FALSE
        END
    )
);
CREATE INDEX IF NOT EXISTS idx_cotizaciones_web_vigencia
    ON cotizaciones_web (vigente_hasta DESC);

-- El lead conserva el pedido de entrega y su estado real. ENVIADO significa
-- exclusivamente que SMTP aceptó el mensaje; LEGACY identifica filas previas
-- a esta migración, cuyo resultado no se puede reconstruir honestamente.
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
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS cotizacion_id BIGINT
    REFERENCES cotizaciones_web(id) ON DELETE RESTRICT;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_estado TEXT NOT NULL DEFAULT 'PENDIENTE';
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_intentos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_error_codigo TEXT;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_claim TEXT;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_message_id TEXT;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_enviado_at TIMESTAMPTZ;
ALTER TABLE leads_cotizacion ADD COLUMN IF NOT EXISTS email_actualizado_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
UPDATE leads_cotizacion
   SET email_estado = 'LEGACY'
 WHERE cotizacion_id IS NULL AND email_estado = 'PENDIENTE';
DO $$
DECLARE
    definicion TEXT;
BEGIN
    SELECT PG_GET_CONSTRAINTDEF(oid)
      INTO definicion
      FROM pg_constraint
     WHERE conname = 'ck_leads_cotizacion_email_estado'
       AND conrelid = 'leads_cotizacion'::regclass;
    IF definicion IS NOT NULL AND definicion NOT ILIKE '%VERIFICAR_EMAIL%' THEN
        ALTER TABLE leads_cotizacion
            DROP CONSTRAINT ck_leads_cotizacion_email_estado;
        definicion := NULL;
    END IF;
    IF definicion IS NULL THEN
        ALTER TABLE leads_cotizacion
            ADD CONSTRAINT ck_leads_cotizacion_email_estado
            CHECK (email_estado IN (
                'PENDIENTE','ENVIANDO','ENVIADO','FALLIDO',
                'VERIFICAR_EMAIL','LEGACY'
            ));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_leads_cotizacion_email_fecha
    ON leads_cotizacion (LOWER(email), created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_cotizacion_email_estado_fecha
    ON leads_cotizacion (LOWER(email), email_actualizado_at DESC, email_estado);
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_cotizacion_email
    ON leads_cotizacion (cotizacion_id, LOWER(email))
    WHERE cotizacion_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_cotizacion_estado
    ON leads_cotizacion (email_estado, email_actualizado_at)
    WHERE email_estado IN ('PENDIENTE','ENVIANDO','FALLIDO');

-- ── Envíos / Facturas (ex ENVIOS 2026) ─────────────────────
CREATE TABLE IF NOT EXISTS envios (
    id           SERIAL PRIMARY KEY,
    cliente_id   TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE RESTRICT,
    fecha        DATE NOT NULL,
    nro_fc       TEXT,
    monto_ars    NUMERIC(14,2) NOT NULL DEFAULT 0,
    estado       TEXT NOT NULL DEFAULT 'ACTIVO',  -- ACTIVO | CANCELADO | NC
    descripcion  TEXT,
    tracking     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
DO $$
DECLARE
    cantidad_exactas INTEGER;
    todas_canonicas  BOOLEAN;
    fk               RECORD;
BEGIN
    SELECT COUNT(*), COALESCE(BOOL_AND(
               c.conname = 'envios_cliente_id_fkey'
               AND c.confdeltype = 'r'
               AND c.convalidated
           ), FALSE)
      INTO cantidad_exactas, todas_canonicas
      FROM pg_constraint c
     WHERE c.contype = 'f'
       AND c.conrelid = 'envios'::regclass
       AND c.confrelid = 'clientes'::regclass
       AND c.conkey = ARRAY[(
           SELECT a.attnum
             FROM pg_attribute a
            WHERE a.attrelid = 'envios'::regclass
              AND a.attname = 'cliente_id'
              AND NOT a.attisdropped
       )]::SMALLINT[]
       AND c.confkey = ARRAY[(
           SELECT a.attnum
             FROM pg_attribute a
            WHERE a.attrelid = 'clientes'::regclass
              AND a.attname = 'cliente_id'
              AND NOT a.attisdropped
       )]::SMALLINT[];

    IF cantidad_exactas <> 1 OR NOT todas_canonicas THEN
        FOR fk IN
            SELECT c.conrelid, c.conname
              FROM pg_constraint c
             WHERE c.contype = 'f'
               AND c.conrelid = 'envios'::regclass
               AND c.confrelid = 'clientes'::regclass
               AND c.conkey = ARRAY[(
                   SELECT a.attnum FROM pg_attribute a
                    WHERE a.attrelid = 'envios'::regclass
                      AND a.attname = 'cliente_id' AND NOT a.attisdropped
               )]::SMALLINT[]
               AND c.confkey = ARRAY[(
                   SELECT a.attnum FROM pg_attribute a
                    WHERE a.attrelid = 'clientes'::regclass
                      AND a.attname = 'cliente_id' AND NOT a.attisdropped
               )]::SMALLINT[]
        LOOP
            EXECUTE FORMAT(
                'ALTER TABLE %s DROP CONSTRAINT %I',
                fk.conrelid::regclass,
                fk.conname
            );
        END LOOP;

        ALTER TABLE envios
            ADD CONSTRAINT envios_cliente_id_fkey
            FOREIGN KEY (cliente_id) REFERENCES clientes(cliente_id)
            ON DELETE RESTRICT;
    END IF;
END $$;
-- Vacío significa cargo aún pendiente de facturación. Si se informa una FC,
-- debe contener al menos un carácter alfanumérico después de normalizarla;
-- valores como "---" no pueden eludir el índice único global.
DO $$
BEGIN
    -- Nombre transitorio usado sólo durante desarrollo de este worktree.
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'envios'::regclass
          AND conname = 'ck_envios_nro_fc_valido'
    ) THEN
        ALTER TABLE envios DROP CONSTRAINT ck_envios_nro_fc_valido;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'envios'::regclass
          AND conname = 'ck_envios_nro_fc_valida'
    ) THEN
        ALTER TABLE envios ADD CONSTRAINT ck_envios_nro_fc_valida
            CHECK (
                nro_fc IS NULL
                OR BTRIM(nro_fc) = ''
                OR REGEXP_REPLACE(
                    UPPER(BTRIM(nro_fc)), '[^A-Z0-9]', '', 'g'
                ) <> ''
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE envios VALIDATE CONSTRAINT ck_envios_nro_fc_valida;
-- Factura emitida con su PDF adjunto (cargada por el admin). Estos ALTER van
-- después del CREATE para que una base nueva también reciba las columnas.
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_pdf BYTEA;
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS factura_nombre TEXT;
ALTER TABLE IF EXISTS envios ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'envios'::regclass
          AND conname = 'ck_envios_idempotency_key'
    ) THEN
        ALTER TABLE envios ADD CONSTRAINT ck_envios_idempotency_key
            CHECK (
                idempotency_key IS NULL
                OR idempotency_key ~ '^[A-Za-z0-9_-]{32,128}$'
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE envios VALIDATE CONSTRAINT ck_envios_idempotency_key;
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
CREATE UNIQUE INDEX IF NOT EXISTS uq_envios_cliente_idempotency
    ON envios(cliente_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- Una FC puede escribirse con espacios, guiones o distinta capitalización,
-- pero sigue siendo la misma factura en toda TAURO. La creación del índice
-- falla (intencionalmente) si el preflight detectó historia duplicada: no se
-- elige ni borra una fila automáticamente. NC no es FC; tracking no participa.
CREATE UNIQUE INDEX IF NOT EXISTS uq_envios_fc_normalizada
    ON envios (
        (REGEXP_REPLACE(UPPER(BTRIM(nro_fc)), '[^A-Z0-9]', '', 'g'))
    )
    WHERE COALESCE(UPPER(BTRIM(estado)), '') <> 'NC'
      AND REGEXP_REPLACE(UPPER(BTRIM(COALESCE(nro_fc, ''))), '[^A-Z0-9]', '', 'g') <> '';
-- Sólo se retira el índice anterior, más débil, si el global existe. Aun si
-- un runner continuara después de un error, nunca deja la tabla sin control.
DO $$
BEGIN
    IF TO_REGCLASS('uq_envios_fc_normalizada') IS NOT NULL THEN
        DROP INDEX IF EXISTS uq_envios_cliente_fc_normalizada;
    END IF;
END $$;
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
    origen_plataforma        TEXT,
    origen_dominio           TEXT,
    origen_pedido_externo_id TEXT,
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
-- Linaje durable: una solicitud nacida de una venta conserva su origen en el
-- INSERT inicial. No depende del vínculo posterior pedidos_tienda.solicitud_id,
-- que puede fallar o desaparecer durante una solicitud de privacidad.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS origen_plataforma TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS origen_dominio TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS origen_pedido_externo_id TEXT;
CREATE INDEX IF NOT EXISTS idx_solicitudes_guia_origen_tienda
    ON solicitudes_guia(origen_plataforma, origen_dominio, origen_pedido_externo_id)
    WHERE origen_plataforma IS NOT NULL AND origen_dominio IS NOT NULL;

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
-- DHL devuelve la factura comercial separada del label. Railway no garantiza
-- persistencia del filesystem, por lo que ambos documentos viven en Postgres.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS commercial_invoice_pdf BYTEA;
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

-- Contrato B2B idempotente: un retry de POST /pedido con la misma clave
-- devuelve la solicitud original. El fingerprint impide reutilizar esa clave
-- accidentalmente con otro comprador o contenido.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS api_referencia TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS idempotency_key_hash TEXT;
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS request_fingerprint TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_solicitudes_cliente_idempotency
    ON solicitudes_guia(cliente_id, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;

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
