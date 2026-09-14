# Experiencia del portal TAURO — revisión con WAIMAO

14 de septiembre de 2026 · Primera mejora preparada para revisión; todavía sin publicar.

El portal ya tiene una base útil para operar: cotización por cuenta, remitentes y destinatarios reutilizables, paquetes, invoice con varios artículos, seguimiento y cuenta corriente. El mayor problema encontrado es la jerarquía: obliga a leer información repetida y no distingue con suficiente claridad qué envío necesita atención y cuál ya terminó.

La dirección propuesta es que el cliente pueda responder rápidamente tres preguntas: **qué necesita mi atención, dónde está mi envío y cuál es el próximo paso**. La identidad del avión, las conexiones y los violetas de TAURO debe acompañar esa operación.

## Qué se revisó

Se ingresó en la sesión real de WAIMAO y se recorrieron Inicio, Mis envíos, Nuevo envío internacional, Cotizar, Mi cuenta, Recolecciones y Embalajes y tarifas. También se revisó la lista en una ventana móvil de 390 × 844. No se crearon solicitudes, guías, retiros, pagos ni cotizaciones reales durante esta revisión.

La verificación de los cambios se hizo aparte, con las plantillas reales y datos ficticios. El informe visual del 09/09 es un antecedente; no se presenta como una nueva inspección de tiendas o catálogos conectados.

## Hallazgos y cambios preparados

| Hallazgo observado | Impacto para el cliente | Cambio preparado |
| --- | --- | --- |
| Los 47 envíos del historial aparecían agrupados como recolectados, aunque había entregas confirmadas. | La lista no permite separar trabajo activo e historial terminado. | Filtros distintos para En seguimiento, Retenidos y Entregados. Contadores y filas usan el mismo criterio. |
| Una misma fila mostraba Recolectado y Entregado. | Dos respuestas diferentes para el mismo envío. | Un estado principal coherente en inicio, lista y detalle, conservando los estados originales en los registros. |
| “Proceso de entrega” se usaba para movimientos de tránsito. | Puede interpretarse como reparto final. | “En tránsito”, con el último mensaje del courier y fecha de sincronización. El detalle aclara que la entrega aún no está confirmada. |
| Imagen de portada muy alta; la actividad quedaba debajo. | Más desplazamiento para la operación diaria. | Portada más corta con el avión original, accesos por estado, búsqueda y actividad antes de la cuenta. |
| Búsqueda oculta y fechas abiertas al entrar. | Se tarda más en encontrar un tracking conocido. | Búsqueda siempre visible; fechas y resumen del período desplegables. Se elimina la opción de año 0. |
| Se repetía el precio inicial y final cuando eran iguales. | Más ruido y duda sobre qué importe mirar. | Un total registrado en ARS; desglose accesible cuando existen diferencias o impuestos. Las anulaciones no suman al total de la página. |
| Varias acciones competían en cada fila. | Cuesta identificar cómo abrir o descargar un envío. | Ver envío y Guía PDF a la vista; Verificar y Repetir dentro de Más opciones. |
| El acceso desde Recolecciones enviaba un filtro que la lista no reconoce. | El cliente debía buscar de nuevo la guía. | Enlace directo a las guías internacionales listas. No se ofrece retiro si el seguimiento ya registra tránsito, retención o entrega. |
| Textos y campos pequeños; etiquetas de invoice cortadas en móvil. | Errores y esfuerzo al cargar información. | Campos de 16 px, etiquetas legibles, mejor separación, rótulos completos y distribución móvil. |
| El cotizador en modo claro conservaba una tarjeta oscura. | Algunos títulos tenían contraste insuficiente. | Fondo y textos compatibles con el tema; menos encabezados repetidos. |
| Los filtros solicitaban de nuevo toda la página. | Transferencia y trabajo de presentación innecesarios. | Respuestas parciales, preservación de lista y desplazamiento, recuperación del foco y mensaje persistente si una consulta falla o supera 15 segundos. |

## Qué se conserva y qué sobra

Se conservan los destinatarios y remitentes guardados, la separación nacional/internacional, la cuenta corriente, los embalajes reutilizables, la invoice por artículo y el vínculo al seguimiento oficial. Son capacidades necesarias, aunque necesitan una presentación consistente.

Se quitaron de la primera lectura los estados duplicados, los precios repetidos, las explicaciones internas de APIs, la búsqueda escondida y la prominencia excesiva de filtros secundarios. Los registros históricos y los detalles económicos siguen disponibles.

No eliminaría Productos ni Embalajes y tarifas: resuelven cosas diferentes, contenido comercial y caja física. Sí explicaría esa diferencia al configurar la cuenta y los agruparía visualmente con clientes y remitentes. Tampoco llenaría el inicio con gráficos de entregas, velocidad o cobertura si no hay datos confiables para calcularlos.

## Seguimiento DHL: pendiente operativo prioritario

En la revisión, el último envío de WAIMAO seguía mostrado como “Proceso de entrega” y con sincronización del 14/09 a las 08:20. Eso no confirma ni descarta por sí solo la retención comunicada por DHL.

Esta entrega mejora cómo se presentan los datos disponibles. **No cambia el clasificador DHL ni la frecuencia diaria de consulta y no marca una retención por suposición.** Para cerrar aquel incidente hace falta contrastar el aviso de DHL con los eventos del envío y su hora. La ubicación de un trámite aduanero tampoco debe presentarse automáticamente como la ubicación física del paquete.

La siguiente mejora funcional debería mostrar por separado: último evento, lugar informado por el courier, momento del evento, momento de consulta y acción solicitada. Un estado desactualizado debe verse como tal. El aumento de frecuencia requiere revisar límites de la API y priorizar envíos activos; un entregado no necesita consultas permanentes.

## Próxima etapa propuesta

| Prioridad | Trabajo | Criterio de aceptación |
| --- | --- | --- |
| 1 | Bandeja de incidencias: retención, documentos pendientes y novedades sin resolver. | Cada caso muestra qué informó el courier, cuándo, quién debe intervenir y una acción concreta cuando exista evidencia. |
| 1 | Borradores recuperables y revisión antes de crear la solicitud. | Se puede salir y retomar por cuenta; se revisan ruta, cajas, artículos, moneda, seguro y pagador. Los datos de destinatarios se guardan con acceso restringido. |
| 2 | Recorrido entre cotización, paquete guardado y nueva guía. | Se conserva lo ya ingresado y se distingue tarifa estimada, precio aceptado y cargos posteriores documentados. |
| 2 | Ordenar navegación de configuración. | La operación frecuente queda a un clic; productos, embalajes, remitentes e integraciones tienen una explicación corta y un lugar previsible. |
| 2 | Medir la velocidad real y optimizar consultas. | Tiempos separados de página, base de datos y transportista. Paginación y filtros en la base para historiales grandes, en vez de cargar todo para recortarlo después. |
| 3 | Familia de piezas gráficas del portal. | Una portada, ilustraciones pequeñas para pantallas vacías e iconos consistentes de paquete, invoice, retiro y entrega. Imágenes adaptadas al tamaño visible; sin carruseles ni video automático que compitan con el trabajo. |

Para la dirección visual: violeta reservado para acciones y marca, verde para confirmaciones, ámbar para pendientes y rojo para incidencias. Los estados también llevan texto; el color no transmite información por sí solo. La pieza del avión puede seguir como identidad, con una altura acotada. Las conexiones pueden ser un recurso de fondo discreto; un mapa sólo debería mostrar posiciones verificadas.

## Velocidad y accesibilidad: objetivos, no resultados declarados

Tomar como objetivos de medición LCP ≤ 2,5 s, INP ≤ 200 ms y CLS ≤ 0,1, en el percentil 75 y separando móvil y escritorio. Son las referencias de [Core Web Vitals de Google](https://web.dev/articles/vitals); todavía no se midieron en producción para esta versión.

Como criterio de legibilidad, comprobar contraste mínimo 4,5:1 para texto normal y 3:1 para texto grande, según [WCAG 2.2, contraste mínimo](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html). La revisión visual realizada no equivale a una certificación de accesibilidad de todo el portal. Para controles táctiles, respetar el mínimo de 24 × 24 px o las excepciones de separación indicadas por [WCAG 2.2, tamaño del objetivo](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html); se priorizan controles de unos 44 px en formularios frecuentes.

## Validación de esta entrega

- Navegador: escritorio y móvil; modos claro y oscuro; búsqueda global desde un filtro de entregados; filtro de retenidos; volver atrás recupera la lista; acciones adicionales; detalle y cuatro pasos del formulario, con un segundo artículo agregado sin emitir.
- Sin desbordamiento horizontal en las vistas móviles comprobadas. La primera tarjeta de Mis envíos empieza aproximadamente a 685 px en la vista local de 390 × 844, que incluye el aviso de datos ficticios. El formulario usa campos de 16 px.
- Respuesta local sin compresión, para el mismo contenido: un resultado pasa de 34.917 a 10.936 bytes (68,7% menos); una página de diez filas, de 67.318 a 43.337 bytes (35,6% menos). No son mediciones del tiempo de DHL ni de la velocidad real de los clientes.
- 240 pruebas aprobadas de portal, estados, períodos, precios visibles, cotizaciones, invoice, paquetes, repetición, cancelación y retiros. Una prueba de PostgreSQL omitida porque requiere una base aislada. Un caso de husos horarios excluido del pase final tras reproducir exactamente su falla en la base `5f91386` sin esta mejora: Windows no dispone del archivo de zonas que usa el contenedor Linux.
- Sintaxis del JavaScript y comprobación de diferencias correctas. No se agregaron dependencias ni migraciones. En las pruebas del navegador aparecieron dos avisos de transición visual omitida de Chrome; la navegación completó y no se consideran una medición de rendimiento.

Desarrollo aislado en `codex/portal-experiencia-cliente`, sobre `5f91386`. La vista de revisión está en [el portal local](http://127.0.0.1:8771/portal/home) mientras el servidor de esta computadora permanezca activo. La publicación y su verificación en el dominio real siguen pendientes.
