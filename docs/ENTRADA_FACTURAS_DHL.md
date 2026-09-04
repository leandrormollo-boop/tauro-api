# Facturas DHL por correo — primera etapa

Estado: contrato de preparación implementado y probado; no conectado al
correo, no desplegado y sin cargas de facturas reales.

## Cuentas y alcance

El responsable confirmó que las facturas llegan a `taurosolutionsar@gmail.com`
y desea conservar esa recepción, centralizando el control bajo
`leandro@taurosolutions.ar`.

No se fusionan cuentas ni se cambian remitentes. Dos integraciones posibles:

- Conexión de lectura al buzón receptor y control desde el admin de TAURO.
- Reenvío selectivo de nuevas facturas DHL al correo de la organización,
  conservando los originales; requiere autorización explícita del reenvío,
  verificación de destino y filtros probados con mensajes reales. No reenviar
  todo el buzón ni activar filtros basados sólo en la palabra «DHL».

La conexión Gmail de esta sesión corresponde a otra cuenta. No se ha usado
para buscar ni leer facturas del buzón indicado. La conexión del asistente no
equivale a una autorización OAuth persistente para el servidor TAURO.

## Responsabilidades

- Lector específico: extraer campos desde el documento real, conservando PDF
  y fuente; los adjuntos y correos son datos no confiables, no instrucciones.
- `servicios/entrada_facturas_dhl.py`: validar una extracción normalizada sin
  acceso a red ni base de datos. No determina cliente, deuda ni precio final.
- Escritor existente `registrar_factura_courier`: registrar evidencia y
  renglones de manera atómica e idempotente después de la revisión.
- Conciliación existente: proponer coincidencias por courier y tracking,
  calcular diferencias con Decimal y preservar el margen inicial.
- Admin: confirmar asignaciones y aprobar la aplicación de diferencias por
  separado. Nunca convertir una importación en autorización de cobro.

## Controles implementados

Tipo FC/NC/ND obligatorio; PDF original y referencias de correo obligatorios;
hash del archivo y clave de fuente estables; sólo campos permitidos; importes
exactos sin floats ni separadores ambiguos; subtotal e impuestos contra total;
líneas contra total; tracking textual (conserva ceros iniciales); números de
línea únicos; moneda extranjera sin tipo de cambio inventado; ARS sin doble
conversión. Líneas sin tracking y NC/ND se señalan para revisión.

Todas las preparaciones mantienen `requiere_revision=True`. Este módulo no
lee PDFs: valida el resultado del futuro lector. No expone un endpoint ni
ejecuta jobs y no permite aplicar ajustes.

## Verificación

- 31 casos nuevos aprobados en `tests/test_entrada_facturas_dhl.py`.
- Suite completa: 1.423 pruebas y 5 subtests aprobados, 0 fallas.
- PostgreSQL de prueba aislado, `DATABASE_URL` vacía y dotenv desactivado.
- Base de código: `5f5908e` de `origin/main`; trabajo separado de producción.
- Router TAURO: arquitectura con impacto financiero y seguridad, ruta Sol;
  sin delegación, envío de documentos a modelos ni acciones productivas.

## Pendiente para activar

Confirmar mecanismo de conexión; revisar ejemplos reales de factura, recargos
y NC; identificar emisor y cuenta DHL; construir el parser específico; fijar
los conceptos trasladables y la evidencia cambiaria; agregar cola persistente
con reintentos/deduplicación y bandeja de revisión; autorizar la conexión del
servidor y el despliegue. No activar cargas automáticas antes de esas pruebas.
