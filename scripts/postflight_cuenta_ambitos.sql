-- ==============================================================
-- Postflight READ-ONLY — cuenta corriente Nacional/Internacional
-- ==============================================================
-- Ejecutar después de migrar y antes de habilitar la funcionalidad.
-- Todas las sentencias ejecutables de este archivo son SELECT/WITH: no
-- corrigen, reclasifican, imputan ni eliminan datos.
--
-- Evidencia obligatoria del gate:
--   1. Conservar el snapshot restaurable y la salida completa de
--      scripts/preflight_cuenta_ambitos.sql.
--   2. Guardar la salida de este archivo con fecha y base consultada.
--   3. Comparar por cliente debe_consolidado, haber_consolidado y
--      saldo_consolidado con la sección 6 del preflight.
--   4. Comparar cuarentenas/conflictos con las secciones 3, 5 y 8, y
--      duplicados de FC con la sección 7 del preflight.
--   5. No habilitar producción ante un objeto FALTANTE/REVISAR, una fila en
--      los controles que esperan cero, o una diferencia contable distinta
--      de 0. Las cuarentenas se resuelven sólo con evidencia; este script no
--      propone ni ejecuta backfill automático.
-- ==============================================================

-- 1) Todas las columnas monetarias de la migración deben ser NUMERIC con la
-- precisión indicada. Todas las filas deben informar OK.
WITH esperadas(tabla, columna, precision_esperada, escala_esperada) AS (
    VALUES
        ('pagos', 'monto_ars', 14, 2),
        ('envios', 'monto_ars', 14, 2),
        ('pagos_aplicaciones', 'monto_ars', 14, 2),
        ('cotizaciones', 'costo_fedex_usd', 14, 2),
        ('cotizaciones', 'precio_final_usd', 14, 2),
        ('cotizaciones', 'precio_final_ars', 14, 2),
        ('cotizaciones', 'markup_valor', 14, 4),
        ('solicitudes_guia', 'valor_declarado_usd', 14, 2),
        ('solicitudes_guia', 'precio_tauro_ars', 14, 2),
        ('solicitudes_guia', 'precio_tauro_usd', 14, 2),
        ('solicitudes_guia', 'precio_cliente_final_ars', 14, 2),
        ('clientes', 'markup_valor', 14, 4)
)
SELECT e.tabla,
       e.columna,
       c.data_type,
       c.numeric_precision,
       c.numeric_scale,
       CASE
         WHEN c.column_name IS NULL THEN 'FALTANTE'
         WHEN c.data_type = 'numeric'
          AND c.numeric_precision = e.precision_esperada
          AND c.numeric_scale = e.escala_esperada THEN 'OK'
         ELSE 'REVISAR'
       END AS estado_control
FROM esperadas e
LEFT JOIN information_schema.columns c
  ON c.table_schema = 'public'
 AND c.table_name = e.tabla
 AND c.column_name = e.columna
ORDER BY e.tabla, e.columna;

-- 2) Estructura de pagos_aplicaciones. Debe existir y conservar las columnas
-- declaradas; el detalle permite archivar los defaults/nullability reales.
SELECT 'pagos_aplicaciones' AS objeto,
       CASE WHEN TO_REGCLASS('public.pagos_aplicaciones') IS NOT NULL
            THEN 'OK' ELSE 'FALTANTE' END AS estado_control;

SELECT column_name, data_type, is_nullable, column_default,
       numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'pagos_aplicaciones'
ORDER BY ordinal_position;

-- 3) Índices necesarios para aislamiento, paginación e idempotencia. Todas
-- las filas deben informar OK; indexdef queda como evidencia de su forma.
WITH esperados(indice) AS (
    VALUES
        ('idx_pagos_cliente'),
        ('idx_pagos_fecha_desc'),
        ('idx_pagos_aplicaciones_pago'),
        ('idx_envios_cliente'),
        ('idx_envios_cliente_estado'),
        ('idx_envios_cliente_ambito_fecha'),
        ('uq_envios_solicitud'),
        ('uq_pagos_cliente_idempotency'),
        ('uq_envios_cliente_idempotency'),
        ('uq_envios_fc_normalizada')
)
SELECT e.indice,
       COALESCE(i.tablename, '—') AS tabla,
       i.indexdef,
       CASE
         WHEN i.indexname IS NULL THEN 'FALTANTE'
         WHEN e.indice = 'uq_envios_fc_normalizada'
          AND (UPPER(i.indexdef) NOT LIKE 'CREATE UNIQUE INDEX%'
               OR i.tablename <> 'envios'
               OR UPPER(i.indexdef) LIKE '%CLIENTE_ID%'
               OR UPPER(i.indexdef) NOT LIKE '%REGEXP_REPLACE%'
               OR UPPER(i.indexdef) NOT LIKE '%<> ''NC''%')
           THEN 'REVISAR_INDICE_GLOBAL'
         ELSE 'OK'
       END AS estado_control
FROM esperados e
LEFT JOIN pg_indexes i
  ON i.schemaname = 'public'
 AND i.indexname = e.indice
ORDER BY e.indice;

-- 4) Constraints contables. Todos los booleanos deben ser true y las
-- restricciones CHECK deben estar validadas.
SELECT
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.contype = 'p'
    ) AS primary_key_ok,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.contype = 'f'
          AND PG_GET_CONSTRAINTDEF(c.oid) LIKE
              '%FOREIGN KEY (pago_id) REFERENCES pagos(id)%'
    ) AS pago_fk_ok,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.contype = 'u'
          AND PG_GET_CONSTRAINTDEF(c.oid) LIKE '%UNIQUE (pago_id, ambito)%'
    ) AS pago_ambito_unique_ok,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.conname = 'ck_pagos_aplicaciones_ambito'
          AND c.convalidated
    ) AS ambito_check_ok,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.conname = 'ck_pagos_aplicaciones_monto'
          AND c.convalidated
    ) AS monto_positivo_check_ok,
    EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
          AND c.conname = 'ck_pagos_aplicaciones_estado'
          AND c.convalidated
    ) AS estado_check_ok;

SELECT c.conname,
       c.contype,
       c.convalidated,
       PG_GET_CONSTRAINTDEF(c.oid) AS definicion
FROM pg_constraint c
WHERE c.conrelid = TO_REGCLASS('public.pagos_aplicaciones')
ORDER BY c.contype, c.conname;

-- 4b) Cada libro contable debe tener exactamente UNA FK canónica, validada y
-- RESTRICT: cliente_id -> clientes(cliente_id). No se confía en el nombre de
-- la constraint. Una FK adicional se informa como FK_EXTRA_INSEGURA; una FK
-- única pero no validada, no RESTRICT o sobre otras columnas exige REVISAR.
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
), inventario AS (
    SELECT e.tabla,
           c.oid,
           c.conname,
           c.confdeltype,
           c.convalidated,
           PG_GET_CONSTRAINTDEF(c.oid) AS definicion,
           (
               c.confrelid = e.clientes_oid
               AND c.conkey = ARRAY[e.cliente_attnum]::smallint[]
               AND c.confkey = ARRAY[e.clientes_cliente_attnum]::smallint[]
               AND c.confdeltype = 'r'
               AND c.convalidated
           ) AS canonica_segura
    FROM esperadas e
    LEFT JOIN pg_constraint c
      ON c.conrelid = e.tabla_oid
     AND c.contype = 'f'
), resumen AS (
    SELECT tabla,
           COUNT(oid) AS total_fks,
           COUNT(oid) FILTER (WHERE canonica_segura) AS fks_canonicas_seguras,
           COALESCE(
               ARRAY_AGG(conname::text ORDER BY conname)
                   FILTER (WHERE oid IS NOT NULL),
               ARRAY[]::text[]
           ) AS constraints_encontradas,
           COALESCE(
               ARRAY_AGG(definicion ORDER BY conname)
                   FILTER (WHERE oid IS NOT NULL),
               ARRAY[]::text[]
           ) AS definiciones,
           COALESCE(
               ARRAY_AGG(confdeltype::text ORDER BY conname)
                   FILTER (WHERE oid IS NOT NULL),
               ARRAY[]::text[]
           ) AS acciones_delete
    FROM inventario
    GROUP BY tabla
)
SELECT tabla,
       total_fks,
       fks_canonicas_seguras,
       constraints_encontradas,
       definiciones,
       acciones_delete,
       CASE
         WHEN total_fks = 1 AND fks_canonicas_seguras = 1 THEN 'OK'
         WHEN total_fks > 1 THEN 'FK_EXTRA_INSEGURA'
         ELSE 'REVISAR'
       END AS estado_control
FROM resumen
ORDER BY tabla;

-- 4c) Una FC NULL o vacía sigue permitida; cualquier otro valor debe conservar
-- al menos un carácter alfanumérico al normalizar. La constraint debe existir
-- como CHECK y estar validada. La definición se conserva como evidencia.
SELECT 'ck_envios_nro_fc_valida' AS constraint_nombre,
       PG_GET_CONSTRAINTDEF(c.oid) AS definicion,
       c.convalidated,
       CASE
         WHEN c.oid IS NULL THEN 'FALTANTE'
         WHEN NOT c.convalidated THEN 'NO_VALIDADA'
         ELSE 'OK'
       END AS estado_control
FROM (SELECT 1) AS esperado
LEFT JOIN pg_constraint c
  ON c.conrelid = TO_REGCLASS('public.envios')
 AND c.conname = 'ck_envios_nro_fc_valida'
 AND c.contype = 'c';

-- 5) Triggers de concurrencia/integridad. Deben existir, estar habilitados y
-- apuntar a las tablas esperadas. La definición se guarda para comparación.
WITH esperados(trigger_nombre, tabla) AS (
    VALUES
        ('trg_validar_pago_aplicacion', 'pagos_aplicaciones'),
        ('trg_validar_pago_con_aplicaciones', 'pagos')
), actuales AS (
    SELECT t.tgname,
           cl.relname AS tabla,
           t.tgenabled,
           PG_GET_TRIGGERDEF(t.oid) AS definicion
    FROM pg_trigger t
    JOIN pg_class cl ON cl.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = cl.relnamespace
    WHERE n.nspname = 'public'
      AND NOT t.tgisinternal
)
SELECT e.trigger_nombre,
       e.tabla,
       a.tgenabled,
       a.definicion,
       CASE
         WHEN a.tgname IS NULL THEN 'FALTANTE'
         WHEN a.tgenabled = 'D' THEN 'DESHABILITADO'
         ELSE 'OK'
       END AS estado_control
FROM esperados e
LEFT JOIN actuales a
  ON a.tgname = e.trigger_nombre
 AND a.tabla = e.tabla
ORDER BY e.trigger_nombre;

-- 6) La suma de TODAS las aplicaciones de un pago nunca puede superar el
-- comprobante. También denuncia aplicaciones huérfanas. Debe devolver 0 filas.
WITH totales AS (
    SELECT pago_id, SUM(monto_ars) AS total_aplicaciones
    FROM pagos_aplicaciones
    GROUP BY pago_id
)
SELECT t.pago_id,
       p.cliente_id,
       p.monto_ars AS monto_pago,
       t.total_aplicaciones,
       t.total_aplicaciones - p.monto_ars AS exceso_ars,
       CASE WHEN p.id IS NULL THEN 'PAGO_INEXISTENTE'
            ELSE 'SUPERA_PAGO' END AS motivo
FROM totales t
LEFT JOIN pagos p ON p.id = t.pago_id
WHERE p.id IS NULL
   OR t.total_aplicaciones > p.monto_ars
ORDER BY t.pago_id;

-- 7) Coherencia de estados. APLICADA exige pago APROBADO; SOLICITADA exige
-- pago PENDIENTE. Los estados NULL históricos cuentan como APROBADO sólo para
-- el primer caso. Debe devolver 0 filas.
SELECT pa.id AS aplicacion_id,
       pa.pago_id,
       p.cliente_id,
       pa.ambito,
       pa.monto_ars,
       pa.estado AS estado_aplicacion,
       p.estado AS estado_pago,
       CASE
         WHEN p.id IS NULL THEN 'PAGO_INEXISTENTE'
         WHEN pa.estado = 'APLICADA'
          AND COALESCE(p.estado, 'APROBADO') <> 'APROBADO'
           THEN 'APLICADA_SIN_PAGO_APROBADO'
         WHEN pa.estado = 'SOLICITADA'
          AND p.estado IS DISTINCT FROM 'PENDIENTE'
           THEN 'SOLICITADA_SIN_PAGO_PENDIENTE'
       END AS motivo
FROM pagos_aplicaciones pa
LEFT JOIN pagos p ON p.id = pa.pago_id
WHERE p.id IS NULL
   OR (pa.estado = 'APLICADA'
       AND COALESCE(p.estado, 'APROBADO') <> 'APROBADO')
   OR (pa.estado = 'SOLICITADA'
       AND p.estado IS DISTINCT FROM 'PENDIENTE')
ORDER BY pa.pago_id, pa.id;

-- 8) Reconciliación exacta por cliente. Sólo cargos activos positivos y pagos
-- aprobados impactan, igual que en el ledger. Las tres diferencias deben ser
-- 0 y estado_control debe ser OK para cada cliente.
WITH cargos AS (
    SELECT cliente_id,
           COALESCE(SUM(monto_ars), 0) AS debe_consolidado,
           COALESCE(SUM(monto_ars) FILTER (
               WHERE ambito = 'NACIONAL'), 0) AS debe_nacional,
           COALESCE(SUM(monto_ars) FILTER (
               WHERE ambito = 'INTERNACIONAL'), 0) AS debe_internacional,
           COALESCE(SUM(monto_ars) FILTER (
               WHERE ambito IS NULL
                  OR ambito NOT IN ('NACIONAL', 'INTERNACIONAL')), 0)
               AS cargos_sin_clasificar
    FROM envios
    WHERE estado NOT IN ('CANCELADO', 'NC')
      AND monto_ars > 0
    GROUP BY cliente_id
), aplicaciones_por_pago AS (
    SELECT pago_id, COALESCE(SUM(monto_ars), 0) AS aplicado
    FROM pagos_aplicaciones
    WHERE estado = 'APLICADA'
    GROUP BY pago_id
), aplicaciones_por_cliente AS (
    SELECT p.cliente_id,
           COALESCE(SUM(pa.monto_ars) FILTER (
               WHERE pa.ambito = 'NACIONAL'), 0) AS haber_nacional,
           COALESCE(SUM(pa.monto_ars) FILTER (
               WHERE pa.ambito = 'INTERNACIONAL'), 0) AS haber_internacional
    FROM pagos_aplicaciones pa
    JOIN pagos p ON p.id = pa.pago_id
    WHERE pa.estado = 'APLICADA'
      AND COALESCE(p.estado, 'APROBADO') = 'APROBADO'
    GROUP BY p.cliente_id
), pagos_por_cliente AS (
    SELECT p.cliente_id,
           COALESCE(SUM(p.monto_ars), 0) AS haber_consolidado,
           COALESCE(SUM(
               p.monto_ars - COALESCE(app.aplicado, 0)
           ), 0) AS credito_sin_imputar
    FROM pagos p
    LEFT JOIN aplicaciones_por_pago app ON app.pago_id = p.id
    WHERE COALESCE(p.estado, 'APROBADO') = 'APROBADO'
    GROUP BY p.cliente_id
), base AS (
    SELECT c.cliente_id,
           COALESCE(cg.debe_consolidado, 0) AS debe_consolidado,
           COALESCE(cg.debe_nacional, 0) AS debe_nacional,
           COALESCE(cg.debe_internacional, 0) AS debe_internacional,
           COALESCE(cg.cargos_sin_clasificar, 0) AS cargos_sin_clasificar,
           COALESCE(ap.haber_nacional, 0) AS haber_nacional,
           COALESCE(ap.haber_internacional, 0) AS haber_internacional,
           COALESCE(pg.credito_sin_imputar, 0) AS credito_sin_imputar,
           COALESCE(pg.haber_consolidado, 0) AS haber_consolidado
    FROM clientes c
    LEFT JOIN cargos cg ON cg.cliente_id = c.cliente_id
    LEFT JOIN aplicaciones_por_cliente ap ON ap.cliente_id = c.cliente_id
    LEFT JOIN pagos_por_cliente pg ON pg.cliente_id = c.cliente_id
), control AS (
    SELECT b.*,
           debe_consolidado
             - (debe_nacional + debe_internacional + cargos_sin_clasificar)
             AS diferencia_debe,
           haber_consolidado
             - (haber_nacional + haber_internacional + credito_sin_imputar)
             AS diferencia_haber,
           (debe_consolidado - haber_consolidado)
             - ((debe_nacional - haber_nacional)
                + (debe_internacional - haber_internacional)
                + cargos_sin_clasificar - credito_sin_imputar)
             AS diferencia_saldo
    FROM base b
)
SELECT cliente_id,
       debe_consolidado,
       debe_nacional,
       debe_internacional,
       cargos_sin_clasificar,
       diferencia_debe,
       haber_consolidado,
       haber_nacional,
       haber_internacional,
       credito_sin_imputar,
       diferencia_haber,
       debe_consolidado - haber_consolidado AS saldo_consolidado,
       diferencia_saldo,
       CASE WHEN diferencia_debe = 0
              AND diferencia_haber = 0
              AND diferencia_saldo = 0
            THEN 'OK' ELSE 'REVISAR' END AS estado_control
FROM control
ORDER BY cliente_id;

-- 9) Conteos de cuarentena. No autorizan autoimputación ni reclasificación:
-- comparar con preflight/snapshot y resolver sólo contra evidencia documental.
WITH aplicaciones_por_pago AS (
    SELECT pago_id, COALESCE(SUM(monto_ars), 0) AS aplicado
    FROM pagos_aplicaciones
    WHERE estado = 'APLICADA'
    GROUP BY pago_id
), cuarentena AS (
    SELECT e.cliente_id,
           'CARGO_SIN_CLASIFICAR'::text AS categoria,
           e.id AS origen_id,
           e.monto_ars AS monto_ars
    FROM envios e
    WHERE e.estado NOT IN ('CANCELADO', 'NC')
      AND e.monto_ars > 0
      AND (e.ambito IS NULL
           OR e.ambito NOT IN ('NACIONAL', 'INTERNACIONAL'))

    UNION ALL

    SELECT e.cliente_id,
           'CONFLICTO_CARGO_SOLICITUD',
           e.id,
           e.monto_ars
    FROM envios e
    JOIN solicitudes_guia s ON s.id = e.solicitud_id
    WHERE e.estado NOT IN ('CANCELADO', 'NC')
      AND UPPER(TRIM(COALESCE(e.ambito, '')))
          IN ('NACIONAL', 'INTERNACIONAL')
      AND UPPER(TRIM(COALESCE(s.ambito, '')))
          IN ('NACIONAL', 'INTERNACIONAL')
      AND UPPER(TRIM(e.ambito)) <> UPPER(TRIM(s.ambito))

    UNION ALL

    SELECT p.cliente_id,
           CASE WHEN COALESCE(app.aplicado, 0) = 0
                THEN 'PAGO_APROBADO_TOTALMENTE_SIN_IMPUTAR'
                ELSE 'PAGO_APROBADO_PARCIALMENTE_SIN_IMPUTAR' END,
           p.id,
           p.monto_ars - COALESCE(app.aplicado, 0)
    FROM pagos p
    LEFT JOIN aplicaciones_por_pago app ON app.pago_id = p.id
    WHERE COALESCE(p.estado, 'APROBADO') = 'APROBADO'
      AND p.monto_ars > COALESCE(app.aplicado, 0)
)
SELECT cliente_id,
       categoria,
       COUNT(*) AS cantidad,
       SUM(monto_ars) AS monto_ars,
       ARRAY_AGG(origen_id ORDER BY origen_id) AS origen_ids
FROM cuarentena
GROUP BY cliente_id, categoria
ORDER BY cliente_id, categoria;

-- 10) FC potencialmente duplicadas globalmente en TAURO. Debe coincidir con
-- la sección 7 del preflight; no borrar ni reasignar en automático.
SELECT REGEXP_REPLACE(UPPER(TRIM(nro_fc)), '[^A-Z0-9]', '', 'g')
           AS fc_normalizada,
       COUNT(*) AS cantidad,
       ARRAY_AGG(id ORDER BY id) AS envio_ids,
       ARRAY_AGG(DISTINCT cliente_id ORDER BY cliente_id) AS clientes,
       SUM(monto_ars) AS total_ars
FROM envios
WHERE COALESCE(UPPER(BTRIM(estado)), '') <> 'NC'
  AND REGEXP_REPLACE(
        UPPER(BTRIM(COALESCE(nro_fc, ''))), '[^A-Z0-9]', '', 'g'
      ) <> ''
GROUP BY REGEXP_REPLACE(UPPER(TRIM(nro_fc)), '[^A-Z0-9]', '', 'g')
HAVING COUNT(*) > 1
ORDER BY fc_normalizada;

-- 11) Defensa en profundidad de ck_envios_nro_fc_valida. Debe devolver 0
-- filas incluso si una instalación heredada tuvo la constraint sin validar.
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
