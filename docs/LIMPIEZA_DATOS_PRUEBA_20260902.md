# Acta de limpieza reversible de datos de prueba

Fecha: 2 de septiembre de 2026 (America/Argentina/Buenos_Aires).

La limpieza se ejecutó sin borrar historia. Antes del cambio se generaron:

- `tauro_prod_pre_tarea2_20260902.dump`, dump PostgreSQL custom validado con `pg_restore --list`, SHA-256 `48699b045dd195992136b7f65211ade858119cef384e5110f19d513ccfbc7954`.
- `tauro_prod_pre_tarea2_20260902.json`, backup lógico del servicio TAURO, SHA-256 `f71db7e65a34719834ff1f66b6c24287d210449546415324c1aebc7037a2deb1`.
- Tablas puntuales `codex_backup_clientes_tarea2_20260902`, `codex_backup_solicitudes_tarea2_20260902` y `codex_backup_solicitud_52_tarea2_20260902` en producción.

Resultado:

- `TEST_CLIENT`, `SANDRA`, `LEANDRO MOLLO` y `PRETE ROSSO` quedaron con `test=true`. Las dos últimas fueron incluidas porque tenían reglas internacionales fijas de ARS 91.000.000 y ARS 100.000.000. Ninguna cuenta ni movimiento fue eliminado.
- Solicitudes #2 y #3: `CANCELADO`, `test=true`.
- Solicitudes WAIMAO #4 (`eee`) y #51 (`PRUEBA`): `CANCELADO`, `test=true`; sus cargos ya estaban cancelados.
- La regla de consistencia detectó además la solicitud WAIMAO #52, tracking `9802908161`, todavía `GUIA_LISTA` con cargo cancelado. Se tomó snapshot, se cambió a `CANCELADO` y se registró `sistema.solicitud_cancelada_por_cargo` en `security_audit`.
- Postflight: tres clientes activos operativos, cero solicitudes activas con cargo cancelado y cero pruebas WAIMAO visibles según la nueva regla de portal.

El script idempotente es `scripts/limpiar_datos_prueba_20260902.sql`. La aplicación conserva las rutas históricas de Tracking FedEx, pero quita el acceso del menú y reemplaza errores técnicos por mensajes genéricos.
