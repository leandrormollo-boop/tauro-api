# Pruebas automáticas reproducibles

La plantilla **inactiva** `docs/ci/audit-ci.yml` separa la validación del backend,
la compilación web y el build de la imagen. Queda pendiente su activación en `.github/workflows/audit-ci.yml` con una credencial autorizada para `workflow`. Esta publicación no modifica los permisos de GitHub ni activa Actions. La plantilla no publica la imagen, no usa
credenciales de couriers ni llama a sus APIs. PostgreSQL vive en un contenedor
aislado y cada fixture crea y elimina su propio schema.

## Dependencias Python

`requirements.lock` contiene el cierre transitivo de `requirements.txt`, con
versiones y hashes. Fue resuelto para CPython 3.11 y Linux x86_64 manylinux 2.17.
Las dependencias directas conservan las versiones del entorno funcional de
auditoría cuando son compatibles con Python 3.11.

Instalación de runtime, igual a la imagen Docker:

```bash
python -m pip install --only-binary=:all: --require-hashes -r requirements.lock
python -m pip check
```

Para regenerar el mismo cierre con `uv`, usando el lock actual como conjunto de
restricciones:

```bash
uv pip compile requirements.txt \
  --constraints requirements.lock \
  --python-version 3.11 \
  --python-platform x86_64-manylinux_2_17 \
  --generate-hashes \
  --only-binary=:all: \
  --no-annotate \
  --output-file requirements.lock
```

Ese comando conserva las versiones existentes. Para actualizar una dependencia
hay que cambiar de forma deliberada su versión en el lock o usar
`--upgrade-package NOMBRE`, revisar el diff y volver a ejecutar las pruebas.
`--only-binary=:all:` hace fallar la resolución si una dependencia no publica
un wheel compatible, en lugar de introducir una compilación no controlada.

La disponibilidad de todos los wheels Linux del lock se puede verificar sin
instalarlos:

```bash
python -m pip download \
  --dest /tmp/tauro-lock-linux-wheels \
  --platform manylinux2014_x86_64 \
  --python-version 3.11 \
  --implementation cp \
  --abi cp311 \
  --only-binary=:all: \
  --require-hashes \
  -r requirements.lock
```

Con un daemon Docker activo, la misma imagen que CI construye sin publicar se
valida con:

```bash
docker build --tag tauro:audit .
```

Pytest es una herramienta de CI y no forma parte del runtime de producción. El
workflow fija también sus dependencias directas:

```bash
python -m pip install \
  pytest==9.1.1 iniconfig==2.3.0 packaging==26.3 \
  pluggy==1.6.0 pygments==2.21.0 \
  httpx2==2.13.1 httpcore2==2.13.1 truststore==0.10.4
```

`httpx2` es el transporte que Starlette 1.7 carga solamente para sus clientes
de prueba. Queda fuera de `requirements.lock` y de la imagen de producción.
Si el entorno fue creado con `uv venv` sin `pip`, el chequeo equivalente es
`uv pip check --python .venv/bin/python`.

## Suite Python auditada

Con PostgreSQL local disponible, crear una base vacía y exportar únicamente su
URL de prueba:

```bash
createdb tauro_test
export TAURO_TEST_DATABASE_URL='postgresql://localhost/tauro_test'
python -m pytest \
  tests/test_integridad_pagos_postgres.py \
  tests/test_pagos_documentales_postgres.py \
  tests/test_imputacion_pagos_portal.py \
  tests/test_conciliacion_couriers_postgres.py \
  tests/test_libreta_aislada.py \
  tests/test_visibilidad_envios_cliente.py \
  tests/test_shopify_catalogo_postgres.py \
  tests/test_fedex_emision_recuperacion.py \
  tests/test_dhl_emision.py \
  tests/test_portal_experiencia_cliente.py \
  tests/test_cotizacion_portal_handoff.py \
  tests/test_ubicaciones_handoff.py \
  tests/test_control_automatizaciones.py \
  tests/test_backup_privacy.py \
  tests/test_restauracion_aislada.py \
  tests/test_experiencia_cuenta.py \
  tests/test_periodo_cuenta.py \
  tests/test_admin_cuenta_ambitos.py \
  tests/test_admin_pago_revision_ui.py \
  tests/test_ups_emision.py \
  tests/test_monitoreo_guias_reemplazadas.py \
  tests/test_recolecciones_portal_claridad.py \
  tests/test_carrier_contract.py \
  tests/test_tiendanube_app.py \
  tests/test_tiendanube_preflight.py \
  tests/test_admin_negocio_postgres.py \
  tests/test_control_negocio.py \
  tests/test_numeros_operativos_fail_closed.py \
  tests/test_dhl_financial_safety.py
```

La selección cubre identidad e imputación documental de pagos, conciliación de
facturas courier, aislamiento entre clientes, recuperación de emisiones
inciertas y el handoff de cotización y domicilios hacia la solicitud. Las
pruebas de automatizaciones, backup y restauración controlan evidencia durable,
exclusión de secretos y destino local. Las pruebas de courier usan dobles
locales y no emiten guías, retiros ni mensajes.

También se protegen la claridad de recolecciones, el monitoreo de guías
reemplazadas, los contratos publicados de carriers, el preflight de Tiendanube
y los cálculos financieros que deben fallar de forma cerrada ante datos
incompletos.

CI instala `postgresql-client-16` y antepone
`/usr/lib/postgresql/16/bin` al `PATH`. Así `pg_dump` y `pg_restore` usan la
misma versión mayor que el servicio PostgreSQL 16, aunque la imagen del runner
incluya otro cliente por defecto.

## Build y pruebas JavaScript

```bash
npm ci
npm run build:web
node --test \
  tests/js/payment-allocation.test.cjs \
  tests/js/pago-preview.test.cjs \
  tests/js/quote-request.test.cjs \
  tests/js/quote-locations.test.cjs \
  tests/js/shipment-location-confirmation.test.cjs
```

`npm ci` instala exactamente el árbol de `package-lock.json`. El build genera
`static/js/app.js`; si cambia, el archivo generado debe revisarse junto con sus
componentes fuente.

## Alcance de la validación

La ejecución local valida el lock con CPython 3.11 y las suites sin red externa.
Una vez activado, el workflow validará además la instalación sobre Ubuntu y PostgreSQL en un
contenedor de servicio. No reemplaza UAT con cuentas reales de FedEx, DHL, OCA,
Shopify o Tiendanube, y no autoriza publicación ni activación de integraciones.
El estado de GitHub Actions debe verificarse en cada commit o pull request; la
presencia del workflow no implica que una ejecución remota haya finalizado.
