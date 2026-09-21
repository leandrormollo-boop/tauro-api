# Rutas frecuentes del cliente

Implementación revisada el 21 de septiembre de 2026. Publicación autorizada por el usuario mediante «avancemos» tras revisar la entrega. El resultado operativo del despliegue se registra en la evidencia local. Base de la rama: `d09deee5d8eb9e2e86108adb6678fb2131554657`.

## Comportamiento

En el primer paso del cotizador internacional aparecen hasta seis botones con países completos y sentido del viaje. Se ordenan por cantidad de guías vigentes del cliente en los últimos doce meses, luego por fecha más reciente. No se inventa el sentido inverso como ruta frecuente; el botón **Invertir ruta** lo permite cuando ambos países están disponibles.

Elegir una ruta completa únicamente los países. Conserva cajas, cantidades, medidas, peso y valor declarado. Limpia ciudad y código postal solamente del extremo cuyo país cambió y mantiene abierto el paso de ruta para revisar esos datos. Al recuperar el borrador también se respetan campos vacíos, sin rellenarlos con ciudades de referencia. Funciona en página completa y ventana de cotización.

Sin historial válido se conserva la carga manual. No se consultan tarifas ni se emiten guías al tocar estos botones. La ayuda adicional usa el control informativo existente, accesible también al tocarlo.

## Datos y controles

`servicios/rutas_frecuentes.py` realiza una lectura agregada filtrada por la identidad autenticada. No acepta otra cuenta desde el formulario ni desde parámetros de la URL. Excluye pruebas, guías ocultas, canceladas, reemplazadas, sin tracking, pendientes, antiguas o con países inválidos. Comprueba además anulaciones en `envios` y reemplazos emitidos en el historial de reemisiones. Si falta origen sólo usa la ruta identificada del registro; nunca presupone Argentina. Un error al obtener sugerencias no bloquea la cotización manual.

Sin migraciones, escrituras financieras, cambios de autenticación ni llamadas nuevas a proveedores. Los datos de otras cuentas no completan listas vacías. El historial de WAIMAO se comprobó mediante una consulta de sólo lectura a producción; sólo se conservaron agregados de países y cantidades en evidencia local.

## Validación

- Suite completa: 2373 pruebas y 5 subtests aprobados.
- Tras ajustar restauración de ubicaciones y versión del archivo JS: 59 pruebas del área aprobadas.
- JavaScript: 13 pruebas aprobadas, incluyendo aislamiento de formularios y conservación de cajas/borradores.
- `git diff --check` y comprobación de sintaxis JS sin errores.
- Navegador local: selección, cambio de un extremo, inversión, vuelta al paso anterior y recarga conservan cajas de prueba. Página completa y modal recuperan las ubicaciones vacías. Revisión visual de escritorio y móvil, ayuda al tocar y ausencia de errores JS.

Vista local: `http://127.0.0.1:8784/portal/cotizar?ambito=internacional`. Utiliza plantillas y archivos reales con datos agregados; no ejecuta cotizaciones, emisiones ni workers. Evidencia en `qa_client_routes_20260920` fuera del repositorio.

## Registro de ejecución

Router determinístico TAURO: arquitectura con datos privados y publicación productiva aprobada; mínimo Sol/high. Ejecución por Astra, manteniendo el modelo superior solicitado por el usuario. Sin delegación ni API de modelos. Controles: aislamiento por sesión, consulta de producción de sólo lectura, pruebas locales y revisión de interfaz. Resultado previo al despliegue: función validada y publicación autorizada. Los cambios de lanzamiento pendientes en otras ramas no forman parte de esta entrega.
