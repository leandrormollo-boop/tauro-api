# Portal por ámbito: Nacional / Internacional

Decisión de producto del 15/08/2026: el cliente nunca opera sobre una lista o
cotización que mezcle envíos nacionales e internacionales. La cuenta corriente
queda rotulada como consolidada hasta completar la aplicación de pagos por
ámbito; no se simulan dos saldos usando dos veces el mismo comprobante.

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
- Mis envíos abre como selector de ámbito; no existe un listado operativo
  “Todos”.
- Home separa actividad nacional e internacional.
- `solicitudes_guia.ambito` se deriva de los países y el cargo automático lo
  copia a `envios.ambito`.
- El backup genera `Envios_Nacionales` y `Envios_Internacionales`; la hoja
  financiera se rotula `Cuenta_consolidada` hasta completar las aplicaciones
  de pagos.

## Cuenta corriente

El saldo lateral seguirá siendo consolidado y debe estar rotulado como tal.
La separación contable completa requiere `pagos_aplicaciones`: un comprobante
puede aplicarse parcialmente a cargos nacionales e internacionales sin
duplicar el crédito. Hasta implementar y conciliar esa tabla, no se deben
mostrar dos saldos que resten el mismo pago.

Migración segura:

1. Agregar columnas nullable y dual-write para operaciones nuevas.
2. Clasificar historia por países/ruta; usar courier sólo como evidencia de
   nacionales legados.
3. Dejar en cuarentena todo conflicto o fila sin evidencia.
4. Crear aplicaciones de pagos con evidencia y validar que su suma nunca
   exceda el comprobante.
5. Recién entonces activar Cuenta nacional / Cuenta internacional, exports y
   hojas separadas.

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
