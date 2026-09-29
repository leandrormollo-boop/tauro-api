# Correcciones de la auditoría del 29 de septiembre

Alcance: web pública, portal cliente y Admin. Los cambios se preparan en una rama de revisión; este documento no acredita publicación ni conciliación bancaria.

## Operación y cuentas

- Continuar una cotización abre el formulario real y conserva localidades, CP y bultos. Un CP de referencia exige confirmación de la dirección; no se transforma en dirección postal validada.
- Se detectan pagos repetidos por referencia normalizada dentro del cliente o por archivo de comprobante compartido, incluso con formularios diferentes. La verificación se ejecuta en PostgreSQL bajo bloqueo transaccional. Las anomalías anteriores se muestran para revisar: no se borran ni se reinterpretan como pagos inválidos automáticamente.
- No se admiten pagos futuros. Los casos históricos detectados quedan marcados para revisión de fecha y no acreditan ni reservan documentos. La marca persiste aunque llegue la fecha: una revisión administrativa explícita debe resolverla, con auditoría y control de sobreimputación. No se cambian sus importes ni estados de forma automática.
- Los pagos importados cuya fecha bancaria no se conoce conservan el saldo y muestran esa limitación. La fecha de importación no prueba cobranza de ese mes.
- El Admin interpreta por separado los importes canónicos y los escritos con formato argentino. Los cargos manuales no se presentan como fletes sin evidencia de envío.
- Proveedores separa deuda verificada, crédito disponible e importe nominal de documentos por verificar, siempre por moneda. El bruto por verificar no equivale a deuda exigible ni se suma como tal.

## Seguimiento e integraciones

- Una consulta DHL sin eventos o con 404 no confirma anulación física. Las guías descartadas siguen bajo control; detectar actividad abre una incidencia interna y no reactiva el cargo del cliente.
- Recolecciones pasadas o asociadas a envíos entregados aparecen como historial. No se afirma que el chofer retiró físicamente el paquete sin confirmación del operador.
- FedEx y UPS registran la referencia del intento antes de llamar al operador. Una respuesta incierta bloquea el reintento automático y exige conciliación manual. Esto no habilita esos operadores ni crea recuperación automática donde el proveedor no la ofrece.
- La oferta pública distingue un tarifario de una integración operativa. Shopify se describe por pedidos y seguimiento; no se ofrecen tarifas en checkout donde el endpoint está retirado.
- Tiendanube sólo permite instalación con readiness suficiente. Las pruebas de contratos/código no reemplazan la instalación y operación en tiendas de ensayo.

## Lectura y control

- Mi cuenta prioriza los movimientos, con atajo directo desde la cabecera. Las aplicaciones de un mismo pago muestran destinatario y enlace al envío propio.
- Mis envíos agrupa la información en seis columnas y usa tarjetas en pantallas angostas. Precio del envío sustituye “Saldo inicial/final”. Cancelados y reemplazados conservan “Sin cargo”.
- En escritorio el dock conserva su estilo flotante dentro de una franja lateral reservada, fuera de la tabla. El desplazamiento y los borradores siguen usando el scroll normal de la página.
- Admin → Automatizaciones registra inicio, resultado y último éxito de cuatro tareas: rastreo DHL diario, segunda ronda de retenidos, recepción de facturas DHL y actualización de tarifas de tienda. Conserva hasta 100 eventos por tarea y muestra atraso según su frecuencia. Una tarea omitida no se considera exitosa.
- La instrumentación no reemplaza alertas externas de infraestructura ni cubre todas las colas. El scheduler sigue dentro del proceso web; separar un worker requiere despliegue y verificación operativa.

## Recuperación y validación

La descarga JSON del Admin es una **exportación parcial**. Declara tablas y exclusiones, conserva decimales como texto exacto y aborta ante un fallo, sin entregar un archivo incompleto con apariencia de éxito. No contiene todos los documentos ni sirve para restaurar TAURO.

El procedimiento de restauración aislada y la evidencia local se documentan en `RESPALDOS_Y_RESTAURACION.md`; la reproducibilidad de dependencias y CI en `PRUEBAS_AUTOMATICAS.md`.

## Pendientes que requieren evidencia externa

1. Instalación, consentimiento, pedidos y seguimiento reales en tiendas de ensayo Shopify/Tiendanube; homologación/capacidades específicas de cada proveedor antes de habilitarlas.
2. Verificar en Railway la fecha y retención del último respaldo real y restaurar una copia real en un entorno aislado autorizado. Una prueba con datos ficticios sólo valida el procedimiento.
3. Conciliar bancos, facturas y pagos de proveedores para cerrar costos y rentabilidad. Ninguna cifra del panel por sí sola certifica esos documentos.
4. La reasignación de tres envíos ajenos a WAIMAO sigue en PR34, separada de este conjunto. No se modificaron esas cuentas en producción.
5. Completar el despliegue autorizado y comprobar login, saldos de control y primeras ejecuciones de tareas. Activar después Actions con el permiso `workflow`.

No se emitieron guías, reservaron retiros, cargaron pagos reales ni contactaron proveedores durante estas correcciones.

## Evidencia final de esta revisión

El commit `cd74890` superó una ejecución conjunta de los 29 archivos Python del workflow: **425 passed**, con 24 avisos preexistentes de deprecación de Pydantic. Se usó CPython 3.11.16 y PostgreSQL 18 local, en una base ficticia efímera eliminada al finalizar. Los cinco archivos JavaScript seleccionados dieron **21 passed**. La revisión independiente Sol del control financiero aprobó el cambio sin hallazgos materiales; sus pruebas se superponen con la suite conjunta y no se suman como casos nuevos.

La revisión visual cubrió Mi cuenta y Mis envíos en escritorio/móvil y claro/oscuro, además de la vista Admin de automatizaciones. El build web pasó. El ensayo de restauración usó datos ficticios, no una copia de Railway.

El usuario autorizó publicar el 29/09/2026. Para conservar los permisos actuales de GitHub, esta rama lleva la configuración de pruebas como plantilla inactiva en `docs/ci/audit-ci.yml`; su activación en Actions sigue pendiente. No hay validación remota de Actions/PostgreSQL 16. El build Docker, la migración y la salud deben verificarse durante el despliegue en Railway. El resultado del release se registra por separado después de esa comprobación.

Límites del control de fechas: la autenticación actual identifica al responsable como `admin` compartido; la migración detecta fechas futuras al desplegar y desde entonces, pero no reconstruye anomalías cuya fecha ya pasó antes del primer despliegue.
