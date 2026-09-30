# Validación de migraciones PostgreSQL 17

Fecha: 2026-09-29  
Motor: `postgres:17-alpine` (`17.11`)  
Origen de upgrade: `origin/main` en `06b5bc9`

Las pruebas se ejecutaron contra bases locales aisladas dentro del contenedor
`tauro-pr28-pg17`; no se conectaron a staging ni a producción.

## Fresh e idempotencia

Base aislada: `tauro_migrate_fresh`.

1. `python scripts/migrate_database.py` sobre base vacía: **OK**.
2. El mismo comando, sin recrear la base: **OK**.

Ambas ejecuciones terminaron con:

```text
[db] Schema inicializado OK.
[migrate] Schema, API keys y secretos OAuth listos.
```

## Upgrade desde main

Base aislada: `tauro_upgrade_main`.

1. Se aplicó `sql/schema.sql` leído directamente desde `origin/main`.
2. Se ejecutó `python scripts/migrate_database.py` con el código de la rama.
3. La verificación de readiness contable y e-commerce terminó en **OK**.

Resultado final:

```text
[db] Schema inicializado OK.
[migrate] Schema, API keys y secretos OAuth listos.
```

Resultado global: fresh, segunda ejecución idempotente y upgrade desde main,
todos con código de salida `0`.

