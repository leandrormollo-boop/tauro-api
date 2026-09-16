# Nombre de descarga y número interno TAURO — 16/09/2026

Pedido del usuario: `TAURO - CUENTA - DESTINATARIO - ORIGEN - NÚMERO.pdf`,
con el contador comenzando en **50300**.

Ejemplo: `TAURO - WAIMAO - MARSANTEX - CN - 50300.pdf`.
El siguiente envío numerado recibe 50301. El contador es global de TAURO,
compartido entre cuentas y couriers. No reemplaza el tracking del operador.

## Comportamiento

- La cuenta sale de `cliente_id` (WAIMAO), no de una razón social que pudiera
  estar guardada en el campo nombre del cliente.
- El origen sale de `remitente_pais`, normalizado a ISO-2. No se usa el país
  de destino ni el de fabricación de un artículo. Un histórico sin origen
  reconocido muestra `ORIGEN SIN DATO`; no se inventa un país.
- El número se asigna al persistir una guía confirmada, dentro de la misma
  transacción que guarda su tracking. Los reintentos conservan ese número.
- Las guías anteriores sin número lo reciben en su primera descarga
  autorizada, después de verificar y preparar sus documentos. No hay
  renumeración masiva de la historia.
- Una nueva solicitud emitida por repetición o reemplazo tiene su propio
  número. Descargar nuevamente la misma guía conserva el número.
- La secuencia de PostgreSQL y un índice único impiden números duplicados.
  No se reciclan números de guías canceladas. Pueden existir saltos por
  transacciones revertidas; no es numeración fiscal ni promete ser sin huecos.
- Los envíos marcados como prueba no consumen la secuencia comercial.
- El nombre conserva el saneamiento ASCII existente para los encabezados
  HTTP: mayúsculas, sin saltos de línea, separadores de ruta ni comillas.

El PDF conserva etiquetas e invoice en el mismo orden y con el mismo
contenido. No se alteran el tracking, las etiquetas del courier, importes,
cuentas corrientes ni la emisión. El cambio solicitado afecta la descarga
del cliente; la descarga técnica del administrador mantiene su función.

## Migración y publicación

Rama `codex/guide-download-names-20260916`, basada en `468a59e`; incluye todas
las entregas locales previas del portal, HS y errores DHL.

`sql/schema.sql` añade una columna nullable `numero_guia_tauro`, la secuencia
`numero_guia_tauro_seq` (inicio/mínimo 50300, sin ciclo) y un índice único.
La migración es aditiva e idempotente; volver a ejecutar el schema no
reinicia el contador. No usar `RESTART` ni `setval` durante despliegues.
La reversión del código puede conservar esta metadata para no reutilizar
números cuando se reactive la función.

**Preparado localmente, pendiente de publicación.** No se ejecutó la
migración en producción ni se consumieron números de guías reales.

## Verificación

Suite completa: **2.300 pruebas y 5 subpruebas aprobadas**, sin fallos ni
pruebas omitidas; 33 advertencias existentes. 24 regresiones nuevas.

Pruebas de nombres/headers y PostgreSQL aislado: inicio 50300, contador global,
descargas concurrentes de la misma guía, numeración de guías distintas en
paralelo, persistencia al emitir, reintentos, migración repetida, restricción
única, cancelación y restricciones de acceso. Un cliente ajeno, una solicitud
oculta/de prueba o un documento faltante/inválido no obtiene PDF ni número.

Evidencia: `qa_portal_readiness_20260916/evidence/guide-numbers-targeted.log`,
`guide-numbers-full.log` y `guide-names-validation.json`.
Ejecución: Astra elegido por el usuario, sin subagentes; router ejecutado y
piso Sol de la política para arquitectura respetado manteniendo Astra.
