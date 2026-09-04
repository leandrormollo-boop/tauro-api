# Revisión independiente — Operadores, 04/09/2026

Tres rondas de revisión de código con el CLI real de Claude, principal solicitado
`claude-fable-5-1`. En cada respuesta se verificó modelUsage y coincidencia de los
tokens de salida principales. El CLI también reportó Haiku auxiliar: no se afirma
ejecución exclusivamente Fable. Código/datos sintéticos únicamente; sin documentos,
credenciales, correos ni información personal real. Herramientas y persistencia
de sesión deshabilitadas. No se conectó el modelo a producción.

La revisión de un modelo no es una certificación. Se reprodujeron los hallazgos
aplicables con pruebas y se descartaron hipótesis contradichas por el código real.
Fable no ejecutó pruebas: las ejecutó el agente principal contra PostgreSQL aislado.

## Hallazgos aplicables corregidos

- Sin camino de corrección de pagos: se agregaron reversas append-only de pagos y
  aplicaciones, restitución de saldos, comprobante original conservado y recarga
  correcta de la misma transferencia tras revertirla.
- Origen inexistente devolvía error SQL sin tratar: ahora error de dominio.
- TC 1 entre monedas distintas podía confundirse: conversión con confirmación
  adicional persistida, aun en paridad; parser de TC rechaza formatos ambiguos.
- Remanentes parciales invisibles: lista de pendientes y total sin desglosar;
  créditos NC con signo correcto. Se muestran excesos en vez de ocultarlos.
- Replay de una clave ya revertida: error explícito, no falso “registrado”.
  Plazos/verificaciones también tienen idempotencia.
- Plazo por sí solo bloqueaba anulación: sólo aplicaciones vigentes bloquean
  anular. La evidencia de documentos utilizados se conserva, incluso después
  de revertir: no se reescribe la historia. Se permite agregar el PDF faltante
  con hash validado; no reemplazar un original existente.
- Corrección del vencimiento acordado: rectificaciones auditadas, sin modificar
  fecha documental ni borrar snapshots. Se protege la emisión que los sustenta.
- Factura vieja cargada hoy: no recibe automáticamente plazo configurado después
  de su emisión. Requiere confirmación explícita.
- Después de limpiar un vencimiento por rectificación, el botón de asignar plazo
  antiguo podía dar éxito sin efecto: oculto, y servicio rechaza ese camino.
  Se debe usar una nueva rectificación. Detectado también localmente antes de
  recibir la tercera revisión; cubierto por regresión.
- READ COMMITTED explícito en el servicio y fail-closed en triggers relevantes.

## Observaciones contextualizadas

- No existe `cc.anular_factura` en este repositorio. Se probó directamente la
  protección SQL de anulaciones, sin inventar un flujo Admin nuevo.
- `ajustes_cliente.conciliacion_id` tiene UNIQUE: no multiplica las líneas.
- El fixture auth es autouse; conexiones de prueba son independientes por hilo.
  `archivo_contenido` es opcional en el registrador. Las sospechas de fallos de
  esos tests se contrastaron con ejecuciones reales, no se asumieron ciertas.
- Se agregaron pruebas ASGI de rutas, cookie, multipart, carga/aplicación y errores
  de campos/archivo grande. La última revisión no incluyó ese archivo nuevo en
  su selección; no se interpreta su ausencia en el prompt como falta de pruebas.
- Autenticación heredada: único Admin compartido. El actor registrado es `admin`;
  distinguir personas requerirá identidades propias, fuera de este cambio.
- Una fecha manual respaldada por un acuerdo puede registrarse como rectificación
  explícita aunque no hubiera snapshot. Se identifica como “Rectificación manual
  auditada”, no como fecha leída ni plazo calculado. No se infiere automáticamente.

## Evidencia local

Artefactos de las rondas (manifest de archivos/hash, entrada y respuesta):

- `/tmp/tauro-operadores-review.4BrUk7`
- `/tmp/tauro-operadores-final.qim2uR`
- `/tmp/tauro-operadores-cierre.JsHl3u`

QA final: suite completa, tests específicos de operadores, upgrade b7d1fb1→actual
repetido tres veces sobre schema descartable y cinco vistas a 1440/840/390 px.
Los datos productivos no fueron consultados ni modificados para estas pruebas.

Resultado: implementación local probada. Ningún release, pago, NC real ni cargo
al cliente aplicado. Producción requiere autorización y verificación del release.
