# Acta de migración monetaria REAL → NUMERIC

Fecha de verificación: 2 de septiembre de 2026 (America/Argentina/Buenos_Aires).

Entorno verificado: Railway, proyecto `pleasing-youthfulness`, ambiente `production`, servicio PostgreSQL.

## Preflight

La consulta a `information_schema.columns` encontró quince columnas `REAL` y ninguna `DOUBLE PRECISION`. Catorce son porcentajes, pesos o dimensiones y deben conservar su tipo actual. La única columna monetaria todavía en punto flotante es:

- `productos.valor_usd_default`: `REAL`.

Las demás columnas monetarias incluidas por `scripts/migrar_dinero_numeric.sql` ya son `NUMERIC` en producción. El script fue ampliado para incluir `productos.valor_usd_default` y el esquema fresco ahora la crea como `NUMERIC(14,2)`.

## Backups

Railway Hobby no permite crear un snapshot nuevo: el panel reserva Backups/PITR para el plan Pro. Con autorización del responsable se reemplazó ese control por dos copias restaurables independientes y una copia puntual dentro de PostgreSQL, sin mostrar credenciales:

- Backup lógico equivalente a `/admin/backup.json`, generado con el mismo servicio `servicios.backup.generar_backup_json`: `tauro_prod_pre_numeric_20260902_193736.json`, 2.041.729 bytes, SHA-256 `c14c81deb3c36a37e523739d93243670dd267c3ffb7de8db9669b23dc446fc9e`.
- Dump PostgreSQL custom restaurable: `tauro_prod_pre_numeric_20260902_193736.dump`, 761.178 bytes, validado con `pg_restore --list`, SHA-256 `8a3ffc60b46dedb85ae8d681e834787fcd202d95522924acbc573946ab12b812`.
- Snapshot puntual en producción: tabla `codex_backup_productos_valor_usd_20260902`, con las dos filas existentes de `productos` y sus valores previos.

Los archivos se conservan fuera del repositorio en `/Users/leanrmollo/Documents/TAURO/.codex_tmp/tauro_prompt_20260902/` y no se versionan porque contienen información privada del negocio.

## Ejecución

Estado: EJECUTADA.

Salida real del 2 de septiembre de 2026:

`Migrada productos.valor_usd_default a NUMERIC(14,2)`

El log completo quedó en `migracion_numeric_20260902.log`, SHA-256 `480131161f3224694e816bcae46570b0603f95c4eb5fedd39a8849516a8ca1d0`, junto a los respaldos externos al repositorio.

## Postflight

- `productos.valor_usd_default` quedó como `NUMERIC(14,2)`.
- La comparación por `productos.id` contra el snapshot puntual dio cero valores distintos.
- La consulta sobre las catorce columnas monetarias administradas por el script dio cero columnas `REAL` o `DOUBLE PRECISION`.
- El dump y el JSON fueron validados antes de ejecutar el `ALTER`.

La ruta `/admin/migracion` se conserva para operación controlada, pero se retiró del menú lateral.
