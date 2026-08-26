# TAURO como plataforma multioperador

## Objetivo

TAURO debe permitir que un cliente conecte sus tiendas y opere distintos
carriers desde una única experiencia, sin cambiar de formulario, cuenta
corriente o tablero cada vez que cambia el operador.

La API del operador es un adapter reemplazable. Las credenciales, contratos,
cuentas EXPO/IMPO, operativas, descuentos y permisos viven en configuración
server-side; nunca en el navegador.

## Catálogo inicial

| Ámbito | Operador | Estado de código | Operación comercial |
|---|---|---|---|
| Internacional | DHL | preparado/operativo según UAT | requiere credenciales productivas y prueba EXPO+IMPO |
| Internacional | FedEx | pendiente | no habilitar hasta certificación |
| Internacional | UPS | pendiente | no habilitar hasta certificación |
| Nacional | Andreani | pendiente | requiere contrato, cliente, modalidades y sucursales |
| Nacional | OCA | pendiente | requiere e-Pak, CUIT, cuenta y operativas |

Para OCA y Andreani se declaran por ahora cotización, emisión, etiqueta y
tracking. Pickup y cancelación sólo se agregarán cuando la documentación y el
contrato comercial confirmen esos endpoints; la interfaz no los promete por
anticipado. UPS también queda sin pickup hasta certificarlo.

Agregar otro operador sólo requiere un nuevo adapter y su `CarrierSpec`; no
debe duplicarse el portal ni el flujo contable.

## Contrato común

Cada adapter implementará estas operaciones:

```text
cotizar(request) -> Quote[]
crear_guia(quote_id, shipment) -> ShipmentResult
obtener_etiqueta(shipment_id) -> LabelResult
crear_recoleccion(shipment_id, pickup) -> PickupResult
cancelar(operation_id) -> CancelResult
rastrear(tracking) -> TrackingResult
```

La interfaz ejecutable vive en `servicios/carrier_adapter.py`. Su registro
arranca vacío de manera deliberada: aparecer en el catálogo o en una pantalla
no alcanza para tocar una API real. El adapter sólo se registra después de
pasar fixtures contractuales, sandbox, UAT y configuración productiva.
La tarifa interna conserva costo del carrier y precio final como campos
separados; `public_quote()` elimina el costo antes de entregar datos a la UI.

El contrato común exige:

- respuestas normalizadas con `carrier`, `servicio`, `precio`, `moneda`,
  `plazo`, `modalidad` y `quote_id` server-side;
- estados explícitos `cotizado`, `sin_tarifa`, `pendiente`, `incierto` y
  `error_definitivo`;
- idempotencia para guía, etiqueta, recolección y cancelación;
- ningún fallback silencioso hacia otro operador;
- revalidación de la cotización antes de emitir;
- costo estimado y costo final separados para calcular rentabilidad.

## Experiencia única del portal

El cliente sigue el mismo flujo:

```text
Conectar tienda
  → importar pedido
  → completar/validar dirección y bultos
  → cotizar operadores del ámbito
  → elegir una oferta
  → pagar o reservar crédito
  → emitir guía
  → descargar etiqueta
  → pedir recolección
  → seguir envío
  → ver cuenta corriente y margen
```

Los campos comunes son remitente, destinatario, contacto, ruta, bultos,
peso, medidas, valor y modalidad. Después de elegir la oferta, el adapter
agrega sólo sus campos específicos:

- Internacional: invoice, HS, unidades comerciales, origen de fabricación,
  impuestos e incoterm.
- OCA: operativa, cuenta, centro de costo, sucursal y modalidad.
- Andreani: contrato, código de cliente, B2C/B2B, puerta/sucursal y remito.

## Panel de TAURO

El admin debe configurar por cliente y operador:

- cotizar, emitir, etiqueta y recolección;
- markup fijo, porcentual o multiplicador;
- límite de crédito y forma de pago;
- cuenta EXPO/IMPO, contrato, operativa y sucursales;
- remitente predeterminado y perfil aduanero;
- estado operativo y fecha de última prueba;
- motivo visible cuando la integración está pendiente.

## Orden de implementación

1. Certificar DHL con una cuenta EXPO y una IMPO reales.
2. Conectar credenciales sin cambiar el portal ni el esquema de precios.
3. Implementar adapters OCA y Andreani contra QA y fixtures contractuales.
4. Ejecutar cotizar → emitir → etiqueta → recolección por cada modalidad.
5. Activar clientes sólo mediante la matriz del admin.
6. Incorporar FedEx, UPS u otro operador siguiendo el mismo contrato.

No se publican precios simulados ni se habilita emisión porque exista una
clase de cliente. La presencia del adapter y la disponibilidad productiva son
controles separados.

## Gate de activación por operador

Un operador sólo cambia de `pendiente` a `operativo` cuando todos estos puntos
quedan documentados y aprobados:

| Control | Evidencia mínima |
|---|---|
| Contrato técnico | fixtures versionados y pruebas del adapter sin red |
| Credenciales QA | cotización y rechazo controlado contra sandbox/QA |
| Credenciales productivas | entorno, cuenta y permisos verificados sin exponer secretos |
| Cotización | costo, moneda, impuestos, servicio, modalidad y vigencia reconciliados |
| Emisión | idempotencia probada; guía, tracking y etiqueta recuperables |
| Operaciones posteriores | pickup/cancelación sólo si el contrato los soporta |
| Incertidumbre | timeout/5xx deriva a conciliación; nunca a reintento ciego |
| Cuenta corriente | costo y precio final quedan separados por cliente y ámbito |
| Permisos | alta opt-in desde admin; ninguna cuenta hereda emisión automáticamente |
| UAT | recorrido real supervisado y evidencia archivada antes del piloto |

El orden seguro de encendido es: registrar adapter → QA → UAT → cargar
credenciales productivas → habilitar cotización para un cliente piloto →
habilitar emisión → habilitar operaciones posteriores. Cargar una variable de
entorno por sí sola nunca saltea este orden.

## Datos pendientes por integración

- **DHL:** key/secret productivos, cuenta EXPO, cuenta IMPO, validación de
  descuentos, rate → shipment → label y pickup/cancelación donde corresponda.
- **FedEx:** credenciales y cuenta productivas, servicios contratados, recargos,
  rating real, emisión, etiqueta y operaciones posteriores certificadas.
- **UPS:** credenciales y cuenta productivas, productos habilitados, billing y
  certificación de emisión/etiqueta; pickup queda fuera hasta confirmarlo.
- **Andreani:** cliente, contratos por modalidad, sucursales/puntos habilitados,
  definición de costo con/sin IVA, QA de tarifa y orden de envío.
- **OCA:** CUIT, cuenta, operativas, centros de costo/sucursales, e-Pak QA/prod,
  tarifa corporativa y emisión certificada.

## Modelo de negocio

TAURO puede cobrar por el servicio de operación, no solamente trasladar el
precio del courier:

- margen por envío;
- abono o mínimo mensual para clientes integrados;
- onboarding e integración especial;
- gestión documental/aduanera;
- reclamos, rescates y excepciones.

La rentabilidad se mide por envío:

```text
contribución = precio cobrado
             - factura final del carrier
             - costo financiero
             - costo operativo
             - reclamos/seguro
```

No se debe habilitar un operador nuevo hasta poder reconciliar esa cuenta por
tracking y por cliente.
