# Textos breves y ayudas contextuales — 17/09/2026

Preparado en `codex/portal-copy-help-20260917`, desde `3ca332d`.
**Publicación autorizada por el usuario: «publicalo».** Cambio `2a9088c`,
sobre `3ca332d`, sin divergencia. La evidencia posterior se guarda en
`qa_portal_copy_20260917/release-after.json`.

## Resultado

- Redacción simplificada en 40 plantillas del portal y administrador, más la web pública.
- 39 puntos de ayuda en las plantillas y 2 en el cotizador público.
- Definiciones junto al saldo, pagos en revisión, valor por caja, valor unitario,
  factura comercial, HS, seguro, SKU, recolecciones y control de facturas.
- Permanecen visibles errores, importes, confirmaciones, acciones requeridas,
  aprobación de pagos, diferencias y disponibilidad de servicios.
- Se retiraron promesas sin respaldo del texto: aprobación de productos en el
  día, avisos por mail garantizados y convertir pedidos en guía con un clic.

No cambian cálculos, datos, permisos, APIs, emisión, recolecciones ni pagos.
No hay migraciones ni nuevas dependencias. Se recompiló `static/js/app.js`
y se actualizó su versión a 17.

## Componente de ayuda

`static/js/info-ayuda.js` y `static/css/info-ayuda.css` se comparten entre
el portal, el administrador y el cotizador público. Marcar sólo aclaraciones
secundarias con `<span data-help="Nombre del concepto">Explicación breve.</span>`.
No colocar ayudas dentro de enlaces, botones, etiquetas de formulario ni
resúmenes desplegables. No incluir acciones o enlaces dentro de la explicación.

El ícono usa un botón de tipo `button`, nombre accesible y descripción asociada.
Se abre con puntero, foco o clic; Escape y clic fuera lo cierran. Se puede
mover el puntero al texto sin que desaparezca. Sólo una ayuda queda abierta.
Popover nativo evita recortes por tablas o ventanas modales; hay presentación
fija de respaldo para navegadores anteriores. Sin JavaScript el texto queda
visible. Las cajas clonadas reciben identificadores nuevos. React cede un
contenedor vacío al mismo componente y lo libera al desmontarlo.

## Validación

- Compilación pública y comprobación de sintaxis JavaScript correctas.
- Suite completa: **2.318 pruebas y 5 subpruebas aprobadas**, 33 advertencias
  existentes, sin fallos ni omisiones. PostgreSQL local aislado y conexiones
  externas bloqueadas. Los tests de texto se ajustaron a la nueva redacción;
  se conservaron controles funcionales y financieros.
- Pruebas de navegador: abrir/cerrar por clic, foco de teclado y Escape;
  Escape cierra la ayuda sin cerrar el diálogo de producto; artículo añadido
  con ayuda funcional e identificadores únicos; navegación AJAX del admin
  inicializa las nuevas ayudas y cierra las anteriores; React conserva la
  ayuda al editar la cotización.
- Revisadas cuenta, catálogo, factura comercial, web pública y control de
  facturas. Sin desborde horizontal a 320 px; ayuda dentro del viewport,
  contraste en temas claro y oscuro. Verificados límites del panel y tabla
  en escritorio por DOM. La captura amplia del navegador embebido mostró
  artefactos; las capturas móviles se visualizaron correctamente.
- Recolecciones verificadas nuevamente tras alinear su ícono.
- Vista previa con datos sintéticos y escrituras bloqueadas. No se enviaron
  guías, reservas, pagos ni mensajes reales.

Evidencia local: `qa_portal_copy_20260917/` en el workspace TAURO.
Router: Terra para edición de interfaz; ejecución con Astra solicitado por el
usuario, sin degradación ni delegación. Antes de publicar: autorización
explícita, ruta de producción, verificar divergencia de main y comprobar la
versión desplegada en Railway y los recursos en producción.
