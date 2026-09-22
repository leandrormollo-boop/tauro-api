# Visor privado de documentos del portal

Implementado sobre `954bc2b`, en `codex/portal-document-viewer-20260922`, e integrado sobre `dedebe8` antes de publicar. Publicación autorizada explícitamente por el dueño el 22/09/2026: «publicalo». No cambia el esquema ni migra documentos.

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

## Ajuste visual solicitado: visor compacto

- Ancho máximo reducido de 1100 a 480 px; en paneles angostos la altura se adapta al ancho del documento.
- Tarjeta flotante con bordes suaves, fondo tenue y controles en una barra pequeña debajo del PDF; descarga y cierre como íconos.
- Entrada de 180 ms y salida de 120 ms. Cierre con X, Escape y fondo conserva la imagen durante la transición y restaura foco/scroll al finalizar. Respeta movimiento reducido.
- Cambio de página y zoom renderizan fuera de pantalla y reemplazan el canvas al terminar, evitando el parpadeo.
- Validado en Chrome, viewport móvil 390 × 844 y navegador integrado de Codex de 320 px; páginas, zoom 125%, cierre durante render, Escape, clic afuera y reapertura. Sin errores JS; 17 pruebas JS existentes aprobadas. No se repitió la suite Python porque este ajuste sólo modifica presentación y JS.
- Sigue pendiente de publicación.

## Esquinas redondeadas en las ventanas

- Regla común de 32 px para los diálogos del portal, conservada en móvil y en las tarjetas internas de formularios de clientes.
- El visor recorta su fondo según esa curva. El documento lleva un marco blanco de 8 px y esquinas de 22 px: el margen protege todos los píxeles del PDF o imagen y no modifica el archivo.
- El ajuste de página descuenta ese marco para mantener el documento completo dentro del visor.
- Validación visual en navegador integrado a 320 px y viewport de escritorio de 1280 × 900: marco visible, página completa sin desbordamiento al ajustar; segunda página, zoom, cierre y retorno del foco operativos. Sintaxis JS y diff verificados.
- Ajuste local, pendiente de publicación.

## Preparación de la publicación autorizada

- Se conserva íntegra la actualización productiva `dedebe8` de ajustes de precio por envío; integración sin conflictos.
- Se actualizan únicamente fixtures y expectativas de cuatro pruebas anteriores que no contemplaban sus columnas y los ajustes aplicados al cupo. No se cambia la lógica financiera durante esta publicación.
- Suite final integrada: **2.428 pruebas y 5 subpruebas aprobadas**, 33 advertencias; JavaScript: **17 aprobadas**, sintaxis y diff correctos. PostgreSQL local aislado, sin llamadas externas.
- Router `production_release --production-change --security-sensitive --external-write --final-decision --approved`: mínimo Sol/high y ejecución autorizada; revisión del agente principal Astra, sin delegación. Evidencia en `qa_document_viewer_20260922/release-routing.jsonl` y `release-tests-final.txt`.
- Publicar mediante avance de `main`, sin force-push. Verificar el commit exacto en Railway, `/health`, recursos del visor y lectura autenticada de documentos propios.
- Versión anterior operativa: `dedebe86e05d430bbb78b128a4826bcd6f89f9e7`, despliegue `c61ccff8-71da-4c03-8df6-5390d00014a1`. El visor no requiere una migración para volver a esa versión.
