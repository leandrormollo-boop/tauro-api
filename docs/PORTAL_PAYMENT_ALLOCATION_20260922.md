# Imputación de pagos desde Mi cuenta

Estado: implementado y probado localmente; pendiente de publicación.

## Recorrido del cliente

- Mi cuenta muestra «Imputar un pago» y el bloque «Pagos e imputaciones».
- Cada pago muestra primero su importe, referencia y estado; después, la cantidad de envíos vinculados y el remanente disponible.
- «Elegir envíos» permite vincular el importe todavía no asignado de un pago existente, tanto pendiente como aprobado.
- Al informar un pago nuevo, el selector de envíos está visible. Permite buscar por destinatario, guía o factura.
- La distribución se muestra por renglón en el orden del listado. El último documento puede recibir un pago parcial; el excedente queda a cuenta. Selecciones sin importe bloquean la confirmación en el formulario.
- Los pagos aprobados muestran «Pago imputado a X envíos»; los pendientes, «Imputación en revisión: X envíos». Las facturas se cuentan como facturas.
- Los pagos antiguos siguen accesibles mediante «Ver / imputar pago» en sus movimientos.

## Reglas contables y límites

No hay migración de esquema. Se utiliza `pagos_aplicaciones` y sus controles existentes.

La nueva operación sólo agrega aplicaciones al remanente: no vuelve a registrar el pago, no modifica su importe ni cambia su estado. El saldo consolidado no cambia al vincular un pago ya registrado. La aprobación de comprobantes sigue siendo responsabilidad del Admin.

Se bloquea el pago durante la transacción y se comprueba el remanente visto por el cliente. Una segunda confirmación o pestaña desactualizada no duplica la imputación. Se validan titularidad, estado, saldo disponible y duplicados. El acceso HTTP mantiene autenticación y protección de origen del portal.

No se admiten guías canceladas, reemplazadas, ocultas ni de prueba. Un envío facturado se paga seleccionando su factura, para conservar el arrastre contable. Las aplicaciones históricas, incluidas las asignadas sólo por ámbito, no se reasignan desde este formulario.

Las tarjetas conservan el límite de 8 pagos recientes y 24 aplicaciones visibles, pero cuentan y suman todas las aplicaciones. La página de un pago muestra el detalle completo.

## Validación

- Suite completa: 2.441 pruebas y 5 subpruebas aprobadas antes del último ajuste visual y los casos adicionales.
- Verificación focal final: 59 pruebas aprobadas, incluidos los 5 casos adicionales de conteos y guías excluidas.
- JavaScript: 23 pruebas aprobadas, incluidas 6 nuevas de distribución, centavos, pagos parciales, excedentes y búsqueda.
- PostgreSQL aislado: pago aprobado y pendiente, aprobación posterior, saldo consolidado invariable, reintento, concurrencia, propiedad de los documentos y selección inválida.
- Navegador local con datos sintéticos: pago aprobado de ARS 180.000 asignado a dos envíos; carga de ARS 90.000 con comprobante y dos solicitudes; pago pendiente de ARS 150.000 vinculado por ARS 130.000 conservando ARS 20.000 a cuenta. El saldo consolidado permaneció en ARS 220.000 durante las tres operaciones.
- Revisión visual móvil y comprobación de ancho sin desbordes en escritorio. Vista previa: `http://127.0.0.1:8787/portal/cuenta#pagos`.

No se utilizaron pagos reales ni integraciones externas durante las pruebas. No se publicó esta modificación.

Enrutamiento de riesgo: architecture, financial-impact y security-sensitive → mínimo Sol/high. Revisión e implementación por el agente principal Astra; sin delegación. Evidencia local en `qa_payment_allocation_20260922`.
