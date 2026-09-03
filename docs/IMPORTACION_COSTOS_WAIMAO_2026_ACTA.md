# Acta de importación de costos iniciales WAIMAO 2026

Fecha de ejecución: 3 de septiembre de 2026, aproximadamente 00:12 ART.

## Objetivo

Completar la base interna de los envíos históricos de WAIMAO que aparecían como “Sin base” en `/admin/conciliacion-couriers`, sin inferir costos ni confundir la cotización inicial con la factura final del courier.

## Fuente y criterio contable

Se usó una copia XLSX autenticada del libro madre `TAURO 2026`, hoja `ENVIOS 2026`. SHA-256 de la copia analizada: `bea54116c533abed95e9f2e57c6ddec93c8eb54130fbf52b4dcced2ce59e9319`.

La hoja define:

- `FACTURADO`: importe cobrado al cliente.
- `SALDO ARS`: importe real de la factura del courier.
- `COSTOINICIAL`: costo interno al momento de cotizar.

Sólo se tomó `COSTOINICIAL` de filas WAIMAO con concepto `FLETE`, tracking no vacío y valor mayor que cero. `SALDO ARS` no se convirtió en snapshot porque hacerlo reescribiría retrospectivamente la cotización con el costo final. Las filas `TAX` tampoco generan un snapshot de flete.

## Preflight

La base productiva no tenía otra columna ni tabla de importación con costos históricos para estos envíos. El dry-run encontró:

- 32 trackings con costo inicial documentado.
- 3 snapshots ya existentes y compatibles centavo a centavo.
- 29 snapshots faltantes y aptos para importar.
- 14 envíos activos sin `COSTOINICIAL` en la hoja; se dejaron pendientes y no se inventó un costo.
- 0 conflictos de tracking, cargo, precio o snapshot.

## Respaldo

Antes de escribir se generó el dump restaurable:

`/Users/leanrmollo/Documents/TAURO/.codex_tmp/tauro_prompt_20260903/tauro_prod_pre_waimao_costos_20260903.dump`

SHA-256: `7bda290b73c0d7f0688463227ecde710556c93dc7227f5ac719d6b2a846cdd27`.

También se creó en producción `codex_backup_snapshots_waimao_20260903`, con los 5 snapshots WAIMAO existentes antes de esta carga.

## Ejecución y resultado

Se ejecutó `scripts/importar_costos_waimao_2026.py` con `--aplicar`. Cada fila fue registrada por el escritor canónico de snapshots, con:

- moneda `ARS` y tipo de cambio 1;
- costo tomado de `COSTOINICIAL`;
- precio tomado de la solicitud productiva;
- margen calculado determinísticamente como precio menos costo;
- fecha aceptada, peso y bultos conservados desde la solicitud;
- `origen_calculo.fuente = IMPORT_SHEET_2026`;
- SHA-256, hoja, fila y tracking de la evidencia en el origen;
- un evento de auditoría `SNAPSHOT_COTIZACION_REGISTRADO` por alta.

Resultado productivo: 29 snapshots insertados y 29 eventos de auditoría. Un segundo dry-run devolvió 0 candidatos y 32 snapshots compatibles, confirmando idempotencia.

La verificación visual de `/admin/conciliacion-couriers` recorrió los 32 trackings con costo conocido: los 32 muestran “Costo est.” y “Margen protegido”; ninguno conserva “Sin base”. Los 14 sin evidencia inicial siguen pendientes deliberadamente.

## Reversión

Los snapshots financieros son inmutables y no deben borrarse manualmente. Ante una reversión justificada, restaurar la base completa desde el dump o preparar una intervención auditada específica comparando primero contra `codex_backup_snapshots_waimao_20260903`. No ejecutar un `DELETE` directo en producción.

