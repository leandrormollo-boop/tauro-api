# Visor privado de documentos del portal

Implementado sobre `954bc2b`, en `codex/portal-document-viewer-20260922`. Pendiente de autorización de publicación de esta funcionalidad. No cambia el esquema ni migra documentos.

## Resultado

- Tarjetas compactas en Mis envíos, detalle de envío, seguimiento de pagos y movimientos de cuenta.
- Visor integrado para PDF e imágenes, páginas, zoom, ajuste a página, descarga, cierre por X/Escape/fondo, foco y posición de lectura conservados.
- Factura comercial con vista independiente. La descarga principal de la guía conserva el PDF unificado, el nombre comercial y el registro de descarga existentes.
- El visor no consume números internos ni registra descargas, aprueba pagos o modifica saldos.
- Miniaturas JPEG de hasta 160 × 208; carga al quedar visibles, máximo dos solicitudes a la vez. Los filtros que reemplazan filas sin recargar también inicializan miniaturas.
- PDF.js 6.3.289 se sirve localmente y se importa sólo al abrir un PDF. CSP conserva `frame-src 'none'`, `object-src 'none'` y scripts sin `unsafe-eval`; se explicita `worker-src 'self'`.

## Permisos y límites

Tipos permitidos: guía, invoice comercial, factura cliente, factura cliente legacy y comprobante de pago. Cada lectura y cada miniatura verifica el cliente autenticado mediante los getters existentes; ningún tipo accede a documentos internos del courier. Se conservan los bloqueos de guías canceladas, reemplazadas, ocultas, de prueba o pendientes de reemisión.

Respuestas privadas `no-store`, MIME derivado de los bytes y `nosniff`. Sin URLs arbitrarias, conversores externos o caché pública. La caché en memoria contiene sólo 64 miniaturas (hasta 8 MB), se consulta después de autorizar y cambia al cambiar el contenido.

PDFium corre en procesos efímeros con timeout de 8 s, CPU de 6 s, límite de memoria de 768 MB en Linux y máximo dos procesos simultáneos por instancia. Archivos de más de 32 MB no se previsualizan: sigue disponible su descarga original. Archivos dañados o protegidos por contraseña muestran un mensaje y la descarga; nunca se oculta el movimiento contable por un fallo del visor.

Para actualizar PDF.js: cambiar la versión fijada en package.json y en documentos.js, ejecutar `npm ci --ignore-scripts` y `npm run vendor:pdfjs`, y versionar static/vendor/pdfjs con sus licencias. Railway no necesita compilar JavaScript.

## Validación, 22/09/2026

- Suite completa: **2.426 passed + 5 subtests**, 33 advertencias preexistentes/de dependencias.
- Pruebas JS existentes: **17 passed**; sintaxis de documentos.js válida.
- PostgreSQL local con schemas descartables: aislamiento entre clientes, bloqueos de guías, contador y fecha de descarga intactos, pagos pendientes sin acreditar, facturas sin cambios.
- PDFs reales sintéticos y PNG/JPEG/WEBP: render, tamaño y caché; archivo dañado y timeout recuperables.
- Chrome con CSP real: guía de 2 páginas, factura de 3 páginas, PNG, zoom, Escape, foco dentro del modal, conservación de campo sin guardar, error 404, PDF dañado y PDF protegido.
- Escritorio y viewport 390 × 844; temas oscuro y claro; filtros de Mis envíos sin recarga; retorno móvil comprobado de 2450 px a 2450 px.
- Cero llamadas a la ruta de descarga de guías durante las vistas previas. No se consultaron ni modificaron documentos de producción para estas pruebas.

Evidencia local: `qa_document_viewer_20260922/` en el workspace TAURO; preview en puerto 8786, exclusivamente con datos ficticios. La comprobación autenticada en Railway queda para después de publicar.

## Registro de ejecución

Router determinístico: `architecture --final-decision --approved`, ruta base Terra/medium. Revisión elevada al agente principal de la sesión Astra por arquitectura y permisos, conforme al mínimo de riesgo del AGENTS.md. Sin subagentes ni API de modelos. Autorización usada: implementar el visor; no autoriza publicar, borrar históricos, emitir guías ni registrar pagos reales.
