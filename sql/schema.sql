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
-- ============================================================

-- ── Clientes (ex PERFILES) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS clientes (
    cliente_id   TEXT PRIMARY KEY,        -- UPPERCASE, ej "MENDEZ"
    email        TEXT UNIQUE NOT NULL,
    api_key      TEXT,
    markup_pct   REAL    NOT NULL DEFAULT 25.0,
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
CREATE INDEX IF NOT EXISTS idx_envios_cliente ON envios(cliente_id);

-- ── Log de cotizaciones (ex COTI) ───────────────────────────
CREATE TABLE IF NOT EXISTS cotizaciones (
    id               SERIAL PRIMARY KEY,
    cliente_id       TEXT NOT NULL,
    ruta_id          TEXT NOT NULL,
    peso_kg          REAL NOT NULL,
    dimensiones      TEXT,
    peso_usado_kg    REAL NOT NULL,
    costo_fedex_usd  REAL,
    markup_pct       REAL,
    precio_final_usd REAL,
    precio_final_ars REAL,
    dias_estimados   INTEGER,
    valida_hasta     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Configuración global (ex CONFIG) ────────────────────────
CREATE TABLE IF NOT EXISTS config (
    parametro TEXT PRIMARY KEY,
    valor     TEXT NOT NULL
);

-- Valores default de config
INSERT INTO config (parametro, valor) VALUES
    ('COTIZACION_DOLAR_ARS', '1450'),
    ('WEB_MARKUP_PCT', '20'),
    ('MARGEN_MINIMO_ARS', '5000')
ON CONFLICT (parametro) DO NOTHING;
