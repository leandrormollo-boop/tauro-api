# Operación de privacidad Shopify

Este runbook cubre datos de compradores que TAURO recibe desde Shopify. No
reemplaza una política legal ni afirma que Railway, Google Drive o Google
Sheets ya tengan una retención configurada: esos controles externos deben ser
aplicados y verificados por una persona con acceso a cada cuenta.

## Controles que sí están en el código

- Las solicitudes de privacidad se registran sin email, teléfono ni body crudo.
- Los pedidos huérfanos vencen a los 90 días. Hay poda oportunista al recibir o
  vincular una tienda y una poda diaria para tiendas dormidas.
- Los cuerpos de error de FedEx, DHL y UPS no se imprimen en logs. La
  observabilidad conserva HTTP, código del courier y referencia técnica cuando
  existe.
- Guías, facturas, comprobantes, backups y exportaciones privadas responden
  `Cache-Control: private, no-store`.
- `customers/redact` y `shop/redact` limpian en una sola transacción las copias
  derivadas del pedido: destinatario/remitente según corresponda, label,
  tracking, descripción del envío y datos libres de recolección. Se
  conservan sólo filas e importes/fechas/estados necesarios como evidencia
  financiera u operativa, y el aislamiento entre tiendas se prueba con
  PostgreSQL real.
- El espejo operativo nuevo usa la pestaña `PLATAFORMA_SIN_PII`: no copia
  nombre, ciudad ni tracking del comprador.

## Google Sheet histórico: decisión pendiente del responsable

La pestaña histórica `PLATAFORMA` puede contener nombre, ciudad y tracking de
compradores. El job nuevo **no la modifica ni la borra**; escribe únicamente en
`PLATAFORMA_SIN_PII`. Así se evita una eliminación silenciosa sin conocer la
obligación contable u operativa aplicable.

Antes de dar por cerrado el histórico, el responsable de TAURO debe:

1. identificar propietarios y personas con acceso al Sheet;
2. definir por escrito finalidad y plazo de retención;
3. decidir si corresponde anonimizar, exportar a un repositorio restringido o
   eliminar la pestaña histórica;
4. registrar fecha, alcance, responsable y evidencia de la decisión;
5. revisar también versiones, copias y exportaciones existentes en Drive.

Hasta completar esos pasos, `PLATAFORMA` es una deuda operativa conocida. No se
debe reactivar ningún job que vuelva a escribir PII allí.

## Backups: requisitos externos pendientes

Los snapshots de PostgreSQL y las copias descargadas desde el admin pueden
conservar datos que luego fueron anonimizados. La aplicación no controla por sí
sola la retención de Railway ni las copias guardadas en Drive o equipos.

El responsable de infraestructura debe configurar y documentar:

- cifrado y acceso mínimo;
- retención máxima y eliminación automática de snapshots vencidos;
- inventario de copias manuales y su ubicación;
- prueba periódica de restauración en un entorno aislado;
- responsable y evidencia de cada restauración.

No se considera aplicado hasta verificarlo en los paneles de Railway/Drive y
guardar evidencia. Un backup del admin tampoco debe enviarse por correo ni
guardarse en una carpeta pública.

## Restauración sin reintroducir PII borrada

Nunca se publica una base restaurada directamente. Procedimiento obligatorio:

1. poner el servicio de destino sin tráfico público y sin workers salientes;
2. registrar el identificador y la fecha del snapshot;
3. exportar desde la producción vigente el ledger de solicitudes GDPR y todas
   las redacciones posteriores a la fecha del snapshot;
4. restaurar y aplicar el schema actual;
5. volver a ejecutar en la copia restaurada cada `customers/redact` y
   `shop/redact` posterior al snapshot, de forma idempotente;
6. ejecutar la poda de huérfanos y confirmar que no quede ninguno con más de
   90 días;
7. comprobar que las solicitudes de acceso pendientes sigan pendientes y que
   sus exportaciones no incluyan labels binarios;
8. auditar por dominio y pedido una muestra de redacciones y guardar sólo
   conteos/IDs técnicos como evidencia;
9. habilitar tráfico únicamente con aprobación del responsable de privacidad.

Si no existe una fuente confiable para reconstruir las redacciones posteriores
al snapshot, la restauración queda **bloqueada**: no se puede afirmar que esa
base cumple privacidad. Mantener el ledger fuera del ciclo de vida del snapshot
es una tarea de infraestructura todavía pendiente.

## Verificación técnica mínima

Después de cada cambio o restauración:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_privacy_hardening.py tests/test_shopify_gdpr.py
TAURO_TEST_DATABASE_URL=<base-aislada> PYTHONPATH=. .venv/bin/pytest -q tests/test_shopify_gdpr_postgres.py
rg -n '\.text' core/fedex_client.py core/dhl_client.py core/ups_client.py
```

El segundo comando debe terminar sin coincidencias. También se debe probar que
las descargas autenticadas llevan `private, no-store` y que el scheduler tiene
registrada la poda diaria de huérfanos.
