# Control del negocio y vigilancia DHL

Estado: publicación autorizada el 17/09/2026 con «ok perfecto. publicalo».
Cambio aprobado: `6a776c4`; verificar el despliegue y las rondas reales después del push.
Rama: `codex/admin-control-20260917`. Base: `d88ffc08`.
Vista: `/admin/home`, protegida por la autenticación de Admin.

## Qué cambia

El inicio de Admin organiza cuatro áreas con acceso directo: atención hoy,
rentabilidad, cuentas corrientes y vigilancia. Las explicaciones ampliadas
usan las ayudas ⓘ existentes, accesibles con mouse, teclado y toque.

- Atención hoy: retenidos, pagos pendientes, guías sin cargo activo o con
  cargo pendiente, y cancelados/reemplazados que todavía tienen cargo.
- Rentabilidad: período seleccionable, acceso a los últimos 90 días,
  gráficos de cargos por cliente y margen confirmado mensual, y tabla por cliente.
- Cuentas: saldos actuales completos, deuda a cobrar, pagos por aprobar
  y créditos de clientes separados. Incluye clientes inactivos.
- Vigilancia: cliente, destinatario, referencia TAURO, ruta, guía, motivo
  del courier, antigüedad del seguimiento, último dato confirmado y último intento.
  Filtros de retenidos, seguimiento posterior, errores y rastreos atrasados.
  Cada guía permite abrir sus costos y el rastreo oficial del operador.

Las listas de vigilancia y excepciones de cargo tienen paginación independiente.
Los totales incluyen todas las filas, no sólo las 25 visibles. Los gráficos
por cliente muestran los ocho con más cargos y la tabla contiene todos.

## Reglas de gestión

| Indicador | Criterio |
| --- | --- |
| Cargos del período | Envíos con cargo ACTIVO, por fecha del cargo, más ajustes APLICADOS asociados. |
| Costo confirmado | Última versión de conciliación CERRADA con evidencia completa. Si se reabre, vuelve a provisional. |
| Margen confirmado | Cargos actuales de esos mismos envíos menos costos confirmados. |
| Margen porcentual | Margen confirmado / cargos de los envíos confirmados. Nunca divide por ventas de otra cohorte. |
| Margen estimado | Cargos actuales menos costo cotizado congelado; sólo envíos sin costo final confirmado. |
| Sin costo | No tiene costo final ni cotización guardada; no se presupone costo cero. |
| Cobertura | Cantidad de envíos con costo confirmado / cantidad de cargos activos del período. |
| Por cobrar | Suma de saldos positivos por cliente. No se compensa con créditos de otros clientes. |
| Crédito de clientes | Suma separada de saldos a favor. |
| Pago pendiente | No reduce deuda hasta estar aprobado. Un rechazado no se acredita. |
| Cancelado con cargo | Excepción visible para revisar. No borra el cargo ni crea un crédito automático. |

Los importes son NUMERIC/Decimal, sin cálculos monetarios por modelo ni float.
El panel lee una transacción REPEATABLE READ / READ ONLY; no modifica cuentas,
estados, facturas, pagos o ajustes al abrirse. Tampoco consulta el courier.
Se evita la antigua reparación de estados que realizaba el contador general
de solicitudes al cargar el inicio; otros flujos conservan su comportamiento.

El margen mostrado es **contribución por envío en los importes registrados**,
no utilidad neta: no descuenta estructura ni realiza una separación impositiva.
Las conciliaciones pueden incorporar FC y NC del operador según las reglas
existentes; el panel no vuelve a sumar documentos ni trata una NC como FC.

El filtro de fechas afecta únicamente Rentabilidad. Los ajustes posteriores
se atribuyen al envío original y los importes están actualizados a hoy.
Cuentas y operación siempre muestran el estado actual completo. Las cuentas
reflejan el libro real: una inconsistencia de estado no oculta un cargo.
Los clientes de prueba quedan excluidos; la rentabilidad y operación también
excluyen solicitudes de prueba. No se altera el libro para corregir datos de prueba.

## Vigilancia DHL

1. Un evento RETENIDO registra `tracking_vigilancia_desde`.
2. El envío sigue en vigilancia cuando vuelve a PROCESO_ENTREGA.
3. La ronda normal consulta todas las guías pendientes; una segunda ronda
   consulta sólo las vigiladas, 12 horas después.
4. Por defecto: **05:20 y 17:20, hora de Argentina**.
   `DHL_TRACKING_CRON_HOUR` y `DHL_TRACKING_CRON_MINUTE` siguen configurando
   la ronda normal; la segunda usa la misma configuración + 12 horas.
5. La fecha de última consulta limita a una consulta en cada media jornada
   argentina (00–12 / 12–24). Reinicios recuperan pendientes sin repetir una
   consulta ya realizada en esa ventana; ambas rondas comparten advisory lock.
6. Los errores también ocupan la ventana y conservan el último estado confirmado.
   Se distinguen último intento y último dato para no simular actualización.
7. ENTREGADO, CANCELADO y REEMPLAZADO detienen el rastreo normal/reforzado.
   Se conserva el inicio de vigilancia como historia. Una nueva guía emitida
   reinicia los datos; un cambio manual que mantiene el mismo tracking conserva
   el inicio de vigilancia.
8. El control existente de guías descartadas sigue separado y sin cambios.
9. No se consulta a DHL para clientes/guías de prueba.

Se conserva el límite configurado `DHL_TRACKING_DAILY_LIMIT` (1.000 por lote,
máximo 5.000) y se prioriza vigilancia. Si el volumen supera ese límite, debe
dimensionarse con la cuota del courier; el panel permite identificar atrasos.
Otros operadores se muestran con seguimiento manual; no se promete una API
de tracking que no está implementada.

Un rastreo DHL activo se señala sin actualización si pasaron 36 horas desde
el último dato confirmado; sin datos previos, desde emisión/creación.
Una guía recién emitida sin eventos todavía no se considera atrasada.
El campo se agrega de forma idempotente en schema.sql y forma parte de readiness.
Los retenidos existentes se incorporan usando su última observación conocida.
No se reconstruyen retenciones históricas que la base no registra.

Se corrigió además la detección de entrega para no confundir “not delivered”,
“undelivered” o una promesa de entrega futura con una entrega confirmada.
Una respuesta tardía de DHL no puede sobrescribir el tracking de otra guía
que se haya asignado al mismo expediente durante la consulta.

## Próxima etapa recomendada

| Prioridad | Control | Decisión que habilita |
| --- | --- | --- |
| 1 | Responsable, próxima acción y fecha compromiso por incidencia | Saber quién debe intervenir y qué gestión está vencida; guardar historial. |
| 1 | Antigüedad documental de cobranza con FC, vencimiento e imputaciones | Priorizar cobros; distinguir deuda vencida, sin vencer y sin documentación. |
| 1 | Calendario de FC/NC y pagos del operador | Planificar salida de caja sin usar cobros estimados como dinero disponible. |
| 2 | Margen por ruta, servicio, peso facturable y recargo | Detectar dónde revisar tarifas o condiciones comerciales. |
| 2 | Gastos operativos, tratamiento impositivo y cierre mensual | Pasar de contribución por envío a resultado neto validado por administración. |
| 2 | Alertas asignadas y reglas de escalamiento | Escalar retenciones, consultas fallidas y diferencias sin duplicar avisos. |
| 3 | Presupuesto, concentración comercial y comparación mensual | Medir crecimiento con margen, exposición por cliente y dependencia del courier. |

No conviene automatizar créditos, bloquear clientes ni enviar comunicaciones
por una alerta sin una política aprobada y evidencia. Esta versión muestra
las excepciones y conserva las acciones controladas que ya existen.

## Validación y publicación

QA aislado en `/Users/leanrmollo/Documents/TAURO/qa_admin_control_20260917`.
Pruebas con PostgreSQL real descartable y red externa bloqueada: ciclo completo
FC → diferencia propuesta → aplicada → NC → reapertura/cierre; margen,
saldo y cobertura; desconocidos, cancelados y datos de prueba; bloqueo entre
workers; límites de ventanas; errores; entrega; carrera de cambio de tracking;
migración repetible; autorización; render vacío; paginación y lectura sin reparación.
QA visual local en escritorio, 390 px y 320 px, con datos ficticios y sin
escrituras en producción ni consultas al courier.
Resultado final: **2.345 pruebas y 5 subpruebas aprobadas**, 33 advertencias
preexistentes. Se verificaron filtros, ayudas táctiles, tablas móviles y
fechas/importes completos a 320 px; sin desbordes horizontales del documento.

Antes de publicar: autorización explícita, fetch de main y resolver divergencia
si apareciera. Push a main despliega a producción automáticamente.
Después: verificar deploy/salud/readiness, dashboard autenticado, saldos sin
cambios y registro de los dos jobs. Confirmar ejecución real de cada ronda
con logs/fechas; las pruebas locales no acreditan una consulta productiva.
No crear envíos, retiros, pagos o NC para probar este cambio.
