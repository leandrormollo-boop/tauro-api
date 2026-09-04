-- Libro de proveedores: independiente de pagos/cargos de clientes.
-- Este archivo se replica en schema.sql para instalaciones y upgrades existentes.
CREATE TABLE IF NOT EXISTS condiciones_operador (
    id BIGSERIAL PRIMARY KEY,
    courier TEXT NOT NULL CHECK (courier IN ('DHL','FEDEX','ANDREANI','OCA')),
    plazo_dias INTEGER CHECK (plazo_dias BETWEEN 0 AND 365),
    motivo TEXT NOT NULL CHECK (length(btrim(motivo)) BETWEEN 5 AND 1000),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vencimientos_operador (
    factura_id BIGINT PRIMARY KEY REFERENCES facturas_courier(id),
    condicion_id BIGINT NOT NULL REFERENCES condiciones_operador(id),
    fecha DATE NOT NULL,
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pagos_operador (
    id BIGSERIAL PRIMARY KEY,
    courier TEXT NOT NULL CHECK (courier IN ('DHL','FEDEX','ANDREANI','OCA')),
    fecha DATE NOT NULL,
    moneda TEXT NOT NULL CHECK (moneda ~ '^[A-Z]{3}$'),
    importe NUMERIC(18,4) NOT NULL CHECK (importe > 0 AND importe < 'Infinity'::numeric),
    referencia TEXT NOT NULL CHECK (length(btrim(referencia)) BETWEEN 3 AND 200),
    referencia_normalizada TEXT GENERATED ALWAYS AS (upper(btrim(referencia))) STORED,
    comprobante_pdf BYTEA NOT NULL CHECK (octet_length(comprobante_pdf) BETWEEN 5 AND 8388608
        AND substring(comprobante_pdf from 1 for 5) = decode('255044462d','hex')),
    comprobante_sha256 TEXT NOT NULL CHECK (comprobante_sha256 ~ '^[a-f0-9]{64}$'),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    clave TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- La identidad se valida bajo lock sobre registros vigentes: una reversa permite
-- volver a cargar correctamente el mismo comprobante sin borrar el original.
ALTER TABLE pagos_operador DROP CONSTRAINT IF EXISTS pagos_operador_courier_referencia_normalizada_key;
ALTER TABLE pagos_operador DROP CONSTRAINT IF EXISTS pagos_operador_courier_comprobante_sha256_key;
CREATE TABLE IF NOT EXISTS aplicaciones_operador (
    id BIGSERIAL PRIMARY KEY,
    factura_id BIGINT NOT NULL REFERENCES facturas_courier(id),
    pago_id BIGINT REFERENCES pagos_operador(id),
    nc_id BIGINT REFERENCES facturas_courier(id),
    importe_documento NUMERIC(18,4) NOT NULL CHECK (importe_documento > 0 AND importe_documento < 'Infinity'::numeric),
    tipo_cambio NUMERIC(20,8) NOT NULL CHECK (tipo_cambio > 0 AND tipo_cambio < 'Infinity'::numeric),
    importe_origen NUMERIC(18,4) NOT NULL CHECK (importe_origen > 0 AND importe_origen < 'Infinity'::numeric),
    conversion_confirmada BOOLEAN NOT NULL DEFAULT false,
    motivo TEXT NOT NULL CHECK (length(btrim(motivo)) BETWEEN 5 AND 1000),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    clave TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(pago_id, nc_id) = 1),
    CHECK (nc_id IS NULL OR nc_id <> factura_id)
);
CREATE INDEX IF NOT EXISTS ix_aplicaciones_operador_fc ON aplicaciones_operador(factura_id);
ALTER TABLE aplicaciones_operador ADD COLUMN IF NOT EXISTS conversion_confirmada BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_aplicaciones_operador_pago ON aplicaciones_operador(pago_id);
CREATE INDEX IF NOT EXISTS ix_aplicaciones_operador_nc ON aplicaciones_operador(nc_id);
CREATE OR REPLACE FUNCTION tauro_validar_pago_operador() RETURNS trigger AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'El libro de operadores requiere aislamiento READ COMMITTED';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('tauro:operador:' || NEW.courier, 0));
    IF NEW.fecha > (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
       OR NEW.comprobante_sha256 <> encode(sha256(NEW.comprobante_pdf),'hex') THEN
        RAISE EXCEPTION 'Pago futuro o comprobante inconsistente';
    END IF;
    IF EXISTS(SELECT 1 FROM pagos_operador p WHERE p.courier=NEW.courier
        AND (p.referencia_normalizada=upper(btrim(NEW.referencia)) OR p.comprobante_sha256=NEW.comprobante_sha256)
        AND NOT EXISTS(SELECT 1 FROM reversiones_pago_operador r WHERE r.pago_id=p.id)) THEN
        RAISE EXCEPTION 'Operación duplicada: referencia o comprobante de un pago vigente';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_validar_pago_operador ON pagos_operador;
CREATE TRIGGER trg_validar_pago_operador BEFORE INSERT ON pagos_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_pago_operador();
CREATE TABLE IF NOT EXISTS verificaciones_operador (
    id BIGSERIAL PRIMARY KEY,
    factura_id BIGINT NOT NULL REFERENCES facturas_courier(id),
    motivo TEXT NOT NULL CHECK (length(btrim(motivo)) BETWEEN 5 AND 1000),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reversiones_aplicacion_operador (
    id BIGSERIAL PRIMARY KEY,
    aplicacion_id BIGINT NOT NULL UNIQUE REFERENCES aplicaciones_operador(id),
    motivo TEXT NOT NULL CHECK(length(btrim(motivo)) BETWEEN 10 AND 1000),
    actor TEXT NOT NULL CHECK(btrim(actor)<>''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS reversiones_pago_operador (
    id BIGSERIAL PRIMARY KEY,
    pago_id BIGINT NOT NULL UNIQUE REFERENCES pagos_operador(id),
    motivo TEXT NOT NULL CHECK(length(btrim(motivo)) BETWEEN 10 AND 1000),
    actor TEXT NOT NULL CHECK(btrim(actor)<>''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION tauro_validar_reversion_operador() RETURNS trigger AS $$
DECLARE c text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'El libro de operadores requiere aislamiento READ COMMITTED';
    END IF;
    IF TG_TABLE_NAME='reversiones_pago_operador' THEN
        SELECT courier INTO STRICT c FROM pagos_operador WHERE id=NEW.pago_id;
    ELSE
        SELECT f.courier INTO STRICT c FROM aplicaciones_operador a
            JOIN facturas_courier f ON f.id=a.factura_id WHERE a.id=NEW.aplicacion_id;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('tauro:operador:' || c, 0));
    IF TG_TABLE_NAME='reversiones_pago_operador' THEN
        IF EXISTS(SELECT 1 FROM aplicaciones_operador a WHERE a.pago_id=NEW.pago_id
            AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id)
        ) THEN RAISE EXCEPTION 'Revertí primero las aplicaciones vigentes del pago'; END IF;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_reversion_pago_operador ON reversiones_pago_operador;
CREATE TRIGGER trg_reversion_pago_operador BEFORE INSERT ON reversiones_pago_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_reversion_operador();
DROP TRIGGER IF EXISTS trg_reversion_aplicacion_operador ON reversiones_aplicacion_operador;
CREATE TRIGGER trg_reversion_aplicacion_operador BEFORE INSERT ON reversiones_aplicacion_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_reversion_operador();

CREATE OR REPLACE FUNCTION tauro_validar_aplicacion_operador() RETURNS trigger AS $$
DECLARE f facturas_courier%ROWTYPE; n facturas_courier%ROWTYPE;
        p pagos_operador%ROWTYPE; disponible numeric; operador text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'El libro de operadores requiere aislamiento READ COMMITTED';
    END IF;
    SELECT courier INTO operador FROM facturas_courier WHERE id=NEW.factura_id;
    PERFORM pg_advisory_xact_lock(hashtextextended('tauro:operador:' || operador, 0));
    SELECT * INTO STRICT f FROM facturas_courier WHERE id=NEW.factura_id FOR UPDATE;
    IF f.tipo_documento NOT IN ('FC','ND') OR f.estado='ANULADA' OR f.total <= 0
       OR f.total >= 'Infinity'::numeric THEN
        RAISE EXCEPTION 'Destino no pagable: sólo FC/ND válidas';
    END IF;
    SELECT f.total - coalesce(sum(importe_documento),0) INTO disponible
        FROM aplicaciones_operador a WHERE factura_id=f.id
        AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id);
    IF NEW.importe_documento > disponible THEN
        RAISE EXCEPTION 'La aplicación supera el saldo de la factura';
    END IF;
    IF NEW.pago_id IS NOT NULL THEN
        SELECT * INTO STRICT p FROM pagos_operador WHERE id=NEW.pago_id FOR UPDATE;
        IF EXISTS(SELECT 1 FROM reversiones_pago_operador WHERE pago_id=p.id) THEN
            RAISE EXCEPTION 'El pago fue revertido';
        END IF;
        IF p.courier <> f.courier THEN RAISE EXCEPTION 'Operador de pago diferente'; END IF;
        IF p.moneda<>f.moneda AND NOT NEW.conversion_confirmada THEN
            RAISE EXCEPTION 'Confirmá expresamente la conversión entre monedas contra el comprobante';
        END IF;
        IF p.moneda=f.moneda AND NEW.tipo_cambio <> 1 THEN
            RAISE EXCEPTION 'Misma moneda requiere tipo de cambio 1';
        END IF;
        SELECT p.importe-coalesce(sum(importe_origen),0) INTO disponible
            FROM aplicaciones_operador a WHERE pago_id=p.id
            AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id);
    ELSE
        SELECT * INTO STRICT n FROM facturas_courier WHERE id=NEW.nc_id FOR UPDATE;
        IF NOT EXISTS(SELECT 1 FROM verificaciones_operador WHERE factura_id=n.id) THEN
            RAISE EXCEPTION 'Verificá el historial de la NC antes de disponer de su crédito';
        END IF;
        IF n.tipo_documento <> 'NC' OR n.estado='ANULADA' OR n.courier <> f.courier
           OR n.moneda <> f.moneda OR NEW.tipo_cambio <> 1 OR n.total >= 'Infinity'::numeric
           OR n.fecha_emision IS NULL
           OR n.fecha_emision > (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
           OR n.archivo_pdf IS NULL
           OR substring(n.archivo_pdf from 1 for 5) <> decode('255044462d','hex')
           OR (n.factura_referenciada_id IS NOT NULL AND n.factura_referenciada_id <> f.id) THEN
            RAISE EXCEPTION 'NC incompatible o sin PDF/fecha documental';
        END IF;
        SELECT n.total-coalesce(sum(importe_origen),0) INTO disponible
            FROM aplicaciones_operador a WHERE nc_id=n.id
            AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id);
    END IF;
    IF NEW.importe_origen <> round(NEW.importe_documento*NEW.tipo_cambio,4)
       OR NEW.importe_origen > disponible THEN
        RAISE EXCEPTION 'Conversión inválida o saldo de origen insuficiente';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_aplicacion_operador ON aplicaciones_operador;
CREATE TRIGGER trg_aplicacion_operador BEFORE INSERT ON aplicaciones_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_aplicacion_operador();

CREATE OR REPLACE FUNCTION tauro_validar_vencimiento_operador() RETURNS trigger AS $$
DECLARE f facturas_courier%ROWTYPE; c condiciones_operador%ROWTYPE;
BEGIN
    SELECT * INTO STRICT f FROM facturas_courier WHERE id=NEW.factura_id;
    SELECT * INTO STRICT c FROM condiciones_operador WHERE id=NEW.condicion_id;
    IF f.courier<>c.courier OR f.tipo_documento='NC' OR f.estado='ANULADA'
       OR f.fecha_emision IS NULL OR f.fecha_vencimiento IS NOT NULL
       OR c.plazo_dias IS NULL OR NEW.fecha <> f.fecha_emision+c.plazo_dias THEN
        RAISE EXCEPTION 'Vencimiento acordado incompatible con el documento';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_validar_vencimiento_operador ON vencimientos_operador;
CREATE TRIGGER trg_validar_vencimiento_operador BEFORE INSERT ON vencimientos_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_vencimiento_operador();

CREATE OR REPLACE FUNCTION tauro_fijar_vencimiento_operador() RETURNS trigger AS $$
DECLARE c condiciones_operador%ROWTYPE;
BEGIN
    IF NEW.tipo_documento IN ('FC','ND') AND NEW.fecha_emision IS NOT NULL
       AND NEW.fecha_vencimiento IS NULL AND NEW.estado <> 'ANULADA' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('tauro:operador:' || NEW.courier, 0));
        SELECT * INTO c FROM condiciones_operador WHERE courier=NEW.courier ORDER BY id DESC LIMIT 1;
        IF c.plazo_dias IS NOT NULL
           AND NEW.fecha_emision >= (c.created_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date THEN
            INSERT INTO vencimientos_operador(factura_id,condicion_id,fecha,actor)
                VALUES(NEW.id,c.id,NEW.fecha_emision+c.plazo_dias,'sistema:plazo-acordado');
        END IF;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_fijar_vencimiento_operador ON facturas_courier;
CREATE TRIGGER trg_fijar_vencimiento_operador AFTER INSERT ON facturas_courier
FOR EACH ROW EXECUTE FUNCTION tauro_fijar_vencimiento_operador();

CREATE OR REPLACE FUNCTION tauro_proteger_documento_operador() RETURNS trigger AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'El libro de operadores requiere aislamiento READ COMMITTED';
    END IF;
    IF (OLD.archivo_pdf IS NOT NULL AND NEW.archivo_pdf IS DISTINCT FROM OLD.archivo_pdf)
       OR (OLD.archivo_sha256 IS NOT NULL AND NEW.archivo_sha256 IS DISTINCT FROM OLD.archivo_sha256) THEN
        RAISE EXCEPTION 'No reemplazar la evidencia original del documento';
    END IF;
    IF NEW.fecha_emision IS DISTINCT FROM OLD.fecha_emision AND (
        EXISTS(SELECT 1 FROM vencimientos_operador WHERE factura_id=OLD.id)
        OR EXISTS(SELECT 1 FROM rectificaciones_vencimiento_operador WHERE factura_id=OLD.id)
    ) THEN
        RAISE EXCEPTION 'La emisión sustenta un vencimiento auditado; no modificarla';
    END IF;
    IF (ROW(NEW.courier,NEW.tipo_documento,NEW.numero,NEW.total,NEW.moneda,
            NEW.fecha_emision,NEW.fecha_vencimiento,NEW.factura_referenciada_id)
        IS DISTINCT FROM ROW(OLD.courier,OLD.tipo_documento,OLD.numero,OLD.total,OLD.moneda,
            OLD.fecha_emision,OLD.fecha_vencimiento,OLD.factura_referenciada_id)
        OR (OLD.archivo_pdf IS NOT NULL AND NEW.archivo_pdf IS DISTINCT FROM OLD.archivo_pdf)
        OR (OLD.archivo_sha256 IS NOT NULL AND NEW.archivo_sha256 IS DISTINCT FROM OLD.archivo_sha256))
        AND (EXISTS(SELECT 1 FROM aplicaciones_operador WHERE factura_id=OLD.id OR nc_id=OLD.id)
          OR EXISTS(SELECT 1 FROM verificaciones_operador WHERE factura_id=OLD.id)) THEN
        RAISE EXCEPTION 'Documento con historial de proveedor: no modificar su evidencia original';
    END IF;
    IF OLD.archivo_pdf IS NULL AND NEW.archivo_pdf IS NOT NULL
       AND NEW.archivo_sha256 IS DISTINCT FROM encode(sha256(NEW.archivo_pdf),'hex') THEN
        RAISE EXCEPTION 'El PDF agregado no coincide con su hash';
    END IF;
    IF NEW.estado='ANULADA' AND OLD.estado<>'ANULADA' AND EXISTS(
        SELECT 1 FROM aplicaciones_operador a WHERE (a.factura_id=OLD.id OR a.nc_id=OLD.id)
        AND NOT EXISTS(SELECT 1 FROM reversiones_aplicacion_operador r WHERE r.aplicacion_id=a.id)
    ) THEN
        RAISE EXCEPTION 'Revertí primero las aplicaciones vigentes antes de anular el documento';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_proteger_documento_operador ON facturas_courier;
CREATE TRIGGER trg_proteger_documento_operador BEFORE UPDATE ON facturas_courier
FOR EACH ROW EXECUTE FUNCTION tauro_proteger_documento_operador();

ALTER TABLE condiciones_operador ADD COLUMN IF NOT EXISTS clave TEXT UNIQUE;
ALTER TABLE verificaciones_operador ADD COLUMN IF NOT EXISTS clave TEXT UNIQUE;
CREATE INDEX IF NOT EXISTS ix_condiciones_operador_vigente ON condiciones_operador(courier,id DESC);
CREATE INDEX IF NOT EXISTS ix_verificaciones_operador_fc ON verificaciones_operador(factura_id);
CREATE INDEX IF NOT EXISTS ix_pagos_operador_ref ON pagos_operador(courier,referencia_normalizada);
CREATE INDEX IF NOT EXISTS ix_pagos_operador_sha ON pagos_operador(courier,comprobante_sha256);
CREATE TABLE IF NOT EXISTS rectificaciones_vencimiento_operador (
    id BIGSERIAL PRIMARY KEY,
    factura_id BIGINT NOT NULL REFERENCES facturas_courier(id),
    fecha DATE,
    motivo TEXT NOT NULL CHECK(length(btrim(motivo)) BETWEEN 10 AND 1000),
    actor TEXT NOT NULL CHECK(btrim(actor)<>''),
    clave TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rectificaciones_vencimiento_fc ON rectificaciones_vencimiento_operador(factura_id,id DESC);
CREATE OR REPLACE FUNCTION tauro_validar_rectificacion_vencimiento() RETURNS trigger AS $$
DECLARE f facturas_courier%ROWTYPE; c text;
BEGIN
    SELECT courier INTO STRICT c FROM facturas_courier WHERE id=NEW.factura_id;
    PERFORM pg_advisory_xact_lock(hashtextextended('tauro:operador:' || c,0));
    SELECT * INTO STRICT f FROM facturas_courier WHERE id=NEW.factura_id FOR UPDATE;
    IF f.tipo_documento='NC' OR f.estado='ANULADA' OR f.fecha_vencimiento IS NOT NULL
        OR (NEW.fecha IS NOT NULL AND (f.fecha_emision IS NULL OR NEW.fecha<f.fecha_emision)) THEN
        RAISE EXCEPTION 'Rectificación incompatible: no cambia el vencimiento del documento original';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_rectificacion_vencimiento ON rectificaciones_vencimiento_operador;
CREATE TRIGGER trg_rectificacion_vencimiento BEFORE INSERT ON rectificaciones_vencimiento_operador
FOR EACH ROW EXECUTE FUNCTION tauro_validar_rectificacion_vencimiento();

CREATE OR REPLACE FUNCTION tauro_operador_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'El libro de operadores es append-only: no modificar ni borrar historia'; END;
$$ LANGUAGE plpgsql;
DO $$ DECLARE tabla text; BEGIN
    FOREACH tabla IN ARRAY ARRAY['condiciones_operador','vencimientos_operador',
        'pagos_operador','aplicaciones_operador','verificaciones_operador',
        'reversiones_pago_operador','reversiones_aplicacion_operador',
        'rectificaciones_vencimiento_operador'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_operador_inmutable ON %I',tabla);
        EXECUTE format('CREATE TRIGGER trg_operador_inmutable BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION tauro_operador_append_only()',tabla);
    END LOOP;
END $$;
