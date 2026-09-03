-- ============================================================
-- Preflight de estados contables y operativos
-- ============================================================
-- Ejecutar SÓLO LECTURA contra la base ANTES de desplegar un schema que
-- valida ck_solicitudes_guia_estado, ck_envios_estado y ck_pagos_estado.
-- Las consultas 1 a 3 deben devolver 0 filas. Si alguna devuelve datos, el
-- VALIDATE CONSTRAINT del arranque fallará nombrando el constraint y el
-- release no reemplazará al proceso sano (/health responde 503).
--
-- Este script no corrige nada: la decisión sobre cada fila es humana y
-- debe quedar registrada en un acta con el motivo.

-- 1) Estados operativos fuera de la máquina de estados.
SELECT 'solicitudes_guia' AS tabla, id, cliente_id, estado, created_at
  FROM solicitudes_guia
 WHERE estado IS NULL
    OR estado NOT IN (
        'SOLICITADO', 'EN_PROCESO', 'EMITIENDO',
        'VERIFICAR_COURIER', 'GUIA_LISTA', 'DESPACHADO',
        'ENTREGADO', 'REEMPLAZADO', 'CANCELADO'
    )
 ORDER BY id;

-- 2) Estados de cargo fuera de ACTIVO / CANCELADO / NC (incluye variantes
--    en minúscula o con espacios, que el código operativo ya no reconoce).
SELECT 'envios' AS tabla, id, cliente_id, estado, fecha, monto_ars
  FROM envios
 WHERE estado IS NULL
    OR estado NOT IN ('ACTIVO', 'CANCELADO', 'NC')
 ORDER BY id;

-- 3) Estados de pago fuera de PENDIENTE / APROBADO / RECHAZADO. NULL es
--    válido: equivale a APROBADO histórico.
SELECT 'pagos' AS tabla, id, cliente_id, estado, fecha, monto_ars
  FROM pagos
 WHERE estado IS NOT NULL
   AND estado NOT IN ('PENDIENTE', 'APROBADO', 'RECHAZADO')
 ORDER BY id;

-- 4) Inventario del legado de facturación por cargo. Informativo: estas
--    filas quedan de sólo lectura tras trg_proteger_fc_legacy_envios y no
--    entran al facturador por lote. No requiere acción para el despliegue.
SELECT COUNT(*)                                   AS cargos_con_fc_legacy,
       COUNT(*) FILTER (WHERE factura_pdf IS NOT NULL) AS con_pdf_legacy,
       MIN(fecha)                                 AS primera_fecha,
       MAX(fecha)                                 AS ultima_fecha
  FROM envios
 WHERE NULLIF(BTRIM(nro_fc), '') IS NOT NULL;
