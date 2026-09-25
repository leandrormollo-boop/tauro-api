# Cotizador del portal en una sola ventana

Estado: implementación sobre `a0fdf94`, revisada por el usuario y con publicación autorizada el 25/09/2026. No se cambiaron permisos, precios configurados ni datos operativos. El resultado del despliegue se registra en el historial de release y en el log de ejecución.

## Problema confirmado

En el portal autenticado, seleccionar Nacional dentro de la ventana llamaba a `/portal/cotizar?ambito=nacional`. Ese GET redirigía al formulario de emisión `/portal/oca/nuevo`, que no contiene el elemento esperado por la ventana. El cliente veía un error aunque OCA estuviera habilitado.

## Comportamiento

- Nacional e Internacional se seleccionan arriba y conservan borradores separados. Ruta, paquetes y resultados comparten una pantalla.
- Al completar los campos se consulta automáticamente después de 900 ms sin cambios. No se consulta con campos incompletos. Cada edición retira inmediatamente los precios anteriores; las respuestas atrasadas se descartan.
- Se muestran las opciones devueltas por operadores habilitados para el cliente autenticado, ordenadas por precio, con plazo informado por el operador. Sin plazo, se muestra «A confirmar».
- Cada consulta actualiza únicamente los resultados. Cerrar la ventana o cambiar de ámbito cancela la solicitud pendiente. Se conserva un botón para consultar nuevamente ante errores y un POST tradicional de respaldo.
- Los selectores usan íconos SVG y los campos están alineados; ayudas breves en tooltips. Verificado en tema claro, oscuro y ancho móvil de 390 px.
- Continuar precarga ruta y paquetes en la emisión. El precio no se transfiere como autoridad: emisión valida y cotiza nuevamente.

## Integraciones y controles

Internacional reutiliza el motor de cotización y el pricing existente por cliente. No se habilita FedEx por mostrarlo en una prueba. El filtrado de opciones refuerza la lista de couriers habilitados.

Nacional consulta OCA usando su adapter y configuración por cliente, por CP, cantidad, peso, medidas y valor declarado. No exige dirección ni contacto completos hasta la emisión. El alcance actual es puerta a puerta y cajas del mismo peso/medidas, coherente con la emisión OCA existente. Otros operadores nacionales requieren integrar su adapter con pricing por cliente antes de ofrecer tarifas: no hay precios ficticios de respaldo.

La consulta nacional no emite envíos, no reserva recolecciones ni escribe cargos o snapshots de cotización. Valida moneda, precio y costo con Decimal y contrato de adapter. No expone costo, margen, credenciales ni errores XML. Se limita el endpoint a 30 consultas por minuto por cliente.

Cambiar país o provincia vacía ciudad/localidad y CP para evitar cotizar una ubicación anterior. En el paso a emisión internacional, una ciudad o CP de origen distinta al remitente guardado obliga a completar su dirección, sin modificar la libreta.

## Evidencia de validación

- Suites enfocadas de cotización, OCA, permisos y pricing: 202 pruebas aprobadas.
- Regresión adicional: 80 aprobadas y una omitida por requerir base PostgreSQL.
- Ejecución ampliada: 295 aprobadas; una no pudo correr por dependencia HTTP de prueba ausente. Instalada la dependencia únicamente en el entorno de pruebas, se volvió a ejecutar el grupo de PostgreSQL OCA, rutas frecuentes y cotizador unificado: 31 aprobadas, incluidas las pruebas bloqueadas.
- Máquina de solicitudes JavaScript: 5 pruebas aprobadas (debounce, respuestas atrasadas, datos incompletos, pausa/reanudación y reintento).
- Compilación de plantillas, sintaxis JavaScript y `git diff --check` sin errores.
- Navegador: consulta automática nacional e internacional, cambio de ámbito conservando valores, regreso al borrador, rutas frecuentes sincronizadas, precarga de OCA, invalidación al cambiar provincia, temas claro/oscuro y móvil sin desborde horizontal.

La revisión visual se realiza con un servidor auxiliar externo al repositorio en `127.0.0.1:8790`, con tarifas simuladas, sin credenciales ni acceso a base de datos y con red externa bloqueada. No constituye una validación comercial de precios o plazos contra los operadores reales. El error original y la habilitación de OCA se comprobaron de forma autenticada y de solo lectura en producción.

Antes de publicar: confirmar el diseño con el usuario y obtener su autorización de publicación. Después: verificar en el portal real la apertura de ambos ámbitos y una cotización de referencia, sin emitir un envío.

## Segunda revisión: diseño y ubicaciones

- Se eliminó el nombre repetido del operador en las tarjetas: logo con texto alternativo, servicio y acción «Elegir envío». La acción conserva el nombre del operador para lectores de pantalla.
- Selector Nacional/Internacional con forma de cápsula, tipografía Inter, campos y botones redondeados. Rutas frecuentes plegadas por defecto. Banderas SVG locales en la búsqueda y selección de países, manteniendo el select real y la navegación por teclado.
- Autocompletado bidireccional ciudad/CP en ambos ámbitos, también provincia nacional. CABA completa 1000. Coincidencias únicas se completan; códigos/localidades ambiguos ofrecen hasta ocho alternativas. Siempre se puede escribir manualmente.
- Consulta autenticada `/portal/cotizar/ubicaciones`, limitada a 90/minuto por cliente. Índice local GeoNames (ver `data/postal/README.md`): 1.613.918 referencias en 116 países, incluyendo referencias puntuales preexistentes de TAURO. Cobertura desigual; no es un validador postal universal. No se envían las búsquedas a terceros ni se invoca un modelo para adivinar códigos.
- El bundle postal comprimido ocupa 44,3 MiB del servidor y se descomprime una sola vez por proceso (162,2 MiB de almacenamiento temporal, SQLite de solo lectura). Primera búsqueda medida localmente: 176 ms; siguientes consultas por índice, menos de 1 ms en los ejemplos probados. El navegador sólo recibe sugerencias pequeñas e imágenes de banderas visibles.
- Debounce de 400 ms, cancelación de respuestas anteriores al cambiar datos, ámbito o cerrar. Búsquedas pendientes suspenden la cotización automática; ante una falla queda disponible la carga manual. Se reanuda la búsqueda incompleta al abrir nuevamente.
- Los valores sugeridos se marcan como referencia y persisten así en el borrador. Se usan para estimar, pero no se precargan como domicilio confirmado al continuar a emisión. Debe completarse la ubicación exacta del envío.

Validación de esta revisión: 115 pruebas Python aprobadas inicialmente; se actualizó una expectativa estática de versión del recurso JS. El grupo posterior (incluyendo dicha expectativa, OCA PostgreSQL, reseller y países) aprobó 27 pruebas. JavaScript: 14 aprobadas. Navegador: CABA→1000, CP 90210→Beverly Hills, elección La Plata para CP 1900 ambiguo, actualización de provincia, retiro de tarifas anteriores, referencias excluidas del link de emisión y recuperación del borrador. Ancho móvil: 379 px de contenido sobre 379 px disponibles dentro del marco de 390 px, sin desborde horizontal. Tarifas del visor local siguen simuladas; no hubo publicación ni escrituras operativas.
