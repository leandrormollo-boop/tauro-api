# Facturas DHL por correo — primera etapa

Estado: contrato de preparación, preselector Gmail y lector PDF inicial
implementados y probados localmente. Tres originales reales leídos sin
registrarlos. Sin conexión automática al correo, sin despliegue y sin cambios
en saldos ni registros de facturas reales.

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

Dependencia de extracción fijada en `pdfplumber==0.11.9`. Antes de exponer el
lector a adjuntos arbitrarios, ejecutar en worker aislado con límites de
tiempo/memoria, más cola persistente, evidencias y control de autenticidad.

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
- PostgreSQL de prueba aislado, `DATABASE_URL` vacía y dotenv desactivado.
- Base de código: `5f5908e` de `origin/main`; trabajo separado de producción.
- Router TAURO: arquitectura con impacto financiero y seguridad, ruta Sol;
  sin delegación, envío de documentos a modelos ni acciones productivas.

## Pendiente para activar

Revisar variantes, recargos adicionales y NC/ND; completar controles de origen
y cuenta contractual DHL; fijar conceptos trasladables y política cambiaria;
agregar cola persistente con reintentos/deduplicación y bandeja de revisión;
implementar y autorizar la conexión del servidor y el despliegue. La sesión
Chrome y el acceso del asistente no son credenciales del servidor. No activar
cargas automáticas antes de esas pruebas y autorizaciones.

## Referencias técnicas

- [Estructura Message y partes MIME de Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages).
- [Obtención de adjuntos Gmail](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get).
- [API de extracción y coordenadas de pdfplumber](https://github.com/jsvine/pdfplumber).
