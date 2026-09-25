# OCA API Directa · diseño y habilitación

Estado: **adapter completo en código, apagado hasta UAT contractual**.

Fecha de revisión: 25/09/2026.

## Alcance confirmado

La presentación comercial entregada por OCA confirma cotización, creación de
envíos, etiquetas, tracking e informes para entrega a domicilio o sucursal. El
contrato técnico se contrastó con la documentación oficial e-Pak vigente:

- `Tarifar_Envio_Corporativo` para cotizar.
- `IngresoORMultiplesRetiros_v2` para crear una orden.
- `GetPdfDeEtiquetasPorOrdenOrNumeroEnvio*` para obtener PDF A4 o 10x15.
- `AnularOrdenGenerada` para anular una orden.
- `Tracking_Pieza_ConIdEstado` para consultar sus eventos.
- `GetCentroCostoPorOperativa` para descubrir, fuera del callback, los centros
  habilitados para una operativa.

El archivo recibido como “Manual de uso ePak” no contiene el manual: es una
página HTML de inicio de sesión de Microsoft guardada con extensión `.pdf`. Se
debe volver a descargar el PDF real desde el enlace original.

Referencia oficial: <https://developers.oca.com.ar/epak.html>.

## Qué implementa TAURO

`servicios/oca_adapter.py` implementa el contrato neutral multicarrier:

1. Cotización en ARS con pricing nacional estricto por cliente.
2. Creación de una orden e-Pak a partir de un XML ISO-8859-1 construido con
   `ElementTree`, con escape correcto y límites oficiales por campo.
3. Remito determinístico de 30 caracteres derivado de la clave idempotente.
4. Paquetes expandidos con `cant="1"`, tal como exige OCA.
5. Valor por bulto en cero para operativa no asegurada, o prorrateado sin
   perder centavos si la operativa fue configurada como asegurada.
6. Descarga y validación del PDF Base64, con límite de tamaño y firma `%PDF-`.
7. Anulación por número de orden y traducción de los códigos oficiales.
8. Tracking normalizado, ordenado cronológicamente y sin exponer XML crudo.
9. Operativas, centros de costo y confirmación de seguro separados para altas
   normales y devoluciones; nunca se heredan entre sí.

No se implementa `create_pickup` como llamada separada: e-Pak define el retiro
o la admisión dentro de la propia creación. Por eso OCA declara todas las
capacidades nacionales salvo `recolectar` independiente.

## Contrato de entrada para emitir

El método `create_shipment` recibe una estructura neutral como esta:

```python
{
    "origin": {
        "calle": "Av. Corrientes",
        "nro": "1234",
        "cp": "1043",
        "localidad": "CABA",
        "provincia": "Buenos Aires",
        "contacto": "TAURO",
        "email": "operaciones@empresa.com",
    },
    "destination": {
        "calle": "San Martín",
        "nro": "550",
        "cp": "2000",
        "localidad": "Rosario",
        "provincia": "Santa Fe",
        "branch_id": 123,  # sólo para destino sucursal
    },
    "recipient": {
        "first_name": "Ana",
        "last_name": "Pérez",
        "phone": "3415550101",
        "email": "cliente@example.com",
    },
    "packages": [{
        "quantity": 1,
        "weight_kg": "1.25",
        "length_cm": "20",
        "width_cm": "10",
        "height_cm": "5",
    }],
    "declared_value": "50000",
    "pickup_date": "2026-09-18",
    "oca_operation_kind": "normal",  # normal (default) | devolucion
}
```

Las devoluciones requieren `OCA_OPERATIVA_DEVOLUCION` y
`OCA_CENTRO_COSTO_DEVOLUCION` propios, además de
`oca_operation_kind="devolucion"` en el expediente. El adapter rechaza una
operativa o cotización cruzada. El flujo Tiendanube piloto no expone todavía
devoluciones; esta ruta queda reservada para un futuro flujo administrativo.

`OCAAdapter.discover_cost_centers(operation=...)` es un helper explícito de
onboarding. No se ejecuta al arrancar ni dentro de callbacks, no persiste ni
selecciona automáticamente el primer resultado y falla ante cero o varios
centros cuando se solicita uno único. Su resultado debe validarse con OCA antes
de cargarlo en el entorno.

El traductor Tiendanube → contrato neutral ya está implementado detrás de
flags. Revalida que el Fulfillment Order coincida con el snapshot inmutable de
la tarifa (servicio, ruta, bultos, peso, valor e importes) y toma los datos
personales exclusivamente del callback autenticado. La ejecución continúa
bloqueada hasta UAT: el callback permanece durable y fail-closed y no acepta
falsamente una etiqueta.

## Gates de seguridad

Cotizar y ejecutar son permisos independientes:

```text
OCA_ADAPTER_ENABLED=false
OCA_UAT_APPROVED=false
OCA_ENVIRONMENT=qa
OCA_PRODUCTION_APPROVED=false

OCA_FULFILLMENT_ENABLED=false
OCA_FULFILLMENT_UAT_APPROVED=false
OCA_CONFIRM_WITHDRAWAL=false
OCA_CENTRO_COSTO=
OCA_OPERATIVA_SEGURO_CONFIRMADO=false
OCA_OPERATIVA_DEVOLUCION=
OCA_CENTRO_COSTO_DEVOLUCION=
OCA_OPERATIVA_DEVOLUCION_SEGURO_CONFIRMADO=false
```

- En QA, `OCA_CONFIRM_WITHDRAWAL=false` deja la orden en el carrito e-Pak.
- En producción el adapter no permite emitir mientras ese valor siga en
  `false`; la confirmación debe habilitarse de forma explícita.
- Altas y anulaciones no se reintentan automáticamente. Un timeout se clasifica
  como `OCAOutcomeUnknown` y se concilia por remito antes de cualquier reintento.
- No se usaron las credenciales públicas de ejemplo de OCA ni se ejecutó ninguna
  operación externa durante el desarrollo.

## Datos contractuales pendientes

Solicitar o confirmar con OCA:

1. CUIT y número de cuenta de TAURO habilitados.
2. Usuario y contraseña e-Pak propios para QA y producción.
3. Una operativa por modalidad que TAURO vaya a publicar.
4. Centro de costo asociado a cada operativa, seleccionado explícitamente si
   el servicio devuelve más de uno.
5. Centro de imposición si el origen es sucursal.
6. Confirmación de seguro independiente para cada operativa.
7. PDF real del manual y contacto técnico de homologación.

Nunca copiar a TAURO las credenciales de prueba publicadas en la documentación.

## UAT obligatorio

Ejecutar en este orden con la cuenta contractual de QA:

1. Cotizar domicilio → domicilio y validar costo/SLA contra e-Pak.
2. Crear con `ConfirmarRetiro=false` y verificar que queda en el carrito.
3. Repetir la misma clave idempotente y conciliar por `nroremito`; no aceptar
   duplicados.
4. Si OCA habilita devoluciones en QA, validar su operativa/centro separados y
   la etiqueta con logística inversa explícita.
5. Descargar PDF A4 y 10x15; validar tracking y legibilidad.
6. Consultar tracking de una pieza real de QA.
7. Anular una orden en estado permitido y validar código `100`.
8. Simular timeout de alta/anulación y confirmar cuarentena sin reintento.
9. Guardar fixtures anonimizados de todas las respuestas.
10. Activar el worker de Labels API sólo en QA y ejecutar el flujo completo
   Tiendanube → snapshot → alta OCA → PDF → descarga/cancelación.
11. Habilitar producción con aprobación explícita y rollback documentado.

## Registro de control

- Ruta de modelo: `api_release_preparation` con regla de seguridad/producción.
- Modelo seleccionado por el router determinístico: `gpt-5.6-sol`, esfuerzo
  alto por arquitectura, seguridad e impacto financiero/operativo.
- Evidencia: presentación comercial OCA, WSDL QA/producción y documentación
  oficial e-Pak vigente.
- Resultado: implementación local y tests con respuestas simuladas; sin
  despliegue, sin credenciales reales y sin escrituras en OCA.
