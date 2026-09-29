# Incidente de aislamiento de Google Sheets en staging

## Resumen

Durante la validación de staging del 29 de septiembre de 2026, el servicio
heredó temporalmente la credencial de Google usada por producción. El proceso
de staging ejecutó el espejo programado sobre la pestaña
`PLATAFORMA_SIN_PII` y, como su base aislada no tenía envíos, la dejó vacía.

No se hizo ninguna corrección manual sobre producción. La siguiente ejecución
normal de producción repobló la pestaña desde su base de datos.

## Cronología UTC verificada en Railway

- `20:03:39`: staging registró `0 envío(s) espejados` y vació la pestaña.
- `20:20:41`: se inició el redeploy de staging sin la credencial heredada.
- `20:22:16`: producción registró `622 envío(s) espejados` y restauró la
  pestaña.
- `20:22:49`: el nuevo proceso de staging confirmó el espejo de Sheets
  apagado por falta de credenciales.

Ventana de impacto observada: aproximadamente **18 minutos y 37 segundos**.
La fuente PostgreSQL de producción no fue modificada por este job; el impacto
observado quedó limitado a la pestaña espejo.

## Contención y prevención

- Se retiró `GOOGLE_CREDENTIALS_JSON` del entorno de staging.
- Staging fue reiniciado mediante deployment
  `41b4943d-bb43-4aad-93fe-8affbdde5e99`, estado `SUCCESS`.
- El código ahora bloquea el job en `ENV=STAGING` incluso si alguien vuelve a
  copiar credenciales. El scheduler global no tiene bypass. Una ejecución
  one-shot autorizada exige además el opt-in separado
  `TAURO_STAGING_SHEET_SYNC_ENABLED=true`, una credencial y un Sheet exclusivos
  de staging.
- Se agregaron pruebas automatizadas para el comportamiento fail-closed y el
  opt-in explícito.

## Estado final

Restaurado y contenido. La pestaña quedó nuevamente con 622 envíos según el
log de producción, y el proceso activo de staging quedó sin acceso a Google
Sheets. El incidente se conserva como riesgo nuevo del release y no se
presenta como una validación sin impacto sobre producción.
