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
    test         BOOLEAN NOT NULL DEFAULT FALSE,
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
-- Las cuentas de prueba se conservan para auditoría, pero no contaminan
-- tableros, selectores operativos ni saldos agregados del negocio.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT FALSE;
-- Un reseller puede descargar una cotización comercial TAURO con un precio
-- de reventa propio. El flag no altera guías, invoices ni costos internos.
ALTER TABLE IF EXISTS clientes ADD COLUMN IF NOT EXISTS es_reseller BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_clientes_operativos ON clientes(activo, test);

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
    valor_usd_default NUMERIC(14,2) NOT NULL DEFAULT 0,
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

-- Tiendanube: OAuth, lifecycle y claim seguro posterior. Una instalación
-- iniciada desde la App Store puede existir sin cliente TAURO; el navegador
-- que completó OAuth recibe el secreto y acá sólo se conserva su hash.
CREATE TABLE IF NOT EXISTS tiendanube_instalaciones (
    id                   SERIAL PRIMARY KEY,
    store_id             TEXT NOT NULL UNIQUE,
    access_token         TEXT NOT NULL,
    cliente_id           TEXT,
    nombre               TEXT,
    estado               TEXT NOT NULL DEFAULT 'ACTIVA',
    install_generation   TEXT,
    webhooks_ready       BOOLEAN NOT NULL DEFAULT FALSE,
    webhooks_verified_at TIMESTAMPTZ,
    claim_token_hash     TEXT,
    claim_expires_at     TIMESTAMPTZ,
    instalada_en         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    suspendida_en        TIMESTAMPTZ,
    desinstalada_en      TIMESTAMPTZ,
    redactada_en         TIMESTAMPTZ
);
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'ACTIVA';
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS install_generation TEXT;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS webhooks_ready BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS webhooks_verified_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS claim_token_hash TEXT;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS actualizada_en TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS suspendida_en TIMESTAMPTZ;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS desinstalada_en TIMESTAMPTZ;
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ADD COLUMN IF NOT EXISTS redactada_en TIMESTAMPTZ;
UPDATE tiendanube_instalaciones
   SET install_generation = md5(
       store_id || ':' || instalada_en::text || ':' || random()::text
   )
 WHERE install_generation IS NULL OR BTRIM(install_generation) = '';
ALTER TABLE IF EXISTS tiendanube_instalaciones
    ALTER COLUMN install_generation SET NOT NULL;

CREATE TABLE IF NOT EXISTS tiendanube_lifecycle_eventos (
    id                 BIGSERIAL PRIMARY KEY,
    store_id           TEXT NOT NULL,
    evento             TEXT NOT NULL,
    install_generation TEXT NOT NULL,
    recibido_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_tiendanube_lifecycle_store
    ON tiendanube_lifecycle_eventos(store_id, recibido_at DESC);

-- El endpoint sólo responde 2xx después de este INSERT. El worker puede
-- reiniciarse, recuperar PROCESANDO stale y aplicar backoff sin perder eventos.
CREATE TABLE IF NOT EXISTS tiendanube_webhook_eventos (
    evento_id          TEXT PRIMARY KEY,
    store_id           TEXT NOT NULL,
    evento             TEXT NOT NULL,
    recurso_id         TEXT NOT NULL DEFAULT '',
    install_generation TEXT NOT NULL,
    payload            JSONB NOT NULL,
    estado             TEXT NOT NULL DEFAULT 'PENDIENTE',
    intentos           INTEGER NOT NULL DEFAULT 0,
    proximo_intento_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ultimo_error       TEXT,
    recibido_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at         TIMESTAMPTZ,
    procesado_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_tiendanube_webhook_pendientes
    ON tiendanube_webhook_eventos(estado, proximo_intento_at, recibido_at);

-- No persiste email, teléfono ni identificación del consumidor. La solicitud
-- conserva sólo referencias necesarias para responder al merchant.
CREATE TABLE IF NOT EXISTS tiendanube_privacidad_solicitudes (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    tipo        TEXT NOT NULL,
    customer_id TEXT NOT NULL DEFAULT '',
    recursos    JSONB NOT NULL DEFAULT '[]'::jsonb,
    estado      TEXT NOT NULL DEFAULT 'PENDIENTE',
    resolucion  TEXT,
    creado_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resuelto_at TIMESTAMPTZ,
    UNIQUE(store_id, tipo, request_id)
);
ALTER TABLE IF EXISTS tiendanube_privacidad_solicitudes
    ADD COLUMN IF NOT EXISTS resolucion TEXT;
CREATE INDEX IF NOT EXISTS ix_tiendanube_privacidad_pendientes
    ON tiendanube_privacidad_solicitudes(estado, creado_at);

-- Tombstone de privacidad: un order/updated atrasado no puede reintroducir
-- datos personales después de customers/redact o store/redact.
CREATE TABLE IF NOT EXISTS tiendanube_pedidos_redactados (
    dominio           TEXT NOT NULL,
    pedido_externo_id TEXT NOT NULL,
    redactado_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dominio, pedido_externo_id)
);

-- Shipping Carrier: sólo se persisten hashes de los secretos de callback.
-- Eliminar esta configuración durante store/redact borra también la evidencia
-- de labels por las FKs ON DELETE CASCADE definidas debajo.
CREATE TABLE IF NOT EXISTS tiendanube_shipping_config (
    store_id                  TEXT PRIMARY KEY,
    callback_token_hash       TEXT NOT NULL,
    label_callback_token_hash TEXT,
    carrier_id                TEXT NOT NULL,
    carrier_option_id         TEXT NOT NULL,
    activa                    BOOLEAN NOT NULL DEFAULT TRUE,
    creada_en                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE IF EXISTS tiendanube_shipping_config
    ADD COLUMN IF NOT EXISTS label_callback_token_hash TEXT;

-- Labels API: registro canónico y outbox idempotente. Mientras el adapter
-- nacional no esté homologado, generate_payload sólo conserva IDs y la huella
-- del payload original; no conserva datos personales del destinatario.
CREATE TABLE IF NOT EXISTS tiendanube_labels (
    store_id                    TEXT NOT NULL,
    label_id                    TEXT NOT NULL,
    fulfillment_order_id        TEXT NOT NULL,
    generate_payload            JSONB,
    generate_fingerprint        CHAR(64),
    generate_payload_complete   BOOLEAN NOT NULL DEFAULT FALSE,
    estado                      TEXT NOT NULL,
    external_operation_id       TEXT,
    tracking_number             TEXT,
    creada_en                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (store_id, label_id),
    FOREIGN KEY (store_id)
        REFERENCES tiendanube_shipping_config(store_id)
        ON DELETE CASCADE
);
ALTER TABLE IF EXISTS tiendanube_labels
    ADD COLUMN IF NOT EXISTS generate_payload_complete
        BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS tiendanube_label_outbox (
    id                      BIGSERIAL PRIMARY KEY,
    store_id                TEXT NOT NULL,
    label_id                TEXT NOT NULL,
    operacion               TEXT NOT NULL
        CHECK (operacion IN ('GENERATE', 'CANCEL')),
    payload                 JSONB NOT NULL,
    payload_fingerprint     CHAR(64) NOT NULL,
    payload_complete        BOOLEAN NOT NULL DEFAULT FALSE,
    estado                  TEXT NOT NULL,
    intentos                INTEGER NOT NULL DEFAULT 0,
    proximo_intento_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ultimo_error_codigo     TEXT,
    creada_en               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actualizada_en          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    procesada_en            TIMESTAMPTZ,
    UNIQUE (store_id, label_id, operacion),
    FOREIGN KEY (store_id, label_id)
        REFERENCES tiendanube_labels(store_id, label_id)
        ON DELETE CASCADE
);
ALTER TABLE IF EXISTS tiendanube_label_outbox
    ADD COLUMN IF NOT EXISTS payload_complete
        BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_tiendanube_label_outbox_pendiente
    ON tiendanube_label_outbox(estado, proximo_intento_en, id);

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
-- NULL histórico equivale a APROBADO (cargas viejas del admin). Cualquier
-- otro texto es un error de datos y no debe entrar al ledger.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'pagos'::regclass
          AND conname = 'ck_pagos_estado'
    ) THEN
        ALTER TABLE pagos ADD CONSTRAINT ck_pagos_estado
            CHECK (
                estado IS NULL
                OR estado IN ('PENDIENTE', 'APROBADO', 'RECHAZADO')
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE pagos VALIDATE CONSTRAINT ck_pagos_estado;
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
-- Estados contables del cargo. NC nunca es FC ni deuda pagable; se conserva
-- como valor histórico legible. NOT VALID + VALIDATE: si una instalación
-- trae un valor desconocido, el arranque falla con el nombre del constraint
-- (ver scripts/preflight_estados_contables.sql) en vez de operar a ciegas.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'envios'::regclass
          AND conname = 'ck_envios_estado'
    ) THEN
        ALTER TABLE envios ADD CONSTRAINT ck_envios_estado
            CHECK (estado IN ('ACTIVO', 'CANCELADO', 'NC')) NOT VALID;
    END IF;
END $$;
ALTER TABLE envios VALIDATE CONSTRAINT ck_envios_estado;
-- ── Legado de facturación por cargo: sólo lectura ───────────
-- nro_fc, factura_pdf y factura_nombre documentaban una FC por cargo. Con
-- facturas_cliente ese modelo quedó cerrado: la historia se conserva y se
-- sigue mostrando (portal y admin), pero ninguna escritura nueva puede crear
-- ni modificar una FC legacy. Un cargo nuevo nace con nro_fc NULL o ''
-- (pendiente de facturar por lote). No se borra ninguna fila ni columna.
CREATE OR REPLACE FUNCTION tauro_proteger_fc_legacy_envios()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NULLIF(BTRIM(NEW.nro_fc), '') IS NOT NULL
           OR NEW.factura_pdf IS NOT NULL
           OR NULLIF(BTRIM(NEW.factura_nombre), '') IS NOT NULL THEN
            RAISE EXCEPTION
                'La factura por cargo (envios.nro_fc) es legado de sólo lectura; usá facturas_cliente';
        END IF;
    ELSIF NEW.nro_fc IS DISTINCT FROM OLD.nro_fc
       OR NEW.factura_pdf IS DISTINCT FROM OLD.factura_pdf
       OR NEW.factura_nombre IS DISTINCT FROM OLD.factura_nombre THEN
        RAISE EXCEPTION
            'La factura por cargo (envios.nro_fc) es legado de sólo lectura; usá facturas_cliente';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_proteger_fc_legacy_envios ON envios;
CREATE TRIGGER trg_proteger_fc_legacy_envios
BEFORE INSERT OR UPDATE ON envios
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_fc_legacy_envios();
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
-- El detalle mensual del ADMIN filtra por cliente y un rango semiabierto de
-- fechas; este orden evita recorrer toda la historia del cliente cada vez.
CREATE INDEX IF NOT EXISTS idx_envios_cliente_fecha
    ON envios(cliente_id, fecha DESC, id DESC);
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

-- Cotizaciones comerciales que un reseller descarga con su precio. Sólo
-- persiste datos visibles: jamás costo courier, dólar ni margen TAURO.
CREATE TABLE IF NOT EXISTS cotizaciones_reseller (
    quote_id                TEXT PRIMARY KEY,
    cliente_id              TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    ruta                    TEXT NOT NULL,
    bultos                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    peso_facturable_kg      NUMERIC(12,3) NOT NULL,
    tiempo_estimado         TEXT NOT NULL,
    precio_base_ars         NUMERIC(14,2) NOT NULL,
    courier                 TEXT NOT NULL,
    servicio                TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    vigente_hasta           TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cotizaciones_reseller_cliente_vigencia
    ON cotizaciones_reseller(cliente_id, vigente_hasta DESC);

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
    asegurar_carga           BOOLEAN NOT NULL DEFAULT FALSE,
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
    visible_cliente          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_solicitudes_guia_cliente
    ON solicitudes_guia(cliente_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_solicitudes_guia_estado
    ON solicitudes_guia(estado, created_at DESC);
-- Máquina de estados operativa única. Las consultas críticas usan
-- NOT IN ('EMITIENDO','VERIFICAR_COURIER') y fallarían abiertas ante un
-- estado desconocido; el CHECK lo impide. NOT VALID + VALIDATE separa el
-- alta del control de historia: si una instalación trae un valor fuera de la
-- lista, el arranque falla nombrando el constraint y el preflight
-- (scripts/preflight_estados_contables.sql) muestra las filas a corregir.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'solicitudes_guia'::regclass
          AND conname = 'ck_solicitudes_guia_estado'
    ) THEN
        ALTER TABLE solicitudes_guia
            ADD CONSTRAINT ck_solicitudes_guia_estado
            CHECK (estado IN (
                'SOLICITADO', 'EN_PROCESO', 'EMITIENDO',
                'VERIFICAR_COURIER', 'GUIA_LISTA', 'DESPACHADO',
                'ENTREGADO', 'REEMPLAZADO', 'CANCELADO'
            )) NOT VALID;
    END IF;
END $$;
ALTER TABLE solicitudes_guia VALIDATE CONSTRAINT ck_solicitudes_guia_estado;
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
-- Primera entrega efectiva del PDF al cliente. Las descargas posteriores
-- siguen habilitadas, pero ya no alimentan recordatorios ni contadores.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS guia_descargada_at TIMESTAMPTZ;
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
--    valor_declarado_caja_usd, valor_unitario_usd, hs_code, descripcion_en}
-- cantidad = cajas IDÉNTICAS de ese producto (cada caja viaja como pieza
-- con su propio label). Los campos legacy (producto_alias, cantidad,
-- peso_kg, ...) guardan el primer bulto + totales para retrocompat.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS bultos JSONB;
-- Quién paga los impuestos de destino EN ESTE ENVÍO. Se copia del default
-- del cliente al crear la solicitud y puede pisarse desde el wizard. Queda
-- guardado porque define el incoterm de la guía: si el cliente cambia su
-- default mañana, los envíos ya despachados no pueden cambiar de manos.
ALTER TABLE IF EXISTS solicitudes_guia ADD COLUMN IF NOT EXISTS tax_paga TEXT;
-- Protección elegida para ESTE envío. En DHL se cotiza y emite como el
-- servicio adicional II, por el valor declarado total de todas las cajas.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS asegurar_carga BOOLEAN NOT NULL DEFAULT FALSE;
-- Casos de QA/históricos que se preservan sin mostrarlos al cliente ni
-- mezclarlos con la operación real.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT FALSE;
-- Visibilidad comercial reversible. Permite retirar del portal una carga de
-- demostración o un registro operativo sin borrarlo, cancelarlo ni alterar su
-- cargo, factura, pago o trazabilidad interna.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS visible_cliente BOOLEAN NOT NULL DEFAULT TRUE;
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

-- Snapshot de rastreo que consume el portal. Se mantiene separado del estado
-- operativo de la solicitud: el job diario consulta únicamente guías DHL
-- pendientes y una entrega confirmada queda excluida definitivamente.
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_estado TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_estado_courier TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_descripcion TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_consultado_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_actualizado_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_finalizado_at TIMESTAMPTZ;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_error TEXT;
ALTER TABLE IF EXISTS solicitudes_guia
    ADD COLUMN IF NOT EXISTS tracking_error_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'solicitudes_guia'::regclass
          AND conname = 'ck_solicitudes_tracking_estado'
    ) THEN
        ALTER TABLE solicitudes_guia
            ADD CONSTRAINT ck_solicitudes_tracking_estado
            CHECK (
                tracking_estado IS NULL
                OR tracking_estado IN (
                    'PROCESO_ENTREGA', 'ENTREGADO', 'RETENIDO'
                )
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_solicitudes_tracking_dhl_pendiente
    ON solicitudes_guia (tracking_consultado_at ASC NULLS FIRST, id)
    WHERE UPPER(courier) = 'DHL'
      AND tracking IS NOT NULL AND BTRIM(tracking) <> ''
      AND estado NOT IN ('CANCELADO', 'ENTREGADO')
      AND estado <> 'REEMPLAZADO'
      AND (tracking_estado IS NULL OR tracking_estado <> 'ENTREGADO');

-- ── Corrección, reemisión y cancelación de guías DHL ────────
-- Una guía emitida no se modifica dentro de MyDHL. TAURO conserva la guía
-- anterior como historia y, si se corrige, crea una solicitud nueva. Una
-- cancelación no crea reemplazo: conserva el tracking descartado para el
-- mismo control de riesgo. Ninguna operación borra la fila histórica.
CREATE TABLE IF NOT EXISTS solicitudes_guia_reemisiones (
    id                       SERIAL PRIMARY KEY,
    cliente_id               TEXT NOT NULL REFERENCES clientes(cliente_id) ON DELETE CASCADE,
    solicitud_anterior_id    INTEGER NOT NULL UNIQUE
                               REFERENCES solicitudes_guia(id) ON DELETE CASCADE,
    solicitud_nueva_id       INTEGER UNIQUE
                               REFERENCES solicitudes_guia(id) ON DELETE CASCADE,
    -- REEMPLAZO enlaza una guía nueva; CANCELACION conserva únicamente la
    -- anterior para controlar a los 7 días que nadie haya usado la etiqueta.
    operacion                 TEXT NOT NULL DEFAULT 'REEMPLAZO',
    tracking_anterior        TEXT NOT NULL,
    tracking_nuevo           TEXT,
    campos_modificados       JSONB NOT NULL DEFAULT '[]'::jsonb,
    motivo                   TEXT,
    estado                   TEXT NOT NULL DEFAULT 'PENDIENTE',
    -- Riesgo independiente del estado de la reemisión: la guía anterior
    -- puede seguir siendo físicamente utilizable aunque TAURO la descarte.
    -- Se controla una sola vez a los 7 días. Sin eventos queda confirmada;
    -- con movimiento abre alerta. Nunca se reactiva su cargo automáticamente.
    riesgo_estado            TEXT NOT NULL DEFAULT 'VIGILAR',
    tracking_anterior_consultado_at TIMESTAMPTZ,
    tracking_anterior_estado_courier TEXT,
    tracking_anterior_descripcion TEXT,
    tracking_anterior_evento_fecha TEXT,
    tracking_anterior_actualizado_at TIMESTAMPTZ,
    tracking_anterior_error  TEXT,
    tracking_anterior_error_at TIMESTAMPTZ,
    alerta_movimiento_at     TIMESTAMPTZ,
    riesgo_resuelto_at       TIMESTAMPTZ,
    riesgo_resuelto_nota     TEXT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at             TIMESTAMPTZ,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_solicitudes_guia_reemisiones_distintas
        CHECK (solicitud_anterior_id <> solicitud_nueva_id),
    CONSTRAINT ck_solicitudes_guia_reemisiones_estado
        CHECK (estado IN ('PENDIENTE', 'EMITIDA', 'VERIFICAR_COURIER')),
    CONSTRAINT ck_solicitudes_guia_reemisiones_operacion
        CHECK (operacion IN ('REEMPLAZO', 'CANCELACION')),
    CONSTRAINT ck_solicitudes_guia_reemisiones_riesgo
        CHECK (riesgo_estado IN (
            'VIGILAR', 'ALERTA_MOVIMIENTO', 'CERRADA'
        ))
);
-- Compatibilidad con bases que ya recibieron la primera versión de la tabla.
ALTER TABLE solicitudes_guia_reemisiones
    ALTER COLUMN solicitud_nueva_id DROP NOT NULL;
ALTER TABLE solicitudes_guia_reemisiones
    ADD COLUMN IF NOT EXISTS operacion TEXT NOT NULL DEFAULT 'REEMPLAZO',
    ADD COLUMN IF NOT EXISTS riesgo_estado TEXT NOT NULL DEFAULT 'VIGILAR',
    ADD COLUMN IF NOT EXISTS tracking_anterior_consultado_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS tracking_anterior_estado_courier TEXT,
    ADD COLUMN IF NOT EXISTS tracking_anterior_descripcion TEXT,
    ADD COLUMN IF NOT EXISTS tracking_anterior_evento_fecha TEXT,
    ADD COLUMN IF NOT EXISTS tracking_anterior_actualizado_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS tracking_anterior_error TEXT,
    ADD COLUMN IF NOT EXISTS tracking_anterior_error_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS alerta_movimiento_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS riesgo_resuelto_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS riesgo_resuelto_nota TEXT;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_solicitudes_guia_reemisiones_riesgo'
          AND conrelid = 'solicitudes_guia_reemisiones'::regclass
    ) THEN
        ALTER TABLE solicitudes_guia_reemisiones
            ADD CONSTRAINT ck_solicitudes_guia_reemisiones_riesgo
            CHECK (riesgo_estado IN (
                'VIGILAR', 'ALERTA_MOVIMIENTO', 'CERRADA'
            ));
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_solicitudes_guia_reemisiones_operacion'
          AND conrelid = 'solicitudes_guia_reemisiones'::regclass
    ) THEN
        ALTER TABLE solicitudes_guia_reemisiones
            ADD CONSTRAINT ck_solicitudes_guia_reemisiones_operacion
            CHECK (operacion IN ('REEMPLAZO', 'CANCELACION'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_reemisiones_cliente_fecha
    ON solicitudes_guia_reemisiones (cliente_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reemisiones_estado
    ON solicitudes_guia_reemisiones (estado, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_reemisiones_riesgo_tracking
    ON solicitudes_guia_reemisiones (
        riesgo_estado, completed_at ASC NULLS FIRST, id
    )
    WHERE estado = 'EMITIDA' AND riesgo_estado = 'VIGILAR';

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

-- ── Conciliación de facturas de couriers ──────────────────
-- Fuente de verdad financiera para DHL, FedEx, Andreani y OCA. El Excel o
-- Google Sheet puede funcionar como bandeja operativa, pero nunca reemplaza
-- estos snapshots ni el historial de aprobación del portal.
--
-- 1) Congela el precio aceptado y el margen al crear la guía. Esta fila es
--    inmutable: un cambio posterior de markup no puede reescribir la venta.
CREATE TABLE IF NOT EXISTS envio_cotizacion_snapshots (
    id                           BIGSERIAL PRIMARY KEY,
    solicitud_id                 INTEGER NOT NULL UNIQUE
        REFERENCES solicitudes_guia(id) ON DELETE RESTRICT,
    coti_id                      TEXT,
    courier                      TEXT NOT NULL,
    servicio_courier             TEXT,
    moneda_courier               TEXT NOT NULL,
    tipo_cambio_ars              NUMERIC(18,6) NOT NULL,
    costo_courier_estimado       NUMERIC(18,4) NOT NULL,
    costo_courier_estimado_ars   NUMERIC(18,4) NOT NULL,
    precio_cliente_inicial_ars   NUMERIC(18,4) NOT NULL,
    margen_tauro_protegido_ars   NUMERIC(18,4) NOT NULL,
    markup_tipo                  TEXT,
    markup_valor                 NUMERIC(18,4),
    peso_real_cotizado_kg        NUMERIC(12,3),
    peso_volumetrico_cotizado_kg NUMERIC(12,3),
    peso_facturable_cotizado_kg  NUMERIC(12,3),
    bultos                       JSONB NOT NULL DEFAULT '[]'::jsonb,
    origen_calculo               JSONB NOT NULL DEFAULT '{}'::jsonb,
    aceptado_at                  TIMESTAMPTZ NOT NULL,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_snapshot_courier CHECK (
        courier IN ('DHL','FEDEX','ANDREANI','OCA')
    ),
    CONSTRAINT ck_snapshot_moneda CHECK (
        moneda_courier ~ '^[A-Z]{3}$'
    ),
    CONSTRAINT ck_snapshot_importes CHECK (
        tipo_cambio_ars > 0
        AND costo_courier_estimado >= 0
        AND costo_courier_estimado_ars >= 0
        AND precio_cliente_inicial_ars >= 0
        AND margen_tauro_protegido_ars >= 0
    ),
    CONSTRAINT ck_snapshot_conversion CHECK (
        ABS(
            costo_courier_estimado_ars
            - costo_courier_estimado * tipo_cambio_ars
        ) <= 0.02
    ),
    CONSTRAINT ck_snapshot_margen CHECK (
        ABS(
            precio_cliente_inicial_ars
            - costo_courier_estimado_ars
            - margen_tauro_protegido_ars
        ) <= 0.02
    ),
    CONSTRAINT ck_snapshot_pesos CHECK (
        COALESCE(peso_real_cotizado_kg, 0) >= 0
        AND COALESCE(peso_volumetrico_cotizado_kg, 0) >= 0
        AND COALESCE(peso_facturable_cotizado_kg, 0) >= 0
    )
);
CREATE INDEX IF NOT EXISTS ix_snapshot_courier_fecha
    ON envio_cotizacion_snapshots (UPPER(courier), aceptado_at DESC);

CREATE OR REPLACE FUNCTION tauro_validar_snapshot_cotizacion()
RETURNS TRIGGER AS $$
DECLARE
    solicitud_actual RECORD;
BEGIN
    SELECT courier, coti_id, precio_tauro_ars
      INTO solicitud_actual
      FROM solicitudes_guia
     WHERE id = NEW.solicitud_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Solicitud de guía inexistente: %', NEW.solicitud_id;
    END IF;
    IF UPPER(BTRIM(solicitud_actual.courier)) <> NEW.courier THEN
        RAISE EXCEPTION 'El courier del snapshot no coincide con la solicitud';
    END IF;
    IF solicitud_actual.precio_tauro_ars IS NOT NULL
       AND ABS(solicitud_actual.precio_tauro_ars::NUMERIC
            - NEW.precio_cliente_inicial_ars) > 0.02 THEN
        RAISE EXCEPTION 'El precio del snapshot no coincide con la solicitud';
    END IF;
    IF NULLIF(BTRIM(solicitud_actual.coti_id), '') IS NOT NULL
       AND NULLIF(BTRIM(NEW.coti_id), '') IS NOT NULL
       AND BTRIM(solicitud_actual.coti_id) <> BTRIM(NEW.coti_id) THEN
        RAISE EXCEPTION 'La cotización del snapshot no coincide con la solicitud';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_snapshot_cotizacion
    ON envio_cotizacion_snapshots;
CREATE TRIGGER trg_validar_snapshot_cotizacion
BEFORE INSERT ON envio_cotizacion_snapshots
FOR EACH ROW EXECUTE FUNCTION tauro_validar_snapshot_cotizacion();

-- 2) Documento recibido. El número normalizado evita duplicar una factura
--    por guiones, espacios o mayúsculas. NC nunca se guarda como FC ni como
--    deuda: su signo contable se aplica al calcular la conciliación.
CREATE TABLE IF NOT EXISTS facturas_courier (
    id                       BIGSERIAL PRIMARY KEY,
    courier                  TEXT NOT NULL,
    tipo_documento           TEXT NOT NULL,
    numero                   TEXT NOT NULL,
    numero_normalizado       TEXT GENERATED ALWAYS AS (
        REGEXP_REPLACE(UPPER(BTRIM(numero)), '[^A-Z0-9]', '', 'g')
    ) STORED,
    factura_referenciada_id  BIGINT REFERENCES facturas_courier(id)
        ON DELETE RESTRICT,
    fecha_emision            DATE,
    fecha_vencimiento        DATE,
    periodo_desde            DATE,
    periodo_hasta            DATE,
    moneda                   TEXT NOT NULL,
    subtotal                 NUMERIC(18,4) NOT NULL DEFAULT 0,
    impuestos                NUMERIC(18,4) NOT NULL DEFAULT 0,
    total                    NUMERIC(18,4) NOT NULL,
    estado                   TEXT NOT NULL DEFAULT 'RECIBIDA',
    mensaje_origen_id        TEXT,
    evidencia_uri            TEXT,
    archivo_nombre           TEXT,
    archivo_sha256           TEXT,
    archivo_pdf              BYTEA,
    archivo_mime             TEXT,
    metadatos_origen         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_factura_courier CHECK (
        courier IN ('DHL','FEDEX','ANDREANI','OCA')
    ),
    CONSTRAINT ck_factura_tipo CHECK (
        tipo_documento IN ('FC','NC','ND')
    ),
    CONSTRAINT ck_factura_numero CHECK (
        REGEXP_REPLACE(UPPER(BTRIM(numero)), '[^A-Z0-9]', '', 'g') <> ''
    ),
    CONSTRAINT ck_factura_moneda CHECK (moneda ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_factura_importes CHECK (
        subtotal >= 0 AND impuestos >= 0 AND total >= 0
    ),
    CONSTRAINT ck_factura_fechas CHECK (
        (fecha_vencimiento IS NULL OR fecha_emision IS NULL
            OR fecha_vencimiento >= fecha_emision)
        AND (periodo_hasta IS NULL OR periodo_desde IS NULL
            OR periodo_hasta >= periodo_desde)
    ),
    CONSTRAINT ck_factura_estado CHECK (
        estado IN (
            'RECIBIDA','EXTRAIDA','PARCIAL','CONCILIADA',
            'OBSERVADA','CERRADA','ANULADA'
        )
    ),
    CONSTRAINT ck_factura_hash CHECK (
        archivo_sha256 IS NULL OR archivo_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_factura_referencia_nc CHECK (
        tipo_documento <> 'NC' OR factura_referenciada_id IS NULL
            OR factura_referenciada_id <> id
    ),
    CONSTRAINT uq_factura_courier_documento UNIQUE (
        courier, tipo_documento, numero_normalizado
    )
);
ALTER TABLE IF EXISTS facturas_courier
    ADD COLUMN IF NOT EXISTS archivo_pdf BYTEA;
ALTER TABLE IF EXISTS facturas_courier
    ADD COLUMN IF NOT EXISTS archivo_mime TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_courier_archivo
    ON facturas_courier (archivo_sha256)
    WHERE archivo_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_factura_courier_estado_fecha
    ON facturas_courier (UPPER(courier), estado, fecha_emision DESC);

-- 3) Una factura mensual puede traer muchas líneas y el mismo tracking puede
--    repetirse en FLETE, COMBUSTIBLE e IMPUESTOS. Se deduplica por número de
--    línea dentro del documento, no por tracking.
CREATE TABLE IF NOT EXISTS facturas_courier_items (
    id                   BIGSERIAL PRIMARY KEY,
    factura_id           BIGINT NOT NULL REFERENCES facturas_courier(id)
        ON DELETE RESTRICT,
    linea_numero         INTEGER NOT NULL,
    tracking_raw         TEXT,
    tracking_normalizado TEXT GENERATED ALWAYS AS (
        NULLIF(REGEXP_REPLACE(UPPER(BTRIM(tracking_raw)), '[^A-Z0-9]', '', 'g'), '')
    ) STORED,
    concepto_codigo      TEXT,
    concepto_tipo        TEXT NOT NULL DEFAULT 'OTRO',
    descripcion          TEXT,
    signo                SMALLINT NOT NULL DEFAULT 1,
    importe              NUMERIC(18,4) NOT NULL,
    moneda               TEXT NOT NULL,
    tipo_cambio_ars      NUMERIC(18,6) NOT NULL,
    importe_ars          NUMERIC(18,4) NOT NULL,
    fecha_envio          DATE,
    peso_real_kg         NUMERIC(12,3),
    peso_volumetrico_kg  NUMERIC(12,3),
    peso_facturado_kg    NUMERIC(12,3),
    peso_base            TEXT NOT NULL DEFAULT 'NO_INFORMADO',
    dimensiones          JSONB NOT NULL DEFAULT '[]'::jsonb,
    datos_crudos         JSONB NOT NULL DEFAULT '{}'::jsonb,
    parse_confianza      NUMERIC(5,4),
    estado               TEXT NOT NULL DEFAULT 'EXTRAIDO',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_factura_item_linea UNIQUE (factura_id, linea_numero),
    CONSTRAINT ck_factura_item_linea CHECK (linea_numero > 0),
    CONSTRAINT ck_factura_item_concepto CHECK (
        concepto_tipo IN (
            'FLETE','COMBUSTIBLE','IMPUESTO','ADUANA','MANEJO',
            'SEGURO','DESCUENTO','OTRO'
        )
    ),
    CONSTRAINT ck_factura_item_signo CHECK (signo IN (-1, 1)),
    CONSTRAINT ck_factura_item_moneda CHECK (moneda ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_factura_item_importes CHECK (
        importe > 0 AND tipo_cambio_ars > 0 AND importe_ars > 0
    ),
    CONSTRAINT ck_factura_item_conversion CHECK (
        ABS(importe_ars - importe * tipo_cambio_ars) <= 0.02
    ),
    CONSTRAINT ck_factura_item_pesos CHECK (
        COALESCE(peso_real_kg, 0) >= 0
        AND COALESCE(peso_volumetrico_kg, 0) >= 0
        AND COALESCE(peso_facturado_kg, 0) >= 0
    ),
    CONSTRAINT ck_factura_item_peso_base CHECK (
        peso_base IN (
            'REAL','VOLUMETRICO','DECLARADO','OTRO','NO_INFORMADO'
        )
    ),
    CONSTRAINT ck_factura_item_confianza CHECK (
        parse_confianza IS NULL OR parse_confianza BETWEEN 0 AND 1
    ),
    CONSTRAINT ck_factura_item_estado CHECK (
        estado IN ('EXTRAIDO','LISTO','OBSERVADO','CONCILIADO','IGNORADO')
    )
);
CREATE INDEX IF NOT EXISTS ix_factura_item_tracking
    ON facturas_courier_items (tracking_normalizado)
    WHERE tracking_normalizado IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_factura_item_factura_estado
    ON facturas_courier_items (factura_id, estado, linea_numero);

-- 4) El match puede ser propuesto automáticamente, pero confirmar una línea
--    o repartirla entre varios envíos es una decisión auditada. Los montos
--    asignados permiten prorratear líneas agrupadas sin perder el original.
CREATE TABLE IF NOT EXISTS factura_courier_item_matches (
    id                    BIGSERIAL PRIMARY KEY,
    item_id               BIGINT NOT NULL REFERENCES facturas_courier_items(id)
        ON DELETE RESTRICT,
    solicitud_id          INTEGER NOT NULL REFERENCES solicitudes_guia(id)
        ON DELETE RESTRICT,
    monto_asignado        NUMERIC(18,4) NOT NULL,
    monto_asignado_ars    NUMERIC(18,4) NOT NULL,
    metodo                TEXT NOT NULL,
    confianza             NUMERIC(5,4),
    estado                TEXT NOT NULL DEFAULT 'PROPUESTO',
    evidencia_uri         TEXT,
    creado_por            TEXT NOT NULL,
    confirmado_por        TEXT,
    confirmado_at         TIMESTAMPTZ,
    motivo_rechazo        TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_factura_item_match UNIQUE (item_id, solicitud_id),
    CONSTRAINT ck_factura_match_importes CHECK (
        monto_asignado > 0 AND monto_asignado_ars > 0
    ),
    CONSTRAINT ck_factura_match_metodo CHECK (
        metodo IN ('EXACTO_TRACKING','REFERENCIA','MANUAL')
    ),
    CONSTRAINT ck_factura_match_confianza CHECK (
        confianza IS NULL OR confianza BETWEEN 0 AND 1
    ),
    CONSTRAINT ck_factura_match_estado CHECK (
        estado IN ('PROPUESTO','CONFIRMADO','RECHAZADO')
    ),
    CONSTRAINT ck_factura_match_manual_evidencia CHECK (
        metodo <> 'MANUAL' OR NULLIF(BTRIM(evidencia_uri), '') IS NOT NULL
    ),
    CONSTRAINT ck_factura_match_confirmacion CHECK (
        estado <> 'CONFIRMADO'
        OR (NULLIF(BTRIM(confirmado_por), '') IS NOT NULL
            AND confirmado_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_factura_match_solicitud_estado
    ON factura_courier_item_matches (solicitud_id, estado);

CREATE OR REPLACE FUNCTION tauro_validar_match_factura_courier()
RETURNS TRIGGER AS $$
DECLARE
    item_actual RECORD;
    solicitud_actual RECORD;
    asignado NUMERIC(18,4);
    asignado_ars NUMERIC(18,4);
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.item_id IS DISTINCT FROM NEW.item_id
           OR OLD.solicitud_id IS DISTINCT FROM NEW.solicitud_id
           OR OLD.monto_asignado IS DISTINCT FROM NEW.monto_asignado
           OR OLD.monto_asignado_ars IS DISTINCT FROM NEW.monto_asignado_ars
           OR OLD.metodo IS DISTINCT FROM NEW.metodo THEN
            RAISE EXCEPTION 'La identidad y los importes del match son inmutables';
        END IF;
        IF OLD.estado = 'CONFIRMADO' AND NEW.estado <> 'CONFIRMADO' THEN
            RAISE EXCEPTION 'Un match confirmado no puede reabrirse';
        END IF;
        IF OLD.estado = 'RECHAZADO' AND NEW.estado <> 'RECHAZADO' THEN
            RAISE EXCEPTION 'Un match rechazado no puede reabrirse';
        END IF;
    END IF;

    SELECT i.importe, i.importe_ars, i.tipo_cambio_ars,
           i.tracking_normalizado, f.courier
      INTO item_actual
      FROM facturas_courier_items i
      JOIN facturas_courier f ON f.id = i.factura_id
     WHERE i.id = NEW.item_id
     FOR UPDATE OF i;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Ítem de factura inexistente: %', NEW.item_id;
    END IF;

    SELECT courier,
           NULLIF(REGEXP_REPLACE(
               UPPER(BTRIM(tracking)), '[^A-Z0-9]', '', 'g'
           ), '') AS tracking_normalizado
      INTO solicitud_actual
      FROM solicitudes_guia
     WHERE id = NEW.solicitud_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Solicitud de guía inexistente: %', NEW.solicitud_id;
    END IF;

    IF UPPER(BTRIM(solicitud_actual.courier))
       <> UPPER(BTRIM(item_actual.courier)) THEN
        RAISE EXCEPTION 'Courier de factura y solicitud no coinciden';
    END IF;

    IF NEW.metodo = 'EXACTO_TRACKING' AND (
        item_actual.tracking_normalizado IS NULL
        OR solicitud_actual.tracking_normalizado IS NULL
        OR item_actual.tracking_normalizado
            <> solicitud_actual.tracking_normalizado
    ) THEN
        RAISE EXCEPTION 'El tracking exacto no coincide';
    END IF;

    IF ABS(
        NEW.monto_asignado_ars
        - NEW.monto_asignado * item_actual.tipo_cambio_ars
    ) > 0.02 THEN
        RAISE EXCEPTION 'La conversión ARS del match no coincide';
    END IF;

    SELECT COALESCE(SUM(monto_asignado), 0),
           COALESCE(SUM(monto_asignado_ars), 0)
      INTO asignado, asignado_ars
      FROM factura_courier_item_matches
     WHERE item_id = NEW.item_id
       AND estado IN ('PROPUESTO','CONFIRMADO')
       AND id <> COALESCE(NEW.id, 0);

    IF NEW.estado IN ('PROPUESTO','CONFIRMADO') AND (
        asignado + NEW.monto_asignado > item_actual.importe + 0.02
        OR asignado_ars + NEW.monto_asignado_ars
            > item_actual.importe_ars + 0.02
    ) THEN
        RAISE EXCEPTION 'Los matches exceden el importe del ítem';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_match_factura_courier
    ON factura_courier_item_matches;
CREATE TRIGGER trg_validar_match_factura_courier
BEFORE INSERT OR UPDATE ON factura_courier_item_matches
FOR EACH ROW EXECUTE FUNCTION tauro_validar_match_factura_courier();

-- Una vez que una cabecera tiene líneas o una línea tiene un match, sus datos
-- financieros dejan de ser editables. Las correcciones se modelan con
-- anulación + nuevo documento/match, no reescribiendo evidencia histórica.
CREATE OR REPLACE FUNCTION tauro_proteger_factura_con_items()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM facturas_courier_items WHERE factura_id = OLD.id
    ) AND (
        OLD.courier IS DISTINCT FROM NEW.courier
        OR OLD.tipo_documento IS DISTINCT FROM NEW.tipo_documento
        OR OLD.numero IS DISTINCT FROM NEW.numero
        OR OLD.factura_referenciada_id IS DISTINCT FROM NEW.factura_referenciada_id
        OR OLD.moneda IS DISTINCT FROM NEW.moneda
        OR OLD.subtotal IS DISTINCT FROM NEW.subtotal
        OR OLD.impuestos IS DISTINCT FROM NEW.impuestos
        OR OLD.total IS DISTINCT FROM NEW.total
    ) THEN
        RAISE EXCEPTION 'La cabecera financiera con ítems es inmutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_proteger_factura_con_items ON facturas_courier;
CREATE TRIGGER trg_proteger_factura_con_items
BEFORE UPDATE ON facturas_courier
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_factura_con_items();

CREATE OR REPLACE FUNCTION tauro_proteger_item_matcheado()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM factura_courier_item_matches WHERE item_id = OLD.id
    ) AND (
        OLD.factura_id IS DISTINCT FROM NEW.factura_id
        OR OLD.linea_numero IS DISTINCT FROM NEW.linea_numero
        OR OLD.tracking_raw IS DISTINCT FROM NEW.tracking_raw
        OR OLD.concepto_codigo IS DISTINCT FROM NEW.concepto_codigo
        OR OLD.concepto_tipo IS DISTINCT FROM NEW.concepto_tipo
        OR OLD.signo IS DISTINCT FROM NEW.signo
        OR OLD.importe IS DISTINCT FROM NEW.importe
        OR OLD.moneda IS DISTINCT FROM NEW.moneda
        OR OLD.tipo_cambio_ars IS DISTINCT FROM NEW.tipo_cambio_ars
        OR OLD.importe_ars IS DISTINCT FROM NEW.importe_ars
        OR OLD.peso_real_kg IS DISTINCT FROM NEW.peso_real_kg
        OR OLD.peso_volumetrico_kg IS DISTINCT FROM NEW.peso_volumetrico_kg
        OR OLD.peso_facturado_kg IS DISTINCT FROM NEW.peso_facturado_kg
        OR OLD.peso_base IS DISTINCT FROM NEW.peso_base
    ) THEN
        RAISE EXCEPTION 'El ítem con match es inmutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_proteger_item_matcheado ON facturas_courier_items;
CREATE TRIGGER trg_proteger_item_matcheado
BEFORE UPDATE ON facturas_courier_items
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_item_matcheado();

-- 5) Resultado versionado del cálculo. El precio final siempre es costo real
--    + margen protegido; el ajuste es precio final - precio inicial.
CREATE TABLE IF NOT EXISTS conciliaciones_envio (
    id                           BIGSERIAL PRIMARY KEY,
    solicitud_id                 INTEGER NOT NULL REFERENCES solicitudes_guia(id)
        ON DELETE RESTRICT,
    version                      INTEGER NOT NULL,
    estado                       TEXT NOT NULL DEFAULT 'BORRADOR',
    precio_cliente_inicial_ars   NUMERIC(18,4) NOT NULL,
    costo_courier_estimado_ars   NUMERIC(18,4) NOT NULL,
    margen_tauro_protegido_ars   NUMERIC(18,4) NOT NULL,
    costo_courier_real_ars       NUMERIC(18,4) NOT NULL,
    precio_cliente_final_ars     NUMERIC(18,4) NOT NULL,
    ajuste_cliente_ars           NUMERIC(18,4) NOT NULL,
    diferencia_flete_ars         NUMERIC(18,4) NOT NULL DEFAULT 0,
    tax_cliente_ars              NUMERIC(18,4) NOT NULL DEFAULT 0,
    peso_cotizado_kg             NUMERIC(12,3),
    peso_real_facturado_kg       NUMERIC(12,3),
    peso_volumetrico_facturado_kg NUMERIC(12,3),
    peso_final_facturado_kg      NUMERIC(12,3),
    peso_base_facturado          TEXT NOT NULL DEFAULT 'NO_INFORMADO',
    motivo_diferencia            TEXT NOT NULL DEFAULT 'OTRO',
    formula_version              TEXT NOT NULL DEFAULT 'MARGEN_PROTEGIDO_V1',
    calculo_hash                 TEXT NOT NULL,
    evidencias                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidencia_completa           BOOLEAN NOT NULL DEFAULT FALSE,
    calculado_por                TEXT NOT NULL,
    calculado_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    aprobado_por                 TEXT,
    aprobado_at                  TIMESTAMPTZ,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_conciliacion_envio_version UNIQUE (solicitud_id, version),
    CONSTRAINT uq_conciliacion_calculo_hash UNIQUE (calculo_hash),
    CONSTRAINT ck_conciliacion_version CHECK (version > 0),
    CONSTRAINT ck_conciliacion_estado CHECK (
        estado IN (
            'BORRADOR','PARA_REVISION','APROBADA','RECLAMADA',
            'CERRADA','ANULADA'
        )
    ),
    CONSTRAINT ck_conciliacion_importes CHECK (
        precio_cliente_inicial_ars >= 0
        AND costo_courier_estimado_ars >= 0
        AND margen_tauro_protegido_ars >= 0
        AND precio_cliente_final_ars >= 0
    ),
    CONSTRAINT ck_conciliacion_formula_final CHECK (
        ABS(
            precio_cliente_final_ars
            - costo_courier_real_ars
            - margen_tauro_protegido_ars
        ) <= 0.02
    ),
    CONSTRAINT ck_conciliacion_formula_ajuste CHECK (
        ABS(
            ajuste_cliente_ars
            - precio_cliente_final_ars
            + precio_cliente_inicial_ars
        ) <= 0.02
    ),
    CONSTRAINT ck_conciliacion_componentes CHECK (
        ABS(
            ajuste_cliente_ars
            - diferencia_flete_ars
            - tax_cliente_ars
        ) <= 0.02
    ),
    CONSTRAINT ck_conciliacion_pesos CHECK (
        COALESCE(peso_cotizado_kg, 0) >= 0
        AND COALESCE(peso_real_facturado_kg, 0) >= 0
        AND COALESCE(peso_volumetrico_facturado_kg, 0) >= 0
        AND COALESCE(peso_final_facturado_kg, 0) >= 0
    ),
    CONSTRAINT ck_conciliacion_peso_base CHECK (
        peso_base_facturado IN (
            'REAL','VOLUMETRICO','DECLARADO','OTRO','NO_INFORMADO'
        )
    ),
    CONSTRAINT ck_conciliacion_motivo CHECK (
        motivo_diferencia IN (
            'PESO_REAL','PESO_VOLUMETRICO','RECARGO','IMPUESTOS',
            'DEVOLUCION','DESCUENTO','MIXTO','SIN_DIFERENCIA','OTRO'
        )
    ),
    CONSTRAINT ck_conciliacion_hash CHECK (
        calculo_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_conciliacion_aprobacion CHECK (
        estado NOT IN ('APROBADA','CERRADA')
        OR (NULLIF(BTRIM(aprobado_por), '') IS NOT NULL
            AND aprobado_at IS NOT NULL AND evidencia_completa)
    )
);
-- Las conciliaciones anteriores no distinguían el TAX del resto del ajuste.
-- Al migrarlas se conserva exactamente el total histórico: todo queda como
-- diferencia de flete y TAX en cero. Los cálculos nuevos ya guardan ambos
-- componentes por separado.
ALTER TABLE IF EXISTS conciliaciones_envio
    ADD COLUMN IF NOT EXISTS diferencia_flete_ars NUMERIC(18,4);
ALTER TABLE IF EXISTS conciliaciones_envio
    ADD COLUMN IF NOT EXISTS tax_cliente_ars NUMERIC(18,4);
UPDATE conciliaciones_envio
   SET diferencia_flete_ars = ajuste_cliente_ars
 WHERE diferencia_flete_ars IS NULL;
UPDATE conciliaciones_envio
   SET tax_cliente_ars = 0
 WHERE tax_cliente_ars IS NULL;
ALTER TABLE IF EXISTS conciliaciones_envio
    ALTER COLUMN diferencia_flete_ars SET DEFAULT 0,
    ALTER COLUMN diferencia_flete_ars SET NOT NULL,
    ALTER COLUMN tax_cliente_ars SET DEFAULT 0,
    ALTER COLUMN tax_cliente_ars SET NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'conciliaciones_envio'::regclass
           AND conname = 'ck_conciliacion_componentes'
    ) THEN
        ALTER TABLE conciliaciones_envio
            ADD CONSTRAINT ck_conciliacion_componentes CHECK (
                ABS(
                    ajuste_cliente_ars
                    - diferencia_flete_ars
                    - tax_cliente_ars
                ) <= 0.02
            ) NOT VALID;
    END IF;
END $$;
ALTER TABLE conciliaciones_envio
    VALIDATE CONSTRAINT ck_conciliacion_componentes;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conciliacion_envio_activa
    ON conciliaciones_envio (solicitud_id)
    WHERE estado IN ('BORRADOR','PARA_REVISION','APROBADA','RECLAMADA');
CREATE INDEX IF NOT EXISTS ix_conciliacion_estado_fecha
    ON conciliaciones_envio (estado, calculado_at DESC);

CREATE OR REPLACE FUNCTION tauro_proteger_calculo_conciliacion()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.solicitud_id IS DISTINCT FROM NEW.solicitud_id
       OR OLD.version IS DISTINCT FROM NEW.version
       OR OLD.precio_cliente_inicial_ars IS DISTINCT FROM NEW.precio_cliente_inicial_ars
       OR OLD.costo_courier_estimado_ars IS DISTINCT FROM NEW.costo_courier_estimado_ars
       OR OLD.margen_tauro_protegido_ars IS DISTINCT FROM NEW.margen_tauro_protegido_ars
       OR OLD.costo_courier_real_ars IS DISTINCT FROM NEW.costo_courier_real_ars
       OR OLD.precio_cliente_final_ars IS DISTINCT FROM NEW.precio_cliente_final_ars
       OR OLD.ajuste_cliente_ars IS DISTINCT FROM NEW.ajuste_cliente_ars
       OR OLD.diferencia_flete_ars IS DISTINCT FROM NEW.diferencia_flete_ars
       OR OLD.tax_cliente_ars IS DISTINCT FROM NEW.tax_cliente_ars
       OR OLD.peso_cotizado_kg IS DISTINCT FROM NEW.peso_cotizado_kg
       OR OLD.peso_real_facturado_kg IS DISTINCT FROM NEW.peso_real_facturado_kg
       OR OLD.peso_volumetrico_facturado_kg IS DISTINCT FROM NEW.peso_volumetrico_facturado_kg
       OR OLD.peso_final_facturado_kg IS DISTINCT FROM NEW.peso_final_facturado_kg
       OR OLD.peso_base_facturado IS DISTINCT FROM NEW.peso_base_facturado
       OR OLD.motivo_diferencia IS DISTINCT FROM NEW.motivo_diferencia
       OR OLD.formula_version IS DISTINCT FROM NEW.formula_version
       OR OLD.calculo_hash IS DISTINCT FROM NEW.calculo_hash
       OR OLD.evidencias IS DISTINCT FROM NEW.evidencias
       OR OLD.calculado_por IS DISTINCT FROM NEW.calculado_por
       OR OLD.calculado_at IS DISTINCT FROM NEW.calculado_at THEN
        RAISE EXCEPTION 'El cálculo de conciliación es inmutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_proteger_calculo_conciliacion
    ON conciliaciones_envio;
CREATE TRIGGER trg_proteger_calculo_conciliacion
BEFORE UPDATE ON conciliaciones_envio
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_calculo_conciliacion();

-- 6) El cálculo sólo propone un débito o crédito. Aplicarlo al saldo exige
--    aprobación explícita y queda identificado por una clave idempotente.
CREATE TABLE IF NOT EXISTS ajustes_cliente (
    id                    BIGSERIAL PRIMARY KEY,
    conciliacion_id       BIGINT NOT NULL UNIQUE
        REFERENCES conciliaciones_envio(id) ON DELETE RESTRICT,
    solicitud_id          INTEGER NOT NULL REFERENCES solicitudes_guia(id)
        ON DELETE RESTRICT,
    tipo                  TEXT NOT NULL,
    monto_ars             NUMERIC(18,4) NOT NULL,
    precio_anterior_ars   NUMERIC(18,4) NOT NULL,
    precio_nuevo_ars      NUMERIC(18,4) NOT NULL,
    estado                TEXT NOT NULL DEFAULT 'PROPUESTO',
    idempotency_key       TEXT NOT NULL UNIQUE,
    motivo                TEXT,
    propuesto_por         TEXT NOT NULL,
    aprobado_por          TEXT,
    aprobado_at           TIMESTAMPTZ,
    aplicado_por          TEXT,
    aplicado_at           TIMESTAMPTZ,
    referencia_aplicacion TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ajuste_tipo_monto CHECK (
        (tipo = 'DEBITO' AND monto_ars > 0)
        OR (tipo = 'CREDITO' AND monto_ars < 0)
    ),
    CONSTRAINT ck_ajuste_formula CHECK (
        ABS(monto_ars - precio_nuevo_ars + precio_anterior_ars) <= 0.02
    ),
    CONSTRAINT ck_ajuste_precios CHECK (
        precio_anterior_ars >= 0 AND precio_nuevo_ars >= 0
    ),
    CONSTRAINT ck_ajuste_estado CHECK (
        estado IN ('PROPUESTO','APROBADO','APLICADO','ANULADO')
    ),
    CONSTRAINT ck_ajuste_aprobado CHECK (
        estado NOT IN ('APROBADO','APLICADO')
        OR (NULLIF(BTRIM(aprobado_por), '') IS NOT NULL
            AND aprobado_at IS NOT NULL)
    ),
    CONSTRAINT ck_ajuste_aplicado CHECK (
        estado <> 'APLICADO'
        OR (NULLIF(BTRIM(aplicado_por), '') IS NOT NULL
            AND aplicado_at IS NOT NULL
            AND NULLIF(BTRIM(referencia_aplicacion), '') IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_ajuste_cliente_estado_fecha
    ON ajustes_cliente (estado, created_at DESC);

CREATE OR REPLACE FUNCTION tauro_validar_ajuste_cliente()
RETURNS TRIGGER AS $$
DECLARE
    conciliacion_actual RECORD;
    precio_previo NUMERIC(18,4);
BEGIN
    IF TG_OP = 'UPDATE' AND (
        OLD.conciliacion_id IS DISTINCT FROM NEW.conciliacion_id
        OR OLD.solicitud_id IS DISTINCT FROM NEW.solicitud_id
        OR OLD.tipo IS DISTINCT FROM NEW.tipo
        OR OLD.monto_ars IS DISTINCT FROM NEW.monto_ars
        OR OLD.precio_anterior_ars IS DISTINCT FROM NEW.precio_anterior_ars
        OR OLD.precio_nuevo_ars IS DISTINCT FROM NEW.precio_nuevo_ars
        OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
    ) THEN
        RAISE EXCEPTION 'Los importes y la identidad del ajuste son inmutables';
    END IF;

    SELECT solicitud_id, estado, precio_cliente_inicial_ars,
           precio_cliente_final_ars, ajuste_cliente_ars
      INTO conciliacion_actual
      FROM conciliaciones_envio
     WHERE id = NEW.conciliacion_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conciliación inexistente: %', NEW.conciliacion_id;
    END IF;
    SELECT COALESCE((
               SELECT anterior.precio_cliente_final_ars
                 FROM conciliaciones_envio anterior
                WHERE anterior.solicitud_id = conciliacion_actual.solicitud_id
                  AND anterior.version < (
                      SELECT version FROM conciliaciones_envio
                       WHERE id = NEW.conciliacion_id
                  )
                  AND anterior.estado = 'CERRADA'
                ORDER BY anterior.version DESC
                LIMIT 1
           ), conciliacion_actual.precio_cliente_inicial_ars)
      INTO precio_previo;
    IF conciliacion_actual.solicitud_id <> NEW.solicitud_id
       OR ABS(precio_previo
            - NEW.precio_anterior_ars) > 0.02
       OR ABS(conciliacion_actual.precio_cliente_final_ars
            - NEW.precio_nuevo_ars) > 0.02
       OR ABS(NEW.precio_nuevo_ars - NEW.precio_anterior_ars
            - NEW.monto_ars) > 0.02 THEN
        RAISE EXCEPTION 'El ajuste no coincide con la conciliación';
    END IF;
    IF NEW.estado IN ('APROBADO','APLICADO')
       AND conciliacion_actual.estado NOT IN ('APROBADA','CERRADA') THEN
        RAISE EXCEPTION 'La conciliación debe estar aprobada antes del ajuste';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_ajuste_cliente ON ajustes_cliente;
CREATE TRIGGER trg_validar_ajuste_cliente
BEFORE INSERT OR UPDATE ON ajustes_cliente
FOR EACH ROW EXECUTE FUNCTION tauro_validar_ajuste_cliente();

-- ── Facturación TAURO a clientes ─────────────────────────
-- La factura documenta cargos/ajustes que ya existen en la cuenta corriente;
-- no vuelve a debitarlos. Los campos de factura que todavía viven en envios
-- quedan como legado de sólo lectura para instalaciones históricas.
CREATE TABLE IF NOT EXISTS facturas_cliente (
    id                BIGSERIAL PRIMARY KEY,
    cliente_id        TEXT NOT NULL REFERENCES clientes(cliente_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    tipo              TEXT NOT NULL,
    punto_venta       INTEGER NOT NULL,
    numero            BIGINT NOT NULL,
    cae               TEXT,
    fecha_emision     DATE NOT NULL,
    fecha_vencimiento DATE,
    periodo_desde     DATE,
    periodo_hasta     DATE,
    subtotal          NUMERIC(14,2) NOT NULL,
    iva               NUMERIC(14,2) NOT NULL DEFAULT 0,
    total             NUMERIC(14,2) NOT NULL,
    pdf               BYTEA NOT NULL,
    pdf_nombre        TEXT,
    estado            TEXT NOT NULL DEFAULT 'EMITIDA',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        TEXT NOT NULL,
    CONSTRAINT uq_factura_cliente_numero
        UNIQUE (tipo, punto_venta, numero),
    CONSTRAINT ck_factura_cliente_tipo CHECK (tipo IN ('FC','NC')),
    CONSTRAINT ck_factura_cliente_estado CHECK (estado IN ('EMITIDA','ANULADA')),
    CONSTRAINT ck_factura_cliente_numero CHECK (punto_venta > 0 AND numero > 0),
    CONSTRAINT ck_factura_cliente_importes CHECK (
        subtotal >= 0 AND iva >= 0 AND total > 0
        AND ABS(total - subtotal - iva) <= 0.02
    ),
    CONSTRAINT ck_factura_cliente_fechas CHECK (
        (fecha_vencimiento IS NULL OR fecha_vencimiento >= fecha_emision)
        AND (periodo_desde IS NULL OR periodo_hasta IS NULL
             OR periodo_hasta >= periodo_desde)
    )
);
CREATE INDEX IF NOT EXISTS ix_facturas_cliente_cliente_fecha
    ON facturas_cliente (cliente_id, fecha_emision DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_facturas_cliente_estado_vencimiento
    ON facturas_cliente (estado, fecha_vencimiento);

CREATE TABLE IF NOT EXISTS facturas_cliente_items (
    id          BIGSERIAL PRIMARY KEY,
    factura_id  BIGINT NOT NULL REFERENCES facturas_cliente(id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    envio_id    INTEGER REFERENCES envios(id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    ajuste_id   BIGINT REFERENCES ajustes_cliente(id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    descripcion TEXT NOT NULL,
    monto       NUMERIC(14,2) NOT NULL,
    CONSTRAINT ck_factura_cliente_item_objetivo CHECK (
        NUM_NONNULLS(envio_id, ajuste_id) = 1
    ),
    CONSTRAINT ck_factura_cliente_item_monto CHECK (monto > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_cliente_item_envio
    ON facturas_cliente_items (factura_id, envio_id)
    WHERE envio_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_factura_cliente_item_ajuste
    ON facturas_cliente_items (factura_id, ajuste_id)
    WHERE ajuste_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_factura_cliente_items_factura
    ON facturas_cliente_items (factura_id, id);

-- Serializa por el cargo/ajuste de destino. Además de ownership y ámbito,
-- evita que dos facturas EMITIDAS concurrentes documenten la misma partida.
CREATE OR REPLACE FUNCTION tauro_validar_factura_cliente_item()
RETURNS TRIGGER AS $$
DECLARE
    factura_actual RECORD;
    objetivo RECORD;
    ambito_objetivo TEXT;
BEGIN
    SELECT id, cliente_id, tipo, estado
      INTO factura_actual
      FROM facturas_cliente
     WHERE id = NEW.factura_id
     FOR UPDATE;
    IF NOT FOUND OR factura_actual.estado <> 'EMITIDA' THEN
        RAISE EXCEPTION 'La factura cliente no existe o no está emitida';
    END IF;

    IF NEW.envio_id IS NOT NULL THEN
        SELECT id, cliente_id, estado, ambito, nro_fc
          INTO objetivo
          FROM envios
         WHERE id = NEW.envio_id
         FOR UPDATE;
        IF NOT FOUND OR objetivo.cliente_id <> factura_actual.cliente_id
           OR objetivo.estado <> 'ACTIVO' THEN
            RAISE EXCEPTION 'El cargo no pertenece al cliente o no está activo';
        END IF;
        IF NULLIF(BTRIM(objetivo.nro_fc), '') IS NOT NULL THEN
            RAISE EXCEPTION 'El cargo ya tiene una factura legacy';
        END IF;
        IF factura_actual.tipo <> 'FC' THEN
            RAISE EXCEPTION 'Un cargo de envío sólo puede integrar una FC';
        END IF;
        ambito_objetivo := objetivo.ambito;
        IF EXISTS (
            SELECT 1
              FROM facturas_cliente_items otro
              JOIN facturas_cliente f ON f.id = otro.factura_id
             WHERE otro.envio_id = NEW.envio_id
               AND otro.id <> COALESCE(NEW.id, -1)
               AND f.estado = 'EMITIDA'
        ) THEN
            RAISE EXCEPTION 'El cargo ya integra otra factura emitida';
        END IF;
    ELSE
        SELECT a.id, a.tipo, a.estado, e.cliente_id, e.ambito
          INTO objetivo
          FROM ajustes_cliente a
          JOIN envios e ON e.solicitud_id = a.solicitud_id
         WHERE a.id = NEW.ajuste_id
         FOR UPDATE OF a, e;
        IF NOT FOUND OR objetivo.cliente_id <> factura_actual.cliente_id
           OR objetivo.estado <> 'APLICADO' THEN
            RAISE EXCEPTION 'El ajuste no pertenece al cliente o no está aplicado';
        END IF;
        IF (factura_actual.tipo = 'FC' AND objetivo.tipo <> 'DEBITO')
           OR (factura_actual.tipo = 'NC' AND objetivo.tipo <> 'CREDITO') THEN
            RAISE EXCEPTION 'El tipo de factura no coincide con el ajuste';
        END IF;
        ambito_objetivo := objetivo.ambito;
        IF EXISTS (
            SELECT 1
              FROM facturas_cliente_items otro
              JOIN facturas_cliente f ON f.id = otro.factura_id
             WHERE otro.ajuste_id = NEW.ajuste_id
               AND otro.id <> COALESCE(NEW.id, -1)
               AND f.estado = 'EMITIDA'
        ) THEN
            RAISE EXCEPTION 'El ajuste ya integra otra factura emitida';
        END IF;
    END IF;

    IF ambito_objetivo NOT IN ('NACIONAL','INTERNACIONAL') THEN
        RAISE EXCEPTION 'La partida debe tener ámbito contable antes de facturar';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM facturas_cliente_items item
          LEFT JOIN envios e ON e.id = item.envio_id
          LEFT JOIN ajustes_cliente a ON a.id = item.ajuste_id
          LEFT JOIN envios ea ON ea.solicitud_id = a.solicitud_id
         WHERE item.factura_id = NEW.factura_id
           AND item.id <> COALESCE(NEW.id, -1)
           AND COALESCE(e.ambito, ea.ambito) <> ambito_objetivo
    ) THEN
        RAISE EXCEPTION 'Una factura cliente no puede mezclar ámbitos';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_factura_cliente_item
    ON facturas_cliente_items;
CREATE TRIGGER trg_validar_factura_cliente_item
BEFORE INSERT OR UPDATE ON facturas_cliente_items
FOR EACH ROW EXECUTE FUNCTION tauro_validar_factura_cliente_item();

CREATE OR REPLACE FUNCTION tauro_validar_total_factura_cliente()
RETURNS TRIGGER AS $$
DECLARE
    factura_objetivo BIGINT;
    factura_actual RECORD;
    cantidad INTEGER;
    suma NUMERIC(14,2);
BEGIN
    -- PL/pgSQL valida los campos de NEW/OLD aun dentro de un CASE. Separar
    -- las ramas evita intentar resolver factura_id en la cabecera (o id en
    -- el ítem) cuando el mismo trigger se reutiliza en ambas tablas.
    IF TG_TABLE_NAME = 'facturas_cliente' THEN
        factura_objetivo := COALESCE(NEW.id, OLD.id);
    ELSE
        factura_objetivo := COALESCE(NEW.factura_id, OLD.factura_id);
    END IF;
    SELECT id, estado, total INTO factura_actual
      FROM facturas_cliente WHERE id = factura_objetivo;
    IF NOT FOUND OR factura_actual.estado = 'ANULADA' THEN
        RETURN NULL;
    END IF;
    SELECT COUNT(*), COALESCE(SUM(monto), 0)
      INTO cantidad, suma
      FROM facturas_cliente_items
     WHERE factura_id = factura_objetivo;
    IF cantidad = 0 OR ABS(suma - factura_actual.total) > 0.02 THEN
        RAISE EXCEPTION 'Los ítems no coinciden con el total de la factura cliente';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_total_factura_cliente_cabecera ON facturas_cliente;
CREATE CONSTRAINT TRIGGER trg_total_factura_cliente_cabecera
AFTER INSERT OR UPDATE ON facturas_cliente
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION tauro_validar_total_factura_cliente();
DROP TRIGGER IF EXISTS trg_total_factura_cliente_item ON facturas_cliente_items;
CREATE CONSTRAINT TRIGGER trg_total_factura_cliente_item
AFTER INSERT OR UPDATE OR DELETE ON facturas_cliente_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION tauro_validar_total_factura_cliente();

CREATE OR REPLACE FUNCTION tauro_proteger_factura_cliente()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.cliente_id IS DISTINCT FROM NEW.cliente_id
       OR OLD.tipo IS DISTINCT FROM NEW.tipo
       OR OLD.punto_venta IS DISTINCT FROM NEW.punto_venta
       OR OLD.numero IS DISTINCT FROM NEW.numero
       OR OLD.cae IS DISTINCT FROM NEW.cae
       OR OLD.fecha_emision IS DISTINCT FROM NEW.fecha_emision
       OR OLD.fecha_vencimiento IS DISTINCT FROM NEW.fecha_vencimiento
       OR OLD.periodo_desde IS DISTINCT FROM NEW.periodo_desde
       OR OLD.periodo_hasta IS DISTINCT FROM NEW.periodo_hasta
       OR OLD.subtotal IS DISTINCT FROM NEW.subtotal
       OR OLD.iva IS DISTINCT FROM NEW.iva
       OR OLD.total IS DISTINCT FROM NEW.total
       OR OLD.pdf IS DISTINCT FROM NEW.pdf
       OR OLD.pdf_nombre IS DISTINCT FROM NEW.pdf_nombre
       OR OLD.created_at IS DISTINCT FROM NEW.created_at
       OR OLD.created_by IS DISTINCT FROM NEW.created_by
       OR NOT (OLD.estado = NEW.estado OR (
           OLD.estado = 'EMITIDA' AND NEW.estado = 'ANULADA'
       )) THEN
        RAISE EXCEPTION 'La factura cliente es inmutable; sólo puede anularse';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_proteger_factura_cliente ON facturas_cliente;
CREATE TRIGGER trg_proteger_factura_cliente
BEFORE UPDATE ON facturas_cliente
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_factura_cliente();

CREATE OR REPLACE FUNCTION tauro_proteger_factura_cliente_item()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Los ítems de una factura cliente son inmutables';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_proteger_factura_cliente_item
    ON facturas_cliente_items;
CREATE TRIGGER trg_proteger_factura_cliente_item
BEFORE UPDATE ON facturas_cliente_items
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_factura_cliente_item();

-- ── Imputación documental de pagos ────────────────────────
-- Las filas históricas conservan factura_id/envio_id NULL y su ámbito
-- explícito. Todo flujo nuevo apunta a una factura o a un cargo aún no
-- facturado; el remanente del pago no genera fila y queda a favor.
ALTER TABLE pagos_aplicaciones
    ADD COLUMN IF NOT EXISTS factura_id BIGINT;
ALTER TABLE pagos_aplicaciones
    ADD COLUMN IF NOT EXISTS envio_id INTEGER;

DO $$
DECLARE
    restriccion RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='pagos_aplicaciones'::regclass
           AND conname='pagos_aplicaciones_factura_id_fkey'
    ) THEN
        ALTER TABLE pagos_aplicaciones
            ADD CONSTRAINT pagos_aplicaciones_factura_id_fkey
            FOREIGN KEY (factura_id) REFERENCES facturas_cliente(id)
            ON DELETE RESTRICT ON UPDATE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='pagos_aplicaciones'::regclass
           AND conname='pagos_aplicaciones_envio_id_fkey'
    ) THEN
        ALTER TABLE pagos_aplicaciones
            ADD CONSTRAINT pagos_aplicaciones_envio_id_fkey
            FOREIGN KEY (envio_id) REFERENCES envios(id)
            ON DELETE RESTRICT ON UPDATE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid='pagos_aplicaciones'::regclass
           AND conname='ck_pagos_aplicaciones_objetivo'
    ) THEN
        ALTER TABLE pagos_aplicaciones
            ADD CONSTRAINT ck_pagos_aplicaciones_objetivo
            CHECK (NUM_NONNULLS(factura_id, envio_id) <= 1);
    END IF;

    -- Quita sólo la UNIQUE legacy exacta (pago_id, ambito), cualquiera sea
    -- el nombre que PostgreSQL le haya asignado. Los índices nuevos permiten
    -- varias imputaciones del mismo ámbito a documentos distintos.
    FOR restriccion IN
        SELECT c.conname
          FROM pg_constraint c
         WHERE c.conrelid='pagos_aplicaciones'::regclass
           AND c.contype='u'
           AND (
               SELECT ARRAY_AGG(a.attname ORDER BY u.ord)
                 FROM UNNEST(c.conkey) WITH ORDINALITY u(attnum, ord)
                 JOIN pg_attribute a
                   ON a.attrelid=c.conrelid AND a.attnum=u.attnum
           ) = ARRAY['pago_id','ambito']::name[]
    LOOP
        EXECUTE FORMAT(
            'ALTER TABLE pagos_aplicaciones DROP CONSTRAINT %I',
            restriccion.conname
        );
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pago_aplicacion_factura
    ON pagos_aplicaciones (pago_id, factura_id)
    WHERE factura_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_pago_aplicacion_envio
    ON pagos_aplicaciones (pago_id, envio_id)
    WHERE envio_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_pago_aplicacion_factura_estado
    ON pagos_aplicaciones (factura_id, estado)
    WHERE factura_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_pago_aplicacion_envio_estado
    ON pagos_aplicaciones (envio_id, estado)
    WHERE envio_id IS NOT NULL;

CREATE OR REPLACE FUNCTION validar_pago_aplicacion()
RETURNS TRIGGER AS $$
DECLARE
    pago_actual RECORD;
    factura_actual RECORD;
    envio_actual RECORD;
    ambito_documento TEXT;
    ambito_documento_max TEXT;
    aplicado_pago NUMERIC(14,2);
    aplicado_documento NUMERIC(14,2);
    total_documento NUMERIC(14,2);
BEGIN
    IF TG_OP = 'UPDATE' AND (
        NEW.pago_id IS DISTINCT FROM OLD.pago_id
        OR NEW.factura_id IS DISTINCT FROM OLD.factura_id
        OR NEW.envio_id IS DISTINCT FROM OLD.envio_id
        OR NEW.monto_ars IS DISTINCT FROM OLD.monto_ars
        OR NEW.ambito IS DISTINCT FROM OLD.ambito
        OR NOT (
            NEW.estado = OLD.estado
            OR (OLD.estado='SOLICITADA' AND NEW.estado='APLICADA')
        )
    ) THEN
        RAISE EXCEPTION 'Una aplicación sólo puede confirmarse; no se reescribe';
    END IF;

    SELECT id, cliente_id, monto_ars,
           COALESCE(estado, 'APROBADO') AS estado
      INTO pago_actual
      FROM pagos
     WHERE id=NEW.pago_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'El pago % no existe', NEW.pago_id;
    END IF;
    IF NEW.estado='APLICADA' AND pago_actual.estado <> 'APROBADO' THEN
        RAISE EXCEPTION 'El pago % no está aprobado', NEW.pago_id;
    END IF;
    IF NEW.estado='SOLICITADA' AND pago_actual.estado <> 'PENDIENTE' THEN
        RAISE EXCEPTION 'Sólo un pago pendiente admite una imputación solicitada';
    END IF;

    IF NEW.factura_id IS NOT NULL THEN
        SELECT id, cliente_id, tipo, estado, total
          INTO factura_actual
          FROM facturas_cliente
         WHERE id=NEW.factura_id
         FOR UPDATE;
        IF NOT FOUND OR factura_actual.cliente_id <> pago_actual.cliente_id
           OR factura_actual.estado <> 'EMITIDA' OR factura_actual.tipo <> 'FC' THEN
            RAISE EXCEPTION 'La factura no pertenece al cliente o no es imputable';
        END IF;
        SELECT MIN(COALESCE(e.ambito, ea.ambito)),
               MAX(COALESCE(e.ambito, ea.ambito))
          INTO ambito_documento, ambito_documento_max
          FROM facturas_cliente_items i
     LEFT JOIN envios e ON e.id=i.envio_id
     LEFT JOIN ajustes_cliente a ON a.id=i.ajuste_id
     LEFT JOIN envios ea ON ea.solicitud_id=a.solicitud_id
         WHERE i.factura_id=NEW.factura_id;
        IF ambito_documento IS DISTINCT FROM ambito_documento_max THEN
            RAISE EXCEPTION 'La factura mezcla ámbitos contables';
        END IF;
        total_documento := factura_actual.total;
        SELECT COALESCE(SUM(pa.monto_ars), 0)
          INTO aplicado_documento
          FROM pagos_aplicaciones pa
         WHERE pa.id <> COALESCE(NEW.id, -1)
           AND pa.estado IN ('SOLICITADA','APLICADA')
           AND (
               pa.factura_id=NEW.factura_id
               OR pa.envio_id IN (
                   SELECT i.envio_id FROM facturas_cliente_items i
                    WHERE i.factura_id=NEW.factura_id
                      AND i.envio_id IS NOT NULL
               )
           );
    ELSIF NEW.envio_id IS NOT NULL THEN
        SELECT id, cliente_id, estado, monto_ars, ambito
          INTO envio_actual
          FROM envios
         WHERE id=NEW.envio_id
         FOR UPDATE;
        IF NOT FOUND OR envio_actual.cliente_id <> pago_actual.cliente_id
           OR envio_actual.estado <> 'ACTIVO' THEN
            RAISE EXCEPTION 'El cargo no pertenece al cliente o no está activo';
        END IF;
        IF TG_OP='INSERT' AND EXISTS (
            SELECT 1 FROM facturas_cliente_items i
            JOIN facturas_cliente f ON f.id=i.factura_id
            WHERE i.envio_id=NEW.envio_id AND f.estado='EMITIDA'
        ) THEN
            RAISE EXCEPTION 'El cargo ya está facturado; imputá la factura';
        END IF;
        ambito_documento := envio_actual.ambito;
        total_documento := envio_actual.monto_ars;
        SELECT COALESCE(SUM(pa.monto_ars), 0)
          INTO aplicado_documento
          FROM pagos_aplicaciones pa
         WHERE pa.id <> COALESCE(NEW.id, -1)
           AND pa.envio_id=NEW.envio_id
           AND pa.estado IN ('SOLICITADA','APLICADA');
    ELSE
        -- Compatibilidad: las aplicaciones anteriores a esta migración no
        -- tienen documento y conservan el ámbito que ya tenían.
        ambito_documento := NEW.ambito;
        aplicado_documento := 0;
        total_documento := NULL;
    END IF;

    IF ambito_documento NOT IN ('NACIONAL','INTERNACIONAL') THEN
        RAISE EXCEPTION 'El documento no tiene ámbito contable válido';
    END IF;
    NEW.ambito := ambito_documento;
    IF total_documento IS NOT NULL
       AND aplicado_documento + NEW.monto_ars > total_documento THEN
        RAISE EXCEPTION 'La aplicación supera el saldo del documento';
    END IF;

    SELECT COALESCE(SUM(monto_ars), 0)
      INTO aplicado_pago
      FROM pagos_aplicaciones
     WHERE pago_id=NEW.pago_id
       AND id <> COALESCE(NEW.id, -1);
    IF aplicado_pago + NEW.monto_ars > pago_actual.monto_ars THEN
        RAISE EXCEPTION 'Las aplicaciones superan el monto del pago';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_validar_pago_aplicacion ON pagos_aplicaciones;
CREATE TRIGGER trg_validar_pago_aplicacion
BEFORE INSERT OR UPDATE ON pagos_aplicaciones
FOR EACH ROW EXECUTE FUNCTION validar_pago_aplicacion();

-- 7) Auditoría permanente del módulo. No comparte la política de retención
--    corta de security_audit porque forma parte de la evidencia financiera.
CREATE TABLE IF NOT EXISTS auditoria_facturas_courier (
    id               BIGSERIAL PRIMARY KEY,
    evento           TEXT NOT NULL,
    factura_id       BIGINT REFERENCES facturas_courier(id) ON DELETE RESTRICT,
    item_id          BIGINT REFERENCES facturas_courier_items(id) ON DELETE RESTRICT,
    solicitud_id     INTEGER REFERENCES solicitudes_guia(id) ON DELETE RESTRICT,
    conciliacion_id  BIGINT REFERENCES conciliaciones_envio(id) ON DELETE RESTRICT,
    ajuste_id        BIGINT REFERENCES ajustes_cliente(id) ON DELETE RESTRICT,
    actor            TEXT NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_auditoria_courier_fecha
    ON auditoria_facturas_courier (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_auditoria_courier_solicitud
    ON auditoria_facturas_courier (solicitud_id, created_at DESC);

-- Ningún documento financiero se borra físicamente. Los errores se anulan o
-- rechazan preservando evidencia. DROP SCHEMA de tests/migraciones no dispara
-- estos triggers y sigue siendo posible.
CREATE OR REPLACE FUNCTION tauro_bloquear_borrado_financiero()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Los registros financieros no se eliminan; deben anularse';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tauro_bloquear_mutacion_snapshot()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'El snapshot de cotización aceptada es inmutable';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tauro_bloquear_mutacion_auditoria()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'La auditoría financiera es append-only';
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    tabla TEXT;
    trigger_nombre TEXT;
BEGIN
    FOREACH tabla IN ARRAY ARRAY[
        'facturas_courier',
        'facturas_courier_items',
        'factura_courier_item_matches',
        'conciliaciones_envio',
        'ajustes_cliente',
        'facturas_cliente',
        'facturas_cliente_items',
        'auditoria_facturas_courier'
    ]
    LOOP
        trigger_nombre := 'trg_no_delete_' || tabla;
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
             WHERE tgrelid = TO_REGCLASS(tabla)
               AND tgname = trigger_nombre
               AND NOT tgisinternal
        ) THEN
            EXECUTE FORMAT(
                'CREATE TRIGGER %I BEFORE DELETE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION tauro_bloquear_borrado_financiero()',
                trigger_nombre, tabla
            );
        END IF;
    END LOOP;
END $$;

DROP TRIGGER IF EXISTS trg_snapshot_inmutable
    ON envio_cotizacion_snapshots;
CREATE TRIGGER trg_snapshot_inmutable
BEFORE UPDATE OR DELETE ON envio_cotizacion_snapshots
FOR EACH ROW EXECUTE FUNCTION tauro_bloquear_mutacion_snapshot();

DROP TRIGGER IF EXISTS trg_auditoria_courier_append_only
    ON auditoria_facturas_courier;
CREATE TRIGGER trg_auditoria_courier_append_only
BEFORE UPDATE ON auditoria_facturas_courier
FOR EACH ROW EXECUTE FUNCTION tauro_bloquear_mutacion_auditoria();

ALTER TABLE clientes ADD COLUMN IF NOT EXISTS pricing_rangos_internacional JSONB NOT NULL DEFAULT '[]';
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS pricing_rangos_nacional JSONB NOT NULL DEFAULT '[]';
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS perfil_comercial TEXT NOT NULL DEFAULT '';

-- Valores default de config
-- Entrada administrativa de PDFs DHL. Evidencia pendiente, NO cuenta corriente.
CREATE TABLE IF NOT EXISTS entradas_pdf_dhl (
    id BIGSERIAL PRIMARY KEY,
    archivo_nombre TEXT NOT NULL,
    archivo_pdf BYTEA NOT NULL CHECK (octet_length(archivo_pdf) BETWEEN 4 AND 8388608),
    archivo_sha256 TEXT NOT NULL UNIQUE CHECK (archivo_sha256 ~ '^[0-9a-f]{64}$'),
    numero_esperado TEXT NOT NULL CHECK (numero_esperado ~ '^[0-9]{4}A[0-9]{8}$'),
    cuit_esperado TEXT NOT NULL CHECK (cuit_esperado ~ '^[0-9]{11}$'),
    canal TEXT NOT NULL DEFAULT 'ADMIN_PDF' CHECK (canal = 'ADMIN_PDF'),
    estado TEXT NOT NULL DEFAULT 'RECIBIDA'
        CHECK (estado IN ('RECIBIDA','PARA_REVISION','REVISION_MANUAL','IMPORTADA')),
    extraccion JSONB,
    observaciones JSONB NOT NULL DEFAULT '[]',
    revision_sha256 TEXT,
    lector_version INTEGER,
    error_lectura TEXT,
    intentos INTEGER NOT NULL DEFAULT 0 CHECK (intentos >= 0),
    factura_id BIGINT REFERENCES facturas_courier(id) ON DELETE RESTRICT,
    creado_por TEXT NOT NULL,
    revisado_por TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revisado_at TIMESTAMPTZ,
    CHECK ((estado = 'IMPORTADA') = (factura_id IS NOT NULL)),
    CHECK (estado NOT IN ('PARA_REVISION','IMPORTADA') OR
        (extraccion IS NOT NULL AND revision_sha256 IS NOT NULL AND lector_version IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_entradas_dhl_estado ON entradas_pdf_dhl(estado, id DESC);

CREATE OR REPLACE FUNCTION tauro_proteger_entrada_dhl() RETURNS TRIGGER AS $$
BEGIN
    IF ROW(NEW.archivo_pdf, NEW.archivo_sha256, NEW.archivo_nombre, NEW.canal,
           NEW.creado_por, NEW.created_at)
       IS DISTINCT FROM ROW(OLD.archivo_pdf, OLD.archivo_sha256, OLD.archivo_nombre,
           OLD.canal, OLD.creado_por, OLD.created_at) THEN
        RAISE EXCEPTION 'La evidencia original DHL es inmutable';
    END IF;
    IF OLD.estado = 'IMPORTADA' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'La entrada importada DHL es inmutable';
    END IF;
    IF OLD.estado = 'PARA_REVISION' AND (
        NEW.estado NOT IN ('PARA_REVISION','IMPORTADA') OR
        ROW(NEW.numero_esperado, NEW.cuit_esperado, NEW.extraccion,
            NEW.observaciones, NEW.revision_sha256, NEW.lector_version)
        IS DISTINCT FROM ROW(OLD.numero_esperado, OLD.cuit_esperado, OLD.extraccion,
            OLD.observaciones, OLD.revision_sha256, OLD.lector_version)
    ) THEN
        RAISE EXCEPTION 'La extracción presentada para revisión es inmutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_proteger_entrada_dhl ON entradas_pdf_dhl;
CREATE TRIGGER trg_proteger_entrada_dhl BEFORE UPDATE ON entradas_pdf_dhl
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_entrada_dhl();
DROP TRIGGER IF EXISTS trg_no_borrar_entrada_dhl ON entradas_pdf_dhl;
CREATE TRIGGER trg_no_borrar_entrada_dhl BEFORE DELETE ON entradas_pdf_dhl
FOR EACH ROW EXECUTE FUNCTION tauro_bloquear_borrado_financiero();

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
