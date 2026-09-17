# Navegación y envíos históricos — 17/09/2026

Estado: implementado y verificado localmente. Rama
`codex/navigation-history-20260917`, base `c1ad266`. Pendiente autorización
específica de publicación; no se modificaron registros de producción.

## Volver y continuar

- Cotizador internacional y nacional: conserva campos, cajas y edición de la
  ruta. Cerrar el cotizador o alternar ámbitos conserva el avance de cada uno.
- Nueva guía internacional: conserva remitente, destinatario, cajas, artículos,
  servicio elegido y paso. Atrás del navegador recorre los pasos; volver desde
  otra pantalla recupera el formulario y su posición. Las tarifas se recalculan.
- Admin: edición de pedidos, carga de guía ya emitida y carga manual de cargo
  recuperan sus campos y posición. La clave de idempotencia del cargo se conserva.
- «Empezar de nuevo» descarta el formulario actual con confirmación.
- Un error de validación mantiene lo ingresado. Un guardado confirmado limpia
  sólo ese borrador. Volver a la página anterior después del éxito no permite
  reenviar el formulario confirmado desde la caché del navegador.

Los borradores viven en `sessionStorage`, separados por cuenta y flujo, durante
cuatro horas de inactividad en esa pestaña. No son borradores compartidos entre
dispositivos. Cerrar sesión limpia los borradores del rol correspondiente. No
guardan contraseñas, credenciales, archivos ni precios calculados. Se avisa si
el almacenamiento falla o hay que volver a adjuntar un archivo. El acuse UUID
no otorga permisos ni reemplaza las validaciones e idempotencia del servidor.

## Organización

Mis envíos y Cuenta corriente separan:

- **Todos:** operaciones vigentes y movimientos contables efectivos.
- **Modificados:** guías anteriores reemplazadas, con su identificación e historia.
- **Cancelados:** envíos cancelados, identificados y sin cargo.

La búsqueda de Mis envíos conserva la solapa seleccionada. Los contadores de
ámbito respetan el grupo; los filtros operativos aparecen dentro de Todos.
El Excel de movimientos aplica el mismo filtro que la Cuenta corriente.

## Regla contable y controles

Cancelados y reemplazados quedan fuera del saldo; se conserva su historia y
su importe original como evidencia. La cancelación desde Admin confirma en
una misma transacción la baja del cargo y el estado de la solicitud. Los pagos
ya registrados se conservan: no se borran ni reasignan al ocultar un envío.

El cambio genérico de estado no permite reemplazar una guía sin el flujo que
vincula la nueva y anula el cargo anterior, cancelar dejando cargos activos ni
reactivar un envío ya anulado. Una guía cancelada/reemplazada no genera nuevos
cargos. Facturas emitidas (legacy o módulo actual) y ajustes activos requieren
su circuito documentado; se bloquea su anulación directa. No se borra una FC
para forzar saldo cero. Las lecturas y actualizaciones vuelven a validar estas
condiciones bajo bloqueo de filas.

Una inconsistencia histórica con un cargo realmente activo no se esconde en
Cuenta corriente: debe corregirse con evidencia. No se hizo migración ni
reparación masiva de datos, y este cambio no cancela guías en el courier.

## Validación

- Suite completa: **2.356 pruebas + 5 subpruebas aprobadas**, 33 avisos existentes.
- JavaScript: **8 pruebas** de aislamiento por cuenta, restauración, caducidad,
  errores, acuses, formularios desmontados y regreso tras un guardado.
- PostgreSQL local desechable: cancelación atómica, rollback de auditoría,
  propiedad del cargo, pagos conservados, facturas actuales y estados inválidos.
- Recorrido visual con datos ficticios: cotizador completo y modal; guía de dos
  cajas y tres artículos; Atrás, recarga, salida y regreso; edición Admin;
  guardados simulados y limpieza del borrador; tres solapas en ambas pantallas.
- Mis envíos y Cuenta corriente sin desbordes a 320 px; revisión de temas claro
  y oscuro. Sin errores de consola en el guardado simulado de la guía.
- Revisión final de plantillas y contadores: 60 pruebas aprobadas adicionales.
- Sintaxis JavaScript y `git diff --check` aprobados.

QA local en `qa_navigation_history_20260917/` del workspace: `pytest-final.log`,
`pytest-final.xml`, `js-tests.log`, `route.jsonl`, `outcome.json` y `preview.py`.
Preview aislado: `http://127.0.0.1:8781`, operaciones simuladas y red externa
bloqueada. No se emitieron guías, retiros, facturas ni pagos reales para probar.

## Publicación y comprobación pendiente

Después de autorización, integrar en main y comprobar el despliegue Railway.
Verificar salud, recursos estáticos y sesiones reales de cliente/Admin. Comparar
saldos antes/después y confirmar la separación de las tres solapas sin ejecutar
operaciones reales de prueba. La publicación no requiere cambios de esquema.
