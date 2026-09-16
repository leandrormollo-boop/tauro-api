# Asistente HS del portal — 16/09/2026

**Implementado localmente, sin publicar.** Rama `codex/hs-code-assistant-20260916`,
sobre la entrega del portal `5cd977b`. Conserva aquella entrega completa.

## Experiencia

Al completar la descripción del catálogo o de un artículo de la invoice, el
portal busca automáticamente posibles HS. Muestra la descripción de referencia,
la fuente y preguntas para completar material, construcción o uso cuando son
relevantes. El cliente elige «Usar código»; el campo se completa sin teclearlo.
Las sugerencias no se guardan ni se aprueban por el hecho de aparecer.

Cada artículo conserva su consulta por separado. Si se modifica su descripción
tras elegir una sugerencia, se limpia ese código para evitar declaraciones
incoherentes. Los códigos manuales se conservan. Los productos del catálogo
siguen el circuito existente de aprobación de TAURO.

El catálogo admite HS de seis dígitos y extensiones de ocho/diez, con o sin
puntos, sin completar ceros ni truncar extensiones. Las sugerencias nuevas son
siempre de seis dígitos. La parte nacional necesita su nomenclatura de destino;
no se presenta NCM, HTS ni TARIC como una extensión universal.

## Base y procedencia

Se descargó y verificó primero el catálogo HS2022 H6 de UNSD/UN Comtrade:
5.612 códigos internacionales. La entrega utiliza **los textos del HTS publicado
por USITC**, cuyo catálogo oficial identifica el conjunto como dominio público.
Se proyectó su jerarquía a seis dígitos y se contrastó el conjunto completo de
identificadores contra H6: coincidencia exacta, sin faltantes ni códigos extras.
No se redistribuyen textos de Comtrade en esta entrega.

- [Catálogo oficial y licencia de USITC](https://www.usitc.gov/data.json).
- [Archivo utilizado: HTS 2026 Revision 19](https://www.usitc.gov/sites/default/files/tata/hts/hts_2026_revision_19_json.json).
- [Referencia internacional para contrastar cobertura](https://unstats.un.org/unsd/classifications/econ).
- [Guía oficial de estructura y uso de USITC](https://www.usitc.gov/documents/hts/hts_external_user_guide.pdf).

`datos/hs2022.json` conserva edición, URL, licencia, fecha y SHA256 de origen.
Se excluyen capítulos nacionales 98/99, sufijos nacionales, tipos de arancel,
notas legales e impuestos. Cuando la fuente agrupa el HS con extensiones, se
retiene el ancestro común de todas sus ramas, no una rama nacional arbitraria.
Las descripciones jerárquicas son una referencia de búsqueda, no una copia de
las notas explicativas completas de la OMA.

Para actualizar: descargar una edición oficial y ejecutar
`scripts/actualizar_hs.py --archivo ARCHIVO --url URL_OFICIAL --edicion EDICION`.
El importador exige 5.612 códigos para HS2022 y frena ante variaciones de cobertura.
Revisar los cambios y pruebas antes de publicar; no actualiza producción solo.
El cambio de edición internacional requiere adaptar también esa validación.

## Motor y límites

Esta primera versión es **búsqueda asistida determinista**, con vocabulario
español/inglés, ordenamiento léxico, atención a exclusiones textuales y preguntas
por familia. No es un clasificador semántico general ni emite una decisión
aduanera definitiva. El vocabulario español no cubre todos los productos; la
invoice mantiene su descripción en inglés. Las descripciones muy genéricas o
sin coincidencias piden detalle. Incluso un único candidato necesita elección.

Una base descargada por sí sola no permite clasificar automáticamente toda
mercadería: también importan reglas interpretativas, notas, composición, uso y
criterios del destino. La próxima capa recomendada es interpretación semántica
apoyada en recuperación de evidencia y notas autorizadas, evaluada con ejemplos
revisados por TAURO. Ningún modelo debe generar códigos fuera del catálogo ni
inventar confianza numérica. No se agregó una integración IA sin configurar ni
se efectuaron llamadas pagas.

## Controles y pruebas

- API POST autenticada `/portal/api/hs-code`, protegida por el control de origen
  existente, máximo 8 KiB por consulta, validación de tipo/longitud y 60 consultas
  por cliente por minuto. Respuestas privadas sin caché compartida.
- Consulta local sin base de clientes, sin transmitir declaraciones a terceros,
  sin escrituras contables ni emisión de guías. Índice en memoria por proceso;
  no consulta la fuente externa durante la operación del cliente.
- El navegador descarta respuestas viejas, cancela consultas previas, limita la
  espera y construye el texto como texto DOM. Al clonar artículos limpia los
  resultados anteriores. La carga manual permanece disponible ante fallos.
- **2.237 pruebas y 5 subpruebas aprobadas, 0 fallos, 0 omitidas**; 33 advertencias,
  32,89 s. Incluye 35 pruebas nuevas del HS: cobertura, códigos, idiomas,
  ambigüedad, formatos, permisos, tamaño, rate limit y referencia no disponible.
- La primera corrida ampliada falló porque PostgreSQL local arrancó en 5432 y
  el runner esperaba 55438. Se corrigió el puerto y se repitió toda la suite;
  no fue una modificación de datos productivos ni se omitieron esos tests.
- Sintaxis Python 3.11 y JavaScript verificadas; revisión visual local en
  catálogo e invoice con datos ficticios. Se verificó elección, invalidación,
  preservación de código manual y aislamiento al agregar artículos.

La suite valida los comportamientos programados; no mide precisión aduanera
universal. Antes de anunciar clasificación sin intervención debe construirse
un conjunto de productos etiquetados por especialistas y medir sus errores.

## Estado de publicación

Los cambios permanecen locales. No se obtuvo aún autorización de producción
para esta rama ni para su entrega base. No hay migraciones ni dependencias
nuevas. La publicación debe integrar ambos commits y verificar las sesiones
reales de cliente/admin después del despliegue.
