# Cuenta corriente por ámbito

Estado: implementación en preparación. No habilitar en producción hasta
completar la migración, la conciliación histórica y las pruebas de aceptación.

## Objetivo

Cada cliente debe poder controlar tres vistas coherentes de su cuenta con
TAURO:

- **Nacional**: cargos y pagos imputados a operaciones dentro de Argentina.
- **Internacional**: cargos y pagos imputados a importaciones y exportaciones.
- **Consolidado**: control general de todos los cargos menos todos los pagos
  aprobados.

La interfaz debe mostrar también dos importes que no se pueden ocultar ni
repartir automáticamente:

- **Crédito sin imputar**: parte de pagos aprobados que todavía no fue
  asignada a Nacional o Internacional.
- **Cargos sin clasificar**: historia sin evidencia suficiente para decidir el
  ámbito.

## Regla contable

Un cargo activo es un **Debe**. Un pago aprobado e imputado es un **Haber** del
ámbito correspondiente. Un pago pendiente nunca modifica un saldo.

```text
Debe consolidado = Debe nacional + Debe internacional + Cargos sin clasificar
Haber consolidado = Haber nacional + Haber internacional + Crédito sin imputar
Saldo consolidado = Debe consolidado - Haber consolidado
Saldo nacional = Debe nacional - Haber nacional
Saldo internacional = Debe internacional - Haber internacional
```

Un saldo positivo es deuda del cliente. Un saldo negativo es saldo a favor.

## Imputación de pagos

Al informar o registrar un pago se puede indicar:

- Nacional.
- Internacional.
- Dividir entre ambos.
- Dejar sin imputar.

La elección del cliente es una solicitud. El pago y sus imputaciones sólo
impactan después de la aprobación de TAURO. El total imputado nunca puede
superar el monto del pago; el remanente queda visible como crédito sin
imputar. Los pagos históricos no se reparten automáticamente.

## Integridad y concurrencia

- Los cálculos monetarios usan `NUMERIC`/`Decimal`, no punto flotante.
- Una transacción bloquea el pago antes de crear o modificar imputaciones.
- La base rechaza una suma de imputaciones superior al comprobante incluso
  ante solicitudes simultáneas.
- Los movimientos sensibles dejan auditoría en la misma transacción.
- Un cargo manual nuevo exige ámbito explícito.
- La factura de una guía automática se adjunta al cargo existente; nunca crea
  un segundo Debe.
- Una FC es única en toda TAURO. Un cargo ya facturado no se cancela: cualquier
  reversión futura deberá ser una NC/asiento inmutable con evidencia.
- Pagos y cargos usan `ON DELETE RESTRICT`; desactivar un cliente no borra su
  historia financiera.
- Una operación con ámbito desconocido queda en cuarentena y no entra en un
  saldo Nacional o Internacional.
- La pertenencia del pago y del cargo se deriva de la base y de la sesión; no
  se confía en un `cliente_id` enviado por el navegador.

## Orden de release

Abrir una ventana de mantenimiento y bloquear **todas** las escrituras
financieras antes del paso 1. La aplicación anterior no puede seguir creando
pagos, cargos, facturas, clasificaciones ni cancelaciones durante el gate: una
escritura entre preflight y postflight volvería no determinista la
reconciliación. Mantener el bloqueo hasta terminar el paso 5 y desplegar la
versión nueva.

1. Tomar un snapshot restaurable de PostgreSQL y registrar su identificador.
2. Ejecutar y guardar el preflight read-only:

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
     -f scripts/preflight_cuenta_ambitos.sql > cuenta_ambitos_preflight.txt 2>&1
   ```

   Si la sección 7a informa una FC no vacía cuya normalización queda vacía,
   detener el release, resolverla con evidencia y repetir el preflight. Debe
   quedar en cero antes de validar `ck_envios_nro_fc_valida`; no aplicar un
   backfill automático.

3. Con las escrituras todavía bloqueadas y sin desplegar el código nuevo,
   migrar el dinero y aplicar el schema:

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
     -f scripts/migrar_dinero_numeric.sql
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/schema.sql
   ```

4. Ejecutar y guardar el postflight:

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
     -f scripts/postflight_cuenta_ambitos.sql > cuenta_ambitos_postflight.txt 2>&1
   ```

5. Comparar ambas salidas con el snapshot y aprobar el gate. Recién después
   desplegar la versión nueva del código y levantar la ventana de
   mantenimiento.

No ejecutar backfill automático entre estos pasos. Los ámbitos sin evidencia
permanecen `SIN_CLASIFICAR` y los pagos históricos aprobados permanecen como
crédito sin imputar hasta una intervención deliberada y documentada.

## Gate post-migración

Después de migrar y antes de habilitar producción, ejecutar en modo sólo
lectura `scripts/postflight_cuenta_ambitos.sql` y guardar su salida junto con
el snapshot restaurable y la salida de `scripts/preflight_cuenta_ambitos.sql`.
El Debe, Haber y Saldo consolidado de cada cliente deben coincidir exactamente
con el preflight; también deben compararse los conteos de cuarentena/conflictos
y la lista global de FC duplicadas. Tipos, tabla, índices, constraints y triggers
deben informar `OK`, las diferencias contables deben ser cero y los controles
de aplicaciones/estados/FC inválidas no deben devolver filas. En particular,
`ck_envios_nro_fc_valida` debe existir y estar validada.

Una diferencia o un objeto faltante bloquea el gate. El postflight no modifica
datos ni autoriza a repartir pagos históricos o reclasificar cargos: toda
resolución requiere evidencia, una intervención deliberada y una nueva corrida
completa de ambos controles sobre el snapshot correspondiente.

## Criterios de aceptación

- Veinte imputaciones concurrentes al mismo pago jamás exceden su monto.
- Un reintento no duplica cargos, pagos ni imputaciones.
- Facturar un cargo pendiente conserva exactamente monto y ámbito.
- Una FC repetida entre dos clientes es rechazada.
- Una FC no vacía conserva al menos un carácter alfanumérico al normalizarse.
- Un cliente no puede leer comprobantes, cargos ni movimientos de otro.
- Un pago pendiente aparece como “En revisión” y no altera ningún saldo.
- Nacional e Internacional muestran historiales paginados independientes.
- Consolidado reconcilia exactamente con ambas subcuentas, el crédito sin
  imputar y los cargos sin clasificar.
- Los formatos `5,5` y `5.5`, y los importes `100.000` y `100,000`, conservan
  la normalización común del proyecto sin perder centavos.
