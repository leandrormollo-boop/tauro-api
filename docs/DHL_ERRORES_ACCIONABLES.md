# Errores de DHL al emitir — 16/09/2026

Rama: `codex/dhl-emission-errors-20260916`, basada en `df32b56`.
Incluye las mejoras de portal y HS pendientes de publicación. **Versión local;
sin push ni despliegue y sin emitir guías reales.**

## Problema y resultado

Al confirmar la tarifa previa a emitir, el servicio descartaba tanto las
validaciones locales de invoice como el motivo de rechazo de DHL y devolvía
“DHL no devolvió una tarifa”. El administrador además recortaba el resultado a
200 caracteres. Ambos portales repetían el aviso en dos lugares.

Ahora se conserva la explicación a través de adaptador → cotización → reserva
del cliente → emisión → pantalla. El administrador recibe el mensaje completo.
Cada observación ocupa un párrafo, se escapa como texto y se muestra una sola
vez. Una solicitud rechazada ya no afirma que está lista para emitir.

Ejemplos:

- Ciudad del destinatario: DHL no encontró la ciudad indicada. Revisá ciudad,
  código postal y país del destinatario.
- Caja 2 (2 bultos): total declarado USD 160.00; mercadería de la factura
  comercial USD 200.00; diferencia USD 40.00. Revisar ambos cálculos y corregir
  el dato que no refleje el contenido real.
- Bulto 1, alto: completá este valor. No se emite ni se cobra.
- DHL exige un máximo de 45 caracteres en la ciudad del remitente.

El traductor identifica dirección, contactos, identificación fiscal, HS,
cantidades, valores, peso, medidas, fechas, permisos de cuenta y servicio.
Cuando DHL entrega una ruta JSON, diferencia remitente/destinatario y muestra
el índice de bulto o artículo comenzando en uno. Traduce restricciones de
longitud, límites numéricos, datos obligatorios y datos no encontrados.

## Controles y límites

- No se inventan causas a partir del código numérico. Los códigos y HTTP
  acompañan el mensaje para soporte. Motivos no reconocidos se indican como
  tales y se derivan a Tauro; no se publica el body crudo ni valores rechazados,
  números de cuenta, credenciales o detalles privados de una conexión.
- No es un traductor universal de cualquier respuesta futura de DHL: cubre
  los campos y motivos reconocidos. Muestra hasta ocho observaciones distintas
  y avisa cuando existen más; no corta una explicación a mitad de frase.
- El desfase muestra los totales de cada renglón de cajas y su diferencia con
  `Decimal`. Mantiene la validación y la tolerancia ya existentes de USD 0.02;
  no ajusta importes, valores de mercadería, tarifas ni cantidades.
- Los errores previos al POST de emisión liberan la reserva y aclaran que no
  se emitió ni cobró. Si hay timeout o respuesta incierta al emitir, permanece
  `VERIFICAR_COURIER`: no hay reintento automático. Tampoco se ofrece emitir
  desde el detalle en estado `EMITIENDO`.
- La explicación compartida también mejora los errores de validación de
  cotizaciones y recolecciones. No cambia sus reglas ni sus solicitudes.
- La corrección de datos mantiene los mecanismos existentes: el admin puede
  editar una solicitud sin guía y el cliente puede repetirla con datos
  corregidos. Esta entrega no agrega edición de solicitudes al cliente.

## Verificación

2.276 pruebas aprobadas, 5 subpruebas, 0 fallos y 0 omitidas; 33 advertencias
de la suite existente. Son 39 regresiones nuevas sobre la entrega HS.
Incluye recorrido completo de rechazo DHL `/rates` hasta el cliente, errores
de invoice antes de llamar al courier, liberación de reservas, falta de
tracking ante rechazo, incertidumbre, redirects sin truncamiento, escape HTML
y ausencia de carteles duplicados. HTTP simulado y runner con conexiones
externas bloqueadas; PostgreSQL de pruebas local en puerto 55438.

Vista visual con datos sintéticos, a 320 px: portal claro y admin oscuro;
un aviso, sin desborde horizontal. Preview local:

- `http://127.0.0.1:8776/qa/dhl-errors` — ciudad.
- `http://127.0.0.1:8776/qa/dhl-errors?caso=valor` — diferencia de valores.
- `http://127.0.0.1:8776/qa/dhl-errors?admin=1&caso=valor` — admin.

Evidencia fuera del repo:
`qa_portal_readiness_20260916/evidence/dhl-release-candidate.log`, `.xml`,
`dhl-errors-validation.json` y `dhl-errors-route.jsonl`.

Contrato consultado: [MyDHL REST 3.3.2](https://developer.dhl.com/api-reference/dhl-express-mydhl-api)
y su [OpenAPI oficial](https://developer.dhl.com/sites/default/files/2026-09/dpdhl-express-api-3.3.2.yaml).
Las pruebas incluyen ejemplos oficiales de schema validation, factura y
servicio, más casos sintéticos de ciudad, invoice y fallas de conexión.
No se migró la versión de la API contratada por el adaptador.

Sin migraciones ni dependencias nuevas. Ejecución: Astra, elegido por el
usuario; sin subagentes. El router marcó arquitectura con Terra, pero la
política de TAURO exige al menos Sol para arquitectura: se mantuvo Astra.
