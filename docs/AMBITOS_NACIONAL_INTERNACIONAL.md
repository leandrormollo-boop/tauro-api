# Portal por ámbito: Nacional / Internacional

Decisión de producto del 15/08/2026: el cliente nunca opera sobre una lista o
cotización que mezcle envíos nacionales e internacionales. La cuenta corriente
por ámbito ya está implementada en la rama de preparación, pero no se habilita
en producción hasta migrar y conciliar la historia. Ningún comprobante se resta
dos veces: las aplicaciones Nacional/Internacional comparten el mismo pago y su
suma nunca puede superarlo.

## Regla canónica

- `NACIONAL`: origen `AR` y destino `AR`.
- `INTERNACIONAL`: cualquier otra combinación válida.
- Si falta evidencia histórica, queda `SIN_CLASIFICAR`; no se presume
  internacional por usar DHL/FedEx/UPS.
- El courier no define el ámbito. La ruta sí.

## Flujo

```text
Elegir ámbito
  → cargar ruta y bultos comunes
  → cotizar carriers habilitados de ese ámbito
  → elegir una oferta concreta
  → completar sólo los campos específicos del adapter
  → revalidar precio y datos
  → emitir
  → descargar label
  → guardar solicitud y cargo en el mismo ámbito
```

El formulario base se comparte para remitente, destinatario, contacto, ruta,
bultos, peso, medidas, referencia y valor declarado. Después de elegir la
oferta, cada adapter agrega sus campos:

- Internacional: invoice, unidades comerciales, descripción en inglés, HS,
  origen de fabricación, impuestos/incoterm y razón de exportación.
- OCA: operativa, cuenta, centro de costo, admisión/retiro, sucursal/franja y
  domicilio estructurado.
- Andreani: contrato, código de cliente, B2C/B2B, puerta/sucursal, remito y
  modalidad de retiro.

No se duplicará el formulario completo por courier. Cada integración tendrá
validación propia y jamás podrá hacer fallback silencioso a otra empresa.

## Estado implementado

- Cotizar y Nuevo envío empiezan con selector 🇦🇷 / 🌐.
- El cotizador nacional ya permite elegir cualquiera de las 24 jurisdicciones
  como origen y cualquiera como destino, incluso dos localidades de la misma
  provincia. La ruta común guarda provincia canónica, localidad y CP tradicional
  o CPA completo; también normaliza peso y medidas con coma o punto decimal,
  cantidad de bultos iguales, modalidad domicilio/sucursal y valor declarado
  total en ARS. El DTO deriva CP de cuatro dígitos, volumen por bulto, volumen
  total en cm³/m³ y peso total usando `Decimal`, sin redondeo binario.
- Nacional muestra OCA/Andreani como integración pendiente y no simula
  cotizaciones, coberturas, plazos ni guías. La validación actual prepara la
  entrada que consumirán sus adapters, pero no constituye una tarifa.
- Internacional conserva el flujo vigente.
- Cada tarjeta internacional transporta el courier elegido al wizard.
- Mis envíos abre en Internacional y ofrece pestañas persistentes Nacional /
  Internacional, con conteos, filtros y paginación propios. No existe un listado
  operativo que mezcle ambos ámbitos.
- Home separa actividad nacional e internacional.
- `solicitudes_guia.ambito` se deriva de los países y el cargo automático lo
  copia a `envios.ambito`.
- El backup genera `Envios_Nacionales`, `Envios_Internacionales`,
  `Cuenta_Consolidada`, `Cuenta_Nacional` y `Cuenta_Internacional`; cuando
  corresponde agrega hojas separadas para crédito sin imputar y cargos sin
  clasificar.

## Cuenta corriente

El saldo lateral sigue siendo consolidado. La vista de cuenta muestra, además,
Debe, Haber y Saldo Nacional/Internacional, crédito aprobado todavía sin
imputar y cargos que continúan en cuarentena. `pagos_aplicaciones` permite que
un comprobante se aplique parcialmente a ambos ámbitos sin duplicar crédito;
una solicitud del cliente recién impacta cuando TAURO aprueba el comprobante.

La implementación está lista en código, no en la base productiva. Antes del
despliegue deben ejecutarse el preflight, la migración monetaria, el backfill
con evidencia y la reconciliación por cliente documentados en
`docs/CUENTA_CORRIENTE_POR_AMBITO.md`.

Migración segura:

1. Tomar un snapshot restaurable y ejecutar el preflight read-only.
2. Clasificar historia por países/ruta; usar courier sólo como evidencia de
   nacionales legados.
3. Dejar en cuarentena todo conflicto o fila sin evidencia.
4. Crear la estructura de aplicaciones y convertir dinero a `NUMERIC`.
5. Mantener pagos históricos aprobados sin imputar hasta conciliarlos; validar
   que la suma aplicada nunca exceda el comprobante.
6. Reconciliar el consolidado anterior contra Nacional + Internacional +
   cuarentena, y recién entonces habilitar la nueva vista en producción.

## Para conectar OCA y Andreani

OCA necesita credenciales e-Pak de QA/producción, CUIT, cuenta, operativas,
centros de costo/sucursales y reglas comerciales. Andreani necesita
credenciales QA/producción, código de cliente, contratos, matriz B2C/B2B y
sucursales/retiros habilitados. Ninguno está operativo hasta contar con esos
datos y completar una prueba real de cotizar → emitir → label → retiro.

La futura respuesta de cada adapter debe conservar por separado modalidad
(domicilio o sucursal), servicio, plazo, precio ARS y condiciones/impuestos.
Sólo se podrán comparar ofertas equivalentes. Al elegir una, el portal guardará
un identificador de cotización del servidor y volverá a validarla antes de
emitir; nunca aceptará un precio enviado por query string desde el navegador.

Referencias primarias usadas para definir el contrato neutral:

- OCA e-Pak y su operación `Tarifar_Envio_Corporativo`:
  https://developers.oca.com.ar/epak.html
- Andreani Developers y contrato del cotizador nacional:
  https://developers.andreani.com/document
- Formato CPA de Correo Argentino:
  https://www.correoargentino.com.ar/formularios/cpa
