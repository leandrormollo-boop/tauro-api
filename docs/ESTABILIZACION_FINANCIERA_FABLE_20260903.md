# Tauro Solutions Ar — estabilización financiera

Fecha: 3 de septiembre de 2026.

Estado: primer corte implementado y verificado localmente. **No desplegado, no enviado a revisión de Tiendanube.**

## Trabajo realizado

- Los rechazos conocidos de PostgreSQL por facturación duplicada, partidas incompatibles, totales o imputaciones se traducen a errores de negocio. Los fallos técnicos desconocidos siguen llegando al handler técnico, sin copiar detalles SQL al formulario.
- El alta manual registra un cargo, no una factura. La documentación se realiza en el facturador por lote. Un formulario antiguo con número de factura o PDF se rechaza antes de crear deuda; no se descarta el comprobante silenciosamente.
- El servicio antiguo de facturación por cargo fue retirado. Los enlaces antiguos redirigen al facturador nuevo.
- Un trigger impide crear o modificar números, PDFs y nombres de facturas legacy. Los comprobantes históricos siguen siendo legibles y no se borraron columnas ni registros históricos.
- Se agregaron controles SQL para estados de cargos y pagos. El control de estados operativos de solicitudes ya existía en esta base; se reforzó su validación y su comprobación de disponibilidad junto con los demás controles.
- Las reglas de precio ausentes o inválidas dejan de convertirse silenciosamente en 25%, cero o factor uno. Se conserva el 0% o importe fijo cero cuando están explícitamente configurados. Sin una regla válida, el permiso efectivo de cotizar/emitir queda cerrado.
- El cotizador exige un tipo de cambio válido en `config`; ya no cae a un dólar fijo de 1450. Web y checkout manejan esa indisponibilidad sin publicar tarifas inventadas.
- El actualizador del dólar también exige una referencia válida y explícita para controlar saltos. Sin referencia no actualiza ni envía una falsa alerta calculada contra 1450.

No se cambiaron porcentajes comerciales existentes ni se aplicaron cargos, pagos, NC o ajustes a clientes reales.

## Verificación

Entorno: PostgreSQL 18.6 temporal, accesible exclusivamente por socket local; datos sintéticos y schemas independientes. `DATABASE_URL` vacía y carga de `.env` desactivada en los comandos de pruebas.

| Control | Resultado |
| --- | --- |
| Suite antes de los cambios | 1.331 pruebas aprobadas + 5 subtests |
| Suite final completa con PostgreSQL | **1.363 pruebas aprobadas + 5 subtests, 0 fallas, 0 omitidas** |
| Revisión independiente agregada | 29 casos aprobados, 9 de ellos con PostgreSQL |
| Revisión independiente sin URL de pruebas | 20 aprobados; los 9 de PostgreSQL se omiten expresamente, sin intentar una conexión por defecto |
| `git diff --check` | Sin errores |

Los 30 avisos de la suite son deprecaciones de Pydantic ya presentes en la corrida inicial; no son fallas de pruebas.

Los casos nuevos cubren, entre otros:

- dos intentos concurrentes de facturar un cargo, tanto por servicio como directamente por SQL;
- rechazo de un número documental repetido;
- rollback completo cuando el error aparece al confirmar la transacción, sin cabeceras ni renglones parciales;
- distinción entre un conflicto conocido y un diagnóstico técnico desconocido;
- repetición del schema conservando exactamente importe, número y PDF históricos;
- rechazo de estados inválidos y preservación de márgenes cero explícitos;
- formularios viejos y ausencia de tipo de cambio o referencia para su actualización.
- presentación y guardado del tope de deuda sin convertir importes grandes a notación científica.

Pruebas nuevas: `tests/test_financial_stabilization_review.py`. La conexión debe indicarse siempre mediante una `TAURO_TEST_DATABASE_URL` exclusivamente de pruebas: las fixtures crean y eliminan schemas sintéticos.

## Trazabilidad y controles

- Rama: `codex/fable-financial-stabilization`.
- Base: `c4dd402de2a8b3f75732919255f467db6d2c5405`.
- Router TAURO: arquitectura con impacto financiero y seguridad; ruta de revisión crítica, controles determinísticos y revisión independiente.
- Implementación inicial: Claude Code, modelo verificado `claude-fable-5-1`, por pedido explícito del usuario.
- Revisión independiente, correcciones de cierre y ejecución de pruebas: Codex. Se detuvo el worker antes de retomar las escrituras, manteniendo un solo escritor.
- No hubo commit, push, merge, despliegue ni cambios en bases remotas, cuentas de operadores o portales de tiendas.
- Se retiró únicamente el template de código obsoleto `templates/admin/facturar_cargo_form.html`; es recuperable desde Git. No se eliminaron documentos de clientes.

## Antes de desplegar

1. Integrar y revisar este corte con la rama de Tiendanube/OCA y con los demás cambios pendientes, sin sobrescribir sus modificaciones locales.
2. Con autorización de release, ejecutar primero `scripts/preflight_estados_contables.sql` en modo de sólo lectura. Sus consultas 1 a 3 deben devolver cero filas; la cuarta es un inventario informativo. No normalizar estados históricos automáticamente.
3. Verificar que las cuentas habilitadas tengan precios explícitos y un dólar válido en `config`. Una falta de configuración ahora bloquea la cotización deliberadamente.
4. Revisar cualquier importador histórico que aún intente escribir `envios.nro_fc` o sus PDFs: el flujo operativo nuevo los rechaza. Una migración histórica futura requiere un procedimiento específico, documentado y aprobado; no desactivar el trigger en producción para continuar a ciegas.
5. Repetir las pruebas sobre la versión integrada y autorizar expresamente el despliegue.

## Fuera de este corte

Los nuevos flujos de anulación/NC, la integración de la rama nacional, las pruebas reales con OCA/Andreani, la unificación completa de políticas de precios públicos y la homologación/publicación en Tiendanube siguen pendientes. La aprobación de la aplicación no se puede deducir de estas pruebas locales.
