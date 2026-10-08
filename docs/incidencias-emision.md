# Incidencias de emisión

La bandeja `/admin/incidencias` reúne errores al emitir guías desde los endpoints
de cliente y Admin que usan `ejecutar_emision_registrada`. La campana del Admin
consulta el contador cada 45 segundos mientras la página está visible. No envía
correos, mensajes ni notificaciones del sistema operativo.

## Registro y catálogo

- `emision_intentos` deja constancia antes de llamar al servicio de emisión.
  Si falla esa escritura, no se invoca el servicio. Un intento sin respuesta
  después de diez minutos aparece separado para verificarlo.
- `emision_catalogo_errores` se sincroniza con el catálogo versionado de código
  en el arranque. Cada incidencia conserva una copia de su explicación.
- `emision_incidencias` agrupa por solicitud y código, cuenta repeticiones y
  distingue Pendiente, En revisión y Resuelta. La bandeja pagina de a 50.
- El historial previo continúa en `/admin/incidencias-emision`. Los eventos
  anteriores a este cambio no se convierten retroactivamente en alertas.
- Una respuesta exitosa con PDF o cargo incompletos también abre incidencia.
  Ninguna clasificación modifica el resultado comercial que recibe el cliente.
- Los códigos desconocidos quedan sin clasificar. Las coincidencias de texto
  se limitan a mensajes reconocidos de los servicios actuales. Nuevos códigos
  se incorporan mediante una revisión del catálogo, no por aprendizaje libre.

## Asistente y permisos

El asistente aplica primero el catálogo y consultas SQL de lectura. Puede
cerrar la incidencia al comprobar estado emitido, tracking, PDF, fecha y
referencia del courier, cargo activo del mismo cliente y mismo precio, sin
cargo pendiente. Un mero tracking o enlace de PDF no alcanza. No certifica
la vigencia de una tarifa del courier ni consulta su panel remoto.

No emite ni reintenta guías, no libera reservas, no cambia datos declarados,
precios, pagos, permisos ni código de producción. Las correcciones comerciales
y la conciliación con el operador permanecen en sus flujos existentes. Esto
es un asistente de diagnóstico y seguimiento, no un reparador autónomo de software.

`Comprobar resolución` vuelve a leer la evidencia actual. `Analizar con IA`
aparece para causas no confirmadas únicamente si están configuradas:

```text
TAURO_INCIDENCIAS_IA_ENABLED=1
OPENAI_API_KEY=<secreto del servidor>
```

El modo IA está desactivado por defecto. No se necesita una clave para el
catálogo, la campana, los registros ni las comprobaciones deterministas.
Usa la ruta de seguridad Sol/high, cero herramientas, cero reintentos de API,
20 segundos de timeout, salida estructurada y `store=False`. La entrada sólo
contiene códigos, estados, courier y booleanos: no recibe nombres, correos,
direcciones, tracking, importes, documentos ni mensajes de error libres.
La salida se valida y se muestra como hipótesis. No puede cerrar incidencias.
El presupuesto es de doce solicitudes de análisis por diez minutos; la cuota
de IA se persiste y hay un bloqueo por caso para evitar llamadas simultáneas.

Cada revisión guarda actor, caso, política, modelo, hash de entrada y resultado
en auditoría. Las acciones están autenticadas y protegidas por CSRF, y rechazan
resultados de análisis que hayan quedado viejos por un intento nuevo.

Referencia de integración: [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

## Verificación

Pruebas con PostgreSQL aislado en `test_control_incidencias_emision_postgres.py`;
catálogo, privacidad/contrato IA y HTTP en los otros tres archivos de pruebas
de incidencias. Incluyen carreras de emisión, fallos de registro, evidencia
incompleta, intento inconcluso, autenticación, CSRF y salida del modelo no válida.
No requieren emitir guías ni llamar a una API de IA real.

El despliegue agrega tres tablas y sus índices. No modifica importes históricos.
La activación IA requiere configurar su flag y verificar acceso al modelo en el
entorno de despliegue. La credencial nunca se carga en una página ni en Git.
