# Cotizador compacto y formulario nacional — 25/09/2026

Implementado sobre main `322b536` en `codex/compact-quote-national-20260925`. Vista previa local; sin publicación ni operaciones productivas durante este cambio.

## Cambios

- Ventana de cotización compacta con tarifas junto a los datos desde 780 px; cajas adicionales editables en solapas sin apilar formularios.
- Invertir visible en nacional e internacional: intercambia país/provincia, localidad, CP y marcador de referencia, conserva paquetes/valor e invalida la tarifa anterior y las búsquedas pendientes.
- Versión móvil propuesta en tres pasos, conservando datos y posición del paso en el borrador. Navegar nunca emite una guía.
- Referencia Wilde ↔ 1875 agregada desde fuente documentada. Prefijos sólo sugieren y códigos compartidos requieren elección.
- Formulario OCA unificado visualmente y separado en Origen, Destino y Paquetes. Campos numéricos localizados, ayudas compactas, avance/retroceso y guardado en la pestaña.
- Editar una cotización OCA restaura sus campos desde el servidor, bajo autenticación y propiedad. Al continuar exige otra cotización. No se reutiliza el precio viejo ni se modifica una solicitud emitida.

## Validación

- 152 pruebas Python aprobadas del cotizador, ubicaciones, OCA con PostgreSQL aislado, permisos, tarifación, handoff y rutas frecuentes.
- 16 pruebas JavaScript de debounce/cancelación/respuestas antiguas y cambio/inversión de ruta.
- Navegador local con tarifas simuladas: Wilde → 1875, 1875 → Wilde, inversión nacional e internacional, provincias actualizadas, paquetes conservados, respuestas automáticas, ambigüedad 1870, pasos móviles y vuelta del formulario nacional.
- 1280 × 720: cotizador internacional con dos operadores simulados y dos cajas tiene contenido de 601 px y scrollHeight de 601 px. Cotizador nacional y botón Siguiente de creación nacional también entran.
- Celular de 390 px: ruta, paquetes y tarifas por pasos, sin desbordamiento horizontal; temas claro/oscuro y cero errores de consola durante las comprobaciones.

## Límites

No se cambian permisos de operadores, tarifas, markup, saldo, emisión ni datos de clientes. Las direcciones sugeridas del cotizador siguen siendo referencias y no se transfieren como domicilios completos de emisión. Más operadores, zoom o ventanas muy bajas pueden requerir un desplazamiento acotado para conservar legibilidad y acceso a todo el contenido. No se ha vuelto a validar cobertura postal mundial completa.
