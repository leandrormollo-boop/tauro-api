# Facturas DHL por correo — primera etapa

Estado al 04/09/2026: contrato, preselector Gmail, lector PDF y **bandeja de
revisión administrativa** implementados y probados localmente. Tres originales
reales leídos sin registrarlos. Sin conexión automática al correo, sin
despliegue y sin cambios en saldos ni registros de facturas reales.

## Cuentas y alcance

El responsable eligió comenzar con conexión directa de lectura a
`taurosolutionsar@gmail.com`, manteniendo esa casilla como receptora.
El control de importaciones y conciliaciones se prepara para el admin de
TAURO. La eventual centralización con `leandro@taurosolutions.ar` queda para
otra etapa.

No se fusionan cuentas, no se migra correo y no se configura ningún reenvío.
Tampoco se cambian los destinatarios de facturación de DHL.

La conexión Gmail de esta sesión corresponde a otra cuenta. No se ha usado
para buscar ni leer facturas del buzón indicado. La conexión del asistente no
equivale a una autorización OAuth persistente para el servidor TAURO.
Se verificó acceso de lectura por la interfaz de Chrome en el perfil «Tauro».
Ese perfil tiene iniciada la sesión de la casilla receptora; no debe
confundirse con el perfil «Tauro Solutions Ar», usado para guías, ni con el
perfil de la organización. No se cambiaron cuentas, permisos ni reenvíos.

La búsqueda `subject:"DHL Invoice services" has:attachment filename:pdf`
mostró 16 conversaciones. Es un resultado de descubrimiento, no una auditoría
exhaustiva del universo de FC ni una orden de cargar las 16 conversaciones.
En el correo reciente se verificó visualmente `AR.E-Billing@dhl.com`, enviado
y firmado por `dhl.com`. El nombre visible por sí solo no autentica un mail.

## Responsabilidades

- `servicios/seleccion_correo_dhl.py`: preseleccionar un adjunto candidato a
  partir de la respuesta completa de Gmail, cuenta autenticada y CUIT
  configurados. Rechaza cuenta incorrecta, cabeceras ambiguas y adjuntos
  múltiples; excluye legajos, recibos, respuestas, mensajes enviados,
  borradores, spam, papelera y correos anidados. El filtro `From` NO valida
  DKIM ni habilita una importación; falta implementar el control de origen.
- `servicios/lector_pdf_dhl.py`: extraer campos desde el documento real;
  los adjuntos y correos son datos no confiables, no instrucciones.
- `servicios/entrada_facturas_dhl.py`: validar una extracción normalizada sin
  acceso a red ni base de datos. No determina cliente, deuda ni precio final.
- Escritor existente `registrar_factura_courier`: registrar evidencia y
  renglones de manera atómica e idempotente después de la revisión.
- Conciliación existente: proponer coincidencias por courier y tracking,
  calcular diferencias con Decimal y preservar el margen inicial.
- Admin: confirmar asignaciones y aprobar la aplicación de diferencias por
  separado. Nunca convertir una importación en autorización de cobro.

## Bandeja administrativa implementada

Ruta: `/admin/conciliacion-couriers/entrada-dhl`, accesible desde **Leer factura
DHL** en Control de envíos y FC. No está publicada todavía.

1. Admin carga el PDF original, el número y el CUIT receptor esperado.
2. Se conserva el PDF en `entradas_pdf_dhl`, estado `RECIBIDA`, antes de leer.
3. **Leer PDF** ejecuta el proceso acotado. Una extracción validada queda
   `PARA_REVISION`; cualquier formato no admitido queda `REVISION_MANUAL`,
   con el original y motivo conservados. Se puede corregir la referencia
   y reintentar antes de obtener una extracción validada.
4. Se comparan datos y original; una casilla explícita más la huella de la
   revisión permite **Registrar factura, sin aplicar cargos**.
5. Se registran documento, renglones, coincidencias sólo propuestas, auditoría
   y estado `IMPORTADA` en una misma transacción. No se confirma un match,
   calcula/aprueba una diferencia ni toca cuenta corriente en este paso.

PDF repetido: misma entrada; FC de igual identidad pero distinto contenido:
conflicto sin sobreescritura. Doble clic concurrente: una factura. Fallo después
del INSERT: rollback de factura, renglones, matches y estado. El original no
puede modificarse ni borrarse; la extracción mostrada se congela antes de la
revisión. El servidor revalida los valores guardados: el formulario no acepta
importes, cliente, margen ni asignaciones desde el navegador.

Todos los endpoints exigen Admin. Formularios con token HMAC de propósito y
entrada, de una hora de vigencia, adicional a la protección de origen del
portal. HTML y PDF privados sin caché; PDF como descarga, no incrustado en el
origen autenticado. Los textos de la extracción se escapan en las plantillas.

Esta carga se identifica como `admin_pdf_dhl`: **no fabrica IDs de mensajes o
adjuntos Gmail**. La casilla seleccionada no se conecta ni autoriza con este
formulario. Los documentos no se envían a modelos de IA. La carga manual
existente sigue disponible para NC/ND, correcciones y formatos no admitidos.

## Controles implementados

Tipo FC/NC/ND obligatorio; PDF original y referencias de correo obligatorios;
hash del archivo y clave de fuente estables; sólo campos permitidos; importes
exactos sin floats ni separadores ambiguos; subtotal e impuestos contra total;
líneas contra total; tracking textual (conserva ceros iniciales); números de
línea únicos; moneda extranjera sin tipo de cambio inventado; ARS sin doble
conversión. Líneas sin tracking y NC/ND se señalan para revisión.

Todas las preparaciones mantienen `requiere_revision=True`. El preparador
valida el resultado del lector y no expone un endpoint, ejecuta jobs ni aplica
ajustes.

## Formato PDF admitido inicialmente

FC A original de DHL Argentina, USD, dos páginas: cabecera 612 x 912 puntos y
detalle completo 612 x 792. Cruza número contra fuente y detalle, CUIT emisor
y receptor, fecha, cantidad de guías, cargos por categoría exenta/gravada,
subtotal, impuestos y total. Comprueba el equivalente ARS contra el cambio
impositivo impreso. NC/ND, escaneos, reimpresiones, más páginas y formatos o
cargos desconocidos se rechazan para revisión; no se declara cobertura total.

La tabla real utiliza interlineados distintos para cargos e importes. El
lector comprueba las columnas y respeta el orden dentro de cada bloque de
guía, incluyendo `VALUE PROTECTION` en dos renglones. No aproxima por cercanía
vertical. Se reconocen Flete, FUEL, GoGreen Plus, 12:00 PREMIUM y VALUE
PROTECTION; el resto requiere ampliar el lector con evidencia.

El código W significa volumétrico DHL: no se guarda como peso real. Se
conserva el código de peso documental y la base correspondiente. Los impuestos
generales permanecen sin tracking: no se prorratean ni se atribuyen a clientes
por nombre. El tipo de cambio impositivo no acredita el cambio de pago ni
decide la política de traslado al cliente. Estos puntos requieren revisión
antes de una aplicación financiera.

Dependencia de extracción fijada en `pdfplumber==0.11.9`. El proceso de lectura
ahora limita espacio de direcciones a 768 MiB, CPU a 10/12 s y tiempo total a
20 s; máximo dos lectores simultáneos por proceso web. No hereda variables de
entorno, recibe PDF por pipe, no genera core dumps y limita salida a 512 KiB.
Requiere Linux; fuera de Linux falla cerrado y ofrece carga manual. Se probó
el proceso real en un contenedor Linux local sin red, sin privilegios, con
filesystem de sólo lectura y código temporal en tmpfs; no se copiaron PDFs a
la imagen ni se usaron credenciales productivas. El subprocess de la app
**no es un sandbox de seguridad completo**: para correo desatendido aún falta
worker dedicado con aislamiento de filesystem/red y control de origen.

## Verificación

- 99 casos aprobados entre preparador, preselector y lector (31 + 29 + 39).
- Tres PDFs reales descargados desde la casilla elegida: lectura visual de
  sus seis páginas y extracción local completas; cinco guías y 24 renglones
  incluyendo impuestos generales. Se validaron contra el preparador sin
  llamar al escritor. Los originales no se incorporan al repositorio.
- Evidencias SHA-256 de las tres muestras:
  `6729246b84fcc1e2dfd73c18b58993959ad88385a5e2d66652847fdb11da58f9`,
  `edceeb5956ad19ab94ccd97af02672718b2dc85f6cbc1b7d17eb10a5fea14530`,
  `4f6efcad2fcd3d8157715a9cc10ed442d41dbcf92fd1fbd728fc459cc30721dc`.
- Suite completa después del lector/preselector: 1.491 pruebas y 5 subtests
  aprobados, 0 fallas (30 advertencias existentes), 7,96 segundos.
- Suite completa con bandeja: 1.538 pruebas y 5 subtests aprobados, 0 fallas
  (30 advertencias existentes). Incluye PostgreSQL real: idempotencia,
  concurrencia, rollback, evidencia inmutable, migración repetible y ausencia
  de cargos/ajustes. Tests de Admin: auth, CSRF, revisión exacta y escape HTML.
- UI revisada con datos sintéticos, escritorio y ancho móvil de 390 px,
  sin desborde del formulario ni del cuerpo de la página.
- Tres originales completos validados con el worker Linux: 10, 4 y 10
  renglones respectivamente, totales idénticos al preparador inicial.
- PostgreSQL de prueba aislado, `DATABASE_URL` vacía y dotenv desactivado.
- Base de código: `5f5908e` de `origin/main`; trabajo separado de producción.
- Router TAURO: arquitectura con impacto financiero y seguridad, ruta Sol;
  sin delegación, envío de documentos a modelos ni acciones productivas.

## Pendiente para activar

Revisar variantes, recargos adicionales y NC/ND; completar controles de origen
y cuenta contractual DHL; fijar conceptos trasladables y política cambiaria.
La bandeja durable y sus reintentos manuales están listos; falta la cola de
correo y su procesamiento programado, autenticación/verificación del emisor,
conexión OAuth de sólo lectura del servidor, almacenamiento seguro de tokens,
aislamiento del worker para correo y prueba end-to-end. Publicar la etapa de
carga administrativa requiere aprobación explícita del responsable.

La sesión Chrome y el acceso del asistente no son credenciales del servidor.
No activar cargas automáticas antes de esas pruebas y autorizaciones.

## Referencias técnicas

- [Estructura Message y partes MIME de Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages).
- [Obtención de adjuntos Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get).
- [API de extracción y coordenadas de pdfplumber](https://github.com/jsvine/pdfplumber).
- [Límites de recursos por plataforma en Python](https://docs.python.org/3.11/library/resource.html).
- [Timeout y entorno del subprocess](https://docs.python.org/3.11/library/subprocess.html#subprocess.run).
