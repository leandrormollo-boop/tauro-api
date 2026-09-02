# FAQ de homologación Shipping · TAURO Solutions Ar

Documento candidato basado en la plantilla oficial de métodos de envío. Los
campos `[DEFINIR]` deben aprobarse antes de compartirlo con Tiendanube.

## 1. General

### 1.1. ¿Qué método logístico utiliza y para qué sirve?

TAURO Solutions Ar integra Tiendanube con la operación nacional de TAURO
Solutions. Cotiza entregas a domicilio en Argentina, importa los pedidos que
eligieron esa opción y centraliza su gestión operativa.

### 1.2. ¿Qué problema resuelve?

Evita copiar pedidos y domicilios manualmente, calcula el envío en checkout y
mantiene el despacho y el tracking vinculados con la orden original.

### 1.3. ¿Para qué comercios se recomienda?

Comercios argentinos con productos físicos que informen peso y las tres
dimensiones de cada variante, y cuyos envíos cumplan las condiciones
contractuales del operador nacional habilitado.

### 1.4. ¿Dónde funciona?

Primera versión: rutas nacionales Argentina→Argentina con entrega a domicilio,
sujetas a cobertura real del operador. No cotiza envíos internacionales.

### 1.5. ¿Quién la desarrolló y cuáles son los canales de soporte?

- Desarrollador: TAURO Solutions, Buenos Aires, Argentina.
- Nivel 1: `cotizaciones@taurosolutions.ar` — horario `[DEFINIR]`, SLA `[DEFINIR]`.
- Nivel 2 y técnico: `integraciones@taurosolutions.ar` — horario `[DEFINIR]`, SLA `[DEFINIR]`.
- Comercial: `[DEFINIR]` — horario `[DEFINIR]`, SLA `[DEFINIR]`.
- Financiero: `[DEFINIR]` — horario `[DEFINIR]`, SLA `[DEFINIR]`.

### 1.6. ¿Hay cuenta de prueba?

Se entregará una cuenta TAURO y una tienda demo Tiendanube argentinas, sin
espera de aprobación ni pago obligatorio. Credenciales: `[CREAR Y ENTREGAR POR
CANAL SEGURO]`.

## 2. Planes y precios

- Tabla de precios: `[DEFINIR]`.
- Descuento para comercios Tiendanube: `[DEFINIR]`.
- Prueba gratuita: `[DEFINIR]`.
- Costo de alta: `[DEFINIR]`.
- Condiciones Tiendanube Next: `[DEFINIR]`.

Las tarifas de envío se obtienen de la cuenta contractual del operador y de la
regla comercial aprobada para el cliente; nunca se inventan precios de fallback.

## 3. Instalación

1. El comercio inicia la instalación desde Tiendanube.
2. Acepta únicamente los permisos declarados.
3. Tiendanube redirige el código OAuth al backend de TAURO.
4. TAURO registra los webhooks y un único Shipping Carrier.
5. El comercio inicia sesión o crea su cuenta TAURO y queda vinculado sin
   copiar claves.
6. El medio se configura desde el panel Tiendanube.

Panel y preferencias: `https://taurosolutions.ar/portal/tienda`.

## 4. Operación

- SLA/OTD, pérdidas y extravíos: `[CONFIRMAR CONTRATO OCA]`.
- Alcance de origen y destino: Argentina, entrega domicilio→domicilio en la
  primera versión; cobertura validada en cada cotización.
- El comprador elige TAURO Solutions Ar en checkout.
- Tiendanube notifica el pedido mediante webhook y TAURO importa solamente los
  Fulfillment Orders de su carrier.
- Recibir una venta no emite una guía automáticamente.
- La emisión requiere una acción deliberada del comercio en TAURO.
- Tracking: se informa a Tiendanube cuando el paquete está realmente
  despachado, mediante Fulfillment Orders.
- Cancelación, etiqueta y códigos de tracking OCA: `[IMPLEMENTAR Y VALIDAR UAT]`.
- Exportación CSV: `[DEFINIR]`.
- Mercadería prohibida/restringida: `[CONFIRMAR CONTRATO OCA]`.

## 5. Información del servicio

- Peso y dimensiones máximas: `[CONFIRMAR CONTRATO OCA]`.
- Logística inversa: no incluida en la primera versión, salvo aprobación
  contractual posterior.
- Recolección: `[CONFIRMAR CONTRATO OCA]`.
- Seguro y valor declarado: `[CONFIRMAR CONTRATO OCA]`.
- Forma de pago del envío: `[DEFINIR]`.
- Emisión y cancelación de etiquetas: `[IMPLEMENTAR Y VALIDAR UAT]`.
