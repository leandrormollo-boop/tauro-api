# Respaldos y restauración

## Alcance de los artefactos

`/admin/backup.json` es una **exportación parcial administrativa**. Sirve para inspección o extracción acotada, pero no contiene todos los objetos, contratos ni datos necesarios para reconstruir PostgreSQL. No debe describirse, retenerse ni probarse como un backup restaurable.

Un respaldo restaurable de la aplicación es un archivo creado por `pg_dump` en formato custom (`-Fc`), producido por una política operativa externa que también defina frecuencia, retención, cifrado, acceso y monitoreo. Este repositorio no afirma que esa política esté activa en Railway.

## Ensayo local aislado

`scripts/verificar_restauracion.py` prueba un archivo custom contra una base que debe existir y estar vacía. El helper sólo acepta `localhost` o `127.0.0.1` y nombres que comiencen con `tauro_restore_test_`. Nunca crea, elimina, limpia ni sobrescribe la base. Ejecuta `pg_restore` con:

```text
--exit-on-error --single-transaction --no-owner --no-privileges
```

Ejemplo local, sin una URL de conexión:

```bash
/opt/homebrew/bin/createdb -h 127.0.0.1 tauro_restore_test_20260929
python scripts/verificar_restauracion.py \
  --archive /ruta/segura/respaldo.dump \
  --host 127.0.0.1 \
  --database tauro_restore_test_20260929 \
  --report /tmp/tauro_restore_test_20260929.json
```

El usuario se toma de `--user`, `PGUSER` o el usuario local. Si la instancia local exige contraseña, se entrega mediante `PGPASSWORD`; el helper no acepta DSN ni contraseña como argumentos. Host, puerto, usuario y contraseña van en variables `PG*`; `pg_restore` recibe como argumento únicamente el nombre local de base ya validado porque su CLI exige `--dbname`. El informe omite usuario, credenciales, rutas y filas: registra fecha UTC, SHA-256 y tamaño del archivo, destino local, contratos de readiness, recuentos de `envios`, `pagos` y `solicitudes_guia`, y bytes agregados de documentos disponibles.

Antes de reintentar hay que eliminar manualmente la base fallida y crear otra vacía con el prefijo permitido. Esta separación evita que un ensayo parcial parezca exitoso y evita que el helper ejecute `drop`, `clean` o `create`.

## Comparación con manifest

Puede aportarse un manifest sin datos personales para contrastar recuentos y bytes esperados:

```json
{
  "row_counts": {
    "envios": 1,
    "pagos": 1,
    "solicitudes_guia": 1
  },
  "document_bytes": {
    "envios.factura_pdf": 12,
    "pagos.comprobante": 8,
    "solicitudes_guia.label_pdf": 10,
    "solicitudes_guia.commercial_invoice_pdf": 11
  }
}
```

Se pasa con `--manifest /ruta/manifest.json`. Cualquier diferencia termina el ensayo con error. El manifest prueba integridad cuantitativa del archivo conocido; no prueba por sí solo completitud semántica ni vigencia.

## Qué demuestra el ensayo

La prueba automatizada crea bases ficticias locales, aplica el esquema actual, inserta filas sintéticas, genera un `pg_dump -Fc` y restaura en otra base vacía. Así valida que el procedimiento, las opciones de restauración, los contratos de readiness y las verificaciones cuantitativas funcionan juntos.

Este ensayo **no valida la vigencia, retención, cifrado, disponibilidad ni restaurabilidad de un backup real de Railway**. Esa evidencia requiere seleccionar un respaldo real según la política operativa, copiarlo por un canal autorizado y repetir el procedimiento en infraestructura aislada aprobada. Ninguna de esas acciones se ejecuta desde esta prueba local.
