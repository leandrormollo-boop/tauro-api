# Railway — pre-deploy, healthcheck y config-as-code (hallazgo 08/10/2026)

## Qué pasó
Tras mergear `main` en `codex/pr28-hardening`, staging quedó en crash-loop:
`RuntimeError: Schema crítico no listo: solicitud_cotizacion_revisiones_existe,
revisiones_cotizacion_protegidas, recolecciones_origen_* …` → `main.py` aborta el
arranque (fail-closed, por diseño de `main`: el web no ejecuta DDL; las
migraciones corren en el **pre-deploy**).

El pre-deploy (`python scripts/migrate_database.py`) **nunca corrió en staging**:
el servicio de staging nunca usó Config as Code, y Railway lo deprecó
(28/08/2026: los servicios que nunca lo usaron ya no pueden optar). Por eso
`railway.json` (preDeployCommand, healthcheckPath, restartPolicy) es letra
muerta en staging. En el panel de staging: *Pre-deploy step* vacío,
*Healthcheck Path* vacío → Railway marcaba `success` sin healthcheck y a los
~20 s "Deployment failed" por el crash-loop.

Reproducido localmente: base con schema del 30/09 → mismo error; corriendo
`scripts/migrate_database.py` → readiness OK → la app arranca.

## Producción
El servicio de **production** leía `/railway.json` (healthcheck y restart
policy figuraban "set in /railway.json"), así que al mergear PR #36 el
`preDeployCommand` corrió en prod. Como Config as Code deja de funcionar el
2026-12-01, el 09/10/2026 se cargó lo mismo en el panel de production
(sin redeploy). Ya no depende de `railway.json`.

## Receta por entorno (panel → servicio tauro-api → Settings → Deploy)
| Campo | Valor |
|---|---|
| Pre-deploy step | `python scripts/migrate_database.py` |
| Healthcheck Path | `/health` |
| Healthcheck Timeout | `180` |
| Restart Policy | On Failure · 10 reintentos (default de Railway, no hace falta tocarlo) |

Staging (entorno `staging`, rama `codex/pr28-hardening`): cargado el 08/10/2026.
Production (entorno `production`, rama `main`): cargado el 09/10/2026.

Verificación sin entrar al panel (no imprime variables):
```bash
railway environment config --environment production --json | python3 -c 'import json,sys; c=json.load(sys.stdin); c=c.get("config",c); [print(k, s.get("deploy")) for k,s in c["services"].items()]'
```

## Orden correcto de un release con schema nuevo
1. Pre-deploy: `migrate_database.py` (schema.sql idempotente + backfills + readiness).
2. Arranque del web: `verificar_readiness_db()` sólo verifica; si falta algo, aborta
   y Railway conserva el deployment anterior (gracias al healthcheck).
3. Sin healthcheck configurado el paso 2 no protege: Railway corta tráfico al
   candidato roto. Por eso el healthcheck en el panel es obligatorio.
