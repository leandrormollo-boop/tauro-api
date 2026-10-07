# Marca TAURO en las invoices DHL del portal

La descarga del cliente conserva un único PDF: primero las etiquetas de DHL y
después la factura comercial. `servicios/invoice_marca.py` agrega el logo negro
de TAURO en la esquina superior derecha de cada página compatible de la invoice.
El visor de facturas utiliza la misma copia de presentación.

## Referencia y alcance

- Formato y ubicación aprobados por TAURO el 02/10/2026.
- `static/img/tauro-invoice-logo.png` es una copia sin modificaciones del recurso
  oficial `tauro-personalizar-guias-boxfly/assets/tauro-logo-boxfly.png`. Se usa
  esta versión negra, aprobada en el ejemplo, para conservar la identidad del PDF.
- Compatible con `COMMERCIAL_INVOICE_P_10`: páginas verticales de aproximadamente
  595 × 841 puntos, encabezado identificable y zona de marca totalmente libre.
- No modifica la emisión, los documentos enviados a DHL, los importes ni la base
  de datos. El administrador sigue teniendo acceso al original de DHL.

## Conservación y acceso

Los getters verifican primero el cliente dueño y la visibilidad del envío. El
render se ejecuta después de cerrar la conexión de base de datos. La caché es
interna, por hash del contenido, con un máximo de 8 MiB y 64 documentos; no saltea
la autorización de cada solicitud.

La marca se agrega como imagen sobre el PDF original, sin rasterizar el documento.
Se comprueban el texto, la cantidad de páginas y sus tamaños. Antes de colocar la
imagen se renderiza la zona para comprobar que no contenga texto, dibujos u otras
imágenes. Las etiquetas no pasan por el proceso de marca.

Ante formatos distintos, páginas rotadas, formularios, anotaciones, una zona
ocupada o un error del renderer se devuelve el original completo. Un documento
marcado no se marca nuevamente. El proceso está limitado a 30 páginas, 32 MiB,
dos workers simultáneos y 10 segundos; en Linux también tiene límite de memoria.

## Verificación

`test_invoice_marca.py` comprueba PDFs sintéticos con varias páginas, conservación
de píxeles fuera del logo, texto y geometría, idempotencia y fallbacks.
`test_portal_pdf_unificado.py` cubre el PDF combinado y el visor con el mismo
contenido, el nombre de descarga y la conservación del original del administrador.
La suite de CI incluye también aislamiento por cliente y numeración de guías.

Los documentos reales usados como referencia y sus renders permanecen fuera del
repositorio. En la muestra aprobada se verificaron las dos etiquetas píxel por
píxel y la invoice fuera de la zona del logo, sin diferencias.
