# Piloto local OCA QA

Pantalla aislada para cotizar, emitir, recuperar PDF, consultar seguimiento y anular contra el servicio real de pruebas de OCA. No se monta en main.py; no utiliza DATABASE_URL, cuentas corrientes ni sesiones de clientes. No está publicado en producción.

## Ejecución

Instalar las dependencias del proyecto. Definir OCA_TEST_USER, OCA_TEST_PASSWORD con las credenciales QA provistas por OCA y OCA_QA_DB_PATH con una ruta local fuera del repositorio. Ejecutar `python scripts/oca_portal_qa.py` y abrir http://127.0.0.1:8787/portal/nacional/oca.

El launcher fija entorno QA, cuenta de pruebas 111757/001, operativa 64665, centro 2, un paquete y direcciones ficticias. No admite credenciales/cuenta productivas. El valor declarado sintético es 1 ARS para cumplir el contrato común de cotización; la operativa se configura sin seguro. La tarifa no es comercial y no aplica margen.

## Controles

Acceso sólo loopback y Host local; formularios con token CSRF y control de Origin. SQLite separado, huella de configuración, cotizaciones inmutables con vencimiento de 15 minutos y transición atómica antes de emitir/anular. Una respuesta de emisión incierta bloquea el reintento. El ID de OCA se guarda antes de buscar el PDF, que puede recuperarse sin emitir nuevamente. No se guardan credenciales en SQLite.

## Validación real del 24/09/2026

Desde Chrome: cotización 476 ARS de prueba, orden 20908665, guía 1217400000000333771, PDF visible y anulación confirmada. Seguimiento código 54: Envío Cancelado. Una sola emisión en este piloto. Evidencia en auditoria_local/oca_qa_20260924 del workspace TAURO.

## Alcance pendiente

El flujo autenticado se agregó en esta rama; ver OCA_PORTAL_INTEGRACION.md. Todavía faltan validación y configuración productiva, condiciones comerciales y aprobación de publicación. Este piloto no valida seguro ni logística inversa de las operativas TAURO 472095/472096.
