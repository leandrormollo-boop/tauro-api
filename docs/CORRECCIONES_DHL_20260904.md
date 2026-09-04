# Correcciones de la revisión DHL

Fecha: 04/09/2026. Rama: `codex/dhl-invoice-intake`.
Base de los cambios: `7d3a117`.

## Alcance y resultado

H1, H2 y H3 corregidos localmente. Sin push, despliegue, conexión a Gmail ni
operaciones sobre datos productivos. El importador continúa siendo determinístico,
sin llamadas a IA por factura. Los tests usan datos sintéticos y schemas aislados.

### H1 — Revisión financiera obligatoria

- Registro separado e inmutable `revisiones_financieras_courier`: moneda, TC elegido,
  fuente, fundamento, actor, fecha y huellas documentales.
- Admin debe elegir y confirmar expresamente el respaldo. `DOCUMENTO` exige el
  mismo TC de las líneas originales; `COMPROBANTE` exige adjuntar un PDF privado
  de hasta 8 MB. No se inventa una política cambiaria ni se preselecciona un TC.
- El TC documental se muestra como referencia de sólo lectura. Original, ítems y
  matches documentales permanecen intactos. El cálculo usa el TC aprobado con
  Decimal y registra el ID de la aprobación en sus evidencias y huella.
- Sin aprobación durable se bloquean confirmar, calcular, aplicar y cerrar.
  Cambiar una bandera no permite eludir la revisión. También hay defensa SQL
  contra aprobación/cierre de cálculos anteriores a la revisión.
- Aprobar TC no genera movimientos; aplicar un ajuste sigue siendo otra acción.
- Al recalcular, sólo se reemplazan borradores anteriores a la aprobación
  financiera, conservándolos como ANULADOS junto con sus ajustes PROPUESTOS y
  auditoría. No se reemplazan conciliaciones aprobadas/reclamadas ni ajustes
  resueltos. Si falla el nuevo cálculo o la auditoría, revierte toda la operación.
- Una aprobación de TC ya registrada no puede sobrescribirse. Corregir una
  decisión financiera aprobada requiere un flujo específico futuro, no SQL manual.

### H2 — Versiones anteriores del lector

- La importación exige la versión vigente y la huella exacta presentada.
- La relectura permite una versión posterior, nunca una degradación.
- `historial_extracciones_dhl` conserva automáticamente cada extracción y su
  huella/versionado, incluso las anteriores a la migración. Es append-only.
- La evidencia original y las entradas ya importadas siguen inmutables.
- Si falla la relectura, no se pierde la extracción anterior ni se habilita su
  importación. La pantalla indica que hay que volver a leer el PDF.

### H3 — Errores operativos y errores del documento

- Saturación, falta de worker Linux, fallo de arranque, crash o respuesta inválida
  generan `LectorDHLNoDisponible` y estado `REINTENTAR`.
- No aumentan el contador de intentos documentales. Se conserva la evidencia y
  no hay reintentos automáticos ilimitados.
- Rechazos documentales estructurados del worker siguen en `REVISION_MANUAL`.
  Un timeout de procesamiento mantiene el límite y deriva a revisión manual.
- No se expone stderr del proceso ni se heredan credenciales.

## Verificación

- Suite completa final: **1592 tests y 5 subtests aprobados**, 30 advertencias
  existentes. Ejecutada con `DATABASE_URL` vacío, dotenv deshabilitado y una
  `TAURO_TEST_DATABASE_URL` local aislada.
- Pruebas de aprobación inválida, evidencia ausente, TC no finito, diferencias de
  TC, idempotencia y concurrencia, rollback, protección SQL e inmutabilidad.
- Se reprodujeron y corrigieron el recálculo de un borrador legacy, su rollback
  ante falla de auditoría, y la prohibición de anular conciliaciones reclamadas.
- Un TC respaldado que produce diferencia cero cierra sin crear un cargo.
- Migración real: schema obtenido de `git show 7d3a117:sql/schema.sql`, poblado con
  dos entradas sintéticas (una importada y otra pendiente). Se aplicó el nuevo
  schema dos veces: originales, huellas y estados preservados; dos revisiones
  históricas conservadas; importación vieja bloqueada y relectura vigente válida.
- QA de 4 estados en anchos 1440, 840 y 390 px: sin desborde de la página.
  Inspección visual de los formularios; las tablas mantienen desplazamiento
  interno cuando su contenido no cabe en móvil.
- `git diff --check` limpio.

## Segunda revisión externa y decisiones

Claude Code recibió sólo 11 archivos/extractos de código y tests sintéticos,
155858 caracteres, con herramientas deshabilitadas, modo seguro y sin persistencia
de sesión. No recibió PDFs reales, correo, credenciales ni acceso a producción.

- Modelo principal solicitado y verificado por métricas de respuesta:
  `claude-fable-5-1`, esfuerzo high. El CLI también registró Haiku; no se afirma uso
  exclusivo de Fable ni se atribuye a Haiku la revisión principal.
- El revisor no encontró un camino nuevo para confirmar/calcular/aplicar/cerrar
  sin revisión financiera, y consideró consistentes los controles H1/H2/H3.
- Se incorporaron sus observaciones posteriores: recálculo de borradores legacy,
  TC documental visible y separación de crashes del worker. Estas últimas
  modificaciones se verificaron localmente; no se afirma una tercera revisión.
- Su advertencia de migración por una extracción PARA_REVISION sin versión no
  corresponde al schema base real: éste ya tenía el CHECK de versión obligatoria.
  La migración desde ese schema, con datos sintéticos, pasó. No se inventaron
  versiones con COALESCE ni se debilitaron constraints para aceptar datos corruptos.
- Coste equivalente informado por CLI: USD 2.467882, `costBasis=list`. No es un
  comprobante de cargo adicional a la suscripción. No se compraron créditos.

Router TAURO: tarea `dhl-review-fixes`, arquitectura con impacto financiero y
seguridad; revisión crítica Sol/high, ejecución local permitida. Revisor externo
Fable elegido por pedido del usuario, validaciones determinísticas y controles
financieros conservados en el agente principal.

## Pendientes fuera de este cambio

Publicar requiere autorización separada. Gmail/OAuth y la ingesta desatendida
siguen pendientes; este corte prepara la carga manual supervisada desde Admin.
No cambia el criterio de redondeo del lector sin evidencia documental adicional.
