-- Una guía sin dueño queda exclusivamente en administración, con su identidad.
ALTER TABLE solicitudes_guia ALTER COLUMN cliente_id DROP NOT NULL;
ALTER TABLE solicitudes_guia ADD COLUMN IF NOT EXISTS cliente_pendiente_nombre TEXT;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='solicitudes_guia'::regclass
          AND conname='ck_solicitud_pendiente_privada') THEN
        ALTER TABLE solicitudes_guia ADD CONSTRAINT ck_solicitud_pendiente_privada
            CHECK (cliente_id IS NOT NULL OR (
                visible_cliente=FALSE AND cargo_pendiente=FALSE
                AND COALESCE(LENGTH(BTRIM(cliente_pendiente_nombre)),0)>0
            ));
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS asignaciones_envio (
    id BIGSERIAL PRIMARY KEY,
    solicitud_id INTEGER NOT NULL REFERENCES solicitudes_guia(id) ON DELETE RESTRICT,
    cliente_anterior TEXT,
    cliente_nuevo TEXT,
    cliente_indicado TEXT,
    motivo TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_asignaciones_envio_historia
    ON asignaciones_envio(solicitud_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_solicitudes_sin_cliente
    ON solicitudes_guia(created_at DESC,id DESC) WHERE cliente_id IS NULL;
