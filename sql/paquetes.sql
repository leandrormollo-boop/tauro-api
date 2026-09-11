-- Embalajes del cliente y recetas de contenido verificadas físicamente.
-- Ejecutado por init_db después del schema base; se puede repetir.
CREATE UNIQUE INDEX IF NOT EXISTS uq_productos_cliente_id ON productos(cliente_id, id);
CREATE TABLE IF NOT EXISTS paquetes_guardados (
    id BIGSERIAL PRIMARY KEY,
    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
    nombre TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 80),
    largo_cm NUMERIC(9,2) NOT NULL CHECK (largo_cm > 0 AND largo_cm <= 300),
    ancho_cm NUMERIC(9,2) NOT NULL CHECK (ancho_cm > 0 AND ancho_cm <= 300),
    alto_cm NUMERIC(9,2) NOT NULL CHECK (alto_cm > 0 AND alto_cm <= 300),
    tara_kg NUMERIC(8,3) NOT NULL CHECK (tara_kg >= 0 AND tara_kg <= 70),
    proteccion_kg NUMERIC(8,3) NOT NULL CHECK (proteccion_kg >= 0 AND proteccion_kg <= 70),
    max_kg NUMERIC(8,3) NOT NULL CHECK (max_kg > tara_kg + proteccion_kg AND max_kg <= 70),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(cliente_id, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_paquetes_nombre_activo
    ON paquetes_guardados(cliente_id, lower(nombre)) WHERE activo;
CREATE TABLE IF NOT EXISTS paquetes_productos (
    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
    producto_id INTEGER NOT NULL,
    paquete_id BIGINT NOT NULL,
    peso_neto_kg NUMERIC(8,3) NOT NULL CHECK (peso_neto_kg > 0 AND peso_neto_kg <= 70),
    unidades_por_caja INTEGER NOT NULL CHECK (unidades_por_caja BETWEEN 1 AND 100),
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(cliente_id, producto_id),
    FOREIGN KEY(cliente_id, producto_id) REFERENCES productos(cliente_id, id) ON DELETE CASCADE,
    FOREIGN KEY(cliente_id, paquete_id) REFERENCES paquetes_guardados(cliente_id, id)
);
CREATE TABLE IF NOT EXISTS paquetes_combinaciones (
    id BIGSERIAL PRIMARY KEY,
    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
    nombre TEXT NOT NULL CHECK (length(nombre) BETWEEN 1 AND 80),
    paquete_id BIGINT NOT NULL,
    contenido JSONB NOT NULL CHECK (jsonb_typeof(contenido) = 'array'),
    prioridad INTEGER NOT NULL DEFAULT 0 CHECK (prioridad BETWEEN 0 AND 100),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(cliente_id, paquete_id) REFERENCES paquetes_guardados(cliente_id, id)
);
CREATE INDEX IF NOT EXISTS ix_paquetes_combinaciones_cliente ON paquetes_combinaciones(cliente_id);
CREATE TABLE IF NOT EXISTS paquetes_tiendas (
    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
    plataforma TEXT NOT NULL CHECK (plataforma IN ('shopify','tiendanube')),
    dominio TEXT NOT NULL,
    usar_paquetes BOOLEAN NOT NULL DEFAULT FALSE,
    nacional JSONB NOT NULL DEFAULT '{"habilitado":false,"politica":"real"}',
    internacional JSONB NOT NULL DEFAULT '{"habilitado":false,"politica":"real"}',
    callback_token_hash TEXT,
    carrier_id TEXT,
    install_generation TEXT,
    checkout_activo BOOLEAN NOT NULL DEFAULT FALSE,
    version INTEGER NOT NULL DEFAULT 1,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(cliente_id, plataforma, dominio),
    UNIQUE(callback_token_hash)
);
CREATE TABLE IF NOT EXISTS paquetes_planes_pedido (
    cliente_id TEXT NOT NULL REFERENCES clientes(cliente_id),
    pedido_id INTEGER NOT NULL,
    plan JSONB NOT NULL,
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(cliente_id, pedido_id)
);
