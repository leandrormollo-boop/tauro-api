# Diseño conjunto: facturación, imputación de pagos y diferencias

Fecha de diseño: 3 de septiembre de 2026.

Este documento fija el contrato común de las tareas 5, 6 y 7 antes de implementarlas. Las tres comparten los mismos cargos, ajustes y pagos; resolverlas por separado permitiría doble deuda o doble haber.

## Principios contables

1. `envios` sigue siendo el débito original. Emitir una factura TAURO sólo agrupa y documenta cargos existentes; no agrega otro debe.
2. Un `ajustes_cliente` aplicado ya forma parte del debe o haber. Incluirlo en una factura sólo lo documenta; no vuelve a impactar el saldo.
3. Un pago aprobado impacta el haber una sola vez por su monto total. Las aplicaciones explican a qué documentos se imputó; el remanente es crédito sin imputar.
4. Si un pago se aplicó a un cargo todavía no facturado y luego ese cargo entra en una factura, esa aplicación cuenta como pagado de la factura sin moverse ni duplicarse.
5. `envios.nro_fc`, `envios.factura_pdf` y `envios.factura_nombre` quedan como legado de sólo lectura. El flujo nuevo no los escribe.
6. Ningún total se deriva con `float`: formularios, servicios y SQL trabajan con `Decimal` y `NUMERIC`.

## Factura TAURO

`facturas_cliente` es la cabecera legal: cliente, FC/NC, punto de venta, número, CAE, fechas, período, subtotal, IVA, total, PDF, nombre, estado, fecha y autor. Los importes son `NUMERIC(14,2)`.

`facturas_cliente_items` contiene renglones que apuntan exactamente a un cargo (`envio_id`) o a una diferencia aplicada (`ajuste_id`). El monto del renglón es positivo; el signo contable surge del tipo de factura y del tipo de ajuste. La suma de ítems debe coincidir con el total y subtotal más IVA debe coincidir con total.

Una FC admite cargos activos y ajustes DEBITO aplicados. Una NC admite ajustes CREDITO aplicados. Una factura no puede mezclar ámbitos, clientes ni elementos ya documentados por otra factura EMITIDA.

La exclusividad no puede resolverse con un índice parcial simple porque el estado vive en la cabecera. Se asegura con triggers que bloquean la fila objetivo (`envios` o `ajustes_cliente`) antes de buscar otra factura emitida. Así dos transacciones concurrentes no pueden facturar el mismo elemento.

Anular una factura conserva cabecera, PDF e ítems. Libera sus elementos para una factura posterior, pero no borra historia ni modifica los cargos.

## Pagos por documento

`pagos_aplicaciones` incorpora `factura_id` y `envio_id`, ambos opcionales por compatibilidad. Una aplicación nueva apunta a exactamente uno. Las filas históricas con ambos nulos conservan su imputación por ámbito; el crédito nuevo sin imputar se representa como monto del pago menos la suma de aplicaciones, sin una fila artificial.

El ámbito se toma del cargo o de los ítems de la factura. Una factura sólo es imputable si todos sus renglones pertenecen al mismo ámbito. El trigger valida ownership, estado del pago, documento vigente, monto positivo, no sobreaplicación del pago y no sobrepago del documento.

En portal y admin se envían identificadores opacos `F:<id>` o `E:<id>`. El navegador muestra saldos, pero el servidor vuelve a leerlos con bloqueo. La asignación es determinística en el orden seleccionado: cubre cada documento hasta su saldo y deja el último parcial si el pago no alcanza. Si sobra, queda a favor.

Las aplicaciones de un pago informado por el cliente nacen `SOLICITADA`. Al aprobar el comprobante pasan a `APLICADA` en la misma transacción; rechazarlas no genera haber y elimina sólo la propuesta todavía no aplicada.

## Pagado y saldo de una factura

`pagado` de una factura EMITIDA es:

- aplicaciones APLICADA que apuntan directamente a la factura; más
- aplicaciones APLICADA previas que apuntan a cargos incluidos como ítems de esa factura.

Cada aplicación entra una sola vez. Los ítems de ajuste no heredan pagos del cargo base. `saldo = total - pagado`, nunca menor que cero por los controles de sobrepago.

## Diferencias visibles

El cliente ve exclusivamente la explicación comercial:

- peso cotizado;
- peso facturado por el courier;
- base de peso usada;
- diferencia trasladada en ARS;
- leyenda “TAURO traslada la diferencia del courier sin agregar margen”.

Para peso se usa `conciliaciones_envio`. Para recargos, impuestos, combustible, zona extendida u otros conceptos se agregan las descripciones documentadas en las líneas confirmadas de `facturas_courier_items`. No se seleccionan ni serializan costo interno, margen protegido o precio de compra.

## Compatibilidad productiva relevada

Al diseñar la migración, producción tenía 0 cargos con FC legacy, 594 cargos activos pendientes, 26 pagos, 25 aplicaciones APLICADA por ámbito y 253 ajustes aplicados, todos DEBITO. Por eso:

- no hace falta backfill automático de facturas;
- las 25 aplicaciones por ámbito deben preservarse;
- la restricción única actual `(pago_id, ambito)` debe reemplazarse antes de crear aplicaciones por documento;
- el primer flujo real puede comenzar con una FC de cargos internacionales de WAIMAO sin convertir documentación vieja.

## Secuencia de implementación

1. Crear cabeceras, ítems, constraints y servicio de facturación por lote.
2. Reemplazar la UI de factura por cargo por las pantallas de lote y agregar pestañas en portal.
3. Ampliar aplicaciones de pago, cálculos de saldos y formularios portal/admin.
4. Enriquecer las líneas de diferencia con pesos y concepto courier sin exponer costos.
5. Ejecutar tests de concurrencia e invariantes sobre PostgreSQL aislado cuando `TAURO_TEST_DATABASE_URL` esté disponible, además de la suite unitaria completa.
