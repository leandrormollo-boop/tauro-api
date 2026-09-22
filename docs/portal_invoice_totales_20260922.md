# Invoice por valor total y alineación del formulario

Estado: preparado localmente; sin publicación ni emisión real de guías.
Base: `2e8930ddc98ec8603dde9d6d3497abe16791aaea`.

El cliente declara cantidad y total de cada artículo. Por ejemplo, 3 prendas por USD 100 conservan USD 100; el unitario de USD 33,333 que exige DHL no vuelve a multiplicarse para determinar el total. Se guarda `valor_total_usd` en `items_invoice`, con `Decimal` y dos decimales. Los formularios y solicitudes anteriores por unitario siguen admitidos.

Se envían los cuatro campos documentados por DHL: `preCalculatedLineItemTotalValue`, `invoice.preCalculatedTotalValues.preCalculatedTotalGoodsValue`, `preCalculatedTotalInvoiceValue` e `invoice.indicativeCustomsValues.totalWithImportDutiesAndTaxes`. La factura actual sólo declara mercadería, sin cargos adicionales ni estimaciones de impuestos. `price` usa tres decimales; ningún total declarado se reajusta para hacerlo coincidir con ese unitario. Se rechazan diferencias reales de un centavo en las nuevas declaraciones.

Fuente: [MyDHL OpenAPI 3.3.1](https://developer.dhl.com/sites/default/files/2026-07/dpdhl-express-api-3.3.1.yaml), la versión que usa el adaptador. También se revisó el changelog oficial sobre totales precalculados. La validación offline comprobó nombres, rutas, campos obligatorios, límites y `multipleOf: 0.001` del bloque exportDeclaration. No sustituye una prueba de aceptación en el ambiente de DHL.

La entrada de totales permite los formatos ES/EN que ya usa TAURO. Invoice y SmartNumber comparten ahora un único parser. Borradores anteriores convierten su unitario a total una vez; los errores preservan lo escrito. Confirmación y detalle muestran el total guardado. FedEx/UPS conservan el caso representable por su contrato actual de un artículo por caja con unitario exacto; cantidades comerciales distintas de las cajas, varios artículos o repartos no exactos requieren DHL, con bloqueo explícito previo a emitir.

Paquete separa catálogo de las seis medidas/datos y alinea los controles a 46 px. Invoice comparte alturas y márgenes. Se revisaron escritorio, 390 px, modo claro/oscuro, agregar/quitar cajas y artículos, navegación atrás y recuperación del borrador. Los ensayos usaron sólo datos ficticios y bloquearon operaciones de courier.

Validación: 2.396 pruebas Python y 5 subtests, 17 pruebas JavaScript; 24 pruebas enfocadas adicionales incluyendo validación offline contra el YAML oficial. Evidencia local en `qa_package_layout_20260922` del workspace padre. Sin cambios de base de datos, tarifas, autenticación ni despliegue.

Enrutamiento: router determinista `architecture` -> Terra/medium; elevado a Astra por tratar datos declarados y arquitectura (política local: mínimo Sol para revisión final). Sin delegación. Controles: cálculos deterministas, red externa bloqueada durante tests, DB local desechable, ningún dato personal en evidencia, ninguna guía real emitida. La publicación requiere aprobación explícita según la política del workspace.
