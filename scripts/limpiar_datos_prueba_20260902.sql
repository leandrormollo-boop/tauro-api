-- Limpieza reversible de datos de prueba relevados el 2026-09-02.
-- Idempotente: conserva clientes, solicitudes, cargos y documentos.

BEGIN;

ALTER TABLE clientes
    ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE solicitudes_guia
    ADD COLUMN IF NOT EXISTS test BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE clientes
SET test=TRUE
WHERE cliente_id IN ('TEST_CLIENT', 'SANDRA', 'LEANDRO MOLLO', 'PRETE ROSSO')
  AND test IS DISTINCT FROM TRUE;

UPDATE solicitudes_guia
SET estado='CANCELADO',
    test=TRUE,
    updated_at=NOW()
WHERE (
       id IN (2, 3)
       OR (
       cliente_id='WAIMAO'
       AND UPPER(BTRIM(COALESCE(dest_nombre, ''))) IN ('PRUEBA', 'EEE')
       )
   )
  AND (estado <> 'CANCELADO' OR test IS DISTINCT FROM TRUE);

-- Reparación general equivalente a la regla del servicio. No marca como
-- prueba una guía real: sólo alinea su expediente con el cargo cancelado.
UPDATE solicitudes_guia s
SET estado='CANCELADO', updated_at=NOW()
WHERE s.estado NOT IN ('CANCELADO', 'REEMPLAZADO')
  AND EXISTS (
      SELECT 1 FROM envios e
      WHERE e.solicitud_id=s.id
        AND e.cliente_id=s.cliente_id
        AND e.estado='CANCELADO'
  );

COMMIT;

SELECT cliente_id, activo, test
FROM clientes
WHERE cliente_id IN ('TEST_CLIENT', 'SANDRA', 'LEANDRO MOLLO', 'PRETE ROSSO')
ORDER BY cliente_id;

SELECT id, cliente_id, estado, test, dest_nombre
FROM solicitudes_guia
WHERE id IN (2, 3, 4, 51, 52)
ORDER BY id;
