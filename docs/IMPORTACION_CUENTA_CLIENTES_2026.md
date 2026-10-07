# Cuenta 2026 de MELCIOR y PRETE ROSSO desde sus planillas

Rama `claude/portal-clientes-historicos-20261001`, 01/10/2026. Se basa en
`codex/tax-differences-release-20260930` (PR #40: el portal muestra TAX y
diferencias en filas separadas).

## Problema

- **MELCIOR:** enero–agosto se importó con `importacion_historica_melcior`.
  - Ese lote **excluyó todas las filas TAX**.
  - Es anterior a las correcciones del 01/10: diferencias de mayo a julio, TAX,
    importes de agosto en $0, envíos faltantes, septiembre completo y el 7º pago.
  - El importador original está fijado a una huella, así que no puede volver a correr.
- **PRETE ROSSO:** sin importar. Codex dejó un importador local (no publicado)
  fijado a un manifiesto del 30/09, que ya quedó viejo.

## Solución: importador incremental

`servicios/importacion_cuenta_cliente.py` compara cada envío del manifiesto con
lo que ya existe en el portal y **agrega sólo lo que falta**.

| Caso | Acción |
|---|---|
| El envío no existe | Crea la solicitud histórica, el cargo y, si corresponde, la conciliación con su ajuste. |
| La posición de la fila la ocupa otro envío (se borraron o insertaron filas) | Se busca por tracking. Si hay que crearlo, usa la clave alterna `<source_key>#<tracking>`. |
| La posición la ocupa un envío que ya no está en la planilla (tracking corregido) | `CONFLICTO`: no se crea un duplicado. |
| Existe, cargo en $0 sin facturar ni imputar | Completa el cargo con el importe de la planilla. |
| Al total le falta diferencia o TAX | Nueva conciliación `CERRADA` (versión +1, `HISTORICO_CLIENTE_V1`) más ajuste `APLICADO` por el faltante. Separa diferencia de flete y TAX. |
| El portal cobra más que la planilla | `CONFLICTO`: no se toca. |
| El tracking es de otro cliente, o hay otro estado | `CONFLICTO`: no se toca. |
| Tracking repetido en la planilla (MELCIOR) | No se crea. Se informa para que Leandro decida. |
| Tracking repetido en la planilla (PRETE ROSSO) | Movimiento contable oculto, como en el lote de Codex. |
| Saldo 2025 | Por idempotencia. |
| Pagos | Se reconocen por clave e importe, o por fecha e importe (las filas de DETALLE se pueden correr). Si queda un pago del portal sin reconocer o hay un conflicto, no se agrega ningún pago y se informa. |
| Mismo tracking con otro destinatario | `CONFLICTO` (regla de Leandro: es un error de datos). |

Garantías:

- Nunca borra ni reduce.
- Las versiones de conciliación son **acumulativas**, igual que la conciliación courier: precio inicial
  fijo, diferencia y TAX totales. El movimiento del cliente (`ajustes_cliente`) es sólo lo nuevo.
- Costo y margen: el manifiesto trae el costo de TAURO 2026 (`costo_courier_ars`, SALDO o COSTOINICIAL
  del flete más el TAX). Si falta, se conserva el margen de la versión anterior. Nunca margen negativo.
- `observaciones` sólo lleva texto apto para el cliente: el portal lo muestra.
- La `source_key` es la posición de la fila. Una coincidencia por clave sólo se acepta si es el mismo
  envío (tracking, o destinatario si falta el tracking). Así, borrar filas en la planilla no cruza envíos.
- Una transacción por cliente, con un `SAVEPOINT` por fila.
- Control posterior: cada envío procesado queda con el total exacto de la planilla.
- El informe cierra contra la planilla completa: `diferencia_no_explicada_ars` debe ser 0.
- Vista previa: corre todo y revierte.
- Sin hash fijado en código: el manifiesto trae su `manifest_sha256` y se
  verifica su integridad. Para importar hay que escribir `IMPORTAR <CLIENTE>` y
  los 12 primeros caracteres de la huella que mostró la vista previa.

## Manifiestos

Se generan con `scripts/generar_manifiesto_cuenta_cliente_2026.py`. No se
versionan, porque contienen datos de clientes.

```bash
python3 scripts/generar_manifiesto_cuenta_cliente_2026.py --cliente MELCIOR \
  --planilla "MELCIOR 2026 TAURO.xlsx" --maestra "TAURO 2026.xlsx" --output melcior.json
python3 scripts/generar_manifiesto_cuenta_cliente_2026.py --cliente "PRETE ROSSO" \
  --planilla "PRETE ROSSO - TAURO 2026.xlsx" --maestra "TAURO 2026.xlsx" --output prete.json --incluir-ocultas
```

- **Fuente:** la planilla del cliente (lo facturado).
- **TAX:** se suman al envío con el mismo tracking. Si no hay flete, van a un ancla oculta.
- **TAURO 2026:** sólo completa la fecha faltante y el courier físico
  (BOXFLY/ORION → FEDEX, DHL → DHL). Nunca pisa importes. Ningún dato del operador llega al portal.
- **PRETE:** por defecto excluye filas ocultas, como hacía Codex. Para la carga del 01/10 se usa
  `--incluir-ocultas`: los 50 TAX ocultos de ENERO (FC 122) están sumados en el total de enero
  de la propia planilla.

## Operación (después del deploy)

1. Admin → Importaciones históricas → **Cuenta 2026 desde la planilla del cliente**.
2. Subir el manifiesto → **Previsualizar**. Revisar:
   - "Diferencia sin explicar" = 0;
   - conflictos;
   - filas repetidas;
   - pagos nuevos.
3. Volver a subir el mismo archivo, escribir `IMPORTAR MELCIOR` (o
   `IMPORTAR PRETE ROSSO`) y la huella → **Importar**.
4. Repetir con un manifiesto nuevo cada vez que cambie la planilla: sólo agrega la diferencia.

## Verificación hecha (Postgres 16 local con `sql/schema.sql`)

Simulación de producción: MELCIOR enero–agosto + cierre cargados con el importador original desde
la planilla del 01/10 a la mañana, con 6 pagos y los cargos de agosto en $0. El complemento
se corrió con la planilla de la tarde, después de borrar 4 filas duplicadas (3 en febrero y 1 en marzo):
las 3 filas de febrero que quedaron debajo se encuentran por tracking y no cambian.

| Cliente | Saldo antes | Saldo después | Planilla | No cargado | Sin explicar |
|---|---:|---:|---:|---:|---:|
| MELCIOR | 24.409.634,81 | 26.671.866,79 | 26.861.266,79 | 189.400,00 (2 filas repetidas de marzo, a decidir) | 0,00 |
| PRETE ROSSO | 0,00 | 17.101.486,79 | 17.101.486,79 | 0,00 | 0,00 |

- `cuenta_corriente.resumen_cuenta_por_ambito` da el mismo saldo.
- `movimientos_cuenta_paginados` muestra Flete, Diferencia de envío y TAX como filas separadas.
- La segunda ejecución sólo da `SIN_CAMBIOS`.
- Un cargo alterado a mano se informa como conflicto y no se modifica.

Tests: `tests/test_importacion_cuenta_cliente.py` (19: filas borradas e insertadas, tracking corregido,
otro destinatario, pagos corridos, versiones acumulativas, costo y notas internas) y
`tests/test_portal_raiz.py` (2).

## Cambio en el portal (`servicios/cuenta_corriente.py`)

Con dos versiones de conciliación en un mismo envío (por ejemplo, primero la relación y después el
archivo de diferencias), la cuenta mostraba la diferencia y el TAX **acumulados** en cada versión: el
cliente los veía dos veces. Ahora cada versión muestra lo que cambió respecto de la versión CERRADA
anterior, en la lista de movimientos y en el resumen del mes. Test:
`test_cuenta_del_cliente_muestra_solo_lo_nuevo_de_cada_version`.

## Extra

`/portal` respondía 404 porque no existe ruta raíz: el portal vive en
`/portal/home` y `/portal/login`. Se agregó una redirección 303 a `/portal/home`.
