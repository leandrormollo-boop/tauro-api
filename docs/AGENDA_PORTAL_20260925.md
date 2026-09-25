# Agenda del portal — 25/09/2026

Estado: implementado y verificado en vista previa local; pendiente de publicación.

## Comportamiento

- Mis clientes reúne los remitentes y destinatarios de la cuenta. Incluye búsqueda y filtros por rol.
- Ambos formularios muestran siempre los selectores de contactos guardados. El nacional sólo ofrece direcciones argentinas; el internacional conserva el catálogo completo.
- Un contacto seleccionado rellena sus datos, que siguen siendo editables para ese envío. Cambiar de contacto reemplaza también los campos opcionales vacíos, evitando mezclar domicilios.
- Mis clientes se abre aparte. Al volver o recibir la señal de guardado de otra pestaña se actualizan las opciones, sin sobrescribir el formulario ni su borrador. El endpoint exige sesión, no admite un cliente por parámetro y usa Cache-Control private/no-store. El navegador rechaza respuestas cuyo ámbito no coincide con el de la página.
- La ficha argentina guarda calle, número, piso, departamento y apellido por separado. La dirección completa sigue disponible para las integraciones e internacionales.
- Los registros históricos permanecen intactos. Si una dirección antigua no tiene calle/número separados, se muestra su texto original y se pide completarlos. No se deducen números ni apellidos.

## Base de datos

`sql/schema.sql` añade `direcciones.datos_nacionales JSONB NOT NULL DEFAULT '{}'` mediante ALTER idempotente al inicio. No modifica contactos existentes ni envíos. El editor anterior conserva estos datos para cambios de contacto y los descarta si cambia la dirección completa, el nombre o el país, evitando usar detalles antiguos.

## Validación

- 66 pruebas Python aprobadas: agenda HTTP/PostgreSQL aislado, CRUD, roles, país/CP/provincia, sesión requerida, aislamiento de cuentas, formulario OCA, handoff y regresiones del cotizador.
- 10 pruebas JS aprobadas: refresco de agenda, conservación de campos manuales, contactos eliminados, cambio de sesión, señal entre pestañas, inversión y autocompletado de rutas.
- Navegador: formulario nacional e internacional; selección de remitente/destinatario; piso y departamento; Atrás y recarga con borrador; alta y edición de contactos ficticios; actualización de alias en el desplegable manteniendo un número editado para el envío; formularios claros/oscuros; sin errores de consola.
- Datos demo en memoria, cotizaciones simuladas y transporte externo bloqueado en la vista previa. Sin emisiones, cargos ni cambios de datos reales.
