-- ============================================================
-- Preflight READ-ONLY — cuenta corriente Nacional/Internacional
-- ============================================================
-- Ejecutar contra una copia/snapshot antes de aplicar la migración.
-- Este archivo no modifica datos: todas sus sentencias son SELECT.
-- Revisar y guardar la salida como evidencia del despliegue.
-- ============================================================

-- 1) Tipos monetarios actuales. En producción deben terminar en NUMERIC.
SELECT table_name, column_name, data_type, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (table_name, column_name) IN (
    ('pagos', 'monto_ars'),
    ('envios', 'monto_ars'),
    ('solicitudes_guia', 'precio_tauro_ars'),
    ('clientes', 'markup_nac_valor'),
    ('clientes', 'tope_deuda_ars')
  )
ORDER BY table_name, column_name;

-- 2) Montos inválidos o especiales. Debe devolver cero filas.
SELECT 'pagos' AS fuente, id, cliente_id, monto_ars::text AS monto
FROM pagos
WHERE monto_ars IS NULL
   OR monto_ars <= 0
   OR LOWER(monto_ars::text) IN ('nan', 'infinity', '-infinity')
UNION ALL
SELECT 'envios', id, cliente_id, monto_ars::text
FROM envios
WHERE monto_ars IS NULL
   OR monto_ars < 0
   OR LOWER(monto_ars::text) IN ('nan', 'infinity', '-infinity')
ORDER BY fuente, cliente_id, id;

-- 3) Distribución de cargos activos por ámbito. NULL/otro queda en cuarentena.
SELECT
    cliente_id,
    CASE
      WHEN UPPER(COALESCE(ambito, '')) IN ('NACIONAL', 'INTERNACIONAL')
        THEN UPPER(ambito)
      ELSE 'SIN_CLASIFICAR'
    END AS ambito_control,
    COUNT(*) AS cantidad,
    SUM(monto_ars) AS debe_ars
FROM envios
WHERE estado NOT IN ('CANCELADO', 'NC')
GROUP BY cliente_id, ambito_control
ORDER BY cliente_id, ambito_control;

-- 4) Cargos vinculados cuya solicitud permite un backfill seguro.
SELECT e.id AS envio_id, e.cliente_id, e.ambito AS ambito_cargo,
       s.id AS solicitud_id, s.ambito AS ambito_solicitud
FROM envios e
JOIN solicitudes_guia s ON s.id = e.solicitud_id
WHERE NULLIF(UPPER(TRIM(COALESCE(e.ambito, ''))), '') IS NULL
  AND UPPER(TRIM(COALESCE(s.ambito, ''))) IN ('NACIONAL', 'INTERNACIONAL')
ORDER BY e.cliente_id, e.id;

-- 5) Conflictos cargo/solicitud. Deben resolverse con evidencia, nunca pisarse.
SELECT e.id AS envio_id, e.cliente_id, e.ambito AS ambito_cargo,
       s.id AS solicitud_id, s.ambito AS ambito_solicitud
FROM envios e
JOIN solicitudes_guia s ON s.id = e.solicitud_id
WHERE UPPER(TRIM(COALESCE(e.ambito, ''))) IN ('NACIONAL', 'INTERNACIONAL')
  AND UPPER(TRIM(COALESCE(s.ambito, ''))) IN ('NACIONAL', 'INTERNACIONAL')
  AND UPPER(TRIM(e.ambito)) <> UPPER(TRIM(s.ambito))
ORDER BY e.cliente_id, e.id;

-- 6) Control consolidado previo por cliente. Es la cifra a reconciliar luego.
WITH debe AS (
    SELECT cliente_id, SUM(monto_ars) AS total
    FROM envios
    WHERE estado NOT IN ('CANCELADO', 'NC')
    GROUP BY cliente_id
), haber AS (
    SELECT cliente_id, SUM(monto_ars) AS total
    FROM pagos
    WHERE COALESCE(estado, 'APROBADO') = 'APROBADO'
    GROUP BY cliente_id
)
SELECT c.cliente_id,
       COALESCE(d.total, 0) AS debe_consolidado,
       COALESCE(h.total, 0) AS haber_consolidado,
       COALESCE(d.total, 0) - COALESCE(h.total, 0) AS saldo_consolidado
FROM clientes c
LEFT JOIN debe d ON d.cliente_id = c.cliente_id
LEFT JOIN haber h ON h.cliente_id = c.cliente_id
ORDER BY c.cliente_id;

-- 7a) FC con texto no vacío pero sin ningún carácter alfanumérico después de
-- normalizar. Debe devolver 0 filas antes de validar ck_envios_nro_fc_valida.
-- Resolver cada caso con evidencia; no convertirlo mediante backfill masivo.
SELECT id AS envio_id,
       cliente_id,
       nro_fc,
       estado
FROM envios
WHERE NULLIF(BTRIM(COALESCE(nro_fc, '')), '') IS NOT NULL
  AND REGEXP_REPLACE(
        UPPER(BTRIM(nro_fc)), '[^A-Z0-9]', '', 'g'
      ) = ''
ORDER BY cliente_id, id;

-- 7b) Facturas potencialmente duplicadas globalmente en TAURO (diagnóstico,
-- no borrado automático). Una misma FC no puede pertenecer a dos clientes.
SELECT REGEXP_REPLACE(UPPER(TRIM(nro_fc)), '[^A-Z0-9]', '', 'g') AS fc_normalizada,
       COUNT(*) AS cantidad,
       ARRAY_AGG(id ORDER BY id) AS envio_ids,
       ARRAY_AGG(DISTINCT cliente_id ORDER BY cliente_id) AS clientes,
       SUM(monto_ars) AS total_ars
FROM envios
WHERE COALESCE(UPPER(TRIM(estado)), '') <> 'NC'
  AND REGEXP_REPLACE(
        UPPER(TRIM(COALESCE(nro_fc, ''))), '[^A-Z0-9]', '', 'g'
      ) <> ''
GROUP BY REGEXP_REPLACE(UPPER(TRIM(nro_fc)), '[^A-Z0-9]', '', 'g')
HAVING COUNT(*) > 1
ORDER BY fc_normalizada;

-- 8) Pagos históricos aprobados. En el primer corte quedan SIN IMPUTAR.
SELECT cliente_id, COUNT(*) AS pagos,
       SUM(monto_ars) AS credito_sin_imputar_inicial
FROM pagos
WHERE COALESCE(estado, 'APROBADO') = 'APROBADO'
GROUP BY cliente_id
ORDER BY cliente_id;

-- 9) Inventario de TODAS las FKs existentes en los libros contables. Se
-- guarda nombre, definición y acción de borrado para detectar constraints
-- duplicadas o heredadas antes de aplicar el schema. La relación canónica es
-- exactamente cliente_id -> clientes(cliente_id).
WITH tablas(tabla) AS (
    VALUES ('pagos'), ('envios')
), esperadas AS (
    SELECT t.tabla,
           TO_REGCLASS('public.' || t.tabla) AS tabla_oid,
           TO_REGCLASS('public.clientes') AS clientes_oid,
           (
               SELECT a.attnum
               FROM pg_attribute a
               WHERE a.attrelid = TO_REGCLASS('public.' || t.tabla)
                 AND a.attname = 'cliente_id'
                 AND NOT a.attisdropped
           ) AS cliente_attnum,
           (
               SELECT a.attnum
               FROM pg_attribute a
               WHERE a.attrelid = TO_REGCLASS('public.clientes')
                 AND a.attname = 'cliente_id'
                 AND NOT a.attisdropped
           ) AS clientes_cliente_attnum
    FROM tablas t
)
SELECT e.tabla,
       c.conname AS constraint_nombre,
       PG_GET_CONSTRAINTDEF(c.oid) AS definicion,
       c.confdeltype,
       c.convalidated,
       CASE
         WHEN c.oid IS NULL THEN 'SIN_FK'
         WHEN c.confrelid = e.clientes_oid
          AND c.conkey = ARRAY[e.cliente_attnum]::smallint[]
          AND c.confkey = ARRAY[e.clientes_cliente_attnum]::smallint[]
           THEN 'CLIENTE_ID_A_CLIENTES'
         ELSE 'FK_NO_CANONICA'
       END AS relacion_control
FROM esperadas e
LEFT JOIN pg_constraint c
  ON c.conrelid = e.tabla_oid
 AND c.contype = 'f'
ORDER BY e.tabla, c.conname;
