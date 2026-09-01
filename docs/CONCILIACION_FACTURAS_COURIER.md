# Conciliación de facturas courier

## Objetivo

Cruzar cada guía emitida por TAURO con las líneas que DHL, FedEx, Andreani u
OCA facturan después de la entrega. El sistema conserva por separado:

1. El precio que el cliente aceptó al generar la guía.
2. El costo final documentado por el courier.
3. El precio final calculado y el ajuste propuesto al cliente.

La base del portal es la fuente de verdad. Gmail y Excel/Google Sheets serán
entradas o vistas operativas, no registros financieros maestros.

## Fórmula vigente

`precio final = costo courier real + margen TAURO protegido`

`ajuste cliente = precio final - precio inicial aceptado`

Ejemplo: costo estimado `$5.000`, margen protegido `$5.000` y precio inicial
`$10.000`. Si el courier termina facturando `$15.000`, el precio final es
`$20.000` y se propone un débito de `$10.000`.

El cálculo sólo crea un ajuste `PROPUESTO`. No modifica `envios`, no cambia el
saldo del cliente y no aplica un cobro sin aprobación humana.

## Tablas

- `envio_cotizacion_snapshots`: costo estimado, precio aceptado, margen,
  tipo de cambio, peso y bultos. Una fila inmutable por solicitud.
- `facturas_courier`: cabecera FC, NC o ND, archivo/evidencia y deduplicación
  por courier, tipo y número normalizado.
- `facturas_courier_items`: líneas de flete, combustible, impuestos, etc. El
  tracking puede repetirse dentro de una factura.
- `factura_courier_item_matches`: asignación total o parcial de una línea a
  una guía, con método, confianza y confirmación.
- `conciliaciones_envio`: resultado versionado, pesos cotizado/facturado,
  motivo de diferencia y estado de revisión.
- `ajustes_cliente`: débito o crédito propuesto, aprobado y eventualmente
  aplicado mediante una clave idempotente.
- `auditoria_facturas_courier`: evidencia financiera permanente de cada paso.

## Controles obligatorios

- Los importes usan `NUMERIC` y los cálculos Python usan `Decimal`.
- FC y ND suman costo; NC resta y nunca se registra como FC pagable.
- Un mismo número de factura no puede duplicarse por espacios o guiones.
- El mismo PDF no puede entrar dos veces con documentos diferentes.
- El match automático exige igualdad de courier y tracking normalizado.
- La suma asignada entre matches no puede superar la línea original.
- Los documentos financieros no se borran; se anulan o rechazan.
- El precio final debe respetar la fórmula de margen también en PostgreSQL.
- Aprobar una conciliación exige evidencia completa e identidad del operador.
- La aplicación al saldo sólo existe como aprobación manual en ADMIN.

## Flujo previsto

1. Al aceptar una cotización, guardar su snapshot junto con la solicitud.
2. Recibir el correo del courier y conservar PDF, ID de mensaje y hash.
3. Extraer la factura a una estructura común y registrar cabecera e ítems.
4. Proponer matches exactos por `courier + tracking`.
5. Revisar excepciones, repartos, pesos y conceptos sin tracking.
6. Confirmar los matches válidos.
7. Calcular conciliación y ajuste propuesto.
8. Aprobar en ADMIN y recién entonces aplicar el débito o crédito.
9. Mostrar al cliente precio inicial, peso facturado, motivo y precio final.

## Estado de implementación

Ya están implementados el snapshot automático para envíos nuevos, la carga
manual de factura PDF con líneas pegadas desde Excel, el match exacto, la
bandeja ADMIN, la aprobación humana, el movimiento separado en cuenta
corriente y la vista de precio/peso final en el portal del cliente.

Los envíos históricos sin información suficiente quedan marcados como
`BASE_PENDIENTE`: ADMIN debe informar el costo estimado original; el sistema
no lo infiere ni lo inventa.

La automatización pendiente es el conector de ingreso desde correo: descargar
el adjunto de Gmail, extraer las líneas a la misma estructura y dejarlo en la
misma bandeja. Ese conector tampoco deberá aprobar diferencias por sí solo.
