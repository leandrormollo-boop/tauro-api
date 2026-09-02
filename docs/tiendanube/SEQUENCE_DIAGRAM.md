# Diagrama de secuencia · TAURO Solutions Ar

Este es el artefacto técnico base para homologación. No existe polling de
pedidos: la sincronización parte de webhooks.

```mermaid
sequenceDiagram
    autonumber
    actor M as Merchant
    participant TN as Tiendanube
    participant API as API TAURO
    participant DB as PostgreSQL
    participant C as Courier nacional

    M->>TN: Instalar TAURO Solutions Ar
    TN->>API: GET callback?code=...
    API->>TN: POST OAuth token
    TN-->>API: access_token + store_id
    API->>DB: Alta/rotación de instalación
    API->>TN: Registrar webhooks idempotentes
    API->>TN: Crear Shipping Carrier + option
    API->>DB: Guardar IDs + hash del callback token
    API-->>M: Crear o vincular cuenta TAURO

    Note over TN,C: Cotización de checkout (máximo 5 s)
    TN->>API: POST rates/{token} con carrito
    API->>DB: Validar tienda, estado y token
    API->>C: Cotizar con contrato nacional
    C-->>API: Costo, servicio y plazo
    API->>API: Aplicar precio TAURO y free shipping
    API-->>TN: rates (precio final, SLA, reference)

    Note over TN,API: Venta y preparación
    TN->>API: Webhook order/created firmado
    API->>DB: Persistir evento idempotente
    API-->>TN: 200 después de persistir
    API->>TN: GET order por ID
    TN-->>API: Pedido completo
    API->>DB: Crear solicitud pendiente
    M->>API: Revisar y autorizar emisión
    API->>DB: Mantener emisión bloqueada hasta aprobar UAT OCA
    Note over API,C: Emisión, etiqueta y tracking se habilitan sólo después de UAT

    alt app/suspended
        TN->>API: Webhook firmado
        API->>DB: Marcar suspendida
        API-->>TN: 200
    else app/uninstalled
        TN->>API: Webhook firmado
        API->>DB: Revocar instalación y carrier local
        API-->>TN: 200
    else solicitud de eliminación
        TN->>API: Webhook de privacidad firmado
        API->>DB: Encolar y ejecutar retención/anonimización
        API-->>TN: 200 después de persistir
    end
```

El diagrama refleja el release candidate actual. La cotización, recepción del
pedido y revisión manual están implementadas. La emisión de guías, cancelación,
retiro y tracking permanecen deliberadamente bloqueados hasta completar las
credenciales QA, la prueba UAT y la aprobación operativa de OCA. Recién entonces
se ampliará este flujo y se registrará `callback_labels_url` en Tiendanube.

## Datos que no salen de TAURO

- Costo contractual del courier.
- Markup y reglas comerciales.
- Credenciales del courier.
- Client secret y access tokens de Tiendanube.
- Documentos o datos personales no necesarios para el checkout.
