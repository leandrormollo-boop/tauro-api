-- Sin payloads, tracking, nombres, documentos ni mensajes externos.
CREATE TABLE IF NOT EXISTS automatizaciones_estado (
    clave TEXT PRIMARY KEY,
    estado TEXT NOT NULL CHECK (estado IN ('EN_CURSO','OK','ERROR','PARCIAL','OMITIDA','SIN_CONFIRMAR')),
    ultima_ejecucion TIMESTAMPTZ NOT NULL,
    ultimo_exito TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS automatizaciones_historial (
    id BIGSERIAL PRIMARY KEY,
    clave TEXT NOT NULL REFERENCES automatizaciones_estado(clave),
    estado TEXT NOT NULL CHECK (estado IN ('EN_CURSO','OK','ERROR','PARCIAL','OMITIDA','SIN_CONFIRMAR')),
    fecha TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automatizaciones_historial_clave ON automatizaciones_historial(clave,id DESC);
