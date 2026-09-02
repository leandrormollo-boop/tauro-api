# Acta de control — reemisión WAIMAO #52 → #53

Fecha de investigación: 2 de septiembre de 2026.

## Hallazgo

La solicitud #52 de WAIMAO emitió la guía DHL `9802908161` el 1 de septiembre. La solicitud #53 emitió `6781215324` el 2 de septiembre para corregir el valor declarado. Las dos conservan el precio comercial de ARS 1.714.134 / USD 1.116,70, pero fueron creadas sin `coti_id`, sin relación en `solicitudes_guia_reemisiones` y sin filas en `envio_cotizacion_snapshots`.

La limpieza operativa previa dejó #52 y su cargo en `CANCELADO`, y #53 con su cargo propio `ACTIVO`. No se reactivó ni se borró historia.

## Evidencia externa

Se abrió la hoja Google `WAIMAO - TAURO SOLUTIONS`, pestaña `ENVIOS 2026`, mediante la cuenta autenticada del propietario y se descargó una copia de solo lectura. La última guía registrada allí es del 13 de agosto de 2026. Por eso #52/#53, emitidas el 1–2 de septiembre, no tienen una fila Sheet desde la cual copiar costo.

## Reconstrucción determinística

La evidencia persistida permite reconstruir la base sin consultar una tarifa actual mutable:

- dólar almacenado: ARS 1.535 por USD;
- regla DHL efectiva de WAIMAO para costos mayores a USD 190: adicional USD 100;
- precio aceptado: USD 1.116,70 / ARS 1.714.134;
- costo inferido: USD 1.016,70;
- costo exacto convertido: ARS 1.560.634,50;
- margen protegido: ARS 153.499,50;
- peso real: 10,5 kg;
- peso volumétrico/facturable internacional: 20,2176 kg, persistido a tres decimales como 20,218 kg.

El origen del snapshot queda identificado como `RECONSTRUCCION_PRECIO_2026`, no como importación del Sheet ni como costo documental final del courier.

## Reparación

El script idempotente `scripts/reparar_reemision_waimao_52_53.sql`:

1. crea copias locales de preescritura y bloquea/valida IDs, cliente, courier, trackings, estados, cargos e importes exactos;
2. crea un snapshot propio para #52 y otro para #53 sólo si faltan;
3. reconstruye la relación #52 → #53 como reemisión emitida;
4. conserva #52/cargo anterior cancelados y #53/cargo nuevo activos;
5. agrega un evento auditable sin modificar ni borrar evidencia existente;
6. ejecuta postflight dentro de la misma transacción.

## Ejecución y postflight productivo

Ejecutado el 2 de septiembre de 2026 a las 20:15 ART. SHA-256 del script aplicado: `4b861c31987b83b7c0e3b72cb3a8faace0967aeddc96f54f4a6bbb3c1400b048`.

La copia Excel de solo lectura usada para controlar cobertura temporal del Sheet tuvo SHA-256 `e660d02a8bf4252fd7e13afa2a7f09be9935a81b31d0e10fb4633668fac78dfd`.

Resultado:

- snapshots #52 y #53 presentes con costo USD 1.016,7000, tipo de cambio 1.535, costo ARS 1.560.634,5000, precio ARS 1.714.134,0000 y margen ARS 153.499,5000;
- relación #52 → #53 en estado `EMITIDA`;
- dos solicitudes y dos cargos preservados en tablas de preescritura;
- un evento `sistema.reemision_historica_reconstruida`;
- verificación visual en `/admin/conciliacion-couriers`: `6781215324` y `9802908161` muestran costo y margen, no “Sin base”.

Estado de ejecución productiva: COMPLETADA Y VERIFICADA.
