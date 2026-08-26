# Preflight productivo — cuenta corriente por ámbito

Fecha: 2026-08-24  
Entorno: producción  
Modo: sólo lectura; no se modificó ninguna fila ni objeto de la base.

## Resultado ejecutivo

La migración es viable, pero todavía no está autorizada para ejecutarse. La
base es pequeña y la conciliación consolidada cierra exactamente. Antes del
corte se requiere un snapshot nuevo, ventana de mantenimiento y aprobación
explícita de producción.

## Universo y conciliación

| Control | Resultado |
|---|---:|
| Clientes | 6 |
| Cargos | 2 |
| Pagos | 1 |
| Cargos con monto inválido | 0 |
| Pagos con monto inválido | 0 |
| Debe consolidado | ARS 295.858,00 |
| Haber aprobado | ARS 150.000,00 |
| Saldo consolidado | ARS 145.858,00 |
| Grupos de FC duplicada | 0 |
| FC inválidas | 0 |
| Cargos huérfanos de cliente | 0 |
| Pagos huérfanos de cliente | 0 |

## Ámbitos históricos

- Los 2 cargos activos están todavía `SIN_CLASIFICAR`.
- Las 4 solicitudes históricas están todavía `SIN_CLASIFICAR`.
- No hay conflictos entre ámbito de cargo y solicitud.
- Las rutas de las solicitudes son internacionales: una DHL AR→US, una
  FedEx AR→US y dos FedEx con destino Estados Unidos y origen histórico vacío.
- Un cargo automático por ARS 145.858,00 está vinculado a la solicitud DHL
  AR→US. Existe evidencia suficiente para una clasificación internacional
  deliberada después de instalar los controles, pero no se hará un backfill
  automático.
- El cargo manual histórico por ARS 150.000,00 tiene un pago aprobado del
  mismo cliente y por el mismo importe. Su efecto neto es cero. Se conserva
  sin clasificar y el pago queda como crédito sin imputar hasta una decisión
  documentada; no se inventa un ámbito.

## Objetos y controles pendientes

- `pagos_aplicaciones` todavía no existe.
- Faltan los índices de idempotencia de pagos y cargos.
- Faltan el índice global de FC normalizada y sus checks asociados.
- Las FKs de `pagos` y `envios` hacia `clientes` usan `ON DELETE CASCADE`; el
  schema preparado las reemplaza por `ON DELETE RESTRICT` para preservar la
  historia financiera.
- `envios.monto_ars`, `pagos.monto_ars` y los precios principales ya son
  `NUMERIC`.
- `clientes.tope_deuda_ars` y `clientes.markup_nac_valor` seguían en `REAL`.
  La migración fue corregida para pasarlos a `NUMERIC(14,2)` y
  `NUMERIC(14,4)` respectivamente.

## Respaldo y gate

- No hay backup programado en Railway.
- El último backup manual visible tiene 2 días y 225 MB.
- Ese respaldo no reemplaza el snapshot inmediatamente anterior al corte.

Orden obligatorio:

1. Abrir ventana de mantenimiento y bloquear escrituras financieras.
2. Crear snapshot restaurable nuevo y registrar su identificador.
3. Guardar la salida completa del preflight.
4. Ejecutar la migración monetaria y el schema preparados.
5. Ejecutar postflight y exigir diferencia contable igual a cero.
6. Desplegar el código nuevo sólo si todos los controles quedan en `OK`.
7. Levantar mantenimiento y realizar pruebas de aceptación con admin y un
   cliente piloto.

Estado del gate: **BLOQUEADO hasta snapshot nuevo y aprobación explícita del
corte productivo**.
