-- ============================================================
-- Migración: dinero de REAL (float4) a NUMERIC(14,2)
-- ============================================================
-- Por qué: REAL es punto flotante; SUM() de muchos montos acumula
-- error de redondeo y la cuenta corriente puede no cerrar por centavos.
-- NUMERIC es aritmética decimal exacta, el tipo correcto para plata.
--
-- Cómo aplicar (deliberadamente, NO en cada boot):
--   psql "$DATABASE_URL" -f scripts/migrar_dinero_numeric.sql
-- o pegarlo en el panel de migración del admin / consola de Railway.
--
-- Es idempotente: cada bloque solo migra si la columna todavía es 'real',
-- así que correrlo dos veces no reescribe las tablas de nuevo.
-- Hacer un backup/snapshot de la DB antes de correrlo.
-- ============================================================

DO $$
DECLARE
    col RECORD;
    -- columnas de dinero (tabla, columna, precisión)
    money_cols TEXT[][] := ARRAY[
        ['pagos','monto_ars','14,2'],
        ['envios','monto_ars','14,2'],
        ['cotizaciones','costo_fedex_usd','14,2'],
        ['cotizaciones','precio_final_usd','14,2'],
        ['cotizaciones','precio_final_ars','14,2'],
        ['cotizaciones','markup_valor','14,4'],
        ['solicitudes_guia','valor_declarado_usd','14,2'],
        ['solicitudes_guia','precio_tauro_ars','14,2'],
        ['solicitudes_guia','precio_tauro_usd','14,2'],
        ['solicitudes_guia','precio_cliente_final_ars','14,2'],
        ['clientes','markup_valor','14,4']
    ];
    i INT;
BEGIN
    FOR i IN 1 .. array_length(money_cols, 1) LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = money_cols[i][1]
              AND column_name = money_cols[i][2]
              AND data_type = 'real'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN %I TYPE NUMERIC(%s)',
                money_cols[i][1], money_cols[i][2], money_cols[i][3]
            );
            RAISE NOTICE 'Migrada %.% a NUMERIC(%)', money_cols[i][1], money_cols[i][2], money_cols[i][3];
        END IF;
    END LOOP;
END $$;

-- Nota: markup_pct queda como REAL a propósito (es un porcentaje, no un saldo
-- que se acumule). Se puede migrar también si se prefiere consistencia total.
