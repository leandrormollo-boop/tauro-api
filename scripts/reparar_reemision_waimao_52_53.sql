-- Reparación puntual e idempotente de la reemisión WAIMAO #52 -> #53.
--
-- Evidencia disponible al 2026-09-02:
-- * ambas solicitudes conservan el mismo precio ARS/USD y los mismos bultos;
-- * el dólar guardado en config es 1535 ARS/USD;
-- * la regla DHL de WAIMAO para costo > USD 190 suma USD 100;
-- * la hoja WAIMAO - TAURO SOLUTIONS todavía termina el 2026-08-13, por lo
--   que estas guías del 2026-09-01/02 no tienen una fila Sheet para importar.
--
-- Derivación determinística: 1116,70 - 100 = USD 1016,70 de costo;
-- 1016,70 * 1535 = ARS 1.560.634,50; margen protegido ARS 153.499,50.

BEGIN;
SELECT PG_ADVISORY_XACT_LOCK(520053);

CREATE TABLE IF NOT EXISTS codex_backup_solicitudes_52_53_20260902 AS
SELECT * FROM solicitudes_guia WHERE id IN (52, 53);
CREATE TABLE IF NOT EXISTS codex_backup_envios_52_53_20260902 AS
SELECT * FROM envios WHERE solicitud_id IN (52, 53);
CREATE TABLE IF NOT EXISTS codex_backup_snapshots_52_53_20260902 AS
SELECT * FROM envio_cotizacion_snapshots WHERE solicitud_id IN (52, 53);
CREATE TABLE IF NOT EXISTS codex_backup_reemisiones_52_53_20260902 AS
SELECT * FROM solicitudes_guia_reemisiones
 WHERE solicitud_anterior_id IN (52, 53) OR solicitud_nueva_id IN (52, 53);

DO $$
DECLARE
    anterior RECORD;
    nueva RECORD;
    cargo_anterior RECORD;
    cargo_nuevo RECORD;
BEGIN
    SELECT id, cliente_id, estado, courier, tracking, precio_tauro_ars,
           precio_tauro_usd, peso_kg, bultos, created_at
      INTO anterior
      FROM solicitudes_guia
     WHERE id = 52
     FOR UPDATE;
    SELECT id, cliente_id, estado, courier, tracking, precio_tauro_ars,
           precio_tauro_usd, peso_kg, bultos, created_at
      INTO nueva
      FROM solicitudes_guia
     WHERE id = 53
     FOR UPDATE;
    SELECT id, estado, monto_ars INTO cargo_anterior
      FROM envios WHERE solicitud_id = 52 FOR UPDATE;
    SELECT id, estado, monto_ars INTO cargo_nuevo
      FROM envios WHERE solicitud_id = 53 FOR UPDATE;

    IF anterior.id IS NULL OR nueva.id IS NULL
       OR anterior.cliente_id <> 'WAIMAO' OR nueva.cliente_id <> 'WAIMAO'
       OR UPPER(anterior.courier) <> 'DHL' OR UPPER(nueva.courier) <> 'DHL'
       OR anterior.tracking <> '9802908161' OR nueva.tracking <> '6781215324'
       OR anterior.estado <> 'CANCELADO' OR nueva.estado <> 'GUIA_LISTA'
       OR cargo_anterior.estado <> 'CANCELADO' OR cargo_nuevo.estado <> 'ACTIVO'
       OR ABS(anterior.precio_tauro_ars::NUMERIC - 1714134) > 0.02
       OR ABS(nueva.precio_tauro_ars::NUMERIC - 1714134) > 0.02
       OR ABS(anterior.precio_tauro_usd::NUMERIC - 1116.70) > 0.001
       OR ABS(nueva.precio_tauro_usd::NUMERIC - 1116.70) > 0.001
       OR ABS(cargo_anterior.monto_ars::NUMERIC - 1714134) > 0.02
       OR ABS(cargo_nuevo.monto_ars::NUMERIC - 1714134) > 0.02
       OR cargo_anterior.id IS NULL OR cargo_nuevo.id IS NULL THEN
        RAISE EXCEPTION 'La evidencia de #52/#53 cambió; abortar y volver a auditar';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM envio_cotizacion_snapshots snap
         WHERE snap.solicitud_id IN (52, 53)
           AND (
               snap.courier <> 'DHL'
               OR snap.moneda_courier <> 'USD'
               OR ABS(snap.tipo_cambio_ars - 1535) > 0.000001
               OR ABS(snap.costo_courier_estimado - 1016.70) > 0.0001
               OR ABS(snap.precio_cliente_inicial_ars - 1714134) > 0.02
           )
    ) THEN
        RAISE EXCEPTION 'Ya existe un snapshot diferente para #52/#53; no sobrescribir';
    END IF;

    INSERT INTO envio_cotizacion_snapshots (
        solicitud_id, coti_id, courier, servicio_courier,
        moneda_courier, tipo_cambio_ars,
        costo_courier_estimado, costo_courier_estimado_ars,
        precio_cliente_inicial_ars, margen_tauro_protegido_ars,
        markup_tipo, markup_valor,
        peso_real_cotizado_kg, peso_volumetrico_cotizado_kg,
        peso_facturable_cotizado_kg, bultos, origen_calculo, aceptado_at
    )
    SELECT s.id, NULL, 'DHL', s.servicio_courier,
           'USD', 1535.000000,
           1016.7000, 1560634.5000,
           1714134.0000, 153499.5000,
           'FIJO_ARS', 153500.0000,
           10.500, 20.218, 20.218, COALESCE(s.bultos, '[]'::jsonb),
           jsonb_build_object(
               'fuente', 'RECONSTRUCCION_PRECIO_2026',
               'evidencia', 'precio_usd - tramo_alto_usd; hoja_sin_fila',
               'precio_usd', 1116.70,
               'tramo_alto_usd', 100,
               'tipo_cambio_ars', 1535,
               'reparacion', 'WAIMAO_52_53_20260902'
           ),
           s.created_at
      FROM solicitudes_guia s
     WHERE s.id IN (52, 53)
       AND NOT EXISTS (
           SELECT 1 FROM envio_cotizacion_snapshots snap
            WHERE snap.solicitud_id = s.id
       );

    INSERT INTO solicitudes_guia_reemisiones (
        cliente_id, solicitud_anterior_id, solicitud_nueva_id,
        operacion, tracking_anterior, tracking_nuevo,
        campos_modificados, motivo, estado, completed_at
    ) VALUES (
        'WAIMAO', 52, 53, 'REEMPLAZO', '9802908161', '6781215324',
        '["valor_declarado_usd"]'::jsonb,
        'Corrección de invoice/valor declarado; relación reconstruida con evidencia',
        'EMITIDA', nueva.created_at
    )
    ON CONFLICT (solicitud_anterior_id) DO NOTHING;

    INSERT INTO security_audit (
        event, actor_type, actor_ref, success, metadata
    )
    SELECT
        'sistema.reemision_historica_reconstruida', 'sistema',
        'script:reparar_reemision_waimao_52_53', TRUE,
        jsonb_build_object(
            'solicitud_anterior_id', 52,
            'solicitud_nueva_id', 53,
            'tracking_anterior', '9802908161',
            'tracking_nuevo', '6781215324',
            'snapshots', jsonb_build_array(52, 53),
            'costo_inferido_usd', 1016.70,
            'tipo_cambio_ars', 1535
        )
    WHERE NOT EXISTS (
        SELECT 1 FROM security_audit
         WHERE event = 'sistema.reemision_historica_reconstruida'
           AND metadata->>'solicitud_anterior_id' = '52'
           AND metadata->>'solicitud_nueva_id' = '53'
    );
END $$;

DO $$
BEGIN
    IF (SELECT COUNT(*) FROM envio_cotizacion_snapshots
         WHERE solicitud_id IN (52, 53)) <> 2
       OR NOT EXISTS (
           SELECT 1 FROM solicitudes_guia_reemisiones
            WHERE solicitud_anterior_id = 52
              AND solicitud_nueva_id = 53
              AND estado = 'EMITIDA'
       )
       OR NOT EXISTS (
           SELECT 1 FROM envios
            WHERE solicitud_id = 52 AND estado = 'CANCELADO'
       )
       OR NOT EXISTS (
           SELECT 1 FROM envios
            WHERE solicitud_id = 53 AND estado = 'ACTIVO'
       ) THEN
        RAISE EXCEPTION 'Postflight de reemisión #52/#53 incompleto';
    END IF;
END $$;

COMMIT;
